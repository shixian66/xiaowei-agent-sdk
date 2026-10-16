"""准入凭证不可伪造，且必须与当前 policy revision 绑定。

此前 Gateway 的**输出** ``ToolResult`` 有签发凭据保护，而它信任的**授权输入**
``AdmissionCertificate`` 是普通 public DTO——任何调用方都能构造一张
``allow=True`` 的凭证直接驱动 adapter。伪造结果只污染一次回答，伪造凭证绕过的
是整条策略链，所以授权凭据比结果更需要不可伪造。
"""

import asyncio

import pytest
from pydantic import ValidationError
from tests.conftest import make_certificate
from tests.fakes.fixtures import FIXTURE_TOOL_CALL

from xiaowei_agent.contracts import (
    AdapterStatus,
    AdmissionCertificate,
    EffectClass,
    PolicyDecision,
    RequestContext,
    RiskLevel,
)
from xiaowei_agent.governance import BindingError, issue_admission_certificate
from xiaowei_agent.planning import compute_tool_call_hash
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.fake import RecordingToolAdapter
from xiaowei_agent.tools.gateway import DeterministicToolGateway

pytestmark = pytest.mark.security

HEX = "0" * 64
CURRENT = "policy-2026-09-01"


def _decision(*, allow: bool = True, revision: str = CURRENT) -> PolicyDecision:
    return PolicyDecision(
        allow=allow,
        reason_code="readonly.allowed",
        risk=RiskLevel.LOW,
        policy_revision=revision,
        obligations=(),
    )


def _context(revision: str = CURRENT) -> RequestContext:
    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="0" * 32,
        policy_revision=revision,
    )


def _gateway() -> tuple[DeterministicToolGateway, RecordingToolAdapter]:
    adapter = RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.OK,
                payload=({"query_id": "q1"},),
                source="starrocks-fake",
                error=None,
                elapsed_ms=1,
            ),
        )
    )
    return DeterministicToolGateway(adapters={FIXTURE_TOOL_CALL.gateway: adapter}), adapter


# --- 不可伪造 ---------------------------------------------------------------


def test_public_construction_is_refused() -> None:
    with pytest.raises(ValidationError):
        AdmissionCertificate(
            step_id=FIXTURE_TOOL_CALL.step_id,
            operation=FIXTURE_TOOL_CALL.operation,
            effect_class=EffectClass.READ,
            policy_decision=_decision(),
            approval_ref=None,
            plan_hash=HEX,
            target_fingerprint=HEX,
            tool_call_hash=compute_tool_call_hash(FIXTURE_TOOL_CALL),
        )


def test_model_validate_without_the_witness_is_refused() -> None:
    """绕过 ``__init__`` 走 ``model_validate`` 同样必须失败。"""
    with pytest.raises(ValidationError):
        AdmissionCertificate.model_validate(
            {
                "step_id": "s1",
                "operation": "op",
                "effect_class": EffectClass.READ,
                "policy_decision": _decision(),
                "approval_ref": None,
                "plan_hash": HEX,
                "target_fingerprint": HEX,
                "tool_call_hash": HEX,
            }
        )


def test_model_construct_is_refused() -> None:
    with pytest.raises(NotImplementedError):
        AdmissionCertificate.model_construct()


def test_a_legitimate_certificate_cannot_be_copied() -> None:
    """复制出的凭证不再由 StepAdmission 签发。

    允许复制等于允许"拿一张合法凭证改 operation"，绑定就形同虚设。
    """
    certificate = make_certificate(FIXTURE_TOOL_CALL)
    with pytest.raises(NotImplementedError):
        certificate.model_copy(update={"operation": "starrocks.write"})


def test_the_issuer_recomputes_the_call_hash_instead_of_trusting_input() -> None:
    """签发入口不接受调用方传入的 tool_call_hash。

    否则"凭证绑定调用内容"这件事又回到了调用方的诚实上。
    """
    certificate = issue_admission_certificate(
        call=FIXTURE_TOOL_CALL,
        context=_context(),
        decision=_decision(),
        effect_class=EffectClass.READ,
        plan_hash=HEX,
        target_fingerprint=HEX,
    )
    assert certificate.tool_call_hash == compute_tool_call_hash(FIXTURE_TOOL_CALL)


# --- policy revision 绑定 ---------------------------------------------------


def test_issuing_under_a_stale_revision_is_refused() -> None:
    with pytest.raises(BindingError):
        issue_admission_certificate(
            call=FIXTURE_TOOL_CALL,
            context=_context("policy-current"),
            decision=_decision(revision="policy-old"),
            effect_class=EffectClass.READ,
            plan_hash=HEX,
            target_fingerprint=HEX,
        )


def test_gateway_refuses_a_stale_certificate_and_never_calls_the_adapter() -> None:
    """签发入口拦得住，不代表消费端拦得住——两处必须各自证明。

    policy 收紧后旧凭证若仍可用，"policy 变化不能静默让旧授权继续生效"在执行
    边界上就没有落实。
    """
    gateway, adapter = _gateway()
    certificate = make_certificate(FIXTURE_TOOL_CALL)  # 签发于 CURRENT
    with pytest.raises(PermissionError, match="stale policy revision"):
        asyncio.run(
            gateway.invoke(
                FIXTURE_TOOL_CALL,
                context=_context("policy-rotated"),
                admission=certificate,
            )
        )
    assert adapter.calls == []


def test_revision_is_checked_before_allow() -> None:
    """顺序断言：凭证过期时 ``allow`` 已无意义，不能先看 allow。

    用一张 ``allow=False`` 且 revision 也过期的凭证：如果先看 allow，拒绝原因
    会是 "policy denied"；正确顺序下必须报 revision 过期。
    """
    gateway, adapter = _gateway()
    certificate = make_certificate(FIXTURE_TOOL_CALL, policy_decision=_decision(allow=False))
    with pytest.raises(PermissionError, match="stale policy revision"):
        asyncio.run(
            gateway.invoke(
                FIXTURE_TOOL_CALL,
                context=_context("policy-rotated"),
                admission=certificate,
            )
        )
    assert adapter.calls == []


def test_matching_revision_still_passes() -> None:
    """正例：否则上面几条可能因为别的原因而"碰巧"拒绝。"""
    gateway, adapter = _gateway()
    result = asyncio.run(
        gateway.invoke(
            FIXTURE_TOOL_CALL,
            context=_context(),
            admission=make_certificate(FIXTURE_TOOL_CALL),
        )
    )
    assert result.status.value == "ok"
    assert len(adapter.calls) == 1
