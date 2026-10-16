"""M5 任务分派、领取与重试契约。"""

import datetime as dt

import pytest
from pydantic import ValidationError
from tests.conftest import make_envelope, make_submission
from tests.fakes.sinks import make_event
from tests.suites.task_store import DISPATCH_ATTEMPT_CASES, bind

from xiaowei_agent import contracts
from xiaowei_agent.contracts import (
    AttemptIntent,
    LeaseGrant,
    PipelineStage,
    RequestContext,
    TaskRecord,
    TaskStatus,
)
from xiaowei_agent.persistence.store import (
    DispatchQuery,
    RetryCommand,
    RetryDecision,
    RetryReason,
    RetryResult,
    TaskAttemptCommand,
    TaskAttemptGrant,
    TaskAttemptRejection,
    TaskAttemptResult,
    submission_digest,
)

_NOW = dt.datetime(2026, 9, 5, 11, 0, tzinfo=dt.UTC)


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="0" * 32,
        policy_revision="policy-1",
    )


def _winner(**updates: object) -> TaskRecord:
    values: dict[str, object] = {
        "task_id": "task-1",
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "actor": "alice",
        "idempotency_key": "idem-1",
        "request_digest": "a" * 64,
        "status": TaskStatus.RUNNING,
        "version": 1,
        "created_seq": 1,
        "attempt_number": 1,
        "task_failure_count": 0,
        "next_attempt_at": None,
        "retry_scheduled_by_attempt": None,
        "retry_command_digest": None,
        "lease_owner": "worker-1",
        "lease_expires_at": _NOW + dt.timedelta(seconds=60),
        "fencing_token": 7,
    }
    return TaskRecord(**(values | updates))


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


def test_dispatch_query_has_no_caller_supplied_clock() -> None:
    assert set(DispatchQuery.model_fields) == {"tenant_id", "environment_id", "limit"}


def test_attempt_command_has_no_clock_or_failure_limit() -> None:
    assert set(TaskAttemptCommand.model_fields) == {
        "task_id",
        "intent",
        "owner",
        "ttl_seconds",
        "trace_id",
    }


def test_attempt_grant_delegates_task_and_fencing_identity() -> None:
    assert _grant().task_id == "task-1"
    assert _grant().fencing_token == 7


def test_successful_attempt_result_requires_grant_and_submission() -> None:
    with pytest.raises(ValidationError):
        TaskAttemptResult(applied=True, winner=_winner())


def test_rejected_attempt_result_cannot_expose_submission() -> None:
    with pytest.raises(ValidationError):
        TaskAttemptResult(
            applied=False,
            winner=_winner(),
            rejection=TaskAttemptRejection.LIVE_LEASE,
            submission=make_submission(_context()),
        )


def test_successful_attempt_result_requires_cross_object_identity() -> None:
    with pytest.raises(ValidationError):
        TaskAttemptResult(
            applied=True,
            winner=_winner(task_id="other-task"),
            grant=_grant(),
            submission=make_submission(_context()),
        )


def test_only_internal_terminal_rejections_may_return_committed_audit() -> None:
    event = make_event(task_id="task-1", stage=PipelineStage.LIFECYCLE).model_copy(
        update={"attempt_number": 1}
    )
    with pytest.raises(ValidationError):
        TaskAttemptResult(
            applied=False,
            winner=_winner(),
            rejection=TaskAttemptRejection.LIVE_LEASE,
            committed_audit_events=(event,),
        )


@pytest.mark.parametrize("rejection", list(TaskAttemptRejection))
def test_every_attempt_rejection_hides_submission(rejection: TaskAttemptRejection) -> None:
    terminal = rejection in {
        TaskAttemptRejection.RETRY_EXHAUSTED,
        TaskAttemptRejection.SUBMISSION_INVARIANT_VIOLATION,
    }
    winner = _winner(
        status=TaskStatus.FAILED if terminal else TaskStatus.RUNNING,
        terminal_reason=rejection.value if terminal else None,
    )
    event = make_event(task_id="task-1", stage=PipelineStage.LIFECYCLE).model_copy(
        update={"attempt_number": 1}
    )
    result = TaskAttemptResult(
        applied=False,
        winner=winner,
        rejection=rejection,
        committed_audit_events=(event,) if terminal else (),
    )
    assert result.submission is None


def test_retry_command_requires_a_non_empty_audit_set() -> None:
    with pytest.raises(ValidationError):
        RetryCommand(
            grant=_grant(),
            next_attempt_at=_NOW + dt.timedelta(seconds=10),
            reason=RetryReason.UNCLASSIFIED_ERROR,
            audit_events=(),
        )


def test_retry_result_has_one_unambiguous_decision() -> None:
    RetryResult(decision=RetryDecision.SCHEDULED, winner=_winner())
    assert set(RetryResult.model_fields) == {"decision", "winner"}


def test_retry_class_is_not_part_of_the_contract_surface() -> None:
    assert not hasattr(contracts, "RetryClass")


def test_idempotency_key_boundary_is_preserved_in_submission() -> None:
    context = _context()
    submission = make_submission(
        context,
        envelope=make_envelope(idempotency_key="k" * 200),
    )
    assert len(submission.envelope.idempotency_key) == 200


@pytest.mark.parametrize("damage", ["missing", "stored_digest", "request_digest", "scope"])
async def test_corrupt_submission_fails_atomically_without_a_grant(
    store, memory_state, context, damage: str
) -> None:
    submission = make_submission(context)
    task = await store.create_task(submission=submission)
    if damage == "missing":
        del memory_state.submissions[task.task_id]
        del memory_state.submission_digests[task.task_id]
    elif damage == "stored_digest":
        memory_state.submission_digests[task.task_id] = "f" * 64
    elif damage == "request_digest":
        memory_state.tasks[task.task_id] = task.model_copy(update={"request_digest": "f" * 64})
    else:
        memory_state.tasks[task.task_id] = task.model_copy(update={"actor": "mallory"})
        assert memory_state.submission_digests[task.task_id] == submission_digest(submission)

    before_token = memory_state.next_fencing_token
    result = await store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=task.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id="1" * 32,
        )
    )
    assert result.rejection is TaskAttemptRejection.SUBMISSION_INVARIANT_VIOLATION
    assert result.winner.status is TaskStatus.FAILED
    assert result.winner.version == task.version + 1
    assert result.grant is None
    assert result.submission is None
    assert memory_state.next_fencing_token == before_token
    assert tuple(memory_state.audit_events[task.task_id]) == result.committed_audit_events


bind(globals(), DISPATCH_ATTEMPT_CASES)
