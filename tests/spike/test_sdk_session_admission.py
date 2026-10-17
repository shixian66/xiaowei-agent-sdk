"""会话准入的 PostgreSQL 原语实验及现有执行事实回执；不是正式 SDK 审批实现。

原语实验仅证明：同一事务中锁定原会话、消耗保存状态和占用 Action，
可以与产品的会话关闭排序；没有复制完整权限、证据、送达或实例准入。
"""

import asyncio

import pytest
from agents import Agent, function_tool
from agents.testing import ScriptedModel, function_call
from sqlalchemy import text
from tests.sdk_core.test_action_lifecycle import propose
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_actions import Actions
from tests.sdk_core.test_group_actions import actions as actions
from tests.sdk_core.test_group_gateway import running
from tests.sdk_core.test_group_identity import B
from tests.spike.test_sdk_hitl_reuse import approval_context, run, snapshot

from xiaowei.session import close_sessions

pytestmark = pytest.mark.loopback


async def saved_candidate(actions):
    action_id = await propose(actions)
    current = await approval_context(actions)
    effects = []

    @function_tool(needs_approval=True, failure_error_function=None)
    async def apply(action_id: str) -> str:
        effects.append(action_id)
        return action_id

    agent = Agent(
        name="session-admission-spike",
        tools=[apply],
        model=ScriptedModel([[function_call("apply", {"action_id": action_id}, call_id="c1")]]),
    )
    paused = await run(agent, "申请修改", ctx=current)
    assert len(paused.interruptions) == 1 and effects == []
    async with actions.env.engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE sdk_spike_resume (action_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL,"
                " consumed BOOLEAN NOT NULL DEFAULT FALSE, failure_code TEXT)"
            )
        )
        await conn.execute(
            text("INSERT INTO sdk_spike_resume(action_id, snapshot) VALUES (:id, :snapshot)"),
            {"id": action_id, "snapshot": snapshot(paused)},
        )
    return action_id, current, effects


async def session_id_of(actions, action_id):
    ((session_id,),) = await actions.env.rows(
        "SELECT proposal_session_id FROM xiaowei_action WHERE action_id=:id", id=action_id
    )
    return session_id


async def claim_primitive(
    actions, action_id, current, *, locked=None, release=None, application_name=None
):
    """只验证事务排序；正式 claim 必须再包含现有所有业务准入条件。"""
    async with actions.env.engine.begin() as conn:
        if application_name is not None:
            await conn.execute(
                text("SELECT set_config('application_name', :name, true)"),
                {"name": application_name},
            )
        state, expires_at = (
            await conn.execute(
                text(
                    "SELECT s.state, s.expires_at FROM xiaowei_session s JOIN xiaowei_action a"
                    " ON s.session_id=a.proposal_session_id WHERE a.action_id=:id FOR UPDATE OF s"
                ),
                {"id": action_id},
            )
        ).one()
        if locked is not None:
            locked.set()
        if release is not None:
            await release.wait()
        valid = state == "active" and expires_at > actions.env.clock()
        changed = await conn.scalar(
            text(
                "UPDATE xiaowei_action SET state=:state, attempt=:attempt,"
                " approver_id=:approver, approval_turn_id=:turn WHERE action_id=:id"
                " AND state='pending' RETURNING action_id"
            ),
            {
                "id": action_id,
                "state": "executing" if valid else "rejected",
                "attempt": "sdk-spike-attempt" if valid else None,
                "approver": B if valid else None,
                "turn": current.identity.turn_id if valid else None,
            },
        )
        if changed is None:
            return "already_consumed"
        code = None if valid else "action_session_invalid"
        await conn.execute(
            text(
                "UPDATE sdk_spike_resume SET consumed=TRUE, failure_code=:code WHERE action_id=:id"
            ),
            {"id": action_id, "code": code},
        )
        return "claimed" if valid else code


@pytest.mark.parametrize("case", ["sealed", "closed", "expired"])
async def test_invalid_original_session_rejects_before_restore(actions: Actions, case):
    action_id, current, effects = await saved_candidate(actions)
    session_id = await session_id_of(actions, action_id)
    async with actions.env.engine.begin() as conn:
        if case == "closed":
            await close_sessions(conn, (session_id,))
        elif case == "sealed":
            await conn.execute(
                text("UPDATE xiaowei_session SET state='sealed' WHERE session_id=:id"),
                {"id": session_id},
            )
        else:
            await conn.execute(
                text("UPDATE xiaowei_session SET expires_at=:now WHERE session_id=:id"),
                {"id": session_id, "now": actions.env.clock()},
            )
    assert await claim_primitive(actions, action_id, current) == "action_session_invalid"
    assert await actions.env.rows("SELECT state, attempt FROM xiaowei_action") == [
        ("rejected", None)
    ]
    assert await actions.env.rows("SELECT consumed, failure_code FROM sdk_spike_resume") == [
        (True, "action_session_invalid")
    ]
    assert await claim_primitive(actions, action_id, current) == "already_consumed"
    assert effects == [] and actions.adapter.calls == []


async def wait_for_session_lock(engine, application_name):
    """只观察本用例数据库和已标记的连接，避免其他工作树干扰。"""
    async with asyncio.timeout(3):
        while True:
            async with engine.connect() as conn:
                waiting = await conn.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity"
                        " WHERE datname=current_database()"
                        " AND application_name=:name AND wait_event_type='Lock')"
                    ),
                    {"name": application_name},
                )
            if waiting:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("winner", ["close", "claim"])
async def test_close_and_claim_have_one_atomic_order(actions: Actions, winner):
    action_id, current, effects = await saved_candidate(actions)
    env = actions.env
    session_id = await session_id_of(actions, action_id)
    marked = "sdk-spike-close-waiter" if winner == "claim" else "sdk-spike-claim-waiter"

    async def close():
        async with env.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('application_name', :name, true)"), {"name": marked}
            )
            await close_sessions(conn, (session_id,))

    if winner == "close":
        async with env.engine.begin() as conn:
            await close_sessions(conn, (session_id,))
            waiter = asyncio.create_task(
                claim_primitive(actions, action_id, current, application_name=marked)
            )
            await wait_for_session_lock(env.engine, marked)
        assert await asyncio.wait_for(waiter, 3) == "action_session_invalid"
        expected = ("rejected", None)
    else:
        locked, release = asyncio.Event(), asyncio.Event()
        claimer = asyncio.create_task(
            claim_primitive(actions, action_id, current, locked=locked, release=release)
        )
        await asyncio.wait_for(locked.wait(), 3)
        waiter = asyncio.create_task(close())
        try:
            await wait_for_session_lock(env.engine, marked)
        finally:
            release.set()
        assert await asyncio.wait_for(claimer, 3) == "claimed"
        await asyncio.wait_for(waiter, 3)
        expected = ("executing", "sdk-spike-attempt")
    assert await env.rows("SELECT state, attempt FROM xiaowei_action") == [expected]
    assert await env.rows(
        "SELECT state FROM xiaowei_session WHERE session_id=:id", id=session_id
    ) == [("closed",)]
    assert effects == [] and actions.adapter.calls == []


async def test_cancelled_admission_rolls_back_both_claim_and_snapshot(actions: Actions):
    action_id, current, effects = await saved_candidate(actions)
    env = actions.env
    entered, release = asyncio.Event(), asyncio.Event()

    async def incomplete_transaction():
        async with env.engine.begin() as conn:
            await conn.execute(
                text("SELECT 1 FROM xiaowei_session WHERE session_id=:id FOR UPDATE"),
                {"id": await session_id_of(actions, action_id)},
            )
            await conn.execute(
                text(
                    "UPDATE xiaowei_action SET state='executing', attempt='cancelled-spike',"
                    " approver_id=:approver, approval_turn_id=:turn WHERE action_id=:id"
                ),
                {"id": action_id, "approver": B, "turn": current.identity.turn_id},
            )
            await conn.execute(text("UPDATE sdk_spike_resume SET consumed=TRUE"))
            entered.set()
            await release.wait()

    task = asyncio.create_task(incomplete_transaction())
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert await env.rows("SELECT state, attempt FROM xiaowei_action") == [("pending", None)]
    assert await env.rows("SELECT consumed FROM sdk_spike_resume") == [(False,)]
    assert await claim_primitive(actions, action_id, current) == "claimed"
    assert effects == [] and actions.adapter.calls == []


async def test_inflight_write_receipt_does_not_require_original_session(actions: Actions):
    """SDK-A在写中关闭后，经Action事实失败回执反馈，不丢成功、不重放。"""
    action_id = await propose(actions)
    env, adapter = actions.env, actions.adapter
    session_id = await session_id_of(actions, action_id)
    history = await env.rows("SELECT message_data FROM agent_messages ORDER BY id")
    adapter.entered, adapter.release = asyncio.Event(), asyncio.Event()
    async with running(env, max_reply_chars=3500) as group:
        approval = asyncio.create_task(
            group.ask(f"/批准 {action_id}", sender=B, message_id="om_close_while_writing")
        )
        try:
            await asyncio.wait_for(adapter.entered.wait(), 3)
            async with env.engine.begin() as conn:
                await close_sessions(conn, (session_id,))
        finally:
            adapter.release.set()
        await asyncio.wait_for(approval, 5)
        rendered = message_text(group.outbox.sent[-1][1])
        assert "动作已执行" in rendered and "Action ID" in rendered
        assert "工具结果不可用" not in rendered
        assert await env.rows(
            "SELECT state, delivery FROM xiaowei_request WHERE reply_message_id=:id",
            id="om_close_while_writing",
        ) == [("failed", "sent")]
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_close_duplicate")
    assert len(adapter.calls) == 1
    assert await env.rows("SELECT state, result FROM xiaowei_action") == [
        ("succeeded", '{"value": "after"}')
    ]
    assert await env.rows(
        "SELECT state FROM xiaowei_session WHERE session_id=:id", id=session_id
    ) == [("closed",)]
    assert await env.rows("SELECT message_data FROM agent_messages ORDER BY id") == history
