"""B1：审批新请求、原轮身份和持久账本的公开 SDK 接续实验。

不修改产品。当前 GovernedTools 没有导出/装载接口，实验只在此文件访问
应用自身的 _used/_runs，明确验证所需的薄接口；不使用 SDK 私有 API。
c2 用受治理的合成读取触发 native approval，尚不证明实际写 gate 的绑定。
"""

import json
from dataclasses import replace

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from pydantic import TypeAdapter
from sqlalchemy import text
from tests.sdk_core.synthetic_tools import TOTAL_TOOL, catalog, context, s3_answer
from tests.sdk_core.test_group_actions import SHARED
from tests.sdk_core.test_group_actions import SOURCE as AM_SOURCE
from tests.sdk_core.test_group_actions import actions as actions
from tests.sdk_core.test_group_identity import GROUP, A, B
from tests.sdk_core.test_monitoring_r1a import SOURCE, TOOL, monitoring_config
from tests.sdk_core.test_runtime import serve_config
from tests.sdk_core.test_session_policy import Harness, _call, harness
from tests.spike.test_sdk_hitl_reuse import restore, run, snapshot

from xiaowei.feishu import attributed
from xiaowei.governance import (
    GovernedTools,
    MonitoringReadError,
    PromQLExpressionError,
    ToolCatalog,
    ToolRejectedError,
    TurnRuns,
)
from xiaowei.models import RunContext, ToolRequest, TurnAnswer
from xiaowei.runtime import ServeConfig, _monitoring_catalog
from xiaowei.session import SessionItemRejectedError
from xiaowei.tools import governed_function_tool

pytestmark = pytest.mark.loopback
RUNS = TypeAdapter(TurnRuns)


def key(ctx):
    return (ctx.identity.subject_id, ctx.identity.session_id, ctx.identity.turn_id)


def cite_all(call):
    ids = [
        envelope["evidence_id"]
        for item in call.input
        if item.get("type") == "function_call_output"
        and "evidence_id" in (envelope := json.loads(item["output"]))
    ]
    return [assistant_message(json.dumps(s3_answer(ids)))]


async def gated_c2(_ctx, _arguments, call_id):
    return call_id == "c2"


def agent_for(h, model, other_tools=()):
    agent = h.agent(model)
    agent.tools = [replace(agent.tools[0], needs_approval=gated_c2), *other_tools]
    return agent


async def save_checkpoint(h, ctx, paused):
    runs = h.governed.turn_runs(ctx.identity)
    assert not runs.slot_retrying, "只允许所有非审批工具已落定的暂停点"
    ledger = {
        "used": h.governed._used.get(key(ctx), 0),
        "runs": json.loads(RUNS.dump_json(runs)),
    }
    async with h.engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE sdk_spike_checkpoint(id INT PRIMARY KEY, context TEXT NOT NULL,"
                " state TEXT NOT NULL, ledger TEXT NOT NULL)"
            )
        )
        # 一行/同一事务保存原轮 context、SDK 状态和治理账本，不会只提交其中一项。
        await conn.execute(
            text("INSERT INTO sdk_spike_checkpoint VALUES (1, :ctx, :state, :ledger)"),
            {"ctx": ctx.model_dump_json(), "state": snapshot(paused), "ledger": json.dumps(ledger)},
        )


async def load_checkpoint(h):
    async with h.engine.connect() as conn:
        original, saved, ledger_json = (
            await conn.execute(text("SELECT context, state, ledger FROM sdk_spike_checkpoint"))
        ).one()
    ctx = RunContext.model_validate_json(original)
    ledger = json.loads(ledger_json)
    h.governed._used[key(ctx)] = ledger["used"]
    h.governed._runs[key(ctx)] = RUNS.validate_python(ledger["runs"])
    return ctx, saved


async def commit_result(h, ctx, result, message):
    session = h.session(ctx)
    await session.get_items()
    await session.add_items(
        [{"role": "user", "content": message}]
        + [i.to_input_item() for i in result.new_items if i.type != "tool_approval_item"]
    )
    runs = h.governed.turn_runs(ctx.identity)
    seen = h.seen(session, ctx)
    turn = TurnAnswer(
        answer=result.final_output,
        context_evidence=seen,
        monitoring_failures=runs.monitoring_failures,
    )
    delivered = await h.evidence.validate_answer(turn, ctx)
    await session.commit_validated(seen, monitoring_failures=runs.monitoring_failures)
    return delivered


async def original_group_context(actions):
    env = actions.env
    receipt = await env.service.accept(env.group(attributed(A, "SDK 原任务"), "sdk-original"))
    record = await env.store.start(receipt.record, message=receipt.message, policy_version="w5")
    ctx = context(
        subject=A,
        session=record.session_id,
        turn=record.turn_id,
        channel="feishu",
        tools=SHARED,
        targets=frozenset({AM_SOURCE, "synthetic-warehouse"}),
    )
    return ctx.model_copy(
        update={"identity": ctx.identity.model_copy(update={"owner": GROUP.owner})}
    )


async def new_approval(actions, ctx):
    env = actions.env
    command = "/批准 00000000-0000-4000-8000-000000000001"
    receipt = await env.service.accept(
        env.group(attributed(B, command), "sdk-new-approval", actor=B)
    )
    record = await env.store.start(receipt.record, message=receipt.message, policy_version="w5")
    assert record.subject_id == B and record.turn_id != ctx.identity.turn_id
    assert record.session_id == ctx.identity.session_id
    return record


@pytest.mark.parametrize("new_governance", [False, True])
async def test_approval_new_request_restores_original_run_and_commits(actions, new_governance):
    env = actions.env
    h = Harness(env.engine, env.access, env.clock, env.adapter, env.evidence, actions.governance)
    original = await original_group_context(actions)
    model = ScriptedModel([_call("c1"), _call("c2")])
    agent = agent_for(h, model)
    session = h.session(original)
    paused = await run(agent, "SDK 原任务", ctx=original, session=session)
    assert len(paused.interruptions) == 1 and len(h.adapter.calls) == 1
    await save_checkpoint(h, original, paused)
    await session.discard_pending()
    approval = await new_approval(actions, original)
    if new_governance:
        h.governed = GovernedTools(h.evidence)
        assert h.governed.turn_runs(original.identity) == TurnRuns()
    # 审批记录属于 B 的新 turn；SDK context 仍由服务端保存的 A/原 turn 重建。
    restored, saved = await load_checkpoint(h)
    assert restored.identity == original.identity
    assert restored.identity.turn_id != approval.turn_id
    decision = await env.access.resolve_group(GROUP, A, verify_member=True)
    assert decision is not None
    assert restored.tool_scope <= decision.authorized_tools
    for target in restored.target_scope:
        assert target in decision.target_ids
    resumed_agent = agent_for(h, ScriptedModel([ModelStep(responder=cite_all)]))
    state = await restore(resumed_agent, saved, restored)
    state.approve(state.get_interruptions()[0], always_approve=False)
    result = await run(resumed_agent, state)
    await commit_result(h, restored, result, "SDK 原任务")
    assert len(h.adapter.calls) == 2
    assert h.governed.turn_runs(restored.identity).started == (TOTAL_TOOL, TOTAL_TOOL)
    assert (
        h.governed.turn_runs(
            original.identity.model_copy(update={"subject_id": B, "turn_id": approval.turn_id})
        )
        == TurnRuns()
    )
    rows = await env.rows("SELECT subject_id, session_id, turn_id FROM xiaowei_evidence")
    assert rows == [
        (A, original.identity.session_id, original.identity.turn_id),
        (A, original.identity.session_id, original.identity.turn_id),
    ]
    assert len(await h.session(restored).get_items()) == 6


async def test_using_approval_identity_splits_the_ledger_and_rejects_commit(actions):
    env = actions.env
    h = Harness(env.engine, env.access, env.clock, env.adapter, env.evidence, actions.governance)
    original = await original_group_context(actions)
    agent = agent_for(h, ScriptedModel([_call("c1"), _call("c2")]))
    session = h.session(original)
    paused = await run(agent, "SDK 原任务", ctx=original, session=session)
    await save_checkpoint(h, original, paused)
    await session.discard_pending()
    approval = await new_approval(actions, original)
    wrong = original.model_copy(
        update={
            "identity": original.identity.model_copy(
                update={"subject_id": B, "turn_id": approval.turn_id}
            )
        }
    )
    resumed_agent = agent_for(h, ScriptedModel([ModelStep(responder=cite_all)]))
    state = await restore(resumed_agent, snapshot(paused), wrong)
    state.approve(state.get_interruptions()[0], always_approve=False)
    result = await run(resumed_agent, state)
    fresh = h.session(wrong)
    await fresh.add_items(
        [{"role": "user", "content": "SDK 原任务"}]
        + [i.to_input_item() for i in result.new_items if i.type != "tool_approval_item"]
    )
    with pytest.raises(SessionItemRejectedError):
        await fresh.commit_validated(h.seen(fresh, wrong))
    assert h.governed.turn_runs(original.identity).started == (TOTAL_TOOL,)
    assert h.governed.turn_runs(wrong.identity).started == (TOTAL_TOOL,)
    assert await h.inner(original.identity.session_id).get_items() == []


async def test_persisted_tool_budget_is_not_reset_after_restart(postgres_url):
    async with harness(postgres_url) as h:
        original = context(max_tool_calls=2)
        agent = agent_for(h, ScriptedModel([_call("c1"), _call("c2")]))
        session = h.session(original)
        paused = await run(agent, "预算不能重置", ctx=original, session=session)
        await save_checkpoint(h, original, paused)
        await session.discard_pending()
        h.governed = GovernedTools(h.evidence)
        restored, saved = await load_checkpoint(h)
        resumed_agent = agent_for(h, ScriptedModel([ModelStep(responder=cite_all)]))
        state = await restore(resumed_agent, saved, restored)
        state.approve(state.get_interruptions()[0], always_approve=False)
        result = await run(resumed_agent, state)
        request = ToolRequest(
            target_id="synthetic-warehouse",
            tool_id=TOTAL_TOOL,
            tool_name="order_total",
            call_id="c3",
            arguments={"region": "east"},
        )
        with pytest.raises(ToolRejectedError) as refused:
            await h.governed.invoke(restored, request, h.adapter.execute)
        assert refused.value.code == "budget_exhausted"
        assert len(h.adapter.calls) == 2
        await commit_result(h, restored, result, "预算不能重置")


@pytest.mark.parametrize("failure", ["syntax", "unavailable"])
async def test_failures_before_pause_survive_restart_and_reach_delivery(postgres_url, failure):
    values = monitoring_config("http://127.0.0.1:9000/mcp")
    config = ServeConfig.model_validate(serve_config(8501, **values))
    contracts, policies = _monitoring_catalog(config)
    base = catalog()
    combined = ToolCatalog(
        (*base.contracts, *contracts), (*(base.policy_for(c) for c in base.contracts), *policies)
    )
    calls = []

    async with harness(postgres_url, tools=combined) as h:
        h.grants.grant("alice", TOOL, target=SOURCE)
        ctx = context(
            targets=frozenset({"synthetic-warehouse", SOURCE}),
            tools=frozenset({TOTAL_TOOL, TOOL}),
            max_tool_calls=6,
        )

        async def monitoring(request):
            expression = request.arguments["query"]
            calls.append(expression)
            if failure == "syntax":
                raise PromQLExpressionError(1, 4)
            raise MonitoringReadError("unavailable")

        def monitor_tool():
            return governed_function_tool(
                f"{SOURCE}__query",
                h.governed.catalog.contract(TOOL, SOURCE),
                h.governed,
                monitoring,
            )

        failed_call = function_call(f"{SOURCE}__query", {"query": "bad("}, call_id="c0")
        agent = agent_for(
            h,
            ScriptedModel([[failed_call], _call("c1"), _call("c2")]),
            [monitor_tool()],
        )
        session = h.session(ctx)
        paused = await run(agent, "源失败后继续调查", ctx=ctx, session=session)
        assert calls == ["bad("] and len(h.adapter.calls) == 1
        await save_checkpoint(h, ctx, paused)
        await session.discard_pending()
        h.governed = GovernedTools(h.evidence)
        restored, saved = await load_checkpoint(h)
        retried = function_call(f"{SOURCE}__query", {"query": "bad("}, call_id="c3")
        resumed_agent = agent_for(
            h,
            ScriptedModel([[retried], ModelStep(responder=cite_all)]),
            [monitor_tool()],
        )
        state = await restore(resumed_agent, saved, restored)
        state.approve(state.get_interruptions()[0], always_approve=False)
        result = await run(resumed_agent, state)
        assert calls == ["bad("]  # 拒绝同一失败表达式/已停止源，未产生第二次 I/O。
        runs = h.governed.turn_runs(restored.identity)
        assert len(runs.monitoring_failures) == 1
        assert runs.monitoring_failures[0].target_id == SOURCE
        if failure == "syntax":
            assert runs.promql_errors == ((SOURCE, "bad("),)
            assert runs.promql_pending == frozenset({SOURCE})
        delivered = await commit_result(h, restored, result, "源失败后继续调查")
        assert SOURCE in delivered.content and "未核实" in delivered.content
        assert len(h.adapter.calls) == 2


async def test_proposal_rejections_and_other_finished_flags_round_trip(actions):
    from tests.sdk_core.test_group_actions import PROPOSE

    from xiaowei.governance import ProposalRejectedError

    env = actions.env
    h = Harness(env.engine, env.access, env.clock, env.adapter, env.evidence, actions.governance)
    original = await original_group_context(actions)
    rejected = []

    async def unavailable(request):
        rejected.append(request.call_id)
        raise ProposalRejectedError("对象已到期", code="object_unavailable")

    proposal = governed_function_tool(
        "am-synthetic__propose_change",
        h.governed.catalog.contract(PROPOSE, AM_SOURCE),
        h.governed,
        unavailable,
    )
    h.adapter.truncated = True
    agent = agent_for(
        h,
        ScriptedModel(
            [
                [function_call(proposal.name, {"value": "after"}, call_id="p1")],
                _call("c1"),
                _call("c2"),
            ]
        ),
        [proposal],
    )
    session = h.session(original)
    paused = await run(agent, "明确拒绝仍记预算", ctx=original, session=session)
    before = h.governed.turn_runs(original.identity)
    assert before.proposal_rejections == (PROPOSE,)
    assert before.truncated == 1
    # 已落定的计数/限额状态也要跨暂停保存；不把正在执行的 slot_retrying 带到恢复端。
    h.governed.note_rejected(original.identity)
    h.governed.note_limit_rescue(original.identity)
    before = h.governed.turn_runs(original.identity)
    await save_checkpoint(h, original, paused)
    await session.discard_pending()
    h.governed = GovernedTools(h.evidence)
    restored, _saved = await load_checkpoint(h)
    assert h.governed.turn_runs(restored.identity) == before
    assert h.governed._used[key(restored)] == 2
    assert rejected == ["p1"]
    assert await env.rows("SELECT count(*) FROM xiaowei_action") == [(0,)]


async def test_approver_permissions_do_not_replace_original_requester_membership(actions):

    env = actions.env
    h = Harness(env.engine, env.access, env.clock, env.adapter, env.evidence, actions.governance)
    original = await original_group_context(actions)
    agent = agent_for(h, ScriptedModel([_call("c1"), _call("c2")]))
    session = h.session(original)
    paused = await run(agent, "权限不能借给原任务", ctx=original, session=session)
    await save_checkpoint(h, original, paused)
    await session.discard_pending()
    approval = await new_approval(actions, original)
    assert approval.subject_id == B
    env.members.current.remove(A)
    assert await env.access.resolve_group(GROUP, B, verify_member=True) is not None
    assert await env.access.resolve_group(GROUP, A, verify_member=True) is None
    # 在 SDK/模型恢复之前停止；审批人仍获准并不使原发起人重新成为成员。
    assert len(h.adapter.calls) == 1
    assert len(agent.model.calls) == 2
    assert await h.inner(original.identity.session_id).get_items() == []


async def test_state_and_ledger_save_roll_back_together(postgres_url):
    async with harness(postgres_url) as h:
        ctx = context()
        agent = agent_for(h, ScriptedModel([_call("c1"), _call("c2")]))
        session = h.session(ctx)
        paused = await run(agent, "同事务保存", ctx=ctx, session=session)
        with pytest.raises(RuntimeError, match="synthetic rollback"):
            async with h.engine.begin() as conn:
                await conn.execute(text("CREATE TABLE sdk_spike_atomic(state TEXT, ledger TEXT)"))
                await conn.execute(
                    text("INSERT INTO sdk_spike_atomic VALUES (:state, :ledger)"),
                    {
                        "state": snapshot(paused),
                        "ledger": RUNS.dump_json(h.governed.turn_runs(ctx.identity)).decode(),
                    },
                )
                raise RuntimeError("synthetic rollback")
        async with h.engine.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('sdk_spike_atomic')")) is None
        assert await h.inner().get_items() == []
