"""P2.5 Task 6：搜表分页与多库慢查询经正式装配的成功与关键失败场景（离线）。

正式装配 ``runtime.open_runtime`` + ``ChannelService`` + 隔离的真实 PostgreSQL；只替换最底层 I/O
（同 ``test_evidence_scope``）：模型是按消息分派的 HTTP 脚本，StarRocks 是 recording 驱动替身。
替身能证明工具参数与快照版本的检查、每条审计记录按自身会话当前库解析、依赖记录与撤权后的历史
拒绝；不能证明 AuditLoader 的实际写入与 StarRocks 的权限行为（见 ``tests/p1b`` 的真实用例），也
不能证明真实模型会这样选择参数。
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


def audited(env: Env) -> runtime.ServeConfig:
    env.db.tables = TABLES
    env.db.query = AUDIT_ROWS
    env.db.overrides = {probe_sql(*AUDIT_TABLE): Result(("1",))}
    return runtime.ServeConfig.model_validate(audit_config(env.port))


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


# ---- 搜表：成功翻页与参数修正 -------------------------------------------------------------------


def search(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "cluster": "sr-test",
        "keyword": "sales",
        "database": None,
        "page": 1,
        "page_size": 1,
        "snapshot": None,
    }
    return base | changes


def next_page(call: ModelCall) -> list[Any]:
    """读取模型输入中最后一页的 snapshot，请求第 2 页。"""
    envelope = json.loads(tool_outputs(call)[-1])
    version = envelope["data"]["snapshot"]
    return [function_call("list_tables", search(page=2, snapshot=version), call_id="call-next")]


async def test_search_pages_through_same_named_tables_after_a_corrected_call(env: Env) -> None:
    env.db.tables = TABLES
    async with env.opened(env.config()) as rt:
        message = env.scripts.add(
            "找 sales 表",
            tool_call("list_tables", **search(page_size=999)),  # 过大页：I/O 前拒绝，可修正
            tool_call("list_tables", **search()),
            next_page,
            cite(),
        )
        record = await env.turn(rt, message, "om_1")
        assert record.state == "completed", record.failure_code
        first, second, third = (tool_outputs(c)[-1] for c in env.scripts.calls[message][1:4])
        assert "page_size" in first and "evidence_id" not in first
        rows = [json.loads(o)["data"]["rows"] for o in (second, third)]
        assert [(r[0]["database"], r[0]["name"]) for r in rows] == [ARCHIVE, ("shop", "sales")]
        assert [json.loads(o)["truncated"] for o in (second, third)] == [True, False]
        # 每页只探测本页的一个对象。
        assert set(env.db.probes()) >= {probe_sql(*ARCHIVE), probe_sql("shop", "sales")}
        assert env.db.business() == []
