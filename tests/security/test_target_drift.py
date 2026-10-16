"""目标漂移：不可信输入不得改变已解析的目标。

目标指纹是审批绑定与恢复校验的承重字段。凡是能让模型输出、用户原文或外部文本
改变指纹的路径，都等于让一份已批准的计划在恢复后指向另一个目标。
"""

import pytest

from xiaowei_agent.capabilities.target import (
    TargetRejection,
    TargetResolutionError,
    resolve_target,
)
from xiaowei_agent.contracts import IntentDraft, IntentSource, RequestContext
from xiaowei_agent.planning import compute_target_fingerprint

pytestmark = pytest.mark.security

CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-01",
)


def _draft(**slots: str) -> IntentDraft:
    return IntentDraft(
        intent="starrocks.slow_query.diagnose",
        slots=slots,
        missing=(),
        confidence=0.9,
        source=IntentSource.USER,
    )


@pytest.mark.parametrize(
    "hostile_environment",
    ["prod", "production", "test", "unknown", "dev "[:3] + "-prod"],
)
def test_slot_environment_never_reaches_the_resolved_target(
    hostile_environment: str,
) -> None:
    """即使槽位指定了别的环境（含生产），目标仍恒为上下文里的环境。"""
    target = resolve_target(context=CONTEXT, draft=_draft(environment_id=hostile_environment))
    assert target.environment_id == CONTEXT.environment_id


def test_target_fingerprint_is_invariant_under_slot_pollution() -> None:
    """把全部槽位塞满敌对取值，指纹必须与空槽位时逐字节相同。"""
    clean = compute_target_fingerprint(resolve_target(context=CONTEXT, draft=_draft()))
    polluted = compute_target_fingerprint(
        resolve_target(
            context=CONTEXT,
            draft=_draft(
                environment_id="prod",
                database="sales",
                user_name="root",
                query_id="q1",
                window_minutes="99999",
            ),
        )
    )
    assert clean == polluted


def test_unknown_environment_is_refused_with_a_closed_set_rejection() -> None:
    """拒绝原因必须是闭集枚举成员，不得是拼接出来的自由文本。"""
    hostile = CONTEXT.model_copy(update={"environment_id": "prod"})
    with pytest.raises(TargetResolutionError) as err:
        resolve_target(context=hostile, draft=_draft())
    assert err.value.rejection is TargetRejection.UNKNOWN_ENVIRONMENT


def test_rejection_message_never_echoes_the_environment_id() -> None:
    """被拒的环境标识来自调用方，不得出现在异常文本里。"""
    canary = "canary" + "-env-7731"
    hostile = CONTEXT.model_copy(update={"environment_id": canary})
    with pytest.raises(TargetResolutionError) as err:
        resolve_target(context=hostile, draft=_draft())
    assert canary not in str(err.value)
    assert canary not in repr(err.value)


def test_production_is_not_in_the_environment_directory() -> None:
    """生产连接当前不授权（ADR-007 D6）：目录里不得存在任何生产环境项。"""
    from xiaowei_agent.capabilities.target import KNOWN_ENVIRONMENT_IDS

    assert not ({"prod", "production", "prd"} & set(KNOWN_ENVIRONMENT_IDS))
