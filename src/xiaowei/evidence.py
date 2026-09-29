"""Evidence 记录、四种用途投影与最终回答校验。

工具结果在写入前按策略投影为模型、Session、Web、飞书四份内容，PostgreSQL 只保存这四份投影、
生成它们的策略指纹与可信元数据，不保存原始结果。模型与 Session 内容会经模型文字到达渠道，
因此同时受记录所在渠道的字段与容量约束。

所有出口（交给模型的工具结果、各用途读取、最终回答）都经同一个读取边界，复核归属、渠道、
过期、目标范围、当前策略与当前授权；不同拒绝原因返回同一条信息，不暴露记录是否存在。
事实区域由代码从获准投影生成，模型分析单独标注。
"""

import hashlib
import json
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei.governance import Authorizer, ToolCatalog, ToolPolicy
from xiaowei.models import (
    AgentAnswer,
    Audience,
    Delivery,
    EvidenceRecord,
    Identity,
    RunContext,
    ToolContract,
    ToolObservation,
    ToolRequest,
    ToolResult,
)

_COLUMNS: Mapping[Audience, str] = {
    "model": "model_content",
    "session": "session_content",
    "web": "web_content",
    "feishu": "feishu_content",
}
# 模型可达的用途：其内容可能被模型复述进最终回答或在后续轮次回放。
_MODEL_REACHABLE: frozenset[Audience] = frozenset({"model", "session"})
# 投影规则变化时更新，使旧规则生成的证据不再可读。
_PROJECTION_RULE = "channel-bounded/1"
_UNAVAILABLE = "证据不存在、已过期或当前无权读取"
_FACTS_HEADER = "查询结果（系统根据证据生成）"
_ANALYSIS_HEADER = "分析建议（模型推断，未经系统核实）"
_CLARIFICATION_HEADER = "需要澄清（本轮未执行查询）"

_INSERT = text(
    """
    INSERT INTO xiaowei_evidence (
        evidence_id, subject_id, session_id, turn_id, channel, target_id, tool_id, call_id,
        policy_id, policy_fingerprint, captured_at, recorded_at, expires_at, truncated,
        model_content, session_content, web_content, feishu_content
    ) VALUES (
        :evidence_id, :subject_id, :session_id, :turn_id, :channel, :target_id, :tool_id, :call_id,
        :policy_id, :policy_fingerprint, :captured_at, :recorded_at, :expires_at, :truncated,
        :model_content, :session_content, :web_content, :feishu_content
    )
    """
)
# 归属条件写在查询里：他人、其他会话或其他渠道的记录与不存在的记录不可区分。
_SELECT_OWNED = text(
    """
    SELECT evidence_id, subject_id, session_id, turn_id, channel, target_id, tool_id, call_id,
           policy_id, policy_fingerprint, captured_at, expires_at, truncated,
           model_content, session_content, web_content, feishu_content
    FROM xiaowei_evidence
    WHERE evidence_id = :evidence_id AND subject_id = :subject_id
      AND session_id = :session_id AND channel = :channel
    """
)


class EvidenceError(Exception):
    """Evidence 边界错误；信息固定，不含原始结果或连接信息。"""


class EvidenceStoreError(EvidenceError):
    """证据无法生成或保存；对应的工具结果不交给模型。"""


class EvidenceUnavailableError(EvidenceError):
    """证据不存在、不属于当前身份/会话/渠道、已过期或当前无权读取。"""

    def __init__(self) -> None:
        super().__init__(_UNAVAILABLE)


class AnswerRejectedError(EvidenceError):
    """最终回答未通过结构与证据校验，不得发送或持久化。"""


class EvidenceStore:
    def __init__(
        self,
        engine: AsyncEngine,
        catalog: ToolCatalog,
        *,
        authorize: Authorizer,
        clock: Callable[[], datetime],
        retention_seconds: int,
    ) -> None:
        if retention_seconds <= 0:
            raise ValueError("证据保留期必须为正数")
        self._engine = engine
        self._catalog = catalog
        self._authorize = authorize
        self._clock = clock
        self._retention = timedelta(seconds=retention_seconds)

    async def record(
        self, ctx: RunContext, request: ToolRequest, observation: ToolObservation
    ) -> ToolResult:
        """投影并保存证据，再经读取边界取回模型可见内容；证据标识由代码生成。

        执行与写入都会等待，期间权限可能被撤销；返回前按当前权限与策略重新读取，拒绝时
        结果不交给模型（I/O 已发生，不退还预算、不重试）。
        """
        contract = self._catalog.contract(request.tool_id)
        if contract is None or contract.target_id != request.target_id:
            raise EvidenceStoreError("未登记的工具结果不能生成证据")
        policy = self._catalog.policy_for(contract)
        evidence_id = f"ev_{secrets.token_hex(16)}"
        identity = ctx.identity
        projected = {
            audience: _project(evidence_id, observation, *_limits(policy, audience, identity))
            for audience in _COLUMNS
        }
        recorded_at = self._clock()
        row = {
            "evidence_id": evidence_id,
            "subject_id": identity.subject_id,
            "session_id": identity.session_id,
            "turn_id": identity.turn_id,
            "channel": identity.channel,
            "target_id": contract.target_id,
            "tool_id": contract.tool_id,
            "call_id": request.call_id,
            "policy_id": contract.policy_id,
            "policy_fingerprint": _policy_fingerprint(contract, policy),
            "captured_at": observation.captured_at,
            "recorded_at": recorded_at,
            "expires_at": recorded_at + self._retention,
            "truncated": observation.truncated,
            **{_COLUMNS[a]: content for a, (content, _) in projected.items()},
        }
        try:
            async with self._engine.begin() as conn:
                await conn.execute(_INSERT, row)
        except (OSError, SQLAlchemyError):
            raise EvidenceStoreError("证据保存失败，工具结果未交给模型") from None
        record = await self._readable(evidence_id, ctx, "model")
        _, truncated = projected["model"]
        return ToolResult(
            evidence_id=evidence_id, model_content=record.projections["model"], truncated=truncated
        )

    async def project(
        self, evidence_id: str, ctx: RunContext, audience: Audience, *, call_id: str | None = None
    ) -> str:
        """按当前身份、范围与权限读取一种用途的获准内容。

        给出 ``call_id`` 时证据还须由该次工具调用生成，Session 回放据此保证调用/结果配对。
        """
        record = await self._readable(evidence_id, ctx, audience)
        if call_id is not None and record.call_id != call_id:
            raise EvidenceUnavailableError
        return record.projections[audience]

    async def validate_answer(self, answer: AgentAnswer, ctx: RunContext) -> Delivery:
        """最终回答出口：核对引用关系与当前可读性，再为接收渠道生成内容。"""
        channel = ctx.identity.channel
        if answer.clarification is not None:
            if answer.evidence_ids or answer.inferences:
                raise AnswerRejectedError("澄清不能与查询结果或分析混用")
            content = f"{_CLARIFICATION_HEADER}\n{answer.clarification}"
            return Delivery(content=content, evidence_ids=(), channel=channel)

        cited = answer.evidence_ids
        if not cited:
            raise AnswerRejectedError("回答缺少证据引用")
        if len(set(cited)) != len(cited):
            raise AnswerRejectedError("证据引用重复")
        for inference in answer.inferences:
            if not inference.evidence_ids or not set(inference.evidence_ids) <= set(cited):
                raise AnswerRejectedError("分析必须引用本回答选择的证据")
        try:
            records = [await self._readable(e, ctx, channel) for e in cited]
        except EvidenceUnavailableError:
            raise AnswerRejectedError("回答引用的证据不可用") from None

        sections = [_FACTS_HEADER, *(_render_fact(r, channel) for r in records)]
        if answer.inferences:
            sections.append(_ANALYSIS_HEADER)
            sections.extend(
                f"- {i.text}（依据：{', '.join(i.evidence_ids)}）" for i in answer.inferences
            )
        return Delivery(content="\n".join(sections), evidence_ids=cited, channel=channel)

    async def _readable(
        self, evidence_id: str, ctx: RunContext, audience: Audience
    ) -> EvidenceRecord:
        identity = ctx.identity
        # 渠道内容只交给相同渠道；模型与 Session 投影属于记录所在会话本身。
        if audience not in _COLUMNS or (
            audience in ("web", "feishu") and audience != identity.channel
        ):
            raise EvidenceUnavailableError
        record = await self._load(evidence_id, identity)
        if (
            record is None
            or not self._matches_current_policy(record)
            or self._clock() >= record.expires_at
            or record.target_id not in ctx.target_scope
            or not await self._currently_authorized(identity, record)
        ):
            raise EvidenceUnavailableError
        return record

    def _matches_current_policy(self, record: EvidenceRecord) -> bool:
        """保存内容须由当前登记的同一契约与投影策略生成；策略收窄、换版或移除后旧证据失效。"""
        contract = self._catalog.contract(record.tool_id)
        if contract is None or contract.target_id != record.target_id:
            return False
        current = _policy_fingerprint(contract, self._catalog.policy_for(contract))
        return current == record.policy_fingerprint

    async def _currently_authorized(self, identity: Identity, record: EvidenceRecord) -> bool:
        try:
            return await self._authorize(identity, record.target_id, record.tool_id) is True
        except Exception:
            # 授权查询失败时不放行已保存内容；统一按不可读报告。
            raise EvidenceUnavailableError from None

    async def _load(self, evidence_id: str, identity: Identity) -> EvidenceRecord | None:
        params = {
            "evidence_id": evidence_id,
            "subject_id": identity.subject_id,
            "session_id": identity.session_id,
            "channel": identity.channel,
        }
        try:
            async with self._engine.connect() as conn:
                row = (await conn.execute(_SELECT_OWNED, params)).mappings().one_or_none()
        except (OSError, SQLAlchemyError):
            raise EvidenceStoreError("证据存储不可用") from None
        if row is None:
            return None
        return EvidenceRecord(
            evidence_id=row["evidence_id"],
            identity=Identity(
                subject_id=row["subject_id"],
                session_id=row["session_id"],
                turn_id=row["turn_id"],
                channel=row["channel"],
            ),
            target_id=row["target_id"],
            tool_id=row["tool_id"],
            call_id=row["call_id"],
            policy_id=row["policy_id"],
            policy_fingerprint=row["policy_fingerprint"],
            captured_at=row["captured_at"],
            expires_at=row["expires_at"],
            truncated=row["truncated"],
            projections={a: row[column] for a, column in _COLUMNS.items()},
        )


def _limits(
    policy: ToolPolicy, audience: Audience, identity: Identity
) -> tuple[tuple[str, ...], int]:
    """一种用途的有效字段与上限；模型可达的用途再与记录所在渠道取字段交集和较小上限。"""
    spec = policy.projections[audience]
    if audience not in _MODEL_REACHABLE:
        return spec.fields, spec.max_bytes
    channel = policy.projections[identity.channel]
    fields = tuple(name for name in spec.fields if name in channel.fields)
    return fields, min(spec.max_bytes, channel.max_bytes)


def _policy_fingerprint(contract: ToolContract, policy: ToolPolicy) -> str:
    """契约（含参数 schema 与目标）、四种投影的字段/上限与投影规则的稳定摘要。"""
    body = {
        "rule": _PROJECTION_RULE,
        "contract": contract.model_dump(mode="json"),
        "projections": {
            audience: {"fields": list(spec.fields), "max_bytes": spec.max_bytes}
            for audience, spec in policy.projections.items()
        },
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _project(
    evidence_id: str, observation: ToolObservation, fields: tuple[str, ...], max_bytes: int
) -> tuple[str, bool]:
    """按字段顺序整体纳入获准字段，放不下的字段整体省略并标记截断，不切开字段值。

    ``empty`` 表示结果中没有任何获准字段；``truncated`` 表示来源已截断或本用途省略了字段。
    """
    present = [name for name in fields if name in observation.payload]
    data: dict[str, object] = {}
    omitted = False
    for name in present:
        candidate = {**data, name: observation.payload[name]}
        # 以两个标记都为 false 的最长形式估算，最终内容只会更短。
        if _size(_envelope(evidence_id, candidate, truncated=False, empty=False)) <= max_bytes:
            data = candidate
        else:
            omitted = True
    truncated = observation.truncated or omitted
    content = _envelope(evidence_id, data, truncated=truncated, empty=not present)
    if _size(content) > max_bytes:
        raise EvidenceStoreError("工具结果无法满足证据投影上限")
    return content, truncated


def _envelope(evidence_id: str, data: dict[str, object], *, truncated: bool, empty: bool) -> str:
    body = {"evidence_id": evidence_id, "data": data, "truncated": truncated, "empty": empty}
    try:
        return json.dumps(body, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise EvidenceStoreError("工具结果不符合证据契约") from None


def _size(content: str) -> int:
    return len(content.encode())


def _render_fact(record: EvidenceRecord, channel: Audience) -> str:
    envelope = json.loads(record.projections[channel])
    meta = (
        f"[{record.evidence_id}] 来源 {record.tool_id} · 目标 {record.target_id}"
        f" · 采集于 {record.captured_at.isoformat()}"
    )
    if envelope["truncated"]:
        meta += " · 结果已截断"
    if envelope["empty"]:
        return f"{meta}\n（无结果）"
    return f"{meta}\n{json.dumps(envelope['data'], ensure_ascii=False)}"
