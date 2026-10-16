"""StepAdmission 的共享夹具。

合成写能力 ``test.synthetic.write`` **只存在于测试夹具**：它不在生产 registry 里
（``test_synthetic_write_capability_is_not_registered`` 承重），也不注册任何能触达
被管运维目标的 adapter。它的唯一用途是反证"审批不能被绕过"与"E1 硬闸独立生效"。
"""

import datetime as dt
from typing import Any, Final

from tests.fakes.fixtures import CAP_VERSION, SNAPSHOT, WRITE_CAP, WRITE_OP

from xiaowei_agent.capabilities.effect import build_plan_step
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.capabilities.specs import (
    CAPABILITY_ID,
    GATEWAY_NAME,
    OP_LIST,
    POLICY_PROFILE,
    SLOW_QUERY_SURFACE,
)
from xiaowei_agent.capabilities.target import resolve_target
from xiaowei_agent.contracts import (
    AdmissionCertificate,
    ApprovalRequest,
    ApprovalState,
    EffectClass,
    ExecutionPlan,
    IntentDraft,
    IntentSource,
    PlanBudget,
    PlanStep,
    PolicyProfile,
    PolicySnapshot,
    RequestContext,
    RiskLevel,
    StepCondition,
    ToolCall,
)
from xiaowei_agent.governance.approval import ApprovalGate, NeverGrantingApprovalGate
from xiaowei_agent.governance.profiles import SLOW_QUERY_READONLY_PROFILE
from xiaowei_agent.governance.step_admission import admit_step
from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint
from xiaowei_agent.planning.starrocks.compiler import compile_plan
from xiaowei_agent.planning.starrocks.params import SlowQueryParams

POLICY_REVISION: Final[str] = "policy-2026-09-01"
WRITE_PROFILE_ID: Final[str] = "write.synthetic"
TASK_ID: Final[str] = "task-1"

NOW: Final[dt.datetime] = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)

CONTEXT: Final[RequestContext] = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision=POLICY_REVISION,
)

DRAFT: Final[IntentDraft] = IntentDraft(
    intent=CAPABILITY_ID, slots={}, missing=(), confidence=0.9, source=IntentSource.USER
)

TARGET = resolve_target(context=CONTEXT, draft=DRAFT)

PARAMS: Final[SlowQueryParams] = SlowQueryParams(
    window_start=dt.datetime(2026, 9, 2, 11, 30, tzinfo=dt.UTC),
    window_end=NOW,
    min_query_time_ms=10_000,
    row_limit=20,
)

REGISTRY_SNAPSHOT = StaticCapabilityRegistry().snapshot()

POLICY_SNAPSHOT: Final[PolicySnapshot] = PolicySnapshot(
    policy_revision=POLICY_REVISION, profiles=(POLICY_PROFILE, WRITE_PROFILE_ID)
)

# 合成写 profile 只在测试里存在：它允许 MUTATE_TARGET，正是为了证明**即使策略层
# 放行、审批也自洽**，Gateway 的 E1 硬闸仍然独立拒绝。
WRITE_PROFILE: Final[PolicyProfile] = PolicyProfile(
    profile_id=WRITE_PROFILE_ID,
    allowed_operations=(WRITE_OP,),
    allowed_effect_classes=(EffectClass.MUTATE_TARGET,),
    allowed_environment_ids=("dev",),
    risk=RiskLevel.HIGH,
    max_timeout_seconds=30.0,
)


def _entry_candidate() -> Any:
    candidates = DeterministicCapabilityResolver().resolve(
        draft=DRAFT, context=CONTEXT, snapshot=REGISTRY_SNAPSHOT
    )
    return next(item for item in candidates.items if item.operation == OP_LIST)


def slow_query_plan(*, steps: tuple[PlanStep, ...] | None = None) -> ExecutionPlan:
    plan = compile_plan(
        candidate=_entry_candidate(),
        params=PARAMS,
        target=TARGET,
        context=CONTEXT,
        snapshot=REGISTRY_SNAPSHOT,
        surface=SLOW_QUERY_SURFACE,
    )
    return plan if steps is None else plan.model_copy(update={"steps": steps})


def slow_query_step() -> PlanStep:
    return slow_query_plan().steps[0]


def slow_query_call(**overrides: Any) -> ToolCall:
    step = slow_query_step()
    base: dict[str, Any] = {
        "gateway": GATEWAY_NAME,
        "operation": step.operation,
        "step_id": step.step_id,
        "typed_args": dict(step.typed_arguments),
        "timeout_seconds": 30.0,
        "idempotency_key": "idem-1",
    }
    return ToolCall(**(base | overrides))


def synthetic_write_step() -> PlanStep:
    """合成副作用步骤。分类由 fixtures 快照派生，不是硬编码。"""
    return build_plan_step(
        SNAPSHOT,
        capability_id=WRITE_CAP,
        capability_version=CAP_VERSION,
        operation=WRITE_OP,
        step_id="s1",
        typed_arguments={"probe": "noop"},
        depends_on=(),
        condition=StepCondition(),
    )


def forged_write_step() -> PlanStep:
    """把已注册为写的 operation 伪标成只读。

    直接构造 PlanStep 只在测试里可行——这正是要模拟的"计划在存储里被篡改"。
    """
    return PlanStep(
        step_id="s1",
        operation=WRITE_OP,
        typed_arguments={"probe": "noop"},
        depends_on=(),
        side_effect=False,
        effect_class=EffectClass.READ,
    )


def synthetic_write_plan(*, steps: tuple[PlanStep, ...] | None = None) -> ExecutionPlan:
    return ExecutionPlan(
        capability_id=WRITE_CAP,
        capability_version=CAP_VERSION,
        steps=steps if steps is not None else (synthetic_write_step(),),
        policy_profile=WRITE_PROFILE_ID,
        policy_revision=POLICY_REVISION,
        budget=PlanBudget(max_steps=2, max_tool_calls=2, max_model_tokens=4000),
    )


def synthetic_write_call(**overrides: Any) -> ToolCall:
    base: dict[str, Any] = {
        "gateway": GATEWAY_NAME,
        "operation": WRITE_OP,
        "step_id": "s1",
        "typed_args": {"probe": "noop"},
        "timeout_seconds": 30.0,
        "idempotency_key": "idem-write-1",
    }
    return ToolCall(**(base | overrides))


def granted_approval(**overrides: Any) -> ApprovalRequest:
    plan = synthetic_write_plan()
    base: dict[str, Any] = {
        "task_id": TASK_ID,
        "step_id": "s1",
        "plan_hash": compute_plan_hash(plan),
        "target_fingerprint": compute_target_fingerprint(TARGET),
        "policy_revision": POLICY_REVISION,
        "subject": "approver-1",
        "expires_at": NOW + dt.timedelta(hours=1),
        "state": ApprovalState.GRANTED,
    }
    return ApprovalRequest(**(base | overrides))


class CountingApprovalGate:
    """记录被调用次数的 gate；用于断言只读步骤不触发审批。"""

    def __init__(self) -> None:
        self.calls = 0
        self._inner = NeverGrantingApprovalGate()

    def require(self, **kwargs: Any) -> str:
        self.calls += 1
        return self._inner.require(**kwargs)


def admit(
    *,
    step: PlanStep,
    call: ToolCall,
    plan: ExecutionPlan | None = None,
    context: RequestContext = CONTEXT,
    approval: ApprovalRequest | None = None,
    approval_gate: ApprovalGate | None = None,
    profile_environments: tuple[str, ...] | None = None,
) -> AdmissionCertificate:
    """按被测步骤挑选正确的快照与 profile，其余一律走生产路径。"""
    is_write = step.operation == WRITE_OP
    resolved_plan = (
        plan if plan is not None else (synthetic_write_plan() if is_write else slow_query_plan())
    )
    profile = WRITE_PROFILE if is_write else SLOW_QUERY_READONLY_PROFILE
    if profile_environments is not None:
        profile = profile.model_copy(update={"allowed_environment_ids": profile_environments})
    return admit_step(
        step=step,
        plan=resolved_plan,
        call=call,
        context=context,
        target=TARGET,
        snapshot=SNAPSHOT if is_write else REGISTRY_SNAPSHOT,
        policy_snapshot=POLICY_SNAPSHOT,
        profile=profile,
        surface=SLOW_QUERY_SURFACE,
        approval_gate=approval_gate if approval_gate is not None else NeverGrantingApprovalGate(),
        approval=approval,
        task_id=TASK_ID,
        now=NOW,
    )
