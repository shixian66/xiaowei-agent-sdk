"""P4 选中监控动作：受治理提出、群批准、一次执行、只读状态。

Runner 只接触提出和状态工具。绑定由对应写切片的可信代码装配；没有通用 API、
工作流、自动恢复或模型提供的 Action。执行事实在 ChannelStore 的应用表内。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID, uuid4

from agents import FunctionTool
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from xiaowei.channel_store import ActionRecord, ChannelStore, RequestUnavailableError
from xiaowei.evidence import (
    AnswerRejectedError,
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
    normalize_arguments,
)
from xiaowei.models import (
    AUDIENCES,
    AgentAnswer,
    ApprovedAction,
    Audience,
    Identity,
    RunContext,
    ToolCall,
    ToolContract,
    ToolObservation,
    ToolRequest,
    TurnAnswer,
)
from xiaowei.tools import governed_function_tool

if TYPE_CHECKING:
    from xiaowei.channel import AccessPolicy

APPROVAL_WINDOW = timedelta(minutes=15)
STATUS_POLICY = "monitoring.action_status"
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


@dataclass(frozen=True)
class ActionBinding:
    """一个已选动作的装配，不是插件注册表；prepare 仅回读，execute 经现有 Adapter/MCP。"""

    proposal: ToolContract
    write: ToolContract
    prepare: Callable[[ToolRequest], Awaitable[ActionPlan]]
    execute: Execute
    target_binding: str
    """可信装配产生的固定源配置/执行语义版本摘要；不含凭据，改变即使旧批准失效。"""


class ActionStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action_id: Annotated[str, AfterValidator(lambda value: str(UUID(value)))]


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
    return contracts, (policy,) if contracts else ()


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
        return frozenset(b.proposal.tool_id for b in self._bindings.values()) | frozenset(
            c.tool_id for c in self.governance.catalog.contracts if c.policy_id == STATUS_POLICY
        )

    def tools_for(self, ctx: RunContext) -> list[FunctionTool]:
        tools = []
        for contract in self.governance.allowed_contracts(ctx):
            binding = self._bindings.get((contract.tool_id, contract.target_id))
            if binding is not None:

                async def propose(
                    req: ToolRequest,
                    b: ActionBinding | None = binding,
                ) -> ToolObservation:
                    if b is None:
                        raise ToolRejectedError("动作不可用")
                    return await self._propose(ctx, req, b)

                execute: Execute = propose
            elif contract.policy_id == STATUS_POLICY:

                async def status(req: ToolRequest) -> ToolObservation:
                    return await self.status(ctx, req)

                execute = status
            else:
                continue
            tools.append(
                governed_function_tool(
                    contract.tool_id.replace("/", "__"),
                    contract,
                    self.governance,
                    execute,
                )
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
    ) -> ToolObservation:
        # 调用来自 GovernedTools；只有当前群有提出权才会回读。
        record = await self._store.proposal_request_for_turn(ctx.identity)
        plan = await binding.prepare(request)
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
                {"proposal_arguments": request.arguments, "plan": plan.model_dump()},
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
            approval_expires_at=min(now + APPROVAL_WINDOW, record.expires_at),
            expires_at=record.expires_at,
        )
        observation = ToolObservation(
            payload=self._material(action), captured_at=now, truncated=False
        )
        check_projection_capacity(self.governance.catalog.policy_for(binding.proposal), observation)
        await self._store.propose_action(action)
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
        )

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

    async def approve(self, action_id: str, ctx: RunContext) -> TurnAnswer:
        action = await self._store.get_action(str(UUID(action_id)), ctx.identity)
        b = self._bindings.get((action.proposal_tool_id, action.target_id))
        status_tool = f"{action.target_id}/get_action_status"
        if (
            b is None
            or action.state != "pending"
            or self._clock() >= action.approval_expires_at
            or action.binding != self._binding_hash(b)
            or action.write_tool_id != b.write.tool_id
            or not await self._access.authorize(ctx.identity, action.target_id, b.proposal.tool_id)
            or not await self._access.authorize(ctx.identity, action.target_id, status_tool)
            or not await self._current_approver(ctx.identity, action.target_id, b.write.tool_id)
        ):
            raise ToolRejectedError("动作不存在、已失效或当前无权批准，未执行")
        original = await self._store.proposal_request(action)
        if original.state != "completed" or original.delivery != "sent" or original.answer is None:
            raise ToolRejectedError("动作材料尚未有效送达，未执行")
        evidence = self.governance.evidence
        origin_ctx = ctx.model_copy(
            update={
                "identity": Identity(
                    subject_id=ctx.identity.subject_id,
                    session_id=action.proposal_session_id,
                    turn_id=action.proposal_turn_id,
                    channel="feishu",
                    owner=ctx.identity.owner,
                )
            }
        )
        if original.context_evidence is None:
            raise ToolRejectedError("动作材料缺少有效证据，未执行")
        try:
            await evidence.validate_answer(
                TurnAnswer(
                    answer=original.answer,
                    context_evidence=original.context_evidence,
                    monitoring_failures=original.monitoring_failures,
                ),
                origin_ctx,
            )
            material = self._material(action)
            proposal_call = ToolCall(
                call_id=action.call_id,
                tool_name=b.proposal.tool_id.replace("/", "__"),
                arguments=json.loads(action.plan)["proposal_arguments"],
            )
            matched = False
            for evidence_id in original.answer.evidence_ids:
                content = json.loads(await evidence.project(evidence_id, origin_ctx, "feishu"))
                if content["data"] == material and not content["truncated"]:
                    await evidence.project(evidence_id, origin_ctx, "feishu", call=proposal_call)
                    matched = True
            if not matched:
                raise ToolRejectedError("动作材料缺少有效证据，未执行")
        except (AnswerRejectedError, EvidenceUnavailableError):
            raise ToolRejectedError("动作材料缺少有效证据，未执行") from None
        stored = json.loads(action.plan)
        approved_plan = ActionPlan.model_validate(stored["plan"])
        claimed = await self._store.claim_action(action, ctx.identity)
        if claimed is None:
            raise ToolRejectedError("动作已过期或已被处理，未执行")
        if claimed.attempt is None:
            raise RequestUnavailableError
        approved = ctx.model_copy(
            update={
                "tool_scope": frozenset({b.write.tool_id}),
                "approved_action": ApprovedAction(
                    action_id=claimed.action_id,
                    attempt=claimed.attempt,
                    tool_id=b.write.tool_id,
                    target_id=b.write.target_id,
                    arguments_digest=arguments_digest(approved_plan.parameters),
                ),
            }
        )
        write_request = ToolRequest(
            tool_id=b.write.tool_id,
            target_id=b.write.target_id,
            call_id=claimed.attempt,
            tool_name=b.write.tool_id.replace("/", "__"),
            arguments=approved_plan.parameters,
        )
        finished = False
        write_started = False
        prepared: Any = None

        def precheck(req: ToolRequest) -> ToolRequest:
            nonlocal prepared
            if isinstance(b.execute, Prechecked):
                prepared = b.execute.check(req)
            return req

        async def execute(req: ToolRequest) -> ToolObservation:
            nonlocal finished, write_started
            if not await self._current_approver(ctx.identity, b.write.target_id, b.write.tool_id):
                raise ToolRejectedError("当前批准已失效，未执行")
            # 复核使用已批准的实际参数，不能重新计算相对时间或默认值。
            current = await b.prepare(req)
            current = current.model_copy(
                update={
                    "parameters": normalize_arguments(
                        self.governance.catalog.policy_for(b.write),
                        current.parameters,
                    )
                }
            )
            if current != approved_plan:
                raise ToolRejectedError("对象、参数或差异已变化，请重新提出动作；未执行")
            if not await self._current_approver(ctx.identity, b.write.target_id, b.write.tool_id):
                raise ToolRejectedError("当前批准已失效，未执行")
            await self._store.admit_action(claimed)
            write_started = True
            observation = await (
                b.execute.run(prepared) if isinstance(b.execute, Prechecked) else b.execute(req)
            )
            projected = action_result_projection(
                self.governance.catalog.policy_for(b.write), observation
            )
            await self._store.finish_action(claimed, "succeeded", projected)
            finished = True
            return observation

        try:
            await self.governance.invoke(approved, write_request, Prechecked(precheck, execute))
        except asyncio.CancelledError:
            self.governance.end_turn(approved.identity)
            self._store.readiness.lock("action_cancelled")
            raise
        except Exception:
            try:
                if not finished:
                    await self._store.finish_action(
                        claimed,
                        "unknown" if write_started else "rejected",
                    )
            finally:
                self.governance.end_turn(approved.identity)
            raise ToolExecutionError() from None
        # 状态反馈也是当前群/源权限下的 Evidence，不在 Runner 外提交 Session。
        request = ToolRequest(
            tool_id=status_tool,
            target_id=action.target_id,
            call_id=f"status-{action.action_id}",
            tool_name=status_tool.replace("/", "__"),
            arguments={"action_id": action.action_id},
        )
        status_ctx = ctx.model_copy(update={"tool_scope": frozenset({status_tool})})
        try:
            result = await self.governance.invoke(
                status_ctx, request, lambda req: self.status(status_ctx, req)
            )
            return TurnAnswer(
                answer=AgentAnswer(
                    evidence_ids=(result.evidence_id,),
                    inferences=[],
                    clarification=None,
                ),
                context_evidence=(result.evidence_id,),
            )
        finally:
            self.governance.end_turn(status_ctx.identity)
