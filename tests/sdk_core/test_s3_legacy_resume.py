"""S3 方案 1：旧格式历史与旧暂停材料经正式群入口恢复，最终按新格式输出。

正式群网关 → Application.resume_action（Action claim、Evidence、PolicySession、当前权限）→
隔离 PostgreSQL；模型与远端写为替身。旧格式由真实保存位置的数据构造：会话中的助手回答改为
S3 之前的 JSON，暂停请求的保存回答改为基线 897ee83 的旧 ``TurnAnswer`` 信封；RunState 不保存
输出类型（S2），暂停快照的输入历史因此同样带着旧回答。

旧数据按基线的结构与语义构造（见 ``_assert_baseline_legal``），不以当前 ``LegacyAnswer`` 能解析
为准：信封只有 ``answer``、``context_evidence``、``monitoring_failures``、``run_turn_id``；有证据
的回答只带引用这些证据的 ``inferences``，不混入 ``advice``（基线以 mixed_kinds 拒绝）。
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
from xiaowei.models import AnswerInference, LegacyAnswer, TurnAnswer

pytestmark = pytest.mark.loopback

LEGACY_TEXT = "旧格式回答：东区平稳"
# 基线 897ee83 ``TurnAnswer`` 序列化的全部字段（``session_state`` 不进入 JSON）。
BASELINE_ENVELOPE = {"answer", "context_evidence", "monitoring_failures", "run_turn_id"}


def _baseline(turn: TurnAnswer, legacy: LegacyAnswer) -> dict[str, object]:
    stored = turn.model_dump(mode="json", include=BASELINE_ENVELOPE - {"answer"})
    stored["answer"] = legacy.model_dump(mode="json")
    _assert_baseline_legal(stored)
    return stored


def _assert_baseline_legal(stored: dict[str, object]) -> None:
    """基线 ``TurnAnswer`` 字段与 ``citation_error`` 的规则（897ee83 evidence.py）逐条转写。"""
    assert set(stored) == BASELINE_ENVELOPE
    answer = stored["answer"]
    assert isinstance(answer, dict)
    cited = set(answer["evidence_ids"])
    unverified = [answer[k] for k in ("clarification", "advice") if answer[k] is not None]
    if unverified:  # 澄清或建议不与证据、分析、显示方式或彼此混用
        assert not (cited or answer["inferences"] or answer["fact_views"]) and len(unverified) == 1
        return
    assert cited and cited <= set(stored["context_evidence"])  # type: ignore[arg-type]
    assert {v["evidence_id"] for v in answer["fact_views"]} <= cited
    assert all(i["evidence_ids"] and set(i["evidence_ids"]) <= cited for i in answer["inferences"])


async def _legacy_history(actions: Actions, group) -> None:
    """先完成一轮，再把它在会话与请求表中的回答改成 S3 之前的格式。"""
    env = actions.env
    env.scripts.add(attributed(A, "先看东区"), tool_call("order_total", region="east"), cite())
    await group.ask("先看东区", message_id="om_legacy_seed")
    ((raw,),) = await env.rows(
        "SELECT answer FROM xiaowei_request WHERE reply_message_id='om_legacy_seed'"
    )
    turn = TurnAnswer.model_validate_json(raw)
    cited = turn.blocks_answer.evidence_ids
    legacy = LegacyAnswer(
        evidence_ids=cited,
        inferences=[AnswerInference(text=LEGACY_TEXT, evidence_ids=cited)],
        clarification=None,
        advice=None,
    )
    stored = _baseline(turn, legacy)
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
    # 暂停材料按基线保存：引用本次请求执行的动作材料与本轮读取结果，无分析。本轮没有读取
    # 查询，基线的材料引用因此正是当前材料列出的动作证据。
    ((raw,),) = await env.rows(
        "SELECT answer FROM xiaowei_request WHERE reply_message_id='om_sdk_origin'"
    )
    material = TurnAnswer.model_validate_json(raw)
    stored = _baseline(
        material,
        LegacyAnswer(
            evidence_ids=material.blocks_answer.evidence_ids, inferences=[], clarification=None
        ),
    )
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
        assert resumed.answer_version == 4
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
    ((state, saved),) = await env.rows(
        "SELECT state, answer FROM xiaowei_request WHERE reply_message_id='om_sdk_reject'"
    )
    assert state == "completed" and saved is not None
    assert TurnAnswer.model_validate_json(saved).answer_version == 4


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
