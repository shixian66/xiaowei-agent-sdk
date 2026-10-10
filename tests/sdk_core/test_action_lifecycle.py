"""真实 PostgreSQL 的执行占用、取消、实例接管与独立保留期。"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_group_actions import Actions
from tests.sdk_core.test_group_actions import actions as actions  # pytest fixture
from tests.sdk_core.test_group_gateway import running
from tests.sdk_core.test_group_identity import GROUP, A, Env

from xiaowei.actions import ActionService
from xiaowei.channel_store import NotReadyError
from xiaowei.feishu import attributed
from xiaowei.models import Identity
from xiaowei.session import cleanup_expired
from xiaowei.storage import hold_instance_lock

pytestmark = pytest.mark.loopback


async def propose(actions: Actions) -> str:
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"), tool_call("am-synthetic__propose_change", value="after"), cite()
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("提出", message_id="om_p")
    assert await env.rows(
        "SELECT state, delivery FROM xiaowei_request WHERE reply_message_id='om_p'"
    ) == [("completed", "sent")]
    return await actions.one()


@asynccontextmanager
async def takeover(actions: Actions):
    """终止旧持锁后端，再由全新应用存储和实例锁接管；不伪造 readiness。"""
    old = actions.env
    async with old.engine.begin() as conn:
        assert (
            await conn.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": actions.lock.pid})
            is True
        )
    new = Env(
        old.engine,
        old.clock,
        old.members,
        old.access,
        old.adapter,
        old.scripts,
        old.evidence,
        old.app,
    )
    new.app.actions = ActionService(
        actions.governance, new.store, new.access, [actions.binding], clock=new.clock
    )
    async with hold_instance_lock(new.engine, new.store.readiness) as lock:
        await new.store.recover(lock)
        yield new


async def test_database_claim_is_single_even_without_gateway_fifo(actions: Actions) -> None:
    action_id = await propose(actions)
    env = actions.env
    receipts = await asyncio.gather(
        *[
            env.service.accept(env.group(attributed(A, f"/批准 {action_id}"), f"approve-{n}"))
            for n in range(12)
        ]
    )
    identities = []
    for receipt in receipts:
        record = await env.store.start(receipt.record, message=receipt.message, policy_version="w5")
        identities.append(
            Identity(
                subject_id=A,
                owner=GROUP.owner,
                channel="feishu",
                session_id=record.session_id,
                turn_id=record.turn_id,
            )
        )
    action = await env.store.get_action(action_id, identities[0])
    claims = await asyncio.gather(
        *[env.store.claim_action(action, identity) for identity in identities]
    )
    accepted = [claim for claim in claims if claim is not None]
    assert len(accepted) == 1
    assert await env.store.claim_action(action, identities[0]) is None
    assert actions.adapter.calls == []
    assert await env.rows("SELECT state, attempt FROM xiaowei_action") == [
        ("executing", accepted[0].attempt)
    ]


@pytest.mark.parametrize("elapsed_seconds", [0, 1, 2])
async def test_approval_expiry_is_rechecked_after_readback(
    actions: Actions, elapsed_seconds: int
) -> None:
    action_id = await propose(actions)
    env, adapter = actions.env, actions.adapter
    env.clock.now += timedelta(minutes=14, seconds=59)
    adapter.read_entered, adapter.read_release = asyncio.Event(), asyncio.Event()
    receipt = await env.service.accept(env.group(attributed(A, f"/批准 {action_id}"), "om_a"))
    task = asyncio.create_task(env.service.approve(receipt, action_id))
    await asyncio.wait_for(adapter.read_entered.wait(), 5)
    assert await env.rows("SELECT state FROM xiaowei_action") == [("executing",)]
    env.clock.now += timedelta(seconds=elapsed_seconds)
    adapter.read_release.set()
    record = await task

    if elapsed_seconds == 0:
        assert [r.arguments for r in adapter.calls] == [{"value": "after"}]
        assert record.state == "completed"
        assert await env.rows("SELECT state FROM xiaowei_action") == [("succeeded",)]
    else:
        assert adapter.calls == []
        assert record.state == "failed"
        assert await env.rows("SELECT state, result FROM xiaowei_action") == [("rejected", None)]
        assert await env.rows(
            "SELECT count(*) FROM xiaowei_evidence WHERE turn_id=:turn", turn=record.turn_id
        ) == [(0,)]


@pytest.mark.parametrize("when", ["read", "write"])
async def test_cancelled_execution_recovers_unknown_and_is_never_replayed(
    actions: Actions, when: str
) -> None:
    action_id = await propose(actions)
    env, adapter = actions.env, actions.adapter
    entered, release = asyncio.Event(), asyncio.Event()
    if when == "read":
        adapter.read_entered, adapter.read_release = entered, release
    else:
        adapter.entered, adapter.release = entered, release
    receipt = await env.service.accept(env.group(attributed(A, f"/批准 {action_id}"), "om_a"))
    task = asyncio.create_task(env.service.approve(receipt, action_id))
    await asyncio.wait_for(entered.wait(), 5)
    assert await env.rows("SELECT state FROM xiaowei_action") == [("executing",)]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not env.store.readiness.ok
    assert len(adapter.calls) == (0 if when == "read" else 1)
    async with takeover(actions) as new:
        assert await new.rows("SELECT state FROM xiaowei_action") == [("unknown",)]
        assert await new.rows(
            "SELECT state FROM xiaowei_request WHERE turn_id=:turn", turn=receipt.record.turn_id
        ) == [("interrupted",)]
        async with running(new, max_reply_chars=3500) as group:
            await group.ask(f"/批准 {action_id}", message_id="om_again")
    assert len(adapter.calls) == (0 if when == "read" else 1)


async def test_loss_of_instance_lock_during_recheck_prevents_write(actions: Actions) -> None:
    action_id = await propose(actions)
    env, adapter = actions.env, actions.adapter
    adapter.read_entered, adapter.read_release = asyncio.Event(), asyncio.Event()
    receipt = await env.service.accept(env.group(attributed(A, f"/批准 {action_id}"), "om_a"))
    task = asyncio.create_task(env.service.approve(receipt, action_id))
    await asyncio.wait_for(adapter.read_entered.wait(), 5)
    async with takeover(actions) as new:
        adapter.read_release.set()
        with pytest.raises(NotReadyError):
            await task
        assert adapter.calls == []
        assert await new.rows("SELECT state FROM xiaowei_action") == [("unknown",)]


async def test_sent_pending_action_survives_restart_and_can_execute_once(actions: Actions) -> None:
    action_id = await propose(actions)
    async with takeover(actions) as new:
        assert await new.rows("SELECT state FROM xiaowei_action") == [("pending",)]
        async with running(new, max_reply_chars=3500) as group:
            await group.ask(f"/批准 {action_id}", message_id="om_a")
            await group.ask(f"/批准 {action_id}", message_id="om_again")
    assert len(actions.adapter.calls) == 1
    assert await new.rows("SELECT state FROM xiaowei_action") == [("succeeded",)]


async def test_stale_attempt_cannot_overwrite_recovered_unknown(actions: Actions) -> None:
    action_id = await propose(actions)
    env = actions.env
    receipt = await env.service.accept(env.group(attributed(A, f"/批准 {action_id}"), "om_a"))
    record = await env.store.start(receipt.record, message=receipt.message, policy_version="w5")
    identity = Identity(
        subject_id=A,
        owner=GROUP.owner,
        channel="feishu",
        session_id=record.session_id,
        turn_id=record.turn_id,
    )
    claimed = await env.store.claim_action(
        await env.store.get_action(action_id, identity), identity
    )
    assert claimed is not None
    async with takeover(actions) as new:
        with pytest.raises(NotReadyError):
            await env.store.finish_action(claimed, "succeeded", '{"value":"after"}')
        # 即使使用新存储，已作废的尝试也不能覆写恢复事实。
        with pytest.raises(NotReadyError):
            await new.store.finish_action(replace(claimed), "succeeded", '{"value":"after"}')
        assert await new.rows("SELECT state, result FROM xiaowei_action") == [("unknown", None)]
    assert actions.adapter.calls == []


@pytest.mark.parametrize("state", ["pending", "succeeded", "unknown", "executing"])
async def test_cleanup_uses_action_retention_and_keeps_executing(
    actions: Actions, state: str
) -> None:
    await propose(actions)
    env = actions.env
    messages = await env.rows("SELECT count(*) FROM agent_messages")
    async with env.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE xiaowei_action SET state=:state, attempt=:attempt, approver_id=:actor, "
                "approval_turn_id=:turn"
            ),
            {
                "state": state,
                "attempt": None if state == "pending" else "attempt-1",
                "actor": None if state == "pending" else A,
                "turn": None if state == "pending" else "approval-1",
            },
        )
    env.clock.now += timedelta(hours=1, seconds=1)
    await cleanup_expired(env.engine, now=env.clock(), batch_size=1)
    assert await env.rows("SELECT count(*) FROM xiaowei_action") == [
        (1 if state == "executing" else 0,)
    ]
    assert await env.rows("SELECT count(*) FROM agent_messages") == messages
    if state == "executing":
        await env.store.recover(actions.lock)
        assert await env.rows("SELECT state FROM xiaowei_action") == [("unknown",)]
        await cleanup_expired(env.engine, now=env.clock(), batch_size=1)
        assert await env.rows("SELECT count(*) FROM xiaowei_action") == [(0,)]
