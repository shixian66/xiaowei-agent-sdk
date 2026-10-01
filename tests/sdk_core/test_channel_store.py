"""P1-B Task 4：请求去重、可重发结果、投递状态、会话映射、中断恢复与维护清理。真实 PostgreSQL。

这里只验证存储状态机本身：不运行 Agent、模型或工具。竞争用两个独立引擎（相当于两个进程）
同时操作同一数据库；写入失败用 PostgreSQL 触发器注入。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from agents.extensions.memory import SQLAlchemySession
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.sdk_core.synthetic_tools import Clock, ready_engine, secret

from xiaowei.channel_store import (
    ChannelStore,
    ChannelStoreUnavailableError,
    NotReadyError,
    RequestConflictError,
    RequestRecord,
    RequestUnavailableError,
    ResultNotSavedError,
    SessionBusyError,
)
from xiaowei.models import AgentAnswer, AnswerInference
from xiaowei.session import SessionStoreError, cleanup_expired
from xiaowei.storage import Readiness, hold_instance_lock, open_engine

pytestmark = pytest.mark.loopback

KEY = SecretStr("synthetic-channel-key")
REQUEST_RETENTION = 3600
SESSION_RETENTION = 7200
EVIDENCE_RETENTION = 3600
ANSWER = AgentAnswer(
    evidence_ids=("ev_1",),
    inferences=[AnswerInference(text="东区平稳", evidence_ids=("ev_1",))],
    clarification=None,
)


def make_store(
    engine: AsyncEngine, clock: Clock, readiness: Readiness | None = None, **overrides: Any
) -> ChannelStore:
    values: dict[str, Any] = {
        "key": KEY,
        "clock": clock,
        "readiness": readiness or Readiness(),
        "request_retention_seconds": REQUEST_RETENTION,
        "session_retention_seconds": SESSION_RETENTION,
        "evidence_retention_seconds": EVIDENCE_RETENTION,
        "max_answer_bytes": 4000,
    }
    values.update(overrides)
    return ChannelStore(engine, **values)


@dataclass
class Env:
    engine: AsyncEngine
    clock: Clock
    readiness: Readiness
    store: ChannelStore

    async def accept(
        self,
        request_id: str = "r1",
        message: str = "东区订单",
        *,
        mode: str = "query",
        subject: str = "alice",
        conversation: str = "cookie-1",
        channel: str = "web",
        store: ChannelStore | None = None,
    ) -> Any:
        return await (store or self.store).accept(
            channel=channel,  # type: ignore[arg-type]
            subject_id=subject,
            conversation=conversation,
            request_id=request_id,
            mode=mode,  # type: ignore[arg-type]
            message=message,
            policy_version="p1",
        )

    async def completed(self, request_id: str = "r1", **kwargs: Any) -> RequestRecord:
        record = (await self.accept(request_id, **kwargs)).record
        record = await self.store.start(record)
        return await self.store.complete(record, ANSWER)

    async def row(self, request: RequestRecord) -> dict[str, Any]:
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM xiaowei_request WHERE turn_id = :t"),
                    {"t": request.turn_id},
                )
            ).mappings()
            return dict(row.one())

    async def session_state(self, session_id: str) -> str | None:
        async with self.engine.connect() as conn:
            return await conn.scalar(
                text("SELECT state FROM xiaowei_session WHERE session_id = :s"), {"s": session_id}
            )

    async def register_session(self, session_id: str, expires_in: int = SESSION_RETENTION) -> None:
        """模拟 PolicySession 登记的会话元数据与一条 SDK 历史。"""
        now = self.clock()
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO xiaowei_session VALUES (:s, 'alice', 'web', 'fp', :c, :e, 1,"
                    " 'active') ON CONFLICT DO NOTHING"
                ),
                {"s": session_id, "c": now, "e": now + timedelta(seconds=expires_in)},
            )
        await SQLAlchemySession(session_id, engine=self.engine).add_items(
            [{"role": "user", "content": "hi"}]
        )

    async def fail_writes(
        self, table: str, when: str = "true", events: str = "INSERT OR UPDATE OR DELETE"
    ) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE FUNCTION xw_fail() RETURNS trigger LANGUAGE plpgsql AS"
                    " $$ BEGIN RAISE EXCEPTION 'injected'; END $$"
                )
            )
            await conn.execute(
                text(
                    f"CREATE TRIGGER xw_fail BEFORE {events} ON {table}"
                    f" FOR EACH ROW WHEN ({when}) EXECUTE FUNCTION xw_fail()"
                )
            )

    async def heal(self, table: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text(f"DROP TRIGGER xw_fail ON {table}"))
            await conn.execute(text("DROP FUNCTION xw_fail()"))


@asynccontextmanager
async def environment(url: URL) -> AsyncIterator[Env]:
    clock = Clock()
    readiness = Readiness()
    async with ready_engine(url) as engine:
        yield Env(engine, clock, readiness, make_store(engine, clock, readiness))


@pytest.fixture
async def env(postgres_url: URL) -> AsyncIterator[Env]:
    async with environment(postgres_url) as value:
        yield value


# ---- 配置 --------------------------------------------------------------------------------


def test_request_retention_cannot_outlive_evidence() -> None:
    with pytest.raises(ValueError, match="保留期"):
        make_store(None, Clock(), request_retention_seconds=EVIDENCE_RETENTION + 1)  # type: ignore[arg-type]


# ---- 去重 --------------------------------------------------------------------------------


async def test_accept_persists_digests_not_the_message(env: Env) -> None:
    first = await env.accept(message="SECRET_MESSAGE_CANARY")
    assert first.created and first.record.state == "accepted"
    assert first.record.delivery == "pending" and first.record.answer is None
    row = await env.row(first.record)
    dumped = json.dumps({k: str(v) for k, v in row.items()}, ensure_ascii=False)
    assert "SECRET_MESSAGE_CANARY" not in dumped and "cookie-1" not in dumped
    assert "r1" not in (row["request_key"], row["conversation_key"])


async def test_same_key_returns_the_existing_request_in_every_state(env: Env) -> None:
    record = (await env.accept()).record
    again = await env.accept()
    assert not again.created and again.record.state == "accepted"

    running = await env.store.start(record)
    assert (await env.accept()).record.state == "running"

    done = await env.store.complete(running, ANSWER)
    replay = await env.accept()
    assert not replay.created and replay.record.state == "completed"
    assert replay.record.answer == ANSWER and done.answer == ANSWER

    failed = (await env.accept("r2")).record
    await env.store.fail(await env.store.start(failed), "model_failed")
    seen = (await env.accept("r2")).record
    assert seen.state == "failed" and seen.failure_code == "model_failed"


@pytest.mark.parametrize(
    "change",
    [{"message": "西区订单"}, {"mode": "diagnose"}],
    ids=["message", "mode"],
)
async def test_same_key_with_different_semantics_conflicts(
    env: Env, change: dict[str, Any]
) -> None:
    await env.accept()
    with pytest.raises(RequestConflictError):
        await env.accept(**change)


async def test_request_ids_are_scoped_to_owner_and_conversation(env: Env) -> None:
    """不同 owner 或会话语境的同名请求编号互不影响，也不能读取对方的记录。"""
    mine = (await env.accept()).record
    theirs = await env.accept(subject="bob", message="别的问题")
    other_tab = await env.accept(conversation="cookie-2", message="另一个问题")
    assert theirs.created and other_tab.created
    assert len({mine.request_key, theirs.record.request_key, other_tab.record.request_key}) == 3
    with pytest.raises(RequestUnavailableError):
        await env.store.get("web", "bob", "cookie-1", "r-missing")
    assert (await env.store.get("web", "alice", "cookie-1", "r1")).turn_id == mine.turn_id


async def test_two_processes_racing_on_one_key_create_one_request(postgres_url: URL) -> None:
    async with environment(postgres_url) as env, open_engine(secret(postgres_url)) as other:
        second = make_store(other, env.clock)
        results = await asyncio.gather(
            *(env.accept(store=s) for s in (env.store, second, env.store, second))
        )
        assert sum(r.created for r in results) == 1
        assert len({r.record.turn_id for r in results}) == 1
        # 状态迁移同样只有一个进程成功。
        record = results[0].record
        started = await asyncio.gather(
            env.store.start(record), second.start(record), return_exceptions=True
        )
        assert sum(isinstance(s, RequestRecord) for s in started) == 1


async def test_expired_requests_are_unreadable(env: Env) -> None:
    record = await env.completed()
    failed = await env.completed("r2")
    assert await env.store.claim_send(failed)
    await env.store.finish_send(failed, "failed")
    env.clock.advance(REQUEST_RETENTION)
    # 过期结果不能首次发送，也不能显式重发。
    assert not await env.store.claim_send(record)
    assert not await env.store.claim_send(failed, resend=True)
    with pytest.raises(RequestUnavailableError):
        await env.store.get("web", "alice", "cookie-1", "r1")
    with pytest.raises(RequestUnavailableError):
        await env.accept()


async def test_saved_answers_are_bounded_and_validated(env: Env) -> None:
    small = make_store(env.engine, env.clock, env.readiness, max_answer_bytes=50)
    record = await small.start((await env.accept(store=small)).record)
    with pytest.raises(ResultNotSavedError):
        await small.complete(record, ANSWER)
    row = await env.row(record)
    assert row["state"] == "failed" and row["failure_code"] == "result_not_saved"
    assert env.readiness.ok  # 失败状态写入成功：不必锁低 readiness

    done = await env.completed("r2")
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE xiaowei_request SET answer = :a WHERE turn_id = :t"),
            {"a": json.dumps({"evidence_ids": ["x"], "facts": []}), "t": done.turn_id},
        )
    with pytest.raises(RequestUnavailableError):
        await env.store.get("web", "alice", "cookie-1", "r2")


# ---- 投递状态 ----------------------------------------------------------------------------


async def test_first_send_and_resend_each_have_one_winner(postgres_url: URL) -> None:
    async with environment(postgres_url) as env, open_engine(secret(postgres_url)) as other:
        second = make_store(other, env.clock)
        record = await env.completed()
        # 首次发送与事件重投只竞争 pending → sending。
        claims = await asyncio.gather(*(s.claim_send(record) for s in (env.store, second) * 2))
        assert sum(claims) == 1
        assert not await env.store.claim_send(record, resend=True)  # sending 不能重发
        await env.store.finish_send(record, "unknown")

        # 重投遇到 unknown/failed：零发送。显式重发只有一个进程取得。
        assert not await env.store.claim_send(record)
        claims = await asyncio.gather(
            *(s.claim_send(record, resend=True) for s in (env.store, second) * 2)
        )
        assert sum(claims) == 1
        await second.finish_send(record, "failed")
        assert not await env.store.claim_send(record)
        assert await env.store.claim_send(record, resend=True)
        await env.store.finish_send(record, "sent")
        assert not await env.store.claim_send(record)
        assert not await env.store.claim_send(record, resend=True)
        assert (await env.row(record))["delivery"] == "sent"


async def test_resend_needs_a_completed_result_of_the_same_owner(env: Env) -> None:
    pending = await env.completed()
    assert not await env.store.claim_send(pending, resend=True)  # pending 留给首次发送

    failed = (await env.accept("r2")).record
    await env.store.fail(failed, "busy")
    # 固定回执走首次发送条件；它不是可重发的结果。
    assert await env.store.claim_send(failed)
    await env.store.finish_send(failed, "failed")
    assert not await env.store.claim_send(failed, resend=True)

    done = await env.completed("r3")
    await env.store.claim_send(done)
    await env.store.finish_send(done, "failed")
    stranger = RequestRecord(**{**done.__dict__, "subject_id": "bob"})
    assert not await env.store.claim_send(stranger, resend=True)


async def test_finish_send_requires_the_sending_state(env: Env) -> None:
    record = await env.completed()
    with pytest.raises(ChannelStoreUnavailableError):
        await env.store.finish_send(record, "sent")
    assert not env.readiness.ok


# ---- 双存储与关键状态失败 ------------------------------------------------------------------


async def test_result_save_failure_closes_the_session(env: Env) -> None:
    record = await env.store.start((await env.accept()).record)
    await env.register_session(record.session_id)
    await env.fail_writes("xiaowei_request", "NEW.state = 'completed'", "UPDATE")
    with pytest.raises(ResultNotSavedError):
        await env.store.complete(record, ANSWER)
    row = await env.row(record)
    assert (row["state"], row["failure_code"]) == ("failed", "result_not_saved")
    assert await env.session_state(record.session_id) == "closed"
    assert env.readiness.ok


async def test_unwritable_failure_state_locks_readiness(env: Env) -> None:
    record = await env.store.start((await env.accept()).record)
    await env.register_session(record.session_id)
    await env.fail_writes("xiaowei_request")
    with pytest.raises(ResultNotSavedError):
        await env.store.complete(record, ANSWER)
    assert not env.readiness.ok
    # 请求仍是 running，会话未关闭：拒绝新工作，等待重启后的持锁恢复。
    await env.heal("xiaowei_request")
    assert (await env.row(record))["state"] == "running"
    with pytest.raises(NotReadyError):
        await env.accept("r2")
    with pytest.raises(NotReadyError):
        await env.store.new_session("web", "alice", "cookie-1")


async def test_key_transition_failures_lock_readiness(env: Env) -> None:
    record = (await env.accept()).record
    await env.fail_writes("xiaowei_request")
    with pytest.raises(ChannelStoreUnavailableError):
        await env.store.start(record)
    assert not env.readiness.ok


async def test_send_outcome_failure_locks_readiness(env: Env) -> None:
    record = await env.completed()
    assert await env.store.claim_send(record)
    await env.fail_writes("xiaowei_request")
    with pytest.raises(ChannelStoreUnavailableError):
        await env.store.finish_send(record, "sent")
    assert not env.readiness.ok
    await env.heal("xiaowei_request")
    assert (await env.row(record))["delivery"] == "sending"


# ---- 启动恢复 ----------------------------------------------------------------------------


async def test_restart_recovery_interrupts_and_never_sends(postgres_url: URL) -> None:
    async with environment(postgres_url) as env:
        # 每个请求在各自的会话语境中，便于分别核对会话是否被关闭。
        accepted = (await env.accept("r1", conversation="c1")).record
        running = await env.store.start((await env.accept("r2", conversation="c2")).record)
        sending = await env.completed("r3", conversation="c3")
        assert await env.store.claim_send(sending)
        done = await env.completed("r4", conversation="c4")
        for record in (accepted, running, sending):
            await env.register_session(record.session_id)

    # 新进程：取得实例锁后恢复一次。
    async with environment(postgres_url) as env:
        async with hold_instance_lock(env.engine, env.readiness) as lock:
            report = await env.store.recover(lock)
        assert (report.interrupted, report.unknown) == (2, 1)
        for record in (accepted, running):
            row = await env.row(record)
            assert (row["state"], row["failure_code"]) == ("interrupted", "interrupted")
            assert await env.session_state(record.session_id) == "closed"
        assert (await env.row(sending))["delivery"] == "unknown"
        assert (await env.row(done))["delivery"] == "pending"
        assert await env.session_state(sending.session_id) == "active"

        # 中断回执最多发送一次；遗留 sending 变为 unknown 后只能显式重发。
        assert await env.store.claim_send(accepted)
        assert not await env.store.claim_send(sending)
        assert await env.store.claim_send(sending, resend=True)
        assert (await env.accept("r2", conversation="c2")).record.state == "interrupted"


async def test_recovery_failure_rolls_back_and_blocks_readiness(env: Env) -> None:
    running = await env.store.start((await env.accept()).record)
    await env.register_session(running.session_id)
    await env.fail_writes("xiaowei_request")
    async with hold_instance_lock(env.engine, env.readiness) as lock:
        with pytest.raises(ChannelStoreUnavailableError):
            await env.store.recover(lock)
    assert not env.readiness.ok
    await env.heal("xiaowei_request")
    assert (await env.row(running))["state"] == "running"
    assert await env.session_state(running.session_id) == "active"


# ---- 会话映射 ----------------------------------------------------------------------------


async def test_channel_sessions_are_owner_scoped_and_stable(env: Env) -> None:
    first = await env.store.current_session("web", "alice", "cookie-1")
    assert first == await env.store.current_session("web", "alice", "cookie-1")
    assert first.generation == 1 and len(first.session_id) >= 32
    others = {
        (await env.store.current_session("web", "bob", "cookie-1")).session_id,
        (await env.store.current_session("web", "alice", "cookie-2")).session_id,
        (await env.store.current_session("feishu", "alice", "cookie-1")).session_id,
    }
    assert first.session_id not in others and len(others) == 3
    record = (await env.accept()).record
    assert record.session_id == first.session_id


async def test_new_session_rotates_and_refuses_while_running(env: Env) -> None:
    old = await env.store.current_session("web", "alice", "cookie-1")
    record = await env.store.start((await env.accept()).record)
    with pytest.raises(SessionBusyError):
        await env.store.new_session("web", "alice", "cookie-1")
    await env.store.complete(record, ANSWER)

    new = await env.store.new_session("web", "alice", "cookie-1")
    assert new.generation == 2 and new.session_id != old.session_id
    assert await env.store.current_session("web", "alice", "cookie-1") == new
    later = (await env.accept("r2")).record
    assert later.session_id == new.session_id
    # 旧请求仍可按原编号读取，归属不变。
    assert (await env.store.get("web", "alice", "cookie-1", "r1")).session_id == old.session_id


async def test_concurrent_new_sessions_leave_one_current(postgres_url: URL) -> None:
    async with environment(postgres_url) as env, open_engine(secret(postgres_url)) as other:
        second = make_store(other, env.clock)
        await env.store.current_session("web", "alice", "cookie-1")
        results = await asyncio.gather(
            env.store.new_session("web", "alice", "cookie-1"),
            second.new_session("web", "alice", "cookie-1"),
            return_exceptions=True,
        )
        created = [r for r in results if not isinstance(r, BaseException)]
        assert created and all(
            isinstance(r, (SessionBusyError, ChannelStoreUnavailableError))
            for r in results
            if isinstance(r, BaseException)
        )
        async with env.engine.connect() as conn:
            current = await conn.scalar(
                text("SELECT count(*) FROM xiaowei_channel_session WHERE state = 'current'")
            )
        assert current == 1


# ---- 维护清理 ----------------------------------------------------------------------------


async def _rotated(env: Env, request_id: str = "r1") -> RequestRecord:
    """完成一个请求后新建会话：返回的请求属于已不再 current 的旧会话。"""
    record = await env.completed(request_id)
    await env.register_session(record.session_id)
    current = await env.store.new_session("web", "alice", "cookie-1")
    await env.register_session(current.session_id)
    return record


_COUNTS = text(
    """
    SELECT (SELECT count(*) FROM xiaowei_session WHERE session_id = :s),
           (SELECT count(*) FROM xiaowei_request WHERE session_id = :s),
           (SELECT count(*) FROM xiaowei_channel_session WHERE session_id = :s)
    """
)


async def _counts(env: Env, session_id: str) -> tuple[int, int, int, int]:
    """会话元数据、请求、会话映射与 SDK 历史的条数。"""
    async with env.engine.connect() as conn:
        sessions, requests, mappings = (await conn.execute(_COUNTS, {"s": session_id})).one()
    history = await SQLAlchemySession(session_id, engine=env.engine).get_items()
    return sessions, requests, mappings, len(history)


async def test_cleanup_removes_only_expired_inactive_state(env: Env) -> None:
    old = (await _rotated(env)).session_id
    current = (await env.store.current_session("web", "alice", "cookie-1")).session_id
    env.clock.advance(SESSION_RETENTION)
    report = await cleanup_expired(env.engine, now=env.clock(), batch_size=10)
    assert report.sessions == 1
    assert await _counts(env, old) == (0, 0, 0, 0)
    # 当前会话即使过期也保留，由用户新建会话后再清理。
    assert await _counts(env, current) == (1, 0, 1, 1)


async def test_cleanup_skips_sessions_with_sending_requests(env: Env) -> None:
    record = await _rotated(env)
    assert await env.store.claim_send(record)
    env.clock.advance(SESSION_RETENTION)
    assert (await cleanup_expired(env.engine, now=env.clock(), batch_size=10)).sessions == 0
    assert await _counts(env, record.session_id) == (1, 1, 1, 1)
    assert await env.session_state(record.session_id) == "active"


async def test_cleanup_racing_with_resend_keeps_unexpired_requests(env: Env) -> None:
    """会话已过期而请求未过期：清理与显式重发并发，重发照常取得，请求与会话元数据保留。"""
    old = await env.store.current_session("web", "alice", "cookie-1")
    await env.register_session(old.session_id)
    env.clock.advance(SESSION_RETENTION - 60)
    record = await env.completed()
    assert record.session_id == old.session_id
    await env.store.claim_send(record)
    await env.store.finish_send(record, "failed")
    await env.store.new_session("web", "alice", "cookie-1")
    env.clock.advance(60)

    claimed, report = await asyncio.gather(
        env.store.claim_send(record, resend=True),
        cleanup_expired(env.engine, now=env.clock(), batch_size=10),
    )
    assert claimed and report.sessions == 0
    assert (await env.row(record))["delivery"] == "sending"
    sessions, requests, _, _ = await _counts(env, old.session_id)
    assert (sessions, requests) == (1, 1)

    # 请求过期并结束投递后，下一次清理完成。
    await env.store.finish_send(record, "sent")
    env.clock.advance(REQUEST_RETENTION)
    assert (await cleanup_expired(env.engine, now=env.clock(), batch_size=10)).sessions == 1
    assert await _counts(env, old.session_id) == (0, 0, 0, 0)


async def test_cleanup_stops_when_sdk_clear_fails(env: Env) -> None:
    old = (await _rotated(env)).session_id
    env.clock.advance(SESSION_RETENTION)
    await env.fail_writes("agent_messages")
    with pytest.raises(SessionStoreError):
        await cleanup_expired(env.engine, now=env.clock(), batch_size=10)
    # 会话已关闭不可回放，历史与元数据保留；修复后再次运行完成清理。
    assert await env.session_state(old) == "closed"
    assert (await _counts(env, old))[3] == 1
    await env.heal("agent_messages")
    assert (await cleanup_expired(env.engine, now=env.clock(), batch_size=10)).sessions == 1
    assert await _counts(env, old) == (0, 0, 0, 0)


async def test_cleanup_is_batched(env: Env) -> None:
    for i in range(3):
        await _rotated(env, f"r{i}")
    env.clock.advance(SESSION_RETENTION)
    assert (await cleanup_expired(env.engine, now=env.clock(), batch_size=2)).sessions == 2
    assert (await cleanup_expired(env.engine, now=env.clock(), batch_size=2)).sessions == 1
    assert (await cleanup_expired(env.engine, now=env.clock(), batch_size=2)).sessions == 0
