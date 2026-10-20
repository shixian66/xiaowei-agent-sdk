"""S3 独立审查修复的回归：旧回答不受新模型输出预算约束；v0 截断卡片保留模型来源标签。

旧记录按基线 897ee83 的保存结构构造：``TurnAnswer`` 信封只有 ``answer``、
``context_evidence``、``monitoring_failures`` 与 ``run_turn_id``；回答是当时合法的
“引用证据 + 关联分析”（分析必须引用本回答的证据）或“无证据建议”，不混用。
"""

import json
from typing import Any

import pytest
from lark_channel.channel.types import SendResult
from pydantic import ValidationError
from sqlalchemy import text
from tests.sdk_core.synthetic_tools import TARGET, TOTAL_TOOL
from tests.sdk_core.test_app import answer, evidence_in, tool_call
from tests.sdk_core.test_channel_service import Env
from tests.sdk_core.test_channel_service import env as env
from tests.sdk_core.test_delivery_parts import Platform, parts, rejected, transmit

from xiaowei.channel import ChannelService, RequestRef, ResultUnavailableError
from xiaowei.channel_store import ChannelStoreError, RequestUnavailableError
from xiaowei.feishu_render import MODEL_TEXT_LABEL, TEXT_CLIPPED, build_feishu_message
from xiaowei.models import (
    AgentAnswer,
    AnswerInference,
    DeliveryText,
    FactBlock,
    LegacyAnswer,
    TextBlock,
    TurnAnswer,
)

pytestmark = pytest.mark.loopback

CHAT = "oc_d1a"
# 存储上限明确容纳原记录：失败只能来自回答适配，不能来自请求表容量。
STORE_BYTES = 64_000

SHAPES = {
    # 一条事实 + 三段各 2,000 汉字的关联分析：模型文字累计 18,000 字节。
    "18000_bytes": ["甲" * 2000, "乙" * 2000, "丙" * 2000],
    # 一条事实 + 64 段短分析：按块计共 65 块。
    "65_blocks": [f"第{i}段关联分析" for i in range(64)],
}


def _ref(request_id: str, *, channel: str = "feishu", subject: str = "alice") -> RequestRef:
    return RequestRef(
        channel=channel,  # type: ignore[arg-type]
        subject_id=subject,
        conversation_id=CHAT,
        request_id=request_id,
    )


def _roomy(env: Env) -> None:
    env.store = env.channel_store(max_answer_bytes=STORE_BYTES)
    env.service = ChannelService(env.app, env.results(env.store))


async def _feishu_request(env: Env, request_id: str, *, format_: str | None) -> str:
    message = env.scripts.add(
        f"查询东区订单 {request_id}",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format=format_),
    )
    record = await env.run(message, request_id, channel="feishu", conversation=CHAT)
    return record.turn_id


async def _store_legacy(
    env: Env, turn_id: str, build: Any, *, delivery_version: int | None = None
) -> LegacyAnswer:
    """把已保存回答改成基线结构的旧记录；``build(context)`` 生成旧回答。"""
    async with env.engine.begin() as conn:
        raw = await conn.scalar(
            text("SELECT answer FROM xiaowei_request WHERE turn_id=:t"), {"t": turn_id}
        )
        turn = TurnAnswer.model_validate_json(raw)
        legacy: LegacyAnswer = build(turn.context_evidence)
        stored = {
            "answer": legacy.model_dump(mode="json"),
            "context_evidence": list(turn.context_evidence),
            "monitoring_failures": [],
            "run_turn_id": turn.run_turn_id,
        }
        content = json.dumps(stored, ensure_ascii=False)
        assert len(content.encode()) <= STORE_BYTES
        await conn.execute(
            text(
                "UPDATE xiaowei_request SET answer=:a,"
                " delivery_version=COALESCE(:v, delivery_version) WHERE turn_id=:t"
            ),
            {"a": content, "v": delivery_version, "t": turn_id},
        )
    return legacy


def _analysis(texts: list[str], report_format: str | None = None) -> Any:
    def build(context: tuple[str, ...]) -> LegacyAnswer:
        (evidence_id,) = context
        return LegacyAnswer(
            evidence_ids=(evidence_id,),
            inferences=[AnswerInference(text=t, evidence_ids=(evidence_id,)) for t in texts],
            clarification=None,
            advice=None,
            report_format=report_format,
        )

    return build


def _strings(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---- 修复一：旧回答不被新模型输出预算拒绝 ---------------------------------------------------


@pytest.mark.parametrize("texts", SHAPES.values(), ids=SHAPES.keys())
def test_new_output_budget_still_rejects_the_same_content(texts: list[str]) -> None:
    """新模型输出的 16,000 字节 / 64 块上限不变：同样内容作为新回答仍被拒绝（可改写）。"""
    blocks = (
        FactBlock(type="fact", evidence_id="e1", view="full"),
        *(TextBlock(type="text", text=t) for t in texts),
    )
    with pytest.raises(ValidationError):
        AgentAnswer(blocks=blocks, report_format=None, awaiting_user=False)


@pytest.mark.parametrize("texts", SHAPES.values(), ids=SHAPES.keys())
async def test_legal_legacy_answer_over_new_budget_is_read_and_sent_in_full(
    env: Env, texts: list[str]
) -> None:
    _roomy(env)
    turn_id = await _feishu_request(env, "om_legacy_big", format_=None)
    legacy = await _store_legacy(env, turn_id, _analysis(texts))
    ref = _ref("om_legacy_big")
    view = await env.service.results.view(ref)
    assert view.delivery is not None
    assert view.delivery.texts == tuple(texts)  # 不截断、不丢分析
    assert [f.evidence_id for f in view.delivery.facts] == list(legacy.evidence_ids)
    assert all(f.section == "body" for f in view.delivery.facts)  # 旧格式不补全
    platform = Platform()
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "sent"
    shown = "".join(_strings(v) for v, _ in platform.sent) + "".join(
        f.decode() for f in platform.files
    )
    assert all(t in shown for t in texts)


async def test_legacy_resend_reuses_saved_part_and_report_bytes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已有 v1 分项与已绑定报告：旧回答超出新预算时重发仍复用原字节与 uuid，不重渲染、不重查。"""
    _roomy(env)
    turn_id = await _feishu_request(env, "om_d1a", format_="md")
    ref = _ref("om_d1a")
    platform = Platform(
        outcomes=[
            SendResult.ok(message_id="om_body_file"),
            SendResult.ok(message_id="om_detail_file"),
            rejected(),
        ]
    )
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "failed"
    before = await parts(env, ref)
    uploaded = tuple(platform.files)
    assert len(uploaded) == 2
    await _store_legacy(env, turn_id, _analysis(SHAPES["18000_bytes"], "md"))
    calls = len(env.adapter.calls)

    def changed(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("重发不能按新模板生成字节")

    monkeypatch.setattr("xiaowei.feishu_delivery.build_reports", changed)
    assert (
        await env.service.results.send(ref, transmit(env, ref, platform, resend=True), resend=True)
        == "sent"
    )
    after = await parts(env, ref)
    assert [p.body for p in after] == [p.body for p in before]
    assert [p.uuid for p in after] == [p.uuid for p in before]
    assert tuple(platform.files) == uploaded and len(env.adapter.calls) == calls


async def test_big_legacy_answer_is_still_refused_without_current_permission(env: Env) -> None:
    _roomy(env)
    turn_id = await _feishu_request(env, "om_legacy_scope", format_=None)
    await _store_legacy(env, turn_id, _analysis(SHAPES["65_blocks"]))
    ref = _ref("om_legacy_scope")
    assert (await env.service.results.view(ref)).delivery is not None
    for other in (_ref("om_legacy_scope", subject="bob"), _ref("om_legacy_scope", channel="web")):
        with pytest.raises(ChannelStoreError):
            await env.service.results.view(other)  # 跨会话 / 跨渠道
    env.grants.allowed.discard(("alice", TARGET, TOTAL_TOOL))
    with pytest.raises(ResultUnavailableError):
        await env.service.results.view(ref)  # 撤权
    env.grants.grant("alice", TOTAL_TOOL)
    async with env.engine.begin() as conn:
        await conn.execute(text("DELETE FROM xiaowei_evidence"))
    with pytest.raises(ResultUnavailableError):
        await env.service.results.view(ref)  # 证据不可用
    assert env.adapter.calls[1:] == []  # 只有原轮次的一次查询


async def test_malformed_legacy_record_is_unavailable_not_an_unhandled_error(env: Env) -> None:
    """旧格式结构检查保留：旧版不合法的记录按不可用处理，不抛出未处理的校验异常。"""
    _roomy(env)
    turn_id = await _feishu_request(env, "om_legacy_bad", format_=None)
    async with env.engine.begin() as conn:
        raw = await conn.scalar(
            text("SELECT answer FROM xiaowei_request WHERE turn_id=:t"), {"t": turn_id}
        )
        stored = json.loads(raw)
        stored.pop("answer_version")
        stored.pop("limit_reached", None)
        stored.pop("material_only", None)
        stored["answer"] = {"evidence_ids": [], "inferences": [{"text": "", "evidence_ids": []}]}
        await conn.execute(
            text("UPDATE xiaowei_request SET answer=:a WHERE turn_id=:t"),
            {"a": json.dumps(stored), "t": turn_id},
        )
    with pytest.raises(RequestUnavailableError):
        await env.service.results.view(_ref("om_legacy_bad"))


# ---- 修复二：v0 截断卡片保留模型来源标签 -----------------------------------------------------


def _advice(content: str) -> Any:
    def build(context: tuple[str, ...]) -> LegacyAnswer:
        return LegacyAnswer(evidence_ids=(), inferences=[], clarification=None, advice=content)

    return build


def _card_texts(message: dict[str, Any]) -> list[str]:
    return [e["text"]["content"] for e in message["card"]["body"]["elements"] if "text" in e]


async def _v0_advice(env: Env, request_id: str, content: str) -> RequestRef:
    _roomy(env)
    message = env.scripts.add(f"只给建议 {request_id}", lambda call: answer([], content))
    record = await env.run(message, request_id, channel="feishu", conversation=CHAT)
    await _store_legacy(env, record.turn_id, _advice(content), delivery_version=0)
    return _ref(request_id)


async def test_v0_clipped_prose_card_keeps_model_label_before_text(env: Env) -> None:
    advice = "建议" * 1500  # 旧版建议上限 4,000 字，超过单消息 2,000 字
    ref = await _v0_advice(env, "om_v0_clip", advice)
    platform = Platform()
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "sent"
    ((message, _),) = platform.sent
    assert isinstance(message, dict) and "card" in message
    texts = _card_texts(message)
    # 标签、模型文字、截断说明是三个独立组件，顺序固定。
    assert texts[0] == MODEL_TEXT_LABEL and texts[-1] == TEXT_CLIPPED
    body = texts[1:-1]
    assert body and all(MODEL_TEXT_LABEL not in t and TEXT_CLIPPED not in t for t in body)
    assert "".join(body) and advice.startswith("".join(body).replace("\n", ""))


async def test_v0_label_counts_toward_capacity_and_is_not_duplicated(env: Env) -> None:
    advice = "建议" * 1500
    ref = await _v0_advice(env, "om_v0_cap", advice)
    delivery = (await env.service.results.view(ref)).delivery
    assert delivery is not None and delivery.delivery_version == 0
    for limit in (300, 800, 2000):
        message = build_feishu_message(delivery, limit, CHAT)
        texts = _card_texts(message)
        assert texts.count(MODEL_TEXT_LABEL) == 1 and texts[-1] == TEXT_CLIPPED
        assert sum(len(t) for t in texts) <= limit  # 标签计入实际容量
    noticed = delivery.model_copy(update={"report_notice": "当前渠道不支持文件，结果以消息展示。"})
    texts = _card_texts(build_feishu_message(noticed, 2000, CHAT))
    assert texts.count(MODEL_TEXT_LABEL) == 1  # 已有系统说明时不重复加标签
    short = delivery.model_copy(update={"blocks": (DeliveryText(text="建议先看东区"),)})
    assert "post" in build_feishu_message(short, 2000, CHAT)  # 未截断纯模型文字仍走 post
