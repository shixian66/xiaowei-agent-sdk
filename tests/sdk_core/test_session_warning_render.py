"""S3: same-message plain system notices survive card formatting and both budgets."""

import dataclasses
import json

import pytest
from tests.sdk_core.test_feishu_render import delivery, fact, size, text_parts

from xiaowei.feishu import send_delivery
from xiaowei.feishu_render import CAPACITY_NOTICE, build_feishu_message
from xiaowei.models import Delivery

NOTICE = "本轮结束时会话已用 80/100 次；历史容量接近上限。发送 /新建；关键线索请自行保存。"


@pytest.mark.parametrize("kind", ["facts", "advice", "clarification", "receipt"])
def test_notice_visible_once_with_existing_business_container(kind: str) -> None:
    if kind == "facts":
        value = delivery(fact(), analysis="结论第一行\n**第二行**").model_copy(
            update={"session_notice": NOTICE}
        )
    else:
        value = Delivery(
            channel="feishu",
            evidence_ids=(),
            content="需要澄清" if kind == "clarification" else "原有说明",
            feishu_text=None if kind == "receipt" else "第一行\n**第二行**",
            session_notice=NOTICE,
        )
    message = build_feishu_message(value, 4000, "oc_test")
    visible = "\n".join(text_parts(message, folded=False))
    assert visible.count(NOTICE) == 1
    if kind == "receipt":
        assert "text" in message and "原有说明" in message["text"]
    elif "post" in message:
        assert "第一行" in visible and "第二行" in visible
        assert [{"tag": "text", "text": NOTICE}] in message["post"]["zh_cn"]["content"]
    else:
        assert "card" in message
        assert "第一行" in visible and "第二行" in visible
        assert any(
            e.get("tag") == "div"
            and e.get("text", {}).get("content") == NOTICE
            and e["text"]["tag"] == "plain_text"
            for e in message["card"]["body"]["elements"]
        )


@pytest.mark.parametrize("chars,byte_limit", [(180, 30_000), (4000, 520)])
def test_capacity_fallback_preserves_warning_and_result_omission(
    chars: int, byte_limit: int
) -> None:
    value = delivery(fact(note="必要来源说明" * 500)).model_copy(update={"session_notice": NOTICE})
    message = build_feishu_message(value, chars, "oc_test", max_bytes=byte_limit)
    assert "text" in message
    assert CAPACITY_NOTICE in message["text"] and NOTICE in message["text"]
    assert len(message["text"]) <= chars and size(message) <= byte_limit
    assert '"orders"' not in message["text"]


async def test_capacity_warning_remains_one_send_and_preserves_action_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent = []

    async def send(chat, message, *, reply_to=None):
        sent.append(message)
        return "sent"

    value = delivery(fact(note="必须完整展示" * 500)).model_copy(
        update={"session_notice": NOTICE, "requires_complete": True}
    )
    outcome = await send_delivery(value, send=send, chat_id="oc_test", max_chars=180)
    assert outcome == "failed"
    assert len(sent) == 1 and NOTICE in sent[0]["text"]
    assert "reason=reply_capacity_notice" in caplog.text


async def test_warning_render_failure_has_no_send_and_post_send_exception_is_unknown() -> None:
    sent = []

    async def send(chat, message, *, reply_to=None):
        sent.append(message)
        raise TimeoutError("private-host-content-sentinel")

    value = delivery(fact()).model_copy(update={"session_notice": NOTICE})
    assert await send_delivery(value, send=send, chat_id="oc_test", max_chars=20) == "failed"
    assert not sent
    assert await send_delivery(value, send=send, chat_id="oc_test", max_chars=4000) == "unknown"
    assert len(sent) == 1


@pytest.mark.loopback
@pytest.mark.parametrize("reply_to", [None, "om_warning_original"])
@pytest.mark.parametrize("fallback", [False, True])
async def test_native_sdk_warning_wire_body_is_single_and_matches_budget(
    monkeypatch: pytest.MonkeyPatch, reply_to: str | None, fallback: bool
) -> None:
    from lark_channel.channel import FeishuChannel
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from tests.sdk_core.mcp_fixture import serve
    from tests.sdk_core.test_feishu import config

    from xiaowei.feishu import LarkTransport, lark_channel
    from xiaowei.feishu_render import request_bytes

    received = []

    async def token(request: Request) -> JSONResponse:
        return JSONResponse({"code": 0, "tenant_access_token": "t-s3-synthetic", "expire": 7200})

    async def posted(request: Request) -> JSONResponse:
        received.append(await request.body())
        return JSONResponse({"code": 0, "data": {"message_id": "om_s3_sent"}})

    def build(_):
        return Starlette(
            routes=[
                Route("/open-apis/auth/v3/tenant_access_token/internal", token, methods=["POST"]),
                Route("/open-apis/im/v1/messages", posted, methods=["POST"]),
                Route("/open-apis/im/v1/messages/{message_id}/reply", posted, methods=["POST"]),
            ]
        )

    monkeypatch.setenv("XW_TEST_FEISHU_SECRET", "not-a-real-secret")
    value = delivery(fact(note="必要说明" * 500) if fallback else fact()).model_copy(
        update={"session_notice": NOTICE}
    )
    max_chars = 180 if fallback else 4000
    expected = build_feishu_message(value, max_chars, "oc_alice", reply_to=reply_to)
    with serve(build, path="") as running:
        product = lark_channel(config(domain="lark")).config
        channel = FeishuChannel(config=dataclasses.replace(product, domain=running.url))
        try:
            outcome = await send_delivery(
                value,
                send=LarkTransport(channel, config()).send,
                chat_id="oc_alice",
                reply_to=reply_to,
                max_chars=max_chars,
            )
        finally:
            channel.stop(join_timeout=5)
    assert outcome == "sent" and len(received) == 1
    raw = received[0]
    body = json.loads(raw)
    content = json.loads(body["content"])
    message = content if fallback else {"card": content}
    assert NOTICE in "\n".join(text_parts(message, folded=False))
    assert body["msg_type"] == ("text" if fallback else "interactive")
    assert len(raw) == request_bytes(expected, "oc_alice", reply_to=reply_to)
    assert len(raw) <= 30_000


def test_full_warning_post_can_fit_even_if_fallback_text_is_larger() -> None:
    value = Delivery(
        channel="feishu", evidence_ids=(), content="建议", feishu_text="OK", session_notice=NOTICE
    )
    message = build_feishu_message(value, 80, "oc_test")
    assert "post" in message
    assert sum(len(part) for part in text_parts(message)) <= 80
    assert NOTICE in "\n".join(text_parts(message, folded=False))
    assert size(message) <= 30_000
