"""P2.5 Task 6：搜表与表结构续取、多库慢查询经正式装配的成功与关键失败场景（离线）。

正式装配 ``runtime.open_runtime`` + ``ChannelService`` + 隔离的真实 PostgreSQL；只替换最底层 I/O
（同 ``test_evidence_scope``）：模型是按消息分派的 HTTP 脚本，StarRocks 是 recording 驱动替身。
替身能证明工具参数与续取游标的检查、每条审计记录按自身会话当前库解析、截断在事实区的显示、依赖
记录与撤权后的历史拒绝；不能证明 AuditLoader 的实际写入与 StarRocks 的权限行为（见 ``tests/p1b``
的真实用例），也不能证明真实模型会这样选择参数。
"""

import json
from typing import Any

import pytest
from agents.testing import function_call
from tests.p1b.test_starrocks_adapter import SCHEMA_TABLES, Result
from tests.sdk_core.test_app import ModelCall, cite, tool_call
from tests.sdk_core.test_evidence_scope import (
    AUDIT_TABLE,
    Env,
    Outbox,
    audit_config,
    ref,
)
from tests.sdk_core.test_evidence_scope import env as env  # pytest fixture
from tests.sdk_core.test_runtime import AUDIT_SOURCE, audit_record
from tests.sdk_core.test_starrocks_tools import tool_outputs

from xiaowei import runtime
from xiaowei.channel import ResultUnavailableError, ResultUnverifiableError
from xiaowei.starrocks import probe_sql

pytestmark = pytest.mark.loopback

# shop 与 archive 都有 sales（同名表）；archive.sales 只有两列。
ARCHIVE = ("archive", "sales")
TABLES = {
    **SCHEMA_TABLES,
    ARCHIVE: (("region", "varchar", "YES", None), ("total", "int", "YES", None)),
}
AUDIT_ROWS = Result(
    AUDIT_SOURCE,
    [
        audit_record("SELECT region, total FROM sales", "q-shop", db="shop"),
        audit_record("SELECT region, total FROM sales", "q-archive", db="archive"),
        audit_record("SELECT region FROM sales", "q-no-db", db=""),  # 没有当前库：不列出
        audit_record("SELECT region FROM archive.sales", "q-qualified", db=""),
        audit_record("SELECT note FROM sales", "q-wrong-db", db="archive"),  # archive.sales 无 note
    ],
)


def list_slow(database: str | None = None) -> Any:
    return tool_call(
        "list_slow_queries",
        cluster="sr-test",
        window_minutes=60,
        order_by="query_time",
        database=database,
    )


def audited(env: Env, **audit: object) -> runtime.ServeConfig:
    env.db.tables = TABLES
    env.db.query = AUDIT_ROWS
    env.db.overrides = {probe_sql(*AUDIT_TABLE): Result(("1",))}
    config = audit_config(env.port)
    (target,) = config["targets"]
    target["starrocks"]["audit"] |= audit
    return runtime.ServeConfig.model_validate(config)


async def delivered(env: Env, rt: runtime.Runtime, request_id: str) -> str:
    outbox = Outbox()
    assert await rt.service.results.send(ref(request_id), outbox) == "failed"
    (delivery,) = outbox.sent
    return delivery.content


async def test_slow_queries_from_every_database_resolve_in_their_own_database(env: Env) -> None:
    config = audited(env)
    async with env.opened(config) as rt:
        message = env.scripts.add("各库最近的慢查询", list_slow(), cite())
        record = await env.turn(rt, message, "om_1")
        assert record.state == "completed", record.failure_code
        content = await delivered(env, rt, "om_1")
        # 同名表按各自记录的库解析；没有当前库的未限定原文与错库的列不列出。
        for shown in ("q-shop", "q-archive", "q-qualified"):
            assert shown in content, shown
        for hidden in ("q-no-db", "q-wrong-db"):
            assert hidden not in content, hidden
        # 只读了一次审计表，未按库过滤（读取全部库的候选）。
        audit = [sql for sql in env.db.business() if "starrocks_audit_tbl__" in sql]
        assert len(audit) == 1 and "AND db = %s" not in audit[0]

        # 关键失败：撤销 archive.sales 后，引用它的历史事实不再交付（含跨库与按 Db 解析的行）。
        env.db.revoked = {ARCHIVE}
        env.db.forget()
        with pytest.raises(ResultUnavailableError) as refused:
            await rt.service.results.view(ref("om_1"))
        assert not isinstance(refused.value, ResultUnverifiableError)
        assert env.db.business() == []
        assert probe_sql(*ARCHIVE) in env.db.probes()


async def test_revoked_database_rows_are_left_out_of_a_new_listing(env: Env) -> None:
    config = audited(env)
    async with env.opened(config) as rt:
        env.db.revoked = {ARCHIVE}  # 快照之后撤权：引用 archive.sales 的行在交付前移除
        message = env.scripts.add("只看 archive 库", list_slow("archive"), cite())
        record = await env.turn(rt, message, "om_1")
        assert record.state == "completed", record.failure_code
        content = await delivered(env, rt, "om_1")
        assert "q-shop" in content  # 替身不按库过滤：Adapter 只绑定库名，过滤由数据库完成
        assert "q-archive" not in content and "q-qualified" not in content
        (audit,) = [sql for sql in env.db.business() if "starrocks_audit_tbl__" in sql]
        assert "AND db = %s" in audit and "archive" not in audit


async def test_a_list_cut_at_max_rows_says_so_in_the_delivered_facts(env: Env) -> None:
    config = audited(env, max_rows=2)  # 三条获准的记录，只列前两条
    async with env.opened(config) as rt:
        message = env.scripts.add("各库最近的慢查询", list_slow(), cite())
        record = await env.turn(rt, message, "om_1")
        assert record.state == "completed", record.failure_code
        (call,) = env.scripts.calls[message][1:2]
        envelope = json.loads(tool_outputs(call)[-1])
        assert envelope["truncated"] is True and len(envelope["data"]["rows"]) == 2
        content = await delivered(env, rt, "om_1")
        assert "结果已截断" in content
        assert "q-shop" in content and "q-archive" in content and "q-qualified" not in content


# ---- 搜表与表结构：续取与参数修正 ---------------------------------------------------------------


def search(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "cluster": "sr-test",
        "keyword": "sales",
        "database": None,
        "page_size": 1,
        "cursor": None,
    }
    return base | changes


def following(call: ModelCall) -> str:
    """模型输入中最近一次成功结果的 next_cursor（被拒的输出是文字说明，跳过）。"""
    for output in reversed(tool_outputs(call)):
        if output.startswith("{"):
            return str(json.loads(output)["data"]["next_cursor"])
    raise AssertionError("没有成功的结果")


def continue_with(**changes: object) -> Any:
    def step(call: ModelCall) -> list[Any]:
        arguments = search(cursor=following(call)) | changes
        return [function_call("list_tables", arguments, call_id=f"call-next-{len(changes)}")]

    return step


async def test_search_continues_after_corrected_calls(env: Env) -> None:
    env.db.tables = TABLES
    async with env.opened(env.config()) as rt:
        message = env.scripts.add(
            "找 sales 表",
            tool_call("list_tables", **search(page_size=999)),  # 过大页：I/O 前拒绝，可修正
            tool_call("list_tables", **search()),
            continue_with(keyword="SALES", database="shop"),  # 续页换了库过滤：拒绝，可修正
            continue_with(),  # 原样传回游标、参数不变
            cite(),
        )
        record = await env.turn(rt, message, "om_1")
        assert record.state == "completed", record.failure_code
        outputs = [tool_outputs(c)[-1] for c in env.scripts.calls[message][1:5]]
        oversized, first, mismatched, second = outputs
        assert "page_size" in oversized and "evidence_id" not in oversized
        assert "cursor 与本次参数不符" in mismatched and "evidence_id" not in mismatched
        pages = [json.loads(o) for o in (first, second)]
        rows = [p["data"]["rows"] for p in pages]
        assert [(r[0]["database"], r[0]["name"]) for r in rows] == [ARCHIVE, ("shop", "sales")]
        assert [p["truncated"] for p in pages] == [True, False]
        assert pages[1]["data"]["next_cursor"] is None
        # 每页探测本页的对象（同一列表中另有快照刷新时的探测）；被拒的调用没有 I/O。
        assert set(env.db.probes()) >= {probe_sql(*ARCHIVE), probe_sql("shop", "sales")}
        assert env.db.business() == []


WIDE = ("shop", "wide")
# 每列约 100 字节：结果上限（1000 字节）一次放得下约 9 列，20 列分三次（每轮至多 3 次工具调用）。
WIDE_COLUMNS = tuple((f"c{i:02d}_" + "y" * 40, "varchar", "YES", None) for i in range(20))


def describe_next(call: ModelCall) -> list[Any]:
    arguments = {"cluster": "sr-test", "database": "shop", "table": "wide"}
    cursor = following(call)
    return [function_call("describe_table", arguments | {"cursor": cursor}, call_id=cursor)]


async def test_columns_of_a_wide_table_are_fetched_past_the_first_result(env: Env) -> None:
    env.db.tables = {**TABLES, WIDE: WIDE_COLUMNS}
    async with env.opened(env.config()) as rt:
        message = env.scripts.add(
            "wide 表最后一列是什么",
            tool_call(
                "describe_table", cluster="sr-test", database="shop", table="wide", cursor=None
            ),
            describe_next,
            describe_next,
            cite(),
        )
        record = await env.turn(rt, message, "om_1")
        assert record.state == "completed", record.failure_code
        pages = [json.loads(tool_outputs(c)[-1]) for c in env.scripts.calls[message][1:4]]
        names = [row["name"] for p in pages for row in p["data"]["rows"]]
        assert names == [c[0] for c in WIDE_COLUMNS]  # 含首屏之外的最后一列，无重复
        assert [p["truncated"] for p in pages] == [True, True, False]
        assert env.db.business() == []
