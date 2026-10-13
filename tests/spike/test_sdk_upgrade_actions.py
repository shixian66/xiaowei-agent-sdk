"""SDK-A 升级原语：旧 pending 作废，成功/未知不改，按已存原因投影固定回执。

只在实验表保存专用原因；产品迁移和专用失败码尚未实现。
真实旧群入口验证零写，新增固定回执仅验证计划中的代码投影，不冒充正式入口。
"""

import uuid

import pytest
from sqlalchemy import text
from tests.sdk_core.test_action_lifecycle import propose
from tests.sdk_core.test_group_actions import Actions
from tests.sdk_core.test_group_actions import actions as actions
from tests.sdk_core.test_group_gateway import running
from tests.sdk_core.test_group_identity import GROUP, B

from xiaowei.models import Identity

pytestmark = pytest.mark.loopback
UPGRADE_CODE = "action_upgrade_invalidated"
UPGRADE_RECEIPT = "已因升级作废、未执行，请重新提出"


async def copied_action_rows(actions, pending):
    async with actions.env.engine.begin() as conn:
        original = dict(
            (
                await conn.execute(
                    text("SELECT * FROM xiaowei_action WHERE action_id=:id"), {"id": pending}
                )
            )
            .mappings()
            .one()
        )
        ids = {}
        for state in ("succeeded", "unknown"):
            row = {
                **original,
                "action_id": str(uuid.uuid4()),
                "call_id": f"fixture-{state}",
                "state": state,
                "attempt": f"fixture-attempt-{state}",
                "approver_id": B,
                "approval_turn_id": f"fixture-approval-{state}",
                "result": '{"value": "after"}' if state == "succeeded" else None,
            }
            await conn.execute(
                text(
                    "INSERT INTO xiaowei_action (action_id, owner_id, requester_id,"
                    " proposal_session_id, proposal_turn_id, call_id, proposal_tool_id,"
                    " write_tool_id, target_id, binding, plan, state, attempt, approver_id,"
                    " approval_turn_id, result, created_at, updated_at, approval_expires_at,"
                    " expires_at) VALUES (:action_id, :owner_id, :requester_id,"
                    " :proposal_session_id, :proposal_turn_id, :call_id, :proposal_tool_id,"
                    " :write_tool_id, :target_id, :binding, :plan, :state, :attempt, :approver_id,"
                    " :approval_turn_id, :result, :created_at, :updated_at, :approval_expires_at,"
                    " :expires_at)"
                ),
                row,
            )
            ids[state] = row["action_id"]
    return ids


async def migration_primitive(env):
    async with env.engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS sdk_spike_action_cause"
                " (action_id TEXT PRIMARY KEY, failure_code TEXT NOT NULL"
                " CHECK(failure_code='action_upgrade_invalidated'))"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS sdk_spike_native_state"
                " (action_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
        )
        invalidated = (
            (
                await conn.execute(
                    text(
                        "UPDATE xiaowei_action a SET state='rejected' WHERE state='pending'"
                        " AND NOT EXISTS(SELECT 1 FROM sdk_spike_native_state s"
                        " WHERE s.action_id=a.action_id) RETURNING action_id"
                    )
                )
            )
            .scalars()
            .all()
        )
        for action_id in invalidated:
            await conn.execute(
                text("INSERT INTO sdk_spike_action_cause VALUES (:id, :code)"),
                {"id": action_id, "code": UPGRADE_CODE},
            )
    return invalidated


async def fixed_receipt_projection(actions, action_id, identity):
    env = actions.env
    decision = await env.access.resolve_group(GROUP, identity.subject_id, verify_member=True)
    assert decision is not None
    action = await env.store.get_action(action_id, identity)
    assert await env.access.authorize(identity, action.target_id, action.write_tool_id)
    ((code,),) = await env.rows(
        "SELECT failure_code FROM sdk_spike_action_cause WHERE action_id=:id", id=action_id
    )
    assert action.state == "rejected" and code == UPGRADE_CODE
    return code, UPGRADE_RECEIPT


async def test_legacy_pending_migration_and_receipt_projection(actions: Actions):
    pending = await propose(actions)
    env = actions.env
    kept = await copied_action_rows(actions, pending)
    async with env.engine.connect() as conn:
        before = {
            row["action_id"]: dict(row)
            for row in (await conn.execute(text("SELECT * FROM xiaowei_action"))).mappings()
        }
    assert await migration_primitive(env) == [pending]
    assert await migration_primitive(env) == []  # 已作废不再次占用或改写。
    async with env.engine.connect() as conn:
        after = {
            row["action_id"]: dict(row)
            for row in (await conn.execute(text("SELECT * FROM xiaowei_action"))).mappings()
        }
    assert after[pending] == {**before[pending], "state": "rejected"}
    for action_id in kept.values():
        assert after[action_id] == before[action_id]
    assert await env.rows("SELECT failure_code FROM sdk_spike_action_cause") == [(UPGRADE_CODE,)]
    async with running(env, max_reply_chars=3500) as group:
        await group.ask(f"/批准 {pending}", sender=B, message_id="om_legacy_approval")
    assert actions.adapter.calls == []  # 当前真实入口也不会重写已 rejected 的旧候选。
    ((session_id, turn_id),) = await env.rows(
        "SELECT session_id, turn_id FROM xiaowei_request WHERE reply_message_id=:id",
        id="om_legacy_approval",
    )
    code, content = await fixed_receipt_projection(
        actions,
        pending,
        Identity(
            subject_id=B,
            owner=GROUP.owner,
            channel="feishu",
            session_id=session_id,
            turn_id=turn_id,
        ),
    )
    assert code == UPGRADE_CODE and content == "已因升级作废、未执行，请重新提出"
    assert "权限" not in content
    assert actions.adapter.calls == []
