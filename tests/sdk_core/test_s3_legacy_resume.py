"""S3 方案 1：旧格式历史与旧暂停材料经正式群入口恢复，最终按新格式输出。

正式群网关 → Application.resume_action（Action claim、Evidence、PolicySession、当前权限）→
隔离 PostgreSQL；模型与远端写为替身。旧格式由真实保存位置的数据构造：会话中的助手回答改为
S3 之前的 JSON，暂停请求的保存回答改为没有版本字段的旧 ``TurnAnswer``；RunState 不保存输出
类型（S2），暂停快照的输入历史因此同样带着旧回答。
"""

import asyncio
import json

import pytest
from sqlalchemy import text
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_group_actions import SOURCE, Actions, request_execution
from tests.sdk_core.test_group_actions import actions as actions
from tests.sdk_core.test_group_gateway import running
from tests.sdk_core.test_group_identity import A, B, group_ref

from xiaowei.feishu import attributed
from xiaowei.models import LegacyAnswer, TurnAnswer

pytestmark = pytest.mark.loopback

LEGACY_TEXT = "旧格式回答：东区平稳"


async def _legacy_history(actions: Actions, group) -> None:
    """先完成一轮，再把它在会话与请求表中的回答改成 S3 之前的格式。"""
    env = actions.env
    env.scripts.add(attributed(A, "先看东区"), tool_call("order_total", region="east"), cite())
    await group.ask("先看东区", message_id="om_legacy_seed")
    ((raw,),) = await env.rows(
        "SELECT answer FROM xiaowei_request WHERE reply_message_id='om_legacy_seed'"
    )
    turn = TurnAnswer.model_validate_json(raw)
    legacy = LegacyAnswer(
        evidence_ids=turn.answer.evidence_ids,
        inferences=[],
        clarification=None,
        advice=LEGACY_TEXT,
    )
    stored = turn.model_dump(mode="json", exclude={"answer_version", "answer"})
    stored["answer"] = legacy.model_dump(mode="json")
    async with env.engine.begin() as conn:
        rows = (
            await conn.execute(text("SELECT id, message_data FROM agent_messages ORDER BY id"))
        ).all()
        last = next(r for r in reversed(rows) if json.loads(r[1]).get("role") == "assistant")
        item = json.loads(last[1])
        item["content"] = legacy.model_dump_json()
        await conn.execute(
            text("UPDATE agent_messages SET message_data=:d WHERE id=:i"),
            {"d": json.dumps(item, ensure_ascii=False), "i": last[0]},
        )
        await conn.execute(
            text("UPDATE xiaowei_request SET answer=:a WHERE reply_message_id='om_legacy_seed'"),
            {"a": json.dumps(stored, ensure_ascii=False)},
        )


async def _legacy_pause(actions: Actions, group) -> tuple[str, object]:
    env = actions.env
    script = env.scripts.add(
        attributed(A, "SDK 静默请求"),
        tool_call(f"{SOURCE}__propose_change", value="after"),
        request_execution,
        cite("根据实际执行结果反馈"),
    )
    await group.ask("SDK 静默请求", message_id="om_sdk_origin")
    assert await env.rows(
        "SELECT state FROM xiaowei_request WHERE reply_message_id='om_sdk_origin'"
    ) == [("paused",)]
    # 暂停快照的输入历史带着旧格式回答（不能以“暂停状态不含旧回答”为前提）。
    ((snapshot,),) = await env.rows("SELECT run_state FROM xiaowei_action_run")
    replayed = [
        item.get("content")
        for item in json.loads(snapshot)["original_input"]
        if isinstance(item, dict) and item.get("role") == "assistant"
    ]
    assert any(LEGACY_TEXT in json.dumps(c, ensure_ascii=False) for c in replayed)
    # 暂停材料按旧格式保存：没有版本字段、旧回答结构。
    ((raw,),) = await env.rows(
        "SELECT answer FROM xiaowei_request WHERE reply_message_id='om_sdk_origin'"
    )
    material = TurnAnswer.model_validate_json(raw)
    stored = material.model_dump(mode="json", exclude={"answer_version", "answer"})
    stored["answer"] = LegacyAnswer(
        evidence_ids=material.answer.evidence_ids, inferences=[], clarification=None
    ).model_dump(mode="json")
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE xiaowei_request SET answer=:a WHERE reply_message_id='om_sdk_origin'"),
            {"a": json.dumps(stored, ensure_ascii=False)},
        )
    return await actions.one(), script


async def test_legacy_history_and_material_resume_on_approval_into_new_format(actions):
    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        await _legacy_history(actions, group)
        action_id, script = await _legacy_pause(actions, group)
        # 旧暂停材料的历史读取仍按当前权限交付（动作材料置顶、完整保护）。
        material = await env.results.view(group_ref("om_sdk_origin", A), first=True)
        assert material.delivery is not None
        assert [f.section for f in material.delivery.facts][:1] == ["action"]
        assert material.delivery.facts[0].requires_complete
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_sdk_approve")
        assert await env.rows(
            "SELECT state, delivery FROM xiaowei_request WHERE reply_message_id='om_sdk_approve'"
        ) == [("completed", "sent")]
        ((saved,),) = await env.rows(
            "SELECT answer FROM xiaowei_request WHERE reply_message_id='om_sdk_approve'"
        )
        resumed = TurnAnswer.model_validate_json(saved)
        assert resumed.answer_version == 2
        ((origin_turn,),) = await env.rows(
            "SELECT turn_id FROM xiaowei_request WHERE reply_message_id='om_sdk_origin'"
        )
        assert resumed.run_turn_id == origin_turn  # 原执行轮次，不是审批请求的新编号
        view = await env.results.view(group_ref("om_sdk_approve", B), first=True)
        assert view.delivery is not None
        outcome = view.delivery.facts[0]
        assert outcome.section == "action" and outcome.requires_complete
        assert outcome.metadata["state"] == "succeeded"
        # 旧格式历史请求在新代码下的历史读取。
        seed = await env.results.view(group_ref("om_legacy_seed", A), first=True)
        assert seed.delivery is not None and LEGACY_TEXT in seed.delivery.texts
        before = len(actions.adapter.calls)
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_sdk_duplicate")
        assert len(actions.adapter.calls) == before == 1  # 已执行不重放
    assert env.model_calls(script) == 3
    assert await env.rows("SELECT state, approver_id FROM xiaowei_action") == [("succeeded", B)]


async def test_legacy_pause_rejection_writes_nothing_and_answers_in_new_format(actions):
    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        await _legacy_history(actions, group)
        action_id, _ = await _legacy_pause(actions, group)
        await group.ask(f"/拒绝 {action_id}", sender=B, message_id="om_sdk_reject")
    assert actions.adapter.calls == []
    ((state,),) = await env.rows("SELECT state FROM xiaowei_action")
    assert state == "rejected"
    rows = await env.rows(
        "SELECT answer FROM xiaowei_request WHERE reply_message_id='om_sdk_reject'"
    )
    if rows and rows[0][0] is not None:
        assert TurnAnswer.model_validate_json(rows[0][0]).answer_version == 2


async def test_concurrent_legacy_approvals_write_at_most_once(actions):
    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        await _legacy_history(actions, group)
        action_id, _ = await _legacy_pause(actions, group)
        await asyncio.gather(
            group.ask(f"/批准 {action_id}", sender=B, message_id="om_sdk_a"),
            group.ask(f"/批准 {action_id}", sender=B, message_id="om_sdk_b"),
        )
    assert len(actions.adapter.calls) == 1
    assert await env.rows("SELECT state FROM xiaowei_action") == [("succeeded",)]


async def test_requester_left_before_legacy_resume_stops_before_model_and_write(actions):
    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        await _legacy_history(actions, group)
        action_id, script = await _legacy_pause(actions, group)
        before = env.model_calls(script)
        env.members.current.remove(A)
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_sdk_left")
    assert env.model_calls(script) == before
    assert actions.adapter.calls == []
    assert await env.rows("SELECT state, failure_code FROM xiaowei_action") == [
        ("rejected", "action_requester_invalid")
    ]
