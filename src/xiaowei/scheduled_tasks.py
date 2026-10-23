"""同进程固定提醒与独立只读 Agent 任务；复用 Action、SDK 和受治理通知。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from agents import FunctionTool
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
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
    from xiaowei.app import Application
    from xiaowei.channel import AccessDecision, AccessPolicy
    from xiaowei.channel_store import ActionRecord

TARGET = "scheduled"
LIST_TASKS = f"{TARGET}/list_tasks"
AGENT_PROPOSE = f"{TARGET}/propose_agent_task"
PROPOSE = f"{TARGET}/propose_reminder"
ACTIVATE = f"{TARGET}/activate_reminder"
STATUS = f"{TARGET}/get_task"
CONTROL = f"{TARGET}/control_task"
TOOLS = frozenset(
    {
        PROPOSE,
        AGENT_PROPOSE,
        LIST_TASKS,
        ACTIVATE,
        STATUS,
        CONTROL,
        f"{TARGET}/get_action_status",
    }
)
logger = logging.getLogger(__name__)


class ScheduledConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = False
    timezone: str = "Asia/Shanghai"
    max_advance_seconds: int = Field(default=30 * 86400, gt=0)
    lateness_seconds: int = Field(default=3600, gt=0)
    poll_seconds: float = Field(default=5, gt=0, le=60)
    max_active_jobs: int = Field(default=32, gt=0)
    automatic_tools: frozenset[str] = frozenset()
    queue_timeout_seconds: int = Field(default=900, gt=0)
    start_tolerance_seconds: float = Field(default=60, gt=0)
    max_long_jobs_per_group: int = Field(default=30, gt=0, le=30)

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


class AgentTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    bot_alias: str
    task: str
    target_ids: list[str]
    tool_ids: list[str]
    at: str | None
    interval_seconds: int | None = Field(ge=3600)
    expires_at: str | None


class AgentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tools: frozenset[str]
    targets: frozenset[str]
    interval_seconds: int | None
    expires_at: AwareDatetime | None
    explicit_start: bool


class JobArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: str


class TaskListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int = Field(ge=1, le=30)
    after_id: str | None
    include_text: bool


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
        (
            AGENT_PROPOSE,
            AgentTaskArgs,
            "propose",
            "起草到期独立执行的只读Agent任务，不能立即执行。task为用户可确认的规范化说明；"
            "target_ids为已授权源，tool_ids为管理员允许自动执行的只读工具ID。"
            "at为有时区绝对ISO时间；interval_seconds为固定周期，单次任务为null。"
            "周期首跑未指定时at=null，激活即首跑；周期至少1小时。expires_at为带时区的绝对到期时间，"
            "仅周期任务可填；未指定则null长期。先澄清缺少的业务口径；"
            "可从可信上下文确定的不重复问。代码冻结全部可见Evidence依赖、显示期限/权限/预算，"
            "再经现有Action确认。后台重新取证，不沿用历史结论、不执行生产写。",
        ),
        (
            LIST_TASKS,
            TaskListArgs,
            "read",
            "列出本群当前用户可见的任务及期限、下次运行等元数据。limit为1至30；"
            "next_cursor非空时可用after_id继续，未列全须说明。include_text仅确需正文时为true，"
            "正文仍按当前数据权限核对；大正文可逐ID get_task，管理员管理权不授予业务数据读取权。",
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
            "直接暂停或取消本群已知任务，不需要生产动作审批。仅当前可核验群成"
            "员中的创建者或管理员；不恢复、不创建、不改内容，不撤回已准入发送。",
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
                    "任务状态与发送回执分别读取。accepted仅表示"
                    "接口接受；unknown或sending可能已发出，"
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
        if name == LIST_TASKS:
            policy = ToolPolicy(
                policy_id,
                args,
                {a: Projection(("tasks", "next_cursor"), limits[a]) for a in AUDIENCES},
                effect="read",
                group_only=True,
                required=("tasks", "next_cursor"),
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


class AuthorizationRevokedError(EvidenceUnavailableError):
    """后台静态授权已撤销；与定义证据不可用分开。"""


class CreatorAbsentError(EvidenceUnavailableError):
    """完整当前成员目录证明创建者已退群。"""


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
    member_status: Callable[[str, str], Awaitable[bool | None]] | None = None

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
                            "SELECT j.*, n.state AS notification_state, n.receipt_id, "
                            "n.reason AS notification_reason "
                            "FROM xiaowei_scheduled_job j LEFT JOIN LATERAL "
                            "(SELECT a.state,a.receipt_id,a.reason FROM "
                            "xiaowei_notification_attempt a "
                            "LEFT JOIN xiaowei_scheduled_run r ON r.run_id=a.run_id "
                            "WHERE "
                            "COALESCE(a.job_id,r.job_id)=j.job_id "
                            "ORDER BY a.sequence DESC LIMIT 1) n "
                            "ON TRUE WHERE j.job_id=:id"
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
        automatic = viewer is None and job.get("kind") == "agent_task"
        decision = await self.decision(identity, member=viewer is None and not automatic)
        if decision is None:
            if automatic:
                raise AuthorizationRevokedError
            if viewer is not None:
                raise EvidenceUnavailableError
            raise EvidenceUnverifiableError
        if automatic:
            spec = AgentSpec.model_validate_json(job["agent_spec"])
            if (
                NOTIFY not in decision.authorized_tools
                or not spec.tools <= decision.authorized_tools
                or not spec.targets <= decision.target_ids
            ):
                raise AuthorizationRevokedError
            if self.member_status is not None:
                _app, _tenant, chat = json.loads(identity.owner.id)
                try:
                    status = await self.member_status(chat, identity.subject_id)
                except Exception:
                    status = None
                if status is False:
                    raise CreatorAbsentError
                if status is not True:
                    raise EvidenceUnverifiableError
            elif await self.decision(identity, member=True) is None:
                raise EvidenceUnverifiableError
        elif viewer is None and NOTIFY not in decision.authorized_tools:
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
        if viewer is not None or automatic:
            recipient_tools = self.model_tools & (
                recipient_tools if recipient_tools is not None else self.model_tools
            )
        if job["text"] is None or (
            job["content_expires_at"] is not None and self.clock() >= job["content_expires_at"]
        ):
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
        for item in data.get("tasks", []):
            if item.get("text") is not None:
                await self.validate(
                    await self.get(item["job_id"]),
                    ctx,
                    recipient_tools=recipient_tools,
                    recipient_targets=recipient_targets,
                )
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
        self._running_jobs: dict[str, asyncio.Task[None]] = {}
        self.run_slot: Callable[[str | None], AbstractContextManager[bool]]
        self.run_slot = lambda _session: nullcontext(False)
        self.application: Application | None = None
        self.notice_sender: Callable[[str, str, str], Awaitable[str]] | None = None

    async def _admit(self, conn: AsyncConnection) -> None:
        if not self.lock.readiness.ok or not await self.lock.admits(conn):
            raise StorageUnavailableError("实例未就绪，定时任务未准入")

    def _plan(self, job: Mapping[str, Any]) -> ActionPlan:
        if job.get("kind") == "agent_task":
            return self._agent_plan(job)
        zone = ZoneInfo(self.config.timezone)
        at = job["run_at"].astimezone(zone).isoformat()
        expiry = job["content_expires_at"].astimezone(zone).isoformat()
        deadline = (job["run_at"] + timedelta(seconds=job["lateness_seconds"])).astimezone(zone)
        return ActionPlan(
            parameters={"job_id": job["job_id"]},
            revision="1",
            change=(
                f"一次性提醒 {job['job_id']}\n{at}（{self.config.timezone}）\n"
                f"机器人：{job['bot_alias']}\n内容：\n{job['text']}\n"
                "必须在提醒时间之前批准；到时或之后批准不激活，请重新创建。\n"
                f"已激活任务遇停机、繁忙或临时核验失败，允许迟到："
                f"{job['lateness_seconds'] / 3600:g}小时；最多发送一次；"
                f"最远提前 {self.config.max_advance_seconds / 86400:g}天；"
                f"活动任务上限 {self.config.max_active_jobs}个。\n"
                f"发送截止：{deadline.isoformat()}。本任务将于 {expiry} 时失效（正文期限）。"
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
        expiry = delivery.content_expires_at or at + timedelta(seconds=self.config.lateness_seconds)
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

    def _agent_plan(self, job: Mapping[str, Any]) -> ActionPlan:
        spec = AgentSpec.model_validate_json(job["agent_spec"])
        zone = ZoneInfo(self.config.timezone)
        start = job["run_at"].astimezone(zone).isoformat() if spec.explicit_start else "确认后尽快"
        deadline = self._agent_deadline(job)
        expiry = (
            deadline.astimezone(zone).isoformat() if deadline else "长期，直到取消或权限/数据失效"
        )
        budget = RunContext.model_validate_json(job["source_context"]).budget
        interval = f"{spec.interval_seconds}秒" if spec.interval_seconds else "仅一次"
        dependency_note = (
            "起草含历史Evidence，实际失效时间受最早数据期限限制，不能续期旧数据；"
            "如需无旧依赖版本，用户可显式 /新建 后完整重述，会轮换全群共享上下文，不能自动新建。"
            if json.loads(job["definition_evidence_ids"])
            else "每次新Session独立取证，不沿用原聊天历史。"
        )
        tolerance = max(self.config.start_tolerance_seconds, self.config.poll_seconds)
        long_limit = self.config.max_long_jobs_per_group
        return ActionPlan(
            parameters={"job_id": job["job_id"]},
            revision="1",
            change=(
                f"独立只读Agent任务 {job['job_id']}\n首次：{start}（{self.config.timezone}）\n"
                f"周期：{interval}；机器人：{job['bot_alias']}\n任务：{job['text']}\n"
                f"获准源：{sorted(spec.targets)}\n自动只读工具上界：{sorted(spec.tools)}\n"
                f"每次预算：最多{budget.max_turns}轮、{budget.max_tool_calls}次工具（含一次通知）、"
                f"{budget.timeout_seconds:g}秒、{budget.max_scope_checks}次数据范围核验。\n"
                f"错过不补跑；准点容差{tolerance:g}秒；"
                f"并发满同一时点可排队{self.config.queue_timeout_seconds}秒，超时skipped。"
                "周期按激活/指定锚点，不随结束时间漂移；"
                f"每群最多{long_limit}个长期任务。\n"
                f"本任务将于 {expiry} 时失效。" + dependency_note
            ),
            impact="创建者＋原群当前权限执行独立只读Agent任务；可信路径通知，不授予生产写。",
            rollback="创建者或当前群成员管理员可暂停/取消；已准入请求不能撤回。",
            content_expires_at=job["content_expires_at"]
            if json.loads(job["definition_evidence_ids"])
            else None,
        )

    async def propose_agent(
        self, ctx: RunContext, request: ToolRequest, visible: Sequence[str]
    ) -> ActionPlan:
        if not self.config.enabled or not self.config.automatic_tools or self.application is None:
            raise ProposalRejectedError("自动只读任务未配置或未启用", code="not_allowed")
        args = AgentTaskArgs.model_validate(request.arguments)
        now = self.clock()
        try:
            at = datetime.fromisoformat(args.at) if args.at is not None else now
        except ValueError:
            raise ProposalRejectedError("无效日期", hint="agent_task_time") from None
        if (
            at.tzinfo is None
            or (args.at is not None and at <= now)
            or (args.at is None and args.interval_seconds is None)
        ):
            raise ProposalRejectedError("无效日期", hint="agent_task_time")
        bot = self.notifications.bot_config(args.bot_alias)
        if bot is None:
            raise ProposalRejectedError("机器人尚未配置或授权", hint="reminder_target")
        decision = await self.definitions.decision(ctx.identity)
        tools, targets = frozenset(args.tool_ids), frozenset(args.target_ids)
        selected = ctx.model_copy(update={"target_scope": targets})
        if (
            decision is None
            or NOTIFY not in decision.authorized_tools
            or not tools
            or not targets
            or not targets <= decision.target_ids
            or not tools.issubset(
                self.application.automatic_scope(
                    selected, self.config.automatic_tools & decision.authorized_tools
                )
            )
        ):
            raise ProposalRejectedError("任务范围没有获准自动只读权限，未创建", code="not_allowed")
        if not tools <= bot.receive_tools or not targets <= bot.receive_targets:
            raise ProposalRejectedError(
                "机器人未获准接收任务所选源/工具", hint="reminder_recipient"
            )
        if (
            not args.task.strip()
            or len(args.task.encode()) > MAX_ANSWER_TEXT_BYTES
            or not self.application.admits_input(args.task)
            or ctx.budget.max_tool_calls < 2
        ):
            raise ProposalRejectedError("任务说明不合规或工具预算不足", code="not_allowed")
        try:
            delivery = await self.governance.evidence.validate_answer(
                TurnAnswer(
                    answer=AgentAnswer(
                        blocks=(TextBlock(type="text", text=args.task),),
                        report_format=None,
                        awaiting_user=False,
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
        try:
            until = datetime.fromisoformat(args.expires_at) if args.expires_at is not None else None
        except ValueError:
            raise ProposalRejectedError(
                "有效期须为绝对日期和时区", hint="agent_task_time"
            ) from None
        if until is not None and (
            args.interval_seconds is None or until.tzinfo is None or until <= max(now, at)
        ):
            raise ProposalRejectedError(
                "周期任务有效期无效；一次任务无需有效期", hint="agent_task_time"
            )
        expiry = delivery.content_expires_at
        if expiry is not None and max(now, at) >= expiry:
            raise ProposalRejectedError(
                "首次执行晚于定义期限", code="not_allowed", hint="reminder_expiry"
            )
        spec = AgentSpec(
            tools=tools,
            targets=targets,
            interval_seconds=args.interval_seconds,
            expires_at=until,
            explicit_start=args.at is not None,
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
            text=args.task,
            definition_evidence_ids=json.dumps(tuple(visible)),
            source_context=ctx.model_copy(
                update={
                    "budget": ctx.budget.model_copy(
                        update={
                            "max_turns": min(ctx.budget.max_turns, 30),
                            "max_tool_calls": min(ctx.budget.max_tool_calls, 30),
                            "timeout_seconds": min(ctx.budget.timeout_seconds, 600),
                        }
                    )
                }
            ).model_dump_json(),
            lateness_seconds=int(
                max(self.config.start_tolerance_seconds, self.config.poll_seconds)
            ),
            agent_spec=spec.model_dump_json(),
            kind="agent_task",
        )
        async with self.engine.begin() as conn:
            await self._admit(conn)
            await conn.execute(
                sql(
                    "INSERT INTO xiaowei_scheduled_job "
                    "(job_id,owner_id,creator_id,bot_id,bot_alias,"
                    "target_version,run_at,next_run,content_"
                    "expires_at,text,definition_evidence_ids,"
                    "source_context,lateness_seconds,kind,agent_spec,state) VALUES "
                    "(:job_id,:owner_id,:creator_id,:bot_id,:b"
                    "ot_alias,:target_version,:run_at,:run_at,"
                    ":content_expires_at,:text,:definition_evid"
                    "ence_ids,:source_context,:lateness_seconds,"
                    ":kind,:agent_spec,'draft')"
                ),
                job,
            )
        return self._plan(job)

    async def _finish_agent(
        self, job: Mapping[str, Any], run_id: str, state: str, reason: str
    ) -> None:
        spec = AgentSpec.model_validate_json(job["agent_spec"])
        try:
            async with self.engine.begin() as conn:
                await self._admit(conn)
                await conn.execute(
                    sql(
                        "UPDATE xiaowei_scheduled_run SET "
                        "state=:state,reason=:reason,finished_at=:now "
                        "WHERE run_id=:id"
                    ),
                    dict(id=run_id, state=state, reason=reason, now=self.clock()),
                )
                await conn.execute(
                    sql(
                        "UPDATE xiaowei_scheduled_job SET reason=:reason,checked_at=:now,"
                        "state=CASE WHEN :once THEN 'completed' ELSE state END,"
                        "last_success_at=CASE WHEN :success THEN :now ELSE last_success_at END,"
                        "unverified_count=CASE WHEN :unverified THEN unverified_count+1 ELSE 0 END,"
                        "next_run=CASE WHEN :unstarted THEN :next ELSE next_run END "
                        "WHERE job_id=:id AND state='active'"
                    ),
                    dict(
                        id=job["job_id"],
                        reason=reason,
                        now=self.clock(),
                        once=spec.interval_seconds is None,
                        success=state == "completed",
                        unverified=reason == "authorization_unverified",
                        unstarted=state == "skipped",
                        next=job["next_run"] + timedelta(seconds=spec.interval_seconds)
                        if spec.interval_seconds
                        else None,
                    ),
                )
        except BaseException:
            self.lock.readiness.lock("scheduled_run_settle_failed")
            raise

    async def _claim_agent_send(
        self, job: Mapping[str, Any], run_id: str, content: str, *, long_notice: str | None = None
    ) -> tuple[str, str, str, bool]:
        async with self.engine.begin() as conn:
            await self._admit(conn)
            current = (
                (
                    await conn.execute(
                        sql("SELECT * FROM xiaowei_scheduled_job WHERE job_id=:id FOR UPDATE"),
                        {"id": job["job_id"]},
                    )
                )
                .mappings()
                .one()
            )
            active = current["state"]
            deadline = self._agent_deadline(dict(current))
            if (
                not self.config.enabled
                or active != "active"
                or (deadline is not None and self.clock() >= deadline)
            ):
                return "none", "not_sent", "task_not_active", False
            old = (
                await conn.execute(
                    sql(
                        "SELECT receipt_id,state,reason FROM "
                        "xiaowei_notification_attempt WHERE run_id=:id"
                    ),
                    {"id": run_id},
                )
            ).first()
            if old is not None:
                return old.receipt_id, old.state, old.reason, False
            receipt = uuid4().hex
            await conn.execute(
                sql(
                    "INSERT INTO xiaowei_notification_attempt (receipt_id,run_id,bot_id,bot_alias,"
                    "target_version,content_digest,state,reason) VALUES "
                    "(:receipt,:run,:bot,:alias,:version,:digest,'sending','pending')"
                ),
                dict(
                    receipt=receipt,
                    run=run_id,
                    bot=job["bot_id"],
                    alias=job["bot_alias"],
                    version=job["target_version"],
                    digest=hashlib.sha256(content.encode()).hexdigest(),
                ),
            )
            if long_notice is not None and content.endswith(long_notice):
                await conn.execute(
                    sql(
                        "UPDATE xiaowei_scheduled_job SET last_long_notice_at=:now WHERE job_id=:id"
                    ),
                    {"id": job["job_id"], "now": self.clock()},
                )
            return receipt, "sending", "pending", True

    async def _settle_agent_send(self, run_id: str, receipt: str, state: str, reason: str) -> None:
        try:
            async with self.engine.begin() as conn:
                result = await conn.execute(
                    sql(
                        "UPDATE xiaowei_notification_attempt SET state=:state,reason=:reason "
                        "WHERE receipt_id=:receipt AND state='sending'"
                    ),
                    dict(state=state, reason=reason, receipt=receipt),
                )
                if result.rowcount != 1:
                    raise StorageUnavailableError("定时Agent通知无法落定")
        except BaseException:
            self.lock.readiness.lock("scheduled_settle_failed")
            raise

    async def _current_definition(self, job: Mapping[str, Any]) -> RunContext:
        """开始前统一核验当前范围、定义/接收方和三态成员；后续工具仍逐次治理。"""
        from xiaowei.app import TurnError

        try:
            current = await self.definitions.validate(job)
            spec = AgentSpec.model_validate_json(job["agent_spec"])
            if self.application is None or spec.tools != self.application.automatic_scope(
                current, spec.tools & self.config.automatic_tools
            ):
                raise AuthorizationRevokedError
            return current
        except AuthorizationRevokedError:
            await self._stop_agent(job, "authorization_revoked")
            raise TurnError("scope_rejected") from None
        except CreatorAbsentError:
            await self._stop_agent(job, "member_absent")
            raise TurnError("scope_rejected") from None
        except EvidenceUnverifiableError:
            raise TurnError("scope_unverifiable") from None
        except (EvidenceUnavailableError, AnswerRejectedError):
            bot = self.notifications.bot_config(job["bot_alias"])
            changed = bot is None or (bot.bot_id, bot.target_version) != (
                job["bot_id"],
                job["target_version"],
            )
            await self._stop_agent(
                job, "recipient_changed" if changed else "definition_unavailable"
            )
            raise TurnError("scope_rejected") from None

    async def validate_run(self, ctx: RunContext) -> None:
        """保存前仅检查可变任务状态和期限；不重复查成员目录或定义证据。"""
        from xiaowei.app import TurnError

        if ctx.definition_job_id is None:
            raise EvidenceUnavailableError
        job = await self.definitions.get(ctx.definition_job_id)
        deadline = self._agent_deadline(job)
        expired = deadline is not None and self.clock() >= deadline
        if job["state"] != "active" or expired:
            if expired:
                reason = (
                    "definition_expired"
                    if job["content_expires_at"] is not None
                    and self.clock() >= job["content_expires_at"]
                    else "validity_expired"
                )
                await self._stop_agent(job, reason)
            raise TurnError("scope_rejected")

    async def _send_agent_failure(
        self,
        job: Mapping[str, Any],
        run: Mapping[str, Any],
        ctx: RunContext,
        reason: str,
        description: str,
    ) -> None:
        """执行失败的一次固定状态发送；仍经当前定义与原通知账本。"""
        async with asyncio.timeout(10):
            with scope_checks():
                current = await self._current_definition(job)
                targets = ", ".join(
                    sorted(AgentSpec.model_validate_json(job["agent_spec"]).targets)
                )
                scheduled = (
                    run["scheduled_at"].astimezone(ZoneInfo(self.config.timezone)).isoformat()
                )
                content = (
                    f"⚪ 本次定时巡检未完成。任务〈{job['job_id']}〉；任务获准目标：{targets}；"
                    f"计划时刻：{scheduled}；实际查询时间窗：未取得；"
                    f"原因：{reason}（{description}）。请核对后单独补查。"
                )
                sending = current.model_copy(
                    update={
                        "identity": ctx.identity,
                        "automatic": False,
                        "tool_scope": frozenset({NOTIFY}),
                        "target_scope": frozenset({"notifications"}),
                        "content_expires_at": self._agent_deadline(job),
                        "evidence_ids": (),
                        "budget": current.budget.model_copy(update={"max_tool_calls": 1}),
                    }
                )
                outcome = await self.notifications.send_fixed(
                    sending,
                    job["bot_alias"],
                    content,
                    partial(self._claim_agent_send, job, run["run_id"]),
                    partial(self._settle_agent_send, run["run_id"]),
                )
                logger.info(
                    "scheduled_agent_failure_notice job=%s run=%s status=%s reason=%s",
                    job["job_id"],
                    run["run_id"],
                    outcome.status,
                    outcome.reason,
                )

    async def _run_agent(self, job: Mapping[str, Any]) -> None:
        from xiaowei.app import PausedTurn, TurnError

        if self.application is None:
            raise RuntimeError("后台Agent未装配")
        spec = AgentSpec.model_validate_json(job["agent_spec"])
        deadline = self._agent_deadline(job)
        if deadline is not None and self.clock() >= deadline:
            await self._stop_agent(
                job,
                "definition_expired"
                if job["content_expires_at"] is not None
                and self.clock() >= job["content_expires_at"]
                else "validity_expired",
            )
            return
        if job["state"] != "active":
            return
        if job["next_run"] is None or job["next_run"] > self.clock():
            return
        due = await self._schedule_agent(job)
        if due is None:
            return
        job, run = due
        with self.run_slot(run["session_id"]) as reserved:
            if not reserved:
                return
            try:
                ctx = await self._current_definition(job)
            except TurnError as exc:
                if exc.reason == "scope_unverifiable":
                    await self._finish_agent(
                        job, run["run_id"], "skipped", "authorization_unverified"
                    )
                return
            bot = self.notifications.bot_config(job["bot_alias"])
            assert bot is not None  # noqa: S101 - 开始前已核验可信接收配置。
            if ctx.budget.max_tool_calls < 2:
                await self._stop_agent(job, "budget_unavailable")
                return
            identity = ctx.identity.model_copy(
                update={"session_id": run["session_id"], "turn_id": run["turn_id"]}
            )
            budget = ctx.budget.model_copy(
                update={
                    "max_tool_calls": min(ctx.budget.max_tool_calls, 30) - 1,
                    "max_turns": min(ctx.budget.max_turns, 30),
                    "timeout_seconds": min(ctx.budget.timeout_seconds, 600),
                }
            )
            if deadline is not None:
                budget = budget.model_copy(
                    update={
                        "timeout_seconds": min(
                            budget.timeout_seconds,
                            max(0.001, (deadline - self.clock()).total_seconds()),
                        )
                    }
                )
            ctx = ctx.model_copy(
                update={
                    "identity": identity,
                    "automatic": True,
                    "definition_job_id": job["job_id"],
                    "content_expires_at": deadline,
                    "target_scope": spec.targets,
                    "tool_scope": spec.tools,
                    "budget": budget,
                    "evidence_ids": (),
                    "approved_action": None,
                }
            )
            if not await self._start_agent_run(job, run):
                return
            stage = "prepare_tools"
            try:
                with scope_checks():
                    async with asyncio.timeout(ctx.budget.timeout_seconds):
                        await self.application.prepare_tools(
                            "query",
                            ctx.tool_scope,
                            ctx.target_scope,
                            timeout_seconds=ctx.budget.timeout_seconds,
                        )
                        stage = "run_turn"
                        answer = await self.application.run_turn(ctx, job["text"])
                        if isinstance(answer, PausedTurn):
                            raise TurnError("scope_rejected")
                        stage = "validate_delivery"
                        delivery = await self.governance.evidence.validate_answer(
                            answer,
                            ctx,
                            recipient_tools=bot.receive_tools | {NOTIFY},
                            recipient_targets=bot.receive_targets | {"notifications"},
                        )
                        incomplete = (
                            (isinstance(answer.answer, AgentAnswer) and answer.answer.awaiting_user)
                            or answer.limit_reached
                            or bool(delivery.inspection_notice)
                        )
                        stage = "render"
                        content = _notification_text(delivery, "", include_remaining=True)
                        if incomplete:
                            content = (
                                "本次任务未完成；以下为预算内已获得的结果或待澄清问题。\n" + content
                            )
                        if (
                            deadline is not None
                            and spec.interval_seconds
                            and run["scheduled_at"] + timedelta(seconds=spec.interval_seconds)
                            >= deadline
                        ):
                            until = deadline.astimezone(ZoneInfo(self.config.timezone)).isoformat()
                            content += f"\n本次为到期前最后一次计划运行；任务将于{until}到期停止。"
                        since = job.get("last_long_notice_at") or job["approved_at"]
                        long_notice = None
                        if (
                            spec.interval_seconds
                            and spec.expires_at is None
                            and self.clock() - since >= timedelta(days=30)
                        ):
                            days = (self.clock() - job["approved_at"]).days
                            long_notice = (
                                f"本任务已长期运行 {days} 天，"
                                f"不再需要请 @小维 取消任务〈{job['job_id']}〉"
                            )
                            content += "\n" + long_notice
                        sending = ctx.model_copy(
                            update={
                                "automatic": False,
                                "tool_scope": frozenset({NOTIFY}),
                                "target_scope": frozenset({"notifications"}),
                                "budget": ctx.budget.model_copy(update={"max_tool_calls": 1}),
                            }
                        )
                        stage = "send"
                        outcome = await self.notifications.send_fixed(
                            sending,
                            job["bot_alias"],
                            content,
                            partial(
                                self._claim_agent_send, job, run["run_id"], long_notice=long_notice
                            ),
                            partial(self._settle_agent_send, run["run_id"]),
                            oversize_notice=f"任务〈{job['job_id']}〉本次结果超过通知容量，未发送正文。",
                        )
                        if outcome.reason == "sender_not_authorized":
                            state, reason = "skipped", "authorization_unverified"
                        elif outcome.reason == "content_too_large":
                            state, reason = "failed", "content_too_large"
                        elif outcome.status == "not_sent":
                            state, reason = "skipped", outcome.reason
                        else:
                            state = "failed" if incomplete else "completed"
                            reason = "incomplete" if incomplete else "execution_completed"
                        stage = "settle"
                        await self._finish_agent(
                            job,
                            run["run_id"],
                            state,
                            reason,
                        )
            except BaseException as exc:
                reason = (
                    exc.reason
                    if isinstance(exc, TurnError)
                    else ("timeout" if isinstance(exc, TimeoutError) else "execution_failed")
                )
                logger.error(
                    "定时Agent未完成：job=%s run=%s turn=%s stage=%s reason=%s error=%s "
                    "tool_error=%s model_requests=%s",
                    job["job_id"],
                    run["run_id"],
                    identity.turn_id,
                    stage,
                    reason,
                    type(exc).__name__,
                    exc.tool_error if isinstance(exc, TurnError) else "-",
                    "0" if stage == "prepare_tools" else "-",
                )
                if not self.lock.readiness.ok:
                    raise
                cancelled = isinstance(exc, asyncio.CancelledError)
                if stage not in {"send", "settle"} and not isinstance(exc, asyncio.CancelledError):
                    description = (
                        str(TurnError(exc.reason))
                        if isinstance(exc, TurnError)
                        else (str(TurnError("timeout")) if reason == "timeout" else "任务执行失败")
                    )
                    try:
                        await self._send_agent_failure(job, run, ctx, reason, description)
                    except (Exception, asyncio.CancelledError) as notice_error:
                        cancelled = cancelled or isinstance(notice_error, asyncio.CancelledError)
                        logger.warning(
                            "scheduled_agent_failure_notice job=%s run=%s "
                            "reason=unconfirmed error=%s",
                            job["job_id"],
                            run["run_id"],
                            type(notice_error).__name__,
                        )
                async with self.engine.begin() as conn:
                    await self._admit(conn)
                    await conn.execute(
                        sql(
                            "UPDATE xiaowei_notification_attempt SET "
                            "state='unknown',reason='response_unknown' "
                            "WHERE run_id=:id AND state='sending'"
                        ),
                        {"id": run["run_id"]},
                    )
                unverified = isinstance(exc, TurnError) and exc.reason == "scope_unverifiable"
                await self._finish_agent(
                    job,
                    run["run_id"],
                    "unknown"
                    if isinstance(exc, asyncio.CancelledError)
                    else ("skipped" if unverified else "failed"),
                    "authorization_unverified" if unverified else reason,
                )
                if cancelled:
                    raise asyncio.CancelledError from None
            finally:
                self.governance.end_turn(identity)
            if deadline is not None and self.clock() >= deadline:
                await self._stop_agent(
                    job,
                    "definition_expired"
                    if job["content_expires_at"] is not None
                    and self.clock() >= job["content_expires_at"]
                    else "validity_expired",
                )

    def _agent_deadline(self, job: Mapping[str, Any]) -> datetime | None:
        spec = AgentSpec.model_validate_json(job["agent_spec"])
        limits = [
            value for value in (job["content_expires_at"], spec.expires_at) if value is not None
        ]
        return min(limits) if limits else None

    async def _new_agent_run(
        self,
        conn: AsyncConnection,
        job: Mapping[str, Any],
        at: datetime,
    ) -> dict[str, Any]:
        run = dict(
            run_id=uuid4().hex,
            job_id=job["job_id"],
            scheduled_at=at,
            session_id=uuid4().hex,
            turn_id=uuid4().hex,
            state="queued",
            reason="waiting_for_slot",
            queued_at=self.clock(),
            finished_at=None,
        )
        await conn.execute(
            sql(
                "INSERT INTO xiaowei_scheduled_run (run_id,job_id,scheduled_at,session_id,turn_id,"
                "state,reason,queued_at,finished_at) VALUES "
                "(:run_id,:job_id,:scheduled_at,:session_id,:turn_id,:state,:reason,:queued_at,"
                ":finished_at)"
            ),
            run,
        )
        return run

    async def _schedule_agent(
        self, job: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        async with self.engine.begin() as conn:
            await self._admit(conn)
            row = (
                (
                    await conn.execute(
                        sql("SELECT * FROM xiaowei_scheduled_job WHERE job_id=:id FOR UPDATE"),
                        {"id": job["job_id"]},
                    )
                )
                .mappings()
                .one()
            )
            job = dict(row)
            if not self.config.enabled or job["state"] != "active":
                return None
            spec = AgentSpec.model_validate_json(job["agent_spec"])
            runs = (
                (
                    await conn.execute(
                        sql(
                            "SELECT * FROM xiaowei_scheduled_run WHERE job_id=:id "
                            "AND state IN ('queued','running') ORDER BY queued_at LIMIT 1"
                        ),
                        {"id": job["job_id"]},
                    )
                )
                .mappings()
                .first()
            )
            if runs is not None:
                if runs["state"] == "running":
                    return None
                if self.clock() < runs["queued_at"] + timedelta(
                    seconds=self.config.queue_timeout_seconds
                ):
                    return job, dict(runs)
                await conn.execute(
                    sql(
                        "UPDATE xiaowei_scheduled_run SET state='skipped',"
                        "reason='queue_timeout',finished_at=:now WHERE run_id=:id"
                    ),
                    {"id": runs["run_id"], "now": self.clock()},
                )
                following = (
                    runs["scheduled_at"] + timedelta(seconds=spec.interval_seconds)
                    if spec.interval_seconds
                    else None
                )
                await conn.execute(
                    sql(
                        "UPDATE xiaowei_scheduled_job SET next_run=:next,reason='queue_timeout',"
                        "state=CASE WHEN :once THEN 'completed' ELSE state END WHERE job_id=:id"
                    ),
                    {"id": job["job_id"], "next": following, "once": spec.interval_seconds is None},
                )
                return None
            at = job["next_run"]
            if at is None or at > self.clock():
                return None
            if spec.interval_seconds is not None:
                skipped = int((self.clock() - at).total_seconds() // spec.interval_seconds)
                if skipped:
                    at += timedelta(seconds=skipped * spec.interval_seconds)
                    await conn.execute(
                        sql(
                            "UPDATE xiaowei_scheduled_job SET next_run=:at,"
                            "reason='missed_occurrences' "
                            "WHERE job_id=:id"
                        ),
                        {"id": job["job_id"], "at": at},
                    )
                    job = {**job, "next_run": at}
            if self.clock() > at + timedelta(
                seconds=max(self.config.start_tolerance_seconds, self.config.poll_seconds)
            ):
                await conn.execute(
                    sql(
                        "UPDATE xiaowei_scheduled_job SET "
                        "next_run=:next,reason='start_time_missed',"
                        "state=CASE WHEN :once THEN 'completed' ELSE state END WHERE job_id=:id"
                    ),
                    {
                        "id": job["job_id"],
                        "next": at + timedelta(seconds=spec.interval_seconds)
                        if spec.interval_seconds
                        else None,
                        "once": spec.interval_seconds is None,
                    },
                )
                return None
            run = await self._new_agent_run(conn, job, at)
            return job, run

    async def _start_agent_run(self, job: Mapping[str, Any], run: Mapping[str, Any]) -> bool:
        async with self.engine.begin() as conn:
            await self._admit(conn)
            active = await conn.scalar(
                sql("SELECT state FROM xiaowei_scheduled_job WHERE job_id=:id FOR UPDATE"),
                {"id": job["job_id"]},
            )
            deadline = self._agent_deadline(job)
            if (
                not self.config.enabled
                or active != "active"
                or (deadline and self.clock() >= deadline)
            ):
                return False
            changed = await conn.execute(
                sql(
                    "UPDATE xiaowei_scheduled_run SET state='running',started_at=:now,"
                    "reason='pending' WHERE run_id=:id AND state='queued' "
                    "AND queued_at+:seconds*interval '1 second'>:now"
                ),
                {
                    "id": run["run_id"],
                    "now": self.clock(),
                    "seconds": self.config.queue_timeout_seconds,
                },
            )
            if changed.rowcount != 1:
                return False
            spec = AgentSpec.model_validate_json(job["agent_spec"])
            await conn.execute(
                sql(
                    "UPDATE xiaowei_scheduled_job SET next_run=:next,"
                    "checked_at=:now WHERE job_id=:id"
                ),
                {
                    "id": job["job_id"],
                    "now": self.clock(),
                    "next": run["scheduled_at"] + timedelta(seconds=spec.interval_seconds)
                    if spec.interval_seconds
                    else None,
                },
            )
            return True

    async def _stop_agent(self, job: Mapping[str, Any], reason: str) -> None:
        async with self.engine.begin() as conn:
            await self._admit(conn)
            changed = await conn.execute(
                sql(
                    "UPDATE xiaowei_scheduled_job SET state=:state,reason=:reason,"
                    "notice_id=:notice,notice_reason=:reason"
                    ",notice_state='pending',checked_at=:now "
                    "WHERE job_id=:id AND state IN ('active','paused')"
                ),
                dict(
                    id=job["job_id"],
                    state="completed" if reason == "validity_expired" else "disabled",
                    reason=reason,
                    notice=uuid4().hex,
                    now=self.clock(),
                ),
            )
            if changed.rowcount:
                await conn.execute(
                    sql(
                        "UPDATE xiaowei_scheduled_run SET state='skipped',reason=:reason,"
                        "finished_at=:now WHERE job_id=:id AND state='queued'"
                    ),
                    dict(id=job["job_id"], reason=reason, now=self.clock()),
                )

    async def list_tasks(self, ctx: RunContext, request: ToolRequest) -> ToolObservation:
        args = TaskListArgs.model_validate(request.arguments)
        decision = await self.definitions.decision(ctx.identity, member=False)
        if decision is None:
            raise ToolRejectedError("当前群身份不可核验", code="not_allowed")
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sql(
                            "SELECT job_id FROM xiaowei_scheduled_job WHERE owner_id=:owner "
                            "AND (creator_id=:subject OR :admin) AND job_id>:after "
                            "ORDER BY job_id LIMIT :limit"
                        ),
                        dict(
                            owner=ctx.identity.owner.id,
                            subject=ctx.identity.subject_id,
                            admin=self.is_admin(ctx.identity.subject_id),
                            after=args.after_id or "",
                            limit=args.limit + 1,
                        ),
                    )
                )
                .scalars()
                .all()
            )
        items = []
        deadlines = []
        for job_id in rows[: args.limit]:
            job = await self.definitions.get(job_id)
            spec = (
                AgentSpec.model_validate_json(job["agent_spec"])
                if job["kind"] == "agent_task"
                else None
            )
            text = None
            if args.include_text:
                try:
                    await self.definitions.validate(job, ctx)
                    text = job["text"]
                    if (
                        json.loads(job["definition_evidence_ids"])
                        and job["content_expires_at"] is not None
                    ):
                        deadlines.append(job["content_expires_at"])
                except (EvidenceUnavailableError, EvidenceUnverifiableError, AnswerRejectedError):
                    pass  # 元数据管理可见不代表正文可读。
            deadline = self._agent_deadline(job) if spec else job["content_expires_at"]
            items.append(
                dict(
                    job_id=job_id,
                    kind=job["kind"],
                    interval_seconds=spec.interval_seconds if spec else None,
                    next_run=job["next_run"].isoformat() if job["next_run"] else None,
                    state=job["state"],
                    expires_at=deadline.isoformat() if deadline else None,
                    running_days=max(0, (self.clock() - job["approved_at"]).days)
                    if job["approved_at"]
                    else 0,
                    text=text,
                )
            )
        return ToolObservation(
            payload=dict(
                tasks=items, next_cursor=rows[args.limit - 1] if len(rows) > args.limit else None
            ),
            captured_at=self.clock(),
            truncated=len(rows) > args.limit,
            content_expires_at=min(deadlines) if deadlines else None,
        )

    async def _deliver_task_notices(self) -> None:
        if self.notice_sender is None:
            return
        async with self.engine.connect() as conn:
            jobs = (
                (
                    await conn.execute(
                        sql(
                            "SELECT * FROM xiaowei_scheduled_job WHERE notice_state='pending' "
                            "ORDER BY checked_at,job_id LIMIT :limit"
                        ),
                        {"limit": self.config.max_active_jobs},
                    )
                )
                .mappings()
                .all()
            )
        reasons = {
            "authorization_revoked": "创建者当前权限已撤销",
            "member_absent": "创建者已不在原操作群",
            "recipient_changed": "机器人接收目标已变化",
            "definition_expired": "定义依赖数据已到期",
            "definition_unavailable": "定义依赖当前不可用",
            "budget_unavailable": "当前预算不足以完成获准任务",
        }
        for job in jobs:
            with self.run_slot(None) as reserved:
                if not reserved:
                    return
                async with self.engine.begin() as conn:
                    await self._admit(conn)
                    claimed = await conn.execute(
                        sql(
                            "UPDATE xiaowei_scheduled_job SET notice_state='sending' "
                            "WHERE job_id=:id AND notice_id=:notice AND "
                            "notice_state='pending' AND state<>'cancelled'"
                        ),
                        dict(id=job["job_id"], notice=job["notice_id"]),
                    )
                if claimed.rowcount != 1:
                    continue
                if job["notice_reason"] == "validity_expired":
                    until = AgentSpec.model_validate_json(job["agent_spec"]).expires_at
                    at = (until or self.clock()).astimezone(ZoneInfo(self.config.timezone))
                    body = f"任务〈{job['job_id']}〉已于 {at.isoformat()} 到期停止。"
                else:
                    reason = reasons.get(job["notice_reason"], "当前授权条件不再满足")
                    body = f"任务〈{job['job_id']}〉已停止：{reason}。"
                try:
                    outcome = await self.notice_sender(job["owner_id"], job["notice_id"], body)
                    if outcome not in {"sent", "failed", "unknown"}:
                        outcome = "unknown"
                except Exception as exc:
                    logger.warning(
                        "任务停止说明结果未知：job=%s error=%s", job["job_id"], type(exc).__name__
                    )
                    outcome = "unknown"
                async with self.engine.begin() as conn:
                    await conn.execute(
                        sql(
                            "UPDATE xiaowei_scheduled_job SET notice_state=:state "
                            "WHERE job_id=:id AND notice_id=:notice AND notice_state='sending'"
                        ),
                        dict(state=outcome, id=job["job_id"], notice=job["notice_id"]),
                    )

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
            spec = (
                AgentSpec.model_validate_json(job["agent_spec"])
                if job.get("kind") == "agent_task"
                else None
            )
            if (spec is None or spec.interval_seconds is None) and self.clock() >= job["run_at"]:
                raise ActionWriteRejectedError(code="reminder_time_passed")
            if spec is not None:
                deadline = self._agent_deadline(job)
                if deadline is not None and self.clock() >= deadline:
                    raise ActionWriteRejectedError("任务有效期已过，未激活")
                if spec.interval_seconds is not None and spec.expires_at is None:
                    count = await conn.scalar(
                        sql(
                            "SELECT count(*) FROM xiaowei_scheduled_job WHERE owner_id=:owner "
                            "AND kind='agent_task' AND state IN ('active','paused') "
                            "AND agent_spec::jsonb ->> 'interval_seconds' IS NOT NULL "
                            "AND agent_spec::jsonb ->> 'expires_at' IS NULL"
                        ),
                        {"owner": job["owner_id"]},
                    )
                    if count >= self.config.max_long_jobs_per_group:
                        raise ActionWriteRejectedError("本群长期任务已达上限，未激活")
                if not spec.explicit_start:
                    job = {**job, "run_at": self.clock(), "next_run": self.clock()}
                await conn.execute(
                    sql(
                        "UPDATE xiaowei_scheduled_job SET run_at=:at,next_run=:at WHERE job_id=:id"
                    ),
                    {"id": job["job_id"], "at": job["run_at"]},
                )
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

    def binding(self, proposal_id: str = PROPOSE) -> ActionBinding:
        cat = self.governance.catalog
        proposal, write = cat.contract(proposal_id, TARGET), cat.contract(ACTIVATE, TARGET)
        assert proposal is not None and write is not None  # noqa: S101 - 可信装配已登记
        return ActionBinding(
            proposal,
            write,
            self.prepare,
            self.activate,
            target_binding="fixed-reminder/1" if proposal_id == PROPOSE else "scheduled-agent/1",
            available=lambda: (
                self.config.enabled
                and (proposal_id == PROPOSE or bool(self.config.automatic_tools))
            ),
            prepare_proposal=self.propose if proposal_id == PROPOSE else self.propose_agent,
            check_material=self.check_material,
        )

    async def check_material(self, ctx: RunContext, action: ActionRecord) -> None:
        job_id = json.loads(action.plan)["plan"]["parameters"]["job_id"]
        await self.definitions.validate(await self.definitions.get(job_id), ctx)

    def _observation(
        self, job: Mapping[str, Any], state: str | None = None, *, body: bool = True
    ) -> ToolObservation:
        reason = job.get("reason", "pending")
        if job.get("notice_state") is not None:
            reason += f"；原群停止说明={job['notice_state']}"
            if job["notice_state"] == "unknown":
                reason += "（不会自动重发）"
        if (
            job.get("notification_state") == "accepted"
            and job.get("notification_reason") == "proxy_http_success"
        ):
            reason += "；代理返回 HTTP 2xx 成功状态，群内投递未确认"
        deadline = (
            self._agent_deadline(job)
            if job.get("kind") == "agent_task"
            else job["content_expires_at"]
        )
        return ToolObservation(
            payload=dict(
                job_id=job["job_id"],
                state=state or job["state"],
                at=(job.get("next_run") or job["run_at"]).isoformat(),
                expires_at=deadline.isoformat() if deadline else "长期",
                reason=reason,
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
            if contract.tool_id == LIST_TASKS:
                tools.append(
                    governed_function_tool(
                        contract.tool_id.replace("/", "__"),
                        contract,
                        self.governance,
                        partial(self.list_tasks, ctx),
                    )
                )
                continue
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
                        # 与 claim 先锁同一 job；等待提交后，用新语句快照检查 attempt。
                        await conn.execute(
                            sql(
                                "SELECT job_id FROM xiaowei_scheduled_job "
                                "WHERE job_id=:id FOR UPDATE"
                            ),
                            {"id": job["job_id"]},
                        )
                        changed = await conn.execute(
                            sql(
                                "UPDATE xiaowei_scheduled_job j SET state=:state "
                                "WHERE job_id=:id AND state IN ('draft','active','paused') "
                                "AND NOT EXISTS (SELECT 1 FROM xiaowei_notification_attempt n "
                                "WHERE n.job_id=j.job_id OR (n.run_id IN "
                                "(SELECT run_id FROM xiaowei_scheduled_run WHERE job_id=j.job_id) "
                                "AND n.state='sending'))"
                            ),
                            {"id": job["job_id"], "state": state},
                        )
                        if changed.rowcount:
                            await conn.execute(
                                sql(
                                    "UPDATE xiaowei_scheduled_run SET "
                                    "state='skipped',reason='task_controlled',finished_at=:now "
                                    "WHERE job_id=:id AND state='queued'"
                                ),
                                {"id": job["job_id"], "now": self.clock()},
                            )
                    job = await self.definitions.get(job["job_id"])
                    if changed.rowcount == 0:
                        if job.get("kind") == "agent_task" and job["state"] not in {
                            "draft",
                            "active",
                            "paused",
                        }:
                            job["reason"] = "任务已停止"
                        elif job.get("receipt_id") is not None:
                            job["reason"] = "发送已准入，未暂停/取消"
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
                    "UPDATE xiaowei_scheduled_run SET state='unknown',reason='execution_unknown',"
                    "finished_at=:now WHERE state='running'"
                ),
                {"now": self.clock()},
            )
            await conn.execute(
                sql(
                    "UPDATE xiaowei_scheduled_job SET "
                    "notice_state='unknown' WHERE notice_state='sending'"
                )
            )
            await conn.execute(
                sql(
                    "UPDATE xiaowei_scheduled_job j SET "
                    "state='completed',reason='execution_unknown' "
                    "WHERE kind='agent_task' AND state='active' AND next_run IS NULL "
                    "AND EXISTS(SELECT 1 FROM xiaowei_scheduled_run "
                    "r WHERE r.job_id=j.job_id AND r.state='unknown')"
                )
            )
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
            if self.clock() >= job["run_at"] + timedelta(seconds=job["lateness_seconds"]):
                await conn.execute(
                    sql(
                        "UPDATE xiaowei_scheduled_job SET state='missed', "
                        "reason='lateness_exceeded' WHERE job_id=:id"
                    ),
                    {"id": job["job_id"]},
                )
                return "none", "not_sent", "lateness_exceeded", False
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
            receipt = uuid4().hex
            state, reason = "sending", "pending"
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
                    "state": "active",
                },
            )
            return receipt, state, reason, True

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
                                "WHERE (j.state='active' OR "
                                "(j.kind='agent_task' AND j.state='paused' "
                                "AND LEAST(j.content_expires_at,"
                                "(j.agent_spec::jsonb->>'expires_at')::timestamptz)<=:now)) AND "
                                "(j.kind='fixed_message' OR j.next_run IS NOT NULL) "
                                "AND (j.kind='agent_task' OR j.run_at<=:now) AND NOT EXISTS "
                                "(SELECT 1 FROM xiaowei_notification_attempt n "
                                "WHERE n.job_id=j.job_id) "
                                "ORDER BY CASE WHEN j.state='active' AND j.next_run<=:now THEN 0 "
                                "WHEN j.state='paused' THEN 1 ELSE 2 END,run_at,job_id LIMIT :limit"
                            ),
                            {"now": self.clock(), "limit": self.config.max_active_jobs},
                        )
                    )
                    .mappings()
                    .all()
                )

            async def process(job: Mapping[str, Any]) -> None:
                if not self.lock.readiness.ok:
                    return
                if job.get("kind") != "agent_task" and self.clock() >= job["run_at"] + timedelta(
                    seconds=job["lateness_seconds"]
                ):
                    await self._mark(job, "missed", "lateness_exceeded")
                    return
                if job.get("kind") == "agent_task":
                    await self._run_agent(job)
                    return
                with self.run_slot(None) as reserved:
                    if not reserved:
                        await self._mark(job, "active", "busy")
                        return
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

            pending = []
            for row in jobs:
                job_id = row["job_id"]
                if job_id in self._running_jobs:
                    continue
                task = asyncio.create_task(process(dict(row)), name=job_id)
                self._running_jobs[job_id] = task

                def release(done: asyncio.Task[None], key: str = job_id) -> None:
                    self._running_jobs.pop(key, None)
                    if not self.lock.readiness.ok:
                        for other in tuple(self._running_jobs.values()):
                            other.cancel()

                task.add_done_callback(release)
                pending.append(task)
        try:
            outcomes = await asyncio.gather(*pending, return_exceptions=True)
            for task, outcome in zip(pending, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    logger.error(
                        "定时任务失败：job=%s code=task_failed error=%s",
                        task.get_name(),
                        type(outcome).__name__,
                    )
            if not self.lock.readiness.ok:
                failure = next((e for e in outcomes if isinstance(e, Exception)), None)
                if failure is not None:
                    raise failure
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if self.lock.readiness.ok:
            await self._deliver_task_notices()

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
            outcome = await self.notifications.send_fixed(
                sending,
                job["bot_alias"],
                job["text"],
                partial(self._claim, job),
                partial(self._settle, job["job_id"]),
            )
            if outcome.reason == "sender_not_authorized":
                await self._mark(job, "active", "authorization_unverified", unverified=True)
            elif outcome.reason == "content_too_large":
                await self._mark(job, "disabled", "content_too_large")
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
        scans: set[asyncio.Task[None]] = set()

        def completed(task: asyncio.Task[None]) -> None:
            scans.discard(task)
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error("定时检查失败：error=%s", type(exc).__name__)

        try:
            while True:
                if not self._tick_lock.locked():
                    scan = asyncio.create_task(self.tick())
                    scans.add(scan)
                    scan.add_done_callback(completed)
                await asyncio.sleep(self.config.poll_seconds)
        finally:
            pending = tuple(scans)
            for scan in pending:
                scan.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
