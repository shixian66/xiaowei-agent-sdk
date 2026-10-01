"""渠道会话映射与请求结果的 PostgreSQL 状态机：去重、可重发回答、投递状态与中断恢复。

不运行 Agent、不发送消息，也不保存原消息、原始查询结果、原始异常或凭据。请求编号与会话语境
只以带服务端密钥的摘要保存：``request_key`` 绑定渠道、可信 owner、会话语境与原始请求编号，
不同 owner 的同名编号互不冲突；``message_digest`` 另绑定用途、数据策略版本与正文，用于判断
同一编号是否真是同一请求。

状态迁移都是带前置状态条件的单条更新，影响 0 行即表示被其他进程取得或状态已变化：

- 请求：``accepted → running → completed``；``accepted/running → failed``；启动恢复把
  ``accepted/running`` 标为 ``interrupted`` 并关闭关联 Session。
- 投递：首次发送与事件重投只能竞争 ``pending → sending``；显式重发只对 completed 结果竞争
  ``failed/unknown → sending``；发送结束只能从 ``sending`` 改为 ``sent/failed/unknown``；启动恢复
  把遗留 ``sending`` 改为 ``unknown``。任何路径都不自动再次发送。

关键状态无法持久化时锁低进程 readiness：此后拒绝新请求、新会话（含首次创建映射）与新发送，
等待停止并重启后由持有实例锁的启动恢复处理。结果保存失败时先尝试把请求标为 failed 并关闭
Session，这也失败才锁低 readiness。关键写入期间被取消时提交结果不明：锁低 readiness 后原样
传播取消，同样交给重启恢复。失败码只能取 ``FailureCode`` 闭集，写入前与读取时都核对。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal, get_args

from pydantic import SecretStr, ValidationError
from sqlalchemy import TextClause, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from xiaowei.app import Mode
from xiaowei.models import AgentAnswer, Channel
from xiaowei.session import close_sessions
from xiaowei.storage import InstanceLock, Readiness

RequestState = Literal["accepted", "running", "completed", "failed", "interrupted"]
DeliveryState = Literal["pending", "sending", "sent", "failed", "unknown"]
SendOutcome = Literal["sent", "failed", "unknown"]
# 安全失败码闭集（与迁移 002 的 CHECK 一致）：只表达失败类别，不携带异常文字。``fail()`` 可写入
# 前五个；``interrupted`` 只由启动恢复写入。
FailureCode = Literal[
    "busy", "model_failed", "evidence_failed", "session_failed", "result_not_saved", "interrupted"
]

RESULT_NOT_SAVED: FailureCode = "result_not_saved"
INTERRUPTED: FailureCode = "interrupted"
_FAILURE_CODES = frozenset(get_args(FailureCode))
_SESSION_ID_BYTES = 24

_SELECT_CURRENT = text(
    """
    SELECT session_id, generation, created_at, expires_at FROM xiaowei_channel_session
    WHERE channel = :channel AND subject_id = :subject_id AND conversation_key = :conversation_key
      AND state = 'current'
    """
)
# 新建会话以 FOR UPDATE、接受请求以 FOR SHARE 读取 current 映射：二者互斥，新请求不会绑定到
# 正被退役的会话。
_SELECT_CURRENT_FOR_UPDATE = text(
    """
    SELECT session_id, generation, created_at, expires_at FROM xiaowei_channel_session
    WHERE channel = :channel AND subject_id = :subject_id AND conversation_key = :conversation_key
      AND state = 'current'
    FOR UPDATE
    """
)
_SELECT_CURRENT_FOR_SHARE = text(
    """
    SELECT session_id, generation, created_at, expires_at FROM xiaowei_channel_session
    WHERE channel = :channel AND subject_id = :subject_id AND conversation_key = :conversation_key
      AND state = 'current'
    FOR SHARE
    """
)
_INSERT_CURRENT = text(
    """
    INSERT INTO xiaowei_channel_session (
        channel, subject_id, conversation_key, generation, session_id, state, created_at,
        expires_at
    ) VALUES (
        :channel, :subject_id, :conversation_key, :generation, :session_id, 'current', :now,
        :expires_at
    )
    ON CONFLICT DO NOTHING
    """
)
_RETIRE_CURRENT = text(
    """
    UPDATE xiaowei_channel_session SET state = 'retired'
    WHERE channel = :channel AND subject_id = :subject_id AND conversation_key = :conversation_key
      AND state = 'current' AND session_id = :session_id
      AND NOT EXISTS (
          SELECT 1 FROM xiaowei_request r
          WHERE r.session_id = :session_id AND r.state IN ('accepted', 'running')
      )
    """
)
_INSERT_REQUEST = text(
    """
    INSERT INTO xiaowei_request (
        channel, request_key, subject_id, conversation_key, session_id, turn_id, mode,
        message_digest, state, delivery, created_at, updated_at, expires_at
    ) VALUES (
        :channel, :request_key, :subject_id, :conversation_key, :session_id, :turn_id, :mode,
        :message_digest, 'accepted', 'pending', :now, :now, :expires_at
    )
    ON CONFLICT (channel, request_key) DO NOTHING
    """
)
_SELECT_REQUEST = text(
    """
    SELECT channel, request_key, subject_id, conversation_key, session_id, turn_id, mode,
           message_digest, state, answer, failure_code, delivery, created_at, expires_at
    FROM xiaowei_request WHERE channel = :channel AND request_key = :request_key
    """
)
# 所有请求更新都带归属（渠道、请求键、owner、轮次）与前置状态条件。
_START = text(
    """
    UPDATE xiaowei_request SET state = 'running', updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND state = 'accepted'
    """
)
_COMPLETE = text(
    """
    UPDATE xiaowei_request SET state = 'completed', answer = :answer, updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND state = 'running'
    """
)
_FAIL = text(
    """
    UPDATE xiaowei_request SET state = 'failed', failure_code = :code, updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND state IN ('accepted', 'running')
    """
)
# 首次发送：completed 结果或 failed/interrupted 的固定回执，各最多一次。
_CLAIM_FIRST = text(
    """
    UPDATE xiaowei_request SET delivery = 'sending', updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND delivery = 'pending' AND expires_at > :now
      AND state IN ('completed', 'failed', 'interrupted')
    """
)
_CLAIM_RESEND = text(
    """
    UPDATE xiaowei_request SET delivery = 'sending', updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND delivery IN ('failed', 'unknown') AND expires_at > :now AND state = 'completed'
    """
)
_FINISH_SEND = text(
    """
    UPDATE xiaowei_request SET delivery = :outcome, updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND delivery = 'sending'
    """
)
_RECOVER_RUNNING = text(
    """
    UPDATE xiaowei_request SET state = 'interrupted', failure_code = :code, updated_at = :now
    WHERE state IN ('accepted', 'running') RETURNING session_id
    """
)
_RECOVER_SENDING = text(
    "UPDATE xiaowei_request SET delivery = 'unknown', updated_at = :now WHERE delivery = 'sending'"
)


class ChannelStoreError(Exception):
    """请求与渠道状态边界错误；信息固定，不含请求内容、摘要或连接信息。"""


class ChannelStoreUnavailableError(ChannelStoreError):
    """PostgreSQL 不可用或关键状态无法持久化；写入失败时进程 readiness 已锁低。"""

    def __init__(self, message: str = "请求状态存储不可用") -> None:
        super().__init__(message)


class NotReadyError(ChannelStoreError):
    def __init__(self) -> None:
        super().__init__("服务未就绪，暂不接收新请求")


class RequestConflictError(ChannelStoreError):
    """同一请求编号对应的内容或用途不同。"""

    def __init__(self) -> None:
        super().__init__("请求编号已用于其他内容，请使用新的请求编号")


class RequestUnavailableError(ChannelStoreError):
    """请求不存在、不属于当前身份、已过期或保存内容不符合契约；三者不作区分。"""

    def __init__(self) -> None:
        super().__init__("请求不存在或已过期")


class SessionBusyError(ChannelStoreError):
    def __init__(self) -> None:
        super().__init__("当前会话有正在处理的请求，请稍后再新建会话")


class ResultNotSavedError(ChannelStoreError):
    """结果没有保存，不得交付；会话已关闭（或 readiness 已锁低）。"""

    def __init__(self) -> None:
        super().__init__("结果保存失败，请新建会话后重试")


@dataclass(frozen=True)
class ChannelSession:
    channel: Channel
    subject_id: str
    conversation_key: str
    session_id: str
    generation: int
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class RequestRecord:
    channel: Channel
    request_key: str
    subject_id: str
    conversation_key: str
    session_id: str
    turn_id: str
    mode: Mode
    state: RequestState
    delivery: DeliveryState
    answer: AgentAnswer | None
    failure_code: FailureCode | None
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class Acceptance:
    record: RequestRecord
    created: bool


@dataclass(frozen=True)
class RecoveryReport:
    interrupted: int
    unknown: int


@dataclass(frozen=True)
class _Owner:
    """可信 owner 与会话语境摘要；``params`` 用作 SQL 绑定参数。"""

    channel: Channel
    subject_id: str
    conversation_key: str

    @property
    def params(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "subject_id": self.subject_id,
            "conversation_key": self.conversation_key,
        }


class ChannelStore:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        key: SecretStr,
        clock: Callable[[], datetime],
        readiness: Readiness,
        request_retention_seconds: int,
        session_retention_seconds: int,
        evidence_retention_seconds: int,
        max_answer_bytes: int,
    ) -> None:
        if min(request_retention_seconds, session_retention_seconds, max_answer_bytes) <= 0:
            raise ValueError("请求保留期、会话保留期与回答上限必须为正数")
        if request_retention_seconds > evidence_retention_seconds:
            # 保存的回答只引用证据；证据过期后结果无法重验，不能比证据保留更久。
            raise ValueError("可重发结果的保留期不得长于证据保留期")
        if not key.get_secret_value():
            raise ValueError("摘要密钥不能为空")
        self._engine = engine
        self._key = key.get_secret_value().encode()
        self._clock = clock
        self._readiness = readiness
        self._request_retention = timedelta(seconds=request_retention_seconds)
        self._session_retention = timedelta(seconds=session_retention_seconds)
        self._max_answer_bytes = max_answer_bytes

    # ---- 会话映射 --------------------------------------------------------------------

    async def current_session(
        self, channel: Channel, subject_id: str, conversation: str
    ) -> ChannelSession:
        """取得会话语境的 current 会话；不存在时创建第一代。客户端不能指定会话标识。

        readiness 锁低后仍可读取已有映射，但不再创建。
        """
        owner = self._owner(channel, subject_id, conversation)
        try:
            async with self._engine.begin() as conn:
                return await self._current(conn, owner)
        except (OSError, SQLAlchemyError):
            raise ChannelStoreUnavailableError from None

    async def new_session(
        self, channel: Channel, subject_id: str, conversation: str
    ) -> ChannelSession:
        """退役 current 会话并创建下一代；当前会话有未结束请求时拒绝。不复制历史或许可。"""
        self._require_ready()
        owner = self._owner(channel, subject_id, conversation)
        try:
            async with self._engine.begin() as conn:
                current = await self._current(conn, owner, statement=_SELECT_CURRENT_FOR_UPDATE)
                retired = await conn.execute(
                    _RETIRE_CURRENT, {**owner.params, "session_id": current.session_id}
                )
                if retired.rowcount != 1:
                    raise SessionBusyError
                created = await self._insert_current(conn, owner, current.generation + 1)
                if created is None:
                    raise SessionBusyError
                return created
        except (OSError, SQLAlchemyError):
            raise ChannelStoreUnavailableError from None

    async def _current(
        self,
        conn: AsyncConnection,
        owner: _Owner,
        *,
        statement: TextClause = _SELECT_CURRENT,
    ) -> ChannelSession:
        """读取 current 映射；不存在时创建第一代（须 readiness 正常），并发创建时重新读取。"""
        row = (await conn.execute(statement, owner.params)).mappings().one_or_none()
        if row is None:
            self._require_ready()
            created = await self._insert_current(conn, owner, 1)
            if created is not None:
                return created
            # 并发创建：另一个事务已插入，重新读取（加锁时等待其提交）。
            row = (await conn.execute(statement, owner.params)).mappings().one()
        return self._session(owner, dict(row))

    async def _insert_current(
        self, conn: AsyncConnection, owner: _Owner, generation: int
    ) -> ChannelSession | None:
        now = self._clock()
        values = {
            **owner.params,
            "generation": generation,
            "session_id": secrets.token_urlsafe(_SESSION_ID_BYTES),
            "now": now,
            "expires_at": now + self._session_retention,
        }
        if (await conn.execute(_INSERT_CURRENT, values)).rowcount != 1:
            return None
        return self._session(owner, {**values, "created_at": now})

    @staticmethod
    def _session(owner: _Owner, row: Mapping[str, Any]) -> ChannelSession:
        return ChannelSession(
            channel=owner.channel,
            subject_id=owner.subject_id,
            conversation_key=owner.conversation_key,
            session_id=row["session_id"],
            generation=row["generation"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    # ---- 请求 ------------------------------------------------------------------------

    async def accept(
        self,
        *,
        channel: Channel,
        subject_id: str,
        conversation: str,
        request_id: str,
        mode: Mode,
        message: str,
        policy_version: str,
    ) -> Acceptance:
        """持久接受请求，或返回同一请求已有的记录；同编号不同内容时拒绝冲突。

        新请求绑定当前会话；与新建会话互斥（共享锁读取 current 映射）。
        """
        self._require_ready()
        owner = self._owner(channel, subject_id, conversation)
        key = {"channel": channel, "request_key": self._request_key(owner, request_id)}
        digest = self._digest(
            "message", channel, subject_id, owner.conversation_key, mode, policy_version, message
        )
        now = self._clock()
        # 提交结果不明时请求可能已存在：锁低 readiness，交给重启恢复，不让它永久“处理中”。
        with self._critical("request_accept_failed", "请求状态存储不可用"):
            async with self._engine.begin() as conn:
                existing = await self._select(conn, key)
                created = False
                if existing is None:
                    session = await self._current(conn, owner, statement=_SELECT_CURRENT_FOR_SHARE)
                    inserted = await conn.execute(
                        _INSERT_REQUEST,
                        {
                            **owner.params,
                            **key,
                            "session_id": session.session_id,
                            "turn_id": secrets.token_urlsafe(_SESSION_ID_BYTES),
                            "mode": mode,
                            "message_digest": digest,
                            "now": now,
                            "expires_at": now + self._request_retention,
                        },
                    )
                    created = inserted.rowcount == 1
                    existing = await self._select(conn, key)
        if existing is None:
            raise ChannelStoreUnavailableError
        if (
            existing["subject_id"] != subject_id
            or existing["conversation_key"] != owner.conversation_key
            or existing["mode"] != mode
            or not hmac.compare_digest(existing["message_digest"], digest)
        ):
            raise RequestConflictError
        if existing["expires_at"] <= now:
            raise RequestUnavailableError
        return Acceptance(record=self._record(existing), created=created)

    async def get(
        self, channel: Channel, subject_id: str, conversation: str, request_id: str
    ) -> RequestRecord:
        """按当前身份读取请求；不存在、不属于该身份、已过期或内容不符合契约时统一拒绝。"""
        owner = self._owner(channel, subject_id, conversation)
        key = {"channel": channel, "request_key": self._request_key(owner, request_id)}
        try:
            async with self._engine.connect() as conn:
                row = await self._select(conn, key)
        except (OSError, SQLAlchemyError):
            raise ChannelStoreUnavailableError from None
        if row is None or row["subject_id"] != subject_id or row["expires_at"] <= self._clock():
            raise RequestUnavailableError
        return self._record(row)

    async def start(self, record: RequestRecord) -> RequestRecord:
        """``accepted → running``；只有一个进程成功，其余得到 ``ChannelStoreError``。"""
        self._require_ready()
        if await self._update(_START, record, critical="request_start_failed") != 1:
            raise RequestUnavailableError
        return replace(record, state="running")

    async def complete(self, record: RequestRecord, answer: AgentAnswer) -> RequestRecord:
        """``running → completed`` 并保存受限回答；失败时标为 failed 并关闭会话，不交付。"""
        content = answer.model_dump_json()
        if len(content.encode()) > self._max_answer_bytes:
            await self._fail_after_commit(record)
            raise ResultNotSavedError
        try:
            async with self._engine.begin() as conn:
                saved = await conn.execute(
                    _COMPLETE, {**_keys(record), "answer": content, "now": self._clock()}
                )
        except asyncio.CancelledError:
            # 保存结果不明，也不能在取消中再写失败状态：锁低后交给重启恢复。
            self._readiness.lock("result_state_unwritable")
            raise
        except (OSError, SQLAlchemyError):
            await self._fail_after_commit(record)
            raise ResultNotSavedError from None
        if saved.rowcount != 1:
            await self._fail_after_commit(record)
            raise ResultNotSavedError
        return replace(record, state="completed", answer=answer)

    async def fail(self, record: RequestRecord, code: FailureCode) -> RequestRecord:
        """``accepted/running → failed``，保存安全失败码（如 Session 未提交的模型失败、繁忙）。

        失败码不在闭集内（如误传异常文字）时不写入：失败状态无法保存，按关键状态失败锁低
        readiness，请求留待重启恢复标为 interrupted。
        """
        if code not in _FAILURE_CODES or code == INTERRUPTED:
            self._readiness.lock("request_fail_failed")
            raise ValueError("失败码不在允许的范围内")
        params = {"code": code}
        if await self._update(_FAIL, record, critical="request_fail_failed", **params) != 1:
            raise RequestUnavailableError
        return replace(record, state="failed", failure_code=code)

    async def _fail_after_commit(self, record: RequestRecord) -> None:
        """Session 已提交而结果未保存：同一事务标记 failed 并关闭会话；也失败时锁低 readiness。"""
        params = {**_keys(record), "code": RESULT_NOT_SAVED, "now": self._clock()}
        try:
            async with self._engine.begin() as conn:
                if (await conn.execute(_FAIL, params)).rowcount != 1:
                    raise ChannelStoreUnavailableError
                await close_sessions(conn, [record.session_id])
        except asyncio.CancelledError:
            self._readiness.lock("result_state_unwritable")
            raise
        except (OSError, SQLAlchemyError, ChannelStoreUnavailableError):
            self._readiness.lock("result_state_unwritable")

    # ---- 投递 ------------------------------------------------------------------------

    async def claim_send(self, record: RequestRecord, *, resend: bool = False) -> bool:
        """紧邻发送前取得投递权；返回 False 表示不得发送。

        首次发送与事件重投只竞争 ``pending → sending``；``resend=True`` 只供显式重发命令使用，
        只对 completed 结果竞争 ``failed/unknown → sending``。
        """
        self._require_ready()
        statement = _CLAIM_RESEND if resend else _CLAIM_FIRST
        return await self._update(statement, record, critical="delivery_claim_failed") == 1

    async def finish_send(self, record: RequestRecord, outcome: SendOutcome) -> None:
        """``sending → sent/failed/unknown``；不在 sending 或无法写入时锁低 readiness。"""
        params = {"outcome": outcome}
        if (
            await self._update(_FINISH_SEND, record, critical="delivery_state_failed", **params)
            != 1
        ):
            self._readiness.lock("delivery_state_lost")
            raise ChannelStoreUnavailableError("投递状态无法保存")

    # ---- 启动恢复 --------------------------------------------------------------------

    async def recover(self, lock: InstanceLock) -> RecoveryReport:
        """持有实例锁的启动一致性恢复：一个事务内中断未完成请求并关闭其会话，遗留发送改为未知。

        不调用 Runner、不发送消息、不放回队列。失败整体回滚并锁低 readiness。
        """
        await lock.verify()
        now = self._clock()
        conn = lock.connection
        with self._critical("recovery_failed", "启动恢复失败"):
            async with conn.begin():
                rows = await conn.execute(_RECOVER_RUNNING, {"code": INTERRUPTED, "now": now})
                sessions = [r[0] for r in rows]
                await close_sessions(conn, sessions)
                unknown = (await conn.execute(_RECOVER_SENDING, {"now": now})).rowcount
        return RecoveryReport(interrupted=len(sessions), unknown=unknown)

    # ---- 内部 ------------------------------------------------------------------------

    def _require_ready(self) -> None:
        if not self._readiness.ok:
            raise NotReadyError

    @contextmanager
    def _critical(self, reason: str, message: str) -> Iterator[None]:
        """关键状态写入：存储失败时锁低 readiness 并转为固定错误；被取消时提交结果不明，
        锁低后原样传播取消，不吞掉。"""
        try:
            yield
        except asyncio.CancelledError:
            self._readiness.lock(reason)
            raise
        except (OSError, SQLAlchemyError):
            self._readiness.lock(reason)
            raise ChannelStoreUnavailableError(message) from None

    async def _update(
        self, statement: TextClause, record: RequestRecord, *, critical: str, **extra: object
    ) -> int:
        params = {**_keys(record), "now": self._clock(), **extra}
        with self._critical(critical, "请求状态无法保存"):
            async with self._engine.begin() as conn:
                return (await conn.execute(statement, params)).rowcount

    async def _select(
        self, conn: AsyncConnection, key: Mapping[str, str]
    ) -> Mapping[str, Any] | None:
        row = (await conn.execute(_SELECT_REQUEST, key)).mappings().one_or_none()
        return None if row is None else dict(row)

    def _record(self, row: Mapping[str, Any]) -> RequestRecord:
        answer = None
        if row["answer"] is not None:
            raw = row["answer"]
            if len(raw.encode()) > self._max_answer_bytes:
                raise RequestUnavailableError
            try:
                answer = AgentAnswer.model_validate_json(raw)
            except ValidationError:
                raise RequestUnavailableError from None
        if row["failure_code"] is not None and row["failure_code"] not in _FAILURE_CODES:
            raise RequestUnavailableError
        return RequestRecord(
            channel=row["channel"],
            request_key=row["request_key"],
            subject_id=row["subject_id"],
            conversation_key=row["conversation_key"],
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            mode=row["mode"],
            state=row["state"],
            delivery=row["delivery"],
            answer=answer,
            failure_code=row["failure_code"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def _owner(self, channel: Channel, subject_id: str, conversation: str) -> _Owner:
        if not subject_id or not conversation:
            raise ValueError("可信身份与会话语境不能为空")
        return _Owner(
            channel=channel,
            subject_id=subject_id,
            conversation_key=self._digest("conversation", channel, subject_id, conversation),
        )

    def _request_key(self, owner: _Owner, request_id: str) -> str:
        if not request_id:
            raise ValueError("请求编号不能为空")
        return self._digest(
            "request", owner.channel, owner.subject_id, owner.conversation_key, request_id
        )

    def _digest(self, purpose: str, *parts: str) -> str:
        """带服务端密钥的摘要；各部分按 JSON 数组编码，边界不会混淆。"""
        body = json.dumps([purpose, *parts], ensure_ascii=False, separators=(",", ":"))
        return hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()


def _keys(record: RequestRecord) -> dict[str, object]:
    return {
        "channel": record.channel,
        "request_key": record.request_key,
        "subject_id": record.subject_id,
        "turn_id": record.turn_id,
    }
