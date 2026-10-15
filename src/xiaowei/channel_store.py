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
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, get_args
from uuid import uuid4

if TYPE_CHECKING:
    from xiaowei.app import PausedTurn
    from xiaowei.feishu_report import ReportFile

from pydantic import SecretStr, ValidationError
from sqlalchemy import String, TextClause, bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from xiaowei.app import Mode
from xiaowei.models import (
    SESSION_STATE_MAX_BYTES,
    AgentAnswer,
    ApprovedAction,
    Channel,
    Identity,
    MonitoringFailure,
    Owner,
    SessionSnapshot,
    TurnAnswer,
)
from xiaowei.session import close_interrupted_sessions, close_sessions
from xiaowei.storage import Backend, InstanceLock, Readiness

RequestState = Literal["accepted", "running", "paused", "completed", "failed", "interrupted"]
DeliveryState = Literal["pending", "sending", "sent", "failed", "unknown"]
SendOutcome = Literal["sent", "failed", "unknown"]
ReportUploadState = Literal["pending", "uploading", "uploaded", "unknown"]
# 安全失败码闭集（与迁移 007 的 CHECK 一致）：只表达失败类别，不携带异常文字。调用者经 ``fail()``
# 只能写入 ``CallerFailureCode``；两个内部码各有唯一写入路径：``result_not_saved`` 由结果保存
# 失败路径与关闭 Session 同一事务写入，``interrupted`` 由启动恢复写入。
CallerFailureCode = Literal[
    "busy",
    "model_failed",
    "evidence_failed",
    "session_failed",
    "scope_unverifiable",
    "access_denied",
    "action_not_approvable",
    "action_feedback_failed",
    "action_unknown",
    "action_rejected",
    "action_requester_invalid",
    "action_session_invalid",
    "action_upgrade_invalidated",
    "action_expired",
    "action_unresumable",
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
        created_at, updated_at, expires_at, delivery_version
    ) VALUES (
        :channel, :request_key, :owner_kind, :owner_id, :subject_id, :conversation_key,
        :session_id, :turn_id, :mode, :message_digest, 'accepted', 'pending', :reply_chat_id,
        :reply_message_id, :now, :now, :expires_at, 1
    )
    ON CONFLICT (channel, request_key) DO NOTHING
    """
)
_SELECT_REQUEST = text(
    """
    SELECT channel, request_key, owner_kind, owner_id, subject_id, conversation_key, session_id,
           turn_id, mode, message_digest, state, answer, failure_code, delivery, reply_chat_id,
           reply_message_id, created_at, expires_at, session_state, delivery_version
    FROM xiaowei_request WHERE channel = :channel AND request_key = :request_key
    """
)
_SELECT_REQUEST_FOR_UPDATE = text(
    """
    SELECT channel, request_key, owner_kind, owner_id, subject_id, conversation_key, session_id,
           turn_id, mode, message_digest, state, answer, failure_code, delivery, reply_chat_id,
           reply_message_id, created_at, expires_at, session_state, delivery_version
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
    UPDATE xiaowei_request SET state = 'completed', answer = :answer,
        session_state = :session_state, updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND owner_kind = :owner_kind
      AND owner_id = :owner_id AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND state = 'running'
    """
)
_FAIL = text(
    """
    UPDATE xiaowei_request SET state = 'failed', failure_code = :code,
        session_state = :session_state, updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND owner_kind = :owner_kind
      AND owner_id = :owner_id AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND state IN ('accepted', 'running')
    """
)
_SETTLE_UNRESUMABLE = text(
    "UPDATE xiaowei_action SET state='rejected', failure_code='action_unresumable',"
    " updated_at=:now WHERE owner_id=:owner AND proposal_turn_id=:turn"
    " AND state='pending' AND action_id NOT IN :keep"
).bindparams(bindparam("keep", expanding=True, type_=String()))
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
      AND state IN ('completed', 'paused', 'failed', 'interrupted')
    """
)
_CLAIM_RESEND = text(
    """
    UPDATE xiaowei_request SET delivery = 'sending', delivery_attempt = :attempt,
        delivery_owner_pid = :owner_pid, delivery_owner_started = :owner_started, updated_at = :now
    WHERE channel = :channel AND request_key = :request_key AND owner_kind = :owner_kind
      AND owner_id = :owner_id AND subject_id = :subject_id
      AND turn_id = :turn_id
      AND delivery IN ('failed', 'unknown') AND expires_at > :now
      AND state IN ('completed', 'paused')
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
    WHERE channel = 'feishu' AND state IN ('completed', 'paused') AND delivery = 'pending'
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


class ActionAdmissionRejectedError(RequestUnavailableError):
    """最后写准入的确定拒绝，按实际原因落定，不与尝试丢失混同。"""

    def __init__(self, code: Literal["action_session_invalid", "action_expired"]) -> None:
        self.code = code
        super().__init__()


class ReportMismatchError(ChannelStoreError):
    """已保存报告与当前获准投影不一致，或上传状态不能安全继续。"""

    def __init__(self) -> None:
        super().__init__("报告原文件或上传状态无法确认，停止发送")


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
    session_state: SessionSnapshot | None = None
    run_turn_id: str | None = None
    delivery_version: int = 0


@dataclass(frozen=True)
class DeliveryClaim:
    """一次已取得的投递权：``attempt`` 只属于这一次尝试，落定时必须交回。"""

    record: RequestRecord
    attempt: str
    content_expires_at: datetime | None = None


@dataclass(frozen=True)
class DeliveryPart:
    sequence: int
    kind: Literal["approval", "body", "md", "html"]
    body: bytes
    file_key: str | None = None
    upload_state: ReportUploadState = "pending"
    send_state: DeliveryState = "pending"
    attempt: str | None = None
    uuid: str = ""
    message_id: str | None = None


@dataclass(frozen=True)
class ActionRecord:
    """有界、已批准字段的动作事实，独立于 SDK Session 的状态。"""

    action_id: str
    owner_id: str
    requester_id: str
    proposal_session_id: str
    proposal_turn_id: str
    call_id: str
    proposal_tool_id: str
    write_tool_id: str
    target_id: str
    binding: str
    plan: str
    state: str
    attempt: str | None
    approver_id: str | None
    approval_turn_id: str | None
    result: str | None
    created_at: datetime
    updated_at: datetime
    approval_expires_at: datetime
    expires_at: datetime
    failure_code: CallerFailureCode | None = None


@dataclass(frozen=True)
class NativeRunRecord:
    turn_id: str
    owner_id: str
    context: str
    run_state: str
    ledger: str
    message: str
    binding: str
    scope_checks: int
    version: int
    consumed: bool
    material_turn_id: str
    expires_at: datetime


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
        return self._record(dict(row))

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
                await conn.execute(
                    _FAIL, {**params, "code": "access_denied", "session_state": None}
                )
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

    async def complete(
        self,
        record: RequestRecord,
        turn: TurnAnswer,
        *,
        failure_code: Literal["result_not_saved", "action_feedback_failed"] = "result_not_saved",
    ) -> RequestRecord:
        """``running → completed`` 并保存受限回答；失败时标为 failed 并关闭会话，不交付。

        回答与本轮模型可见的证据一起保存为 ``TurnAnswer`` JSON（同一列、同一次写入）。
        已成功执行的批准使用 action_feedback_failed；保留关闭会话和锁低的原有失败策略。
        """
        if failure_code not in {"result_not_saved", "action_feedback_failed"}:
            self._readiness.lock("result_state_unwritable")
            raise ValueError("结果保存失败码不在允许的范围内")
        content = turn.model_dump_json()
        try:
            snapshot = _session_state(turn.session_state, "completed", None)
        except RequestUnavailableError:
            await self._fail_after_commit(record, failure_code, settle_turn_id=turn.run_turn_id)
            raise ResultNotSavedError from None
        if len(content.encode()) > self._max_answer_bytes:
            await self._fail_after_commit(record, failure_code, settle_turn_id=turn.run_turn_id)
            raise ResultNotSavedError
        try:
            async with self._engine.begin() as conn:
                saved = await conn.execute(
                    _COMPLETE,
                    {
                        **_keys(record),
                        "answer": content,
                        "session_state": snapshot,
                        "now": self._clock(),
                    },
                )
                if saved.rowcount == 1:
                    await self._settle_unresumable_actions(
                        conn, record.owner.id, turn.run_turn_id or record.turn_id
                    )
        except asyncio.CancelledError:
            # 保存结果不明，也不能在取消中再写失败状态：锁低后交给重启恢复。
            self._readiness.lock("result_state_unwritable")
            raise
        except (OSError, SQLAlchemyError):
            await self._fail_after_commit(record, failure_code, settle_turn_id=turn.run_turn_id)
            raise ResultNotSavedError from None
        if saved.rowcount != 1:
            await self._fail_after_commit(record, failure_code, settle_turn_id=turn.run_turn_id)
            raise ResultNotSavedError
        return replace(
            record,
            state="completed",
            answer=turn.answer,
            context_evidence=turn.context_evidence,
            monitoring_failures=turn.monitoring_failures,
            session_state=turn.session_state,
            run_turn_id=turn.run_turn_id,
        )

    async def fail(
        self,
        record: RequestRecord,
        code: CallerFailureCode,
        *,
        session_state: SessionSnapshot | None = None,
        settle_action_id: str | None = None,
    ) -> RequestRecord:
        """``accepted/running → failed``，保存安全失败码（如 Session 未提交的模型失败、繁忙）。

        失败码不在调用者闭集内（如误传异常文字或内部码）时不写入：失败状态无法保存，按关键
        状态失败锁低 readiness，请求留待重启恢复标为 interrupted。
        """
        if code not in _CALLER_FAILURE_CODES:
            self._readiness.lock("request_fail_failed")
            raise ValueError("失败码不在允许的范围内")
        try:
            snapshot = _session_state(session_state, "failed", code)
        except RequestUnavailableError:
            self._readiness.lock("request_fail_failed")
            raise
        params = {"code": code, "session_state": snapshot}
        with self._critical("request_fail_failed", "请求状态无法保存"):
            async with self._engine.begin() as conn:
                changed = await conn.execute(
                    _FAIL, {**_keys(record), "now": self._clock(), **params}
                )
                if changed.rowcount != 1:
                    raise RequestUnavailableError
                await self._settle_unresumable_actions(conn, record.owner.id, record.turn_id)
                if settle_action_id is not None:
                    original = (
                        await conn.execute(
                            text(
                                "SELECT a.proposal_turn_id FROM xiaowei_action a"
                                " JOIN xiaowei_action_run r ON r.turn_id=a.proposal_turn_id"
                                " AND r.owner_id=a.owner_id WHERE a.action_id=:action"
                                " AND a.owner_id=:owner AND a.attempt IS NOT NULL"
                                " AND r.consumed=true FOR UPDATE OF r"
                            ),
                            {"action": settle_action_id, "owner": record.owner.id},
                        )
                    ).scalar_one_or_none()
                    if original is not None:
                        await self._settle_unresumable_actions(conn, record.owner.id, original)
        return replace(record, state="failed", failure_code=code, session_state=session_state)

    async def _fail_after_commit(
        self,
        record: RequestRecord,
        code: FailureCode = RESULT_NOT_SAVED,
        *,
        settle_turn_id: str | None = None,
    ) -> None:
        """结果未保存：同一事务标记 failed 并关闭会话；也失败时锁低 readiness。"""
        params = {**_keys(record), "code": code, "session_state": None, "now": self._clock()}
        try:
            async with self._engine.begin() as conn:
                if (await conn.execute(_FAIL, params)).rowcount != 1:
                    raise ChannelStoreUnavailableError
                await self._settle_unresumable_actions(conn, record.owner.id, record.turn_id)
                if settle_turn_id is not None and settle_turn_id != record.turn_id:
                    await self._settle_unresumable_actions(conn, record.owner.id, settle_turn_id)
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

    async def finish_send(self, claim: DeliveryClaim, outcome: SendOutcome) -> SendOutcome:
        """落定自己取得的那次尝试：``sending → sent/failed/unknown``。

        尝试已被启动恢复作废、或已被更新的尝试取代时不改写任何状态，锁低 readiness 并报错。
        """
        params = {**_keys(claim.record), "now": self._clock(), "attempt": claim.attempt}
        with self._critical("delivery_state_failed", "投递状态无法保存"):
            async with self._engine.begin() as conn:
                # 分项和整体在同一事务落定；unknown 不被一次明确失败覆盖。
                rows = (
                    await conn.execute(
                        text(
                            "SELECT send_state, upload_state FROM xiaowei_delivery_part "
                            "WHERE turn_id=:turn_id"
                        ),
                        params,
                    )
                ).all()
                if outcome == "unknown":
                    await conn.execute(
                        text(
                            "UPDATE xiaowei_delivery_part SET "
                            "send_state=CASE WHEN send_state='sending' THEN 'unknown' "
                            "ELSE send_state END, "
                            "upload_state=CASE WHEN upload_state='uploading' THEN 'unknown' "
                            "ELSE upload_state END "
                            "WHERE turn_id=:turn_id AND attempt=:attempt"
                        ),
                        params,
                    )
                if any(r.send_state == "unknown" or r.upload_state == "unknown" for r in rows):
                    outcome = "unknown"
                elif rows and outcome == "sent" and any(r.send_state != "sent" for r in rows):
                    outcome = "failed"
                elif (
                    not rows
                    and outcome == "sent"
                    and claim.record.delivery_version == 1
                    and claim.record.channel == "feishu"
                    and claim.record.state in {"completed", "paused"}
                ):
                    outcome = "failed"
                if claim.record.delivery == "unknown" and outcome == "failed":
                    # 旧记录没有分项，也不能把此前可能送达改成确定未送达。
                    outcome = "unknown"
                updated = (
                    await conn.execute(_FINISH_SEND, {**params, "outcome": outcome})
                ).rowcount
        if updated != 1:
            self._readiness.lock("delivery_state_lost")
            raise ChannelStoreUnavailableError("投递状态无法保存")
        return outcome

    async def delivery_parts(self, record: RequestRecord) -> tuple[DeliveryPart, ...]:
        """调用方已复核原请求/Evidence；这里只按原请求归属和期限读取不可变字节。"""
        params = {**_keys(record), "now": self._clock()}
        with self._critical("delivery_parts_unreadable", "投递内容无法读取"):
            async with self._engine.connect() as conn:
                rows = (
                    (
                        await conn.execute(
                            text(
                                "SELECT p.* FROM xiaowei_delivery_part p JOIN xiaowei_request r "
                                "ON r.turn_id=p.turn_id WHERE r.turn_id=:turn_id "
                                "AND r.channel=:channel "
                                "AND r.request_key=:request_key AND r.owner_kind=:owner_kind "
                                "AND r.owner_id=:owner_id AND r.subject_id=:subject_id "
                                "AND r.expires_at>:now ORDER BY p.sequence"
                            ),
                            params,
                        )
                    )
                    .mappings()
                    .all()
                )
        return tuple(
            DeliveryPart(
                sequence=row["sequence"],
                kind=row["kind"],
                body=bytes(row["body"]),
                file_key=row["file_key"],
                upload_state=row["upload_state"],
                send_state=row["send_state"],
                attempt=row["attempt"],
                uuid=row["uuid"],
                message_id=row["message_id"],
            )
            for row in rows
        )

    @asynccontextmanager
    async def _part_attempt(self, claim: DeliveryClaim) -> AsyncIterator[AsyncConnection]:
        """每次 I/O 的廉价所有权检查，不重新查询数据权限。"""
        self._require_ready()
        if claim.content_expires_at is not None and self._clock() >= claim.content_expires_at:
            raise RequestUnavailableError
        with self._critical("delivery_part_state_failed", "投递状态无法保存"):
            async with self._engine.begin() as conn:
                await self._admit(conn)
                valid = await conn.scalar(
                    text(
                        "SELECT 1 FROM xiaowei_request WHERE turn_id=:turn_id "
                        "AND channel=:channel AND request_key=:request_key "
                        "AND owner_kind=:owner_kind AND owner_id=:owner_id "
                        "AND subject_id=:subject_id AND state IN ('completed','paused') "
                        "AND delivery='sending' AND delivery_attempt=:attempt "
                        "AND expires_at>:now FOR UPDATE"
                    ),
                    {**_keys(claim.record), "now": self._clock(), "attempt": claim.attempt},
                )
                if valid != 1:
                    raise RequestUnavailableError
                yield conn

    async def bind_delivery_parts(
        self, claim: DeliveryClaim, parts: Sequence[DeliveryPart]
    ) -> tuple[DeliveryPart, ...]:
        """新分项首次 I/O 前绑定字节/uuid；已有分项从不按当前模板改写。"""
        async with self._part_attempt(claim) as conn:
            for part in parts:
                await conn.execute(
                    text(
                        "INSERT INTO xiaowei_delivery_part(turn_id,sequence,kind,body,uuid) "
                        "VALUES(:turn,:seq,:kind,:body,:uuid) ON CONFLICT DO NOTHING"
                    ),
                    {
                        "turn": claim.record.turn_id,
                        "seq": part.sequence,
                        "kind": part.kind,
                        "body": part.body,
                        "uuid": str(uuid4()),
                    },
                )
        return await self.delivery_parts(claim.record)

    async def check_delivery_attempt(self, claim: DeliveryClaim) -> None:
        async with self._part_attempt(claim):
            pass

    async def begin_part_upload(self, claim: DeliveryClaim, part: DeliveryPart) -> bool:
        async with self._part_attempt(claim) as conn:
            changed = await conn.execute(
                text(
                    "UPDATE xiaowei_delivery_part SET upload_state='uploading',attempt=:attempt "
                    "WHERE turn_id=:turn AND sequence=:seq AND upload_state='pending'"
                ),
                {"turn": claim.record.turn_id, "seq": part.sequence, "attempt": claim.attempt},
            )
            return changed.rowcount == 1

    async def finish_part_upload(
        self, claim: DeliveryClaim, part: DeliveryPart, key: str | None
    ) -> None:
        if key is not None and not key:
            raise ValueError("上传键无效")
        async with self._part_attempt(claim) as conn:
            changed = await conn.execute(
                text(
                    "UPDATE xiaowei_delivery_part SET upload_state=:state,file_key=:key "
                    "WHERE turn_id=:turn AND sequence=:seq AND upload_state='uploading' "
                    "AND attempt=:attempt"
                ),
                {
                    "turn": claim.record.turn_id,
                    "seq": part.sequence,
                    "attempt": claim.attempt,
                    "state": "uploaded" if key is not None else "unknown",
                    "key": key,
                },
            )
            if changed.rowcount != 1:
                raise RequestUnavailableError

    async def begin_part_send(self, claim: DeliveryClaim, part: DeliveryPart) -> bool:
        async with self._part_attempt(claim) as conn:
            changed = await conn.execute(
                text(
                    "UPDATE xiaowei_delivery_part SET send_state='sending',attempt=:attempt "
                    "WHERE turn_id=:turn AND sequence=:seq "
                    "AND send_state IN ('pending','failed','unknown') "
                    "AND (kind IN ('body','approval') OR upload_state='uploaded')"
                ),
                {"turn": claim.record.turn_id, "seq": part.sequence, "attempt": claim.attempt},
            )
            return changed.rowcount == 1

    async def finish_part_send(
        self,
        claim: DeliveryClaim,
        part: DeliveryPart,
        outcome: SendOutcome,
        message_id: str | None,
    ) -> SendOutcome:
        if outcome == "sent" and not message_id:
            outcome = "unknown"
        if part.send_state == "unknown" and outcome == "failed":
            outcome = "unknown"
        async with self._part_attempt(claim) as conn:
            changed = await conn.execute(
                text(
                    "UPDATE xiaowei_delivery_part SET send_state=:state,message_id=:id "
                    "WHERE turn_id=:turn AND sequence=:seq AND send_state='sending' "
                    "AND attempt=:attempt"
                ),
                {
                    "turn": claim.record.turn_id,
                    "seq": part.sequence,
                    "attempt": claim.attempt,
                    "state": outcome,
                    "id": message_id if outcome == "sent" else None,
                },
            )
            if changed.rowcount != 1:
                raise RequestUnavailableError
        return outcome

    async def bind_report(
        self, record: RequestRecord, report: ReportFile
    ) -> tuple[ReportUploadState, str | None, str]:
        """发送前保存不可变原文件；重发只接受同一字节与摘要，不重查或重写旧报告。"""
        self._require_ready()
        if (
            record.channel != "feishu"
            or hashlib.sha256(report.content).hexdigest() != report.digest
        ):
            raise ReportMismatchError
        params = {
            **_keys(record),
            "format": report.format,
            "body": report.content,
            "digest": report.digest,
            "now": self._clock(),
        }
        with self._critical("report_bind_failed", "报告绑定状态无法保存"):
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO xiaowei_report "
                        "(turn_id, format, body, digest, upload_state, expires_at) "
                        "SELECT r.turn_id, :format, :body, :digest, 'pending', r.expires_at "
                        "FROM xiaowei_request r WHERE r.turn_id = :turn_id "
                        "AND r.channel = :channel AND r.request_key = :request_key "
                        "AND r.owner_kind = :owner_kind AND r.owner_id = :owner_id "
                        "AND r.subject_id = :subject_id AND r.state = 'completed' "
                        "AND r.delivery = 'sending' AND r.expires_at > :now "
                        "ON CONFLICT (turn_id) DO NOTHING"
                    ),
                    params,
                )
                row = (
                    (
                        await conn.execute(
                            text(
                                "SELECT p.format, p.body, p.digest, p.upload_state, p.file_key, "
                                "r.delivery_attempt "
                                "FROM xiaowei_report p JOIN xiaowei_request r "
                                "ON r.turn_id = p.turn_id WHERE r.turn_id = :turn_id "
                                "AND r.channel = :channel AND r.request_key = :request_key "
                                "AND r.owner_kind = :owner_kind AND r.owner_id = :owner_id "
                                "AND r.subject_id = :subject_id AND r.state = 'completed' "
                                "AND r.delivery = 'sending' AND r.expires_at > :now"
                            ),
                            params,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
        if row is None or (
            row["format"] != report.format
            or bytes(row["body"]) != report.content
            or row["digest"] != report.digest
        ):
            raise ReportMismatchError
        state, key = row["upload_state"], row["file_key"]
        if state not in ("pending", "uploading", "uploaded", "unknown"):
            raise ReportMismatchError
        if (state == "uploaded") != (isinstance(key, str) and bool(key)):
            raise ReportMismatchError
        attempt = row["delivery_attempt"]
        if not isinstance(attempt, str) or not attempt:
            raise ReportMismatchError
        return state, key, attempt

    async def has_bound_report(self, record: RequestRecord) -> bool:
        """重发改走卡片前核对是否曾绑定文件；已有上传记录不得绕过未知状态。"""
        params = {**_keys(record), "now": self._clock()}
        try:
            async with self._engine.connect() as conn:
                bound = await conn.scalar(
                    text(
                        "SELECT 1 FROM xiaowei_report p JOIN xiaowei_request r "
                        "ON r.turn_id = p.turn_id WHERE r.turn_id = :turn_id "
                        "AND r.channel = :channel AND r.request_key = :request_key "
                        "AND r.owner_kind = :owner_kind AND r.owner_id = :owner_id "
                        "AND r.subject_id = :subject_id AND r.expires_at > :now"
                    ),
                    params,
                )
        except (OSError, SQLAlchemyError):
            raise ChannelStoreUnavailableError from None
        return bound is not None

    async def begin_report_upload(self, record: RequestRecord, attempt: str) -> bool:
        """上传 I/O 前原子占位；重入、恢复后的 uploading/unknown 一律不再上传。"""
        changed = await self._update(
            text(
                "UPDATE xiaowei_report p SET upload_state = 'uploading' "
                "WHERE p.turn_id = :turn_id AND p.upload_state = 'pending' "
                "AND EXISTS (SELECT 1 FROM xiaowei_request r WHERE r.turn_id = p.turn_id "
                "AND r.channel = :channel AND r.request_key = :request_key "
                "AND r.owner_kind = :owner_kind AND r.owner_id = :owner_id "
                "AND r.subject_id = :subject_id AND r.state = 'completed' "
                "AND r.delivery = 'sending' AND r.delivery_attempt = :attempt "
                "AND r.expires_at > :now)"
            ),
            record,
            critical="report_upload_start_failed",
            attempt=attempt,
        )
        return changed == 1

    async def complete_report_upload(self, record: RequestRecord, attempt: str, key: str) -> None:
        """上传键先持久化，才允许开始向原接收者发送文件消息。"""
        if not isinstance(key, str) or not key:
            raise ReportMismatchError
        changed = await self._update(
            text(
                "UPDATE xiaowei_report p SET upload_state = 'uploaded', file_key = :key "
                "WHERE p.turn_id = :turn_id AND p.upload_state = 'uploading' "
                "AND EXISTS (SELECT 1 FROM xiaowei_request r WHERE r.turn_id = p.turn_id "
                "AND r.channel = :channel AND r.request_key = :request_key "
                "AND r.owner_kind = :owner_kind AND r.owner_id = :owner_id "
                "AND r.subject_id = :subject_id AND r.state = 'completed' "
                "AND r.delivery = 'sending' AND r.delivery_attempt = :attempt "
                "AND r.expires_at > :now)"
            ),
            record,
            critical="report_upload_key_failed",
            key=key,
            attempt=attempt,
        )
        if changed != 1:
            self._readiness.lock("report_upload_state_lost")
            raise ChannelStoreUnavailableError("报告上传状态无法保存")

    async def mark_report_upload_unknown(self, record: RequestRecord, attempt: str) -> None:
        """SDK 上传错误无法证明平台没有保存文件；保留原文但禁止自动或显式再上传。"""
        changed = await self._update(
            text(
                "UPDATE xiaowei_report p SET upload_state = 'unknown' "
                "WHERE p.turn_id = :turn_id AND p.upload_state = 'uploading' "
                "AND EXISTS (SELECT 1 FROM xiaowei_request r WHERE r.turn_id = p.turn_id "
                "AND r.channel = :channel AND r.request_key = :request_key "
                "AND r.owner_kind = :owner_kind AND r.owner_id = :owner_id "
                "AND r.subject_id = :subject_id AND r.state = 'completed' "
                "AND r.delivery = 'sending' AND r.delivery_attempt = :attempt "
                "AND r.expires_at > :now)"
            ),
            record,
            critical="report_upload_unknown_failed",
            attempt=attempt,
        )
        if changed != 1:
            self._readiness.lock("report_upload_state_lost")
            raise ChannelStoreUnavailableError("报告上传状态无法保存")

    async def confirm_report_send(self, record: RequestRecord, attempt: str, key: str) -> None:
        """上传后、文件消息前确认取得的投递尝试未被恢复或重发取代。"""
        self._require_ready()
        params = {**_keys(record), "attempt": attempt, "key": key, "now": self._clock()}
        with self._critical("report_send_state_lost", "报告发送状态无法确认"):
            async with self._engine.connect() as conn:
                valid = await conn.scalar(
                    text(
                        "SELECT 1 FROM xiaowei_report p JOIN xiaowei_request r "
                        "ON r.turn_id = p.turn_id WHERE r.turn_id = :turn_id "
                        "AND r.channel = :channel AND r.request_key = :request_key "
                        "AND r.owner_kind = :owner_kind AND r.owner_id = :owner_id "
                        "AND r.subject_id = :subject_id AND r.state = 'completed' "
                        "AND r.delivery = 'sending' AND r.delivery_attempt = :attempt "
                        "AND r.expires_at > :now AND p.upload_state = 'uploaded' "
                        "AND p.file_key = :key"
                    ),
                    params,
                )
        if valid != 1:
            self._readiness.lock("report_send_state_lost")
            raise ChannelStoreUnavailableError("报告发送状态无法确认")

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
                await conn.execute(
                    text(
                        "UPDATE xiaowei_delivery_part p SET "
                        "send_state=CASE WHEN p.send_state='sending' THEN 'unknown' "
                        "ELSE p.send_state END, "
                        "upload_state=CASE WHEN p.upload_state='uploading' THEN 'unknown' "
                        "ELSE p.upload_state END "
                        "FROM xiaowei_request r WHERE r.turn_id=p.turn_id AND r.delivery='unknown' "
                        "AND r.delivery_attempt IS NULL"
                    )
                )
                unsent = (await conn.execute(_RECOVER_UNSENT, {"now": now})).rowcount
                await conn.execute(
                    text(
                        "UPDATE xiaowei_action SET state = 'unknown', updated_at = :now "
                        "WHERE state = 'executing'"
                    ),
                    {"now": now},
                )
        self._instance = lock
        return RecoveryReport(interrupted=len(rows), unknown=unknown, unsent=unsent)

    # ---- 监控 Action：只复用应用存储和实例锁，不触碰 SDK Session -----------------------

    async def propose_action(
        self, action: ActionRecord, *, approval: ApprovedAction | None = None
    ) -> None:
        self._require_ready()
        if len(action.plan.encode()) > 128_000:
            raise RequestUnavailableError
        params = dict(vars(action))
        with self._critical("action_save_failed", "动作未保存"):
            async with self._engine.begin() as conn:
                await self._admit(conn)
                identity = Identity(
                    subject_id=action.requester_id,
                    session_id=action.proposal_session_id,
                    turn_id=action.proposal_turn_id,
                    owner=Owner(kind="group", id=action.owner_id),
                    channel="feishu",
                )
                request = await self._proposal_request(conn, identity, approval)
                if request is None or action.expires_at > request["expires_at"]:
                    raise RequestUnavailableError
                await conn.execute(
                    text("""
                        INSERT INTO xiaowei_action (
                            action_id, owner_id, requester_id,
                            proposal_session_id, proposal_turn_id,
                            call_id, proposal_tool_id, write_tool_id, target_id, binding, plan,
                            state, attempt, approver_id, approval_turn_id, result, created_at,
                            updated_at, approval_expires_at, expires_at
                        ) VALUES (
                            :action_id, :owner_id, :requester_id,
                            :proposal_session_id, :proposal_turn_id,
                            :call_id, :proposal_tool_id, :write_tool_id,
                            :target_id, :binding, :plan,
                            :state, :attempt, :approver_id, :approval_turn_id,
                            :result, :created_at,
                            :updated_at, :approval_expires_at, :expires_at
                        )
                    """),
                    params,
                )

    async def get_action(self, action_id: str, identity: Identity) -> ActionRecord:
        if identity.channel != "feishu" or identity.owner.kind != "group":
            raise RequestUnavailableError
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM xiaowei_action WHERE action_id = :id AND "
                            "owner_id = :owner "
                            "AND expires_at > :now"
                        ),
                        {"id": action_id, "owner": identity.owner.id, "now": self._clock()},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise RequestUnavailableError
        return ActionRecord(**dict(row))

    async def action_delivery_state(self, action_id: str, owner_id: str) -> tuple[str, str | None]:
        """投递时读取当前动作事实；过期或缺失的提案不能继续展示批准命令。"""
        try:
            async with self._engine.connect() as conn:
                row = (
                    (
                        await conn.execute(
                            text(
                                "SELECT state, failure_code, approval_expires_at, expires_at"
                                " FROM xiaowei_action WHERE action_id=:id AND owner_id=:owner"
                            ),
                            {"id": action_id, "owner": owner_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
        except (OSError, SQLAlchemyError):
            raise ChannelStoreUnavailableError from None
        if row is None:
            return "unavailable", None
        if row["state"] == "pending" and self._clock() >= min(
            row["approval_expires_at"], row["expires_at"]
        ):
            return "expired", None
        return str(row["state"]), row["failure_code"]

    async def _settle_unresumable_actions(
        self,
        conn: AsyncConnection,
        owner_id: str,
        turn_id: str,
        *,
        keep: Sequence[str] = (),
    ) -> None:
        """请求终态或暂停与未绑定候选同事务落定；已开始的写不受影响。"""
        await conn.execute(
            _SETTLE_UNRESUMABLE,
            {"owner": owner_id, "turn": turn_id, "keep": tuple(keep), "now": self._clock()},
        )

    async def proposal_request(self, action: ActionRecord) -> RequestRecord:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM xiaowei_request WHERE turn_id = :turn "
                            "AND channel = 'feishu' AND owner_kind = 'group' AND owner_id = :owner "
                            "AND expires_at > :now"
                        ),
                        {
                            "turn": action.proposal_turn_id,
                            "owner": action.owner_id,
                            "now": self._clock(),
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise RequestUnavailableError
        return self._record(dict(row))

    async def proposal_request_for_turn(
        self, identity: Identity, *, approval: ApprovedAction | None = None
    ) -> RequestRecord:
        async with self._engine.connect() as conn:
            row = await self._proposal_request(conn, identity, approval)
        if row is None:
            raise RequestUnavailableError
        return self._record(dict(row))

    async def _proposal_request(
        self, conn: AsyncConnection, identity: Identity, approval: ApprovedAction | None
    ) -> Mapping[str, Any] | None:
        if identity.channel != "feishu" or identity.owner.kind != "group":
            raise RequestUnavailableError
        approver = approval.approver if approval is not None else None
        # 原请求paused时只接受已消费检查点的当前审批请求，不能拿历史批准再次提出。
        row = (
            (
                await conn.execute(
                    text("""
                    SELECT r.* FROM xiaowei_request r WHERE r.turn_id=:turn
                    AND r.session_id=:session AND r.subject_id=:actor AND r.owner_id=:owner
                    AND r.owner_kind='group' AND r.channel='feishu' AND r.expires_at>:now
                    AND (r.state='running' OR (r.state='paused' AND EXISTS(
                        SELECT 1 FROM xiaowei_action a
                        JOIN xiaowei_action_run n ON n.turn_id=a.proposal_turn_id
                            AND n.owner_id=a.owner_id
                        JOIN xiaowei_request b ON b.turn_id=a.approval_turn_id
                            AND b.owner_id=a.owner_id AND b.owner_kind='group'
                            AND b.channel='feishu'
                        WHERE a.action_id=:approved AND a.attempt=:attempt
                            AND a.proposal_turn_id=r.turn_id AND a.owner_id=r.owner_id
                            AND a.proposal_session_id=r.session_id AND a.requester_id=r.subject_id
                            AND a.approver_id=:approver AND a.approval_turn_id=:approval_turn
                            AND n.consumed=true AND b.subject_id=a.approver_id
                            AND b.state='running' AND b.expires_at>:now
                    )))
                """),
                    {
                        "turn": identity.turn_id,
                        "session": identity.session_id,
                        "actor": identity.subject_id,
                        "owner": identity.owner.id,
                        "now": self._clock(),
                        "approved": approval.action_id if approval else None,
                        "attempt": approval.attempt if approval else None,
                        "approver": approver.subject_id if approver else None,
                        "approval_turn": approver.turn_id if approver else None,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else dict(row)

    async def admit_action(self, action: ActionRecord) -> None:
        """回读后、写 I/O 前复核实例、尝试与期限；过期或已恢复的尝试不能执行。"""
        self._require_ready()
        if self._instance is None:
            raise NotReadyError
        async with self._engine.begin() as conn:
            await self._admit(conn)
            identity = Identity(
                subject_id=action.requester_id,
                session_id=action.proposal_session_id,
                turn_id=action.proposal_turn_id,
                owner=Owner(kind="group", id=action.owner_id),
                channel="feishu",
            )
            if not await self._native_session(conn, identity):
                raise ActionAdmissionRejectedError("action_session_invalid")
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT approval_expires_at, expires_at FROM xiaowei_action"
                            " WHERE action_id = :id AND state = 'executing' "
                            "AND attempt = :attempt AND approver_id = :approver AND "
                            "approval_turn_id = :turn"
                        ),
                        {
                            "id": action.action_id,
                            "attempt": action.attempt,
                            "approver": action.approver_id,
                            "turn": action.approval_turn_id,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise RequestUnavailableError
            if self._clock() >= min(row["approval_expires_at"], row["expires_at"]):
                raise ActionAdmissionRejectedError("action_expired")

    async def finish_action(
        self,
        action: ActionRecord,
        state: Literal["succeeded", "unknown", "rejected"],
        result: str | None = None,
        *,
        failure_code: str | None = None,
    ) -> None:
        if result is not None and len(result.encode()) > 128_000:
            raise RequestUnavailableError
        with self._critical("action_finish_failed", "动作结果无法保存"):
            async with self._engine.begin() as conn:
                await self._admit(conn)
                changed = (
                    await conn.execute(
                        text(
                            "UPDATE xiaowei_action SET state = :state, result = "
                            ":result, failure_code=:failure_code, updated_at = :now "
                            "WHERE action_id = :id AND state = 'executing' AND attempt = :attempt"
                        ),
                        {
                            "id": action.action_id,
                            "attempt": action.attempt,
                            "state": state,
                            "result": result,
                            "failure_code": failure_code if state == "rejected" else None,
                            "now": self._clock(),
                        },
                    )
                ).rowcount
                if changed != 1:
                    self._readiness.lock("action_attempt_lost")
                    raise NotReadyError

    async def native_run(self, action: ActionRecord) -> NativeRunRecord:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT * FROM xiaowei_action_run WHERE turn_id=:turn AND"
                            " owner_id=:owner"
                        ),
                        {"turn": action.proposal_turn_id, "owner": action.owner_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise RequestUnavailableError
        return NativeRunRecord(**dict(row))

    async def save_pause(
        self,
        record: RequestRecord,
        pause: PausedTurn,
        *,
        failure_code: FailureCode = RESULT_NOT_SAVED,
    ) -> RequestRecord:
        """SDK快照/原轮账本与材料请求一起提交，暂停不提交SDK历史。"""
        from pydantic import TypeAdapter

        from xiaowei.governance import TurnLedger

        self._require_ready()
        if record.owner.kind != "group" or record.channel != "feishu":
            raise RequestUnavailableError
        context = pause.context.model_dump_json()
        ledger = TypeAdapter(TurnLedger).dump_json(pause.ledger).decode()
        content = pause.material.model_dump_json()
        if len(content.encode()) > self._max_answer_bytes:
            await self._fail_after_commit(
                record, failure_code, settle_turn_id=pause.context.identity.turn_id
            )
            raise ResultNotSavedError
        params = {
            "turn": pause.context.identity.turn_id,
            "owner": record.owner.id,
            "ctx": context,
            "ledger": ledger,
            "state": pause.run_state,
            "message": pause.message,
            "binding": pause.binding,
            "checks": pause.scope_checks,
            "material": record.turn_id,
            "expires": pause.expires_at,
            "prior": pause.prior_version,
            "prior_binding": pause.prior_binding,
        }
        try:
            with self._critical("resume_save_failed", "暂停状态未保存"):
                async with self._engine.begin() as conn:
                    await self._admit(conn)
                    if not await self._native_session(conn, pause.context.identity):
                        await self._reject_native(
                            conn,
                            record.owner.id,
                            pause.context.identity.turn_id,
                            "action_session_invalid",
                        )
                        code = (
                            "action_session_invalid" if pause.prior_version == 0 else failure_code
                        )
                        failed = await conn.execute(
                            _FAIL,
                            {
                                **_keys(record),
                                "code": code,
                                "session_state": None,
                                "now": self._clock(),
                            },
                        )
                        if failed.rowcount != 1:
                            self._readiness.lock("resume_save_failed")
                            raise NotReadyError
                        return replace(record, state="failed", failure_code=code)
                    if pause.prior_version == 0:
                        saved = await conn.execute(
                            text(
                                "INSERT INTO xiaowei_action_run VALUES"
                                " (:turn,:owner,:ctx,:state,:ledger,"
                                ":message,:binding,:checks,1,false,:material,:expires) ON CONFLICT"
                                " DO NOTHING"
                            ),
                            params,
                        )
                    else:
                        saved = await conn.execute(
                            text(
                                "UPDATE xiaowei_action_run SET context=:ctx, run_state=:state,"
                                " ledger=:ledger,"
                                " scope_checks=:checks, material_turn_id=:material,"
                                " binding=:binding,"
                                " expires_at=:expires,"
                                " consumed=false, version=version+1 WHERE turn_id=:turn AND"
                                " owner_id=:owner"
                                " AND version=:prior AND consumed=true AND binding=:prior_binding"
                            ),
                            params,
                        )
                    if saved.rowcount != 1:
                        raise RequestUnavailableError
                    changed = await conn.execute(
                        text(
                            "UPDATE xiaowei_request SET state='paused', answer=:answer,"
                            " updated_at=:now"
                            " WHERE channel=:channel AND request_key=:request_key AND"
                            " owner_kind=:owner_kind"
                            " AND owner_id=:owner_id AND subject_id=:subject_id"
                            " AND turn_id=:turn_id"
                            " AND state='running'"
                        ),
                        {**_keys(record), "answer": content, "now": self._clock()},
                    )
                    if changed.rowcount != 1:
                        raise RequestUnavailableError
                    await self._settle_unresumable_actions(
                        conn,
                        record.owner.id,
                        pause.context.identity.turn_id,
                        keep=pause.approvable_action_ids,
                    )
        except RequestUnavailableError:
            # 确定未保存的拒绝也须落定请求；不能留下running持续挡住/新建。
            await self._fail_after_commit(
                record, failure_code, settle_turn_id=pause.context.identity.turn_id
            )
            raise ResultNotSavedError from None
        return replace(
            record,
            state="paused",
            answer=pause.material.answer,
            context_evidence=pause.material.context_evidence,
            monitoring_failures=pause.material.monitoring_failures,
            run_turn_id=pause.context.identity.turn_id,
        )

    async def _native_session(self, conn: AsyncConnection, identity: Identity) -> bool:
        # 与/新建保持相同锁顺序：渠道映射先、应用Session后；版本消费/claim仍在本事务。
        current = await conn.scalar(
            text(
                "SELECT 1 FROM xiaowei_channel_session WHERE session_id=:session AND"
                " owner_kind='group'"
                " AND owner_id=:owner AND channel='feishu' AND state='current' AND expires_at>:now"
                " FOR UPDATE"
            ),
            {"session": identity.session_id, "owner": identity.owner.id, "now": self._clock()},
        )
        session = await conn.scalar(
            text(
                "SELECT 1 FROM xiaowei_session WHERE session_id=:session AND owner_kind='group'"
                " AND owner_id=:owner AND channel='feishu' AND state='active' AND expires_at>:now"
                " FOR UPDATE"
            ),
            {"session": identity.session_id, "owner": identity.owner.id, "now": self._clock()},
        )
        return bool(current == 1 and session == 1)

    async def reject_native(self, action: ActionRecord, code: CallerFailureCode) -> None:
        """确定未执行的原轮候选作废；不改已开始的执行事实。"""
        self._require_ready()
        if code not in {"action_requester_invalid", "action_session_invalid", "action_expired"}:
            raise ValueError("作废原因不合约")
        with self._critical("action_reject_failed", "动作作废无法保存"):
            async with self._engine.begin() as conn:
                await self._admit(conn)
                await self._reject_native(conn, action.owner_id, action.proposal_turn_id, code)

    async def _reject_native(
        self, conn: AsyncConnection, owner_id: str, turn_id: str, code: str
    ) -> None:
        await conn.execute(
            text(
                "SELECT 1 FROM xiaowei_action_run WHERE turn_id=:turn "
                "AND owner_id=:owner FOR UPDATE"
            ),
            {"turn": turn_id, "owner": owner_id},
        )
        await conn.execute(
            text(
                "UPDATE xiaowei_action SET state='rejected', failure_code=:code, updated_at=:now"
                " WHERE owner_id=:owner AND proposal_turn_id=:turn AND state='pending'"
            ),
            {
                "owner": owner_id,
                "turn": turn_id,
                "code": code,
                "now": self._clock(),
            },
        )
        await conn.execute(
            text(
                "UPDATE xiaowei_action_run SET consumed=true WHERE turn_id=:turn AND"
                " owner_id=:owner"
            ),
            {"turn": turn_id, "owner": owner_id},
        )

    async def claim_native_action(
        self,
        action: ActionRecord,
        run: NativeRunRecord,
        original: Identity,
        approver: Identity,
        *,
        approve: bool = True,
    ) -> tuple[ActionRecord | None, CallerFailureCode | None]:
        self._require_ready()
        if self._instance is None or original.owner != approver.owner:
            raise RequestUnavailableError
        attempt = secrets.token_hex(_ATTEMPT_BYTES)
        with self._critical("action_claim_failed", "动作占用未保存"):
            async with self._engine.begin() as conn:
                await self._admit(conn)
                if not await self._native_session(conn, original):
                    await self._reject_native(
                        conn, action.owner_id, action.proposal_turn_id, "action_session_invalid"
                    )
                    return None, "action_session_invalid"
                current = (
                    (
                        await conn.execute(
                            text(
                                "SELECT * FROM xiaowei_action_run WHERE turn_id=:turn AND"
                                " owner_id=:owner"
                                " FOR UPDATE"
                            ),
                            {"turn": original.turn_id, "owner": original.owner.id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if current is None or current["version"] != run.version or current["consumed"]:
                    return None, None
                if self._clock() >= min(action.approval_expires_at, current["expires_at"]):
                    await self._reject_native(
                        conn, action.owner_id, action.proposal_turn_id, "action_expired"
                    )
                    return None, "action_expired"
                row = (
                    (
                        await conn.execute(
                            text(
                                "UPDATE xiaowei_action a SET state=:state, failure_code=:cause,"
                                " attempt=:attempt,"
                                " approver_id=:actor, approval_turn_id=:approval, updated_at=:now"
                                " WHERE action_id=:id AND owner_id=:owner AND state='pending'"
                                " AND binding=:binding AND plan=:plan AND expires_at>:now"
                                " AND EXISTS(SELECT 1 FROM xiaowei_request r WHERE"
                                " r.turn_id=:material"
                                " AND r.owner_id=:owner AND r.state='paused' AND ("
                                " (r.delivery_version=0 AND r.delivery='sent') OR "
                                " (r.delivery_version=1 AND EXISTS(SELECT 1 "
                                "FROM xiaowei_delivery_part p "
                                " WHERE p.turn_id=r.turn_id AND p.kind='approval' "
                                "AND p.send_state='sent')))"
                                " AND r.channel='feishu' AND r.expires_at>:now)"
                                " AND EXISTS(SELECT 1 FROM xiaowei_request r WHERE"
                                " r.turn_id=:approval"
                                " AND r.owner_id=:owner AND r.subject_id=:actor AND"
                                " r.state='running'"
                                " AND r.channel='feishu' AND r.expires_at>:now) RETURNING a.*"
                            ),
                            {
                                "attempt": attempt,
                                "state": "executing" if approve else "rejected",
                                "cause": None if approve else "action_rejected",
                                "actor": approver.subject_id,
                                "approval": approver.turn_id,
                                "now": self._clock(),
                                "id": action.action_id,
                                "owner": original.owner.id,
                                "binding": action.binding,
                                "plan": action.plan,
                                "material": current["material_turn_id"],
                            },
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    return None, None
                await conn.execute(
                    text(
                        "UPDATE xiaowei_action_run SET consumed=true WHERE turn_id=:turn AND"
                        " version=:version"
                    ),
                    {"turn": original.turn_id, "version": run.version},
                )
        return ActionRecord(**dict(row)), None

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
        answer, context, run_turn_id = None, None, None
        monitoring_failures: tuple[MonitoringFailure, ...] = ()
        if row["answer"] is not None:
            raw = row["answer"]
            if len(raw.encode()) > self._max_answer_bytes:
                raise RequestUnavailableError
            answer, context, monitoring_failures = _saved_answer(raw)
            try:
                run_turn_id = TurnAnswer.model_validate_json(raw).run_turn_id
            except ValidationError:
                pass  # 已由_saved_answer验证的旧AgentAnswer格式。
        if row["failure_code"] is not None and row["failure_code"] not in _FAILURE_CODES:
            raise RequestUnavailableError
        snapshot = _read_session_state(row["session_state"], row["state"], row["failure_code"])
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
            session_state=snapshot,
            run_turn_id=run_turn_id,
            delivery_version=row.get("delivery_version", 0),
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


def _read_session_state(
    raw: str | None,
    state: str,
    failure_code: FailureCode | None,
) -> SessionSnapshot | None:
    if raw is None:
        return None  # 旧记录及没有进入 PolicySession 的请求不猜原因。
    if not isinstance(raw, str) or len(raw.encode()) > SESSION_STATE_MAX_BYTES:
        raise RequestUnavailableError
    try:
        snapshot = SessionSnapshot.model_validate_json(raw)
    except ValidationError:
        raise RequestUnavailableError from None
    if state == "completed":
        if snapshot.phase != "after_commit" or snapshot.reason is not None:
            raise RequestUnavailableError
    elif state == "failed":
        if (
            failure_code != "session_failed"
            or snapshot.reason is None
            or snapshot.phase == "after_commit"
        ):
            raise RequestUnavailableError
    else:
        raise RequestUnavailableError
    return snapshot


def _session_state(
    snapshot: SessionSnapshot | None,
    state: str,
    code: FailureCode | None,
) -> str | None:
    if snapshot is None:
        return None
    try:
        raw = snapshot.model_dump_json()
    except (TypeError, ValueError):
        raise RequestUnavailableError from None
    _read_session_state(raw, state, code)  # 写入也验证，model_copy 不得绕过闭集和容量。
    return raw


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
