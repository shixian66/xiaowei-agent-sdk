"""P1-B Task 3 的离线契约：StarRocks 工具装配的容量检查与最坏结果，不连接任何服务。

装配时把按声明上限构造的最坏结果交给证据的真实投影器检查。这里用 ``json.dumps`` 独立计算
同一信封的大小作为对照：检查恰好卡在该阈值；再用控制字符、引号与反斜杠构造转义膨胀最大的
真实 Adapter 结果，证明最坏结果确实不小于实际结果。
"""

# ruff: noqa: S608 —— 本文件的 SQL 是被检样本，拼接是有意的。

import json
from typing import Any

import pytest
from tests.p1b.test_starrocks_adapter import NOW, POLICY, Result, driver, ready_schema
from tests.p1b.test_starrocks_adapter import TARGET as SR

from xiaowei.governance import Prechecked, Projection, ToolCatalog, ToolPolicy, ToolRejectedError
from xiaowei.models import ToolContract, ToolObservation, ToolRequest
from xiaowei.sqlguard import guard_explain_query, guard_readonly_query
from xiaowei.starrocks import EXPLAIN_PREFIX, StarRocksAdapter, StarRocksError, StarRocksErrorCode
from xiaowei.starrocks_schema import SchemaCache
from xiaowei.starrocks_tools import (
    DESCRIBE_TABLE,
    EXPLAIN_QUERY,
    LAYOUT_TOOL,
    LIST_TABLES,
    PAGED_FIELDS,
    RESULT_FIELDS,
    RUN_QUERY,
    RunQueryArgs,
    starrocks_tools,
    worst_case_observation,
)

AUDIENCES = ("model", "session", "web", "feishu")
# 控制字符在 JSON 中写作 \u00XX：一个字节膨胀为六个。
NASTY = "\x01"


def adapter(result: Result | None = None, **target: Any) -> StarRocksAdapter:
    return StarRocksAdapter(
        SR.model_copy(update=target),
        connect=driver(result or Result(("region",), [])),
        clock=lambda: NOW,
    )


def request(sql: str, tool_id: str = RUN_QUERY) -> ToolRequest:
    return ToolRequest(
        tool_id=tool_id,
        target_id=SR.target_id,
        call_id="c1",
        tool_name=tool_id.removeprefix("local/"),
        arguments={"cluster": SR.target_id, "sql": sql},
    )


def unrefreshed(ada: StarRocksAdapter) -> SchemaCache:
    """装配期的容量检查不读快照：给一个从未刷新的缓存即可。"""
    return SchemaCache(ada, clock=lambda: NOW)


async def run_query(ada: StarRocksAdapter, sql: str, tool_id: str = RUN_QUERY) -> ToolObservation:
    schema = await ready_schema(ada)
    tools = starrocks_tools(ada, dict.fromkeys(AUDIENCES, needed(ada)), schema=schema)
    execute = tools.executes[(tool_id, SR.target_id)]
    assert isinstance(execute, Prechecked)
    return await execute.run(execute.check(request(sql, tool_id)))


def envelope_size(observation: ToolObservation) -> int:
    """与证据投影相同的编码；truncated=false 是两种写法中较长的一种。"""
    body = {
        "evidence_id": "ev_" + "f" * 32,
        "data": {name: observation.payload[name] for name in RESULT_FIELDS},
        "truncated": False,
        "empty": False,
    }
    return len(json.dumps(body, ensure_ascii=False).encode())


def needed(ada: StarRocksAdapter) -> int:
    """独立对照：最坏结果按生产编码序列化后的信封大小。"""
    return envelope_size(worst_case_observation(ada))


def test_every_audience_must_hold_the_worst_case_result() -> None:
    ada = adapter()
    threshold = needed(ada)
    tools = starrocks_tools(ada, dict.fromkeys(AUDIENCES, threshold), schema=unrefreshed(ada))
    assert {c.tool_id for c in tools.contracts} == {
        "local/list_databases",
        "local/show_create_table",
        LIST_TABLES,
        DESCRIBE_TABLE,
        RUN_QUERY,
        EXPLAIN_QUERY,
        LAYOUT_TOOL,
    }
    required = {p.policy_id.rsplit(".", 1)[1]: p.required for p in tools.policies}
    # 搜表与表结构另带续取游标：SQL 与列头是固定模板，加上游标仍在查询的最坏结果之内（同一阈值
    # 通过）。
    assert required.pop("list_tables") == required.pop("describe_table") == PAGED_FIELDS
    assert required.pop("list_databases") == PAGED_FIELDS
    assert PAGED_FIELDS == (*RESULT_FIELDS, "next_cursor")
    assert required.pop("show_create_table") == (*RESULT_FIELDS, "message")
    assert set(required.values()) == {RESULT_FIELDS}
    ToolCatalog(tools.contracts, tools.policies)  # 登记一致

    for short in AUDIENCES:
        limits = dict.fromkeys(AUDIENCES, threshold)
        limits[short] = threshold - 1
        # 模型与 Session 不足时两条渠道路径都放不下；渠道不足时报该渠道。
        with pytest.raises(ValueError, match="投影放不下"):
            starrocks_tools(ada, limits, schema=unrefreshed(ada))
    with pytest.raises(ValueError, match="四种用途"):
        starrocks_tools(ada, dict.fromkeys(AUDIENCES[:3], threshold), schema=unrefreshed(ada))


def test_worst_case_grows_with_every_declared_limit() -> None:
    base = needed(adapter())
    assert needed(adapter(max_result_bytes=SR.max_result_bytes + 100)) == base + 100
    bigger_sql = SR.policy.model_copy(update={"max_sql_bytes": SR.policy.max_sql_bytes + 10})
    assert needed(adapter(max_plan_lines=SR.max_plan_lines + 100)) == base
    # SQL 与列名各多 10 个字节，转义后各多 60 个字节。
    assert needed(adapter(policy=bigger_sql)) == base + 120


def test_worst_case_sql_includes_the_explain_prefix() -> None:
    """EXPLAIN 发出的语句是固定前缀加上不超过 ``max_sql_bytes`` 的规范化 SQL。"""
    ada = adapter()
    sql = worst_case_observation(ada).payload["sql"]
    assert isinstance(sql, str)
    assert len(sql.encode()) == SR.policy.max_sql_bytes + len(EXPLAIN_PREFIX)


async def test_explain_at_the_sql_limit_fits_the_worst_case() -> None:
    """规范化 SQL 恰好用满 ``max_sql_bytes``、字面量全是控制字符时，带前缀的计划结果仍在
    装配时假定的上限内，不在 ``_observation`` 被判为结果契约不符。"""
    policy = POLICY.model_copy(update={"max_sql_bytes": 400})
    prefix, suffix = "SELECT region FROM sales WHERE note = '", "'"
    # 规范化会补全库名等：按空字面量规范化后的长度反推，使规范化结果恰好等于上限。
    overhead = len(guard_explain_query(prefix + suffix, policy).normalized_sql.encode())
    sql = prefix + NASTY * (policy.max_sql_bytes - overhead) + suffix
    assert len(guard_explain_query(sql, policy).normalized_sql.encode()) == policy.max_sql_bytes
    plan = Result(("Explain String",), [(NASTY * 40,) for _ in range(10)])
    ada = adapter(plan, policy=policy, max_result_bytes=900)

    observation = await run_query(ada, sql, EXPLAIN_QUERY)

    sent = observation.payload["sql"]
    assert isinstance(sent, str) and sent.startswith(EXPLAIN_PREFIX)
    assert len(sent.encode()) == policy.max_sql_bytes + len(EXPLAIN_PREFIX)
    assert envelope_size(observation) <= needed(ada)


async def test_worst_case_is_an_upper_bound_for_escape_heavy_results() -> None:
    policy = POLICY.model_copy(update={"max_sql_bytes": 400})
    limit = policy.max_sql_bytes
    # 规范化后的 SQL 恰在上限附近，字面量全是控制字符；行的每个值同样如此。
    prefix = "SELECT region, note FROM sales WHERE note = '"
    sql = prefix + NASTY * (limit - len(prefix) - 120) + "'"
    rows = [(NASTY * 30, '"\\' * 15) for _ in range(40)]
    ada = adapter(Result(("region", "note"), rows), policy=policy, max_result_bytes=900)

    observation = await run_query(ada, sql)

    assert observation.truncated and observation.payload["rows"]
    assert len(json.dumps(observation.payload["sql"], ensure_ascii=False)) > 3 * limit
    assert envelope_size(observation) <= needed(ada)


async def test_server_column_labels_beyond_the_assumed_bound_fail_closed() -> None:
    long_label = NASTY * (6 * SR.policy.max_sql_bytes)
    ada = adapter(Result((long_label,), [("x",)]))
    with pytest.raises(StarRocksError) as failed:
        await run_query(ada, "SELECT region FROM sales")
    assert failed.value.code is StarRocksErrorCode.RESULT_CONTRACT


def test_required_fields_must_belong_to_every_projection() -> None:
    projections = {a: Projection(fields=("rows", "sql"), max_bytes=1000) for a in AUDIENCES}
    projections["feishu"] = Projection(fields=("sql",), max_bytes=1000)
    policy = ToolPolicy(
        policy_id="p", arguments=RunQueryArgs, projections=projections, required=("rows",)
    )
    contract = ToolContract(
        tool_id=RUN_QUERY,
        target_id=SR.target_id,
        input_schema=RunQueryArgs.model_json_schema(),
        policy_id="p",
    )
    with pytest.raises(ValueError, match="必需字段"):
        ToolCatalog((contract,), (policy,))


# ---- P2.5 Task 3：结果列数与列名的上界 -------------------------------------------------------


def wider(columns: int, **policy: Any) -> StarRocksAdapter:
    return adapter(policy=SR.policy.model_copy(update={"max_result_columns": columns, **policy}))


def test_worst_case_grows_with_max_result_columns() -> None:
    """每多一列，最坏列头多出一对引号与一个分隔符（列名合计字节不变）。"""
    base = needed(wider(4))
    assert needed(wider(5)) == base + 4
    assert needed(wider(4, max_sql_bytes=SR.policy.max_sql_bytes + 1)) > base


async def test_full_width_escape_heavy_headers_stay_within_the_worst_case() -> None:
    """列数取满、别名全是控制字符且规范化 SQL 恰好用满字节上限：实际结果不超过启动时的最坏值。"""
    columns, limit = 4, 400
    labels = [NASTY * (i + 1) for i in range(columns)]
    projections = ", ".join(f"region AS `{label}`" for label in labels)
    sql = f"SELECT {projections} FROM sales LIMIT 1"
    policy = POLICY.model_copy(update={"max_sql_bytes": limit, "max_result_columns": columns})
    size = len(guard_readonly_query(sql, policy).normalized_sql.encode())
    labels[-1] += NASTY * (limit - size)  # 最后一个别名补满，使规范化 SQL 恰好等于上限
    sql = f"SELECT {', '.join(f'region AS `{label}`' for label in labels)} FROM sales LIMIT 1"
    assert len(guard_readonly_query(sql, policy).normalized_sql.encode()) == limit

    ada = StarRocksAdapter(
        wider(columns, max_sql_bytes=limit).target,
        connect=driver(Result(tuple(labels), [("x",) * columns])),
        clock=lambda: NOW,
    )
    observation = await run_query(ada, sql)

    assert observation.payload["columns"] == labels
    assert envelope_size(observation) <= needed(ada)


async def test_star_expansion_beyond_the_column_limit_never_reaches_the_driver() -> None:
    ada = wider(2)
    schema = await ready_schema(ada)
    tools = starrocks_tools(ada, dict.fromkeys(AUDIENCES, needed(ada)), schema=schema)
    execute = tools.executes[(RUN_QUERY, SR.target_id)]
    assert isinstance(execute, Prechecked)
    with pytest.raises(ToolRejectedError, match="too_many_columns"):
        execute.check(request("SELECT * FROM sales"))  # shop.sales 有 3 列
    assert execute.check(request("SELECT * FROM regions"))  # 恰好 2 列
