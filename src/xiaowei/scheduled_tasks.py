"""一次性固定提醒；确认复用 Action/SDK，到期不调用模型。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from agents import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from xiaowei.actions import (
    ACTION_SETTLE_TIMEOUT_SECONDS,
    ActionBinding,
    ActionPlan,
    ActionWriteRejectedError,
    proposal_policy,
)
from xiaowei.evidence import (
    AnswerRejectedError,
    EvidenceStore,
    EvidenceUnavailableError,
    EvidenceUnverifiableError,
    RecipientRejectedError,
    scope_checks,
)
from xiaowei.governance import (
    GovernedTools,
    Projection,
    ProposalRejectedError,
    ToolPolicy,
    ToolRejectedError,
)
from xiaowei.models import (
    AUDIENCES,
    MAX_ANSWER_BLOCKS,
    MAX_ANSWER_TEXT_BYTES,
    AgentAnswer,
    Audience,
    Budget,
    EvidenceRecord,
    FactBlock,
    Identity,
    RunContext,
    TextBlock,
    ToolContract,
    ToolObservation,
    ToolRequest,
    TurnAnswer,
)
from xiaowei.storage import InstanceLock, StorageUnavailableError
from xiaowei.tools import governed_function_tool
from xiaowei.webhook import TOOL as NOTIFY
from xiaowei.webhook import WebhookBotConfig, WebhookNotifications, _notification_text

if TYPE_CHECKING:
    from xiaowei.channel import AccessDecision, AccessPolicy
    from xiaowei.channel_store import ActionRecord

TARGET = "scheduled"
PROPOSE = f"{TARGET}/propose_reminder"
ACTIVATE = f"{TARGET}/activate_reminder"
STATUS = f"{TARGET}/get_task"
CONTROL = f"{TARGET}/control_task"
TOOLS = frozenset({PROPOSE, ACTIVATE, STATUS, CONTROL, f"{TARGET}/get_action_status"})
logger = logging.getLogger(__name__)


class ScheduledConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = False
    timezone: str = "Asia/Shanghai"
    max_advance_seconds: int = Field(default=30 * 86400, gt=0)
    lateness_seconds: int = Field(default=3600, gt=0)
    poll_seconds: float = Field(default=5, gt=0, le=60)
    max_active_jobs: int = Field(default=32, gt=0)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


class ReminderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    bot_alias: str
    text: str
    evidence_ids: list[str]
    at: str


class JobArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: str


class ControlArgs(JobArgs):
    operation: Literal["pause", "cancel"]


def catalog(
    limits: Mapping[Audience, int],
) -> tuple[tuple[ToolContract, ...], tuple[ToolPolicy, ...]]:
    fields = (
        "job_id",
        "state",
        "text",
        "notification_state",
        "reason",
        "unverified_count",
        "at",
        "expires_at",
        "receipt_id",
        "checked_at",
        "last_success_at",
    )
    contracts, policies = [], []
    for name, args, effect, description in (
        (
            PROPOSE,
            ReminderArgs,
            "propose",
            "起草一次性固定提醒，不能立即发送。at 为有时区的绝对 ISO 时间，"
            "先依据当前业务时间解析明天等说法；目标用已授权机器人别名。text 是明确文字或模型说明，"
            "业务事实选可见 evidence_ids，由代码渲染。全部可见历史均形成定义依赖，"
            "不能抄写结果洗掉依赖。代码展示最终时间、时区、内容、目标及迟到策略，"
            "随后请求本源 Action 执行，等待有权用户确认。",
        ),
        (ACTIVATE, JobArgs, "write", "仅本次已批准的任务激活，由 Action 执行，不直接提供给模型。"),
        (
            STATUS,
            JobArgs,
            "read",
            "查询本群已知任务 ID 的状态；仅创建者或当前管理员可管理/查看。"
            "数据不可读或过期时只返回安全状态，不展示正文。状态为接口接受不代表群成员已读。",
        ),
        (
            CONTROL,
            ControlArgs,
            "task_control",
            "直接暂停或取消本群已知任务，不需要生产动作审批。仅当前可核验群成员中的创建者或管理员；不恢复、不创建、不改内容，不撤回已准入发送。",
        ),
    ):
        policy_id = name.replace("/", ".") + "/1"
        policy = (
            proposal_policy(policy_id, args, min(limits.values()))
            if effect == "propose"
            else ToolPolicy(
                policy_id,
                args,
                {a: Projection(fields, limits[a]) for a in AUDIENCES},
                effect="write"
                if name == ACTIVATE
                else "task_control"
                if name == CONTROL
                else "read",
                group_only=True,
                fact_note=(
                    "任务状态与发送回执分别读取。accepted仅表示接口接受；unknown或sending可能已发出，"
                    "不能自动重发。unavailable不证明未发送；请求的管理操作未执行。"
                ),
                required=(
                    "job_id",
                    "state",
                    "text",
                    "notification_state",
                    "reason",
                    "unverified_count",
                ),
            )
        )
        contracts.append(
            ToolContract(
                tool_id=name,
                target_id=TARGET,
                policy_id=policy_id,
                input_schema=args.model_json_schema(),
                description=description,
            )
        )
        policies.append(policy)
    return tuple(contracts), tuple(policies)


@dataclass(frozen=True)
class TaskDefinitions:
    """本地任务材料的读取边界；在线与维护入口共用，不解析凭据或创建发送客户端。"""

    engine: AsyncEngine
    evidence: EvidenceStore
    access: AccessPolicy
    bots: Mapping[str, WebhookBotConfig]
    clock: Callable[[], datetime]
    budget: Budget
    model_tools: frozenset[str]

    async def decision(self, identity: Identity, *, member: bool = True) -> AccessDecision | None:
        from xiaowei.channel import GroupScope

        if identity.channel != "feishu" or identity.owner.kind != "group":
            return None
        app, tenant, chat = json.loads(identity.owner.id)
        try:
            return await self.access.resolve_group(
                GroupScope(app_id=app, tenant_key=tenant, chat_id=chat),
                identity.subject_id,
                verify_member=member,
            )
        except Exception:
            if not member:
                raise
            return None  # 成员目录故障不证明确定撤权，也不沿用上次成员资格。

    async def get(self, job_id: str) -> dict[str, Any]:
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sql(
                            "SELECT j.*, n.state AS notification_state, n.receipt_id "
                            "FROM xiaowei_scheduled_job j LEFT JOIN xiaowei_notification_attempt n "
                            "ON n.job_id=j.job_id WHERE j.job_id=:id"
                        ),
                        {"id": job_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise ToolRejectedError("任务不可用", code="not_allowed")
        return dict(row)

    async def validate(
        self,
        job: Mapping[str, Any],
        viewer: RunContext | None = None,
        *,
        recipient_tools: frozenset[str] | None = None,
        recipient_targets: frozenset[str] | None = None,
    ) -> RunContext:
        source = RunContext.model_validate_json(job["source_context"])
        identity = source.identity
        if viewer is not None:
            if viewer.identity.owner != identity.owner or viewer.identity.channel != "feishu":
                raise EvidenceUnavailableError
            identity = identity.model_copy(update={"subject_id": viewer.identity.subject_id})
        # 普通读取/回放沿用入口已核实的本轮成员资格；后台准入另做当前目录核验。
        decision = await self.decision(identity, member=viewer is None)
        if decision is None:
            if viewer is not None:
                raise EvidenceUnavailableError
            raise EvidenceUnverifiableError
        if NOTIFY not in decision.authorized_tools and viewer is None:
            raise EvidenceUnavailableError
        budget = source.budget.model_copy(
            update={
                name: min(getattr(source.budget, name), getattr(self.budget, name))
                for name in Budget.model_fields
            }
        )
        ctx = source.model_copy(
            update={"identity": identity, "target_scope": decision.target_ids, "budget": budget}
        )
        if viewer is not None:
            recipient_tools = self.model_tools & (
                recipient_tools if recipient_tools is not None else self.model_tools
            )
        if job["text"] is None or self.clock() >= job["content_expires_at"]:
            raise EvidenceUnavailableError
        bot = self.bots.get(job["bot_alias"])
        if bot is None or (bot.bot_id, bot.target_version) != (
            job["bot_id"],
            job["target_version"],
        ):
            raise EvidenceUnavailableError
        ids = tuple(json.loads(job["definition_evidence_ids"]))
        await self.evidence.validate_answer(
            TurnAnswer(
                answer=AgentAnswer(
                    blocks=(TextBlock(type="text", text="核验任务定义"),),
                    report_format=None,
                    awaiting_user=False,
                ),
                context_evidence=ids,
                material_only=True,
            ),
            ctx,
            recipient_tools=(bot.receive_tools | {NOTIFY})
            & (recipient_tools if recipient_tools is not None else bot.receive_tools | {NOTIFY}),
            recipient_targets=(bot.receive_targets | {"notifications"})
            & (
                recipient_targets
                if recipient_targets is not None
                else bot.receive_targets | {"notifications"}
            ),
            include_remaining_results=False,
        )
        return ctx

    async def check_content(
        self,
        record: EvidenceRecord,
        ctx: RunContext,
        recipient_tools: frozenset[str] | None = None,
        recipient_targets: frozenset[str] | None = None,
    ) -> None:
        if record.target_id != TARGET:
            return
        data = json.loads(record.projections["model"])["data"]
        job_id = data.get("job_id")
        if job_id is None and "parameters" in data:
            job_id = json.loads(data["parameters"]).get("job_id")
        if job_id is not None and data.get("text", "") is not None:
            job = await self.get(job_id)
            # 纯文字的历史副本沿普通 Evidence 保留；迟到窗口不是整群会话的数据期限。
            if json.loads(job["definition_evidence_ids"]):
                await self.validate(
                    job,
                    ctx,
                    recipient_tools=recipient_tools,
                    recipient_targets=recipient_targets,
                )


class ScheduledTasks:
    def __init__(
        self,
        config: ScheduledConfig,
        governance: GovernedTools,
        engine: AsyncEngine,
        lock: InstanceLock,
        definitions: TaskDefinitions,
        notifications: WebhookNotifications,
        *,
        is_admin: Callable[[str], bool],
    ) -> None:
        self.config, self.governance, self.engine = config, governance, engine
        self.lock, self.definitions, self.notifications = lock, definitions, notifications
        self.clock, self.is_admin = definitions.clock, is_admin
        self.budget = definitions.budget
        self._tick_lock = asyncio.Lock()
        self.run_slot: Callable[[], AbstractContextManager[bool]] = lambda: nullcontext(False)

    async def _admit(self, conn: AsyncConnection) -> None:
        if not self.lock.readiness.ok or not await self.lock.admits(conn):
            raise StorageUnavailableError("实例未就绪，定时任务未准入")

    def _plan(self, job: Mapping[str, Any]) -> ActionPlan:
        zone = ZoneInfo(self.config.timezone)
        at = job["run_at"].astimezone(zone).isoformat()
        expiry = job["content_expires_at"].astimezone(zone).isoformat()
        return ActionPlan(
            parameters={"job_id": job["job_id"]},
            revision="1",
            change=(
                f"一次性提醒 {job['job_id']}\n{at}（{self.config.timezone}）\n"
                f"机器人：{job['bot_alias']}\n内容：\n{job['text']}\n"
                f"允许迟到：{self.config.lateness_seconds / 3600:g}小时；最多发送一次；"
                f"最远提前 {self.config.max_advance_seconds / 86400:g}天；"
                f"活动任务上限 {self.config.max_active_jobs}个。\n"
                f"本任务将于 {expiry} 时失效。"
                + (
                    "起草时包含历史 Evidence，不能续期；如需无旧依赖版本，"
                    "用户可显式 /新建 后完整重述，"
                    "这会轮换全群共享上下文，不能自动新建。"
                    if json.loads(job["definition_evidence_ids"])
                    else ""
                )
            ),
            impact="确认后由创建者＋原群的当前权限到期发送一次；不执行生产写，不启动模型。",
            rollback="创建者或当前群成员管理员可直接暂停/取消；已准入发送不能撤回。",
            content_expires_at=job["content_expires_at"]
            if json.loads(job["definition_evidence_ids"])
            else None,
        )

    async def propose(
        self, ctx: RunContext, request: ToolRequest, visible: Sequence[str]
    ) -> ActionPlan:
        if not self.config.enabled:
            raise ProposalRejectedError("定时任务未启用", code="not_allowed")
        args = ReminderArgs.model_validate(request.arguments)
        try:
            at = datetime.fromisoformat(args.at)
        except ValueError:
            raise ProposalRejectedError("无效日期", hint="reminder_time") from None
        if at.tzinfo is None or not self.clock() < at <= self.clock() + timedelta(
            seconds=self.config.max_advance_seconds
        ):
            raise ProposalRejectedError("无效日期", hint="reminder_time")
        bot = self.notifications.bot_config(args.bot_alias)
        if bot is None:
            raise ProposalRejectedError("机器人尚未配置或授权", hint="reminder_target")
        if (
            not set(args.evidence_ids) <= set(visible)
            or (not args.text.strip() and not args.evidence_ids)
            or len(args.text.encode()) > MAX_ANSWER_TEXT_BYTES
            or (len(set(args.evidence_ids)) + bool(args.text.strip()) > MAX_ANSWER_BLOCKS)
        ):
            raise ProposalRejectedError("内容无效", hint="reminder_content")
        decision = await self.definitions.decision(ctx.identity)
        if decision is None or NOTIFY not in decision.authorized_tools:
            raise ProposalRejectedError("当前无权创建该通知任务", code="not_allowed")
        blocks: list[TextBlock | FactBlock] = (
            [TextBlock(type="text", text=args.text)] if args.text.strip() else []
        )
        blocks.extend(
            FactBlock(type="fact", evidence_id=e, view="full")
            for e in dict.fromkeys(args.evidence_ids)
        )
        try:
            delivery = await self.governance.evidence.validate_answer(
                TurnAnswer(
                    answer=AgentAnswer(
                        blocks=tuple(blocks), report_format=None, awaiting_user=False
                    ),
                    context_evidence=tuple(visible),
                    material_only=True,
                ),
                ctx,
                recipient_tools=bot.receive_tools | {NOTIFY},
                recipient_targets=bot.receive_targets | {"notifications"},
                include_remaining_results=False,
            )
        except RecipientRejectedError:
            raise ProposalRejectedError(
                "接收范围不允许", code="not_allowed", hint="reminder_recipient"
            ) from None
        except EvidenceUnverifiableError:
            raise ProposalRejectedError("证据未核实", code="authorization_unavailable") from None
        except (EvidenceUnavailableError, AnswerRejectedError):
            raise ProposalRejectedError("证据不可用", code="not_allowed") from None
        expiry = min(
            at + timedelta(seconds=self.config.lateness_seconds),
            delivery.content_expires_at or at + timedelta(seconds=self.config.lateness_seconds),
        )
        if at >= expiry:
            raise ProposalRejectedError(
                "发送时间晚于定义依赖的数据期限", code="not_allowed", hint="reminder_expiry"
            )
        content = _notification_text(delivery, args.text)
        if not self.notifications.fixed_message_fits(args.bot_alias, content):
            raise ProposalRejectedError(
                "完整通知超过20000字节", code="capacity_exceeded", hint="reminder_content"
            )
        job = dict(
            job_id=uuid4().hex,
            owner_id=ctx.identity.owner.id,
            creator_id=ctx.identity.subject_id,
            bot_id=bot.bot_id,
            bot_alias=bot.alias,
            target_version=bot.target_version,
            run_at=at.astimezone(UTC),
            content_expires_at=expiry,
            text=content,
            definition_evidence_ids=json.dumps(tuple(visible)),
            source_context=ctx.model_dump_json(),
            lateness_seconds=self.config.lateness_seconds,
        )
        async with self.engine.begin() as conn:
            await self._admit(conn)
            await conn.execute(
                sql(
                    "INSERT INTO xiaowei_scheduled_job "
                    "(job_id,owner_id,creator_id,bot_id,bot_alias,"
                    "target_version,run_at,content_expires_at,text,"
                    "definition_evidence_ids,source_context,lateness_seconds,state) VALUES "
                    "(:job_id,:owner_id,:creator_id,:bot_id,:bot_ali"
                    "as,:target_version,:run_at,:content_expires_at,"
                    ":text,:definition_evidence_ids,:source_context,:lateness_seconds,'draft')"
                ),
                job,
            )
        return self._plan(job)

    async def prepare(self, request: ToolRequest) -> ActionPlan:
        job = await self.definitions.get(str(request.arguments["job_id"]))
        await self.definitions.validate(job)
        return self._plan(job)

    async def activate(self, request: ToolRequest) -> ToolObservation:
        job = await self.definitions.get(str(request.arguments["job_id"]))
        await self.definitions.validate(job)
        async with self.engine.begin() as conn:
            await self._admit(conn)
            await conn.execute(sql("LOCK TABLE xiaowei_scheduled_job IN SHARE ROW EXCLUSIVE MODE"))
            active = await conn.scalar(
                sql("SELECT count(*) FROM xiaowei_scheduled_job WHERE state='active'")
            )
            if active >= self.config.max_active_jobs:
                raise ActionWriteRejectedError("活动任务已达配置上限，未激活")
            result = await conn.execute(
                sql(
                    "UPDATE xiaowei_scheduled_job SET state='active', approved_at=:now "
                    "WHERE job_id=:id AND state='draft'"
                ),
                {"id": job["job_id"], "now": self.clock()},
            )
            if result.rowcount != 1:
                raise ActionWriteRejectedError("该任务版本不能再次激活")
            await conn.execute(
                sql(
                    "UPDATE xiaowei_scheduled_job j SET approval_id=a.action_id, "
                    "approver_id=a.approver_id FROM xiaowei_action a "
                    "WHERE j.job_id=:id AND a.attempt=:attempt AND a.target_id='scheduled' "
                    "AND a.state='executing'"
                ),
                {"id": job["job_id"], "attempt": request.call_id},
            )
        return self._observation(job, "active", body=False)

    def binding(self) -> ActionBinding:
        cat = self.governance.catalog
        proposal, write = cat.contract(PROPOSE, TARGET), cat.contract(ACTIVATE, TARGET)
        assert proposal is not None and write is not None  # noqa: S101 - 可信装配已登记
        return ActionBinding(
            proposal,
            write,
            self.prepare,
            self.activate,
            target_binding="fixed-reminder/1",
            available=lambda: self.config.enabled,
            prepare_proposal=self.propose,
            check_material=self.check_material,
        )

    async def check_material(self, ctx: RunContext, action: ActionRecord) -> None:
        job_id = json.loads(action.plan)["plan"]["parameters"]["job_id"]
        await self.definitions.validate(await self.definitions.get(job_id), ctx)

    def _observation(
        self, job: Mapping[str, Any], state: str | None = None, *, body: bool = True
    ) -> ToolObservation:
        return ToolObservation(
            payload=dict(
                job_id=job["job_id"],
                state=state or job["state"],
                at=job["run_at"].isoformat(),
                expires_at=job["content_expires_at"].isoformat(),
                reason=job.get("reason", "pending"),
                unverified_count=job.get("unverified_count", 0),
                text=job["text"] if body else None,
                notification_state=job.get("notification_state") or "not_sent",
                receipt_id=job.get("receipt_id"),
                checked_at=job["checked_at"].isoformat() if job.get("checked_at") else None,
                last_success_at=job["last_success_at"].isoformat()
                if job.get("last_success_at")
                else None,
            ),
            captured_at=self.clock(),
            truncated=False,
            content_expires_at=job["content_expires_at"]
            if body and json.loads(job["definition_evidence_ids"])
            else None,
        )

    def tools_for(self, ctx: RunContext) -> list[FunctionTool]:
        tools = []
        for contract in self.governance.allowed_contracts(ctx):
            if contract.tool_id not in {STATUS, CONTROL}:
                continue

            async def execute(
                request: ToolRequest, *, controlling: bool = contract.tool_id == CONTROL
            ) -> ToolObservation:
                job_id = str(request.arguments["job_id"])

                def unavailable() -> ToolObservation:
                    return ToolObservation(
                        payload=dict(
                            job_id=job_id,
                            state="unavailable",
                            text=None,
                            notification_state="unverifiable",
                            reason="not_authorized_or_unavailable",
                            unverified_count=None,
                        ),
                        captured_at=self.clock(),
                        truncated=False,
                    )

                try:
                    job = await self.definitions.get(job_id)
                except ToolRejectedError:
                    return unavailable()
                decision = await self.definitions.decision(ctx.identity, member=controlling)
                if (
                    decision is None
                    or job["owner_id"] != ctx.identity.owner.id
                    or (
                        job["creator_id"] != ctx.identity.subject_id
                        and not self.is_admin(ctx.identity.subject_id)
                    )
                ):
                    return unavailable()
                if controlling:
                    state = "paused" if request.arguments["operation"] == "pause" else "cancelled"
                    async with self.engine.begin() as conn:
                        await self._admit(conn)
                        await conn.execute(
                            sql(
                                "UPDATE xiaowei_scheduled_job SET state=:state "
                                "WHERE job_id=:id AND state IN ('draft','active','paused')"
                            ),
                            {"id": job["job_id"], "state": state},
                        )
                    job = await self.definitions.get(job["job_id"])
                if job.get("notification_state") in {"unknown", "sending"}:
                    await self.notifications.record_task_warning(ctx.identity, job["receipt_id"])
                try:
                    await self.definitions.validate(job, ctx)
                except (EvidenceUnavailableError, EvidenceUnverifiableError, AnswerRejectedError):
                    return self._observation(job, body=False)
                return self._observation(job)

            tools.append(
                governed_function_tool(
                    contract.tool_id.replace("/", "__"), contract, self.governance, execute
                )
            )
        return tools

    async def recover(self) -> None:
        async with self.engine.begin() as conn:
            await self._admit(conn)
            await conn.execute(
                sql(
                    "UPDATE xiaowei_scheduled_job j SET state='completed', reason=n.reason "
                    "FROM xiaowei_notification_attempt n WHERE n.job_id=j.job_id "
                    "AND j.state='active'"
                )
            )

    async def _claim(self, job: Mapping[str, Any], content: str) -> tuple[str, str, str, bool]:
        async with self.engine.begin() as conn:
            await self._admit(conn)
            if not self.config.enabled:
                return "none", "not_sent", "scheduler_disabled", False
            current = (
                await conn.execute(
                    sql("SELECT state FROM xiaowei_scheduled_job WHERE job_id=:id FOR UPDATE"),
                    {"id": job["job_id"]},
                )
            ).scalar_one()
            if current != "active":
                return "none", "not_sent", "task_not_active", False
            if self.clock() >= job["content_expires_at"]:
                await conn.execute(
                    sql(
                        "UPDATE xiaowei_scheduled_job SET state='disabled', "
                        "reason='definition_expired' WHERE job_id=:id"
                    ),
                    {"id": job["job_id"]},
                )
                return "none", "not_sent", "definition_expired", False
            old = (
                await conn.execute(
                    sql(
                        "SELECT receipt_id,state,reason FROM xiaowei_notification_attempt "
                        "WHERE job_id=:id"
                    ),
                    {"id": job["job_id"]},
                )
            ).first()
            if old is not None:
                return old.receipt_id, old.state, old.reason, False
            permitted = await self.notifications.warning_seen(conn, job["owner_id"], job["bot_id"])
            receipt = uuid4().hex
            state, reason = (
                ("sending", "pending") if permitted else ("not_sent", "duplicate_warning")
            )
            await conn.execute(
                sql(
                    "INSERT INTO xiaowei_notification_attempt "
                    "(receipt_id,job_id,bot_id,bot_alias,target_version,"
                    "content_digest,state,reason) "
                    "VALUES (:receipt,:id,:bot,:alias,:version,:digest,:state,:reason)"
                ),
                dict(
                    receipt=receipt,
                    id=job["job_id"],
                    bot=job["bot_id"],
                    alias=job["bot_alias"],
                    version=job["target_version"],
                    digest=hashlib.sha256(content.encode()).hexdigest(),
                    state=state,
                    reason=reason,
                ),
            )
            await conn.execute(
                sql(
                    "UPDATE xiaowei_scheduled_job SET state=:state, reason=:reason, "
                    "checked_at=:now,unverified_count=0 WHERE job_id=:id"
                ),
                {
                    "id": job["job_id"],
                    "reason": reason,
                    "now": self.clock(),
                    "state": "active" if permitted else "completed",
                },
            )
            return receipt, state, reason, permitted

    async def _settle(self, job_id: str, receipt: str, state: str, reason: str) -> None:
        try:
            async with self.engine.begin() as conn:
                result = await conn.execute(
                    sql(
                        "UPDATE xiaowei_notification_attempt SET state=:state,reason=:reason "
                        "WHERE receipt_id=:receipt AND state='sending'"
                    ),
                    dict(receipt=receipt, state=state, reason=reason),
                )
                if result.rowcount != 1:
                    raise StorageUnavailableError("定时发送结果无法落定")
                await conn.execute(
                    sql(
                        "UPDATE xiaowei_scheduled_job SET state='completed',reason=:reason, "
                        "last_success_at=CASE WHEN :state='accepted' THEN :now "
                        "ELSE last_success_at END "
                        "WHERE job_id=:id"
                    ),
                    dict(id=job_id, reason=reason, state=state, now=self.clock()),
                )
        except BaseException:
            self.lock.readiness.lock("scheduled_settle_failed")
            raise

    async def tick(self) -> None:
        """一次有界到期检查；假时钟测试与进程内循环共用，不是外部入口。"""
        if not self.config.enabled or not self.lock.readiness.ok:
            return
        async with self._tick_lock:
            async with self.engine.connect() as conn:
                jobs = (
                    (
                        await conn.execute(
                            sql(
                                "SELECT j.* FROM xiaowei_scheduled_job j "
                                "WHERE j.state='active' AND j.run_at<=:now AND NOT EXISTS "
                                "(SELECT 1 FROM xiaowei_notification_attempt n "
                                "WHERE n.job_id=j.job_id) "
                                "ORDER BY run_at,job_id LIMIT :limit"
                            ),
                            {"now": self.clock(), "limit": self.config.max_active_jobs},
                        )
                    )
                    .mappings()
                    .all()
                )
            for row in jobs:
                job = dict(row)
                if not self.lock.readiness.ok:
                    return
                if self.clock() >= job["run_at"] + timedelta(seconds=job["lateness_seconds"]):
                    await self._mark(job, "missed", "lateness_exceeded")
                    continue
                with self.run_slot() as reserved:
                    if not reserved:
                        await self._mark(job, "active", "busy")
                        continue
                    try:
                        with scope_checks():
                            approved_budget = RunContext.model_validate_json(
                                job["source_context"]
                            ).budget
                            async with asyncio.timeout(
                                min(self.budget.timeout_seconds, approved_budget.timeout_seconds)
                            ):
                                await self._run_job(job)
                    except Exception as exc:
                        logger.error(
                            "定时任务本次未完成：job=%s error=%s", job["job_id"], type(exc).__name__
                        )
                        if not self.lock.readiness.ok:
                            raise
                        await self._mark(job, "active", "execution_unverified", unverified=True)

    async def _run_job(self, job: Mapping[str, Any]) -> None:
        original = RunContext.model_validate_json(job["source_context"])
        # 静态授权确定被撤销时停用；成员核验失败不能当作确定撤权。
        configured = await self.definitions.decision(original.identity, member=False)
        if configured is None or NOTIFY not in configured.authorized_tools:
            await self._mark(job, "disabled", "authorization_revoked")
            return
        try:
            ctx = await self.definitions.validate(job)
        except EvidenceUnverifiableError:
            await self._mark(job, "active", "authorization_unverified", unverified=True)
            return
        except (EvidenceUnavailableError, AnswerRejectedError):
            await self._mark(job, "disabled", "definition_unavailable")
            return
        if not self.notifications.fixed_message_fits(job["bot_alias"], job["text"]):
            await self._mark(job, "disabled", "content_too_large")
            return
        identity = ctx.identity.model_copy(
            update={"session_id": uuid4().hex, "turn_id": uuid4().hex}
        )
        sending = ctx.model_copy(
            update={
                "identity": identity,
                "target_scope": frozenset({"notifications"}),
                "tool_scope": frozenset({NOTIFY}),
                "evidence_ids": (),
                "approved_action": None,
            }
        )
        try:
            await self.notifications.send_fixed(
                sending,
                job["bot_alias"],
                job["text"],
                partial(self._claim, job),
                partial(self._settle, job["job_id"]),
            )
        except BaseException:
            await self._interrupt(job["job_id"])
            raise
        finally:
            self.governance.end_turn(identity)

    async def _interrupt(self, job_id: str) -> None:
        """有机会清理的取消/超时立刻落定未知；失锁或硬退出交给启动恢复。"""
        if not self.lock.readiness.ok:
            return
        try:
            async with asyncio.timeout(ACTION_SETTLE_TIMEOUT_SECONDS), self.engine.begin() as conn:
                await self._admit(conn)
                changed = await conn.execute(
                    sql(
                        "UPDATE xiaowei_notification_attempt SET s"
                        "tate='unknown',reason='response_unknown' "
                        "WHERE job_id=:id AND state='sending'"
                    ),
                    {"id": job_id},
                )
                if changed.rowcount:
                    await conn.execute(
                        sql(
                            "UPDATE xiaowei_scheduled_job SET state='completed',"
                            "reason='response_unknown' WHERE job_id=:id"
                        ),
                        {"id": job_id},
                    )
        except BaseException:
            self.lock.readiness.lock("scheduled_interrupt_failed")
            raise

    async def _mark(
        self, job: Mapping[str, Any], state: str, reason: str, *, unverified: bool = False
    ) -> None:
        async with self.engine.begin() as conn:
            await self._admit(conn)
            await conn.execute(
                sql(
                    "UPDATE xiaowei_scheduled_job SET state=:state,reason=:reason,checked_at=:now, "
                    "unverified_count=CASE WHEN :unverified THEN unverified_count+1 "
                    "WHEN :reason='busy' THEN unverified_count ELSE 0 END "
                    "WHERE job_id=:id AND state='active' AND NOT EXISTS "
                    "(SELECT 1 FROM xiaowei_notification_attempt n WHERE n.job_id=:id)"
                ),
                dict(
                    id=job["job_id"],
                    state=state,
                    reason=reason,
                    now=self.clock(),
                    unverified=unverified,
                ),
            )
        if unverified:
            logger.warning("定时任务当前权限未核实：job=%s", job["job_id"])

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                # 一次失败不重放已占用的 occurrence，也不记录任务正文或上游异常原文。
                logger.error("定时检查失败：error=%s", type(exc).__name__)
            await asyncio.sleep(self.config.poll_seconds)
