"""R7: real Runner/governance/Evidence; synthetic SQL errors and isolated PostgreSQL."""

import asyncio
import json
from typing import Any

import pytest
from agents import Agent, Runner
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from sqlalchemy.engine import URL
from tests.sdk_core.synthetic_tools import (
    QUERY_TOOL,
    TARGET,
    TOTAL_TOOL,
    Clock,
    Grants,
    RecordingAdapter,
    context,
    ready_engine,
    request,
    store,
)

from xiaowei.governance import GovernedTools, Prechecked, ToolRejectedError
from xiaowei.models import RunContext, ToolRequest
from xiaowei.starrocks import StarRocksError, StarRocksErrorCode
from xiaowei.tools import governed_function_tool

pytestmark = pytest.mark.loopback
DONE = ModelStep(output=[assistant_message("done")])
SENTINEL = "R7-PRIVATE-SENTINEL-https://private.invalid"


def feedback(model: ScriptedModel) -> dict[str, Any]:
    items = model.calls[-1].input
    assert isinstance(items, list)
    outputs = [i["output"] for i in items if i.get("type") == "function_call_output"]
    raw = str(outputs[-1])
    assert "Please try again" not in raw
    assert SENTINEL not in raw
    assert raw.startswith("{"), raw
    return json.loads(raw)


@pytest.mark.parametrize(
    "case,code,retry",
    [
        ("args", "invalid_arguments", True),
        ("denied", "not_allowed", False),
        ("scope", "not_allowed", False),
        ("authority", "authorization_unavailable", False),
        ("budget", "budget_exhausted", False),
        ("unsafe_message", "invalid_arguments", True),
    ],
)
async def test_fixed_feedback_stops_or_corrects(
    postgres_url: URL, case: str, code: str, retry: bool
) -> None:
    grants, adapter = Grants(), RecordingAdapter()
    grants.grant("alice", TOTAL_TOOL)
    if case == "authority":

        async def unavailable(*_: Any) -> bool:
            raise RuntimeError(SENTINEL)

        authorize = unavailable
    else:
        authorize = grants
    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, authorize, Clock()))
        ctx = context(tools=frozenset() if case == "scope" else frozenset({TOTAL_TOOL}))
        execute: Any = adapter.execute
        if case == "denied":
            grants.revoke_all()
        if case == "budget":
            ctx = context(max_tool_calls=1)
            await governed.invoke(ctx, request(), adapter.execute)
            adapter.calls.clear()
        if case == "unsafe_message":

            def reject(_: ToolRequest) -> None:
                raise ToolRejectedError(SENTINEL)

            execute = Prechecked(reject, adapter.execute)
        contract = governed.catalog.contract(TOTAL_TOOL, TARGET)
        assert contract is not None
        tool = governed_function_tool("order_total", contract, governed, execute)
        args = {"region": 3} if case == "args" else {"region": "east"}
        model = ScriptedModel([[function_call("order_total", args, call_id="r7-1")], DONE])
        await Runner.run(
            Agent[RunContext](name="R7", model=model, tools=[tool]),
            "query",
            context=ctx,
            max_turns=3,
        )
        output = feedback(model)
        assert output["status"] == "rejected"
        assert output["code"] == code
        assert output["retry_this_turn"] is retry
        assert governed.turn_runs(ctx.identity).rejected == 1
        assert adapter.calls == []


async def test_busy_quota_survives_success_and_tool_or_argument_changes(postgres_url: URL) -> None:
    grants, adapter = Grants(), RecordingAdapter()
    grants.grant("alice", TOTAL_TOOL, QUERY_TOOL)
    attempts = 0

    async def busy(_: ToolRequest) -> Any:
        nonlocal attempts
        attempts += 1
        raise StarRocksError(StarRocksErrorCode.POOL_TIMEOUT, slot_wait=True)

    async with ready_engine(postgres_url) as engine:
        governed, ctx = GovernedTools(store(engine, grants, Clock())), context(max_tool_calls=1)
        with pytest.raises(ToolRejectedError):
            await governed.invoke(ctx, request(), busy)
        assert governed.turn_runs(ctx.identity).started == ()
        await governed.invoke(ctx, request(region="west"), adapter.execute)
        ctx = context(turn="larger", max_tool_calls=4)
        with pytest.raises(ToolRejectedError):
            await governed.invoke(ctx, request(), busy)
        await governed.invoke(ctx, request(region="north"), adapter.execute)
        with pytest.raises(ToolRejectedError):
            await governed.invoke(ctx, request(QUERY_TOOL, region="south"), busy)
        attempts_at_lock = attempts
        for tool, region in [(TOTAL_TOOL, "east"), (QUERY_TOOL, "west"), (TOTAL_TOOL, "new")]:
            with pytest.raises(ToolRejectedError):
                await governed.invoke(ctx, request(tool, region=region), busy)
        assert attempts == attempts_at_lock, "locked target reached Adapter again"
        assert governed.turn_runs(ctx.identity).started == (TOTAL_TOOL,)
        with pytest.raises(ToolRejectedError):
            await governed.invoke(context(turn="fresh"), request(), busy)
        assert attempts == attempts_at_lock + 1


async def test_one_busy_retry_claimed_before_any_await(postgres_url: URL) -> None:
    grants, adapter = Grants(), RecordingAdapter()
    grants.grant("alice", TOTAL_TOOL, QUERY_TOOL)
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = []

    async def busy(_: ToolRequest) -> Any:
        raise StarRocksError(StarRocksErrorCode.POOL_TIMEOUT, slot_wait=True)

    async def held(req: ToolRequest) -> Any:
        attempts.append(req)
        entered.set()
        await release.wait()
        return await adapter.execute(req)

    async with ready_engine(postgres_url) as engine:
        governed, ctx = GovernedTools(store(engine, grants, Clock())), context()
        with pytest.raises(ToolRejectedError):
            await governed.invoke(ctx, request(), busy)
        retry = asyncio.create_task(governed.invoke(ctx, request(region="retry"), held))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        governed.invoke(ctx, request(QUERY_TOOL, region=str(i)), held)
                        for i in range(5)
                    ),
                    return_exceptions=True,
                ),
                2,
            )
            assert all(isinstance(r, ToolRejectedError) for r in results)
            assert len(attempts) == 1
        finally:
            release.set()
            await retry
        assert len(adapter.calls) == 1


async def test_runner_busy_feedback_has_remaining_allowance(postgres_url: URL) -> None:
    grants = Grants()
    grants.grant("alice", TOTAL_TOOL)
    attempts = 0

    async def busy(_: ToolRequest) -> Any:
        nonlocal attempts
        attempts += 1
        raise StarRocksError(StarRocksErrorCode.POOL_TIMEOUT, slot_wait=True)

    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock()))
        contract = governed.catalog.contract(TOTAL_TOOL, TARGET)
        assert contract is not None
        tool = governed_function_tool("order_total", contract, governed, busy)
        model = ScriptedModel(
            [
                [function_call("order_total", {"region": "east"}, call_id="r7-1")],
                [function_call("order_total", {"region": "west"}, call_id="r7-2")],
                [function_call("order_total", {"region": "third"}, call_id="r7-3")],
                DONE,
            ]
        )
        await Runner.run(
            Agent[RunContext](name="R7", model=model, tools=[tool]),
            "query",
            context=context(max_tool_calls=1),
            max_turns=5,
        )
        assert attempts == 2
        output = feedback(model)
        assert output["code"] == "busy"
        assert output["remaining_retries"] == 0
        assert output["retry_this_turn"] is False
        first = model.calls[1].input
        assert isinstance(first, list)
        raw = next(i["output"] for i in first if i.get("type") == "function_call_output")
        assert json.loads(str(raw))["remaining_retries"] == 1


async def test_cancelled_retry_cannot_be_reclaimed(postgres_url: URL) -> None:
    grants = Grants()
    grants.grant("alice", TOTAL_TOOL)
    entered = asyncio.Event()
    attempts = 0

    async def busy(_: ToolRequest) -> Any:
        raise StarRocksError(StarRocksErrorCode.POOL_TIMEOUT, slot_wait=True)

    async def held(_: ToolRequest) -> Any:
        nonlocal attempts
        attempts += 1
        entered.set()
        await asyncio.Event().wait()

    async with ready_engine(postgres_url) as engine:
        governed, ctx = GovernedTools(store(engine, grants, Clock())), context()
        with pytest.raises(ToolRejectedError):
            await governed.invoke(ctx, request(), busy)
        retry = asyncio.create_task(governed.invoke(ctx, request(), held))
        await asyncio.wait_for(entered.wait(), 2)
        retry.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retry
        with pytest.raises(ToolRejectedError):
            await asyncio.wait_for(governed.invoke(ctx, request(region="changed"), held), 1)
        assert attempts == 1


async def test_busy_lock_does_not_block_another_authorized_target(postgres_url: URL) -> None:
    from tests.sdk_core.test_governance import (
        CLUSTERS,
        ROUTED_TOOL,
        _routed_catalog,
        _routed_grants,
    )

    grants, adapter = _routed_grants(*CLUSTERS), RecordingAdapter()
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, Clock(), tools=_routed_catalog())
        governed = GovernedTools(evidence)
        ctx = context(tools=frozenset({ROUTED_TOOL}), targets=frozenset(CLUSTERS))

        def req(target):
            return ToolRequest(
                tool_id=ROUTED_TOOL,
                target_id=target,
                call_id="r7",
                tool_name="region_total",
                arguments={"cluster": target, "region": "east"},
            )

        async def busy(_):
            raise StarRocksError(StarRocksErrorCode.POOL_TIMEOUT, slot_wait=True)

        for _ in range(2):
            with pytest.raises(ToolRejectedError):
                await governed.invoke(ctx, req(CLUSTERS[0]), busy)
        await governed.invoke(ctx, req(CLUSTERS[1]), adapter.execute)
        assert [call.target_id for call in adapter.calls] == [CLUSTERS[1]]


async def test_budget_and_authorization_rejections_do_not_spend_busy_retry(
    postgres_url: URL,
) -> None:
    grants, adapter = Grants(), RecordingAdapter()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock()))
        ctx = context(max_tool_calls=1)

        async def busy(_):
            raise StarRocksError(StarRocksErrorCode.POOL_TIMEOUT, slot_wait=True)

        with pytest.raises(ToolRejectedError):
            await governed.invoke(ctx, request(), busy)
        grants.revoke_all()
        with pytest.raises(ToolRejectedError) as rejected:
            await governed.invoke(ctx, request(), adapter.execute)
        assert rejected.value.code == "not_allowed"
        assert governed.turn_runs(ctx.identity).slot_retries == frozenset()
        grants.grant("alice", TOTAL_TOOL)
        await governed.invoke(ctx, request(), adapter.execute)
        # Successful retry spends both the ordinary budget and the one-shot allowance.
        with pytest.raises(ToolRejectedError) as rejected:
            await governed.invoke(ctx, request(), adapter.execute)
        assert rejected.value.code == "budget_exhausted" and len(adapter.calls) == 1
        assert governed.turn_runs(ctx.identity).slot_retries == frozenset({TARGET})


async def test_late_retry_cancellation_after_turn_cleanup_keeps_original_cancellation(
    postgres_url: URL,
) -> None:
    """SDK 0.22.3 may detach tool teardown on parent cancellation; app clears turn records."""
    grants = Grants()
    grants.grant("alice", TOTAL_TOOL)
    entered = asyncio.Event()

    async def busy(_):
        raise StarRocksError(StarRocksErrorCode.POOL_TIMEOUT, slot_wait=True)

    async def held(_):
        entered.set()
        await asyncio.Event().wait()

    async with ready_engine(postgres_url) as engine:
        governed, ctx = GovernedTools(store(engine, grants, Clock())), context()
        with pytest.raises(ToolRejectedError):
            await governed.invoke(ctx, request(), busy)
        retry = asyncio.create_task(governed.invoke(ctx, request(), held))
        await asyncio.wait_for(entered.wait(), 2)
        retry.cancel()
        governed.end_turn(ctx.identity)
        with pytest.raises(asyncio.CancelledError):
            await retry
        assert governed.turn_runs(ctx.identity).started == ()
        assert governed.turn_runs(ctx.identity).slot_retrying == frozenset()
