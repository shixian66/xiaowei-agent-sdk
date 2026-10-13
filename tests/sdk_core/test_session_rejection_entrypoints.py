"""S2: formal serve/SDK/governance/PostgreSQL, with model and StarRocks substitutes."""

import json

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_feishu import FakeChannel
from tests.sdk_core.test_runtime import Env, feishu_config, feishu_event, statements, until
from tests.sdk_core.test_runtime import env as env

from xiaowei.storage import open_engine

pytestmark = pytest.mark.loopback


def limits(*, turns: int = 1, size: int = 200_000) -> dict[str, int]:
    return {"max_history_turns": turns, "max_history_bytes": size, "retention_seconds": 7200}


def query(env: Env, message: str) -> str:
    return env.scripts.add(
        message,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        cite(""),
    )


async def test_web_turn_limit_survives_restart_and_duplicate_without_rerun(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="xiaowei.app")
    message = query(env, "s2-success")
    config = env.config(session_limits=limits())
    async with env.running(config) as served:
        await served.page()
        good = (await served.turn(message, "query", "s2-good")).json()
        assert good["state"] == "completed", good
        assert good["delivery"]["facts"][0]["rows"]
        calls = len(statements(env.drv))
        failed = (await served.turn("s2-content-sentinel", "query", "s2-limit")).json()
        assert failed["state"] == "failed", failed
        assert "交互轮数上限" in failed["delivery"]["content"]
        assert "新建会话" in failed["delivery"]["session_notice"]
        assert "删除" in failed["delivery"]["session_notice"]
        assert env.scripts.calls.get("s2-content-sentinel", []) == []
        assert len(statements(env.drv)) == calls
        assert "reason=session_unavailable" in caplog.text
        assert "session_reject=history_turn_limit" in caplog.text
        assert "s2-content-sentinel" not in caplog.text
        raw = await env.scalar("SELECT session_state FROM xiaowei_request WHERE state='failed'")
        snapshot = json.loads(raw)
        assert snapshot["reason"] == "history_turn_limit"
        assert snapshot["phase"] == "before_start"
        assert snapshot["committed_turns"] == 1
        assert snapshot["replay_bytes"] is None
        cookie = dict(served.client.cookies)
        assert await served.finish() == 0
    async with env.running(config) as restarted:
        restarted.client.cookies.update(cookie)
        reread = (await restarted.client.get("/api/turns/s2-limit")).json()
        assert reread == failed
        duplicate = (await restarted.turn("s2-content-sentinel", "query", "s2-limit")).json()
        assert duplicate == failed
        assert len(statements(env.drv)) == 0
        assert env.scripts.calls.get("s2-content-sentinel", []) == []


async def test_commit_bytes_limit_reports_executed_without_committing_or_replaying(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="xiaowei.app")
    message = query(env, "s2-commit")
    async with env.running(env.config(session_limits=limits(turns=5, size=300))) as served:
        await served.page()
        result = (await served.turn(message, "query", "s2-bytes")).json()
        assert result["state"] == "failed", result
        assert "本轮执行后" in result["delivery"]["content"]
        assert "不会自动重试" in result["delivery"]["content"]
        assert "回答未保存" in result["delivery"]["content"]
        assert len(statements(env.drv)) == 1
        snapshot = json.loads(await env.scalar("SELECT session_state FROM xiaowei_request"))
        assert snapshot["reason"] == "history_bytes_limit"
        assert snapshot["phase"] == "before_commit"
        assert snapshot["committed_turns"] == 0
        assert snapshot["replay_bytes"] > 300
        assert await env.scalar("SELECT turns FROM xiaowei_session") == 0
        assert await env.scalar("SELECT state FROM xiaowei_session") == "sealed"
        assert await env.scalar("SELECT count(*) FROM agent_messages") == 0
        await served.turn(message, "query", "s2-bytes")
        assert len(statements(env.drv)) == 1
        assert "session_reject=history_bytes_limit" in caplog.text


async def test_feishu_turn_limit_has_specific_receipt_and_new_context_instruction(env: Env) -> None:
    channel = FakeChannel()
    message = query(env, "s2-feishu-success")
    config = env.config(session_limits=limits(), feishu=feishu_config())
    async with env.running(config, feishu_channel=channel) as served:
        channel.emit_raw_from_sdk_thread(feishu_event(env, message, "om_s2_good"))
        await until(lambda: len(channel.sends) == 1)
        calls = len(statements(env.drv))
        event = feishu_event(env, "s2-feishu-never-run", "om_s2_limit")
        channel.emit_raw_from_sdk_thread(event)
        await until(lambda: len(channel.sends) == 2)
        text = channel.sends[1][1]["text"]
        assert "交互轮数上限" in text
        assert "/新建" in text and "删除" in text
        assert len(statements(env.drv)) == calls
        assert env.scripts.calls.get("s2-feishu-never-run", []) == []
        channel.emit_raw_from_sdk_thread(event)
        await channel.flush()
        assert await served.finish() == 0
        assert len(channel.sends) == 2


@pytest.mark.parametrize(
    ("update", "reason", "words"),
    [
        (text("UPDATE xiaowei_session SET turns=1"), "history_turn_limit", "交互轮数上限"),
        (text("UPDATE xiaowei_session SET expires_at='2020-01-01'"), "expired", "保留期限"),
        (
            text("UPDATE xiaowei_session SET profile_fingerprint='sentinel-binding'"),
            "binding_changed",
            "策略已变化",
        ),
        (text("UPDATE xiaowei_session SET state='sealed'"), "unavailable", "不能安全继续"),
    ],
)
async def test_formal_session_rejection_keeps_original_category_and_specific_reason(
    env: Env,
    caplog: pytest.LogCaptureFixture,
    update,
    reason: str,
    words: str,
) -> None:
    caplog.set_level("INFO", logger="xiaowei.app")
    message = query(env, "s2-formal-first")
    async with env.running(env.config(session_limits=limits())) as served:
        await served.page()
        assert (await served.turn(message, "query", "s2-first")).json()["state"] == "completed"
        async with open_engine(SecretStr(env.url.render_as_string(hide_password=False))) as engine:
            async with engine.begin() as conn:
                await conn.execute(update)
        calls = len(statements(env.drv))
        result = (
            await served.turn("content-password-signature-sentinel", "query", "s2-reject")
        ).json()
        assert result["state"] == "failed"
        assert words in result["delivery"]["content"]
        snapshot = json.loads(
            await env.scalar("SELECT session_state FROM xiaowei_request WHERE state='failed'")
        )
        assert snapshot["reason"] == reason
        assert "reason=session_unavailable" in caplog.text
        assert f"session_reject={reason}" in caplog.text
        assert "sentinel" not in caplog.text
        assert len(statements(env.drv)) == calls
        assert env.scripts.calls.get("content-password-signature-sentinel", []) == []


async def test_formal_replay_bytes_limit_stops_model_but_allows_existing_evidence_checks(
    env: Env,
) -> None:
    message = query(env, "s2-replay-first")
    async with env.running(env.config(session_limits=limits(turns=5))) as served:
        await served.page()
        assert (await served.turn(message, "query", "s2-first")).json()["state"] == "completed"
        cookie = dict(served.client.cookies)
        assert await served.finish() == 0
    async with env.running(env.config(session_limits=limits(turns=5, size=200))) as restarted:
        restarted.client.cookies.update(cookie)
        result = (await restarted.turn("s2-no-model", "query", "s2-replay-limit")).json()
        assert result["state"] == "failed"
        assert "容量上限" in result["delivery"]["content"]
        snapshot = json.loads(
            await env.scalar("SELECT session_state FROM xiaowei_request WHERE state='failed'")
        )
        assert snapshot["reason"] == "history_bytes_limit" and snapshot["phase"] == "before_start"
        assert snapshot["committed_turns"] == 1 and snapshot["replay_bytes"] >= 200
        assert env.scripts.calls.get("s2-no-model", []) == []
        assert len(statements(env.drv)) == 0
