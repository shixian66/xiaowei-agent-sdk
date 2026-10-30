"""相同请求只执行一次：正式应用、真 SDK/PG，仅模型和外部 I/O 使用替身。"""

import asyncio
import json
from datetime import timedelta

import pytest
from agents.testing import function_call
from asyncmy.errors import ProgrammingError
from tests.sdk_core.synthetic_tools import TOTAL_TOOL
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_group_actions import Actions
from tests.sdk_core.test_group_actions import actions as actions
from tests.sdk_core.test_group_gateway import running
from tests.sdk_core.test_group_identity import A
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_scheduled_tasks import task_config
from tests.sdk_core.test_starrocks_tools import (
    PROFILE,
    SALES,
    SR,
    Env,
    assembled,
    driver,
    executed_sql,
    tool_outputs,
)
from tests.sdk_core.test_webhook_notification import running as webhook_running

from xiaowei.app import TurnError
from xiaowei.feishu import attributed
from xiaowei.governance import Prechecked, ReusedToolResult
from xiaowei.models import ToolCall

pytestmark = pytest.mark.loopback
SQL = "SELECT region, total FROM shop.sales"


@pytest.fixture
async def sql_env(postgres_url, monkeypatch):
    # 100 个调用壳保留；只为此压力夹具配置足够请求容量，不改变产品限制。
    async with assembled(
        postgres_url,
        monkeypatch,
        driver(SALES),
        profile=PROFILE.model_copy(update={"max_request_bytes": 512_000}),
    ) as env:
        yield env


def batch(name, arguments, count=100):
    return lambda call: [function_call(name, arguments, call_id=f"batch-{i}") for i in range(count)]


async def test_100_sql_calls_share_io_evidence_and_budget(sql_env: Env, caplog):
    env = sql_env
    ctx = env.ctx(max_tool_calls=2)
    budgets = []
    first_outputs = []

    def different(call):
        budgets.append(env.governed.checkpoint(ctx.identity).used)
        outputs = [json.loads(value) for value in tool_outputs(call)]
        first_outputs.extend(outputs)
        return tool_call("run_readonly_query", cluster=SR.target_id, sql=SQL + " WHERE total > 50")(
            call
        )

    caplog.set_level("INFO", logger="xiaowei.app")
    prompt = env.scripts.add(
        "查看销售数据",
        batch("run_readonly_query", {"cluster": SR.target_id, "sql": SQL}),
        different,
        cite(),
    )
    delivered = await env.deliver(ctx, prompt)
    assert delivered.facts
    assert budgets == [1]
    assert len(first_outputs) == 100
    assert sum("evidence_id" in o for o in first_outputs) == 1
    reused = [o for o in first_outputs if o.get("status") == "reused"]
    assert len(reused) == 99
    assert {o["original_call_id"] for o in reused} == {"batch-0"}
    assert all(o["executed"] is False and o["original_status"] == "succeeded" for o in reused)
    assert len(executed_sql(env.drv)) == 2
    assert await env.evidence_rows() == 2
    assert "tool_calls=101 duplicate_calls=99" in caplog.text
    follow = env.scripts.add(
        "重新查看",
        tool_call("run_readonly_query", cluster=SR.target_id, sql=SQL),
        cite(),
    )
    await env.deliver(env.ctx(turn="t2"), follow)
    assert len(executed_sql(env.drv)) == 3
    assert await env.evidence_rows() == 3


async def test_100_proposals_create_one_action_and_one_approval_material(actions: Actions):
    env = actions.env
    env.scripts.add(
        attributed(A, "准备修改"),
        batch("am-synthetic__propose_change", {"value": "after"}),
        cite(),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("准备修改", message_id="om_reuse_proposal")
    assert len(actions.adapter.reads) == 1
    assert actions.adapter.calls == []
    action_id = await actions.one()
    assert await env.rows("SELECT kind FROM xiaowei_delivery_part ORDER BY kind") == [
        ("approval",),
        ("body",),
    ]
    assert sum(action_id in str(message) for message in group.outbox.sent) == 1


async def test_revoke_after_first_read_rejects_repeat(sql_env: Env):
    env = sql_env

    def revoke(call):
        env.grants.revoke_all()
        return tool_call("run_readonly_query", cluster=SR.target_id, sql=SQL)(call)

    prompt = env.scripts.add(
        "查后撤权",
        tool_call("run_readonly_query", cluster=SR.target_id, sql=SQL),
        revoke,
        cite(),
    )
    with pytest.raises(TurnError):
        await env.deliver(env.ctx(), prompt)
    outputs = [json.loads(v) for v in tool_outputs(env.scripts.calls[prompt][-1])]
    assert outputs[-1]["code"] == "not_allowed"
    assert "evidence_id" not in outputs[-1]
    assert len(executed_sql(env.drv)) == 1


async def test_classified_sql_failure_reused_and_changed_sql_can_continue(sql_env: Env):
    env = sql_env
    env.drv.make = driver(
        ProgrammingError(
            1064, "Getting syntax error. Detail message: Unexpected input 'secret-canary'."
        )
    ).make
    failures = []

    def correct(call):
        failures.extend(json.loads(v) for v in tool_outputs(call))
        env.drv.make = driver(SALES).make
        return tool_call("run_readonly_query", cluster=SR.target_id, sql=SQL + " WHERE total > 50")(
            call
        )

    prompt = env.scripts.add(
        "解析失败后改查",
        batch("run_readonly_query", {"cluster": SR.target_id, "sql": SQL}),
        correct,
        cite(),
    )
    delivered = await env.deliver(env.ctx(), prompt)
    assert delivered.facts
    assert len(executed_sql(env.drv)) == 2
    assert await env.evidence_rows() == 1
    assert sum(o.get("status") == "invalid_sql" for o in failures) == 1
    assert (
        sum(
            o.get("status") == "reused"
            and o["original_status"] == "failed"
            and o["code"] == "invalid_sql"
            for o in failures
        )
        == 99
    )
    assert all("evidence_id" not in o for o in failures)
    assert "secret-canary" not in json.dumps(failures)


async def test_successful_proposal_invalidates_read_cache(actions: Actions):
    env = actions.env
    env.scripts.add(
        attributed(A, "查后准备再查"),
        tool_call("order_total", region="east"),
        tool_call("am-synthetic__propose_change", value="after"),
        tool_call("order_total", region="east"),
        cite(),
    )
    async with running(env) as group:
        await group.ask("查后准备再查", message_id="om_invalidate_read")
    assert len(env.adapter.calls) == 2
    assert all(req.tool_id == TOTAL_TOOL for req in env.adapter.calls)
    assert len(actions.adapter.reads) == 1
    assert actions.adapter.calls == []
    assert await env.rows("SELECT count(*) FROM xiaowei_evidence") == [(3,)]


async def test_completed_result_still_checks_scope_and_parameters(sql_env: Env):
    from xiaowei.governance import ToolRejectedError
    from xiaowei.models import ToolRequest
    from xiaowei.starrocks_tools import RUN_QUERY

    env, ctx = sql_env, sql_env.ctx()
    request = ToolRequest(
        tool_id=RUN_QUERY,
        target_id=SR.target_id,
        call_id="original",
        tool_name="run_readonly_query",
        arguments={"cluster": SR.target_id, "sql": SQL},
    )
    execute = env.executes[(RUN_QUERY, SR.target_id)]
    first = await env.governed.invoke(ctx, request, execute)
    for changed_ctx, changed_request in [
        (ctx.model_copy(update={"tool_scope": frozenset()}), request),
        (
            ctx,
            request.model_copy(
                update={"arguments": {"cluster": SR.target_id, "sql": SQL, "extra": 1}}
            ),
        ),
    ]:
        with pytest.raises(ToolRejectedError):
            await env.governed.invoke(changed_ctx, changed_request, execute)
    assert len(executed_sql(env.drv)) == 1
    # 短回执不允许把首次 Evidence 伪装成另一 call_id 的证据。
    from xiaowei.evidence import EvidenceError

    with pytest.raises(EvidenceError):
        await env.evidence.project_many(
            [
                (
                    first.evidence_id,
                    ToolCall(
                        call_id="alias", tool_name=request.tool_name, arguments=request.arguments
                    ),
                )
            ],
            ctx,
            "model",
        )


async def test_100_scheduled_proposals_create_one_draft(env, monkeypatch):
    config = task_config(env)
    config = config.model_copy(
        update={"model": config.model.model_copy(update={"max_request_bytes": 512_000})}
    )
    args = {
        "bot_alias": "ops_alert_bot",
        "text": "合成提醒",
        "evidence_ids": [],
        "at": (env.clock() + timedelta(minutes=10)).isoformat(),
    }
    env.scripts.add(
        attributed(A, "准备提醒"), batch("scheduled__propose_reminder", args), cite()
    )
    async with webhook_running(env, monkeypatch, config=config) as (group, requests, _rt):
        await group.ask("准备提醒")
        assert requests == []
        assert (
            await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job WHERE state='draft'") == 1
        )
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 1
        assert (
            await env.scalar("SELECT count(*) FROM xiaowei_delivery_part WHERE kind='approval'")
            == 1
        )


async def test_different_requests_run_concurrently_and_waiter_cancel_is_local(sql_env: Env):
    from xiaowei.models import ToolRequest
    from xiaowei.starrocks_tools import RUN_QUERY

    env, ctx = sql_env, sql_env.ctx()
    original = env.executes[(RUN_QUERY, SR.target_id)]
    both, release = asyncio.Event(), asyncio.Event()
    entered = []

    async def held(value):
        entered.append(value)
        if len(entered) == 2:
            both.set()
        await release.wait()
        return await original.run(value)

    execute = Prechecked(original.check, held)

    def req(call_id, sql=SQL):
        return ToolRequest(
            tool_id=RUN_QUERY,
            target_id=SR.target_id,
            call_id=call_id,
            tool_name="run_readonly_query",
            arguments={"sql": sql, "cluster": SR.target_id},
        )

    first = asyncio.create_task(env.governed.invoke(ctx, req("first"), execute))
    other = asyncio.create_task(
        env.governed.invoke(ctx, req("other", SQL + " WHERE total > 50"), execute)
    )
    duplicate = asyncio.create_task(env.governed.invoke(ctx, req("duplicate"), execute))
    try:
        await asyncio.wait_for(both.wait(), 2)
        while env.governed.call_counts(ctx.identity)[0] < 3:
            await asyncio.sleep(0)
        duplicate.cancel()
        with pytest.raises(asyncio.CancelledError):
            await duplicate
        assert not first.done() and not other.done()
    finally:
        release.set()
        await asyncio.gather(first, other)
    assert len(executed_sql(env.drv)) == 2
    receipt = await env.governed.invoke(ctx, req("late"), execute)
    assert isinstance(receipt, ReusedToolResult)
    assert env.governed.checkpoint(ctx.identity).used == 2
    # 暂停账本只有既有业务记录，恢复后同一请求重新读取。
    ledger = env.governed.checkpoint(ctx.identity)
    env.governed.end_turn(ctx.identity)
    env.governed.restore(ctx, ledger)
    assert env.governed.call_counts(ctx.identity) == (0, 0)
    await env.governed.invoke(ctx, req("resumed"), execute)
    assert len(executed_sql(env.drv)) == 3
    assert env.governed.call_counts(ctx.identity) == (1, 0)


async def test_100_prometheus_reads_share_one_mcp_request(env):
    from tests.sdk_core.mcp_fixture import serve
    from tests.sdk_core.test_monitoring_r1a import SOURCE, monitoring_config, query_server

    with serve(query_server) as upstream:
        config = env.config(**monitoring_config(upstream.url))
        config = config.model_copy(
            update={"model": config.model.model_copy(update={"max_request_bytes": 512_000})}
        )
        env.scripts.add("查指标", batch(f"{SOURCE}__query", {"query": "up"}), cite())
        async with env.running(config) as served:
            await served.page()
            result = (await served.turn("查指标", "query", "reuse-prometheus")).json()
            assert result["state"] == "completed"
            assert result["delivery"]["facts"]
        assert upstream.recorder.tool_calls == [("query", {"query": "up"})]
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1


@pytest.mark.parametrize("failure", ["unknown", "evidence"])
async def test_unknown_or_storage_failure_never_becomes_a_reusable_receipt(sql_env: Env, failure):
    from sqlalchemy import text

    from xiaowei.governance import ToolRejectedError
    from xiaowei.models import ToolRequest
    from xiaowei.starrocks_tools import RUN_QUERY

    env, ctx = sql_env, sql_env.ctx(max_tool_calls=1)
    original = env.executes[(RUN_QUERY, SR.target_id)]
    entered, release = asyncio.Event(), asyncio.Event()
    if failure == "evidence":
        async with env.engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE xiaowei_evidence ADD CONSTRAINT synthetic_failure CHECK (false)")
            )

    async def held(value):
        entered.set()
        await release.wait()
        if failure == "unknown":
            raise RuntimeError("synthetic-private-marker")
        return await original.run(value)

    execute = Prechecked(original.check, held)
    req = ToolRequest(
        tool_id=RUN_QUERY,
        target_id=SR.target_id,
        call_id="leader",
        tool_name="run_readonly_query",
        arguments={"cluster": SR.target_id, "sql": SQL},
    )
    leader = asyncio.create_task(env.governed.invoke(ctx, req, execute))
    await asyncio.wait_for(entered.wait(), 2)
    follower = asyncio.create_task(
        env.governed.invoke(ctx, req.model_copy(update={"call_id": "follower"}), execute)
    )
    while env.governed.call_counts(ctx.identity)[0] < 2:
        await asyncio.sleep(0)
    release.set()
    outcomes = await asyncio.gather(leader, follower, return_exceptions=True)
    assert all(isinstance(outcome, Exception) for outcome in outcomes)
    assert all("synthetic-private-marker" not in str(outcome) for outcome in outcomes)
    assert await env.evidence_rows() == 0
    with pytest.raises(ToolRejectedError) as rejected:
        await env.governed.invoke(ctx, req.model_copy(update={"call_id": "later"}), execute)
    assert rejected.value.code == "budget_exhausted"
