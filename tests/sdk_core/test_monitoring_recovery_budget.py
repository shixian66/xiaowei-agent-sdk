"""失败后的补查只共享轮次总预算，不共享源级两次修正额度。"""

import asyncio
import json
import logging
import re

import pytest
from agents.tool_context import ToolContext
from sqlalchemy.engine import URL
from tests.sdk_core.synthetic_tools import Clock, Grants, context, ready_engine, store
from tests.sdk_core.test_monitoring_r1a import SOURCE, TOOL, monitoring_config
from tests.sdk_core.test_runtime import serve_config

from xiaowei.governance import GovernedTools, PromQLExpressionError, ToolCatalog, ToolRejectedError
from xiaowei.models import ToolObservation, ToolRequest
from xiaowei.runtime import ServeConfig, _monitoring_catalog
from xiaowei.tools import governed_function_tool

pytestmark = pytest.mark.loopback


async def test_five_parallel_corrections_reach_io_until_total_budget(
    postgres_url: URL,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="xiaowei.governance")
    config = ServeConfig.model_validate(
        serve_config(8501, **monitoring_config("http://127.0.0.1:9000/mcp"))
    )
    contracts, policies = _monitoring_catalog(config)
    catalog = ToolCatalog(contracts, policies)
    clock = Clock()
    grants = Grants({("alice", SOURCE, TOOL)})
    ctx = context(targets=frozenset({SOURCE}), tools=frozenset({TOOL}), max_tool_calls=11)
    calls: list[str] = []
    release = asyncio.Event()

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
        if expression.startswith("bad"):
            raise PromQLExpressionError(1, 4)
        await release.wait()  # 所有五个正确查询先进入 I/O，不能靠成功清理状态蒙混过关。
        return ToolObservation(
            payload={"query": expression, "result": "up => 1 @[1720000000]", "warnings": None},
            captured_at=clock(),
            truncated=False,
        )

    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, clock, catalog))
        for index in range(5):
            with pytest.raises(PromQLExpressionError) as failure:
                await governed.invoke(ctx, request(f"bad{index}("), execute)
            assert failure.value.correction_allowed
        batch = asyncio.gather(
            *(governed.invoke(ctx, request(f"metric{index}"), execute) for index in range(5)),
            return_exceptions=True,
        )
        await asyncio.sleep(0)  # Grants 授权不挂起；五个任务均在执行等待点或已被拒绝。
        release.set()
        results = await asyncio.wait_for(batch, 2)
        assert all(not isinstance(result, BaseException) for result in results), results
        assert len(governed.turn_runs(ctx.identity).produced) == 5
        with pytest.raises(ToolRejectedError):
            await governed.invoke(ctx, request("bad0("), execute)
        assert len(calls) == 10  # 重复失败请求不占预算也不发 I/O。
        with pytest.raises(PromQLExpressionError) as last:
            await governed.invoke(ctx, request("bad_last("), execute)
        assert not last.value.correction_allowed
        with pytest.raises(ToolRejectedError) as exhausted:
            await governed.invoke(ctx, request("another_metric"), execute)
        assert exhausted.value.code == "budget_exhausted"
        assert len(calls) == 11
        assert len(governed.turn_runs(ctx.identity).monitoring_failures) == 6
        failures = [
            record.message for record in caplog.records if "monitoring_read " in record.message
        ]
        assert len(failures) == 6
        assert all("status=invalid_expression" in line and "tool_id=" in line for line in failures)
        assert "correction_allowed=False" in failures[-1]
        assert not any("bad0(" in line for line in failures)


@pytest.mark.parametrize("rejected", [False, True])
async def test_provider_call_id_is_not_logged_as_plaintext(
    postgres_url: URL,
    caplog: pytest.LogCaptureFixture,
    rejected: bool,
) -> None:
    caplog.set_level(logging.INFO)
    config = ServeConfig.model_validate(
        serve_config(8501, **monitoring_config("http://127.0.0.1:9000/mcp"))
    )
    contracts, policies = _monitoring_catalog(config)
    catalog = ToolCatalog(contracts, policies)
    grants = Grants(set() if rejected else {("alice", SOURCE, TOOL)})
    ctx = context(targets=frozenset({SOURCE}), tools=frozenset({TOOL}))
    calls: list[str] = []

    async def execute(request: ToolRequest) -> ToolObservation:
        calls.append(request.call_id)
        raise PromQLExpressionError(1, 4)

    call_id = "id\nforged-event private-selector=synthetic-private-value"
    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock(), catalog))
        tool = governed_function_tool(f"{SOURCE}__query", contracts[0], governed, execute)
        tool_ctx = ToolContext(
            ctx,
            tool_name=tool.name,
            tool_call_id=call_id,
            tool_arguments='{"query":"bad("}',
        )
        output = json.loads(await tool.on_invoke_tool(tool_ctx, tool_ctx.tool_arguments))
        assert output["status"] == ("rejected" if rejected else "invalid_expression")
        assert calls == ([] if rejected else [call_id])  # 业务调用ID保留，拒绝时零 I/O。
    messages = [
        record.message
        for record in caplog.records
        if record.name
        in {
            "xiaowei.governance",
            "xiaowei.tools",
        }
    ]
    assert len(messages) == 1
    assert "synthetic-private-value" not in messages[0]
    assert "forged-event" not in messages[0] and "\n" not in messages[0]
    assert re.search(r"call_ref=[0-9a-f]{16}(?: |$)", messages[0])
