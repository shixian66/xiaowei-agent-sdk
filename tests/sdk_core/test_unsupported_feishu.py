"""E5a: unsupported input gets bounded feedback before any business or stored turn."""

import asyncio
import dataclasses
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event as sql_event
from tests.sdk_core.test_channel_service import Env
from tests.sdk_core.test_feishu import Feishu, Outbox, config, running
from tests.sdk_core.test_feishu import env as env
from tests.sdk_core.test_runtime import until

from xiaowei.feishu import FeishuGateway, LarkTransport, lark_channel

pytestmark = pytest.mark.loopback
CANARY = "ATTACHMENT_CANARY https://private.invalid/do-not-fetch /批准 123"


def media_event(fs: Any, kind: str, **changes: Any) -> dict[str, Any]:
    content = (
        {"zh_cn": {"title": CANARY, "content": [[{"tag": "text", "text": CANARY}]]}}
        if kind == "post"
        else {"image_key": "img_canary", "file_key": "file_canary", "file_name": CANARY}
    )
    return fs.event(
        "ignored", content=json.dumps(content), event__message__message_type=kind, **changes
    )


@contextmanager
def pg_calls(engine: Any):
    calls = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        calls.append(statement)

    sql_event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield calls
    finally:
        sql_event.remove(engine.sync_engine, "before_cursor_execute", record)


@pytest.mark.parametrize("kind", ["image", "file", "post", "audio", "media", "sticker"])
async def test_private_unsupported_input_replies_once_without_stored_or_model_turn(
    env: Env, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    async with running(env) as fs, asyncio.timeout(5):
        value = media_event(fs, kind)
        with pg_calls(env.engine) as calls:
            await fs.gateway.receive(value)
            await fs.gateway.receive(value)
            await fs.drain()
    assert len(fs.outbox.sent) == 1
    chat, reply = fs.outbox.sent[0]
    assert chat == "oc_alice" and isinstance(reply, str)
    assert "暂不支持" in reply and "文字" in reply
    assert len(reply) <= 200 and CANARY not in reply and CANARY not in caplog.text
    assert fs.outbox.replies == [None]
    assert calls == [] and env.scripts.calls == {} and env.adapter.calls == []


@pytest.mark.parametrize("kind", ["image", "file", "post"])
async def test_unregistered_nontext_has_only_existing_identity_notice(env: Env, kind: str) -> None:
    async with running(env) as fs:
        with pg_calls(env.engine) as calls:
            await fs.gateway.receive(media_event(fs, kind, sender="ou_stranger"))
    assert len(fs.outbox.sent) == 1
    assert "还没有获得授权" in fs.outbox.texts()[0] and "ou_stranger" in fs.outbox.texts()[0]
    assert "暂不支持" not in fs.outbox.texts()[0]
    assert calls == [] and env.scripts.calls == {} and env.adapter.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"header__app_id": "cli_other"},
        {"header__tenant_key": "foreign"},
        {"event__sender__tenant_key": "foreign"},
        {"event__sender__sender_type": "bot"},
        {"age_seconds": 301},
        {"age_seconds": -120},
        {"event__message__message_id": ""},
        {"event__message__chat_type": "group"},
        {"sender": "ou_carol"},
    ],
)
async def test_ineligible_nontext_is_silent_without_business_io(env: Env, changes: dict) -> None:
    async with running(env) as fs:
        with pg_calls(env.engine) as calls:
            await fs.gateway.receive(media_event(fs, "image", **changes))
    assert fs.outbox.sent == [] and calls == []
    assert env.scripts.calls == {} and env.adapter.calls == []


@pytest.mark.parametrize("raw", [None, "not json", "[]", "x" * 2000])
async def test_nontext_malformed_or_over_container_limit_is_silent(env: Env, raw: Any) -> None:
    async with running(env) as fs:
        value = media_event(fs, "file")
        value["event"]["message"]["content"] = raw
        with pg_calls(env.engine) as calls:
            await fs.gateway.receive(value)
    assert fs.outbox.sent == [] and calls == [] and env.scripts.calls == {}
    assert env.adapter.calls == []


@pytest.mark.parametrize("outcome", ["sent", "failed", "unknown", RuntimeError(CANARY)])
async def test_unsupported_notice_reserves_before_send_and_keeps_the_existing_window(
    env: Env, outcome: Any, caplog: pytest.LogCaptureFixture
) -> None:
    outbox = Outbox(outcomes=[outcome])
    async with running(env, outbox) as fs:
        await asyncio.gather(
            fs.gateway.receive(media_event(fs, "image")),
            fs.gateway.receive(media_event(fs, "file")),
            fs.gateway.receive(media_event(fs, "post")),
        )
        assert len(outbox.sent) == 1
        env.clock.advance(600)
        await fs.gateway.receive(media_event(fs, "file"))
    assert len(outbox.sent) == 2 and CANARY not in caplog.text
    assert env.scripts.calls == {} and env.adapter.calls == []


@pytest.mark.parametrize("kind", ["image", "file", "post"])
async def test_native_sdk_raw_input_has_zero_downloads_and_one_bounded_text_send(
    env: Env, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from lark_channel.channel import FeishuChannel
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from tests.sdk_core.mcp_fixture import serve

    paths, sent = [], []

    async def endpoint(request: Request) -> JSONResponse:
        paths.append(request.url.path)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return JSONResponse({"code": 0, "tenant_access_token": "synthetic", "expire": 7200})
        if request.url.path == "/open-apis/bot/v3/info":
            return JSONResponse({"code": 0, "bot": {"open_id": "ou_bot"}})
        if request.url.path == "/open-apis/im/v1/messages":
            sent.append(await request.json())
        return JSONResponse({"code": 0, "data": {"message_id": "om_notice"}})

    def build(_):
        return Starlette(routes=[Route("/{path:path}", endpoint, methods=["GET", "POST"])])

    monkeypatch.setenv("XW_TEST_FEISHU_SECRET", "not-a-real-secret")
    env.clock.now = datetime.now(UTC)  # 应用与真实 SDK 的时效校验都使用当前有效事件。
    with serve(build, path="") as server:
        cfg = dataclasses.replace(lark_channel(config()).config, domain=server.url)
        cfg.transport.kind = "webhook"
        channel = FeishuChannel(config=cfg, name_lookup=lambda _: {})
        transport = LarkTransport(channel, config())
        gateway = FeishuGateway(env.service, config(), transport.send, clock=env.clock)
        fs = Feishu(env, gateway, Outbox())
        task = asyncio.create_task(gateway.run())
        await transport.start(gateway.receive)
        try:
            value = media_event(fs, kind)
            with pg_calls(env.engine) as calls:
                for _ in range(2):
                    status, _ = await channel.handle_webhook_request({}, json.dumps(value).encode())
                    assert status == 200
                await until(lambda: bool(sent))
                await until(lambda: transport.in_flight == 0)
                await gateway.idle()
        finally:
            assert await transport.stop()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert len(sent) == 1 and sent[0]["msg_type"] == "text"
    reply = json.loads(sent[0]["content"])["text"]
    assert "暂不支持" in reply and "文字" in reply and CANARY not in reply
    assert len(reply) <= config().max_reply_chars
    assert set(paths) <= {
        "/open-apis/auth/v3/tenant_access_token/internal",
        "/open-apis/bot/v3/info",
        "/open-apis/im/v1/messages",
    }
    assert calls == [] and env.scripts.calls == {} and env.adapter.calls == []
