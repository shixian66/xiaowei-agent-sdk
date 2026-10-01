"""Evidence 记录、四种用途投影与最终回答校验。

工具结果在写入前按策略投影为模型、Session、Web、飞书四份内容，PostgreSQL 只保存这四份投影、
生成它们的策略指纹与可信元数据，不保存原始结果。模型读到的内容会经模型文字写入 Session
并到达渠道，Session 内容又会回放给模型，因此这两种模型可达内容是同一份投影：按模型的
字段优先级，只取模型、Session 与记录所在渠道都允许的字段，受三者中最小的容量约束。

所有出口（交给模型的工具结果、各用途读取、最终回答）都经同一个读取边界，复核归属、渠道、
过期、目标范围、当前策略与当前授权；不同拒绝原因返回同一条信息，不暴露记录是否存在。
事实区域由代码从获准投影生成，模型分析单独标注：Web 另得到从当前 Web 投影生成的结构化
``DeliveryFact``；飞书只得到纯文本，表格数据逐行渲染，单元格内的换行等控制字符被转义，
不能伪造其他段落。
"""

import hashlib
import json
import secrets
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeGuard

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei.governance import (
    Authorizer,
    ToolCatalog,
    ToolPolicy,
    ToolRejectedError,
    normalize_arguments,
    schema_shape,
)
from xiaowei.models import (
    AgentAnswer,
    Audience,
    Channel,
    Delivery,
    DeliveryFact,
    EvidenceRecord,
    Identity,
    JsonScalar,
    RunContext,
    ToolCall,
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
# 模型可达的用途：模型文字会被保存进 Session、交付到渠道，Session 又回放给模型。
_MODEL_REACHABLE: tuple[Audience, ...] = ("model", "session")
# 结果数据生成（``governance.contract_dump``）、投影或指纹规则变化时更新，使旧规则生成的
# 证据不再可读。/5：结果改为按登记模型的声明字段生成，不再经模型自己的序列化。/6：策略
# 可声明每种用途都必须完整保留的字段。/7：必需字段进入策略指纹，/6 证据因此不再可读。
_PROJECTION_RULE = "model-reachable-shared/7"
_EVIDENCE_ID_BYTES = 16
# 与 ``record`` 生成的证据标识等长：容量检查用它投影最坏结果。
_SAMPLE_EVIDENCE_ID = "ev_" + "0" * (2 * _EVIDENCE_ID_BYTES)
_UNAVAILABLE = "证据不存在、已过期或当前无权读取"
_FACTS_HEADER = "查询结果（系统根据证据生成）"
_ANALYSIS_HEADER = "分析建议（模型推断，未经系统核实）"
_CLARIFICATION_HEADER = "需要澄清（本轮未执行查询）"

_INSERT = text(
    """
    INSERT INTO xiaowei_evidence (
        evidence_id, subject_id, session_id, turn_id, channel, target_id, tool_id, call_id,
        tool_name, arguments_digest, policy_id, policy_fingerprint, captured_at, recorded_at,
        expires_at, truncated, model_content, session_content, web_content, feishu_content
    ) VALUES (
        :evidence_id, :subject_id, :session_id, :turn_id, :channel, :target_id, :tool_id, :call_id,
        :tool_name, :arguments_digest, :policy_id, :policy_fingerprint, :captured_at, :recorded_at,
        :expires_at, :truncated,
        :model_content, :session_content, :web_content, :feishu_content
    )
    """
)
# 归属条件写在查询里：他人、其他会话或其他渠道的记录与不存在的记录不可区分。
_SELECT_OWNED = text(
    """
    SELECT evidence_id, subject_id, session_id, turn_id, channel, target_id, tool_id, call_id,
           tool_name, arguments_digest, policy_id, policy_fingerprint, captured_at, expires_at,
           truncated,
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

    @property
    def catalog(self) -> ToolCatalog:
        return self._catalog

    @property
    def authorize(self) -> Authorizer:
        return self._authorize

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
        evidence_id = f"ev_{secrets.token_hex(_EVIDENCE_ID_BYTES)}"
        identity = ctx.identity
        # 模型与 Session 复用同一份投影：模型看到的内容恰好是 Session 可保存、可回放的内容。
        required = policy.required
        reachable = _project(
            evidence_id, observation, *_reachable_limits(policy, identity.channel), required
        )
        projected = {
            audience: reachable
            if audience in _MODEL_REACHABLE
            else _project(evidence_id, observation, *_channel_limits(policy, audience), required)
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
            "tool_name": request.tool_name,
            "arguments_digest": _arguments_digest(request.arguments),
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
        self, evidence_id: str, ctx: RunContext, audience: Audience, *, call: ToolCall | None = None
    ) -> str:
        """按当前身份、范围与权限读取一种用途的获准内容。

        给出 ``call`` 时证据还须由同一次调用生成：调用标识、SDK 函数名一致，且历史中的参数经
        当前策略规范化后就是证据记录的有效执行参数；Session 据此保证历史中的工具调用与其
        结果来源相符。
        """
        record = await self._readable(evidence_id, ctx, audience)
        if call is not None and (
            record.call_id != call.call_id
            or record.tool_name != call.tool_name
            or record.arguments_digest != self._effective_digest(record, call.arguments)
        ):
            raise EvidenceUnavailableError
        return record.projections[audience]

    def _effective_digest(self, record: EvidenceRecord, arguments: dict[str, object]) -> str:
        contract = self._catalog.contract(record.tool_id)
        if contract is None:
            raise EvidenceUnavailableError
        try:
            effective = normalize_arguments(self._catalog.policy_for(contract), arguments)
        except ToolRejectedError:
            raise EvidenceUnavailableError from None
        return _arguments_digest(effective)

    async def validate_answer(self, answer: AgentAnswer, ctx: RunContext) -> Delivery:
        """最终回答出口：核对引用关系与当前可读性，再为接收渠道生成内容。"""
        channel = ctx.identity.channel
        if answer.clarification is not None:
            if answer.evidence_ids or answer.inferences:
                raise AnswerRejectedError("澄清不能与查询结果或分析混用")
            content = f"{_CLARIFICATION_HEADER}\n{_one_line(answer.clarification)}"
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

        shown = [_shown(r, channel) for r in records]
        sections = [_FACTS_HEADER, *(_render_fact(item, channel) for item in shown)]
        if answer.inferences:
            sections.append(_ANALYSIS_HEADER)
            sections.extend(
                f"- {_one_line(i.text)}（依据：{', '.join(i.evidence_ids)}）"
                for i in answer.inferences
            )
        facts = tuple(_fact(item) for item in shown) if channel == "web" else ()
        return Delivery(
            content="\n".join(sections), evidence_ids=cited, channel=channel, facts=facts
        )

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
            tool_name=row["tool_name"],
            arguments_digest=row["arguments_digest"],
            policy_id=row["policy_id"],
            policy_fingerprint=row["policy_fingerprint"],
            captured_at=row["captured_at"],
            expires_at=row["expires_at"],
            truncated=row["truncated"],
            projections={a: row[column] for a, column in _COLUMNS.items()},
        )


def _reachable_limits(policy: ToolPolicy, channel: Channel) -> tuple[tuple[str, ...], int]:
    """模型可达投影：按模型字段优先级，取模型、Session 与记录所在渠道的共同字段与最小上限。"""
    bounds = [policy.projections[a] for a in (*_MODEL_REACHABLE, channel)]
    fields = tuple(
        name for name in policy.projections["model"].fields if all(name in b.fields for b in bounds)
    )
    return fields, min(b.max_bytes for b in bounds)


def _channel_limits(policy: ToolPolicy, audience: Audience) -> tuple[tuple[str, ...], int]:
    spec = policy.projections[audience]
    return spec.fields, spec.max_bytes


def _policy_fingerprint(contract: ToolContract, policy: ToolPolicy) -> str:
    """契约（参数 schema 与目标）、结果契约、四种投影的字段/上限、必需字段与投影规则的稳定摘要。

    结果契约决定远端数据如何成为事实，改变后旧证据不再可用；schema 的标题与说明文字、交给
    模型的工具说明只影响阅读，不参与摘要。
    """
    result = policy.result
    body = {
        "rule": _PROJECTION_RULE,
        "contract": {
            **contract.model_dump(mode="json", exclude={"description", "input_schema"}),
            "input_schema": schema_shape(contract.input_schema),
        },
        "result": None if result is None else schema_shape(result.model_json_schema()),
        "projections": {
            audience: {"fields": list(spec.fields), "max_bytes": spec.max_bytes}
            for audience, spec in policy.projections.items()
        },
        # 只有集合语义：顺序不同的同一组必需字段得到相同摘要。
        "required": sorted(policy.required),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _arguments_digest(arguments: Mapping[str, object]) -> str:
    """规范化参数摘要：键排序、紧凑 JSON，与参数的书写顺序和空白无关。"""
    try:
        canonical = json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError):
        raise EvidenceStoreError("工具参数不符合证据契约") from None
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def check_projection_capacity(policy: ToolPolicy, observation: ToolObservation) -> None:
    """用真实投影器检查容量：Web 与飞书两条路径的模型可达投影和渠道投影都能完整保留必需字段。

    供启动装配用按声明上限构造的最坏结果调用；任一路径放不下时抛出 ``ValueError``。
    """
    channels: tuple[Channel, ...] = ("web", "feishu")
    for channel in channels:
        paths = {
            f"{channel} 模型可达": _reachable_limits(policy, channel),
            channel: _channel_limits(policy, channel),
        }
        for name, (fields, max_bytes) in paths.items():
            try:
                _project(_SAMPLE_EVIDENCE_ID, observation, fields, max_bytes, policy.required)
            except EvidenceStoreError:
                raise ValueError(f"{name}投影放不下最坏情况的必需字段") from None


def _project(
    evidence_id: str,
    observation: ToolObservation,
    fields: tuple[str, ...],
    max_bytes: int,
    required: tuple[str, ...],
) -> tuple[str, bool]:
    """按字段顺序整体纳入获准字段，放不下的字段整体省略并标记截断，不切开字段值。

    ``empty`` 表示结果中没有任何获准字段；``truncated`` 表示来源已截断或本用途省略了字段。
    ``required`` 中的字段缺失或放不下时整体失败，不能以截断标记代替。
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
    if any(name not in data for name in required):
        raise EvidenceStoreError("工具结果的必需字段无法完整保留，结果未交给模型")
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


@dataclass(frozen=True)
class _Shown:
    """一条证据在某渠道投影中的内容；``data`` 为 ``None`` 表示没有任何获准字段。"""

    record: EvidenceRecord
    data: dict[str, object] | None
    truncated: bool


def _shown(record: EvidenceRecord, channel: Audience) -> _Shown:
    envelope = json.loads(record.projections[channel])
    data = None if envelope["empty"] else envelope["data"]
    return _Shown(record=record, data=data, truncated=bool(envelope["truncated"]))


def _render_fact(shown: _Shown, channel: Audience) -> str:
    record, data = shown.record, shown.data
    meta = (
        f"[{record.evidence_id}] 来源 {record.tool_id} · 目标 {record.target_id}"
        f" · 采集于 {record.captured_at.isoformat()}"
    )
    if shown.truncated:
        meta += " · 结果已截断"
    if data is None:
        return f"{meta}\n（无结果）"
    table = _table(data)
    if channel != "feishu" or table is None:
        return f"{meta}\n{_one_line(json.dumps(data, ensure_ascii=False))}"
    columns, rows = table
    lines = [meta]
    lines.extend(f"{name}: {_cell(value)}" for name, value in _scalars(data).items())
    lines.append(_table_line(columns))
    lines.extend(_table_line(row.get(name) for name in columns) for row in rows)
    if not rows:
        lines.append("（无数据行）")
    return "\n".join(lines)


def _fact(shown: _Shown) -> DeliveryFact:
    record, data = shown.record, shown.data
    table = _table(data) if data is not None else None
    columns, rows = table if table is not None else ((), ())
    return DeliveryFact(
        evidence_id=record.evidence_id,
        tool_id=record.tool_id,
        target_id=record.target_id,
        captured_at=record.captured_at,
        truncated=shown.truncated,
        columns=columns,
        rows=rows,
        metadata=_scalars(data) if data is not None else {},
    )


def _is_scalar(value: object) -> TypeGuard[JsonScalar]:
    return value is None or isinstance(value, str | int | float | bool)


def _table(
    data: Mapping[str, object],
) -> tuple[tuple[str, ...], tuple[dict[str, JsonScalar], ...]] | None:
    """``rows`` 是由标量组成的行对象列表时给出 (列, 行)；列取 ``columns``，否则按行键出现顺序。"""
    raw = data.get("rows")
    if not isinstance(raw, list):
        return None
    rows: list[dict[str, JsonScalar]] = []
    for row in raw:
        if not isinstance(row, dict):
            return None
        cells = {str(name): value for name, value in row.items() if _is_scalar(value)}
        if len(cells) != len(row):
            return None
        rows.append(cells)
    declared = data.get("columns")
    if isinstance(declared, list) and all(isinstance(name, str) for name in declared):
        columns = tuple(str(name) for name in declared)
    else:
        columns = tuple(dict.fromkeys(name for row in rows for name in row))
    return columns, tuple(rows)


def _scalars(data: Mapping[str, object]) -> dict[str, JsonScalar]:
    return {
        name: value
        for name, value in data.items()
        if name not in ("rows", "columns") and _is_scalar(value)
    }


def _table_line(cells: Iterable[object]) -> str:
    """飞书表格行：以 ``| `` 开头、`` |`` 结尾，单元格之间是 `` | ``；数据行不会与标题行相同。"""
    return "| " + " | ".join(_cell(value) for value in cells) + " |"


def _cell(value: object) -> str:
    """纯文本单元格：字符串去掉引号但保留 JSON 转义，只占一行。

    JSON 已把反斜杠写作 ``\\\\``，因此单元格中的竖线写作 ``\\|`` 后，没有转义的 `` | `` 只能是
    分隔符。
    """
    encoded = json.dumps(value, ensure_ascii=False)
    body = encoded[1:-1] if isinstance(value, str) else encoded
    return _one_line(body).replace("|", "\\|")


# JSON 在 ``ensure_ascii=False`` 时仍原样输出的分行与不可见字符：C1 控制字符（含 U+0085）、
# DEL、行与段分隔符（U+2028/U+2029），以及可改变显示顺序的格式字符。
_LINE_UNSAFE = frozenset({"Cc", "Zl", "Zp", "Cf"})


def _one_line(content: str) -> str:
    """把会另起一行或不可见的字符写成 JSON 转义；JSON 文本转换后仍是含义相同的 JSON。"""
    return "".join(
        json.dumps(char)[1:-1] if unicodedata.category(char) in _LINE_UNSAFE else char
        for char in content
    )
