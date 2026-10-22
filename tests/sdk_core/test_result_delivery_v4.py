"""保存版本贯穿 RequestRecord→ResultDelivery→实际发送，外部发送只用替身。"""

import pytest
from sqlalchemy import text
from tests.sdk_core.test_app import answer, evidence_in, tool_call
from tests.sdk_core.test_channel_service import Env
from tests.sdk_core.test_channel_service import env as env
from tests.sdk_core.test_delivery_parts import Platform, parts, rejected, transmit
from tests.sdk_core.test_feishu_render import text_parts

from xiaowei.channel import RequestRef
from xiaowei.models import AgentAnswer, FactBlock, TurnAnswer

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize("partial", [False, True])
async def test_v3_unbound_saved_answer_resends_original_completion(env: Env, partial: bool) -> None:
    message = env.scripts.add(
        "已保存尚未绑定",
        tool_call("run_query", region="east"),
        tool_call("run_query", region="west"),
        lambda c: answer(evidence_in(c)[:1]),
    )
    record = await env.run(message, "old", channel="feishu", conversation="oc_d1a")
    async with env.engine.begin() as conn:
        raw = await conn.scalar(
            text("SELECT answer FROM xiaowei_request WHERE turn_id=:t"), {"t": record.turn_id}
        )
        turn = TurnAnswer.model_validate_json(raw)
        legacy = turn.model_copy(
            update={
                "answer_version": 3,
                "answer": AgentAnswer(
                    blocks=(
                        FactBlock(type="fact", evidence_id=turn.context_evidence[0], view="full"),
                    )
                    if partial
                    else (),
                    report_format=None,
                    awaiting_user=False,
                ),
            }
        )
        await conn.execute(
            text("UPDATE xiaowei_request SET answer=:a,delivery='failed' WHERE turn_id=:t"),
            {"a": legacy.model_dump_json(), "t": record.turn_id},
        )
    ref = RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_d1a", request_id="old"
    )
    view = await env.service.results.view(ref)
    assert view.delivery.answer_version == 3
    assert view.delivery.evidence_ids == turn.context_evidence
    platform = Platform()
    assert (
        await env.service.results.send(ref, transmit(env, ref, platform, resend=True), resend=True)
        == "sent"
    )
    shown = "\n".join(text_parts(platform.sent[0][0]))
    assert '"east"' in shown and '"west"' in shown
    assert len(env.adapter.calls) == 2 and env.model_calls(message) == 3


async def test_v4_bound_message_recovery_keeps_bytes_uuid_and_no_reexecution(env: Env) -> None:
    message = env.scripts.add(
        "v4 正常业务回答",
        tool_call("run_query", region="east"),
        lambda c: answer(evidence_in(c), "继续观察"),
    )
    await env.run(message, "new", channel="feishu", conversation="oc_d1a")
    ref = RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_d1a", request_id="new"
    )
    platform = Platform(outcomes=[rejected()])
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "failed"
    (before,) = await parts(env, ref)
    assert b"\xe6\x8a\x80\xe6\x9c\xaf\xe8\xaf\xa6\xe6\x83\x85" in before.body
    assert (
        await env.service.results.send(ref, transmit(env, ref, platform, resend=True), resend=True)
        == "sent"
    )
    (after,) = await parts(env, ref)
    assert (before.body, before.uuid) == (after.body, after.uuid)
    assert len(env.adapter.calls) == 1 and env.model_calls(message) == 2
    assert not platform.files
