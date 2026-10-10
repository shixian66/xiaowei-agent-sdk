"""PromQL 修正的并发记账；真治理/Evidence/PG，受控 Adapter 用于精确安排完成顺序。"""

import asyncio

import pytest
from sqlalchemy.engine import URL
from tests.sdk_core.synthetic_tools import Clock, Grants, context, ready_engine, store
from tests.sdk_core.test_monitoring_r1a import SOURCE, TOOL, monitoring_config
from tests.sdk_core.test_runtime import serve_config

from xiaowei.governance import GovernedTools, PromQLExpressionError, ToolCatalog, ToolRejectedError
from xiaowei.models import ToolObservation, ToolRequest
from xiaowei.runtime import ServeConfig, _monitoring_catalog

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize("repair_in_flight", [False, True])
async def test_older_success_cannot_clear_later_syntax_failure(
    postgres_url: URL, repair_in_flight: bool
) -> None:
    config = ServeConfig.model_validate(
        serve_config(8501, **monitoring_config("http://127.0.0.1:9000/mcp"))
    )
    contracts, policies = _monitoring_catalog(config)
    catalog = ToolCatalog(contracts, policies)
    clock = Clock()
    grants = Grants({("alice", SOURCE, TOOL)})
    ctx = context(targets=frozenset({SOURCE}), tools=frozenset({TOOL}), max_tool_calls=10)
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    def request(expression: str) -> ToolRequest:
        return ToolRequest(
            target_id=SOURCE,
            tool_id=TOOL,
            tool_name=f"{SOURCE}__query",
            call_id=f"call-{len(calls)}",
            arguments={"query": expression},
        )

    async def execute(request: ToolRequest) -> ToolObservation:
        expression = str(request.arguments["query"])
        calls.append(expression)
        if expression == "older_success":
            entered.set()
            await release.wait()
        if expression.startswith("bad"):
            raise PromQLExpressionError(1, 4)
        return ToolObservation(
            payload={"query": expression, "result": "up => 1 @[1720000000]", "warnings": None},
            captured_at=clock(),
            truncated=False,
        )

    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, clock, catalog))
        if repair_in_flight:
            with pytest.raises(PromQLExpressionError):
                await governed.invoke(ctx, request("bad_first("), execute)
        pending = asyncio.create_task(governed.invoke(ctx, request("older_success"), execute))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            with pytest.raises(PromQLExpressionError):
                await governed.invoke(ctx, request("bad_later("), execute)
            release.set()
            assert (await pending).evidence_id
            runs = governed.turn_runs(ctx.identity)
            assert runs.promql_pending == frozenset({SOURCE})
            if repair_in_flight:
                assert runs.promql_repairs == (SOURCE, SOURCE)
                with pytest.raises(ToolRejectedError, match="两次PromQL修正机会已用完"):
                    await governed.invoke(ctx, request("new_query"), execute)
                assert calls == ["bad_first(", "older_success", "bad_later("]
            else:
                assert runs.promql_repairs == ()
                assert (await governed.invoke(ctx, request("new_query"), execute)).evidence_id
                assert governed.turn_runs(ctx.identity).promql_repairs == (SOURCE,)
                assert not governed.turn_runs(ctx.identity).promql_pending
                assert calls == ["older_success", "bad_later(", "new_query"]
        finally:
            release.set()
            await pending
