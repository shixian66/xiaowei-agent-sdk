"""P4 选中监控动作：受治理提出、群批准、一次执行、只读状态。

Runner 只接触提出、状态和SDK执行请求工具，暂停与恢复由SDK管理。
绑定由对应写切片装配；不从模型文字生成Action，不自动批准或重放写入。
执行事实在ChannelStore应用表内。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID, uuid4

from agents import FunctionTool, RunContextWrapper, UserError
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

from xiaowei.channel_store import (
    ActionAdmissionRejectedError,
    ActionRecord,
    ChannelStore,
    RequestUnavailableError,
)
from xiaowei.evidence import (
    EvidenceUnavailableError,
    action_result_projection,
    arguments_digest,
    check_projection_capacity,
)
from xiaowei.governance import (
    Execute,
    GovernedTools,
    Prechecked,
    Projection,
    ToolExecutionError,
    ToolPolicy,
    ToolRejectedError,
    TurnRuns,
    normalize_arguments,
)
from xiaowei.models import (
    ACTION_STATUS_POLICY,
    AUDIENCES,
    Audience,
    Identity,
    RunContext,
    ToolContract,
    ToolObservation,
    ToolRequest,
)
from xiaowei.tools import governed_function_tool

if TYPE_CHECKING:
    from xiaowei.channel import AccessPolicy

APPROVAL_WINDOW = timedelta(minutes=15)
ACTION_SETTLE_TIMEOUT_SECONDS = 5
STATUS_POLICY = ACTION_STATUS_POLICY
_PROPOSAL_FIELDS = (
    "action_id",
    "action",
    "parameters",
    "revision",
    "change",
    "impact",
    "rollback",
    "approve_before",
    "approval_command",
)


class ActionPlan(BaseModel):
    """具体写切片生成的完整材料；参数和版本回读由代码决定。禁止凭据进入此对象。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    parameters: dict[str, Any]
    revision: str = Field(min_length=1, max_length=256)
    change: str = Field(min_length=1, max_length=32_000)
    impact: str = Field(min_length=1, max_length=4000)
    rollback: str = Field(min_length=1, max_length=32_000)
    content_expires_at: AwareDatetime | None = Field(default=None, strict=False)


@dataclass(frozen=True)
class ActionBinding:
    """一个已选动作的装配，不是插件注册表；prepare 仅回读，execute 经现有 Adapter/MCP。"""

    proposal: ToolContract
    write: ToolContract
    prepare: Callable[[ToolRequest], Awaitable[ActionPlan]]
    execute: Execute
    target_binding: str
    """可信装配产生的固定源配置/执行语义版本摘要；不含凭据，改变即使旧批准失效。"""
    precheck: Callable[[ToolRequest], ToolRequest] | None = None
    """动作切片的零 I/O 提出检查；复用 Prechecked，拒绝可在本轮修正。"""
    available: Callable[[], bool] | None = None
    """选中MCP写切片的当前核约可用性；不探测、不授予权限。"""
    prepare_proposal: (
        Callable[[RunContext, ToolRequest, Sequence[str]], Awaitable[ActionPlan]] | None
    ) = None
    """本地任务草拟入口：由应用冻结模型实际可见的证据，不让模型选择依赖。"""
    check_material: Callable[[RunContext, ActionRecord], Awaitable[None]] | None = None


class ActionStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action_id: Annotated[str, AfterValidator(lambda value: str(UUID(value)))]


class ActionWriteRejectedError(Exception):
    """可信 Adapter 在已核验/批准的上游分支确认未写入，不代表一般4xx可重试。

    仅由当前批准执行捕获为 rejected；不进入 Runner，也不放宽治理的异常处理。
    """


class ActionOutcomeError(ToolExecutionError):
    """占用后已持久化的动作终态；渠道只据此给固定回执，不传递原始异常。"""

    def __init__(self, state: Literal["succeeded", "unknown", "rejected"]) -> None:
        super().__init__()
        self.state = state


def action_status_catalog(
    source_ids: Iterable[str],
    max_bytes: Mapping[Audience, int] | int,
) -> tuple[tuple[ToolContract, ...], tuple[ToolPolicy, ...]]:
    fields = ("action_id", "state", "approver", "result", *_PROPOSAL_FIELDS[1:])
    policy = ToolPolicy(
        STATUS_POLICY,
        ActionStatusArgs,
        {
            a: Projection(fields, max_bytes if isinstance(max_bytes, int) else max_bytes[a])
            for a in AUDIENCES
        },
        required=("action_id", "state"),
        group_only=True,
        fact_note=(
            "状态来自应用动作记录，非远端实时状态；pending 不代表材料已送达可批准；"
            "unknown 表示执行结果未知，不自动重试。"
        ),
    )
    contracts = tuple(
        ToolContract(
            tool_id=f"{sid}/get_action_status",
            target_id=sid,
            policy_id=STATUS_POLICY,
            input_schema=ActionStatusArgs.model_json_schema(),
            description=(
                "按已知 Action ID 查看本群在此源的动作、批准期限与执行结果；不是远端实时状态。"
            ),
        )
        for sid in source_ids
    )
    gates = tuple(
        ToolContract(
            tool_id=f"{c.target_id}/request_action_execution",
            target_id=c.target_id,
            policy_id="action.execution-request/1",
            input_schema=ActionStatusArgs.model_json_schema(),
            description=(
                "请求执行本轮已提出的Action ID，SDK会暂停等待群人工批准；不能执行原始写参数。"
            ),
        )
        for c in contracts
    )
    outcome_fields = ("action_id", "state", "approver", "result")
    gate_policy = replace(
        policy,
        policy_id=EXECUTION_POLICY,
        effect="approval",
        projections={
            a: replace(spec, fields=outcome_fields) for a, spec in policy.projections.items()
        },
        required=outcome_fields,
    )

    return (*contracts, *gates), (policy, gate_policy) if contracts else ()


def proposal_policy(
    policy_id: str,
    arguments: type[BaseModel],
    max_bytes: int,
) -> ToolPolicy:
    """对应写切片复用的提出投影；批准所需字段不允许静默省略。"""
    return ToolPolicy(
        policy_id,
        arguments,
        {a: Projection(_PROPOSAL_FIELDS, max_bytes) for a in AUDIENCES},
        required=_PROPOSAL_FIELDS,
        effect="propose",
        group_only=True,
    )


EXECUTION_POLICY = "action.execution-request/1"
EXECUTION_SUFFIX = "/request_action_execution"


def execution_requests(
    tools: Iterable[str], sources: Iterable[str] | None = None
) -> frozenset[str]:
    """执行请求继承同源状态读取许可；实际写仍只按本次审批人Grant。"""
    tools = frozenset(tools)
    enabled = None if sources is None else frozenset(sources)
    return tools | frozenset(
        t.removesuffix("/get_action_status") + EXECUTION_SUFFIX
        for t in tools
        if t.endswith("/get_action_status")
        and (enabled is None or t.removesuffix("/get_action_status") in enabled)
    )


class ActionService:
    def __init__(
        self,
        governance: GovernedTools,
        store: ChannelStore,
        access: AccessPolicy,
        bindings: Iterable[ActionBinding] = (),
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if getattr(governance.evidence.authorize, "__self__", None) is not access:
            raise ValueError("Action 必须复用 Evidence 的授权来源")
        self.governance = governance
        self._store = store
        self._access = access
        self._clock = clock
        self._bindings = {(b.proposal.tool_id, b.proposal.target_id): b for b in bindings}
        self._executions: dict[str, asyncio.Task[Any]] = {}
        for b in self._bindings.values():
            catalog = governance.catalog
            if (
                b.proposal.target_id != b.write.target_id
                or catalog.contract(b.proposal.tool_id, b.proposal.target_id) != b.proposal
                or catalog.contract(b.write.tool_id, b.write.target_id) != b.write
                or catalog.policy_for(b.proposal).effect != "propose"
                or catalog.policy_for(b.write).effect != "write"
            ):
                raise ValueError("Action 工具绑定不合约")

    @property
    def available_tool_ids(self) -> frozenset[str]:
        bound_sources = {
            b.proposal.target_id
            for b in self._bindings.values()
            if b.available is None or b.available()
        }
        return frozenset(
            b.proposal.tool_id
            for b in self._bindings.values()
            if b.available is None or b.available()
        ) | frozenset(
            c.tool_id
            for c in self.governance.catalog.contracts
            if c.policy_id == STATUS_POLICY
            or (c.policy_id == EXECUTION_POLICY and c.target_id in bound_sources)
        )

    def tools_for(
        self,
        ctx: RunContext,
        approval_requests: dict[str, int] | None = None,
        *,
        replayed: Callable[[], Sequence[str]] | None = None,
    ) -> list[FunctionTool]:
        tools = []
        requested = approval_requests if approval_requests is not None else {}
        for contract in self.governance.allowed_contracts(ctx):
            binding = self._bindings.get((contract.tool_id, contract.target_id))
            if binding is not None:
                if binding.available is not None and not binding.available():
                    continue

                async def propose(
                    req: ToolRequest, b: ActionBinding | None = binding
                ) -> ToolObservation:
                    if b is None:
                        raise ToolRejectedError("动作不可用")
                    visible = tuple(
                        dict.fromkeys(
                            (
                                *(ctx.evidence_ids if replayed is None else replayed()),
                                *(
                                    e
                                    for t, e in self.governance.turn_runs(ctx.identity).produced
                                    if t in ctx.tool_scope
                                ),
                            )
                        )
                    )
                    return await self._propose(ctx, req, b, visible)

                execute: Execute = (
                    Prechecked(binding.precheck, propose) if binding.precheck else propose
                )
            elif contract.policy_id == EXECUTION_POLICY:
                if not any(
                    b.proposal.target_id == contract.target_id for b in self._bindings.values()
                ):
                    continue

                async def execute_action(req: ToolRequest) -> ToolObservation:
                    approval = ctx.approved_action
                    task = asyncio.current_task()
                    if approval is None or task is None:
                        return await self.approve(str(req.arguments["action_id"]), ctx)
                    self._executions[approval.attempt] = task
                    try:
                        return await self.approve(str(req.arguments["action_id"]), ctx)
                    finally:
                        self._executions.pop(approval.attempt, None)

                execute = execute_action

                async def needs_approval(
                    tool_ctx: RunContextWrapper[RunContext],
                    params: dict[str, Any],
                    _call_id: str,
                    c: ToolContract = contract,
                ) -> bool:
                    # SDK 的 False 会继续调用工具；未获批准的执行请求由 GovernedTools
                    # 在预算占用和 Adapter I/O 前固定拒绝，不能由此触发写入。
                    active = tool_ctx.context
                    try:
                        action_id = ActionStatusArgs.model_validate(params).action_id
                        action = await self._store.get_action(action_id, active.identity)
                        if (
                            action.state != "pending"
                            or action.requester_id != active.identity.subject_id
                            or action.proposal_session_id != active.identity.session_id
                            or action.proposal_turn_id != active.identity.turn_id
                            or action.target_id != c.target_id
                            or self._clock() >= action.approval_expires_at
                        ):
                            return False
                        await self.verify_material(
                            action, active, self.governance.turn_runs(active.identity)
                        )
                        used = self.governance.checkpoint(active.identity).used
                    except (ValueError, RequestUnavailableError, ToolRejectedError):
                        return False
                    except Exception as exc:
                        # 锁版 SDK 把普通回调异常当作“需要批准”；UserError 会直接
                        # 结束本轮，避免暂时性检查故障产生看似可批准的材料。
                        raise UserError("执行请求预检失败") from exc
                    count = requested.get(action_id, 0) + 1
                    requested[action_id] = count
                    return count == 1 and used + 2 * len(requested) <= active.budget.max_tool_calls
            elif contract.policy_id == STATUS_POLICY:

                async def status(req: ToolRequest) -> ToolObservation:
                    return await self.status(ctx, req)

                execute = status
            else:
                continue
            tool = governed_function_tool(
                contract.tool_id.replace("/", "__"), contract, self.governance, execute
            )
            tools.append(
                replace(tool, needs_approval=needs_approval)
                if contract.policy_id == EXECUTION_POLICY
                else tool
            )
        return tools

    def _binding_hash(self, b: ActionBinding) -> str:
        return arguments_digest(
            {
                "target": b.target_binding,
                "proposal": self.governance.evidence.fingerprint(b.proposal),
                "write": self.governance.evidence.fingerprint(b.write),
            }
        )

    async def _propose(
        self,
        ctx: RunContext,
        request: ToolRequest,
        binding: ActionBinding,
        visible: Sequence[str] = (),
    ) -> ToolObservation:
        # 调用来自 GovernedTools；只有当前群有提出权才会回读。
        record = await self._store.proposal_request_for_turn(
            ctx.identity, approval=ctx.approved_action
        )
        plan = await (
            binding.prepare_proposal(ctx, request, visible)
            if binding.prepare_proposal is not None
            else binding.prepare(request)
        )
        normalized = normalize_arguments(
            self.governance.catalog.policy_for(binding.write), plan.parameters
        )
        plan = plan.model_copy(update={"parameters": normalized})
        now = self._clock()
        action = ActionRecord(
            action_id=str(uuid4()),
            owner_id=ctx.identity.owner.id,
            requester_id=ctx.identity.subject_id,
            proposal_session_id=ctx.identity.session_id,
            proposal_turn_id=ctx.identity.turn_id,
            call_id=request.call_id,
            proposal_tool_id=binding.proposal.tool_id,
            write_tool_id=binding.write.tool_id,
            target_id=binding.write.target_id,
            binding=self._binding_hash(binding),
            plan=json.dumps(
                {"proposal_arguments": request.arguments, "plan": plan.model_dump(mode="json")},
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            ),
            state="pending",
            attempt=None,
            approver_id=None,
            approval_turn_id=None,
            result=None,
            created_at=now,
            updated_at=now,
            approval_expires_at=min(
                now + APPROVAL_WINDOW,
                record.expires_at,
                plan.content_expires_at or record.expires_at,
            ),
            expires_at=min(record.expires_at, plan.content_expires_at or record.expires_at),
        )
        observation = ToolObservation(
            payload=self._material(action),
            captured_at=now,
            truncated=False,
            content_expires_at=plan.content_expires_at,
        )
        check_projection_capacity(self.governance.catalog.policy_for(binding.proposal), observation)
        await self._store.propose_action(action, approval=ctx.approved_action)
        return observation

    @staticmethod
    def _material(action: ActionRecord) -> dict[str, Any]:
        plan = ActionPlan.model_validate(json.loads(action.plan)["plan"])
        return {
            "action_id": action.action_id,
            "action": action.write_tool_id,
            "parameters": json.dumps(plan.parameters, sort_keys=True, ensure_ascii=False),
            "revision": plan.revision,
            "change": plan.change,
            "impact": plan.impact,
            "rollback": plan.rollback,
            "approve_before": action.approval_expires_at.isoformat(),
            "approval_command": f"@小维 /批准 {action.action_id}",
        }

    async def status(self, ctx: RunContext, request: ToolRequest) -> ToolObservation:
        action_id = str(request.arguments["action_id"])
        try:
            action = await self._store.get_action(action_id, ctx.identity)
        except RequestUnavailableError:
            action = None
        if action is None or action.target_id != request.target_id:
            return ToolObservation(
                payload={"action_id": action_id, "state": "unavailable"},
                captured_at=self._clock(),
                truncated=False,
            )
        state = action.state
        binding = self._bindings.get((action.proposal_tool_id, action.target_id))
        if binding is not None and binding.check_material is not None:
            await binding.check_material(ctx, action)
        if state == "pending" and self._clock() >= action.approval_expires_at:
            state = "expired"
        return ToolObservation(
            payload={
                **self._material(action),
                "state": state,
                "approver": (
                    f"群成员 {hashlib.sha256(action.approver_id.encode()).hexdigest()[:8]}"
                    if action.approver_id
                    else "尚未批准"
                ),
                "result": action.result or "尚无执行结果",
            },
            captured_at=self._clock(),
            truncated=False,
            content_expires_at=ActionPlan.model_validate(
                json.loads(action.plan)["plan"]
            ).content_expires_at,
        )

    async def verify_material(self, action: ActionRecord, ctx: RunContext, runs: TurnRuns) -> str:
        """保存/恢复都从同一提案Evidence核对确定材料，Action表变化不能借旧批准执行。"""
        from xiaowei.models import ToolCall

        call = ToolCall(
            call_id=action.call_id,
            tool_name=action.proposal_tool_id.replace("/", "__"),
            arguments=json.loads(action.plan)["proposal_arguments"],
        )
        for tool, evidence_id in runs.produced:
            if tool != action.proposal_tool_id:
                continue
            try:
                content = await self.governance.evidence.project(
                    evidence_id, ctx, "feishu", call=call
                )
            except EvidenceUnavailableError:
                continue
            if json.loads(content)["data"] == self._material(action):
                return evidence_id
        raise ToolRejectedError("提案Evidence与当前动作不符，未执行，请重新提出")

    async def _current_approver(self, identity: Identity, target: str, tool: str) -> bool:
        # 群成员验证在外部读期间可能已过时：写 I/O 前再按现有目录/Grants 核对。
        from xiaowei.channel import GroupScope

        try:
            app, tenant, chat = json.loads(identity.owner.id)
            decision = await self._access.resolve_group(
                GroupScope(app_id=app, tenant_key=tenant, chat_id=chat),
                identity.subject_id,
                verify_member=True,
            )
            return decision is not None and await self._access.authorize(identity, target, tool)
        except Exception:
            return False

    async def authorize_approval(self, action: ActionRecord, ctx: RunContext) -> ActionBinding:
        b = self._bindings.get((action.proposal_tool_id, action.target_id))
        if (
            b is None
            or (b.available is not None and not b.available())
            or action.write_tool_id != b.write.tool_id
            or action.binding != self._binding_hash(b)
            or not await self._access.authorize(ctx.identity, action.target_id, b.proposal.tool_id)
            or not await self._access.authorize(
                ctx.identity, action.target_id, f"{action.target_id}/get_action_status"
            )
            or not await self._current_approver(ctx.identity, action.target_id, b.write.tool_id)
        ):
            raise ToolRejectedError("当前无权批准或动作契约已变化，未执行")
        return b

    async def wait_for_execution(self, attempt: str) -> None:
        """Runner取消不等待工具清理；恢复层在已有落定时限内等待同一SDK执行任务。"""
        task = self._executions.get(attempt)
        if task is not None:
            await asyncio.wait({task})

    async def approve(self, action_id: str, ctx: RunContext) -> ToolObservation:
        """只由已恢复SDK gate调用；没有本次绑定时不保留旧独立执行后备。"""
        approval = ctx.approved_action
        if approval is None or approval.approver is None or approval.sdk_call_id is None:
            raise ToolRejectedError("缺少可恢复的本次SDK批准，未执行")
        approver = approval.approver
        claimed = await self._store.get_action(str(UUID(action_id)), ctx.identity)
        if (
            claimed.state != "executing"
            or claimed.attempt != approval.attempt
            or claimed.action_id != approval.action_id
            or claimed.requester_id != ctx.identity.subject_id
            or claimed.proposal_turn_id != ctx.identity.turn_id
            or claimed.proposal_session_id != ctx.identity.session_id
            or claimed.approver_id != approval.approver.subject_id
            or claimed.approval_turn_id != approval.approver.turn_id
        ):
            raise ToolRejectedError("批准与原轮绑定不符，未执行")
        b = self._bindings.get((claimed.proposal_tool_id, claimed.target_id))
        if b is None:
            raise ToolRejectedError("动作契约已变化，未执行")
        approved_plan = ActionPlan.model_validate(json.loads(claimed.plan)["plan"])
        approved = ctx.model_copy(update={"tool_scope": frozenset({b.write.tool_id})})
        write_request = ToolRequest(
            tool_id=b.write.tool_id,
            target_id=b.write.target_id,
            call_id=claimed.attempt,
            tool_name=b.write.tool_id.replace("/", "__"),
            arguments=approved_plan.parameters,
        )
        finished = False
        write_started = False
        write_rejected = False
        reason = "action_rejected"
        prepared: Any = None

        def precheck(req: ToolRequest) -> ToolRequest:
            nonlocal prepared
            if isinstance(b.execute, Prechecked):
                prepared = b.execute.check(req)
            return req

        async def execute(req: ToolRequest) -> ToolObservation:
            nonlocal finished, write_started, write_rejected, reason
            if not await self._current_approver(approver, b.write.target_id, b.write.tool_id):
                raise ToolRejectedError("本次批准权限已失效，未执行")
            current = await b.prepare(req)
            current = current.model_copy(
                update={
                    "parameters": normalize_arguments(
                        self.governance.catalog.policy_for(b.write),
                        current.parameters,
                    )
                }
            )
            if current != approved_plan or claimed.binding != self._binding_hash(b):
                raise ToolRejectedError("对象、参数或版本已变化，未执行，请重新提出")
            if not await self._current_approver(approver, b.write.target_id, b.write.tool_id):
                raise ToolRejectedError("本次批准已失效，未执行")
            # 读取身份保持原发起人；最后写准入也不借审批人读取旧证据。
            if not await self._current_requester(ctx):
                reason = "action_requester_invalid"
                raise ToolRejectedError("原发起人已失效，未执行")
            try:
                await self._store.admit_action(claimed)
            except ActionAdmissionRejectedError as exc:
                reason = exc.code
                raise
            write_started = True
            try:
                observation = await (
                    b.execute.run(prepared) if isinstance(b.execute, Prechecked) else b.execute(req)
                )
            except ActionWriteRejectedError:
                write_rejected = True
                raise
            projected = action_result_projection(
                self.governance.catalog.policy_for(b.write), observation
            )
            await self._store.finish_action(claimed, "succeeded", projected)
            finished = True
            return observation

        try:
            await self.governance.invoke(approved, write_request, Prechecked(precheck, execute))
        except asyncio.CancelledError:
            try:
                if not finished and self._store.readiness.ok:
                    async with asyncio.timeout(ACTION_SETTLE_TIMEOUT_SECONDS):
                        await self._store.finish_action(
                            claimed,
                            "unknown" if write_started and not write_rejected else "rejected",
                            failure_code=reason,
                        )
            except BaseException:
                self._store.readiness.lock("action_settle_failed")
                raise
            raise
        except Exception:
            state: Literal["succeeded", "unknown", "rejected"] = (
                "succeeded"
                if finished
                else "rejected"
                if write_rejected or not write_started
                else "unknown"
            )
            if not finished:
                await self._store.finish_action(claimed, state, failure_code=reason)
            raise ActionOutcomeError(state) from None
        return await self.status(
            ctx,
            ToolRequest(
                tool_id=f"{claimed.target_id}/get_action_status",
                target_id=claimed.target_id,
                call_id=approval.sdk_call_id,
                tool_name="action-fact",
                arguments={"action_id": action_id},
            ),
        )

    async def _current_requester(self, ctx: RunContext) -> bool:
        from xiaowei.channel import GroupScope

        app, tenant, chat = json.loads(ctx.identity.owner.id)
        decision = await self._access.resolve_group(
            GroupScope(app_id=app, tenant_key=tenant, chat_id=chat),
            ctx.identity.subject_id,
            verify_member=True,
        )
        if decision is None:
            return False
        from xiaowei.evidence import EvidencePermissionError

        try:
            await self.governance.evidence.project_many(
                [(e, None) for e in ctx.evidence_ids], ctx, "model"
            )
        except EvidencePermissionError:
            return False
        return True
