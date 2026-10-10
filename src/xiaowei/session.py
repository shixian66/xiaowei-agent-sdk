"""SDK Session 的薄策略包装：写入前暂存与过滤，校验后提交，回放前复核。

SDK 在一轮内把用户输入、工具调用/结果与最终消息写入 Session，早于应用校验最终回答；因此
``add_items`` 只把按 Session 策略转换后的副本暂存在内存，``commit_validated`` 重新校验最终
回答后才委托底层写入，失败路径调用 ``discard_pending``。工具结果只保存证据引用，回放时经
Evidence 读取边界按当前权限、目标范围、策略与过期取回 Session 投影（整段历史作为一批，依赖
按目标合并复核一次）；任一条历史确定不能安全回放时整段拒绝并提示新建会话，不裁掉半组工具项、
不以无来源文字替代失效证据。只是暂时无法复核（目标不可达、超时）时抛出
``SessionUnverifiableError``：本轮不运行或不提交，会话状态与历史不变，之后的新请求重新复核。证据须由历史
中同一次调用（调用标识、函数名、参数）生成，暂存与回放都经 Evidence 读取边界核对。

应用表 ``xiaowei_session`` 记录会话归属（owner：个人或指定群，与本轮发起人分开）、运行绑定
（应用传入 Profile 与数据策略的组合指纹，列名沿用 ``profile_fingerprint``）、失效时间、
已提交轮数与状态；只有底层 Session 为空时才登记新会话，缺少元数据的已有历史不被任何身份
认领。SDK 表与应用表不共事务：提交前先把状态置为 ``writing``，底层写入与元数据更新都成功后
才回到 ``active``；任一步失败会话保持不可回放，不自动重跑工具补偿。

会话关闭与维护清理也只在本模块：``close_sessions`` 在调用方的应用表事务中把会话置为不可
回放（不删历史）；``close_interrupted_sessions`` 供启动恢复持久关闭，元数据尚未登记时写入已关闭
的占位；``cleanup_expired`` 按显式批次、数据库过期与状态条件删除过期对象，历史经
SDK 公共 ``clear_session()`` 清除，任一步失败即停下。
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from agents import TResponseInputItem
from agents.extensions.memory import SQLAlchemySession
from agents.memory import Session, SessionSettings
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import TextClause, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from xiaowei.evidence import (
    AnswerRejectedError,
    EvidenceError,
    EvidenceStore,
    EvidenceUnavailableError,
    EvidenceUnverifiableError,
)
from xiaowei.models import AgentAnswer, MonitoringFailure, Owner, RunContext, ToolCall, TurnAnswer

SessionState = Literal["active", "writing", "sealed", "closed"]

# 供应商附加字段中唯一保存的 key；上限与 Vertex 适配器的 signature 上限一致（UTF-8 字节）。
_SIGNATURE = "thought_signature"
_MAX_SIGNATURE_BYTES = 65_536

# 不含可回放证据的工具输出（治理拒绝、执行失败等）以固定文字保存，不保留原始错误文本。
NO_EVIDENCE_OUTPUT = "工具未返回可回放的证据"
_EVIDENCE_REF = "xiaowei_evidence_ref"
_DROPPED_TYPES = frozenset({"reasoning"})

_INSERT_IF_ABSENT = text(
    """
    INSERT INTO xiaowei_session (
        session_id, owner_kind, owner_id, channel, profile_fingerprint, created_at, expires_at,
        turns, state
    ) VALUES (
        :session_id, :owner_kind, :owner_id, :channel, :profile_fingerprint, :created_at,
        :expires_at, 0, 'active'
    )
    ON CONFLICT (session_id) DO NOTHING
    """
)
_SELECT = text(
    """
    SELECT owner_kind, owner_id, channel, profile_fingerprint, expires_at, turns, state
    FROM xiaowei_session WHERE session_id = :session_id
    """
)
# 状态迁移都带归属条件；影响行数为 0 表示状态已被改变或不属于当前身份。
_TRANSITION = text(
    """
    UPDATE xiaowei_session SET state = :to_state, turns = turns + :add_turns
    WHERE session_id = :session_id AND owner_kind = :owner_kind AND owner_id = :owner_id
      AND channel = :channel
      AND state = :from_state
    """
)
_CLOSE = text(
    """
    UPDATE xiaowei_session SET state = 'closed'
    WHERE session_id = :session_id AND owner_kind = :owner_kind AND owner_id = :owner_id
      AND channel = :channel
    """
)


# 维护清理的条件（候选、关闭与删除都重新核对）：会话已过期、不是任何渠道的 current 会话，
# 且没有未结束或正在发送的请求。
_CLEANUP_CANDIDATES = text(
    """
    SELECT s.session_id FROM xiaowei_session s
    WHERE s.expires_at <= :now
      AND NOT EXISTS (
          SELECT 1 FROM xiaowei_channel_session m
          WHERE m.session_id = s.session_id AND m.state = 'current'
      )
      AND NOT EXISTS (
          SELECT 1 FROM xiaowei_request r
          WHERE r.session_id = s.session_id
            AND (r.state IN ('accepted', 'running') OR r.delivery = 'sending')
      )
    ORDER BY s.expires_at, s.session_id LIMIT :limit
    """
)
_CLEANUP_CLOSE = text(
    """
    UPDATE xiaowei_session s SET state = 'closed'
    WHERE s.session_id = :session_id
      AND s.expires_at <= :now
      AND NOT EXISTS (
          SELECT 1 FROM xiaowei_channel_session m
          WHERE m.session_id = s.session_id AND m.state = 'current'
      )
      AND NOT EXISTS (
          SELECT 1 FROM xiaowei_request r
          WHERE r.session_id = s.session_id
            AND (r.state IN ('accepted', 'running') OR r.delivery = 'sending')
      )
    """
)
# 过期请求只删除已结束且不在发送中的；未过期请求保留（仍可重读或显式重发）。
_DELETE_EXPIRED_REQUESTS = text(
    """
    DELETE FROM xiaowei_request WHERE session_id = :session_id AND expires_at <= :now
      AND state IN ('completed', 'failed', 'interrupted') AND delivery <> 'sending'
    """
)
_DELETE_EXPIRED_EVIDENCE = text(
    "DELETE FROM xiaowei_evidence WHERE session_id = :session_id AND expires_at <= :now"
)
_DELETE_EXPIRED_ACTIONS = text(
    """
    DELETE FROM xiaowei_action WHERE action_id IN (
        SELECT action_id FROM xiaowei_action WHERE expires_at <= :now AND state <> 'executing'
        ORDER BY expires_at, action_id LIMIT :limit FOR UPDATE SKIP LOCKED
    )
    """
)
# 元数据与映射在最后删除：会话仍须已关闭、仍满足清理条件，且不再有任何请求记录。
_DELETE_SESSION = text(
    """
    DELETE FROM xiaowei_session s
    WHERE s.session_id = :session_id AND s.state = 'closed'
      AND NOT EXISTS (SELECT 1 FROM xiaowei_request r WHERE r.session_id = s.session_id)
      AND s.expires_at <= :now
      AND NOT EXISTS (
          SELECT 1 FROM xiaowei_channel_session m
          WHERE m.session_id = s.session_id AND m.state = 'current'
      )
      AND NOT EXISTS (
          SELECT 1 FROM xiaowei_request r
          WHERE r.session_id = s.session_id
            AND (r.state IN ('accepted', 'running') OR r.delivery = 'sending')
      )
    """
)
_DELETE_MAPPINGS = text(
    """
    DELETE FROM xiaowei_channel_session WHERE session_id = :session_id AND state = 'retired'
      AND NOT EXISTS (SELECT 1 FROM xiaowei_session s WHERE s.session_id = :session_id)
    """
)
# 从未进入 PolicySession 的渠道会话（Agent 运行前新建会话、繁忙或恢复的终态请求）没有会话
# 元数据，也就没有 SDK 历史：映射已过期、已退役且没有运行中或发送中的请求时，删除其已过期
# 的终态请求与证据，再在不再有任何请求时删除映射。不调用 SDK clear_session()。
_UNREGISTERED_CANDIDATES = text(
    """
    SELECT m.session_id FROM xiaowei_channel_session m
    WHERE m.state = 'retired' AND m.expires_at <= :now
      AND NOT EXISTS (SELECT 1 FROM xiaowei_session s WHERE s.session_id = m.session_id)
      AND NOT EXISTS (
          SELECT 1 FROM xiaowei_request r
          WHERE r.session_id = m.session_id
            AND (r.state IN ('accepted', 'running') OR r.delivery = 'sending')
      )
    ORDER BY m.expires_at, m.session_id LIMIT :limit
    """
)
_DELETE_UNREGISTERED_MAPPING = text(
    """
    DELETE FROM xiaowei_channel_session m
    WHERE m.session_id = :session_id AND m.state = 'retired' AND m.expires_at <= :now
      AND NOT EXISTS (SELECT 1 FROM xiaowei_session s WHERE s.session_id = m.session_id)
      AND NOT EXISTS (SELECT 1 FROM xiaowei_request r WHERE r.session_id = m.session_id)
    """
)


class SessionError(Exception):
    """Session 策略边界错误；信息固定，不含历史内容、证据或连接信息。"""


class SessionUnavailableError(SessionError):
    """历史不能继续使用：归属或 Profile 不符、已过期、已封存/关闭、证据失效或结构异常。"""

    def __init__(self, message: str = "会话历史不可继续使用，请新建会话") -> None:
        super().__init__(message)


class SessionUnverifiableError(SessionError):
    """暂时无法确认历史或本轮证据依赖的数据当前仍可读；会话保持可用，本轮不提交。"""

    def __init__(self) -> None:
        super().__init__("暂时无法确认数据当前权限，本轮未执行或未保存；会话保留，请稍后重试")


class SessionLimitError(SessionUnavailableError):
    """历史轮数或字节达到上限；在新一轮模型调用前拒绝。"""

    def __init__(self) -> None:
        super().__init__("会话历史已达上限，请新建会话")


class SessionItemRejectedError(SessionError):
    """本轮内容没有安全的保存形式；本轮不得提交。"""


class SessionStoreError(SessionError):
    """会话存储不可用或写入中断；写入中断后该会话不再回放。"""


class SessionLimits(BaseModel):
    """可信配置提供的历史上限与会话保留期；与 SDK ``max_turns``（单轮模型调用数）无关。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_history_turns: int = Field(gt=0)
    max_history_bytes: int = Field(gt=0)
    retention_seconds: int = Field(gt=0)


class SessionInputPolicy(BaseModel):
    """可信配置的用户输入准入策略：用户文字进入会话的字节上限与禁止保存的模式。

    SDK 在首个模型调用前写入用户输入，拒绝即本轮零模型调用；回放时按当前策略复核。
    代码只能按确定的模式判断，不能识别任意自由文字中的敏感内容。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_bytes: int = Field(gt=0)
    forbidden_patterns: tuple[str, ...] = ()

    @field_validator("forbidden_patterns")
    @classmethod
    def _compilable(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error:
                raise ValueError("禁止模式不是合法的正则表达式") from None
        return patterns

    def admits(self, text: str) -> bool:
        return len(text.encode()) <= self.max_bytes and not any(
            re.search(pattern, text) for pattern in self.forbidden_patterns
        )


class PolicySession:
    """实现 SDK ``Session`` 协议，委托底层 Session 存储；一个对象只服务一轮。"""

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        inner: Session,
        context: RunContext,
        evidence: EvidenceStore,
        profile_fingerprint: str,
        limits: SessionLimits,
        *,
        input_policy: SessionInputPolicy,
        engine: AsyncEngine,
        clock: Callable[[], datetime],
    ) -> None:
        if inner.session_id != context.identity.session_id:
            raise ValueError("底层 Session 标识必须与可信身份的会话一致")
        if not profile_fingerprint:
            raise ValueError("Profile 指纹不能为空")
        self.session_id = inner.session_id
        self._inner = inner
        self._ctx = context
        self._evidence = evidence
        self._profile = profile_fingerprint
        self._limits = limits
        self._input_policy = input_policy
        self._engine = engine
        self._clock = clock
        self._pending: list[TResponseInputItem] = []
        self._replayed: dict[str, None] = {}

    @property
    def replayed_evidence(self) -> tuple[str, ...]:
        """本轮经 ``get_items`` 回放给模型的证据标识（按首次出现顺序），来自已保存的历史。"""
        return tuple(self._replayed)

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """回放：整段复核后返回已提交历史与本轮暂存项；达到上限或不可回放时拒绝。"""
        if limit is not None:
            # 按条数截断会拆开工具调用/结果配对；历史大小由 SessionLimits 约束。
            raise SessionError("会话历史不支持按条数截断")
        turns = await self._open()
        if turns >= self._limits.max_history_turns:
            raise SessionLimitError
        replay = await self._replay([*await self._committed(), *self._pending], seen=self._replayed)
        if _size(replay) >= self._limits.max_history_bytes:
            raise SessionLimitError
        return replay

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """转换为保存形式后暂存；不写入底层 Session，也不修改 SDK 仍在使用的对象。"""
        # 逐项暂存：工具结果需要找到同批次先到的调用项。失败时整轮由应用丢弃。
        for item in items:
            stored = await self._stored_form(item)
            if stored is not None:
                self._pending.append(stored)

    async def pop_item(self) -> TResponseInputItem | None:
        """只能撤回本轮暂存项；已提交历史不能逐条删除，否则会破坏配对与轮数记录。"""
        if not self._pending:
            raise SessionError("已提交的会话历史不能逐条删除")
        return self._pending.pop()

    async def clear_session(self) -> None:
        """先把会话关闭为不可回放，再清除底层历史；清除失败时会话仍不可回放。"""
        self._pending.clear()
        await self._claim()
        if await self._execute(_CLOSE, self._owner()) == 0:
            raise SessionUnavailableError
        try:
            await self._inner.clear_session()
        except (OSError, SQLAlchemyError):
            raise SessionStoreError("会话历史清除失败，该会话已关闭") from None

    async def discard_pending(self) -> None:
        """丢弃本轮暂存项；运行失败、取消或最终回答未通过校验时调用。"""
        self._pending.clear()

    async def commit_validated(
        self,
        context_evidence: Sequence[str],
        *,
        monitoring_failures: Sequence[MonitoringFailure] = (),
    ) -> None:
        """校验本轮最终回答与完整历史后写入底层 Session；任何失败都不保留本轮暂存项。

        ``context_evidence`` 是应用记录的本轮模型可见证据（见 ``TurnAnswer``），与最终回答一起校验。
        """
        pending, self._pending = self._pending, []
        await self._validate_final_answer(pending, context_evidence, monitoring_failures)
        turns = await self._open()
        if turns >= self._limits.max_history_turns:
            raise SessionLimitError
        replay = await self._replay([*await self._committed(), *pending])
        if _size(replay) > self._limits.max_history_bytes:
            # 本轮已执行的工具不重跑，也不裁剪历史凑数；封存后提示新建。
            await self._transition("active", "sealed")
            raise SessionLimitError
        if not await self._transition("active", "writing"):
            raise SessionUnavailableError
        try:
            await self._inner.add_items(pending)
        except (OSError, SQLAlchemyError):
            raise SessionStoreError("会话保存失败，该会话已停止使用，请新建会话") from None
        if not await self._transition("writing", "active", add_turns=1):
            raise SessionStoreError("会话保存失败，该会话已停止使用，请新建会话")

    async def _validate_final_answer(
        self,
        pending: list[TResponseInputItem],
        context_evidence: Sequence[str],
        monitoring_failures: Sequence[MonitoringFailure],
    ) -> None:
        """最终持久化复用 Evidence 回答验证器：最后一项须是通过校验的 ``AgentAnswer``。"""
        final = pending[-1] if pending else None
        if final is None or _role(final) != "assistant":
            raise SessionItemRejectedError("本轮没有可提交的最终回答")
        try:
            answer = AgentAnswer.model_validate_json(cast(dict[str, str], final)["content"])
            turn = TurnAnswer(
                answer=answer,
                context_evidence=tuple(context_evidence),
                monitoring_failures=tuple(monitoring_failures),
            )
            await self._evidence.validate_answer(turn, self._ctx)
        except (ValidationError, AnswerRejectedError):
            raise SessionItemRejectedError("最终回答未通过校验，本轮不保存") from None
        except EvidenceUnverifiableError:
            raise SessionUnverifiableError from None

    async def _open(self) -> int:
        """返回可继续使用的会话已提交轮数；归属、Profile、状态或期限不符时拒绝。"""
        row = await self._claim()
        # 先判断状态：恢复写入的已关闭占位没有指纹，不能被报成“配置已变化”。
        if row["state"] != "active" or self._clock() >= row["expires_at"]:
            raise SessionUnavailableError
        if row["profile_fingerprint"] != self._profile:
            raise SessionUnavailableError("模型配置或数据策略已变化，请新建会话")
        return cast(int, row["turns"])

    async def _claim(self) -> Mapping[str, Any]:
        """读取属于当前身份的会话元数据；不存在时只为空的底层 Session 登记。

        元数据缺失而底层已有历史（应用表丢失、局部恢复、标识复用）时无法确认归属，拒绝
        认领，要求新建或走单独的恢复流程。
        """
        row = await self._load()
        if row is None:
            try:
                existing = await self._inner.get_items(limit=1)
            except (OSError, SQLAlchemyError):
                raise SessionStoreError("会话存储不可用") from None
            if existing:
                raise SessionUnavailableError
            await self._execute(_INSERT_IF_ABSENT, self._new_row())
            row = await self._load()
        identity = self._ctx.identity
        if (
            row is None
            or row["owner_kind"] != identity.owner.kind
            or row["owner_id"] != identity.owner.id
            or row["channel"] != identity.channel
        ):
            raise SessionUnavailableError
        return row

    async def _load(self) -> Mapping[str, Any] | None:
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(_SELECT, {"session_id": self.session_id})
                row = result.mappings().one_or_none()
        except (OSError, SQLAlchemyError):
            raise SessionStoreError("会话存储不可用") from None
        return None if row is None else dict(row)

    async def _committed(self) -> list[TResponseInputItem]:
        try:
            return await self._inner.get_items()
        except (OSError, SQLAlchemyError):
            raise SessionStoreError("会话存储不可用") from None

    async def _stored_form(self, item: TResponseInputItem) -> TResponseInputItem | None:
        """按类型白名单重建保存形式；工具结果只保留证据引用。无安全形式时拒绝本轮。"""
        raw = cast(dict[str, Any], item)
        kind = raw.get("type", "message")
        if kind in _DROPPED_TYPES:
            # 推理项是模型内部内容，不进入历史；回放不依赖它。
            return None
        if kind == "message":
            return _message(raw, self._input_policy)
        if kind == "function_call":
            return _function_call(raw)
        if kind == "function_call_output":
            call_id = _text(raw.get("call_id"))
            output = raw.get("output")
            evidence_id = _envelope_evidence_id(output)
            if evidence_id is None:
                return _output(call_id, NO_EVIDENCE_OUTPUT)
            call = _tool_call(self._pending_call(call_id))
            try:
                if call is None:
                    raise EvidenceUnavailableError
                await self._evidence.project(evidence_id, self._ctx, "session", call=call)
            except EvidenceUnavailableError:
                # 模型已看到该结果，后续文字可能引用它；不能只丢掉这一项继续保存。
                raise SessionItemRejectedError("本轮工具结果无法保存到会话") from None
            except EvidenceUnverifiableError:
                raise SessionUnverifiableError from None
            return _output(call_id, json.dumps({_EVIDENCE_REF: evidence_id}))
        raise SessionItemRejectedError("本轮包含不支持保存的会话内容")

    def _pending_call(self, call_id: str) -> dict[str, Any] | None:
        for item in reversed(self._pending):
            raw = cast(dict[str, Any], item)
            if raw.get("type") == "function_call" and raw.get("call_id") == call_id:
                return raw
        return None

    async def _replay(
        self, stored: list[TResponseInputItem], *, seen: dict[str, None] | None = None
    ) -> list[TResponseInputItem]:
        """把保存形式转换为回放形式：证据引用经读取边界（一批）换成当前 Session 投影。

        给出 ``seen`` 时，回放成功后把其中的证据标识记入它（交给模型的历史证据）。
        """
        replay: list[TResponseInputItem] = []
        references: list[tuple[int, str, ToolCall]] = []
        open_calls: dict[str, dict[str, Any]] = {}
        closed_calls: set[str] = set()
        try:
            for item in stored:
                raw = cast(dict[str, Any], item)
                kind = raw.get("type", "message")
                if kind == "message":
                    replay.append(_message(raw, self._input_policy))
                elif kind == "function_call":
                    call_id = _text(raw.get("call_id"))
                    if call_id in open_calls or call_id in closed_calls:
                        raise SessionUnavailableError
                    open_calls[call_id] = raw
                    replay.append(_function_call(raw))
                elif kind == "function_call_output":
                    call_id = _text(raw.get("call_id"))
                    if call_id not in open_calls:
                        raise SessionUnavailableError
                    call = _tool_call(open_calls.pop(call_id))
                    closed_calls.add(call_id)
                    output = raw.get("output")
                    if output != NO_EVIDENCE_OUTPUT:
                        evidence_id = _reference(output)
                        if evidence_id is None or call is None:
                            raise SessionUnavailableError
                        references.append((len(replay), evidence_id, call))
                    replay.append(_output(call_id, NO_EVIDENCE_OUTPUT))
                else:
                    raise SessionUnavailableError
        except SessionItemRejectedError:
            raise SessionUnavailableError from None
        if open_calls:
            raise SessionUnavailableError
        for (index, _, _), content in zip(references, await self._project(references), strict=True):
            replay[index] = _output(cast(dict[str, Any], replay[index])["call_id"], content)
        if seen is not None:
            seen.update(dict.fromkeys(evidence_id for _, evidence_id, _ in references))
        return replay

    async def _project(self, references: list[tuple[int, str, ToolCall]]) -> list[str]:
        if not references:
            return []
        try:
            return await self._evidence.project_many(
                [(evidence_id, call) for _, evidence_id, call in references], self._ctx, "session"
            )
        except EvidenceUnavailableError:
            raise SessionUnavailableError from None
        except EvidenceUnverifiableError:
            raise SessionUnverifiableError from None
        except EvidenceError:
            raise SessionStoreError("会话存储不可用") from None

    async def _transition(
        self, from_state: SessionState, to_state: SessionState, *, add_turns: int = 0
    ) -> bool:
        params = {
            **self._owner(),
            "from_state": from_state,
            "to_state": to_state,
            "add_turns": add_turns,
        }
        return await self._execute(_TRANSITION, params) == 1

    async def _execute(self, statement: TextClause, params: dict[str, object]) -> int:
        try:
            async with self._engine.begin() as conn:
                result = await conn.execute(statement, params)
        except (OSError, SQLAlchemyError):
            raise SessionStoreError("会话存储不可用") from None
        return result.rowcount

    def _owner(self) -> dict[str, object]:
        """会话按 owner 归属：群会话由同群各发起人共享，个人会话的 owner 是本人。"""
        identity = self._ctx.identity
        return {
            "session_id": self.session_id,
            "owner_kind": identity.owner.kind,
            "owner_id": identity.owner.id,
            "channel": identity.channel,
        }

    def _new_row(self) -> dict[str, object]:
        now = self._clock()
        return {
            **self._owner(),
            "profile_fingerprint": self._profile,
            "created_at": now,
            "expires_at": now + timedelta(seconds=self._limits.retention_seconds),
        }


def _message(raw: dict[str, Any], input_policy: SessionInputPolicy) -> TResponseInputItem:
    """用户与助手消息只保留纯文本；用户文字另须符合输入准入策略。

    图片、文件、拒答片段与其他角色没有安全保存形式。助手文字只能复述模型可达内容（工具数据
    已按 Session 约束投影，用户输入已准入），不另做模式检查。
    """
    role = raw.get("role")
    content = raw.get("content")
    if role not in ("user", "assistant"):
        raise SessionItemRejectedError("本轮包含不支持保存的会话内容")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and content:
        # 只接受携带文字的片段；图片、文件、音频与拒答片段没有 text，整轮拒绝。
        text = "".join(
            _text(p.get("text") if isinstance(p, dict) else None, allow_empty=True) for p in content
        )
    else:
        raise SessionItemRejectedError("本轮包含不支持保存的会话内容")
    if role == "user" and not input_policy.admits(text):
        raise SessionItemRejectedError("用户输入不符合会话数据策略，本轮未执行")
    return cast(TResponseInputItem, {"role": role, "content": text})


def _function_call(raw: dict[str, Any]) -> TResponseInputItem:
    """调用项只保存标识、函数名、参数，以及供应商 ``provider_data`` 中的 thought signature。

    规则不按 Provider 分支：``model``、``response_id`` 等其他字段一律丢弃，非映射视为没有可保存的
    字段；签名一旦出现就必须是非空、可编码为 UTF-8 且不超过上限的字符串，否则整轮拒绝，不截断、
    不静默删除（同轮回放需要它原样回到模型）。
    """
    call: dict[str, Any] = {
        "type": "function_call",
        "call_id": _text(raw.get("call_id")),
        "name": _text(raw.get("name")),
        "arguments": _text(raw.get("arguments"), allow_empty=True),
    }
    provider_data = raw.get("provider_data")
    if isinstance(provider_data, Mapping) and _SIGNATURE in provider_data:
        call["provider_data"] = {_SIGNATURE: _signature(provider_data[_SIGNATURE])}
    return cast(TResponseInputItem, call)


def _signature(value: object) -> str:
    try:
        size = len(value.encode()) if isinstance(value, str) else 0
    except UnicodeEncodeError:
        size = 0
    if not 0 < size <= _MAX_SIGNATURE_BYTES:
        raise SessionItemRejectedError("本轮模型调用的签名无法保存到会话")
    return cast(str, value)


def _tool_call(raw: dict[str, Any] | None) -> ToolCall | None:
    """从历史中的调用项取出调用标识、函数名与参数；参数不是 JSON 对象时无法核对来源。"""
    if raw is None:
        return None
    arguments = _json_object(raw.get("arguments"))
    if arguments is None:
        return None
    try:
        return ToolCall.model_validate(
            {"call_id": raw.get("call_id"), "tool_name": raw.get("name"), "arguments": arguments}
        )
    except ValidationError:
        return None


def _output(call_id: str, output: str) -> TResponseInputItem:
    return cast(
        TResponseInputItem, {"type": "function_call_output", "call_id": call_id, "output": output}
    )


def _text(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise SessionItemRejectedError("本轮包含不支持保存的会话内容")
    return value


def _envelope_evidence_id(output: object) -> str | None:
    """受治理工具返回 Evidence 模型投影；其他输出（拒绝/失败信息）不含可回放证据。"""
    parsed = _json_object(output)
    evidence_id = parsed.get("evidence_id") if parsed is not None else None
    return evidence_id if isinstance(evidence_id, str) and evidence_id else None


def _reference(output: object) -> str | None:
    parsed = _json_object(output)
    if parsed is None or set(parsed) != {_EVIDENCE_REF}:
        return None
    evidence_id = parsed[_EVIDENCE_REF]
    return evidence_id if isinstance(evidence_id, str) and evidence_id else None


def _json_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _role(item: TResponseInputItem) -> object:
    return cast(dict[str, Any], item).get("role")


def _size(items: list[TResponseInputItem]) -> int:
    return len(json.dumps(items, ensure_ascii=False).encode())


# 恢复的持久关闭：元数据已存在时置为 closed；尚未登记时写入已关闭的占位行（指纹为空、零轮）。
# 首次登记是 ON CONFLICT DO NOTHING，与这里在同一主键上串行：无论谁先提交，结果都是 closed，
# 且没有任何迁移能把 closed 改回 active。同一会话只写一行。
_CLOSE_OR_RESERVE = text(
    """
    INSERT INTO xiaowei_session (
        session_id, owner_kind, owner_id, channel, profile_fingerprint, created_at, expires_at,
        turns, state
    ) VALUES (:session_id, :owner_kind, :owner_id, :channel, '', :now, :expires_at, 0, 'closed')
    ON CONFLICT (session_id) DO UPDATE SET state = 'closed'
    """
)


async def close_interrupted_sessions(
    conn: AsyncConnection,
    owners: Sequence[tuple[str, Owner, str]],
    *,
    now: datetime,
    expires_at: datetime,
) -> None:
    """在启动恢复的事务中持久关闭被中断轮次的会话 ``(session_id, owner, channel)``。

    旧进程的 Runner 可能尚未登记会话元数据、正在登记或正在写入：元数据不存在时写入已关闭的
    占位，使之后的登记只能读到 closed；已存在（含 active/writing）时直接关闭。旧 Runner 之后的
    提交因状态不是 active 被拒，底层可能已写入的历史永不回放。占位行按会话保留期过期，由维护
    清理删除。
    """
    seen: set[str] = set()
    for session_id, owner, channel in owners:
        if session_id in seen:
            continue
        seen.add(session_id)
        await conn.execute(
            _CLOSE_OR_RESERVE,
            {
                "session_id": session_id,
                "owner_kind": owner.kind,
                "owner_id": owner.id,
                "channel": channel,
                "now": now,
                "expires_at": expires_at,
            },
        )


async def close_sessions(conn: AsyncConnection, session_ids: Sequence[str]) -> None:
    """在调用方的应用表事务中关闭会话：不可再回放，不删除历史，不补偿执行。"""
    if session_ids:
        await conn.execute(
            text("UPDATE xiaowei_session SET state = 'closed' WHERE session_id = ANY(:ids)"),
            {"ids": list(session_ids)},
        )


@dataclass(frozen=True)
class CleanupReport:
    """本批次完成清理的会话数：``sessions`` 为删除了会话元数据的，``unregistered`` 为没有会话
    元数据、只删除了渠道映射的。"""

    sessions: int
    unregistered: int = 0


async def cleanup_expired(engine: AsyncEngine, *, now: datetime, batch_size: int) -> CleanupReport:
    """显式维护：清理一批已过期、非 current、没有运行中或发送中请求的会话。

    每个会话依次：带条件关闭 → SDK ``clear_session()`` 清历史 → 删除已过期的请求与证据 →
    会话不再有任何请求时删除元数据与已退役映射。没有会话元数据的已退役映射（从未运行 Agent）
    不清历史，只删除已过期的请求与证据，再在不再有任何请求时删除映射。两类合计不超过
    ``batch_size``。每条删除都重新核对条件，竞争导致条件不再成立时跳过；任一步存储失败立即
    停下并抛出，已关闭的会话保持不可回放，可再次运行完成。未过期的请求与证据保留到各自过期
    后的下一次清理。Action 按自身保留期限独立清理，每批最多 ``batch_size`` 条；执行中的
    Action 不删除，须先启动恢复为 unknown。普通启动不调用本函数。
    """
    if batch_size <= 0:
        raise ValueError("清理批次必须为正数")
    params: dict[str, object] = {"now": now, "limit": batch_size}
    try:
        async with engine.begin() as conn:
            await conn.execute(_DELETE_EXPIRED_ACTIONS, params)
        async with engine.connect() as conn:
            candidates = [r[0] for r in await conn.execute(_CLEANUP_CANDIDATES, params)]
    except (OSError, SQLAlchemyError):
        raise SessionStoreError("存储不可用，清理未完成") from None
    cleaned = 0
    for session_id in candidates:
        scoped = {"now": now, "session_id": session_id}
        try:
            async with engine.begin() as conn:
                if (await conn.execute(_CLEANUP_CLOSE, scoped)).rowcount == 0:
                    continue
            await SQLAlchemySession(session_id, engine=engine).clear_session()
            async with engine.begin() as conn:
                await conn.execute(_DELETE_EXPIRED_REQUESTS, scoped)
                await conn.execute(_DELETE_EXPIRED_EVIDENCE, scoped)
                deleted = (await conn.execute(_DELETE_SESSION, scoped)).rowcount
                await conn.execute(_DELETE_MAPPINGS, scoped)
        except (OSError, SQLAlchemyError):
            raise SessionStoreError("会话清理中断，已关闭的会话保持不可回放") from None
        cleaned += deleted
    unregistered = 0
    remaining = batch_size - len(candidates)
    if remaining > 0:
        unregistered = await _cleanup_unregistered(engine, now=now, limit=remaining)
    return CleanupReport(sessions=cleaned, unregistered=unregistered)


async def _cleanup_unregistered(engine: AsyncEngine, *, now: datetime, limit: int) -> int:
    try:
        async with engine.connect() as conn:
            params = {"now": now, "limit": limit}
            candidates = [r[0] for r in await conn.execute(_UNREGISTERED_CANDIDATES, params)]
        cleaned = 0
        for session_id in candidates:
            scoped = {"now": now, "session_id": session_id}
            async with engine.begin() as conn:
                await conn.execute(_DELETE_EXPIRED_REQUESTS, scoped)
                await conn.execute(_DELETE_EXPIRED_EVIDENCE, scoped)
                cleaned += (await conn.execute(_DELETE_UNREGISTERED_MAPPING, scoped)).rowcount
    except (OSError, SQLAlchemyError):
        raise SessionStoreError("会话清理中断，可再次运行完成") from None
    return cleaned
