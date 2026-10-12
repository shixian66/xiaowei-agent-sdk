"""S3 formal serve/Web and Feishu Gateway with SDK, isolated storage, and external substitutes."""

import json

import pytest
from tests.sdk_core.test_feishu import FakeChannel
from tests.sdk_core.test_feishu_render import text_parts
from tests.sdk_core.test_runtime import Env, feishu_config, feishu_event, statements, until
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_session_rejection_entrypoints import limits, query

pytestmark = pytest.mark.loopback


async def test_formal_web_warning_saved_restart_duplicate_and_last_turn(env: Env) -> None:
    config = env.config(session_limits=limits(turns=5))
    async with env.running(config) as served:
        await served.page()
        for number in range(1, 5):
            message = query(env, f"s3-web-{number}")
            good = (await served.turn(message, "query", f"s3-{number}")).json()
            assert good["state"] == "completed", good
            assert good["delivery"]["facts"][0]["rows"]
            if number < 4:
                assert good["delivery"]["session_notice"] is None
        notice = good["delivery"]["session_notice"]
        assert notice is not None and "4/5" in notice and "点击" in notice
        raw = await env.scalar(
            "SELECT session_state FROM xiaowei_request ORDER BY created_at, turn_id"
        )
        assert "session_notice" not in raw
        cookie = dict(served.client.cookies)
        calls = len(statements(env.drv))
        assert calls == 4
        assert await served.finish() == 0
    async with env.running(config) as restarted:
        restarted.client.cookies.update(cookie)
        reread = (await restarted.client.get("/api/turns/s3-4")).json()
        assert reread["delivery"]["session_notice"] == notice
        duplicate = (await restarted.turn("s3-web-4", "query", "s3-4")).json()
        assert duplicate == reread
        assert len(statements(env.drv)) == 0
        last = query(env, "s3-last")
        done = (await restarted.turn(last, "query", "s3-5")).json()
        assert done["state"] == "completed"
        assert "5/5" in done["delivery"]["session_notice"]
        assert "下轮需要新建" in done["delivery"]["session_notice"]
        body = json.dumps(env.scripts.calls[last][0].input, ensure_ascii=False)
        assert notice not in body and "session_notice" not in body
        failed = (await restarted.turn("s3-no-model", "query", "s3-6")).json()
        assert failed["state"] == "failed" and "交互轮数上限" in failed["delivery"]["content"]
        assert env.scripts.calls.get("s3-no-model", []) == []
        assert len(statements(env.drv)) == 1


async def test_formal_feishu_fact_and_clarification_warning_are_one_card(env: Env) -> None:
    channel = FakeChannel()
    config = env.config(session_limits=limits(turns=2), feishu=feishu_config())
    async with env.running(config, feishu_channel=channel) as served:
        first = query(env, "s3-feishu-first")
        second = query(env, "s3-feishu-second")
        channel.emit_raw_from_sdk_thread(feishu_event(env, first, "om_s3_first"))
        await until(lambda: len(channel.sends) == 1)
        channel.emit_raw_from_sdk_thread(feishu_event(env, second, "om_s3_second"))
        await until(lambda: len(channel.sends) == 2)
        message = channel.sends[-1][1]
        assert "card" in message
        visible = "\n".join(text_parts(message, folded=False))
        assert "2/2" in visible and "下轮需要新建" in visible and "/新建" in visible
        assert visible.count("本轮结束时") == 1
        assert len(statements(env.drv)) == 2
        channel.emit_raw_from_sdk_thread(feishu_event(env, second, "om_s3_second"))
        await channel.flush()
        assert await served.finish() == 0
        assert len(channel.sends) == 2
