"""E5a formal serve/raw bridge: feedback without starting a stored business turn."""

import copy
import json

import pytest
from tests.sdk_core.test_feishu import FakeChannel
from tests.sdk_core.test_runtime import Env, feishu_config, feishu_event, until
from tests.sdk_core.test_runtime import env as env

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize("kind", ["image", "file", "post"])
async def test_formal_runtime_nontext_receipt_and_invalid_control_have_no_business_turn(
    env: Env, kind: str
) -> None:
    channel = FakeChannel()
    config = env.config(feishu=feishu_config())
    assert config.listen_port != 8501
    async with env.running(config, feishu_channel=channel) as served:
        before = [tuple(conn.executed) for conn in env.drv.connections]
        value = feishu_event(env, "not-a-model-task", "om_unsupported")
        value["event"]["message"].update(
            message_type=kind, content=json.dumps({"file_key": "opaque", "text": "/新建"})
        )
        channel.emit_raw_from_sdk_thread(value)
        await until(lambda: len(channel.sends) == 1)
        invalid = copy.deepcopy(value)
        invalid["header"]["tenant_key"] = "foreign"
        invalid["event"]["message"]["message_id"] = "om_foreign"
        channel.emit_raw_from_sdk_thread(invalid)
        channel.emit_raw_from_sdk_thread(value)
        await channel.flush()
        assert await served.finish() == 0
    assert len(channel.sends) == 1
    chat, message, _opts = channel.sends[0]
    assert chat == "oc_alice" and "暂不支持" in message["text"] and "文字" in message["text"]
    assert env.scripts.calls == {}
    assert [tuple(conn.executed) for conn in env.drv.connections] == before
    for statement in (
        "SELECT count(*) FROM xiaowei_request",
        "SELECT count(*) FROM xiaowei_channel_session",
        "SELECT count(*) FROM xiaowei_session",
        "SELECT count(*) FROM xiaowei_evidence",
    ):
        assert await env.scalar(statement) == 0
