"""有界运维历史读取；只复用应用表、Evidence 和当前读者权限，不回放旧 Session。"""

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from agents import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import String, bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from xiaowei.evidence import (
    AnswerRejectedError,
    EvidenceStore,
    EvidenceStoreError,
    EvidenceUnavailableError,
    EvidenceUnverifiableError,
    check_projection_capacity,
)
from xiaowei.governance import GovernedTools, Projection, ToolPolicy
from xiaowei.models import (
    AUDIENCES,
    Audience,
    EvidenceRecord,
    RunContext,
    TextBlock,
    ToolContract,
    ToolObservation,
    ToolRequest,
    TurnAnswer,
)
from xiaowei.tools import governed_function_tool

TARGET = "history"
SEARCH = f"{TARGET}/search_operations"
READ = f"{TARGET}/read_operation"
TOOLS = frozenset({SEARCH, READ})
PAGE_SIZE = 10
SCOPE_NOTICE = (
    "交互历史仅限本对话；定时结果按创建任务的操作群授权。"
    "请在有权限的创建群询问，或使用当前入口获准原源重新调查。"
)
WORK_METHOD = (
    "运维历史：新建会话不会带入旧上下文，需要过去事实时可自主使用历史搜索或按已知编号直读。"
    "可按时间、目标、已知任务/运行缩小范围；截断时可扩大或前移时间窗、缩小目标，或查获准原源。"
    "有更早候选不代表其内容可读，未找到也不证明未发生。"
    "历史事实、历史模型分析和本轮推断分开；相同症状或时间相邻不足以证明共同根因。"
    "需要当前状态时重新取证，建议、批准、执行与恢复验证分开，旧批准不授予新写许可。"
    "必要目标或时间有歧义且可信上下文/工具无法确定时才问用户；已有信息不重复询问。"
    "没有固定工具顺序；在预算内自主补证或结束，证据不足明确无法确定。"
)


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start: str
    end: str
    target_ids: list[str]
    job_id: str | None
    run_id: str | None

    @model_validator(mode="after")
    def window(self) -> "SearchArgs":
        start, end = datetime.fromisoformat(self.start), datetime.fromisoformat(self.end)
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("start/end须含时区且起点早于终点")
        return self


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    record_ref: str = Field(min_length=1, max_length=500)


class Source(BaseModel):
    """可信工具产生的原始来源绑定；不是模型可提供的参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    record_ref: str
    evidence_ids: tuple[str, ...]
    analysis: bool


_FIELDS = ("records", "sources", "gaps", "earliest_at", "has_earlier", "notice")


def catalog(
    limits: Mapping[Audience, int],
) -> tuple[tuple[ToolContract, ...], tuple[ToolPolicy, ...]]:
    contracts, policies = [], []
    for tool, args, description in (
        (
            SEARCH,
            SearchArgs,
            "查本对话旧generation或本操作群的原始运维记录，不带旧模型分析。"
            "start/end为带时区ISO绝对时间，[start,end)；target_ids空列表表示当前获准目标，"
            "job_id/run_id未知填null。时间按请求创建、独立事实采集或后台计划时刻。"
            "固定最多10条；返回最早时间及更早候选标志，truncated不能当查全。"
            "可缩小目标/任务或前移时间窗继续，无关键词或游标。时间/参数错误可修正，"
            "不可读单条跳过并给gaps，空结果不证明未发生。" + SCOPE_NOTICE,
        ),
        (
            READ,
            ReadArgs,
            "按搜索返回或已知的record_ref读取原调查事实；编号不授予权限。"
            "旧分析须完整来源复核，与事实分列；缺失、过期或无权时只给可读事实和gaps。"
            "不重建旧SDK上下文，不递归读取历史工具结果。容量/对象上限时少返回或不复制旧分析。"
            "可查其他获准记录或原源，不能从空结果推断未发生。" + SCOPE_NOTICE,
        ),
    ):
        policy_id = tool.replace("/", ".")
        policies.append(
            ToolPolicy(
                policy_id,
                args,
                {a: Projection(_FIELDS, limits[a]) for a in AUDIENCES},
                required=_FIELDS,
                fact_note="历史事实不代表当前状态；历史模型分析未经因果验证。",
            )
        )
        contracts.append(
            ToolContract(
                tool_id=tool,
                target_id=TARGET,
                policy_id=policy_id,
                input_schema=args.model_json_schema(),
                description=description,
            )
        )
    return tuple(contracts), tuple(policies)


# 交互范围只来自可信当前会话映射/已接纳请求；不把共用 Web owner 当对话。
_CONVERSATION = text("""
    SELECT conversation_key FROM xiaowei_request
    WHERE channel=:channel AND owner_kind=:kind AND owner_id=:owner
      AND session_id=:session AND turn_id=:turn AND expires_at>:now
    UNION ALL
    SELECT conversation_key FROM xiaowei_channel_session
    WHERE channel=:channel AND owner_kind=:kind AND owner_id=:owner
      AND session_id=:session AND expires_at>:now LIMIT 1
""")
_CANDIDATES = text("""
    WITH sessions AS (
        SELECT session_id FROM xiaowei_channel_session
        WHERE channel=:channel AND owner_kind=:kind AND owner_id=:owner
          AND conversation_key=:conversation AND expires_at>:now
        UNION
        SELECT session_id FROM xiaowei_request
        WHERE channel=:channel AND owner_kind=:kind AND owner_id=:owner
          AND conversation_key=:conversation AND expires_at>:now
    ), candidates AS (
        SELECT 'turn:' || r.turn_id AS record_ref, r.session_id, r.turn_id,
               r.created_at AS observed_at, r.state, r.expires_at,
               NULL::text AS job_id, NULL::text AS run_id
        FROM xiaowei_request r JOIN sessions s ON s.session_id=r.session_id
        WHERE r.channel=:channel AND r.owner_kind=:kind AND r.owner_id=:owner
          AND r.expires_at>:now AND r.turn_id<>:turn
        UNION ALL
        SELECT 'turn:' || e.turn_id, e.session_id, e.turn_id, MIN(e.captured_at),
               'facts_only', MIN(e.expires_at), NULL::text, NULL::text
        FROM xiaowei_evidence e JOIN sessions s ON s.session_id=e.session_id
        WHERE e.channel=:channel AND e.owner_kind=:kind AND e.owner_id=:owner
          AND e.expires_at>:now AND e.turn_id<>:turn
          AND e.tool_id NOT IN ('history/search_operations','history/read_operation')
          AND NOT EXISTS (SELECT 1 FROM xiaowei_request r WHERE r.turn_id=e.turn_id
                          AND r.session_id=e.session_id AND r.expires_at>:now)
          AND NOT EXISTS (SELECT 1 FROM xiaowei_scheduled_run r WHERE r.turn_id=e.turn_id)
        GROUP BY e.session_id,e.turn_id
        UNION ALL
        SELECT 'run:' || r.run_id, r.session_id, r.turn_id, r.scheduled_at,
               r.state, NULL::timestamptz, r.job_id, r.run_id
        FROM xiaowei_scheduled_run r JOIN xiaowei_scheduled_job j ON j.job_id=r.job_id
        WHERE :kind='group' AND :channel='feishu' AND j.owner_id=:owner AND j.kind='agent_task'
          AND r.turn_id<>:turn
    )
    SELECT * FROM candidates c
    WHERE (:ref IS NULL OR record_ref=:ref)
      AND (:start IS NULL OR observed_at>=CAST(:start AS timestamptz))
      AND (:end IS NULL OR observed_at<CAST(:end AS timestamptz))
      AND (:job IS NULL OR job_id=:job) AND (:run IS NULL OR run_id=:run)
      AND (:all_targets OR EXISTS (SELECT 1 FROM xiaowei_evidence e
           WHERE e.session_id=c.session_id AND e.turn_id=c.turn_id AND e.target_id IN :targets
             AND e.tool_id NOT IN ('history/search_operations','history/read_operation')))
    ORDER BY observed_at DESC,record_ref LIMIT :limit
""").bindparams(
    *(bindparam(k, type_=String()) for k in ("ref", "start", "end", "job", "run", "conversation")),
    bindparam("targets", expanding=True, type_=String()),
)
_EVIDENCE = text("""
    SELECT evidence_id FROM xiaowei_evidence
    WHERE channel=:channel AND owner_kind=:kind AND owner_id=:owner
      AND session_id=:session AND turn_id=:turn
      AND tool_id NOT IN ('history/search_operations','history/read_operation')
    ORDER BY recorded_at,evidence_id LIMIT :limit
""")
_ANSWER = text("""
    SELECT answer FROM xiaowei_request WHERE channel=:channel AND owner_kind=:kind
      AND owner_id=:owner AND session_id=:session AND turn_id=:turn AND expires_at>:now
""")


@dataclass
class _Bundle:
    item: dict[str, Any]
    source: Source
    records: list[EvidenceRecord]
    context: RunContext
    expires_at: datetime | None
    turn: TurnAnswer | None = None


def _objects(records: Sequence[EvidenceRecord]) -> set[tuple[str, str, str]]:
    return {(r.target_id, d.database, d.name) for r in records for d in r.dependencies or ()}


@dataclass(frozen=True)
class OperationHistory:
    engine: AsyncEngine
    evidence: EvidenceStore
    clock: Callable[[], datetime]
    model_tools: frozenset[str]
    max_rows: int

    @asynccontextmanager
    async def _reading(self) -> AsyncIterator[AsyncConnection]:
        try:
            async with self.engine.connect() as conn:
                yield conn
        except (OSError, SQLAlchemyError):
            raise EvidenceStoreError("历史存储不可用，本次未交付历史内容") from None

    def _params(self, ctx: RunContext) -> dict[str, Any]:
        i = ctx.identity
        return dict(
            channel=i.channel,
            kind=i.owner.kind,
            owner=i.owner.id,
            session=i.session_id,
            turn=i.turn_id,
            now=self.clock(),
        )

    async def _candidates(
        self, ctx: RunContext, *, args: SearchArgs | None = None, ref: str | None = None
    ) -> list[dict[str, Any]]:
        params = self._params(ctx)
        async with self._reading() as conn:
            conversation = await conn.scalar(_CONVERSATION, params)
            rows = await conn.execute(
                _CANDIDATES,
                {
                    **params,
                    "conversation": conversation,
                    "ref": ref,
                    "start": args.start if args else None,
                    "end": args.end if args else None,
                    "job": args.job_id if args else None,
                    "run": args.run_id if args else None,
                    "all_targets": not args or not args.target_ids,
                    "targets": args.target_ids if args else [],
                    "limit": max(PAGE_SIZE, self.max_rows) + 1 if args else 1,
                },
            )
            return [dict(row) for row in rows.mappings()]

    async def _bundle(
        self,
        ctx: RunContext,
        row: Mapping[str, Any],
        *,
        analysis: bool,
        ids: tuple[str, ...] | None = None,
    ) -> _Bundle:
        original = ctx.model_copy(
            update={
                "identity": ctx.identity.model_copy(
                    update={
                        "session_id": row["session_id"],
                        "turn_id": row["turn_id"],
                    }
                )
            }
        )
        params = {**self._params(original), "limit": self.max_rows + 1}
        async with self._reading() as conn:
            raw_ids = (
                list((await conn.execute(_EVIDENCE, params)).scalars())
                if ids is None
                else list(ids)
            )
            answer = await conn.scalar(_ANSWER, params) if not row["run_id"] else None
        saved = None
        if answer:
            try:
                saved = TurnAnswer.model_validate_json(answer)
            except ValidationError:
                pass
        turn = saved if analysis else None
        wanted = tuple(dict.fromkeys(turn.context_evidence)) if turn is not None else tuple(raw_ids)
        records, missing = [], False
        for eid in wanted:
            try:
                record = await self.evidence.inspect(eid, original)
                if record.tool_id not in self.model_tools or record.tool_id in TOOLS:
                    raise EvidenceUnavailableError
            except EvidenceUnavailableError:
                missing = True
                continue
            records.append(record)
        # 旧分析的全部依赖必须完整可读；不能删除其中的历史工具或受限来源后复制文字。
        if turn is not None and (missing or len(_objects(records)) > self.max_rows):
            fallback = await self._bundle(ctx, row, analysis=False)
            fallback.item["gaps"]["Evidence 不可读" if missing else "truncated"] = True
            fallback.item["gaps"]["回答缺失"] = True
            return fallback
        gaps: dict[str, bool] = {}
        if missing:
            gaps["Evidence 不可读"] = True
        if len(_objects(records)) > self.max_rows or (
            turn is None and ids is None and len(raw_ids) > self.max_rows
        ):
            gaps["truncated"] = True
            limited: list[EvidenceRecord] = []
            for record in records:
                if (
                    len(_objects([*limited, record])) <= self.max_rows
                    and len(limited) < self.max_rows
                ):
                    limited.append(record)
            records = limited
        if saved is None:
            gaps["回答缺失"] = True
        else:
            # 搜索只透传不完整标记，不复制旧文字、失败来源名或旧分析依赖。
            for key in ("limit_reached", "monitoring_failures", "inspection_failures"):
                if getattr(saved, key):
                    gaps[key] = True
            if saved.blocks_answer.awaiting_user:
                gaps["awaiting_user"] = True
        facts = []
        for record in records:
            projection = json.loads(record.projections["model"])
            partial = record.truncated or projection["truncated"]
            if partial:
                gaps["truncated"] = True
            facts.append(
                {
                    "tool": record.tool_id,
                    "target": record.target_id,
                    "captured_at": record.captured_at.isoformat(),
                    "data": projection["data"],
                    "truncated": partial,
                }
            )
        item = dict(
            record_ref=row["record_ref"],
            time=row["observed_at"].isoformat(),
            state=row["state"],
            facts=facts,
            gaps=gaps,
        )
        if turn is not None:
            item["analysis"] = "\n".join(
                b.text for b in turn.blocks_answer.blocks if isinstance(b, TextBlock)
            )
            item["analysis_kind"] = "历史模型分析，未经因果验证"
        expires = [r.expires_at for r in records]
        if row["expires_at"] is not None:
            expires.append(row["expires_at"])
        return _Bundle(
            item,
            Source(
                record_ref=row["record_ref"],
                evidence_ids=tuple(r.evidence_id for r in records),
                analysis=turn is not None,
            ),
            records,
            original,
            min(expires, default=None),
            turn,
        )

    async def _validate(
        self,
        bundles: Sequence[_Bundle],
        *,
        recipient_tools: frozenset[str] | None = None,
        recipient_targets: frozenset[str] | None = None,
    ) -> None:
        b = bundles[0] if len(bundles) == 1 else None
        if b is not None and b.turn is not None:
            await self.evidence.validate_answer(
                b.turn,
                b.context,
                history=True,
                recipient_tools=recipient_tools,
                recipient_targets=recipient_targets,
            )
        else:
            await self.evidence.validate_records(
                [(r, b.context) for b in bundles for r in b.records],
                recipient_tools=recipient_tools,
                recipient_targets=recipient_targets,
            )

    async def execute(self, ctx: RunContext, request: ToolRequest) -> ToolObservation:
        args = SearchArgs.model_validate(request.arguments) if request.tool_id == SEARCH else None
        rows = await self._candidates(
            ctx, args=args, ref=None if args else str(request.arguments["record_ref"])
        )
        bundles: list[_Bundle] = []
        scan_limit = max(PAGE_SIZE, self.max_rows)
        truncated = args is not None and len(rows) > scan_limit
        lookup_gaps: dict[str, bool] = {}
        for row in rows[:scan_limit]:
            if len(bundles) >= PAGE_SIZE:
                truncated = True
                break
            try:
                b = await self._bundle(ctx, row, analysis=args is None)
                if args is not None and args.target_ids:
                    b.records = [r for r in b.records if r.target_id in args.target_ids]
                    keep = {(r.tool_id, r.target_id, r.captured_at.isoformat()) for r in b.records}
                    b.item["facts"] = [
                        f
                        for f in b.item["facts"]
                        if (f["tool"], f["target"], f["captured_at"]) in keep
                    ]
                    b.source = b.source.model_copy(
                        update={"evidence_ids": tuple(r.evidence_id for r in b.records)}
                    )
                if (
                    len(_objects([r for old in bundles for r in old.records] + b.records))
                    > self.max_rows
                ):
                    truncated = True
                    break
            except (EvidenceUnavailableError, EvidenceUnverifiableError, AnswerRejectedError):
                lookup_gaps["Evidence 不可读"] = True
                continue
            if not b.records and b.item["gaps"].get("Evidence 不可读"):
                lookup_gaps["Evidence 不可读"] = True
                continue
            bundles.append(b)
        try:
            # 一批成功候选只探测一次，同对象跨原调查去重，不跨保存/交付缓存结论。
            await self._validate(bundles)
        except (EvidenceUnavailableError, EvidenceUnverifiableError, AnswerRejectedError):
            readable = []
            lookup_gaps["Evidence 不可读"] = True
            # 失败批次逐条定位；单条旧分析失败时，只再查可独立验证的原事实。
            for b in bundles:
                if b.turn is None and len(bundles) == 1:
                    continue  # 单条原事实已失败，不重复相同复核。
                try:
                    if b.turn is not None:
                        row = next(r for r in rows if r["record_ref"] == b.source.record_ref)
                        b = await self._bundle(ctx, row, analysis=False)
                        b.item["gaps"]["回答缺失"] = True
                        if not b.records:
                            continue
                    await self._validate([b])
                    readable.append(b)
                except (EvidenceUnavailableError, EvidenceUnverifiableError, AnswerRejectedError):
                    continue
            bundles = readable
        contract = self.evidence.catalog.contract(request.tool_id, TARGET)
        assert contract is not None  # noqa: S101 - 仅由登记后的受治理工具调用
        while True:
            gaps = {**lookup_gaps, **{k: v for b in bundles for k, v in b.item["gaps"].items()}}
            if not bundles:
                gaps["Evidence 不可读"] = True
            if truncated:
                gaps["truncated"] = True
            payload = dict(
                records=[b.item for b in bundles],
                sources=[b.source.model_dump(mode="json") for b in bundles],
                gaps=gaps,
                earliest_at=min((b.item["time"] for b in bundles), default=None),
                has_earlier=False,
                notice=SCOPE_NOTICE,
            )
            observation = ToolObservation(
                payload=payload,
                captured_at=self.clock(),
                truncated=truncated or bool(gaps.get("truncated")),
                content_expires_at=min(
                    (b.expires_at for b in bundles if b.expires_at), default=None
                ),
            )
            try:
                check_projection_capacity(self.evidence.catalog.policy_for(contract), observation)
                # false比true多一个字节，容量校验后再求标志；不对被省略来源探测对象。
                earliest = payload["earliest_at"]
                if earliest is not None:
                    for row in rows:
                        if row[
                            "observed_at"
                        ].isoformat() < earliest and await self._metadata_allowed(ctx, row):
                            payload["has_earlier"] = True
                            break
                return observation.model_copy(update={"payload": payload})
            except ValueError:
                if not bundles:
                    raise
                b = bundles[-1]
                if b.turn is not None:
                    # 先省略旧模型文字，独立事实仍可使用，不因分析过大丢掉整条事件。
                    b.turn = None
                    b.source = b.source.model_copy(update={"analysis": False})
                    b.item.pop("analysis")
                    b.item.pop("analysis_kind")
                elif b.records:
                    b.records.pop()
                    b.item["facts"].pop()
                    b.source = b.source.model_copy(
                        update={"evidence_ids": tuple(r.evidence_id for r in b.records)}
                    )
                else:
                    bundles.pop()
                b.item["gaps"]["truncated"] = True
                truncated = True

    async def _metadata_allowed(self, ctx: RunContext, row: Mapping[str, Any]) -> bool:
        # 仅工具授权；不调用inspect/依赖复核，目标/标题等不进入更早标志。
        async with self._reading() as conn:
            refs = (
                await conn.execute(
                    text("""
                SELECT DISTINCT target_id,tool_id FROM xiaowei_evidence
                WHERE session_id=:session AND turn_id=:turn
                  AND tool_id NOT IN ('history/search_operations','history/read_operation')
            """),
                    {"session": row["session_id"], "turn": row["turn_id"]},
                )
            ).all()
        for target, tool in refs:
            if (
                target not in ctx.target_scope
                or tool not in self.model_tools
                or await self.evidence.authorize(ctx.identity, target, tool) is not True
            ):
                return False
        return True

    async def check_content(
        self,
        record: EvidenceRecord,
        ctx: RunContext,
        recipient_tools: frozenset[str] | None,
        recipient_targets: frozenset[str] | None,
    ) -> None:
        if record.tool_id not in TOOLS:
            return
        try:
            raw = json.loads(record.projections["model"])["data"]["sources"]
            sources = [Source.model_validate(s) for s in raw]
        except (KeyError, TypeError, ValueError):
            raise EvidenceUnavailableError from None
        bundles = []
        for source in sources:
            rows = await self._candidates(ctx, ref=source.record_ref)
            if not rows:
                raise EvidenceUnavailableError
            b = await self._bundle(ctx, rows[0], analysis=source.analysis, ids=source.evidence_ids)
            if b.source != source:
                raise EvidenceUnavailableError
            bundles.append(b)
        await self._validate(
            bundles, recipient_tools=recipient_tools, recipient_targets=recipient_targets
        )

    def tools_for(self, ctx: RunContext, governance: GovernedTools) -> list[FunctionTool]:
        return [
            governed_function_tool(
                c.tool_id.replace("/", "__"), c, governance, partial(self.execute, ctx)
            )
            for c in governance.allowed_contracts(ctx)
            if c.tool_id in TOOLS
        ]
