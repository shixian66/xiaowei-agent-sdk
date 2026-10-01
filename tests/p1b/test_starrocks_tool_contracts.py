"""P1-B Task 3 的离线契约：StarRocks 工具装配的容量检查与最坏结果，不连接任何服务。

装配时把按声明上限构造的最坏结果交给证据的真实投影器检查。这里用 ``json.dumps`` 独立计算
同一信封的大小作为对照：检查恰好卡在该阈值；再用控制字符、引号与反斜杠构造转义膨胀最大的
真实 Adapter 结果，证明最坏结果确实不小于实际结果。
"""

import json
from typing import Any

import pytest
from tests.p1b.test_starrocks_adapter import NOW, POLICY, Result, driver
from tests.p1b.test_starrocks_adapter import TARGET as SR

from xiaowei.governance import Prechecked, Projection, ToolCatalog, ToolPolicy
from xiaowei.models import ToolContract, ToolObservation, ToolRequest
from xiaowei.starrocks import StarRocksAdapter, StarRocksError, StarRocksErrorCode
from xiaowei.starrocks_tools import (
    DESCRIBE_TABLE,
    LIST_TABLES,
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


def request(sql: str) -> ToolRequest:
    return ToolRequest(
        tool_id=RUN_QUERY,
        target_id=SR.target_id,
        call_id="c1",
        tool_name="run_readonly_query",
        arguments={"sql": sql},
    )


async def run_query(ada: StarRocksAdapter, sql: str) -> ToolObservation:
    tools = starrocks_tools(ada, dict.fromkeys(AUDIENCES, needed(ada)))
    execute = tools.executes[RUN_QUERY]
    assert isinstance(execute, Prechecked)
    return await execute.run(execute.check(request(sql)))


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
    tools = starrocks_tools(ada, dict.fromkeys(AUDIENCES, threshold))
    assert {c.tool_id for c in tools.contracts} == {LIST_TABLES, DESCRIBE_TABLE, RUN_QUERY}
    assert all(p.required == RESULT_FIELDS for p in tools.policies)
    ToolCatalog(tools.contracts, tools.policies)  # 登记一致

    for short in AUDIENCES:
        limits = dict.fromkeys(AUDIENCES, threshold)
        limits[short] = threshold - 1
        # 模型与 Session 不足时两条渠道路径都放不下；渠道不足时报该渠道。
        with pytest.raises(ValueError, match="投影放不下"):
            starrocks_tools(ada, limits)
    with pytest.raises(ValueError, match="四种用途"):
        starrocks_tools(ada, dict.fromkeys(AUDIENCES[:3], threshold))


def test_worst_case_grows_with_every_declared_limit() -> None:
    base = needed(adapter())
    assert needed(adapter(max_result_bytes=SR.max_result_bytes + 100)) == base + 100
    bigger_sql = SR.policy.model_copy(update={"max_sql_bytes": SR.policy.max_sql_bytes + 10})
    # SQL 与列名各多 10 个字节，转义后各多 60 个字节。
    assert needed(adapter(policy=bigger_sql)) == base + 120


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
