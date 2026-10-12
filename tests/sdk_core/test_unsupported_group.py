"""E5a: platform mention and current membership precede a no-data group receipt."""

import asyncio
import json

import pytest
from tests.sdk_core.test_feishu import Outbox
from tests.sdk_core.test_group_gateway import BOT, mention, running
from tests.sdk_core.test_group_identity import A, Env
from tests.sdk_core.test_group_identity import env as env
from tests.sdk_core.test_runtime import until
from tests.sdk_core.test_unsupported_feishu import CANARY, media_event, pg_calls

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize("kind", ["image", "file", "post"])
async def test_addressed_member_gets_one_receipt_to_original_message(env: Env, kind: str) -> None:
    async with running(env) as fs:
        value = media_event(fs, kind)
        with pg_calls(env.engine) as calls:
            await fs.gateway.receive(value)
            await fs.gateway.receive(value)
    assert len(fs.outbox.sent) == 1 and fs.outbox.sent[0][0] == "oc_group"
    assert "暂不支持" in fs.outbox.texts()[0] and "文字" in fs.outbox.texts()[0]
    assert fs.outbox.replies == [value["event"]["message"]["message_id"]]
    assert env.members.checks == [("oc_group", A)]
    assert calls == [] and env.scripts.calls == {} and env.adapter.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"mentions": []},
        {"mentions": [mention("ou_other_bot")]},
        {"event__message__chat_id": "oc_other"},
        {"age_seconds": 301},
        {"event__sender__sender_type": "bot"},
        {"header__tenant_key": "foreign"},
    ],
)
async def test_nontext_group_not_addressed_or_invalid_is_silent_before_directory(
    env: Env, changes: dict
) -> None:
    async with running(env) as fs:
        value = media_event(fs, "image", **changes)
        with pg_calls(env.engine) as calls:
            await fs.gateway.receive(value)
    assert fs.outbox.sent == [] and env.members.checks == [] and calls == []
    assert env.scripts.calls == {} and env.adapter.calls == []


async def test_post_embedded_at_or_commands_do_not_establish_group_addressing(env: Env) -> None:
    async with running(env) as fs:
        content = json.dumps(
            {
                "zh_cn": {
                    "title": CANARY,
                    "content": [
                        [{"tag": "at", "user_id": BOT}, {"tag": "text", "text": "/新建 /批准"}]
                    ],
                }
            }
        )
        value = fs.event(
            "ignored", content=content, mentions=[], event__message__message_type="post"
        )
        await fs.gateway.receive(value)
    assert fs.outbox.sent == [] and env.members.checks == []
    assert env.scripts.calls == {} and env.adapter.calls == []


@pytest.mark.parametrize("membership", ["gone", "error", "unknown"])
async def test_group_notice_rechecks_current_member_and_never_retargets(
    env: Env, membership: str
) -> None:
    if membership == "gone":
        env.members.current.remove(A)
    else:
        env.members.mode = membership
    async with running(env) as fs:
        with pg_calls(env.engine) as calls:
            await fs.gateway.receive(media_event(fs, "file"))
    assert env.members.checks == [("oc_group", A)]
    assert fs.outbox.sent == [] and calls == [] and env.adapter.calls == []


async def test_no_bot_identity_cannot_authorize_a_group_nontext_notice(env: Env) -> None:
    async with running(env, bot=None) as fs:
        await fs.gateway.receive(media_event(fs, "post"))
    assert fs.outbox.sent == [] and env.members.checks == [] and env.scripts.calls == {}


async def test_concurrent_nontext_reserves_before_the_member_lookup(env: Env) -> None:
    gate = asyncio.Event()
    env.members.holds[A] = gate
    async with running(env) as fs:
        first = asyncio.create_task(fs.gateway.receive(media_event(fs, "file")))
        try:
            await until(lambda: bool(env.members.checks))
            async with asyncio.timeout(1):
                await fs.gateway.receive(media_event(fs, "image"))
            assert env.members.checks == [("oc_group", A)] and fs.outbox.sent == []
        finally:
            gate.set()
            await first
    assert len(fs.outbox.sent) == 1 and env.scripts.calls == {} and env.adapter.calls == []


async def test_group_unknown_send_is_not_retried_and_does_not_lock_business_readiness(
    env: Env,
) -> None:
    outbox = Outbox(outcomes=["unknown"])
    async with running(env, outbox) as fs:
        value = media_event(fs, "file")
        await fs.gateway.receive(value)
        await fs.gateway.receive(value)
    assert len(outbox.sent) == 1 and env.members.checks == [("oc_group", A)]
    assert env.store.readiness.ok and env.scripts.calls == {} and env.adapter.calls == []
