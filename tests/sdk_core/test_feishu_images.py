"""E5b 截图正式入口：受信事件、授权、原图保存、SDK 与 Vertex 回放。"""

import asyncio
import base64
import json
from io import BytesIO
from typing import Any

import httpx2
import pytest
from PIL import Image
from sqlalchemy import text
from tests.sdk_core.synthetic_tools import QUERY_TOOL, TARGET, TOTAL_TOOL, s3_answer
from tests.sdk_core.test_channel_service import Env, app_config
from tests.sdk_core.test_channel_service import env as env
from tests.sdk_core.test_feishu import Feishu, Outbox, config

from xiaowei.app import Application
from xiaowei.channel import ChannelService
from xiaowei.channel_store import ChannelStoreUnavailableError
from xiaowei.feishu import FeishuGateway
from xiaowei.governance import GovernedTools
from xiaowei.image_input import SessionImageStore, ValidatedImage
from xiaowei.model_api import ModelProfile, open_model

pytestmark = pytest.mark.loopback


def _png() -> bytes:
    out = BytesIO()
    Image.new("RGB", (8, 8), "red").save(out, format="PNG")
    return out.getvalue()


def _event(fs: Feishu) -> dict[str, Any]:
    return fs.event(
        "ignored",
        message_id="om_image",
        content=json.dumps({"image_key": "img_one"}),
        event__message__message_type="image",
    )


async def test_authorized_image_and_followup_replay_original_through_runner_vertex(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _png()
    encoded = base64.b64encode(original).decode("ascii")
    calls: list[dict[str, Any]] = []
    downloads: list[tuple[str, str, str, int]] = []

    def endpoint(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        calls.append(body)
        return httpx2.Response(
            200,
            json={
                "candidates": [
                    {
                        "index": 0,
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "text": json.dumps(
                                        s3_answer(
                                            text="请说明希望分析截图中的哪一处。",
                                            awaiting_user=True,
                                        ),
                                        ensure_ascii=False,
                                    )
                                }
                            ],
                        },
                    }
                ]
            },
        )

    async def download(mid: str, key: str, kind: str, *, max_bytes: int) -> bytes:
        downloads.append((mid, key, kind, max_bytes))
        return original

    monkeypatch.setenv("XW_TEST_IMAGE_MODEL_KEY", "synthetic")
    profile = ModelProfile(
        profile_id="vertex-image-test",
        provider="vertex",
        model="gemini-3-flash-preview",
        api_key_ref="env:XW_TEST_IMAGE_MODEL_KEY",
        request_timeout_seconds=5,
        max_output_tokens=256,
        max_request_bytes=1_000_000,
        max_response_bytes=10_000,
        image_input_enabled=True,
        data_policy_id=env.binding.profile.data_policy_id,
        reasoning_effort=None,
    )
    images = SessionImageStore(env.engine, clock=env.clock)
    async with open_model(profile, transport=httpx2.MockTransport(endpoint)) as model:
        app = Application(
            app_config(),
            model=model,
            engine=env.engine,
            governance=GovernedTools(env.evidence),
            local_tools={
                (TOTAL_TOOL, TARGET): env.adapter.execute,
                (QUERY_TOOL, TARGET): env.adapter.execute,
            },
            clock=env.clock,
            images=images,
        )
        service = ChannelService(app, env.results())
        outbox = Outbox()
        gateway = FeishuGateway(
            service,
            config(image_input_enabled=True),
            outbox,
            clock=env.clock,
            download_resource=download,
        )
        fs = Feishu(env, gateway, outbox)
        task = asyncio.create_task(gateway.run())
        try:
            await gateway.receive(_event(fs))
            await gateway.idle()
            await gateway.receive(fs.event("截图右侧是什么错误？"))
            await gateway.idle()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert downloads == [("om_image", "img_one", "image", 2 * 1024 * 1024)]
    assert len(calls) == 2 and len(outbox.sent) == 2
    for call in calls:
        images_sent = [
            part["inlineData"]
            for content in call["contents"]
            for part in content["parts"]
            if "inlineData" in part
        ]
        assert images_sent == [{"mimeType": "image/png", "data": encoded}]
    assert env.adapter.calls == []
    assert await env.requests() == 2
    async with env.engine.connect() as conn:
        history = [
            str(value)
            for value in (
                await conn.execute(text("SELECT message_data FROM agent_messages"))
            ).scalars()
        ]
        stored = (await conn.execute(text("SELECT original FROM xiaowei_image"))).scalar_one()
    assert bytes(stored) == original
    assert all(encoded not in item and "data:image/" not in item for item in history)
    assert all("已获准原图" not in item for item in history)


async def test_unauthorized_image_does_not_download_or_create_request(env: Env) -> None:
    env.access.broken = True
    downloads: list[object] = []

    async def download(*args: Any, **kwargs: Any) -> bytes:
        downloads.append((args, kwargs))
        return _png()

    gateway = FeishuGateway(
        env.service,
        config(image_input_enabled=True),
        Outbox(),
        clock=env.clock,
        download_resource=download,
    )
    fs = Feishu(env, gateway, Outbox())
    await gateway.receive(_event(fs))
    assert downloads == [] and await env.requests() == 0 and env.adapter.calls == []


async def test_invalid_image_is_rejected_before_request_or_model(env: Env) -> None:
    env.app.images = SessionImageStore(env.engine, clock=env.clock)
    downloads: list[str] = []

    async def download(mid: str, key: str, kind: str, *, max_bytes: int) -> bytes:
        downloads.append(mid)
        return b"\x89PNG\r\n\x1a\n"  # Header only, not a complete screenshot.

    outbox = Outbox()
    gateway = FeishuGateway(
        env.service,
        config(image_input_enabled=True),
        outbox,
        clock=env.clock,
        download_resource=download,
    )
    fs = Feishu(env, gateway, outbox)
    await gateway.receive(_event(fs))
    await gateway.receive(_event(fs))
    assert downloads == ["om_image"]
    assert await env.requests() == 0 and env.adapter.calls == []
    assert len(outbox.sent) == 1 and "无法完整解码" in outbox.texts()[0]


async def test_full_session_rejects_thirteenth_image_before_download(env: Env) -> None:
    images = SessionImageStore(env.engine, clock=env.clock)
    env.app.images = images
    session = await env.store.current_session("feishu", "alice", "oc_alice")
    for n in range(12):
        receipt = await env.service.accept(
            env.inbound(f"image {n}", f"prior-{n}", channel="feishu", conversation="oc_alice")
        )
        await images.save(
            session,
            f"prior-source-{n}",
            ValidatedImage(b"image", "image/png", 1, 1),
            turn_id=receipt.record.turn_id,
        )
    downloads: list[str] = []

    async def download(mid: str, key: str, kind: str, *, max_bytes: int) -> bytes:
        downloads.append(mid)
        return _png()

    outbox = Outbox()
    gateway = FeishuGateway(
        env.service,
        config(image_input_enabled=True),
        outbox,
        clock=env.clock,
        download_resource=download,
    )
    await gateway.receive(_event(Feishu(env, gateway, outbox)))
    assert downloads == [] and await env.requests() == 12
    assert len(outbox.sent) == 1 and "数量或单张大小上限" in outbox.texts()[0]


async def test_image_binding_failure_marks_accepted_request_failed_and_notifies(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.app.images = SessionImageStore(env.engine, clock=env.clock)
    current = env.store.current_session
    calls = 0

    async def fail_second_current(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ChannelStoreUnavailableError
        return await current(*args, **kwargs)

    async def download(mid: str, key: str, kind: str, *, max_bytes: int) -> bytes:
        return _png()

    monkeypatch.setattr(env.store, "current_session", fail_second_current)
    outbox = Outbox()
    gateway = FeishuGateway(
        env.service,
        config(image_input_enabled=True),
        outbox,
        clock=env.clock,
        download_resource=download,
    )
    await gateway.receive(_event(Feishu(env, gateway, outbox)))
    assert await env.requests() == 1 and env.adapter.calls == []
    async with env.engine.connect() as conn:
        state = (await conn.execute(text("SELECT state FROM xiaowei_request"))).scalar_one()
    assert state == "failed"
    assert len(outbox.sent) == 1 and "未处理" in outbox.texts()[0]


async def test_cancellation_between_accept_and_image_save_locks_readiness(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    images = SessionImageStore(env.engine, clock=env.clock)
    env.app.images = images
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def delayed_save(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await hold.wait()

    async def download(mid: str, key: str, kind: str, *, max_bytes: int) -> bytes:
        return _png()

    monkeypatch.setattr(images, "save", delayed_save)
    outbox = Outbox()
    gateway = FeishuGateway(
        env.service,
        config(image_input_enabled=True),
        outbox,
        clock=env.clock,
        download_resource=download,
    )
    task = asyncio.create_task(gateway.receive(_event(Feishu(env, gateway, outbox))))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not env.readiness.ok
    assert await env.requests() == 1 and outbox.sent == []
