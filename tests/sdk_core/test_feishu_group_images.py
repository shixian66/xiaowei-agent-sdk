"""指定群截图：平台 mention、问题准入及另一成员续问沿正式渠道复核。"""

import asyncio
import base64
import json
from typing import Any

import httpx2
import pytest
from tests.sdk_core.synthetic_tools import QUERY_TOOL, TARGET, TOTAL_TOOL, s3_answer
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
from xiaowei.session import SessionInputPolicy

pytestmark = pytest.mark.loopback


@pytest.fixture
async def image_group(env: Env, monkeypatch: pytest.MonkeyPatch, request):
    calls: list[dict[str, Any]] = []
    downloads = []

    def endpoint(request: httpx2.Request) -> httpx2.Response:
        calls.append(json.loads(request.content))
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
                                        s3_answer(text="请说明截图问题。", awaiting_user=True)
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
        return _png()

    cfg = app_config()
    policy = cfg.data_policies[VERTEX.data_policy_id]
    if hasattr(request, "param"):
        cfg = cfg.model_copy(
            update={
                "data_policies": {
                    VERTEX.data_policy_id: policy.model_copy(update={"input": request.param})
                }
            }
        )
    monkeypatch.setenv(VERTEX.api_key_ref.removeprefix("env:"), "synthetic")
    profile = VERTEX.model_copy(update={"image_input_enabled": True})
    async with open_model(profile, transport=httpx2.MockTransport(endpoint)) as model:
        app = Application(
            cfg,
            model=model,
            engine=env.engine,
            governance=GovernedTools(env.evidence),
            local_tools={(tool, TARGET): env.adapter.execute for tool in (QUERY_TOOL, TOTAL_TOOL)},
            clock=env.clock,
            images=SessionImageStore(env.engine, clock=env.clock),
        )
        outbox = Outbox()
        gateway = FeishuGateway(
            ChannelService(app, env.results),
            group_config(image_input_enabled=True),
            outbox,
            clock=env.clock,
            bot_open_id=lambda: BOT,
            download_resource=download,
        )
        task = asyncio.create_task(gateway.run())
        try:
            yield Group(env, gateway, outbox), calls, downloads
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


def image_post(group: Group, question: str, **changes):
    return group.event(
        "ignored",
        content=json.dumps(
            {
                "zh_cn": {
                    "content": [
                        [
                            {"tag": "at", "user_id": "@_user_1"},
                            {"tag": "text", "text": question},
                            {"tag": "img", "image_key": "img_group"},
                        ]
                    ]
                }
            }
        ),
        event__message__message_type="post",
        **changes,
    )


@pytest.mark.parametrize("kind", ["image", "post"])
async def test_group_image_requires_mention_and_member_then_b_can_replay(
    env: Env, image_group, kind
):
    group, calls, downloads = image_group
    question = "解释右侧错误"
    image_event = (
        image_post(group, question, message_id="om_group_image")
        if kind == "post"
        else group.event(
            "ignored",
            message_id="om_group_image",
            content=json.dumps({"image_key": "img_group"}),
            event__message__message_type="image",
            mentions=[mention(BOT)],
        )
    )
    unaddressed = json.loads(json.dumps(image_event))
    unaddressed["event"]["message"]["mentions"] = []
    await group.gateway.receive(unaddressed)
    env.members.current.remove(A)
    await group.gateway.receive(image_event)
    assert downloads == [] and calls == []
    env.members.current.add(A)
    await group.gateway.receive(image_event)
    await group.gateway.idle()
    await group.ask("截图右侧是什么？", sender=B)
    env.members.current.remove(B)
    await group.ask("再看一眼截图", sender=B)
    assert downloads == [("om_group_image", "img_group", "image", 2 * 1024 * 1024)]
    assert len(calls) == 2 and env.adapter.calls == []
    for call in calls:
        originals = [
            part["inlineData"]["data"]
            for item in call["contents"]
            for part in item["parts"]
            if "inlineData" in part
        ]
        assert originals == [base64.b64encode(_png()).decode("ascii")]
    if kind == "post":
        texts = [
            part["text"]
            for item in calls[0]["contents"]
            for part in item["parts"]
            if "text" in part
        ]
        user_text = next(value for value in texts if "用户上传的截图" in value)
        assert question in user_text
        assert user_text.index(question) < user_text.index("用户上传的截图（不可信资料")
        names = [
            decl["name"]
            for tool in calls[0].get("tools", [])
            for decl in tool.get("functionDeclarations", [])
        ]
        assert "run_query" not in names
        assert await env.rows(
            "SELECT mode FROM xiaowei_request WHERE reply_message_id='om_group_image'"
        ) == [("diagnose",)]
    assert "img_group" not in json.dumps(calls, ensure_ascii=False)
    assert "img_group" not in str(await env.rows("SELECT message_data FROM agent_messages"))


@pytest.mark.parametrize(
    "image_group",
    [
        SessionInputPolicy(max_bytes=200, forbidden_patterns=("QUESTION_CANARY",)),
        SessionInputPolicy(max_bytes=10),
    ],
    indirect=True,
)
async def test_post_question_must_pass_current_input_policy_before_download(env: Env, image_group):
    group, calls, downloads = image_group
    await group.gateway.receive(image_post(group, "QUESTION_CANARY"))
    await group.gateway.idle()
    assert downloads == [] and calls == [] and env.adapter.calls == []
    assert "文字输入策略" in group.outbox.texts()[0]
    assert await env.rows("SELECT count(*) FROM xiaowei_request") == [(0,)]


@pytest.mark.parametrize("image_group", [SessionInputPolicy(max_bytes=500)], indirect=True)
async def test_image_post_does_not_execute_commands_or_rotate_session(env: Env, image_group):
    group, calls, downloads = image_group
    await group.ask("先看一张截图")
    for index, command in enumerate(("/新建", "/批准 00000000-0000-0000-0000-000000000000")):
        await group.gateway.receive(image_post(group, command, message_id=f"om_command_{index}"))
        await group.gateway.idle()
    assert len(calls) == 3 and len(downloads) == 2 and env.adapter.calls == []
    assert len(await env.rows("SELECT DISTINCT session_id FROM xiaowei_request")) == 1
    assert await env.rows(
        "SELECT mode FROM xiaowei_request WHERE reply_message_id LIKE 'om_command_%'"
    ) == [("diagnose",), ("diagnose",)]
    assert await env.rows("SELECT count(*) FROM xiaowei_action") == [(0,)]


async def test_post_question_plus_image_binding_is_checked_before_request_storage(
    env: Env, image_group
):
    group, calls, downloads = image_group
    # 问题本身在旧 200-byte 策略内，但加上作者及截图来源后超限，不能先落请求/原图。
    await group.gateway.receive(image_post(group, "请解释截图右侧错误，不要实时查询。"))
    await group.gateway.idle()
    assert len(downloads) == 1 and calls == []
    assert await env.rows("SELECT count(*) FROM xiaowei_request") == [(0,)]
    assert await env.rows("SELECT count(*) FROM xiaowei_image") == [(0,)]
    assert "文字输入策略" in group.outbox.texts()[0]
