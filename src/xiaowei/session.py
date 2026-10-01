"""SDK Session 的薄策略包装：写入前暂存与过滤，校验后提交，回放前复核。

SDK 在一轮内把用户输入、工具调用/结果与最终消息写入 Session，早于应用校验最终回答；因此
``add_items`` 只把按 Session 策略转换后的副本暂存在内存，``commit_validated`` 重新校验最终
回答后才委托底层写入，失败路径调用 ``discard_pending``。工具结果只保存证据引用，回放时经
Evidence 读取边界按当前权限、目标范围、策略与过期取回 Session 投影；任一条历史不能安全
回放时整段拒绝并提示新建会话，不裁掉半组工具项、不以无来源文字替代失效证据。证据须由历史
中同一次调用（调用标识、函数名、参数）生成，暂存与回放都经 Evidence 读取边界核对。

应用表 ``xiaowei_session`` 记录会话归属、运行绑定（应用传入 Profile 与数据策略的组合指纹，
列名沿用 ``profile_fingerprint``）、失效时间、已提交轮数与状态；只有底层 Session 为空时才登记
新会话，缺少元数据的已有历史不被任何身份认领。SDK 表与应用表不共事务：提交前先把状态置为
``writing``，底层写入与元数据更新都成功后才回到 ``active``；任一步失败会话保持不可回放，
不自动重跑工具补偿。

会话关闭与维护清理也只在本模块：``close_sessions`` 在调用方的应用表事务中把会话置为不可
回放（不删历史）；``cleanup_expired`` 按显式批次、数据库过期与状态条件删除过期对象，历史经
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
)
from xiaowei.models import AgentAnswer, RunContext, ToolCall

SessionState = Literal["active", "writing", "sealed", "closed"]

# 不含可回放证据的工具输出（治理拒绝、执行失败等）以固定文字保存，不保留原始错误文本。
NO_EVIDENCE_OUTPUT = "工具未返回可回放的证据"
_EVIDENCE_REF = "xiaowei_evidence_ref"
_DROPPED_TYPES = frozenset({"reasoning"})

_INSERT_IF_ABSENT = text(
    """
    INSERT INTO xiaowei_session (
        session_id, subject_id, channel, profile_fingerprint, created_at, expires_at, turns, state
    ) VALUES (
        :session_id, :subject_id, :channel, :profile_fingerprint, :created_at, :expires_at, 0,
        'active'
    )
    ON CONFLICT (session_id) DO NOTHING
    """
)
_SELECT = text(
    """
    SELECT subject_id, channel, profile_fingerprint, expires_at, turns, state
    FROM xiaowei_session WHERE session_id = :session_id
    """
)
# 状态迁移都带归属条件；影响行数为 0 表示状态已被改变或不属于当前身份。
_TRANSITION = text(
    """
    UPDATE xiaowei_session SET state = :to_state, turns = turns + :add_turns
    WHERE session_id = :session_id AND subject_id = :subject_id AND channel = :channel
      AND state = :from_state
    """
)
_CLOSE = text(
    """
    UPDATE xiaowei_session SET state = 'closed'
    WHERE session_id = :session_id AND subject_id = :subject_id AND channel = :channel
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


class SessionError(Exception):
    """Session 策略边界错误；信息固定，不含历史内容、证据或连接信息。"""


class SessionUnavailableError(SessionError):
    """历史不能继续使用：归属或 Profile 不符、已过期、已封存/关闭、证据失效或结构异常。"""

    def __init__(self, message: str = "会话历史不可继续使用，请新建会话") -> None:
        super().__init__(message)


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

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """回放：整段复核后返回已提交历史与本轮暂存项；达到上限或不可回放时拒绝。"""
        if limit is not None:
            # 按条数截断会拆开工具调用/结果配对；历史大小由 SessionLimits 约束。
            raise SessionError("会话历史不支持按条数截断")
        turns = await self._open()
        if turns >= self._limits.max_history_turns:
            raise SessionLimitError
        replay = await self._replay([*await self._committed(), *self._pending])
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

    async def commit_validated(self) -> None:
        """校验本轮最终回答与完整历史后写入底层 Session；任何失败都不保留本轮暂存项。"""
        pending, self._pending = self._pending, []
        await self._validate_final_answer(pending)
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

    async def _validate_final_answer(self, pending: list[TResponseInputItem]) -> None:
        """最终持久化复用 Evidence 回答验证器：最后一项须是通过校验的 ``AgentAnswer``。"""
        final = pending[-1] if pending else None
        if final is None or _role(final) != "assistant":
            raise SessionItemRejectedError("本轮没有可提交的最终回答")
        try:
            answer = AgentAnswer.model_validate_json(cast(dict[str, str], final)["content"])
            await self._evidence.validate_answer(answer, self._ctx)
        except (ValidationError, AnswerRejectedError):
            raise SessionItemRejectedError("最终回答未通过校验，本轮不保存") from None

    async def _open(self) -> int:
        """返回可继续使用的会话已提交轮数；归属、Profile、状态或期限不符时拒绝。"""
        row = await self._claim()
        if row["profile_fingerprint"] != self._profile:
            raise SessionUnavailableError("模型配置或数据策略已变化，请新建会话")
        if row["state"] != "active" or self._clock() >= row["expires_at"]:
            raise SessionUnavailableError
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
            or row["subject_id"] != identity.subject_id
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
            return _output(call_id, json.dumps({_EVIDENCE_REF: evidence_id}))
        raise SessionItemRejectedError("本轮包含不支持保存的会话内容")

    def _pending_call(self, call_id: str) -> dict[str, Any] | None:
        for item in reversed(self._pending):
            raw = cast(dict[str, Any], item)
            if raw.get("type") == "function_call" and raw.get("call_id") == call_id:
                return raw
        return None

    async def _replay(self, stored: list[TResponseInputItem]) -> list[TResponseInputItem]:
        """把保存形式转换为回放形式：证据引用经读取边界换成当前 Session 投影。"""
        replay: list[TResponseInputItem] = []
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
                    call = open_calls.pop(call_id)
                    closed_calls.add(call_id)
                    output = await self._replay_output(_tool_call(call), raw)
                    replay.append(_output(call_id, output))
                else:
                    raise SessionUnavailableError
        except SessionItemRejectedError:
            raise SessionUnavailableError from None
        if open_calls:
            raise SessionUnavailableError
        return replay

    async def _replay_output(self, call: ToolCall | None, raw: dict[str, Any]) -> str:
        output = raw.get("output")
        if output == NO_EVIDENCE_OUTPUT:
            return NO_EVIDENCE_OUTPUT
        evidence_id = _reference(output)
        if evidence_id is None or call is None:
            raise SessionUnavailableError
        try:
            return await self._evidence.project(evidence_id, self._ctx, "session", call=call)
        except EvidenceUnavailableError:
            raise SessionUnavailableError from None
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
        identity = self._ctx.identity
        return {
            "session_id": self.session_id,
            "subject_id": identity.subject_id,
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
    return cast(
        TResponseInputItem,
        {
            "type": "function_call",
            "call_id": _text(raw.get("call_id")),
            "name": _text(raw.get("name")),
            "arguments": _text(raw.get("arguments"), allow_empty=True),
        },
    )


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


async def close_sessions(conn: AsyncConnection, session_ids: Sequence[str]) -> None:
    """在调用方的应用表事务中关闭会话：不可再回放，不删除历史，不补偿执行。"""
    if session_ids:
        await conn.execute(
            text("UPDATE xiaowei_session SET state = 'closed' WHERE session_id = ANY(:ids)"),
            {"ids": list(session_ids)},
        )


@dataclass(frozen=True)
class CleanupReport:
    """本批次完成清理（元数据已删除）的会话数。"""

    sessions: int


async def cleanup_expired(engine: AsyncEngine, *, now: datetime, batch_size: int) -> CleanupReport:
    """显式维护：清理一批已过期、非 current、没有运行中或发送中请求的会话。

    每个会话依次：带条件关闭 → SDK ``clear_session()`` 清历史 → 删除已过期的请求与证据 →
    会话不再有任何请求时删除元数据与已退役映射。每条删除都重新核对条件，竞争导致条件不再
    成立时跳过；任一步存储失败立即停下并抛出，已关闭的会话保持不可回放，可再次运行完成。
    未过期的请求与证据保留到各自过期后的下一次清理。普通启动不调用本函数。
    """
    if batch_size <= 0:
        raise ValueError("清理批次必须为正数")
    params: dict[str, object] = {"now": now, "limit": batch_size}
    try:
        async with engine.connect() as conn:
            candidates = [r[0] for r in await conn.execute(_CLEANUP_CANDIDATES, params)]
    except (OSError, SQLAlchemyError):
        raise SessionStoreError("会话存储不可用，清理未执行") from None
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
    return CleanupReport(sessions=cleaned)
