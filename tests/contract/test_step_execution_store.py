"""M5 步骤 journal 的可构造状态、非法反例与摘要契约。"""

import datetime as dt

import pytest
from pydantic import ValidationError
from tests.fakes.sinks import make_event
from tests.suites.task_store import STEP_EXECUTION_CASES, bind

from xiaowei_agent.contracts import (
    EvidenceEnvelope,
    ExternalSource,
    LeaseGrant,
    PipelineStage,
    StepAttemptDecision,
    StepCommitRejection,
    StepOutcomeKind,
    StepResultStatus,
    TaskRecord,
    TaskStatus,
)
from xiaowei_agent.contracts.evidence import evidence_id as contract_evidence_id
from xiaowei_agent.evidence.builder import evidence_id as builder_evidence_id
from xiaowei_agent.persistence.store import (
    StepAttemptResult,
    StepCommitCommand,
    StepCommitResult,
    StepExecutionRecord,
    TaskAttemptGrant,
    step_commit_digest,
)

_NOW = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)


def _winner() -> TaskRecord:
    return TaskRecord(
        task_id="task-1",
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        idempotency_key="idem-1",
        request_digest="a" * 64,
        status=TaskStatus.RUNNING,
        version=2,
        created_seq=1,
        attempt_number=1,
        task_failure_count=0,
        next_attempt_at=None,
        lease_owner="worker-1",
        lease_expires_at=_NOW + dt.timedelta(seconds=60),
        fencing_token=7,
    )


def _grant() -> TaskAttemptGrant:
    return TaskAttemptGrant(
        lease=LeaseGrant(
            task_id="task-1",
            owner="worker-1",
            expires_at=_NOW + dt.timedelta(seconds=60),
            fencing_token=7,
        ),
        attempt_number=1,
    )


def _evidence() -> EvidenceEnvelope:
    return EvidenceEnvelope(
        evidence_id=contract_evidence_id(task_id="task-1", step_id="s1"),
        capability_id="starrocks.slow_query.diagnose",
        capability_version="1.0.0",
        facts=({"query_id": "q1"},),
        source="starrocks-fake",
        source_kind=ExternalSource.TOOL,
        captured_at=_NOW,
        sampled=False,
        limitations=("deterministic fixture",),
    )


def _event(*, event_id: str = "event-1"):
    return make_event(
        stage=PipelineStage.GATEWAY,
        task_id="task-1",
        event_id=event_id,
    ).model_copy(update={"attempt_number": 1})


def _in_flight() -> StepExecutionRecord:
    return StepExecutionRecord(
        task_id="task-1",
        step_id="s1",
        attempt_count=1,
        last_fencing_token=7,
        started_at=_NOW,
    )


def test_evidence_id_is_the_same_function_used_by_the_builder() -> None:
    assert builder_evidence_id is contract_evidence_id


def test_in_flight_and_malformed_terminal_rows_are_both_constructible() -> None:
    assert _in_flight().result_status is None
    malformed = StepExecutionRecord(
        task_id="task-1",
        step_id="s1",
        attempt_count=1,
        last_fencing_token=7,
        result_status=StepResultStatus.FAILED,
        kind=StepOutcomeKind.MALFORMED_ADAPTER,
        evidence_id=None,
        commit_digest="b" * 64,
        started_at=_NOW,
        committed_at=_NOW,
    )
    assert malformed.evidence_id is None


@pytest.mark.parametrize(
    "updates",
    [
        {"result_status": StepResultStatus.OK},
        {"commit_digest": "b" * 64},
        {
            "result_status": StepResultStatus.SKIPPED,
            "kind": StepOutcomeKind.TOOL_RESULT,
            "evidence_id": "task-1:s1",
            "commit_digest": "b" * 64,
            "committed_at": _NOW,
        },
        {
            "result_status": StepResultStatus.OK,
            "kind": StepOutcomeKind.TOOL_RESULT,
            "commit_digest": "b" * 64,
            "committed_at": _NOW,
        },
        {
            "result_status": StepResultStatus.FAILED,
            "kind": StepOutcomeKind.MALFORMED_ADAPTER,
            "evidence_id": "task-1:s1",
            "commit_digest": "b" * 64,
            "committed_at": _NOW,
        },
        {
            "result_status": StepResultStatus.OK,
            "kind": StepOutcomeKind.MALFORMED_ADAPTER,
            "commit_digest": "b" * 64,
            "committed_at": _NOW,
        },
    ],
)
def test_invalid_step_record_shapes_are_rejected(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        StepExecutionRecord(**(_in_flight().model_dump() | updates))


@pytest.mark.parametrize(
    "updates",
    [
        {"kind": StepOutcomeKind.TOOL_RESULT, "evidence": None},
        {
            "kind": StepOutcomeKind.MALFORMED_ADAPTER,
            "evidence": _evidence(),
        },
        {
            "kind": StepOutcomeKind.MALFORMED_ADAPTER,
            "status": StepResultStatus.OK,
            "evidence": None,
        },
        {"status": StepResultStatus.SKIPPED},
    ],
)
def test_invalid_step_commit_shapes_are_rejected(updates: dict[str, object]) -> None:
    values: dict[str, object] = {
        "grant": _grant(),
        "step_id": "s1",
        "kind": StepOutcomeKind.TOOL_RESULT,
        "status": StepResultStatus.OK,
        "evidence": _evidence(),
        "audit_events": (_event(),),
    }
    with pytest.raises(ValidationError):
        StepCommitCommand(**(values | updates))


def test_commit_rejects_cross_task_evidence_and_cross_attempt_audit() -> None:
    with pytest.raises(ValidationError):
        StepCommitCommand(
            grant=_grant(),
            step_id="s1",
            kind=StepOutcomeKind.TOOL_RESULT,
            status=StepResultStatus.OK,
            evidence=_evidence().model_copy(update={"evidence_id": "other:s1"}),
            audit_events=(_event(),),
        )
    with pytest.raises(ValidationError):
        StepCommitCommand(
            grant=_grant(),
            step_id="s1",
            kind=StepOutcomeKind.TOOL_RESULT,
            status=StepResultStatus.OK,
            evidence=_evidence(),
            audit_events=(_event().model_copy(update={"attempt_number": 2}),),
        )


def test_step_commit_digest_is_order_independent_but_multiplicity_sensitive() -> None:
    command = StepCommitCommand(
        grant=_grant(),
        step_id="s1",
        kind=StepOutcomeKind.TOOL_RESULT,
        status=StepResultStatus.OK,
        evidence=_evidence(),
        audit_events=(_event(event_id="a"), _event(event_id="b")),
    )
    reordered = command.model_copy(update={"audit_events": tuple(reversed(command.audit_events))})
    assert step_commit_digest(reordered) == step_commit_digest(command)

    extra = command.model_copy(
        update={"audit_events": (*command.audit_events, _event(event_id="c"))}
    )
    assert step_commit_digest(extra) != step_commit_digest(command)


def test_step_results_allow_missing_rows_only_on_rejection_paths() -> None:
    with pytest.raises(ValidationError):
        StepAttemptResult(
            decision=StepAttemptDecision.PROCEED,
            winner=_winner(),
            attempts_used=1,
        )
    result = StepCommitResult(
        committed=False,
        winner=_winner(),
        rejection=StepCommitRejection.STALE_FENCING,
    )
    assert result.record is None


bind(globals(), STEP_EXECUTION_CASES)
