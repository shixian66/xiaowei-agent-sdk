"""P1-B Task 4：请求去重、可重发结果、投递状态、会话映射、中断恢复与维护清理。真实 PostgreSQL。

这里只验证存储状态机本身：不运行 Agent、模型或工具。竞争用两个独立引擎（相当于两个进程）
同时操作同一数据库；写入失败用 PostgreSQL 触发器注入。
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
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
    CallerFailureCode,
    ChannelStore,
    ChannelStoreUnavailableError,
    DeliveryClaim,
    NotReadyError,
    RequestConflictError,
    RequestRecord,
    RequestUnavailableError,
    ResultNotSavedError,
    SessionBusyError,
)
from xiaowei.models import AgentAnswer, AnswerInference, TurnAnswer
from xiaowei.session import SessionStoreError, cleanup_expired
from xiaowei.storage import (
    Backend,
    Readiness,
    _release,
    hold_backend,
    hold_instance_lock,
    open_engine,
)

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
# 保存的是回答与本轮模型可见的证据（另含一条未引用的 ev_2）。
TURN = TurnAnswer(answer=ANSWER, context_evidence=("ev_1", "ev_2"))


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

    async def settle(self, record: RequestRecord, outcome: str, *, resend: bool = False) -> None:
        """取得投递权并落定一次发送（不实际发送）。"""
        claim = await self.store.claim_send(record, resend=resend)
        assert claim is not None
        await self.store.finish_send(claim, outcome)  # type: ignore[arg-type]

    async def completed(self, request_id: str = "r1", **kwargs: Any) -> RequestRecord:
        record = (await self.accept(request_id, **kwargs)).record
        record = await self.store.start(record)
        return await self.store.complete(record, TURN)

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
                    "INSERT INTO xiaowei_session (session_id, owner_kind, owner_id, channel,"
                    " profile_fingerprint, created_at, expires_at, turns, state)"
                    " VALUES (:s, 'personal', 'alice', 'web', 'fp', :c, :e, 1, 'active')"
                    " ON CONFLICT DO NOTHING"
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

    async def slow_writes(self, when: str, events: str = "UPDATE") -> None:
        """让 xiaowei_request 的匹配写入在数据库内等待，用于在关键事务中途取消。"""
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE FUNCTION xw_slow() RETURNS trigger LANGUAGE plpgsql AS"
                    " $$ BEGIN PERFORM pg_sleep(3); RETURN NEW; END $$"
                )
            )
            await conn.execute(
                text(
                    f"CREATE TRIGGER xw_slow BEFORE {events} ON xiaowei_request"
                    f" FOR EACH ROW WHEN ({when}) EXECUTE FUNCTION xw_slow()"
                )
            )

    async def heal_slow(self) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER xw_slow ON xiaowei_request"))
            await conn.execute(text("DROP FUNCTION xw_slow()"))

    async def cancel_while_sleeping(self, operation: Awaitable[object]) -> None:
        """运行操作，待其写入在数据库内等待时取消；取消必须原样传播。"""
        task = asyncio.ensure_future(operation)
        async with self.engine.connect() as conn:
            for _ in range(200):
                sleeping = await conn.scalar(
                    text("SELECT count(*) FROM pg_stat_activity WHERE wait_event = 'PgSleep'")
                )
                if sleeping or task.done():
                    break
                await asyncio.sleep(0.02)
        assert not task.done(), "写入没有进入等待"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


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

    done = await env.store.complete(running, TURN)
    replay = await env.accept()
    assert not replay.created and replay.record.state == "completed"
    assert replay.record.answer == ANSWER and done.answer == ANSWER
    assert replay.record.context_evidence == done.context_evidence == ("ev_1", "ev_2")

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
    await env.settle(failed, "failed")
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
        await small.complete(record, TURN)
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
        (won,) = [c for c in claims if c is not None]
        assert not await env.store.claim_send(record, resend=True)  # sending 不能重发
        await second.finish_send(won, "unknown")  # 落定只认尝试，不认取得它的存储对象

        # 重投遇到 unknown/failed：零发送。显式重发只有一个进程取得。
        assert not await env.store.claim_send(record)
        claims = await asyncio.gather(
            *(s.claim_send(record, resend=True) for s in (env.store, second) * 2)
        )
        (won,) = [c for c in claims if c is not None]
        await second.finish_send(won, "failed")
        assert not await env.store.claim_send(record)
        await env.settle(record, "sent", resend=True)
        assert not await env.store.claim_send(record)
        assert not await env.store.claim_send(record, resend=True)
        assert (await env.row(record))["delivery"] == "sent"


async def test_resend_needs_a_completed_result_of_the_same_owner(env: Env) -> None:
    pending = await env.completed()
    assert not await env.store.claim_send(pending, resend=True)  # pending 留给首次发送

    failed = (await env.accept("r2")).record
    await env.store.fail(failed, "busy")
    # 固定回执走首次发送条件；它不是可重发的结果。
    await env.settle(failed, "failed")
    assert not await env.store.claim_send(failed, resend=True)

    done = await env.completed("r3")
    await env.settle(done, "failed")
    stranger = RequestRecord(**{**done.__dict__, "subject_id": "bob"})
    assert not await env.store.claim_send(stranger, resend=True)


async def test_finish_send_requires_the_sending_state(env: Env) -> None:
    record = await env.completed()
    with pytest.raises(ChannelStoreUnavailableError):
        await env.store.finish_send(DeliveryClaim(record, "never-claimed"), "sent")
    assert not env.readiness.ok


async def test_finish_send_only_settles_its_own_attempt(env: Env) -> None:
    """尝试标识不可复用：已落定的尝试、伪造的尝试都不能改写当前尝试。"""
    record = await env.completed()
    first = await env.store.claim_send(record)
    assert first is not None
    await env.store.finish_send(first, "unknown")
    current = await env.store.claim_send(record, resend=True)
    assert current is not None and current.attempt != first.attempt
    for stale in (first, DeliveryClaim(record, "forged")):
        with pytest.raises(ChannelStoreUnavailableError):
            await env.store.finish_send(stale, "failed")
    row = await env.row(record)
    assert (row["delivery"], row["delivery_attempt"]) == ("sending", current.attempt)
    await env.store.finish_send(current, "sent")
    row = await env.row(record)
    assert (row["delivery"], row["delivery_attempt"]) == ("sent", None)


@pytest.mark.parametrize("outcome", ["sent", "failed", "unknown"])
async def test_a_single_send_settles_each_outcome(env: Env, outcome: str) -> None:
    record = await env.completed()
    await env.settle(record, outcome)
    row = await env.row(record)
    assert (row["delivery"], row["delivery_attempt"], row["delivery_owner_pid"]) == (
        outcome,
        None,
        None,
    )
    assert env.readiness.ok


# ---- 双存储与关键状态失败 ------------------------------------------------------------------


async def test_result_save_failure_closes_the_session(env: Env) -> None:
    record = await env.store.start((await env.accept()).record)
    await env.register_session(record.session_id)
    await env.fail_writes("xiaowei_request", "NEW.state = 'completed'", "UPDATE")
    with pytest.raises(ResultNotSavedError):
        await env.store.complete(record, TURN)
    row = await env.row(record)
    assert (row["state"], row["failure_code"]) == ("failed", "result_not_saved")
    assert await env.session_state(record.session_id) == "closed"
    assert env.readiness.ok


async def test_unwritable_failure_state_locks_readiness(env: Env) -> None:
    record = await env.store.start((await env.accept()).record)
    await env.register_session(record.session_id)
    await env.fail_writes("xiaowei_request")
    with pytest.raises(ResultNotSavedError):
        await env.store.complete(record, TURN)
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
    claim = await env.store.claim_send(record)
    assert claim is not None
    await env.fail_writes("xiaowei_request")
    with pytest.raises(ChannelStoreUnavailableError):
        await env.store.finish_send(claim, "sent")
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
        # 恢复后的投递在持锁期间进行（与 serve 相同）；绑定的锁释放后存储不再开始新工作。
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


class _SendAbortedError(Exception):
    pass


@pytest.mark.parametrize("exit_", ["return", "raise", "cancel"])
async def test_a_released_resend_owner_no_longer_shields_its_sending(
    postgres_url: URL, exit_: str
) -> None:
    """显式重发持有所有者连接时，启动恢复不动它的 sending；所有者以任何方式退出后（正常、
    异常、取消），即使引擎仍打开，其后端身份也已消失，随后一次恢复把遗留 sending 记为 unknown。"""
    async with environment(postgres_url) as env:
        record = await env.completed()
        await env.settle(record, "failed")
        owners: list[Backend] = []
        claimed, leave = asyncio.Event(), asyncio.Event()

        async def resend() -> None:
            # 模拟发送后落定未完成：取得投递权后就退出，记录仍是 sending。
            async with hold_backend(env.engine) as sender:
                owners.append(sender)
                store = make_store(env.engine, env.clock, sender=sender)
                assert await store.claim_send(record, resend=True)
                claimed.set()
                await leave.wait()
                if exit_ == "raise":
                    raise _SendAbortedError

        task = asyncio.create_task(resend())
        await asyncio.wait_for(claimed.wait(), 10)

        async def recover() -> int:
            async with environment(postgres_url) as other:
                async with hold_instance_lock(other.engine, other.readiness) as lock:
                    return (await other.store.recover(lock)).unknown

        assert await recover() == 0  # 所有者仍在：不是遗留发送
        assert (await env.row(record))["delivery"] == "sending"

        if exit_ == "cancel":
            task.cancel()
        else:
            leave.set()
        if exit_ == "return":
            await task
        else:
            with pytest.raises(_SendAbortedError if exit_ == "raise" else asyncio.CancelledError):
                await task

        (owner,) = owners
        async with env.engine.connect() as conn:
            alive = await conn.scalar(
                text("SELECT count(*) FROM pg_stat_activity WHERE pid = :p AND backend_start = :s"),
                {"p": owner.pid, "s": owner.started},
            )
        assert alive == 0
        assert await recover() == 1
        row = await env.row(record)
        assert (row["delivery"], row["delivery_attempt"]) == ("unknown", None)
        assert (row["delivery_owner_pid"], row["delivery_owner_started"]) == (None, None)


async def test_an_owner_still_visible_after_discard_is_not_reported_released(env: Env) -> None:
    """丢弃连接后若该身份仍在 ``pg_stat_activity``（强制断开后后端尚未退出），期限内确认不了
    就不当作已释放。这里用另一条仍存活的连接充当该身份。"""
    async with env.engine.connect() as survivor:
        row = (
            await survivor.execute(
                text("SELECT pid, backend_start FROM pg_stat_activity WHERE pid = pg_backend_pid()")
            )
        ).one()
        await survivor.commit()
        discarded = await env.engine.connect()
        assert not await _release(env.engine, discarded, Backend(row.pid, row.backend_start))


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


# ---- 实例接管 ----------------------------------------------------------------------------

# 只终止持有会话级实例锁的后端（排他）；接收事务的共享屏障锁不受影响。
TERMINATE_INSTANCE_LOCK = text(
    "SELECT count(pg_terminate_backend(pid)) FROM pg_locks"
    " WHERE locktype = 'advisory' AND granted AND mode = 'ExclusiveLock'"
)
WAITING_ON_LOCK = text(
    "SELECT count(*) FROM pg_stat_activity"
    " WHERE wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
)


async def waiting_on_locks(engine: AsyncEngine, count: int) -> bool:
    """在期限内至少有 ``count`` 个后端在数据库锁上等待。"""
    async with engine.connect() as conn:
        for _ in range(100):
            if await conn.scalar(WAITING_ON_LOCK) >= count:
                return True
            await conn.commit()
            await asyncio.sleep(0.02)
    return False


async def test_takeover_recovery_waits_for_an_accept_still_committing(postgres_url: URL) -> None:
    """旧实例的接收事务已通过检查、尚未提交时失去锁：新实例的恢复必须等它提交后再读取，
    请求不能停在 accepted；重复请求得到已有终态。"""
    async with environment(postgres_url) as old, environment(postgres_url) as new:
        async with hold_instance_lock(old.engine, old.readiness) as old_lock:
            await old.store.recover(old_lock)
            await old.accept("r0")  # 建立 current 映射
            # 暂停点：行锁挡住接收事务读取 current 映射（FOR SHARE），此时它已开始、未提交。
            blocker = await new.engine.connect()
            await blocker.begin()
            await blocker.execute(text("SELECT 1 FROM xiaowei_channel_session FOR UPDATE"))
            pending = asyncio.create_task(old.accept("r1", "接管时尚未提交"))
            assert await waiting_on_locks(new.engine, 1)

            async with new.engine.begin() as conn:
                assert await conn.scalar(TERMINATE_INSTANCE_LOCK) == 1
            await asyncio.wait_for(old_lock.lost.wait(), 2)

            async with hold_instance_lock(new.engine, new.readiness) as new_lock:
                recovery = asyncio.create_task(new.store.recover(new_lock))
                await waiting_on_locks(new.engine, 2)  # 恢复在接收屏障上等待
                await blocker.rollback()
                await blocker.close()
                late = await asyncio.wait_for(pending, 10)
                report = await asyncio.wait_for(recovery, 10)
                assert late.created and report.interrupted == 2
                async with new.engine.connect() as conn:
                    states = await conn.scalar(
                        text("SELECT array_agg(DISTINCT state) FROM xiaowei_request")
                    )
                assert states == ["interrupted"]
                again = await new.accept("r1", "接管时尚未提交")
                assert not again.created and again.record.state == "interrupted"
                with pytest.raises(NotReadyError):
                    await old.store.start(late.record)


async def test_accept_after_the_lock_moved_is_refused_without_the_notice(
    postgres_url: URL,
) -> None:
    """终止通知被错过（锁的 readiness 与存储的不同）：数据库内的持有者核对仍拒绝写入新请求。"""
    async with environment(postgres_url) as old, environment(postgres_url) as new:
        async with hold_instance_lock(old.engine, Readiness()) as old_lock:
            await old.store.recover(old_lock)
            assert (await old.accept("r0")).created  # 对照：持锁时正常接收
            async with new.engine.begin() as conn:
                assert await conn.scalar(TERMINATE_INSTANCE_LOCK) == 1
            async with hold_instance_lock(new.engine, new.readiness) as new_lock:
                await new.store.recover(new_lock)
                assert old.readiness.ok
                with pytest.raises(NotReadyError):
                    await old.accept("r1")
                assert old.readiness.reason == "instance_lock_lost"
                async with new.engine.connect() as conn:
                    assert await conn.scalar(text("SELECT count(*) FROM xiaowei_request")) == 1
                assert (await new.accept("r1")).created  # 新实例照常接收


@asynccontextmanager
async def missed_notice(old: Env) -> AsyncIterator[None]:
    """旧实例持锁并完成恢复（绑定实例锁）；锁的 readiness 与存储的分开，模拟终止通知被错过。"""
    async with hold_instance_lock(old.engine, Readiness()) as lock:
        await old.store.recover(lock)
        yield


@asynccontextmanager
async def taken_over(new: Env) -> AsyncIterator[None]:
    """终止旧实例的锁连接，新实例取得锁并完成接管恢复。"""
    async with new.engine.begin() as conn:
        assert await conn.scalar(TERMINATE_INSTANCE_LOCK) == 1
    async with hold_instance_lock(new.engine, new.readiness) as lock:
        await new.store.recover(lock)
        yield


async def session_rows(env: Env) -> list[tuple[str, int, str]]:
    async with env.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT session_id, generation, state FROM xiaowei_channel_session"
                " ORDER BY generation"
            )
        )
        return [tuple(row) for row in rows]


async def test_rotation_after_takeover_is_refused_without_the_notice(postgres_url: URL) -> None:
    async with environment(postgres_url) as old, environment(postgres_url) as new:
        async with missed_notice(old):
            first = await old.store.current_session("web", "alice", "cookie-1")
            async with taken_over(new):
                assert old.readiness.ok
                with pytest.raises(NotReadyError):
                    await old.store.new_session("web", "alice", "cookie-1")
                assert old.readiness.reason == "instance_lock_lost"
                assert await session_rows(new) == [(first.session_id, 1, "current")]
                rotated = await new.store.new_session("web", "alice", "cookie-1")
                assert rotated.generation == 2  # 新实例照常轮换


async def test_first_session_after_takeover_is_not_created(postgres_url: URL) -> None:
    async with environment(postgres_url) as old, environment(postgres_url) as new:
        async with missed_notice(old), taken_over(new):
            with pytest.raises(NotReadyError):
                await old.store.current_session("feishu", "alice", "chat-1")
            assert old.readiness.reason == "instance_lock_lost"
            assert await session_rows(new) == []
            assert (await new.store.current_session("feishu", "alice", "chat-1")).generation == 1


async def test_duplicate_after_takeover_is_not_returned(postgres_url: URL) -> None:
    """接管后旧实例收到重复事件：不返回记录（不去重投、不运行）；新实例按去重与投递契约处理一次。"""
    async with environment(postgres_url) as old, environment(postgres_url) as new:
        async with missed_notice(old):
            record = (await old.accept("r1")).record
            async with taken_over(new):
                assert old.readiness.ok
                with pytest.raises(NotReadyError):
                    await old.accept("r1")
                assert old.readiness.reason == "instance_lock_lost"
                row = await new.row(record)
                assert (row["state"], row["delivery"]) == ("interrupted", "pending")
                again = await new.accept("r1")
                assert not again.created and again.record.state == "interrupted"
                assert await new.store.claim_send(again.record)
                assert not await new.store.claim_send(again.record)


async def test_delivery_claim_after_takeover_is_refused(postgres_url: URL) -> None:
    """旧实例在接管后取得投递权：在数据库内拒绝，delivery 不变，新实例仍能取得一次。"""
    async with environment(postgres_url) as old, environment(postgres_url) as new:
        async with missed_notice(old):
            record = (await old.accept("r1")).record
            async with taken_over(new):
                assert old.readiness.ok
                with pytest.raises(NotReadyError):
                    await old.store.claim_send(record)
                assert old.readiness.reason == "instance_lock_lost"
                assert (await new.row(record))["delivery"] == "pending"
                assert await new.store.claim_send(record)


async def test_a_bound_store_still_creates_rotates_accepts_and_delivers(postgres_url: URL) -> None:
    """成功对照：持锁并已恢复的实例照常创建第一代、轮换、接收与投递；并发轮换只留一个 current。"""
    async with (
        environment(postgres_url) as env,
        open_engine(secret(postgres_url)) as other,
        hold_instance_lock(env.engine, env.readiness) as lock,
    ):
        second = make_store(other, env.clock, env.readiness)
        await env.store.recover(lock)
        await second.recover(lock)
        assert (await env.store.current_session("web", "alice", "cookie-1")).generation == 1
        results = await asyncio.gather(
            env.store.new_session("web", "alice", "cookie-1"),
            second.new_session("web", "alice", "cookie-1"),
            return_exceptions=True,
        )
        assert any(not isinstance(r, BaseException) for r in results)
        assert [state for *_, state in await session_rows(env)].count("current") == 1
        done = await env.completed("r1")
        assert not (await env.accept("r1")).created
        assert await env.store.claim_send(done)
        assert env.readiness.ok


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
    await env.store.complete(record, TURN)

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
    await env.settle(record, "failed")
    await env.store.new_session("web", "alice", "cookie-1")
    env.clock.advance(60)

    claimed, report = await asyncio.gather(
        env.store.claim_send(record, resend=True),
        cleanup_expired(env.engine, now=env.clock(), batch_size=10),
    )
    assert claimed is not None and report.sessions == 0
    assert (await env.row(record))["delivery"] == "sending"
    sessions, requests, _, _ = await _counts(env, old.session_id)
    assert (sessions, requests) == (1, 1)

    # 请求过期并结束投递后，下一次清理完成。
    await env.store.finish_send(claimed, "sent")
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


# ---- 复审回归：readiness 门、取消、失败码闭集与无元数据会话清理 ---------------------------


async def test_not_ready_refuses_to_create_a_mapping(env: Env) -> None:
    existing = await env.store.current_session("web", "alice", "cookie-1")
    env.readiness.lock("test")
    # 已有映射仍可读取；不存在的映射不再创建，也没有写入。
    assert await env.store.current_session("web", "alice", "cookie-1") == existing
    with pytest.raises(NotReadyError):
        await env.store.current_session("web", "alice", "cookie-2")
    async with env.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM xiaowei_channel_session")) == 1


async def _assert_recovered_consistently(postgres_url: URL) -> None:
    """模拟停止并重启：持锁恢复后不再有未结束请求，被中断请求的会话均已关闭。"""
    async with environment(postgres_url) as env:
        async with hold_instance_lock(env.engine, env.readiness) as lock:
            await env.store.recover(lock)
        assert env.readiness.ok
        async with env.engine.connect() as conn:
            open_requests = await conn.scalar(
                text("SELECT count(*) FROM xiaowei_request WHERE state IN ('accepted', 'running')")
            )
            unclosed = await conn.scalar(
                text(
                    "SELECT count(*) FROM xiaowei_request r JOIN xiaowei_session s"
                    " USING (session_id) WHERE r.state = 'interrupted' AND s.state <> 'closed'"
                )
            )
        assert (open_requests, unclosed) == (0, 0)


async def _prepare_accept(env: Env) -> Callable[[], Awaitable[object]]:
    await env.slow_writes("true", "INSERT")
    return lambda: env.accept()


async def _prepare_start(env: Env) -> Callable[[], Awaitable[object]]:
    record = (await env.accept()).record
    await env.slow_writes("NEW.state = 'running'")
    return lambda: env.store.start(record)


async def _prepare_complete(env: Env) -> Callable[[], Awaitable[object]]:
    record = await env.store.start((await env.accept()).record)
    await env.register_session(record.session_id)
    await env.slow_writes("NEW.state = 'completed'")
    return lambda: env.store.complete(record, TURN)


async def _prepare_fail_after_commit(env: Env) -> Callable[[], Awaitable[object]]:
    small = make_store(env.engine, env.clock, env.readiness, max_answer_bytes=50)
    record = await small.start((await env.accept(store=small)).record)
    await env.register_session(record.session_id)
    await env.slow_writes("NEW.state = 'failed'")
    return lambda: small.complete(record, TURN)  # 回答超限，转入失败写入


@pytest.mark.parametrize(
    "prepare",
    [_prepare_accept, _prepare_start, _prepare_complete, _prepare_fail_after_commit],
    ids=["accept", "update", "complete", "fail_after_commit"],
)
async def test_cancelled_critical_writes_lock_readiness(
    postgres_url: URL, prepare: Callable[[Env], Awaitable[Callable[[], Awaitable[object]]]]
) -> None:
    async with environment(postgres_url) as env:
        operation = await prepare(env)
        await env.cancel_while_sleeping(operation())
        assert not env.readiness.ok
        with pytest.raises(NotReadyError):
            await env.accept("r-next", conversation="other")
        await env.heal_slow()
    await _assert_recovered_consistently(postgres_url)


async def test_cancelled_recovery_locks_readiness(postgres_url: URL) -> None:
    async with environment(postgres_url) as env:
        running = await env.store.start((await env.accept()).record)
        await env.register_session(running.session_id)
        await env.slow_writes("NEW.state = 'interrupted'")
        async with hold_instance_lock(env.engine, env.readiness) as lock:
            await env.cancel_while_sleeping(env.store.recover(lock))
        assert not env.readiness.ok
        await env.heal_slow()
        assert (await env.row(running))["state"] == "running"  # 整体回滚
    await _assert_recovered_consistently(postgres_url)


@pytest.mark.parametrize("code", ["busy", "model_failed", "evidence_failed", "session_failed"])
async def test_allowed_failure_codes_round_trip(env: Env, code: CallerFailureCode) -> None:
    record = (await env.accept()).record
    await env.store.fail(record, code)
    assert (await env.store.get("web", "alice", "cookie-1", "r1")).failure_code == code
    assert env.readiness.ok


@pytest.mark.parametrize(
    "code",
    [
        "interrupted",  # 只属于启动恢复
        "result_not_saved",  # 只属于结果保存失败路径，须与关闭 Session 同一事务
        "connection to db.internal:5432 failed for user admin",
        "busy " * 1000,
        "",
    ],
    ids=["interrupted", "result-not-saved", "exception-canary", "long", "empty"],
)
async def test_internal_and_unknown_failure_codes_are_never_written(env: Env, code: str) -> None:
    record = await env.store.start((await env.accept()).record)
    await env.register_session(record.session_id)
    with pytest.raises(ValueError):
        await env.store.fail(record, code)  # type: ignore[arg-type]
    # 不落库，也不会出现“failed/result_not_saved + active Session”的矛盾状态。
    row = await env.row(record)
    assert (row["state"], row["failure_code"]) == ("running", None)
    assert await env.session_state(record.session_id) == "active"
    # 失败状态无法保存：按关键状态失败锁低 readiness，留给重启恢复标为 interrupted。
    assert not env.readiness.ok


async def test_failure_codes_are_closed_in_the_database_and_on_read(env: Env) -> None:
    record = (await env.accept()).record
    await env.store.fail(record, "busy")
    for code in ("db.internal:5432 admin", "interrupted"):
        with pytest.raises(Exception, match="check"):
            async with env.engine.begin() as conn:
                await conn.execute(
                    text("UPDATE xiaowei_request SET failure_code = :c WHERE turn_id = :t"),
                    {"c": code, "t": record.turn_id},
                )
    # 去掉约束后写入的未知值在读取时被拒绝，不回传给调用方。
    async with env.engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE xiaowei_request DROP CONSTRAINT xiaowei_request_failure_code_check")
        )
        await conn.execute(
            text("UPDATE xiaowei_request SET failure_code = 'leak' WHERE turn_id = :t"),
            {"t": record.turn_id},
        )
    with pytest.raises(RequestUnavailableError):
        await env.store.get("web", "alice", "cookie-1", "r1")


async def _unregistered_counts(env: Env, session_id: str) -> tuple[int, int]:
    """渠道映射与请求的条数（这些会话没有元数据，也没有 SDK 历史）。"""
    sessions, requests, mappings, history = await _counts(env, session_id)
    assert (sessions, history) == (0, 0)
    return mappings, requests


async def test_cleanup_removes_retired_sessions_that_never_ran(postgres_url: URL) -> None:
    async with environment(postgres_url) as env:
        # 空会话：创建映射后、首次运行前新建会话。
        empty = await env.store.current_session("web", "alice", "c-empty")
        await env.store.new_session("web", "alice", "c-empty")
        # Agent 运行前失败（如繁忙）。
        busy = (await env.accept("r1", conversation="c-busy")).record
        await env.store.fail(busy, "busy")
        await env.store.new_session("web", "alice", "c-busy")
        # 接受后进程退出：恢复后的 interrupted。
        stranded = (await env.accept("r2", conversation="c-int")).record
    async with environment(postgres_url) as env:
        # 恢复后的写入在持锁期间进行（与 serve 相同）；绑定的锁释放后存储不再开始新工作。
        async with hold_instance_lock(env.engine, env.readiness) as lock:
            await env.store.recover(lock)
            await env.store.new_session("web", "alice", "c-int")
            current = await env.store.current_session("web", "alice", "c-int")

        env.clock.advance(SESSION_RETENTION)
        report = await cleanup_expired(env.engine, now=env.clock(), batch_size=10)
        # 恢复为被中断的会话写入已关闭的占位元数据，所以它按有元数据的会话清理；三者最终都被删除。
        assert (report.sessions, report.unregistered) == (1, 2)
        for session_id in (empty.session_id, busy.session_id, stranded.session_id):
            assert await _unregistered_counts(env, session_id) == (0, 0)
        # 当前会话即使过期也保留。
        assert await _unregistered_counts(env, current.session_id) == (1, 0)


async def test_unregistered_cleanup_keeps_active_and_unexpired_state(env: Env) -> None:
    # 发送中的请求：会话已退役且过期，仍保留。
    sending = await env.completed("r1", conversation="c-send")
    assert await env.store.claim_send(sending)
    await env.store.new_session("web", "alice", "c-send")
    # 运行中的请求（构造：退役会话上不应出现，但清理不能依赖这一点）。
    running = (await env.accept("r2", conversation="c-run")).record
    await env.store.fail(running, "busy")
    await env.store.new_session("web", "alice", "c-run")
    async with env.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE xiaowei_request SET state = 'running', failure_code = NULL"
                " WHERE turn_id = :t"
            ),
            {"t": running.turn_id},
        )
    # 映射过期而请求未过期：已结束、不在发送中的请求，以及与显式重发竞争的请求。
    late = await env.store.current_session("web", "alice", "c-late")
    unexpired = await env.store.current_session("web", "alice", "c-unexpired")
    env.clock.advance(SESSION_RETENTION - 60)
    await env.store.fail((await env.accept("r4", conversation="c-unexpired")).record, "busy")
    await env.store.new_session("web", "alice", "c-unexpired")
    resend = await env.completed("r3", conversation="c-late")
    assert resend.session_id == late.session_id
    await env.settle(resend, "failed")
    await env.store.new_session("web", "alice", "c-late")
    env.clock.advance(60)

    claimed, report = await asyncio.gather(
        env.store.claim_send(resend, resend=True),
        cleanup_expired(env.engine, now=env.clock(), batch_size=10),
    )
    assert claimed is not None and report.unregistered == 0
    assert await _unregistered_counts(env, sending.session_id) == (1, 1)
    assert await _unregistered_counts(env, running.session_id) == (1, 1)
    assert await _unregistered_counts(env, late.session_id) == (1, 1)
    assert await _unregistered_counts(env, unexpired.session_id) == (1, 1)

    # 请求过期并结束投递后，下一次清理完成。
    await env.store.finish_send(claimed, "sent")
    env.clock.advance(REQUEST_RETENTION)
    assert (await cleanup_expired(env.engine, now=env.clock(), batch_size=10)).unregistered == 2
    assert await _unregistered_counts(env, late.session_id) == (0, 0)
    assert await _unregistered_counts(env, unexpired.session_id) == (0, 0)
