"""DS saved result resend uses formal runtime and current authorization, never refetches DS."""

import pytest
from lark_channel.channel.errors import FeishuChannelErrorCode, SendError
from lark_channel.channel.types import SendResult
from tests.sdk_core.test_app import tool_call
from tests.sdk_core.test_dolphinscheduler_runtime import (
    DS_TOOLS,
    config_values,
    ds_answer,
    ds_api,
    list_failed,
    source,
)
from tests.sdk_core.test_feishu import FakeChannel
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_runtime import feishu_config, feishu_event, until

from xiaowei.channel import ResultUnavailableError
from xiaowei.runtime import ServeConfig, resend

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize("revoked", [False, True])
async def test_formal_ds_resend_rechecks_permissions_without_model_or_ds_io(
    env, monkeypatch, revoked
):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        values = config_values(env.port, [source(base_url=url)])
        values["feishu"] = feishu_config(max_reply_chars=3500)
        values["access"]["grants"]["alice"] += sorted(DS_TOOLS)
        config = ServeConfig.model_validate(values)
        channel = FakeChannel(
            result=SendResult.fail(
                SendError(code=FeishuChannelErrorCode.PERMISSION_DENIED, retryable=False)
            )
        )
        message = env.scripts.add(
            "DS B 待重发结果",
            tool_call("ds_select_clusters", clusters=["ds-b"]),
            list_failed(),
            ds_answer,
        )
        async with env.running(config, feishu_channel=channel) as served:
            channel.emit_raw_from_sdk_thread(feishu_event(env, message, "om_ds_resend"))
            await until(lambda: bool(channel.sends))
            assert await served.finish() == 0
        assert await env.scalar("SELECT delivery FROM xiaowei_request") == "failed"
        calls = len(env.scripts.calls[message])
        assert len(requests) == 1
        if revoked:
            values["access"]["grants"]["alice"] = [
                t for t in values["access"]["grants"]["alice"] if t not in DS_TOOLS
            ]
        current = ServeConfig.model_validate(values)
        sender = FakeChannel()
        arguments = dict(
            clock=env.clock,
            feishu_channel=sender,
            starrocks_connect=env.connect(current),
            subject_id="alice",
            chat_id="oc_alice",
            message_id="om_ds_resend",
        )
        if revoked:
            with pytest.raises(ResultUnavailableError):
                await resend(current, **arguments)
            assert sender.sends == []
        else:
            assert await resend(current, **arguments) == "sent"
            assert len(sender.sends) == 1
            assert await resend(current, **{**arguments, "feishu_channel": FakeChannel()}) is None
        assert len(requests) == 1 and len(env.scripts.calls[message]) == calls
