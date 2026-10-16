"""目标解析：从上下文与草案得到 ``ResolvedTarget``。

目标解析是确定性的：同一上下文必须得到同一目标，因此也得到同一
``target_fingerprint``——审批绑定与恢复时的漂移检测都依赖这一点。
"""

import pytest

from xiaowei_agent.capabilities.target import (
    KNOWN_ENVIRONMENT_IDS,
    TargetResolutionError,
    resolve_target,
)
from xiaowei_agent.contracts import IntentDraft, IntentSource, RequestContext
from xiaowei_agent.planning import compute_target_fingerprint


def _context(**overrides: str) -> RequestContext:
    base: dict[str, str] = {
        "tenant_id": "dev-local",
        "actor": "alice",
        "environment_id": "dev",
        "trace_id": "0" * 32,
        "policy_revision": "policy-2026-09-01",
    }
    return RequestContext(**(base | overrides))


def _draft(**slots: str) -> IntentDraft:
    return IntentDraft(
        intent="starrocks.slow_query.diagnose",
        slots=slots,
        missing=(),
        confidence=0.9,
        source=IntentSource.USER,
    )


def test_same_context_yields_identical_fingerprint() -> None:
    first = resolve_target(context=_context(), draft=_draft())
    second = resolve_target(context=_context(), draft=_draft())
    assert first == second
    assert compute_target_fingerprint(first) == compute_target_fingerprint(second)


def test_environment_change_changes_the_fingerprint() -> None:
    dev = resolve_target(context=_context(environment_id="dev"), draft=_draft())
    test = resolve_target(context=_context(environment_id="test"), draft=_draft())
    assert compute_target_fingerprint(dev) != compute_target_fingerprint(test)


def test_tenant_change_changes_the_fingerprint() -> None:
    one = resolve_target(context=_context(tenant_id="dev-local"), draft=_draft())
    other = resolve_target(context=_context(tenant_id="other-tenant"), draft=_draft())
    assert compute_target_fingerprint(one) != compute_target_fingerprint(other)


def test_actor_does_not_change_the_fingerprint() -> None:
    """actor 不是目标的一部分：把它算进去会让同一目标随发起人漂移。"""
    alice = resolve_target(context=_context(actor="alice"), draft=_draft())
    bob = resolve_target(context=_context(actor="bob"), draft=_draft())
    assert compute_target_fingerprint(alice) == compute_target_fingerprint(bob)


def test_resolved_target_carries_the_declared_provider_and_kind() -> None:
    target = resolve_target(context=_context(), draft=_draft())
    assert (target.provider, target.resource_kind) == ("starrocks", "cluster")
    assert target.tenant_id == "dev-local"
    assert target.environment_id == "dev"


def test_every_known_environment_resolves() -> None:
    """环境目录里的每一项都必须真的能解析出目标，否则目录本身是错的。"""
    for environment_id in KNOWN_ENVIRONMENT_IDS:
        target = resolve_target(context=_context(environment_id=environment_id), draft=_draft())
        assert target.environment_id == environment_id


def test_unresolvable_environment_fails_closed() -> None:
    """环境无法解析出唯一值时不得取默认环境（ADR-007 D2）。"""
    with pytest.raises(TargetResolutionError):
        resolve_target(context=_context(environment_id="unknown"), draft=_draft())


def test_model_supplied_environment_cannot_override_the_context() -> None:
    """slots 里的 environment_id 只能作线索；与 RequestContext 冲突时以 context 为准。"""
    target = resolve_target(
        context=_context(environment_id="dev"), draft=_draft(environment_id="test")
    )
    assert target.environment_id == "dev"


def test_slots_cannot_introduce_extra_resource_ids() -> None:
    """槽位不得扩大目标：资源 ID 只能来自环境目录。"""
    plain = resolve_target(context=_context(), draft=_draft())
    polluted = resolve_target(
        context=_context(),
        draft=_draft(database="sales", user_name="app_user_1", query_id="q1"),
    )
    assert plain.resource_ids == polluted.resource_ids
    assert compute_target_fingerprint(plain) == compute_target_fingerprint(polluted)
