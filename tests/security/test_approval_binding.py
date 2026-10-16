"""审批绑定与 policy revision 的确定性校验。

覆盖 M2 测试门中的"未知 policy revision"与"目标不稳定"，以及 ARCHITECTURE §5.7
的恢复重解析顺序。
"""

import datetime as dt

import pytest
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET

from xiaowei_agent.contracts import (
    ApprovalRequest,
    ApprovalState,
    BindingRejection,
    PolicySnapshot,
)
from xiaowei_agent.governance import (
    BindingError,
    verify_approval_binding,
    verify_policy_revision,
)
from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint

pytestmark = pytest.mark.security

_NOW = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)
_SNAPSHOT = PolicySnapshot(policy_revision="policy-2026-09-01", profiles=("readonly.default",))


def _approval(**overrides: object) -> ApprovalRequest:
    base: dict[str, object] = {
        "task_id": "t1",
        "step_id": "s1",
        "plan_hash": compute_plan_hash(FIXTURE_PLAN),
        "target_fingerprint": compute_target_fingerprint(FIXTURE_TARGET),
        "policy_revision": FIXTURE_PLAN.policy_revision,
        "subject": "alice",
        "expires_at": _NOW + dt.timedelta(hours=1),
        "state": ApprovalState.GRANTED,
    }
    return ApprovalRequest(**(base | overrides))


def test_plan_with_unknown_policy_revision_is_rejected() -> None:
    """计划所依据的 revision 不是当前生效的，必须拒绝。"""
    stale = FIXTURE_PLAN.model_copy(update={"policy_revision": "policy-2026-01-01"})
    with pytest.raises(BindingError) as exc:
        verify_policy_revision(_SNAPSHOT, plan=stale)
    assert exc.value.rejection is BindingRejection.POLICY_REVISION_DRIFT


def test_plan_with_unregistered_policy_profile_is_rejected() -> None:
    unknown = FIXTURE_PLAN.model_copy(update={"policy_profile": "write.anything"})
    with pytest.raises(BindingError):
        verify_policy_revision(_SNAPSHOT, plan=unknown)


def test_matching_policy_revision_passes() -> None:
    verify_policy_revision(_SNAPSHOT, plan=FIXTURE_PLAN)


def test_matching_binding_passes() -> None:
    verify_approval_binding(
        approval=_approval(), plan=FIXTURE_PLAN, target=FIXTURE_TARGET, now=_NOW
    )


def test_plan_drift_is_rejected() -> None:
    drifted = FIXTURE_PLAN.model_copy(update={"capability_version": "1.0.1"})
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(approval=_approval(), plan=drifted, target=FIXTURE_TARGET, now=_NOW)
    assert exc.value.rejection is BindingRejection.PLAN_DRIFT


def test_budget_drift_is_detected_through_the_plan_hash() -> None:
    """预算被放大同样是计划漂移——这正是把 budget 纳入 plan_hash 的目的。"""
    bigger = FIXTURE_PLAN.budget.model_copy(update={"max_tool_calls": 999})
    drifted = FIXTURE_PLAN.model_copy(update={"budget": bigger})
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(approval=_approval(), plan=drifted, target=FIXTURE_TARGET, now=_NOW)
    assert exc.value.rejection is BindingRejection.PLAN_DRIFT


def test_target_drift_is_rejected() -> None:
    drifted = FIXTURE_TARGET.model_copy(update={"resource_ids": ("c3",)})
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(approval=_approval(), plan=FIXTURE_PLAN, target=drifted, now=_NOW)
    assert exc.value.rejection is BindingRejection.TARGET_DRIFT


def test_policy_revision_drift_between_approval_and_plan_is_rejected() -> None:
    """policy 变化不能静默让旧审批继续生效（ARCHITECTURE §15）。"""
    approval = _approval(policy_revision="policy-2026-01-01")
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(
            approval=approval, plan=FIXTURE_PLAN, target=FIXTURE_TARGET, now=_NOW
        )
    assert exc.value.rejection is BindingRejection.POLICY_REVISION_DRIFT


def test_expired_approval_is_rejected() -> None:
    approval = _approval(expires_at=_NOW - dt.timedelta(seconds=1))
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(
            approval=approval, plan=FIXTURE_PLAN, target=FIXTURE_TARGET, now=_NOW
        )
    assert exc.value.rejection is BindingRejection.APPROVAL_EXPIRED


def test_approval_expiring_exactly_now_is_rejected() -> None:
    """边界取闭区间：到期时刻本身即失效，不给"刚好赶上"留缝。"""
    approval = _approval(expires_at=_NOW)
    with pytest.raises(BindingError):
        verify_approval_binding(
            approval=approval, plan=FIXTURE_PLAN, target=FIXTURE_TARGET, now=_NOW
        )


@pytest.mark.parametrize(
    "state", [ApprovalState.PENDING, ApprovalState.REJECTED, ApprovalState.EXPIRED]
)
def test_non_granted_approval_is_rejected(state: ApprovalState) -> None:
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(
            approval=_approval(state=state),
            plan=FIXTURE_PLAN,
            target=FIXTURE_TARGET,
            now=_NOW,
        )
    assert exc.value.rejection is BindingRejection.APPROVAL_NOT_GRANTED


def test_expiry_is_reported_when_both_expiry_and_state_are_violated() -> None:
    """两项同时违反时，报出的必须是更具体的过期原因。

    **必须让两项都违反**：只让审批过期而 state 仍是 GRANTED 的话，状态检查本来
    就会通过，顺序对结果毫无影响——那样的测试是空的（变异测试已证实：调换顺序
    仍全绿）。
    """
    approval = _approval(state=ApprovalState.PENDING, expires_at=_NOW - dt.timedelta(seconds=1))
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(
            approval=approval, plan=FIXTURE_PLAN, target=FIXTURE_TARGET, now=_NOW
        )
    assert exc.value.rejection is BindingRejection.APPROVAL_EXPIRED


def test_drift_is_not_reported_while_the_approval_itself_is_invalid() -> None:
    """审批本身无效时，不应先报计划漂移——两者都违反时原因取前者。"""
    drifted = FIXTURE_PLAN.model_copy(update={"capability_version": "9.9.9"})
    approval = _approval(state=ApprovalState.REJECTED)
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(approval=approval, plan=drifted, target=FIXTURE_TARGET, now=_NOW)
    assert exc.value.rejection is BindingRejection.APPROVAL_NOT_GRANTED
