"""渠道会话映射与请求结果的 PostgreSQL 状态机：去重、可重发回答、投递状态与中断恢复。

不运行 Agent、不发送消息，也不保存原消息、原始查询结果、原始异常或凭据。completed 的 ``answer`` 列
保存 ``TurnAnswer`` JSON（回答与本轮模型可见的证据标识）；此前只保存 ``AgentAnswer`` 的行仍可读出
状态，但没有证据记录，交付方拒绝交付。请求编号与会话语境
只以带服务端密钥的摘要保存：``request_key`` 绑定渠道、可信 owner、会话语境与原始请求编号，
不同 owner 的同名编号互不冲突；``message_digest`` 另绑定用途、数据策略版本与正文，用于判断
同一编号是否真是同一请求。

状态迁移都是带前置状态条件的单条更新，影响 0 行即表示被其他进程取得或状态已变化：

- 请求：``accepted → running → completed``；``accepted/running → failed``；启动恢复把
  ``accepted/running`` 标为 ``interrupted`` 并持久关闭关联 Session（元数据尚未登记时写入已关闭
  的占位，旧进程之后的首次登记不能让它变成可回放）。新请求的接收与接管恢复经数据库屏障交接：
  恢复等待仍在提交的接收事务；恢复后，接收（含重复请求）、启动请求、创建或轮换会话与取得投递权
  都须在同一事务内确认实例锁仍属本进程。
- 投递：首次发送与事件重投只能竞争 ``pending → sending``；显式重发只对 completed 结果竞争
  ``failed/unknown → sending``；每次取得写入新的尝试标识，发送结束只能由同一尝试把 ``sending``
  改为 ``sent/failed/unknown``，旧尝试返回不能改写新尝试。启动恢复把没有存活所有者的遗留
  ``sending`` 改为 ``unknown`` 并作废其尝试（仍在进行的显式重发保持不动），把飞书 completed
  结果遗留的 ``pending`` 改为 ``failed``（回复目的地只在进程内存在，重启后再无首次发送触发）。
  任何路径都不自动再次发送。

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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal, get_args

from pydantic import SecretStr, ValidationError
from sqlalchemy import TextClause, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from xiaowei.app import Mode
from xiaowei.models import AgentAnswer, Channel, MonitoringFailure, Owner, TurnAnswer
from xiaowei.session import close_interrupted_sessions, close_sessions
from xiaowei.storage import Backend, InstanceLock, Readiness

RequestState = Literal["accepted", "running", "completed", "failed", "interrupted"]
DeliveryState = Literal["pending", "sending", "sent", "failed", "unknown"]
SendOutcome = Literal["sent", "failed", "unknown"]
# 安全失败码闭集（与迁移 002 的 CHECK 一致）：只表达失败类别，不携带异常文字。调用者经 ``fail()``
# 只能写入 ``CallerFailureCode``；两个内部码各有唯一写入路径：``result_not_saved`` 由结果保存
# 失败路径与关闭 Session 同一事务写入，``interrupted`` 由启动恢复写入。
CallerFailureCode = Literal[
    "busy",
    "model_failed",
    "evidence_failed",
    "session_failed",
    "scope_unverifiable",
    "access_denied",
]
InternalFailureCode = Literal["result_not_saved", "interrupted"]
FailureCode = CallerFailureCode | InternalFailureCode

RESULT_NOT_SAVED: InternalFailureCode = "result_not_saved"
INTERRUPTED: InternalFailureCode = "interrupted"
_CALLER_FAILURE_CODES = frozenset(get_args(CallerFailureCode))
_FAILURE_CODES = _CALLER_FAILURE_CODES | frozenset(get_args(InternalFailureCode))
_SESSION_ID_BYTES = 24
_ATTEMPT_BYTES = 24

_SELECT_CURRENT = text(
    """
    SELECT session_id, generation, created_at, expires_at FROM xiaowei_channel_session
    WHERE channel = :channel AND owner_kind = :owner_kind AND owner_id = :owner_id
      AND conversation_key = :conversation_key AND state = 'current'
    """
)
# 新建会话以 FOR UPDATE、接受请求以 FOR SHARE 读取 current 映射：二者互斥，新请求不会绑定到
# 正被退役的会话。
_SELECT_CURRENT_FOR_UPDATE = text(
    """
    SELECT session_id, generation, created_at, expires_at FROM xiaowei_channel_session
    WHERE channel = :channel AND owner_kind = :owner_kind AND owner_id = :owner_id
      AND conversation_key = :conversation_key AND state = 'current'
    FOR UPDATE
    """
)
_SELECT_CURRENT_FOR_SHARE = text(
    """
    SELECT session_id, generation, created_at, expires_at FROM xiaowei_channel_session
    WHERE channel = :channel AND owner_kind = :owner_kind AND owner_id = :owner_id
      AND conversation_key = :conversation_key AND state = 'current'
    FOR SHARE
    """
)
_INSERT_CURRENT = text(
    """
    INSERT INTO xiaowei_channel_session (
        channel, owner_kind, owner_id, conversation_key, generation, session_id, state,
        created_at, expires_at
    ) VALUES (
        :channel, :owner_kind, :owner_id, :conversation_key, :generation, :session_id, 'current',
        :now, :expires_at
    )
    ON CONFLICT DO NOTHING
    """
)
_RETIRE_CURRENT = text(
    """
    UPDATE xiaowei_channel_session SET state = 'retired'
    WHERE channel = :channel AND owner_kind = :owner_kind AND owner_id = :owner_id
      AND conversation_key = :conversation_key AND state = 'current' AND session_id = :session_id
      AND NOT EXISTS (
          SELECT 1 FROM xiaowei_request r
          WHERE r.session_id = :session_id AND r.state IN ('accepted', 'running')
      )
    """
)
_INSERT_REQUEST = text(
    """
    INSERT INTO xiaowei_request (
        channel, request_key, owner_kind, owner_id, subject_id, conversation_key, session_id,
        turn_id, mode, message_digest, state, delivery, reply_chat_id, reply_message_id,
        created_at, updated_at, expires_at
    ) VALUES (
        :channel, :request_key, :owner_kind, :owner_id, :subject_id, :conversation_key,
        :session_id, :turn_id, :mode, :message_digest, 'accepted', 'pending', :reply_chat_id,
        :reply_message_id, :now, :now, :expires_at
    )
    ON CONFLICT (channel, request_key) DO NOTHING
    """
)
_SELECT_REQUEST = text(
    """
    SELECT channel, request_key, owner_kind, owner_id, subject_id, conversation_key, session_id,
           turn_id, mode, message_digest, state, answer, failure_code, delivery, reply_chat_id,
           reply_message_id, created_at, expires_at
    FROM xiaowei_request WHERE channel = :channel AND request_key = :request_key
    """
)
_SELECT_REQUEST_FOR_UPDATE = text(
    """
    SELECT channel, request_key, owner_kind, owner_id, subject_id, conversation_key, session_id,
           turn_id, mode, message_digest, state, answer, failure_code, delivery, reply_chat_id,
           reply_message_id, created_at, expires_at
    FROM xiaowei_request WHERE channel = :channel AND request_key = :request_key
    FOR UPDATE
    """
)
# 所有请求更新都带归属（渠道、请求键、owner、发起人、轮次）与前置状态条件。
_START = text(
    """
    UPDATE xiaowei_request SET state = 'running', updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND owner_kind = :owner_kind
      AND owner_id = :owner_id AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND state = 'accepted'
    """
)
_COMPLETE = text(
    """
    UPDATE xiaowei_request SET state = 'completed', answer = :answer, updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND owner_kind = :owner_kind
      AND owner_id = :owner_id AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND state = 'running'
    """
)
_FAIL = text(
    """
    UPDATE xiaowei_request SET state = 'failed', failure_code = :code, updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND owner_kind = :owner_kind
      AND owner_id = :owner_id AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND state IN ('accepted', 'running')
    """
)
# 首次发送：completed 结果或 failed/interrupted 的固定回执，各最多一次。
# 取得投递权时写入新的尝试标识（与可选的重发命令连接身份）；落定只认同一标识。
_CLAIM_FIRST = text(
    """
    UPDATE xiaowei_request SET delivery = 'sending', delivery_attempt = :attempt,
        delivery_owner_pid = :owner_pid, delivery_owner_started = :owner_started, updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND owner_kind = :owner_kind
      AND owner_id = :owner_id AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND delivery = 'pending' AND expires_at > :now
      AND state IN ('completed', 'failed', 'interrupted')
    """
)
_CLAIM_RESEND = text(
    """
    UPDATE xiaowei_request SET delivery = 'sending', delivery_attempt = :attempt,
        delivery_owner_pid = :owner_pid, delivery_owner_started = :owner_started, updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND owner_kind = :owner_kind
      AND owner_id = :owner_id AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND delivery IN ('failed', 'unknown') AND expires_at > :now AND state = 'completed'
    """
)
_FINISH_SEND = text(
    """
    UPDATE xiaowei_request SET delivery = :outcome, delivery_attempt = NULL,
        delivery_owner_pid = NULL, delivery_owner_started = NULL, updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND owner_kind = :owner_kind
      AND owner_id = :owner_id AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND delivery = 'sending' AND delivery_attempt = :attempt
    """
)
# 返回中断前的状态：恢复据此区分只在排队、从未开始的群请求（见 ``recover``）。
_RECOVER_RUNNING = text(
    """
    WITH prior AS (
        SELECT channel, request_key, state FROM xiaowei_request
        WHERE state IN ('accepted', 'running') FOR UPDATE
    )
    UPDATE xiaowei_request AS r SET state = 'interrupted', failure_code = :code, updated_at = :now
    FROM prior WHERE r.channel = prior.channel AND r.request_key = prior.request_key
    RETURNING r.session_id, r.owner_kind, r.owner_id, r.channel, prior.state AS prior_state
    """
)
_SESSIONS_WRITING = text(
    "SELECT session_id FROM xiaowei_session WHERE session_id = ANY(:ids) AND state = 'writing'"
    " FOR UPDATE"
)
# 遗留发送：没有存活所有者的 sending 改为 unknown，并作废其尝试标识。serve 的尝试不记所有者（旧
# 实例失去锁即失去投递权）；显式重发命令记录它占用的连接身份，连接仍在即仍在发送，保持不动。
_RECOVER_SENDING = text(
    """
    UPDATE xiaowei_request AS r SET delivery = 'unknown', delivery_attempt = NULL,
        delivery_owner_pid = NULL, delivery_owner_started = NULL, updated_at = :now
    WHERE r.delivery = 'sending' AND NOT EXISTS (
        SELECT 1 FROM pg_stat_activity AS a
        WHERE a.pid = r.delivery_owner_pid AND a.backend_start = r.delivery_owner_started
    )
    """
)
# 飞书结果只在进程内持有回复目的地：进程退出时已保存却未取得投递权的结果再无发送触发。它们确定
# 没有发出，记为 failed，之后只能显式重发。Web 结果经 GET 读取、从不发送，不在此列。
_RECOVER_UNSENT = text(
    """
    UPDATE xiaowei_request SET delivery = 'failed', updated_at = :now
    WHERE channel = 'feishu' AND state = 'completed' AND delivery = 'pending'
    """
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
    owner: Owner
    conversation_key: str
    session_id: str
    generation: int
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class RequestRecord:
    """一条请求：``owner`` 是会话归属，``subject_id`` 是发起这条请求的 actor；群请求另有取自已验证
    入站事件的回复目的地（群与原消息），个人请求没有。"""

    channel: Channel
    request_key: str
    owner: Owner
    subject_id: str
    conversation_key: str
    session_id: str
    turn_id: str
    mode: Mode
    state: RequestState
    delivery: DeliveryState
    answer: AgentAnswer | None
    context_evidence: tuple[str, ...] | None
    """回答保存时记录的本轮模型可见证据（``TurnAnswer``）；此前格式保存的回答没有，为 ``None``。"""
    monitoring_failures: tuple[MonitoringFailure, ...]
    failure_code: FailureCode | None
    created_at: datetime
    expires_at: datetime
    reply_chat_id: str | None = None
    reply_message_id: str | None = None


@dataclass(frozen=True)
class DeliveryClaim:
    """一次已取得的投递权：``attempt`` 只属于这一次尝试，落定时必须交回。"""

    record: RequestRecord
    attempt: str


@dataclass(frozen=True)
class Acceptance:
    record: RequestRecord
    created: bool


@dataclass(frozen=True)
class RecoveryReport:
    interrupted: int
    unknown: int
    unsent: int


@dataclass(frozen=True)
class _Owner:
    """可信 owner 与会话语境摘要；``params`` 用作 SQL 绑定参数。"""

    channel: Channel
    owner: Owner
    conversation_key: str

    @property
    def params(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "owner_kind": self.owner.kind,
            "owner_id": self.owner.id,
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
        sender: Backend | None = None,
    ) -> None:
        """``sender`` 只供不持实例锁的显式重发命令：它在发送期间占用的连接身份，记入投递尝试，
        使并发的启动恢复不把仍在进行的重发当作遗留发送。"""
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
        self._instance: InstanceLock | None = None
        self._sender = sender

    @property
    def readiness(self) -> Readiness:
        """本存储锁低的进程 readiness；共享服务据此判断会话能否继续开放。"""
        return self._readiness

    # ---- 会话映射 --------------------------------------------------------------------

    async def current_session(
        self, channel: Channel, subject_id: str, conversation: str, *, owner: Owner | None = None
    ) -> ChannelSession:
        """取得会话语境的 current 会话；不存在时创建第一代。客户端不能指定会话标识。

        ``owner`` 省略时为 ``subject_id`` 的个人归属；群会话按群 owner 共享，与发起人无关。
        readiness 锁低后仍可读取已有映射，但不再创建。
        """
        owner_ = self._owner(channel, subject_id, conversation, owner)
        try:
            async with self._engine.begin() as conn:
                return await self._current(conn, owner_)
        except (OSError, SQLAlchemyError):
            raise ChannelStoreUnavailableError from None

    async def new_session(
        self, channel: Channel, subject_id: str, conversation: str, *, owner: Owner | None = None
    ) -> ChannelSession:
        """退役 current 会话并创建下一代；当前会话有未结束请求时拒绝。不复制历史或许可。"""
        self._require_ready()
        owner_ = self._owner(channel, subject_id, conversation, owner)
        try:
            async with self._engine.begin() as conn:
                await self._admit(conn)
                current = await self._current(conn, owner_, statement=_SELECT_CURRENT_FOR_UPDATE)
                retired = await conn.execute(
                    _RETIRE_CURRENT, {**owner_.params, "session_id": current.session_id}
                )
                if retired.rowcount != 1:
                    raise SessionBusyError
                created = await self._insert_current(conn, owner_, current.generation + 1)
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
        """读取 current 映射；不存在时创建第一代（须 readiness 正常且仍持有实例锁），并发创建时
        重新读取。"""
        row = (await conn.execute(statement, owner.params)).mappings().one_or_none()
        if row is None:
            self._require_ready()
            await self._admit(conn)
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
            owner=owner.owner,
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
        owner: Owner | None = None,
    ) -> Acceptance:
        """持久接受请求，或返回同一请求已有的记录；同编号不同内容时拒绝冲突。

        新请求绑定当前会话；与新建会话互斥（共享锁读取 current 映射）。新请求与重复请求都先经
        ``_admit``：失去实例锁的进程不写入，也不返回记录去重投。

        群请求（``owner`` 为群）的请求键只绑定群 owner、会话语境与原消息编号，不含发起人：同一
        消息换一个发起人、换正文或用途都是冲突，不能借不同发起人的键空间运行两次。回复目的地
        取自同一入站事件：会话语境（群）与请求编号（原消息），之后的发送只能发往这里。
        """
        self._require_ready()
        owner_ = self._owner(channel, subject_id, conversation, owner)
        group = owner_.owner.kind == "group"
        key = {"channel": channel, "request_key": self._request_key(owner_, request_id)}
        digest = self._message_digest(
            channel,
            owner_.owner,
            subject_id,
            owner_.conversation_key,
            mode,
            policy_version,
            message,
        )
        now = self._clock()
        # 提交结果不明时请求可能已存在：锁低 readiness，交给重启恢复，不让它永久“处理中”。
        with self._critical("request_accept_failed", "请求状态存储不可用"):
            async with self._engine.begin() as conn:
                await self._admit(conn)
                existing = await self._select(conn, key)
                created = False
                if existing is None:
                    session = await self._current(conn, owner_, statement=_SELECT_CURRENT_FOR_SHARE)
                    inserted = await conn.execute(
                        _INSERT_REQUEST,
                        {
                            **owner_.params,
                            **key,
                            "subject_id": subject_id,
                            "reply_chat_id": conversation if group else None,
                            "reply_message_id": request_id if group else None,
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
            not _owned_by(existing, owner_.owner)
            or existing["subject_id"] != subject_id
            or existing["conversation_key"] != owner_.conversation_key
            or existing["mode"] != mode
            or not hmac.compare_digest(existing["message_digest"], digest)
        ):
            raise RequestConflictError
        if existing["expires_at"] <= now:
            raise RequestUnavailableError
        return Acceptance(record=self._record(existing), created=created)

    async def get(
        self,
        channel: Channel,
        subject_id: str,
        conversation: str,
        request_id: str,
        *,
        owner: Owner | None = None,
    ) -> RequestRecord:
        """按当前身份读取请求；不存在、不属于该身份、已过期或内容不符合契约时统一拒绝。

        群请求同样只交给它的发起人：发送状态绑定原请求，同群其他成员不能借用别人的请求。
        """
        owner_ = self._owner(channel, subject_id, conversation, owner)
        key = {"channel": channel, "request_key": self._request_key(owner_, request_id)}
        try:
            async with self._engine.connect() as conn:
                row = await self._select(conn, key)
        except (OSError, SQLAlchemyError):
            raise ChannelStoreUnavailableError from None
        if (
            row is None
            or not _owned_by(row, owner_.owner)
            or row["subject_id"] != subject_id
            or row["expires_at"] <= self._clock()
        ):
            raise RequestUnavailableError
        return self._record(row)

    async def start(
        self, record: RequestRecord, *, message: str, policy_version: str
    ) -> RequestRecord:
        """按数据库中的真实请求 ``accepted → running``，返回数据库中的记录；只有一个进程成功。

        调用方给出的记录、正文与当前数据策略版本只是待核对的声明：在同一事务内锁定请求行，核对
        归属、发起人、会话语境、会话、轮次、用途与回复目的地都与该行一致，并按该行重算
        ``message_digest`` 与正文比对。都一致才启动，执行只用返回的记录；内容不一致（换正文、
        用途、会话或回复目的地，或数据策略已变化）时同一事务把该请求记为 ``failed/access_denied``
        并返回失败记录，不运行。请求不存在、不属于调用方给出的身份（渠道、请求键、owner、发起人、
        轮次）或已不在 ``accepted`` 时得到 ``RequestUnavailableError``，不改动任何请求。

        与接收一样经 ``_admit``：失去实例锁的进程不能再启动已接受的请求。
        """
        self._require_ready()
        key = {"channel": record.channel, "request_key": record.request_key}
        with self._critical("request_start_failed", "请求状态无法保存"):
            async with self._engine.begin() as conn:
                await self._admit(conn)
                found = (await conn.execute(_SELECT_REQUEST_FOR_UPDATE, key)).mappings()
                one = found.one_or_none()
                row: Mapping[str, Any] | None = None if one is None else dict(one)
                if row is None or row["state"] != "accepted" or _keys_of(row) != _keys(record):
                    raise RequestUnavailableError
                stored = self._record(row)
                params = {**_keys(stored), "now": self._clock()}
                if self._binds(row, record, message, policy_version):
                    if (await conn.execute(_START, params)).rowcount != 1:
                        raise RequestUnavailableError
                    return replace(stored, state="running")
                await conn.execute(_FAIL, {**params, "code": "access_denied"})
                return replace(stored, state="failed", failure_code="access_denied")

    def _binds(
        self, row: Mapping[str, Any], record: RequestRecord, message: str, policy_version: str
    ) -> bool:
        claimed = (
            record.conversation_key,
            record.session_id,
            record.mode,
            record.reply_chat_id,
            record.reply_message_id,
        )
        stored = (
            row["conversation_key"],
            row["session_id"],
            row["mode"],
            row["reply_chat_id"],
            row["reply_message_id"],
        )
        digest = self._message_digest(
            row["channel"],
            Owner(kind=row["owner_kind"], id=row["owner_id"]),
            row["subject_id"],
            row["conversation_key"],
            row["mode"],
            policy_version,
            message,
        )
        return claimed == stored and hmac.compare_digest(row["message_digest"], digest)

    async def complete(self, record: RequestRecord, turn: TurnAnswer) -> RequestRecord:
        """``running → completed`` 并保存受限回答；失败时标为 failed 并关闭会话，不交付。

        回答与本轮模型可见的证据一起保存为 ``TurnAnswer`` JSON（同一列、同一次写入）。
        """
        content = turn.model_dump_json()
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
        return replace(
            record,
            state="completed",
            answer=turn.answer,
            context_evidence=turn.context_evidence,
            monitoring_failures=turn.monitoring_failures,
        )

    async def fail(self, record: RequestRecord, code: CallerFailureCode) -> RequestRecord:
        """``accepted/running → failed``，保存安全失败码（如 Session 未提交的模型失败、繁忙）。

        失败码不在调用者闭集内（如误传异常文字或内部码）时不写入：失败状态无法保存，按关键
        状态失败锁低 readiness，请求留待重启恢复标为 interrupted。
        """
        if code not in _CALLER_FAILURE_CODES:
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

    async def claim_send(
        self, record: RequestRecord, *, resend: bool = False
    ) -> DeliveryClaim | None:
        """紧邻发送前取得投递权；返回 None 表示不得发送。

        首次发送与事件重投只竞争 ``pending → sending``；``resend=True`` 只供显式重发命令使用，
        只对 completed 结果竞争 ``failed/unknown → sending``。每次取得都写入新的尝试标识，只有
        持有它的 ``finish_send`` 能落定这次尝试。
        """
        self._require_ready()
        attempt = secrets.token_urlsafe(_ATTEMPT_BYTES)
        sender = self._sender
        params = {
            **_keys(record),
            "now": self._clock(),
            "attempt": attempt,
            "owner_pid": sender.pid if sender is not None else None,
            "owner_started": sender.started if sender is not None else None,
        }
        statement = _CLAIM_RESEND if resend else _CLAIM_FIRST
        with self._critical("delivery_claim_failed", "请求状态无法保存"):
            async with self._engine.begin() as conn:
                await self._admit(conn)
                claimed = (await conn.execute(statement, params)).rowcount == 1
        return DeliveryClaim(record, attempt) if claimed else None

    async def finish_send(self, claim: DeliveryClaim, outcome: SendOutcome) -> None:
        """落定自己取得的那次尝试：``sending → sent/failed/unknown``。

        尝试已被启动恢复作废、或已被更新的尝试取代时不改写任何状态，锁低 readiness 并报错。
        """
        params = {"outcome": outcome, "attempt": claim.attempt}
        updated = await self._update(
            _FINISH_SEND, claim.record, critical="delivery_state_failed", **params
        )
        if updated != 1:
            self._readiness.lock("delivery_state_lost")
            raise ChannelStoreUnavailableError("投递状态无法保存")

    # ---- 启动恢复 --------------------------------------------------------------------

    async def recover(self, lock: InstanceLock) -> RecoveryReport:
        """持有实例锁的启动一致性恢复：一个事务内中断未完成请求并关闭其会话，遗留发送改为未知，
        飞书已保存但未发送的结果记为发送失败。

        例外：群会话中被中断的请求全部只是排队（``accepted``，从未取得启动）、且会话不在写入中时，
        会话保持原状态——没有 Runner 碰过它。同会话有 ``running``、会话处于 ``writing``，或个人会话，
        都照原规则关闭。中断前状态、会话状态与关闭在同一个持有实例锁的事务内读取与写入。

        不调用 Runner、不发送消息、不放回队列。失败整体回滚并锁低 readiness。读取前先等待仍在提交的
        接收事务（可能来自刚失去锁的旧实例）结束；恢复后本存储的接收都要经该锁核对。
        """
        await lock.verify()
        now = self._clock()
        conn = lock.connection
        with self._critical("recovery_failed", "启动恢复失败"):
            async with conn.begin():
                await lock.exclude_accepts()
                rows = (
                    await conn.execute(_RECOVER_RUNNING, {"code": INTERRUPTED, "now": now})
                ).all()
                kept = await _queued_group_sessions(conn, rows)
                await close_interrupted_sessions(
                    conn,
                    [
                        (r.session_id, Owner(kind=r.owner_kind, id=r.owner_id), r.channel)
                        for r in rows
                        if r.session_id not in kept
                    ],
                    now=now,
                    expires_at=now + self._session_retention,
                )
                unknown = (await conn.execute(_RECOVER_SENDING, {"now": now})).rowcount
                unsent = (await conn.execute(_RECOVER_UNSENT, {"now": now})).rowcount
        self._instance = lock
        return RecoveryReport(interrupted=len(rows), unknown=unknown, unsent=unsent)

    # ---- 内部 ------------------------------------------------------------------------

    async def _admit(self, conn: AsyncConnection) -> None:
        """开始新工作的写入（接收、创建或轮换会话、取得投递权）在本事务内的实例所有权核对。

        本存储经 ``recover`` 绑定实例锁后，取得接收屏障共享锁并在数据库内确认实例锁仍属本进程，
        保持到事务结束（``InstanceLock.admits``）；失败时锁低 readiness 并拒绝。未绑定的存储（显式
        重发等维护命令）不核对，仍只靠数据库条件更新。
        """
        if self._instance is not None and not await self._instance.admits(conn):
            self._readiness.lock("instance_lock_lost")
            raise NotReadyError

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
        answer, context = None, None
        monitoring_failures: tuple[MonitoringFailure, ...] = ()
        if row["answer"] is not None:
            raw = row["answer"]
            if len(raw.encode()) > self._max_answer_bytes:
                raise RequestUnavailableError
            answer, context, monitoring_failures = _saved_answer(raw)
        if row["failure_code"] is not None and row["failure_code"] not in _FAILURE_CODES:
            raise RequestUnavailableError
        return RequestRecord(
            channel=row["channel"],
            request_key=row["request_key"],
            owner=Owner(kind=row["owner_kind"], id=row["owner_id"]),
            subject_id=row["subject_id"],
            conversation_key=row["conversation_key"],
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            mode=row["mode"],
            state=row["state"],
            delivery=row["delivery"],
            answer=answer,
            context_evidence=context,
            monitoring_failures=monitoring_failures,
            failure_code=row["failure_code"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            reply_chat_id=row["reply_chat_id"],
            reply_message_id=row["reply_message_id"],
        )

    def _owner(
        self, channel: Channel, subject_id: str, conversation: str, owner: Owner | None
    ) -> _Owner:
        """会话归属与会话语境摘要。个人归属的摘要与 v4 相同（不含 owner 种类），群归属另带
        种类与群 owner，二者的编码长度不同，不会相互碰撞。"""
        if not subject_id or not conversation:
            raise ValueError("可信身份与会话语境不能为空")
        if owner is None:
            owner = Owner(kind="personal", id=subject_id)
        if owner.kind == "personal":
            if owner.id != subject_id:
                raise ValueError("个人会话只属于发起人本人")
            key = self._digest("conversation", channel, subject_id, conversation)
        else:
            if channel != "feishu":
                raise ValueError("群会话只属于飞书渠道")
            key = self._digest("conversation", channel, "group", owner.id, conversation)
        return _Owner(channel=channel, owner=owner, conversation_key=key)

    def _request_key(self, owner: _Owner, request_id: str) -> str:
        if not request_id:
            raise ValueError("请求编号不能为空")
        if owner.owner.kind == "group":
            return self._digest(
                "request",
                owner.channel,
                "group",
                owner.owner.id,
                owner.conversation_key,
                request_id,
            )
        return self._digest(
            "request", owner.channel, owner.owner.id, owner.conversation_key, request_id
        )

    def _message_digest(
        self,
        channel: str,
        owner: Owner,
        subject_id: str,
        conversation_key: str,
        mode: str,
        policy_version: str,
        message: str,
    ) -> str:
        """请求内容摘要：个人请求保持 v4 的原值；群请求另带群 owner，并含发起人。"""
        if owner.kind == "group":
            return self._digest(
                "message",
                channel,
                "group",
                owner.id,
                subject_id,
                conversation_key,
                mode,
                policy_version,
                message,
            )
        return self._digest(
            "message", channel, subject_id, conversation_key, mode, policy_version, message
        )

    def _digest(self, purpose: str, *parts: str) -> str:
        """带服务端密钥的摘要；各部分按 JSON 数组编码，边界不会混淆。"""
        body = json.dumps([purpose, *parts], ensure_ascii=False, separators=(",", ":"))
        return hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()


def _saved_answer(
    raw: str,
) -> tuple[AgentAnswer, tuple[str, ...] | None, tuple[MonitoringFailure, ...]]:
    """读取保存的回答：``TurnAnswer`` JSON，或此前只保存 ``AgentAnswer`` 的格式（没有上下文证据
    记录，交付方据此拒绝）。两者都不是时按不可用处理。"""
    try:
        turn = TurnAnswer.model_validate_json(raw)
    except ValidationError:
        try:
            return AgentAnswer.model_validate_json(raw), None, ()
        except ValidationError:
            raise RequestUnavailableError from None
    return turn.answer, turn.context_evidence, turn.monitoring_failures


def _keys(record: RequestRecord) -> dict[str, object]:
    return {
        "channel": record.channel,
        "request_key": record.request_key,
        "owner_kind": record.owner.kind,
        "owner_id": record.owner.id,
        "subject_id": record.subject_id,
        "turn_id": record.turn_id,
    }


def _keys_of(row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "channel": row["channel"],
        "request_key": row["request_key"],
        "owner_kind": row["owner_kind"],
        "owner_id": row["owner_id"],
        "subject_id": row["subject_id"],
        "turn_id": row["turn_id"],
    }


def _owned_by(row: Mapping[str, Any], owner: Owner) -> bool:
    return bool(row["owner_kind"] == owner.kind and row["owner_id"] == owner.id)


async def _queued_group_sessions(conn: AsyncConnection, rows: Sequence[Any]) -> frozenset[str]:
    """启动恢复中可以保持原状态的群会话：其中断请求全部只在排队，且会话不在写入中。"""
    started = {r.session_id for r in rows if r.prior_state != "accepted"}
    queued = {r.session_id for r in rows if r.owner_kind == "group" and r.session_id not in started}
    if not queued:
        return frozenset()
    writing = await conn.execute(_SESSIONS_WRITING, {"ids": sorted(queued)})
    return frozenset(queued - {row.session_id for row in writing})
