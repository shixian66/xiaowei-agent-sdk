"""指定群截图：平台 mention、当前成员及另一成员续问都沿正式渠道复核。"""

import asyncio
import base64
import json
from typing import Any

import httpx2
import pytest
from tests.sdk_core.test_channel_service import app_config
from tests.sdk_core.test_feishu import Outbox
from tests.sdk_core.test_feishu_images import _png
from tests.sdk_core.test_group_gateway import BOT, Group, group_config, mention
from tests.sdk_core.test_group_identity import A, B, Env
from tests.sdk_core.test_group_identity import env as env
from tests.sdk_core.test_vertex_model import VERTEX

from xiaowei.app import Application
from xiaowei.channel import ChannelService
from xiaowei.feishu import FeishuGateway
from xiaowei.governance import GovernedTools
from xiaowei.image_input import SessionImageStore
from xiaowei.model_api import open_model

pytestmark = pytest.mark.loopback


async def test_group_image_requires_mention_and_member_then_b_can_replay(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _png()
    encoded = base64.b64encode(original).decode("ascii")
    calls: list[dict[str, Any]] = []
    downloads: list[str] = []

    def endpoint(request: httpx2.Request) -> httpx2.Response:
        calls.append(json.loads(request.content))
        answer = json.dumps(
            {"evidence_ids": [], "inferences": [], "clarification": "请说明截图问题。"}
        )
        return httpx2.Response(
            200,
            json={
                "candidates": [
                    {
                        "index": 0,
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": answer}]},
                    }
                ]
            },
        )

    async def download(mid: str, key: str, kind: str, *, max_bytes: int) -> bytes:
        downloads.append(mid)
        return original

    monkeypatch.setenv(VERTEX.api_key_ref.removeprefix("env:"), "synthetic")
    profile = VERTEX.model_copy(update={"image_input_enabled": True})
    async with open_model(profile, transport=httpx2.MockTransport(endpoint)) as model:
        app = Application(
            app_config(),
            model=model,
            engine=env.engine,
            governance=GovernedTools(env.evidence),
            local_tools={},
            clock=env.clock,
            images=SessionImageStore(env.engine, clock=env.clock),
        )
        service = ChannelService(app, env.results)
        outbox = Outbox()
        gateway = FeishuGateway(
            service,
            group_config(image_input_enabled=True),
            outbox,
            clock=env.clock,
            bot_open_id=lambda: BOT,
            download_resource=download,
        )
        group = Group(env, gateway, outbox)
        image_event = group.event(
            "ignored",
            message_id="om_group_image",
            content=json.dumps({"image_key": "img_group"}),
            event__message__message_type="image",
            mentions=[mention(BOT)],
        )
        without_mention = group.event(
            "ignored",
            content=json.dumps({"image_key": "img_group"}),
            event__message__message_type="image",
            mentions=[],
        )
        await gateway.receive(without_mention)
        env.members.current.remove(A)
        await gateway.receive(image_event)
        assert downloads == [] and calls == []
        env.members.current.add(A)
        task = asyncio.create_task(gateway.run())
        try:
            await gateway.receive(image_event)
            await gateway.idle()
            await gateway.receive(group.event("截图右侧是什么？", sender=B))
            await gateway.idle()
            env.members.current.remove(B)
            await gateway.receive(group.event("再看一眼截图", sender=B))
            await gateway.idle()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert downloads == ["om_group_image"] and len(calls) == 2
    for call in calls:
        originals = [
            part["inlineData"]["data"]
            for item in call["contents"]
            for part in item["parts"]
            if "inlineData" in part
        ]
        assert originals == [encoded]
    assert env.adapter.calls == []
