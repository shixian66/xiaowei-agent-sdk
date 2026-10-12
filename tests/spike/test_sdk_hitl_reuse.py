"""锁定 SDK 的 HITL 可行性实验；不实现正式审批或修改产品。"""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from agents import Agent, RunContextWrapper, Runner, RunState, UserError, function_tool
from agents.extensions.memory import SQLAlchemySession
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.tool_guardrails import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    tool_input_guardrail,
)
from sqlalchemy import text
from tests.sdk_core.synthetic_tools import context, ready_engine
from tests.sdk_core.test_action_lifecycle import propose
from tests.sdk_core.test_group_actions import SHARED, SOURCE, WRITE, Actions
from tests.sdk_core.test_group_actions import actions as actions
from tests.sdk_core.test_group_identity import GROUP, A, B
from tests.sdk_core.test_session_policy import _call, _cite, harness

from xiaowei.actions import ActionOutcomeError
from xiaowei.app import safe_run_config
from xiaowei.feishu import attributed
from xiaowei.governance import ToolRejectedError
from xiaowei.models import RunContext
from xiaowei.session import SessionError

pytestmark = pytest.mark.loopback


def snapshot(result):
    return result.to_state().to_string(
        context_serializer=lambda ctx: (
            ctx.model_dump(mode="json") if isinstance(ctx, RunContext) else ctx
        ),
        strict_context=True,
    )


async def restore(agent, saved, current):
    return await RunState.from_string(agent, saved, context_override=current, strict_context=True)


async def run(agent, value, *, ctx=None, session=None):
    return await Runner.run(
        agent, value, context=ctx, session=session, run_config=safe_run_config()
    )


async def test_pause_database_round_trip_and_native_session(postgres_url):
    effects = []

    @function_tool(needs_approval=True, failure_error_function=None)
    async def write(value: str) -> str:
        effects.append(value)
        return "written"

    agent = Agent(
        name="spike",
        model=ScriptedModel(
            [
                [function_call("write", {"value": "after"}, call_id="c1")],
                [assistant_message("done")],
            ]
        ),
        tools=[write],
    )
    async with ready_engine(postgres_url) as engine:
        session = SQLAlchemySession("spike", engine=engine, create_tables=True)
        result = await run(agent, "申请修改", ctx={"subject": "requester"}, session=session)
        assert len(result.interruptions) == 1 and effects == []
        assert result.interruptions[0].name == "write"
        saved = snapshot(result)
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE sdk_spike_state (id INT PRIMARY KEY, snapshot TEXT NOT NULL)")
            )
            await conn.execute(
                text("INSERT INTO sdk_spike_state VALUES (1, :snapshot)"),
                {"snapshot": saved},
            )
        pause_items = await session.get_items()
        print("native_pause_items", [i.get("type", "message") for i in pause_items])
    # 关闭旧连接池后，从 PostgreSQL 读取状态，并新建同一后端/ID 的 Session。
    async with ready_engine(postgres_url) as engine:
        async with engine.connect() as conn:
            saved = await conn.scalar(text("SELECT snapshot FROM sdk_spike_state WHERE id=1"))
        state = await restore(agent, saved, {"subject": "approver"})
        state.approve(state.get_interruptions()[0], always_approve=False)
        result = await run(
            agent,
            state,
            session=SQLAlchemySession("spike", engine=engine, create_tables=True),
        )
        assert result.final_output == "done" and effects == ["after"]
        stored = await SQLAlchemySession("spike", engine=engine).get_items()
        kinds = [i.get("type", "message") for i in stored]
        assert kinds.count("function_call") == 1
        assert kinds.count("function_call_output") == 1
        assert sum(i.get("role") == "user" for i in stored) == 1
        assert stored[-1]["role"] == "assistant"
        print("native_resumed_items", kinds)


async def test_each_new_call_pauses_and_rejection_is_returned():
    effects = []

    @function_tool(needs_approval=True, failure_error_function=None)
    async def write(value: str) -> str:
        effects.append(value)
        return value

    def finish(call):
        outputs = [i["output"] for i in call.input if i.get("type") == "function_call_output"]
        assert outputs == ["first", "本次拒绝，未执行"]
        return [assistant_message("done")]

    agent = Agent(
        name="spike",
        model=ScriptedModel(
            [
                [function_call("write", {"value": "first"}, call_id="c1")],
                [function_call("write", {"value": "second"}, call_id="c2")],
                ModelStep(responder=finish),
            ]
        ),
        tools=[write],
    )
    first = await run(agent, "申请修改", ctx={})
    state = await restore(agent, snapshot(first), {})
    state.approve(state.get_interruptions()[0], always_approve=False)
    second = await run(agent, state)
    assert effects == ["first"] and len(second.interruptions) == 1
    assert json.loads(second.interruptions[0].arguments)["value"] == "second"
    state = await restore(agent, snapshot(second), {})
    state.reject(state.get_interruptions()[0], rejection_message="本次拒绝，未执行")
    final = await run(agent, state)
    assert final.final_output == "done" and effects == ["first"]


async def test_sdk_snapshot_alone_does_not_prevent_duplicate_effects():
    effects = []

    @function_tool(needs_approval=True, failure_error_function=None)
    async def write(value: str) -> str:
        effects.append(value)
        return value

    # stop_on_first_tool 避免把第二次恢复的模型脚本差异当作防重。
    agent = Agent(
        name="spike",
        model=ScriptedModel([[function_call("write", {"value": "after"}, call_id="c1")]]),
        tools=[write],
        tool_use_behavior="stop_on_first_tool",
    )
    paused = await run(agent, "申请修改", ctx={})
    saved = snapshot(paused)
    for _ in range(2):
        state = await restore(agent, saved, {})
        state.approve(state.get_interruptions()[0], always_approve=False)
        assert (await run(agent, state)).final_output == "after"
    assert effects == ["after", "after"]


async def test_preparation_and_guardrails_run_at_different_points():
    events = []

    async def prepare(_ctx, arguments, call_id):
        events.append(("prepare", arguments["value"], call_id))
        return True

    @tool_input_guardrail
    async def current_policy(data: ToolInputGuardrailData):
        events.append(("guard", data.context.context["allowed"]))
        if not data.context.context["allowed"]:
            return ToolGuardrailFunctionOutput.reject_content("权限已撤销，未执行")
        return ToolGuardrailFunctionOutput.allow()

    @function_tool(
        needs_approval=prepare,
        tool_input_guardrails=[current_policy],
        failure_error_function=None,
    )
    async def write(value: str) -> str:
        events.append(("write", value))
        return value

    def finish(call):
        output = [i["output"] for i in call.input if i.get("type") == "function_call_output"]
        assert output == ["权限已撤销，未执行"]
        return [assistant_message("rejected")]

    agent = Agent(
        name="spike",
        tools=[write],
        model=ScriptedModel(
            [
                [function_call("write", {"value": "after"}, call_id="c1")],
                ModelStep(responder=finish),
            ]
        ),
    )
    paused = await run(agent, "申请修改", ctx={"allowed": True})
    assert events == [("prepare", "after", "c1")]
    state = await restore(agent, snapshot(paused), {"allowed": False})
    state.approve(state.get_interruptions()[0], always_approve=False)
    final = await run(agent, state)
    assert final.final_output == "rejected"
    assert events == [("prepare", "after", "c1"), ("guard", False)]


async def test_rehydrated_context_still_passes_current_governance(postgres_url):
    async with harness(postgres_url) as h:
        ctx = context()

        def rejected(call):
            outputs = [i["output"] for i in call.input if i.get("type") == "function_call_output"]
            assert json.loads(outputs[-1])["status"] == "rejected"
            return [
                assistant_message(
                    json.dumps(
                        {
                            "evidence_ids": [],
                            "inferences": [],
                            "clarification": "权限已撤销",
                        }
                    )
                )
            ]

        agent = h.agent(ScriptedModel([_call(), ModelStep(responder=rejected)]))
        agent.tools = [replace(agent.tools[0], needs_approval=True)]
        first = await run(agent, "申请读取", ctx=ctx)
        assert len(first.interruptions) == 1 and h.adapter.calls == []
        saved = snapshot(first)
        h.grants.revoke_all()
        state = await restore(agent, saved, ctx)
        state.approve(state.get_interruptions()[0], always_approve=False)
        result = await run(agent, state)
        assert result.final_output.clarification == "权限已撤销"
        assert h.adapter.calls == []
        assert h.governed.turn_runs(ctx.identity).produced == ()
        print("governance_revoked_zero_adapter_calls")


@pytest.mark.parametrize("fresh_session", [False, True])
async def test_current_policy_session_is_not_a_drop_in_hitl_backend(postgres_url, fresh_session):
    async with harness(postgres_url) as h:
        ctx = context()
        session = h.session(ctx)
        agent = h.agent(ScriptedModel([_call(), _cite()]))
        agent.tools = [replace(agent.tools[0], needs_approval=True)]
        paused = await run(agent, "申请读取", ctx=ctx, session=session)
        assert len(paused.interruptions) == 1 and h.adapter.calls == []
        assert await h.inner().get_items() == []
        state = await restore(agent, snapshot(paused), ctx)
        state.approve(state.get_interruptions()[0], always_approve=False)
        restored_session = h.session(ctx) if fresh_session else session
        with pytest.raises(SessionError) as caught:
            await run(agent, state, session=restored_session)
            await restored_session.commit_validated(h.seen(restored_session, ctx))
        print("policy_resume", fresh_session, type(caught.value).__name__, len(h.adapter.calls))
        # 会话不合约不应误报成功；实际工具事实仍由治理/Evidence 保存。
        assert len(h.adapter.calls) == 1
        assert await h.inner().get_items() == []


async def approval_context(actions: Actions):
    env = actions.env
    receipt = await env.service.accept(
        env.group(attributed(B, "/批准 SDK实验"), "sdk-approval", actor=B)
    )
    record = await env.store.start(receipt.record, message=receipt.message, policy_version="w5")
    current = context(
        subject=B,
        session=record.session_id,
        turn=record.turn_id,
        channel="feishu",
        tools=SHARED | {WRITE},
        targets=frozenset({SOURCE, "synthetic-warehouse"}),
    )
    return current.model_copy(
        update={"identity": current.identity.model_copy(update={"owner": GROUP.owner})}
    )


@pytest.mark.parametrize(
    "case", ["success", "not_sent", "revoked", "left", "expired", "changed", "timeout"]
)
async def test_native_resume_keeps_existing_business_fences(actions: Actions, case: str):
    action_id = await propose(actions)
    current = await approval_context(actions)
    effects = actions.adapter
    assert actions.env.app.actions is not None

    @function_tool(needs_approval=True, failure_error_function=None)
    async def apply(ctx: RunContextWrapper[RunContext], action_id: str) -> str:
        answer = await actions.env.app.actions.approve(action_id, ctx.context)
        return answer.model_dump_json()

    agent = Agent(
        name="spike",
        tools=[apply],
        tool_use_behavior="stop_on_first_tool",
        model=ScriptedModel([[function_call("apply", {"action_id": action_id}, call_id="sdk-c1")]]),
    )
    requester = current.model_copy(
        update={"identity": current.identity.model_copy(update={"subject_id": A})}
    )
    paused = await run(agent, "申请修改", ctx=requester)
    assert effects.calls == [] and len(paused.interruptions) == 1
    if case == "not_sent":
        async with actions.env.engine.begin() as conn:
            await conn.execute(
                text("UPDATE xiaowei_request SET delivery='failed' WHERE reply_message_id='om_p'")
            )
    elif case == "revoked":
        actions.env.access._grants = {A: SHARED}
    elif case == "left":
        actions.env.members.current = {A}
    elif case == "expired":
        actions.env.clock.now += timedelta(minutes=16)
    elif case == "changed":
        effects.revision = "v2"
    elif case == "timeout":
        effects.fail = True
    saved = snapshot(paused)
    state = await restore(agent, saved, current)
    state.approve(state.get_interruptions()[0], always_approve=False)
    if case == "success":
        result = await run(agent, state)
        assert result.interruptions == []
        assert len(effects.calls) == 1
        assert await actions.env.rows("SELECT state, approver_id FROM xiaowei_action") == [
            ("succeeded", B)
        ]
    elif case == "timeout":
        with pytest.raises(UserError) as caught:
            await run(agent, state)
        assert isinstance(caught.value.__cause__, ActionOutcomeError)
        assert len(effects.calls) == 1
        assert await actions.env.rows("SELECT state FROM xiaowei_action") == [("unknown",)]
    else:
        with pytest.raises(UserError) as caught:
            await run(agent, state)
        assert isinstance(caught.value.__cause__, (ToolRejectedError, ActionOutcomeError))
        assert effects.calls == []
    if case in {"success", "timeout"}:
        # 独立恢复的旧快照不能靠 SDK 防重；既有数据库 Action 占用必须继续挡住它。
        duplicate = await restore(agent, saved, current)
        duplicate.approve(duplicate.get_interruptions()[0], always_approve=False)
        with pytest.raises(UserError) as caught:
            await run(agent, duplicate)
        assert isinstance(caught.value.__cause__, ToolRejectedError)
        assert len(effects.calls) == 1


async def test_pause_releases_the_runner_task_without_execution():
    effects = []

    @function_tool(needs_approval=True, failure_error_function=None)
    async def write(value: str) -> str:
        effects.append(value)
        return value

    agent = Agent(
        name="spike",
        tools=[write],
        model=ScriptedModel([[function_call("write", {"value": "after"}, call_id="c1")]]),
    )
    task = asyncio.create_task(run(agent, "申请修改", ctx={}))
    paused = await asyncio.wait_for(task, 2)
    assert task.done() and not task.cancelled()
    assert len(paused.interruptions) == 1 and effects == []
    assert snapshot(paused)
    # SDK 返回 interruption 后已释放原 Runner，不需要挂一个等待用户的 asyncio Task。
    assert task.cancel() is False
    assert effects == []


async def test_native_partial_approval_keeps_other_calls_pending():
    effects = []

    @function_tool(needs_approval=True, failure_error_function=None)
    async def write(value: str) -> str:
        effects.append(value)
        return value

    agent = Agent(
        name="spike",
        tools=[write],
        model=ScriptedModel(
            [
                [
                    function_call("write", {"value": "first"}, call_id="c1"),
                    function_call("write", {"value": "second"}, call_id="c2"),
                ],
                [assistant_message("done")],
            ]
        ),
    )
    paused = await run(agent, "申请两项", ctx={})
    assert len(paused.interruptions) == 2 and effects == []
    state = await restore(agent, snapshot(paused), {})
    first = next(
        i for i in state.get_interruptions() if json.loads(i.arguments)["value"] == "first"
    )
    state.approve(first, always_approve=False)
    partial = await run(agent, state)
    assert effects == ["first"] and len(partial.interruptions) == 1
    state = await restore(agent, snapshot(partial), {})
    state.approve(state.get_interruptions()[0], always_approve=False)
    assert (await run(agent, state)).final_output == "done"
    assert effects == ["first", "second"]


async def test_context_override_does_not_authenticate_a_foreign_snapshot():
    effects = []

    @function_tool(needs_approval=True, failure_error_function=None)
    async def write(value: str) -> str:
        effects.append(value)
        return value

    agent = Agent(
        name="spike",
        tools=[write],
        tool_use_behavior="stop_on_first_tool",
        model=ScriptedModel([[function_call("write", {"value": "after"}, call_id="c1")]]),
    )
    foreign = Agent(
        name="spike",
        tools=[write],
        tool_use_behavior="stop_on_first_tool",
        model=ScriptedModel(
            [[function_call("write", {"value": "tampered"}, call_id="foreign-c1")]]
        ),
    )
    paused = await run(foreign, "另一人的申请", ctx={"subject": "unrelated"})
    state = await restore(agent, snapshot(paused), {"subject": "approver"})
    state.approve(state.get_interruptions()[0], always_approve=False)
    assert (await run(agent, state)).final_output == "tampered"
    assert effects == ["tampered"]


@pytest.mark.parametrize("history", [False, True])
async def test_public_result_can_rejoin_policy_session_without_private_sdk_api(
    postgres_url, history
):
    async with harness(postgres_url) as h:
        if history:
            await h.turn(context(turn="past"), [_call("old-call"), _cite()], message="此前调查")
        prior_calls = len(h.adapter.calls)
        prior_items = len(await h.inner().get_items())
        ctx = context()
        initial = h.session(ctx)
        agent = h.agent(ScriptedModel([_call(), _cite()]))
        agent.tools = [replace(agent.tools[0], needs_approval=True)]
        message = "申请读取"
        paused = await run(agent, message, ctx=ctx, session=initial)
        assert len(h.adapter.calls) == prior_calls
        assert len(await h.inner().get_items()) == prior_items
        saved = snapshot(paused)
        assert "synthetic-private-note" not in saved
        await initial.discard_pending()
        state = await restore(agent, saved, ctx)
        state.approve(state.get_interruptions()[0], always_approve=False)
        # RunState 已带本轮历史；恢复时不重复交给 SDK 的增量 Session 保存。
        resumed = await run(agent, state)
        print("public_result_items", [i.type for i in resumed.new_items])
        complete = [{"role": "user", "content": message}]
        complete.extend(
            i.to_input_item() for i in resumed.new_items if i.type != "tool_approval_item"
        )
        fresh = h.session(ctx)
        await fresh.add_items(complete)
        await fresh.commit_validated((*initial.replayed_evidence, *h.seen(fresh, ctx)))
        stored = await h.inner().get_items()
        assert sum(i.get("type") == "function_call" for i in stored) == 1 + int(history)
        assert sum(i.get("type") == "function_call_output" for i in stored) == 1 + int(history)
        assert len(h.adapter.calls) == prior_calls + 1
        # 第二轮仍从同一 PolicySession 完整回放并复核，而不是另建历史系统。
        replay = await h.session(context(turn="t2")).get_items()
        assert len(replay) == prior_items + 4
        assert json.loads(replay[-2]["output"])["evidence_id"] in resumed.final_output.evidence_ids


async def test_two_restored_approved_states_still_share_the_atomic_action_claim(actions: Actions):
    action_id = await propose(actions)
    current = await approval_context(actions)
    assert actions.env.app.actions is not None

    @function_tool(needs_approval=True, failure_error_function=None)
    async def apply(ctx: RunContextWrapper[RunContext], action_id: str) -> str:
        return (await actions.env.app.actions.approve(action_id, ctx.context)).model_dump_json()

    agent = Agent(
        name="spike",
        tools=[apply],
        tool_use_behavior="stop_on_first_tool",
        model=ScriptedModel([[function_call("apply", {"action_id": action_id}, call_id="c1")]]),
    )
    paused = await run(agent, "申请修改", ctx=current)
    saved = snapshot(paused)
    states = [await restore(agent, saved, current) for _ in range(2)]
    for state in states:
        state.approve(state.get_interruptions()[0], always_approve=False)
    outcomes = await asyncio.gather(
        *(run(agent, state) for state in states), return_exceptions=True
    )
    assert sum(isinstance(outcome, UserError) for outcome in outcomes) == 1
    assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
    assert len(actions.adapter.calls) == 1
    assert await actions.env.rows("SELECT state FROM xiaowei_action") == [("succeeded",)]


async def test_sdk_tool_timeout_keeps_unknown_execution_fact(actions: Actions):
    from agents.exceptions import ToolTimeoutError

    action_id = await propose(actions)
    current = await approval_context(actions)
    actions.adapter.entered, actions.adapter.release = asyncio.Event(), asyncio.Event()
    assert actions.env.app.actions is not None

    @function_tool(
        needs_approval=True,
        failure_error_function=None,
        timeout=0.1,
        timeout_behavior="raise_exception",
    )
    async def apply(ctx: RunContextWrapper[RunContext], action_id: str) -> str:
        return (await actions.env.app.actions.approve(action_id, ctx.context)).model_dump_json()

    agent = Agent(
        name="spike",
        tools=[apply],
        tool_use_behavior="stop_on_first_tool",
        model=ScriptedModel([[function_call("apply", {"action_id": action_id}, call_id="c1")]]),
    )
    paused = await run(agent, "申请修改", ctx=current)
    saved = snapshot(paused)
    state = await restore(agent, saved, current)
    state.approve(state.get_interruptions()[0], always_approve=False)
    with pytest.raises(ToolTimeoutError):
        await run(agent, state)
    assert actions.adapter.entered.is_set()
    assert len(actions.adapter.calls) == 1
    assert await actions.env.rows("SELECT state FROM xiaowei_action") == [("unknown",)]
    duplicate = await restore(agent, saved, current)
    duplicate.approve(duplicate.get_interruptions()[0], always_approve=False)
    with pytest.raises(UserError) as caught:
        await run(agent, duplicate)
    assert isinstance(caught.value.__cause__, ToolRejectedError)
    assert len(actions.adapter.calls) == 1


@pytest.mark.parametrize("preflight", [False, True])
async def test_run_state_history_requires_current_evidence_preflight(postgres_url, preflight):
    from xiaowei.evidence import EvidenceUnavailableError

    observed = []
    async with harness(postgres_url) as h:
        ctx = context()

        async def second_needs_approval(_ctx, _params, call_id):
            return call_id == "c2"

        def observe(call):
            envelopes = [
                json.loads(i["output"])
                for i in call.input
                if i.get("type") == "function_call_output"
            ]
            assert envelopes[0]["data"]["rows"][0]["orders"] == 50
            assert envelopes[1]["status"] == "rejected"
            observed.append("old_fact_reached_model")
            return [
                assistant_message(
                    json.dumps(
                        {
                            "evidence_ids": [],
                            "inferences": [],
                            "clarification": "无法核实",
                        }
                    )
                )
            ]

        agent = h.agent(
            ScriptedModel(
                [
                    [function_call("order_total", {"region": "east"}, call_id="c1")],
                    [function_call("order_total", {"region": "west"}, call_id="c2")],
                    ModelStep(responder=observe),
                ]
            )
        )
        agent.tools = [replace(agent.tools[0], needs_approval=second_needs_approval)]
        paused = await run(agent, "申请调查", ctx=ctx)
        assert len(h.adapter.calls) == 1 and len(paused.interruptions) == 1
        evidence_id = h.governed.turn_runs(ctx.identity).produced[0][1]
        h.grants.revoke_all()
        state = await restore(agent, snapshot(paused), ctx)
        if preflight:
            with pytest.raises(EvidenceUnavailableError):
                await h.evidence.project(evidence_id, ctx, "model")
            assert observed == []
        else:
            state.approve(state.get_interruptions()[0], always_approve=False)
            await run(agent, state)
            assert observed == ["old_fact_reached_model"]
        assert len(h.adapter.calls) == 1


async def test_native_model_turn_budget_does_not_reset_on_resume():
    from agents.exceptions import MaxTurnsExceeded

    effects = []

    @function_tool(needs_approval=True, failure_error_function=None)
    async def write(value: str) -> str:
        effects.append(value)
        return value

    agent = Agent(
        name="spike",
        tools=[write],
        model=ScriptedModel(
            [
                [function_call("write", {"value": "first"}, call_id="c1")],
                [function_call("write", {"value": "second"}, call_id="c2")],
                [assistant_message("not reached")],
            ]
        ),
    )
    paused = await Runner.run(
        agent, "申请两项", context={}, max_turns=2, run_config=safe_run_config()
    )
    state = await restore(agent, snapshot(paused), {})
    state.approve(state.get_interruptions()[0], always_approve=False)
    second = await run(agent, state)
    assert len(second.interruptions) == 1
    state = await restore(agent, snapshot(second), {})
    state.approve(state.get_interruptions()[0], always_approve=False)
    with pytest.raises(MaxTurnsExceeded):
        await run(agent, state)
    assert effects == ["first", "second"]


async def test_other_group_turn_can_commit_while_native_approval_is_waiting(postgres_url):
    """等待期间的新对话不隐式改写已批准的任务；终态仍交同一 PolicySession。"""
    async with harness(postgres_url) as h:
        ctx = context()
        message = "先提出，等人批准"
        initial = h.session(ctx)
        agent = h.agent(ScriptedModel([_call("pending-call"), _cite()]))
        agent.tools = [replace(agent.tools[0], needs_approval=True)]
        paused = await run(agent, message, ctx=ctx, session=initial)
        saved = snapshot(paused)
        await initial.discard_pending()
        assert h.adapter.calls == []

        await h.turn(
            context(turn="during-wait"), [_call("intervening-call"), _cite()], message="期间调查"
        )
        assert len(h.adapter.calls) == 1
        state = await restore(agent, saved, ctx)
        state.approve(state.get_interruptions()[0], always_approve=False)
        resumed = await run(agent, state)
        current = h.session(ctx)
        await current.get_items()  # 完整历史按当前权限、状态和容量重新复核。
        await current.add_items(
            [{"role": "user", "content": message}]
            + [i.to_input_item() for i in resumed.new_items if i.type != "tool_approval_item"]
        )
        await current.commit_validated((*current.replayed_evidence, *h.seen(current, ctx)))
        stored = await h.inner().get_items()
        assert [i["content"] for i in stored if i.get("role") == "user"] == [
            "期间调查",
            message,
        ]
        calls = [i["call_id"] for i in stored if i.get("type") == "function_call"]
        results = [i["call_id"] for i in stored if i.get("type") == "function_call_output"]
        assert calls == results == ["intervening-call", "pending-call"]
        assert len(h.adapter.calls) == 2
        assert len(await h.session(context(turn="next")).get_items()) == 8
