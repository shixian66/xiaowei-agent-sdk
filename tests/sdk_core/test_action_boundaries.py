"""W5 的失败窗口与治理边界；所有存储与 SDK 路径为真实实现。"""

import asyncio

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from tests.sdk_core.test_app import cite, clarify, tool_call
from tests.sdk_core.test_channel_service import BUDGET
from tests.sdk_core.test_feishu import Outbox
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_actions import PROPOSE, SHARED, SOURCE, STATUS, WRITE, Actions
from tests.sdk_core.test_group_actions import actions as actions  # pytest fixture
from tests.sdk_core.test_group_gateway import group_config, running
from tests.sdk_core.test_group_identity import GROUP, A, B

from xiaowei.channel import ChannelService, ResultDelivery
from xiaowei.feishu import attributed
from xiaowei.feishu_render import build_feishu_message
from xiaowei.governance import Prechecked, ToolRejectedError
from xiaowei.models import Delivery, DeliveryFact, Identity, RunContext, ToolRequest
from xiaowei.session import close_sessions

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize("outcome", ["sent", "failed", "unknown"])
async def test_capacity_notice_never_authorizes_unshown_action(
    actions: Actions, outcome: str
) -> None:
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    outbox = Outbox(outcomes=[outcome])
    async with running(env, outbox, max_reply_chars=200) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        await group.ask(f"/批准 {action_id}", message_id="om_a")
    assert actions.adapter.calls == []
    assert "单消息容量不足" in message_text(outbox.sent[0][1])
    assert await env.rows("SELECT delivery FROM xiaowei_request WHERE reply_message_id='om_p'") == [
        ("failed" if outcome == "sent" else outcome,)
    ]


async def test_uncited_proposal_cannot_be_approved(actions: Actions) -> None:
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), clarify()
    )
    async with running(env) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        await group.ask(f"/批准 {action_id}", message_id="om_a")
    assert actions.adapter.calls == []
    assert await env.rows("SELECT state FROM xiaowei_request WHERE reply_message_id='om_p'") == [
        ("failed",)
    ]


@pytest.mark.parametrize("closed", [False, True])
async def test_full_or_closed_session_still_allows_valid_approval(
    actions: Actions, closed: bool
) -> None:
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        ((sid,),) = await env.rows(
            "SELECT session_id FROM xiaowei_request WHERE reply_message_id='om_p'"
        )
        if closed:
            async with env.engine.begin() as conn:
                await close_sessions(conn, [sid])
        else:
            for n in range(4):
                env.scripts.add(attributed(A, f"解释{n}"), clarify())
                await group.ask(f"解释{n}", message_id=f"om_{n}")
        before = await env.rows("SELECT message_data FROM agent_messages ORDER BY id")
        await group.ask(f"/批准 {action_id}", message_id="om_a")
        assert len(actions.adapter.calls) == 1
        assert await env.rows("SELECT message_data FROM agent_messages ORDER BY id") == before
        await group.ask("/新建", message_id="om_new")
        env.scripts.add(
            attributed(A, "查看状态"),
            tool_call("am-synthetic__get_action_status", action_id=action_id),
            cite(),
        )
        await group.ask("查看状态", message_id="om_status")
    assert await env.rows(
        "SELECT state, delivery FROM xiaowei_request WHERE reply_message_id='om_status'"
    ) == [("completed", "sent")]


async def test_unknown_write_is_not_replayed_by_another_approval(actions: Actions) -> None:
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        actions.adapter.fail = True
        await group.ask(f"/批准 {action_id}", message_id="om_a")
        actions.adapter.fail = False
        await group.ask(f"/批准 {action_id}", message_id="om_again")
    assert len(actions.adapter.calls) == 1
    assert await env.rows("SELECT state, result FROM xiaowei_action") == [("unknown", None)]


async def test_same_group_approval_and_agent_turn_are_serial(actions: Actions) -> None:
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    actions.adapter.entered, actions.adapter.release = asyncio.Event(), asyncio.Event()
    next_message = env.scripts.add(attributed(B, "解释状态"), clarify())
    async with running(env, consumer_count=2, max_reply_chars=3500) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        await group.gateway.receive(group.event(f"/批准 {action_id}", message_id="om_a"))
        await asyncio.wait_for(actions.adapter.entered.wait(), 5)
        await group.gateway.receive(group.event("解释状态", sender=B, message_id="om_next"))
        assert env.model_calls(next_message) == 0
        actions.adapter.release.set()
        await asyncio.wait_for(group.gateway.idle(), 5)
    assert env.model_calls(next_message) == 1 and len(actions.adapter.calls) == 1


async def test_write_tools_hidden_and_unapproved_calls_have_zero_io(actions: Actions) -> None:
    ctx = RunContext(
        identity=Identity(
            subject_id=A, channel="feishu", owner=GROUP.owner, session_id="s", turn_id="t"
        ),
        target_scope=frozenset({SOURCE}),
        tool_scope=frozenset({WRITE}),
        budget=BUDGET,
    )
    assert actions.governance.allowed_contracts(ctx) == []
    request = ToolRequest(
        tool_id=WRITE,
        target_id=SOURCE,
        call_id="c",
        tool_name="change",
        arguments={"value": "after"},
    )
    with pytest.raises(ToolRejectedError, match="批准绑定"):
        await actions.governance.invoke(ctx, request, actions.adapter.execute)
    assert actions.adapter.calls == []


def test_large_read_result_can_clip_without_hiding_approval_material() -> None:
    from tests.sdk_core.synthetic_tools import START

    proposal = DeliveryFact(
        evidence_id="ev_p",
        tool_id="am/propose",
        target_id="am",
        captured_at=START,
        truncated=False,
        columns=(),
        rows=(),
        metadata={
            "action_id": "a",
            "change": "only-host1",
            "parameters": "host1",
            "rollback": "new approval to restore",
            "approve_before": "soon",
        },
        requires_complete=True,
    )
    reading = DeliveryFact(
        evidence_id="ev_r",
        tool_id="am/read",
        target_id="am",
        captured_at=START,
        truncated=False,
        columns=("label",),
        rows=tuple({"label": "x" * 90} for _ in range(50)),
        metadata={},
    )
    delivery = Delivery(
        content="",
        evidence_ids=("ev_p", "ev_r"),
        channel="feishu",
        facts=(proposal, reading),
        requires_complete=True,
    )
    shown = message_text(build_feishu_message(delivery, 1200, "oc_group"))
    assert "only-host1" in shown and "new approval to restore" in shown
    assert "事实展示已截断" in shown


async def test_status_feedback_does_not_reset_approval_tool_budget(actions: Actions) -> None:
    env = actions.env
    env.results = ResultDelivery(
        env.store, env.evidence, env.access, budget=BUDGET.model_copy(update={"max_tool_calls": 1})
    )
    env.service = ChannelService(env.app, env.results)
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        await group.ask(f"/批准 {action_id}", message_id="om_a")
    assert len(actions.adapter.calls) == 1
    assert await env.rows("SELECT state FROM xiaowei_action") == [("succeeded",)]
    assert await env.rows("SELECT state FROM xiaowei_request WHERE reply_message_id='om_a'") == [
        ("failed",)
    ]
    assert await env.rows("SELECT count(*) FROM xiaowei_evidence") == [(2,)]


@pytest.mark.parametrize(
    "stage", ["proposal_evidence", "write_evidence", "feedback_evidence", "feedback_save"]
)
async def test_storage_failure_never_replays_a_write(actions: Actions, stage: str) -> None:
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    armed = stage == "proposal_evidence"
    seen = 0

    def fail_sql(_conn, _cursor, statement, _parameters, *_args):
        nonlocal seen
        if not armed:
            return
        if "INSERT INTO xiaowei_evidence" in statement:
            seen += 1
            should_fail = stage in ("proposal_evidence", "write_evidence") or (
                stage == "feedback_evidence" and seen == 2
            )
            if should_fail:
                raise SQLAlchemyError("synthetic storage failure")
        if stage == "feedback_save" and "SET state = 'completed'" in statement:
            raise SQLAlchemyError("synthetic save failure")

    event.listen(env.engine.sync_engine, "before_cursor_execute", fail_sql)
    try:
        async with running(env, max_reply_chars=3500) as group:
            await group.ask("提出", message_id="om_p")
            action_id = await actions.one()
            armed = True
            await group.ask(f"/批准 {action_id}", message_id="om_a")
            armed = False
            await group.ask(f"/批准 {action_id}", message_id="om_again")
    finally:
        event.remove(env.engine.sync_engine, "before_cursor_execute", fail_sql)
    assert len(actions.adapter.calls) == (0 if stage == "proposal_evidence" else 1)
    assert await env.rows("SELECT state FROM xiaowei_action") == [
        ("pending" if stage == "proposal_evidence" else "succeeded",)
    ]


async def test_concurrent_approval_messages_execute_only_once(actions: Actions) -> None:
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    config = group_config().group.model_copy(update={"max_waiting": 12})
    async with running(
        env, consumer_count=2, queue_size=14, group=config, max_reply_chars=3500
    ) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        await asyncio.gather(
            *[
                group.gateway.receive(
                    group.event(f"/批准 {action_id}", sender=B, message_id=f"om_a{n}")
                )
                for n in range(12)
            ]
        )
        await group.gateway.idle()
    assert len(actions.adapter.calls) == 1
    assert await env.rows("SELECT count(*) FROM xiaowei_request WHERE state='completed'") == [(2,)]


@pytest.mark.parametrize("mutation", ["parameters", "target", "binding", "evidence_deleted"])
async def test_changed_binding_or_missing_evidence_blocks_before_remote_io(
    actions: Actions, mutation: str
) -> None:
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        async with env.engine.begin() as conn:
            if mutation == "parameters":
                await conn.execute(
                    text("UPDATE xiaowei_action SET plan = replace(plan, 'after', 'changed')")
                )
            elif mutation == "target":
                await conn.execute(text("UPDATE xiaowei_action SET target_id='another-source'"))
            elif mutation == "binding":
                await conn.execute(text("UPDATE xiaowei_action SET binding='changed'"))
            else:
                await conn.execute(text("DELETE FROM xiaowei_evidence"))
        before = len(actions.adapter.reads)
        await group.ask(f"/批准 {action_id}", message_id="om_a")
    assert actions.adapter.calls == [] and len(actions.adapter.reads) == before


async def test_status_permission_revocation_rejects_history_and_resend(actions: Actions) -> None:
    from tests.sdk_core.test_group_identity import group_ref

    from xiaowei.channel import ResultUnavailableError

    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        await group.ask(f"/批准 {action_id}", message_id="om_a")
    from dataclasses import replace

    assert await env.rows("SELECT state FROM xiaowei_request WHERE reply_message_id='om_a'") == [
        ("completed",)
    ]
    assert (await env.results.view(group_ref("om_a"))).delivery.evidence_ids
    # 显式重发只竞争 failed/unknown；已经 sent 的消息不会再次取得投递权。
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE xiaowei_request SET delivery='failed' WHERE reply_message_id='om_a'")
        )
    env.access._group = replace(env.access._group, tools=SHARED - {STATUS})
    with pytest.raises(ResultUnavailableError):
        await env.results.view(group_ref("om_a"))
    sent = Outbox()

    async def transmit(delivery):
        return await sent(GROUP.chat_id, build_feishu_message(delivery, 3500, GROUP.chat_id))

    with pytest.raises(ResultUnavailableError):
        await env.results.send(group_ref("om_a"), transmit, resend=True)
    assert sent.sent == [] and len(actions.adapter.calls) == 1


async def test_recheck_uses_frozen_actual_parameters_not_relative_proposal(
    actions: Actions,
) -> None:
    async def prepare(request):
        actual = request.model_copy(
            update={
                "arguments": {
                    "value": "frozen-at-proposal"
                    if request.arguments["value"] == "now"
                    else request.arguments["value"]
                }
            }
        )
        return await actions.adapter.prepare(actual)

    actions.rebind(prepare=prepare)
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="now"), cite()
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("提出", message_id="om_p")
        await group.ask(f"/批准 {await actions.one()}", message_id="om_a")
    assert [request.arguments for request in actions.adapter.calls] == [
        {"value": "frozen-at-proposal"}
    ]
    assert await env.rows("SELECT state FROM xiaowei_action") == [("succeeded",)]


async def test_source_configuration_change_invalidates_pending_action(actions: Actions) -> None:
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        before = len(actions.adapter.reads)
        actions.rebind(target_binding="synthetic-v2")
        await group.ask(f"/批准 {action_id}", message_id="om_a")
    assert len(actions.adapter.reads) == before and actions.adapter.calls == []


@pytest.mark.parametrize("reject", [True, False])
async def test_write_precheck_is_preserved_and_runs_before_remote_recheck(
    actions: Actions, reject: bool
) -> None:
    checked = []

    def check(request):
        checked.append(request.arguments)
        if reject:
            raise ToolRejectedError("合成参数不可执行")
        return request

    actions.rebind(execute=Prechecked(check, actions.adapter.execute))
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        before = len(actions.adapter.reads)
        await group.ask(f"/批准 {action_id}", message_id="om_a")
    assert checked == [{"value": "after"}]
    assert len(actions.adapter.reads) == before + (0 if reject else 1)
    assert len(actions.adapter.calls) == (0 if reject else 1)
    assert await env.rows("SELECT state FROM xiaowei_action") == [
        ("rejected" if reject else "succeeded",)
    ]


@pytest.mark.parametrize("change", ["left", "grant_revoked"])
async def test_authority_lost_during_readback_prevents_write(actions: Actions, change: str) -> None:
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    actions.adapter.read_entered, actions.adapter.read_release = asyncio.Event(), asyncio.Event()
    actions.adapter.read_release.set()
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        actions.adapter.read_entered.clear()
        actions.adapter.read_release.clear()
        await group.gateway.receive(group.event(f"/批准 {action_id}", message_id="om_a"))
        await asyncio.wait_for(actions.adapter.read_entered.wait(), 5)
        if change == "left":
            env.members.current.remove(A)
        else:
            env.access._grants = {A: SHARED}
        actions.adapter.read_release.set()
        await asyncio.wait_for(group.gateway.idle(), 5)
    assert actions.adapter.calls == []
    assert await env.rows("SELECT state FROM xiaowei_action") == [("rejected",)]


@pytest.mark.parametrize("channel", ["web", "feishu"])
async def test_personal_entry_hides_proposal_write_and_status(
    actions: Actions, channel: str
) -> None:
    ctx = RunContext(
        identity=Identity(subject_id=A, channel=channel, session_id="s", turn_id="t"),
        target_scope=frozenset({SOURCE}),
        tool_scope=frozenset({PROPOSE, WRITE, STATUS}),
        budget=BUDGET,
    )
    assert actions.governance.allowed_contracts(ctx) == []
    assert actions.env.app.actions.tools_for(ctx) == []
    for tool in (PROPOSE, WRITE, STATUS):
        request = ToolRequest(
            tool_id=tool,
            target_id=SOURCE,
            call_id="c",
            tool_name="forced",
            arguments={"value": "after"} if tool != STATUS else {"action_id": "invalid"},
        )
        with pytest.raises(ToolRejectedError, match="指定飞书群"):
            await actions.governance.invoke(ctx, request, actions.adapter.execute)
    assert actions.adapter.reads == [] and actions.adapter.calls == []


async def test_invalid_status_id_is_correctable_before_storage_io(actions: Actions) -> None:
    ctx = RunContext(
        identity=Identity(
            subject_id=A, channel="feishu", owner=GROUP.owner, session_id="s", turn_id="t"
        ),
        target_scope=frozenset({SOURCE}),
        tool_scope=frozenset({STATUS}),
        budget=BUDGET,
    )
    executed = []

    async def status(request):
        executed.append(request)
        return await actions.env.app.actions.status(ctx, request)

    request = ToolRequest(
        tool_id=STATUS,
        target_id=SOURCE,
        call_id="c",
        tool_name="status",
        arguments={"action_id": "not-a-uuid"},
    )
    with pytest.raises(ToolRejectedError):
        await actions.governance.invoke(ctx, request, status)
    assert executed == [] and actions.adapter.reads == [] and actions.adapter.calls == []


@pytest.mark.parametrize("mismatch", ["owner", "source", "retention"])
async def test_status_cannot_reveal_another_group_source_or_expired_action(
    actions: Actions, mismatch: str
) -> None:
    from tests.sdk_core.test_action_lifecycle import propose

    env = actions.env
    action_id = await propose(actions)
    async with env.engine.begin() as conn:
        if mismatch == "owner":
            await conn.execute(text("UPDATE xiaowei_action SET owner_id='other-group'"))
        elif mismatch == "source":
            await conn.execute(text("UPDATE xiaowei_action SET target_id='other-source'"))
        else:
            await conn.execute(
                text("UPDATE xiaowei_action SET approval_expires_at=:now, expires_at=:now"),
                {"now": env.clock()},
            )
    env.scripts.add(
        attributed(A, "状态"),
        tool_call("am-synthetic__get_action_status", action_id=action_id),
        cite(),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("/新建", message_id="om_new")
        await group.ask("状态", message_id="om_status")
        shown = message_text(group.outbox.sent[-1][1])
    assert "unavailable" in shown and "before" not in shown and "after" not in shown
    assert actions.adapter.calls == [] and len(actions.adapter.reads) == 1
