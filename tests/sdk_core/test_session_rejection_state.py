"""S2 trusted snapshots: real storage, existing application/governance, bounded HTTP substitutes."""

import json
from datetime import UTC, datetime

import pytest
from agents.extensions.memory import SQLAlchemySession
from pydantic import ValidationError
from sqlalchemy import TextClause, text
from sqlalchemy.engine import URL
from tests.sdk_core.synthetic_tools import context
from tests.sdk_core.test_app import cite, clarify, tool_call, upstream_error
from tests.sdk_core.test_channel_service import Env, Outbox, ref
from tests.sdk_core.test_channel_service import env as env
from tests.sdk_core.test_session_policy import LIMITS, _call, _cite, harness

from xiaowei.app import _turn_error
from xiaowei.channel_store import (
    ChannelStoreUnavailableError,
    RequestUnavailableError,
    ResultNotSavedError,
)
from xiaowei.feishu_render import build_feishu_message
from xiaowei.models import Delivery, SessionSnapshot
from xiaowei.session import (
    SessionItemRejectedError,
    SessionLimitError,
    SessionStoreError,
    SessionUnverifiableError,
)
from xiaowei.storage import Readiness, hold_instance_lock

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (SessionItemRejectedError("会话内容不符合数据策略"), "content_rejected"),
        (SessionStoreError("会话存储不可用"), "storage_failed"),
        (SessionUnverifiableError(), "scope_unverifiable"),
    ],
)
def test_non_session_rejections_keep_category_and_have_no_snapshot(error, reason) -> None:
    failed = _turn_error(error)
    assert failed.reason == reason
    assert failed.session_state is None


@pytest.mark.parametrize(
    ("change", "reason", "words", "known_count"),
    [
        (text("UPDATE xiaowei_session SET turns = 5"), "history_turn_limit", "交互轮数上限", 5),
        (text("UPDATE xiaowei_session SET expires_at = '2020-01-01'"), "expired", "保留期限", 1),
        (
            text("UPDATE xiaowei_session SET profile_fingerprint = 'binding-sentinel'"),
            "binding_changed",
            "策略已变化",
            1,
        ),
        (text("UPDATE xiaowei_session SET state = 'sealed'"), "unavailable", "不能安全继续", 1),
        (
            text("UPDATE xiaowei_session SET owner_id = 'foreign-owner-sentinel'"),
            "unavailable",
            "不能安全继续",
            None,
        ),
    ],
)
async def test_session_failure_preserves_closed_reason_without_content_or_extra_io(
    env: Env,
    caplog: pytest.LogCaptureFixture,
    change: TextClause,
    reason: str,
    words: str,
    known_count: int | None,
) -> None:
    caplog.set_level("INFO", logger="xiaowei.app")
    message = env.scripts.add("first", tool_call("order_total", region="east"), cite())
    good = await env.run(message)
    assert good.state == "completed"
    assert good.session_state is not None and good.session_state.phase == "after_commit"
    assert good.session_state.committed_turns == 1
    async with env.engine.begin() as conn:
        await conn.execute(change)
        body = await conn.scalar(text("SELECT answer FROM xiaowei_request"))
        assert "session_state" not in body
    calls = len(env.adapter.calls)
    failed = await env.run("question-sentinel-host-password-signature", "failed")
    assert failed.failure_code == "session_failed"
    snapshot = failed.session_state
    assert snapshot is not None
    assert (snapshot.reason, snapshot.phase, snapshot.committed_turns) == (
        reason,
        "before_start",
        known_count,
    )
    assert snapshot.replay_bytes is None
    if known_count is None:
        assert snapshot.expires_at is None
    assert env.model_calls("question-sentinel-host-password-signature") == 0
    assert len(env.adapter.calls) == calls
    assert "reason=session_unavailable" in caplog.text
    assert f"session_reject={reason}" in caplog.text
    for sentinel in ("question-sentinel", "binding-sentinel", "foreign-owner-sentinel"):
        assert sentinel not in caplog.text
        assert sentinel not in snapshot.model_dump_json()
    persisted = await env.results(env.channel_store()).view(ref("failed"))
    assert persisted.delivery is not None
    assert words in persisted.delivery.content
    assert "本轮结束时" in (persisted.delivery.session_notice or "")


async def test_complete_replay_bytes_limit_is_measured_without_changing_history(
    postgres_url: URL,
) -> None:
    async with harness(postgres_url) as h:
        await h.turn(context(), [_call(), _cite()])
        old = await h.stored()
        session = h.session(
            context(turn="t2"), limits=LIMITS.model_copy(update={"max_history_bytes": 200})
        )
        with pytest.raises(SessionLimitError) as caught:
            await session.get_items()
        snapshot = caught.value.snapshot
        assert snapshot is not None and snapshot.reason == "history_bytes_limit"
        assert snapshot.phase == "before_start" and snapshot.committed_turns == 1
        assert snapshot.replay_bytes is not None and snapshot.replay_bytes >= 200
        assert await h.stored() == old
        assert await h.state() == ("active", 1)
        assert len(h.adapter.calls) == 1


async def test_corrupt_history_is_unavailable_not_a_capacity_limit(env: Env) -> None:
    assert (await env.run(env.scripts.add("first", clarify()))).state == "completed"
    inner = SQLAlchemySession(
        (await env.store.get("web", "alice", "cookie-1", "r1")).session_id, engine=env.engine
    )
    await inner.add_items(
        [{"type": "function_call_output", "call_id": "unpaired", "output": "canary"}]
    )
    failed = await env.run("do-not-run", "r2")
    assert failed.session_state is not None and failed.session_state.reason == "unavailable"
    assert failed.session_state.replay_bytes is None
    assert env.model_calls("do-not-run") == 0


@pytest.mark.parametrize("case", ["content", "model", "storage"])
async def test_other_failures_do_not_acquire_session_limit_notices(
    env: Env,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    caplog.set_level("INFO", logger="xiaowei.app")
    if case == "content":
        failed = await env.run("x" * 300)
        assert env.model_calls("x" * 300) == 0
    elif case == "model":
        failed = await env.run(env.scripts.add("bad-model", upstream_error))
    else:
        await env.fail_request_writes("NEW.state='completed'")
        with pytest.raises(ResultNotSavedError):
            await env.run(env.scripts.add("bad-save", clarify()))
        failed = await env.store.get("web", "alice", "cookie-1", "r1")
        assert failed.failure_code == "result_not_saved"
        assert await env.session_state(failed.session_id) == "closed"
    assert failed.session_state is None
    view = await env.results().view(ref())
    assert view.delivery is not None and view.delivery.session_notice is None
    assert "session_reject=" not in caplog.text


async def test_failed_snapshot_needs_current_owner_channel_and_permission(env: Env) -> None:
    assert (await env.run(env.scripts.add("first", clarify()))).state == "completed"
    async with env.engine.begin() as conn:
        await conn.execute(text("UPDATE xiaowei_session SET turns=5"))
    await env.run("not-run", "r2")
    for other in (ref("r2", subject="bob"), ref("r2", channel="feishu"), ref("missing")):
        with pytest.raises(RequestUnavailableError):
            await env.results().view(other)
        out = Outbox()
        with pytest.raises(RequestUnavailableError):
            await env.results().send(other, out)
        assert out.sent == []
    env.grants.allowed.clear()
    from xiaowei.channel import AccessDeniedError

    with pytest.raises(AccessDeniedError):
        await env.results().view(ref("r2"))


@pytest.mark.parametrize("field", ["owner", "subject"])
async def test_saved_snapshot_is_not_visible_if_record_recipient_no_longer_matches(
    env: Env, field: str
) -> None:
    assert (await env.run(env.scripts.add("first", clarify()))).state == "completed"
    async with env.engine.begin() as conn:
        await conn.execute(text("UPDATE xiaowei_session SET turns=5"))
    await env.run("not-run", "r2")
    statement = (
        text("UPDATE xiaowei_request SET owner_id='bob' WHERE state='failed'")
        if field == "owner"
        else text("UPDATE xiaowei_request SET subject_id='bob' WHERE state='failed'")
    )
    async with env.engine.begin() as conn:
        await conn.execute(statement)
    out = Outbox()
    with pytest.raises(RequestUnavailableError):
        await env.results().view(ref("r2"))
    with pytest.raises(RequestUnavailableError):
        await env.results().send(ref("r2"), out)
    assert out.sent == []


async def test_old_null_snapshot_keeps_generic_receipt_without_guessing(env: Env) -> None:
    assert (await env.run(env.scripts.add("first", clarify()))).state == "completed"
    async with env.engine.begin() as conn:
        await conn.execute(text("UPDATE xiaowei_session SET turns=5"))
    await env.run("not-run", "r2")
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE xiaowei_request SET session_state=NULL WHERE state='failed'")
        )
    view = await env.results(env.channel_store()).view(ref("r2"))
    assert view.delivery is not None
    assert view.delivery.session_notice is None
    assert "本轮内容未被接受或会话不可继续" in view.delivery.content
    assert "交互轮数" not in view.delivery.content


async def test_snapshot_write_failure_after_session_commit_keeps_result_not_saved(env: Env) -> None:
    await env.fail_request_writes("NEW.session_state IS NOT NULL")
    message = env.scripts.add("query-once", tool_call("order_total", region="east"), cite())
    with pytest.raises(ResultNotSavedError):
        await env.run(message)
    record = await env.store.get("web", "alice", "cookie-1", "r1")
    assert record.state == "failed" and record.failure_code == "result_not_saved"
    assert record.answer is None and record.session_state is None
    assert await env.session_state(record.session_id) == "closed"
    async with env.engine.connect() as conn:
        assert await conn.scalar(text("SELECT turns FROM xiaowei_session")) == 1
        assert await conn.scalar(text("SELECT count(*) FROM agent_messages")) > 0
    assert len(env.adapter.calls) == 1
    assert env.readiness.ok
    await env.heal_request_writes()
    view = await env.results().view(ref())
    assert view.delivery is not None and view.delivery.session_notice is None
    assert "结果保存失败" in view.delivery.content
    duplicate = await env.run(message)
    assert duplicate == record and len(env.adapter.calls) == 1


@pytest.mark.parametrize("corrupt", ["extra", "reason", "phase", "count", "invalid_json"])
async def test_corrupt_snapshot_refuses_delivery_instead_of_hiding_notice(
    env: Env, corrupt: str
) -> None:
    assert (await env.run(env.scripts.add("first", clarify()))).state == "completed"
    async with env.engine.begin() as conn:
        await conn.execute(text("UPDATE xiaowei_session SET turns=5"))
    await env.run("no-model", "r2")
    async with env.engine.begin() as conn:
        raw = await conn.scalar(
            text("SELECT session_state FROM xiaowei_request WHERE state='failed'")
        )
        data = json.loads(raw)
        if corrupt == "extra":
            data["private_question"] = "canary"
        elif corrupt == "reason":
            data["reason"] = "canary"
        elif corrupt == "phase":
            data["phase"] = "after_commit"
        elif corrupt == "count":
            data["committed_turns"] = "canary"
        raw = "{canary" if corrupt == "invalid_json" else json.dumps(data)
        await conn.execute(
            text("UPDATE xiaowei_request SET session_state=:v WHERE state='failed'"), {"v": raw}
        )
    out = Outbox()
    with pytest.raises(RequestUnavailableError):
        await env.results().view(ref("r2"))
    with pytest.raises(RequestUnavailableError):
        await env.results().send(ref("r2"), out)
    assert out.sent == []


async def test_snapshot_and_terminal_state_roll_back_together_and_lock_readiness(env: Env) -> None:
    assert (await env.run(env.scripts.add("first", clarify()))).state == "completed"
    async with env.engine.begin() as conn:
        await conn.execute(text("UPDATE xiaowei_session SET turns=5"))
    await env.fail_request_writes("NEW.session_state IS NOT NULL")
    with pytest.raises(ChannelStoreUnavailableError):
        await env.run("no-model", "r2")
    assert not env.readiness.ok
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT state, session_state FROM xiaowei_request WHERE state='running'")
            )
        ).one()
        assert tuple(row) == ("running", None)
    await env.heal_request_writes()
    # Existing startup recovery seals uncertain work; it cannot invent a past rejection snapshot.
    restarted_store = env.channel_store(readiness=Readiness())
    async with hold_instance_lock(env.engine, Readiness()) as lock:
        assert (await restarted_store.recover(lock)).interrupted == 1
    failed = await restarted_store.get("web", "alice", "cookie-1", "r2")
    assert failed.state == "interrupted" and failed.session_state is None


def test_snapshot_is_bounded_closed_and_never_an_agent_answer() -> None:
    values = {
        "observed_at": datetime(2026, 10, 11, tzinfo=UTC),
        "phase": "before_start",
        "committed_turns": None,
        "turn_limit": 100,
        "replay_bytes": None,
        "byte_limit": 400_000,
        "expires_at": None,
        "reason": "unavailable",
    }
    for changes in (
        {"text": "canary"},
        {"reason": "canary"},
        {"committed_turns": True},
        {"phase": "after_commit"},
        {"turn_limit": 10**1200},
    ):
        with pytest.raises(ValidationError):
            SessionSnapshot.model_validate({**values, **changes})


def test_feishu_session_receipt_keeps_all_limit_information_or_fails_before_send() -> None:
    receipt = Delivery(
        channel="feishu",
        content="会话已达交互轮数上限。",
        evidence_ids=(),
        session_notice="本轮结束时已提交 100/100 轮。发送 /新建；新建不等于删除保存记录。",
    )
    full = build_feishu_message(receipt, 300, "chat")
    assert "交互轮数上限" in full["text"] and "/新建" in full["text"]
    with pytest.raises(ValueError):
        build_feishu_message(receipt, 30, "chat")
