"""时间边界与 trace/audit 的卫生规则。

两类问题都不是"少写一个校验"，而是**契约层没把不合法的形状排除掉**：naive
datetime 会让审批过期判定变成 TypeError 崩溃；只脱敏 value 会把 secret 留在 key 里。
"""

import datetime as dt

import pytest
from pydantic import ValidationError
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET

from xiaowei_agent.contracts import (
    ApprovalRequest,
    ApprovalState,
    BindingRejection,
    ExternalContent,
    ExternalInput,
    ExternalInputKind,
    ExternalSource,
    PipelineStage,
    StageOutcome,
    TraceEvent,
)
from xiaowei_agent.governance import BindingError, verify_approval_binding
from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint

# 伪造取值必须**拆开写**：连续的 ``token=<值>`` 会被 secret-scan 判为泄漏。
# 这是仓库既有约定（见 tests/security/test_redaction.py 的 _PW / _TOKEN），
# 由 test_no_contiguous_secret_shaped_literal 在本地 gate 内强制。
_FAKE = "abc123" + "def456"

pytestmark = pytest.mark.security

_AT = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)
_NAIVE = dt.datetime(2026, 9, 2)


# --- naive datetime ------------------------------------------------------
def test_external_content_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ExternalContent.capture(source=ExternalSource.TOOL, content="x", captured_at=_NAIVE)


def test_approval_request_rejects_naive_expiry() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ApprovalRequest(
            task_id="t",
            step_id="s",
            plan_hash="h",
            target_fingerprint="f",
            policy_revision="r",
            subject="alice",
            expires_at=_NAIVE,
            state=ApprovalState.GRANTED,
        )


def test_trace_event_rejects_naive_occurred_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _event({"k": "v"}, occurred_at=_NAIVE)


def test_binding_rejects_naive_now_structurally_not_by_crashing() -> None:
    """naive `now` 与 aware `expires_at` 相比会抛 TypeError——那是崩溃，不是拒绝。"""
    approval = ApprovalRequest(
        task_id="t",
        step_id="s",
        plan_hash=compute_plan_hash(FIXTURE_PLAN),
        target_fingerprint=compute_target_fingerprint(FIXTURE_TARGET),
        policy_revision=FIXTURE_PLAN.policy_revision,
        subject="alice",
        expires_at=_AT + dt.timedelta(hours=1),
        state=ApprovalState.GRANTED,
    )
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(
            approval=approval, plan=FIXTURE_PLAN, target=FIXTURE_TARGET, now=_NAIVE
        )
    assert exc.value.rejection is BindingRejection.APPROVAL_EXPIRED


# --- trace detail --------------------------------------------------------
def _event(detail: dict[str, str], **overrides: object) -> TraceEvent:
    base: dict[str, object] = {
        "event_id": "e1",
        "trace_id": "0" * 32,
        "task_id": "t1",
        "stage": PipelineStage.GATEWAY,
        "outcome": StageOutcome.OK,
        "occurred_at": _AT,
        "capability_id": None,
        "step_id": "s1",
        "policy_revision": "r",
        "error": None,
        "detail": detail,
    }
    return TraceEvent(**(base | overrides))


def test_detail_keys_are_redacted_too() -> None:
    """只脱敏 value 会把 secret 留在 key 里——key 同样是调用方拼出来的自由文本。"""
    event = _event({"token=" + _FAKE: "safe"})
    assert not any(_FAKE in key for key in event.detail)


def test_detail_key_collision_after_redaction_is_rejected() -> None:
    """脱敏后两个键塌成同一个，静默覆盖会丢失一条事件明细。"""
    with pytest.raises(ValidationError, match="collides with an earlier key after redaction"):
        _event({"token=" + "a" * 12: "1", "token=" + "b" * 12: "2"})


def test_trace_id_must_be_a_real_trace_id() -> None:
    """用 StrictStr 而非 TraceId 会让任意字符串混进审计事件。"""
    with pytest.raises(ValidationError):
        _event({"k": "v"}, trace_id="not-a-trace")


def test_tool_result_trace_id_is_typed(gateway, ok_call, context, admission) -> None:
    from xiaowei_agent.contracts import ToolResult

    assert ToolResult.model_fields["trace_id"].metadata  # TraceId 带 AfterValidator


# --- ExternalInput 组合封闭 ----------------------------------------------
def _content() -> ExternalContent:
    return ExternalContent.capture(source=ExternalSource.USER, content="more info", captured_at=_AT)


@pytest.mark.parametrize(
    ("kind", "approval_ref", "user_text"),
    [
        (ExternalInputKind.APPROVAL_DECISION, None, None),
        (ExternalInputKind.APPROVAL_DECISION, None, "content"),
        (ExternalInputKind.APPROVAL_DECISION, "a1", "content"),
        (ExternalInputKind.USER_SUPPLEMENT, None, None),
        (ExternalInputKind.USER_SUPPLEMENT, "a1", None),
        (ExternalInputKind.USER_SUPPLEMENT, "a1", "content"),
    ],
)
def test_contradictory_external_input_combinations_are_rejected(
    kind: ExternalInputKind, approval_ref: str | None, user_text: str | None
) -> None:
    """这是 Runner resume 的跨边界输入，半空或自相矛盾的对象不能留给下游猜。"""
    with pytest.raises(ValidationError):
        ExternalInput(
            kind=kind,
            approval_ref=approval_ref,
            user_text=_content() if user_text else None,
        )


def test_the_two_legitimate_shapes_are_accepted() -> None:
    approval = ExternalInput(
        kind=ExternalInputKind.APPROVAL_DECISION, approval_ref="a1", user_text=None
    )
    supplement = ExternalInput(
        kind=ExternalInputKind.USER_SUPPLEMENT, approval_ref=None, user_text=_content()
    )
    assert approval.approval_ref == "a1"
    assert supplement.user_text is not None
