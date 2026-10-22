"""S2: 原始群 post → 正式渠道/治理/SDK；外部发送和模型使用合成替身。"""

import json
from typing import Any

import pytest
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_group_gateway import BOT, KEY, Group, running, states
from tests.sdk_core.test_group_identity import A, Env
from tests.sdk_core.test_group_identity import env as env

from xiaowei.feishu import attributed


def content(text: str, *, bot_id: str = KEY, **extra: Any) -> str:
    return json.dumps(
        {
            "zh_cn": {
                "title": "",
                "content": [
                    [
                        {"tag": "at", "user_id": bot_id, "user_name": "小维"},
                        {"tag": "text", "text": " " + text},
                    ]
                ],
            },
            **extra,
        },
        ensure_ascii=False,
    )


def post(group: Group, text: str, **changes: Any) -> dict[str, Any]:
    return group.event(
        "ignored", content=content(text), event__message__message_type="post", **changes
    )


@pytest.mark.loopback
async def test_text_and_post_reach_the_same_governed_query(env: Env) -> None:
    prompt = env.scripts.add(
        attributed(A, "东区订单？"),
        tool_call("order_total", region="east"),
        cite(),
        tool_call("order_total", region="east"),
        cite(),
    )
    async with running(env) as group:
        await group.ask("东区订单？", message_id="om_text")
        await group.gateway.receive(post(group, "东区订单？", message_id="om_post"))
        await group.gateway.idle()
        assert await states(env) == {
            "om_text": ("completed", "sent"),
            "om_post": ("completed", "sent"),
        }
    assert env.model_calls(prompt) == 4 and len(env.adapter.calls) == 2
    assert group.outbox.replies == ["om_text", "om_post"]


@pytest.mark.loopback
async def test_post_body_at_does_not_grant_entry(env: Env) -> None:
    async with running(env) as group:
        await group.gateway.receive(post(group, "东区订单？", mentions=[]))
        await group.gateway.idle()
    assert env.scripts.calls == {} and env.adapter.calls == []
    assert env.members.checks == [] and group.outbox.sent == []
    assert await env.rows("SELECT count(*) FROM xiaowei_request") == [(0,)]


def raw_post(value: dict[str, Any]) -> dict[str, Any]:
    from tests.sdk_core.test_group_identity import APP, CHAT, TENANT, WHEN

    return {
        "header": {"event_type": "im.message.receive_v1", "app_id": APP, "tenant_key": TENANT},
        "event": {
            "sender": {"sender_type": "user", "tenant_key": TENANT, "sender_id": {"open_id": A}},
            "message": {
                "chat_id": CHAT,
                "chat_type": "group",
                "message_type": "post",
                "create_time": str(int(WHEN.timestamp() * 1000)),
                "message_id": "om_post",
                "mentions": [{"key": KEY, "id": {"open_id": BOT}, "name": "小维"}],
                "content": json.dumps(value, ensure_ascii=False),
            },
        },
    }


@pytest.mark.parametrize(
    ("value", "text", "image"),
    [
        ({"title": "巡检", "content": [[{"tag": "text", "text": "CPU"}]]}, "巡检\nCPU", None),
        (
            {
                "zh_cn": {"content": [[{"tag": "text", "text": "中文"}]]},
                "en_us": {"content": [[{"tag": "img", "image_key": "img_unselected"}]]},
            },
            "中文",
            None,
        ),
        (
            {
                "zh_cn": {
                    "content": [[{"tag": "img", "image_key": "img_old"}]],
                    "content_v2": [
                        [
                            {"tag": "text", "text": "新问题"},
                            {"tag": "img", "image_key": "img_selected"},
                        ]
                    ],
                }
            },
            "新问题",
            "img_selected",
        ),
        (
            {"content": [[{"tag": "code_block", "language": "sql", "text": "select 1;"}]]},
            "```sql\nselect 1;\n```",
            None,
        ),
        (
            {
                "content": [
                    [
                        {"tag": "a", "text": "业务页", "href": "https://example.invalid"},
                        {"tag": "emotion", "emoji_type": "SMILE"},
                    ],
                    [{"tag": "hr"}],
                ]
            },
            "业务页 https://example.invalid:SMILE:\n---",
            None,
        ),
        ({"content": [[{"tag": "md", "text": "**普通文字**"}]]}, "**普通文字**", None),
        (
            {
                "content_v2": [
                    [{"tag": "text", "text": "仅 v2"}, {"tag": "img", "image_key": "img_v2"}]
                ]
            },
            "仅 v2",
            "img_v2",
        ),
    ],
)
def test_post_uses_one_document_and_same_paragraphs_for_text_and_image(value, text, image):
    from tests.sdk_core.test_group_gateway import group_config
    from tests.sdk_core.test_group_identity import WHEN

    from xiaowei.feishu import _parse

    parsed = _parse(raw_post(value), group_config(image_input_enabled=True), WHEN, lambda: BOT)
    assert not parsed.unsupported and parsed.text == text and parsed.image_key == image
    if image:
        assert image not in parsed.text


def test_post_real_format_long_message_keeps_existing_container_budget():
    from tests.sdk_core.test_group_gateway import group_config
    from tests.sdk_core.test_group_identity import WHEN

    from xiaowei.feishu import _parse

    body = '中文 "引号" ' * 1100
    value = {
        "zh_cn": {
            "title": "长消息",
            "content": [
                [{"tag": "at", "user_id": KEY}, {"tag": "text", "text": body[:4400]}],
                [{"tag": "text", "text": body[4400:], "style": ["bold"]}],
            ],
        }
    }
    parsed = _parse(raw_post(value), group_config(max_message_chars=10_000), WHEN, lambda: BOT)
    assert not parsed.unsupported
    assert parsed.text == "长消息\n" + body[:4400] + "\n" + body[4400:].rstrip()


@pytest.mark.parametrize("bot_id", [BOT, KEY])
def test_structured_bot_mention_is_removed_before_command_parsing(bot_id):
    from tests.sdk_core.test_group_gateway import group_config
    from tests.sdk_core.test_group_identity import WHEN

    from xiaowei.feishu import _parse

    value = json.loads(content("/查询 东区订单？", bot_id=bot_id))
    parsed = _parse(raw_post(value), group_config(), WHEN, lambda: BOT)
    assert not parsed.unsupported and parsed.text == "/查询 东区订单？"


@pytest.mark.loopback
@pytest.mark.parametrize(
    "value",
    [
        {
            "content": [
                [{"tag": "img", "image_key": "img_one"}, {"tag": "img", "image_key": "img_two"}]
            ]
        },
        {"content": [[{"tag": "md", "text": "![x](https://canary.invalid/image)"}]]},
        {"content": [[{"tag": "text", "text": "正文"}]], "files": [{"file_key": "file_canary"}]},
        {"content": [[{"tag": "media", "file_key": "media_canary"}]]},
        {"content": [[{"tag": "unknown", "text": "NODE_CANARY"}]]},
        {"content": [[{"tag": "img", "image_key": "https://canary.invalid/image"}]]},
        {"content": [{"tag": "text", "text": "PARAGRAPH_CANARY"}]},
        {"content": [[{"tag": "text", "text": "不得回退"}]], "content_v2": None},
        {"content": [[{"tag": "md", "text": '<img src="https://canary.invalid/image">'}]]},
    ],
)
async def test_unsupported_post_is_rejected_whole_before_download_or_model(env: Env, value, caplog):
    from tests.sdk_core.test_feishu import Outbox
    from tests.sdk_core.test_group_gateway import group_config

    from xiaowei.feishu import FeishuGateway

    downloads = []

    async def download(*args, **kwargs):
        downloads.append((args, kwargs))
        raise AssertionError("拒绝的 post 不应下载")

    outbox = Outbox()
    gateway = FeishuGateway(
        env.service,
        group_config(image_input_enabled=True),
        outbox,
        clock=env.clock,
        bot_open_id=lambda: BOT,
        download_resource=download,
    )
    event = raw_post(value)
    event["event"]["message"]["create_time"] = str(int(env.clock().timestamp() * 1000))
    await gateway.receive(event)
    assert downloads == [] and env.scripts.calls == {} and env.adapter.calls == []
    assert await env.rows("SELECT count(*) FROM xiaowei_request") == [(0,)]
    assert len(outbox.sent) == 1 and "未处理" in outbox.texts()[0]
    assert "CANARY" not in caplog.text and "canary.invalid" not in str(outbox.sent)


@pytest.mark.loopback
async def test_post_image_disabled_is_explicit_before_download(env: Env):
    value = {
        "content": [
            [{"tag": "text", "text": "解释截图"}, {"tag": "img", "image_key": "img_disabled"}]
        ]
    }
    async with running(env) as group:
        event = group.event(
            "ignored", content=json.dumps(value), event__message__message_type="post"
        )
        await group.gateway.receive(event)
    assert len(group.outbox.sent) == 1 and "尚未启用图片分析" in group.outbox.texts()[0]
    assert env.scripts.calls == {} and env.adapter.calls == []
    assert await env.rows("SELECT count(*) FROM xiaowei_request") == [(0,)]


@pytest.mark.loopback
@pytest.mark.parametrize("kind", ["text", "post"])
async def test_query_command_uses_same_entry(env: Env, kind):
    prompt = env.scripts.add(
        attributed(A, "东区订单？"), tool_call("order_total", region="east"), cite()
    )
    async with running(env) as group:
        kwargs = (
            {"content": content("/查询 东区订单？"), "event__message__message_type": "post"}
            if kind == "post"
            else {}
        )
        await group.ask("/查询 东区订单？", **kwargs)
    assert env.model_calls(prompt) == 2 and len(env.adapter.calls) == 1


def test_post_resource_key_is_not_model_text():
    from tests.sdk_core.test_group_gateway import group_config
    from tests.sdk_core.test_group_identity import WHEN

    from xiaowei.feishu import _parse

    value = {
        "content": [
            [
                {"tag": "text", "text": "解释图片"},
                {"tag": "img", "image_key": "img_ISOLATION_CANARY"},
            ]
        ]
    }
    parsed = _parse(raw_post(value), group_config(image_input_enabled=True), WHEN, lambda: BOT)
    assert not parsed.unsupported
    assert parsed.image_key == "img_ISOLATION_CANARY"
    assert "img_ISOLATION_CANARY" not in parsed.text


def test_post_link_keeps_label_and_target_as_plain_text():
    from tests.sdk_core.test_group_gateway import group_config
    from tests.sdk_core.test_group_identity import WHEN

    from xiaowei.feishu import _parse

    value = {
        "content": [[{"tag": "a", "text": "业务页", "href": "https://example.invalid/dashboard"}]]
    }
    parsed = _parse(raw_post(value), group_config(), WHEN, lambda: BOT)
    assert parsed.text == "业务页 https://example.invalid/dashboard"


@pytest.mark.loopback
async def test_deep_invalid_post_is_rejected_without_crashing_gateway(env: Env):
    async with running(env, max_message_chars=1000) as group:
        raw = '{"content":' + "[" * 1500 + "0" + "]" * 1500 + "}"
        event = group.event("ignored", content=raw, event__message__message_type="post")
        await group.gateway.receive(event)
    assert len(group.outbox.sent) == 1 and "结构或文字不合法" in group.outbox.texts()[0]
    assert env.scripts.calls == {} and env.adapter.calls == []
    assert await env.rows("SELECT count(*) FROM xiaowei_request") == [(0,)]


def test_post_preserves_other_members_mention_as_untrusted_text():
    from tests.sdk_core.test_group_gateway import group_config
    from tests.sdk_core.test_group_identity import WHEN

    from xiaowei.feishu import _parse

    value = {
        "content": [
            [
                {"tag": "at", "user_id": KEY},
                {"tag": "text", "text": "提醒 "},
                {"tag": "at", "user_id": "@_user_2"},
            ]
        ]
    }
    parsed = _parse(raw_post(value), group_config(), WHEN, lambda: BOT)
    assert parsed.text == "提醒 @_user_2"
