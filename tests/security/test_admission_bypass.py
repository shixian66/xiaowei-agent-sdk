"""准入不能被绕过，且两道闸互不替代。

"M0-M7 对被管运维目标的 E1 调用次数恒为 0"这条承诺**不依赖审批链是否被攻破**：
即使伪造出绑定自洽的 GRANTED 审批、策略层也放行，Gateway 的 E1 硬闸仍然拒绝。
"""

from typing import Any

import pytest
from tests.conftest import make_certificate
from tests.fakes.admission import (
    CONTEXT,
    admit,
    forged_write_step,
    granted_approval,
    slow_query_call,
    slow_query_step,
    synthetic_write_call,
    synthetic_write_plan,
    synthetic_write_step,
)

from xiaowei_agent.capabilities.effect import SpecResolutionError
from xiaowei_agent.contracts import EffectClass
from xiaowei_agent.governance.approval import ApprovalRequiredError
from xiaowei_agent.tools.fake import RecordingToolAdapter
from xiaowei_agent.tools.gateway import DeterministicToolGateway

pytestmark = pytest.mark.security


@pytest.fixture
def adapter() -> RecordingToolAdapter:
    from xiaowei_agent.contracts import AdapterStatus
    from xiaowei_agent.tools.adapter import AdapterResponse

    return RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.OK,
                payload=(),
                source="starrocks-fake",
                error=None,
                elapsed_ms=1,
            ),
        )
    )


def test_mislabelled_write_step_is_refused_with_zero_adapter_calls(
    adapter: RecordingToolAdapter,
) -> None:
    """伪标拒绝：ToolGateway 与 adapter 调用次数均为 0。"""
    forged = forged_write_step()
    with pytest.raises(SpecResolutionError):
        admit(
            step=forged,
            call=synthetic_write_call(),
            plan=synthetic_write_plan(steps=(forged,)),
        )
    assert adapter.call_count == 0


def test_side_effect_step_without_approval_calls_nothing(
    adapter: RecordingToolAdapter,
) -> None:
    with pytest.raises(ApprovalRequiredError):
        admit(
            step=synthetic_write_step(),
            call=synthetic_write_call(),
            plan=synthetic_write_plan(),
            approval=None,
        )
    assert adapter.call_count == 0


async def test_granted_approval_still_cannot_execute_e1(
    adapter: RecordingToolAdapter,
) -> None:
    """两道闸互不替代。

    审批链在这条用例里是**完全自洽**的：绑定正确、未过期、状态 GRANTED，策略
    profile 也允许 MUTATE_TARGET。凭证因此被成功签发——然后 Gateway 的 E1 硬闸
    独立拒绝。
    """
    certificate = admit(
        step=synthetic_write_step(),
        call=synthetic_write_call(),
        plan=synthetic_write_plan(),
        approval=granted_approval(),
    )
    assert certificate.effect_class is EffectClass.MUTATE_TARGET

    gateway = DeterministicToolGateway(adapters={"starrocks": adapter})
    with pytest.raises(PermissionError, match="E1 execution is disabled"):
        await gateway.invoke(synthetic_write_call(), context=CONTEXT, admission=certificate)
    assert adapter.call_count == 0


async def test_a_certificate_for_another_step_cannot_reach_the_adapter(
    adapter: RecordingToolAdapter,
) -> None:
    """拿只读步骤的合法凭证去执行别的调用，Gateway 必须拒绝。"""
    certificate = admit(step=slow_query_step(), call=slow_query_call())
    gateway = DeterministicToolGateway(adapters={"starrocks": adapter})
    with pytest.raises(PermissionError):
        await gateway.invoke(
            slow_query_call(idempotency_key="idem-other"),
            context=CONTEXT,
            admission=certificate,
        )
    assert adapter.call_count == 0


def test_admission_is_the_only_certificate_issuer() -> None:
    """凭证无法在准入之外被构造或复制。"""
    certificate = admit(step=slow_query_step(), call=slow_query_call())
    with pytest.raises(NotImplementedError):
        certificate.model_copy(update={"effect_class": EffectClass.READ})


def test_forged_certificate_cannot_be_built_from_a_plain_dict() -> None:
    from pydantic import ValidationError

    from xiaowei_agent.contracts import AdmissionCertificate

    payload: dict[str, Any] = {
        field: getattr(make_certificate(slow_query_call()), field)
        for field in AdmissionCertificate.model_fields
    }
    with pytest.raises(ValidationError):
        AdmissionCertificate.model_validate(payload)


async def test_read_step_admission_reaches_the_adapter(
    adapter: RecordingToolAdapter,
) -> None:
    """反例配对：准入不是恒拒——合法只读调用必须真的到达 adapter。

    没有这条，上面每一条"调用次数为 0"的断言都可能是因为**什么都执行不了**。
    """
    call = slow_query_call()
    certificate = admit(step=slow_query_step(), call=call)
    gateway = DeterministicToolGateway(adapters={"starrocks": adapter})
    result = await gateway.invoke(call, context=CONTEXT, admission=certificate)
    assert adapter.call_count == 1
    assert result.status.value == "ok"
