"""M5 Runtime 的提交、执行、查询与 fencing 契约。"""

import datetime as dt
from urllib.parse import quote

import pytest
from pydantic import ValidationError
from tests.fakes.recordings import GOLDEN
from tests.fakes.runner import RunnerHarness
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.runtime import (
    RECOVERY_DRIFT_REASON,
    RetryableTaskError,
    TaskInProgressError,
)
from xiaowei_agent.capabilities import SpecResolutionError
from xiaowei_agent.contracts import (
    AttemptIntent,
    RenderPayload,
    RetryReason,
    TaskAttemptRejection,
    TaskLookup,
    TaskOutcome,
    TaskStatus,
    TaskView,
)
from xiaowei_agent.observability.durable_sink import DurableTraceSink
from xiaowei_agent.persistence import (
    PersistenceIntegrityCategory,
    PersistenceIntegrityError,
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
)
from xiaowei_agent.persistence.store import TaskAttemptCommand, TransitionCommand
from xiaowei_agent.runners.deterministic import DriftError, LifecycleError


def test_task_view_encodes_its_own_query_path() -> None:
    task_id = "task/with?reserved#chars%"
    view = TaskView(
        task_id=task_id,
        status=TaskStatus.CREATED,
        query_path=f"/v1/tasks/{quote(task_id, safe='')}",
    )
    assert view.query_path == "/v1/tasks/task%2Fwith%3Freserved%23chars%25"


def test_task_view_rejects_a_mismatched_path_or_render_status() -> None:
    with pytest.raises(ValidationError, match="query_path"):
        TaskView(
            task_id="task-1",
            status=TaskStatus.CREATED,
            query_path="/v1/tasks/task-2",
        )
    terminal = RenderPayload(
        answer="constant",
        sections=(),
        next_steps=(),
        status=TaskStatus.FAILED,
        refs=(),
    )
    with pytest.raises(ValidationError, match="render status"):
        TaskView(
            task_id="task-1",
            status=TaskStatus.SUCCEEDED,
            render=terminal,
            query_path="/v1/tasks/task-1",
        )


async def test_submit_is_persistent_but_does_not_execute() -> None:
    harness = RuntimeHarness(GOLDEN)
    view = await harness.runtime.submit_task(
        submission=harness.submission("最近30分钟有哪些慢查询")
    )
    harness.task_id = view.task_id
    record = await harness.store.get(lookup=harness.lookup)
    assert view.status is TaskStatus.CREATED
    assert view.render is None
    assert record.status is TaskStatus.CREATED
    assert harness.gateway.invocations == 0
    assert await harness.store.load_step_executions(task_id=view.task_id) == ()


async def test_execute_uses_the_persisted_as_of_and_finishes_the_task() -> None:
    harness = RuntimeHarness(GOLDEN)
    persisted_as_of = dt.datetime(2026, 9, 3, 8, 15, tzinfo=dt.UTC)
    submission = harness.submission("最近30分钟有哪些慢查询", as_of=persisted_as_of)
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None and attempt.submission is not None

    outcome = await harness.runtime.execute_task(grant=attempt.grant, submission=attempt.submission)

    assert outcome.status is TaskStatus.SUCCEEDED
    assert harness.adapter.calls
    assert harness.adapter.calls[0].typed_args["window_end"] == "2026-09-03T08:15:00+00:00"
    assert (await harness.store.get(lookup=harness.lookup)).status is outcome.status


async def test_query_is_pure_and_projects_terminal_state() -> None:
    harness = RuntimeHarness(GOLDEN)
    await harness.handle("最近30分钟有哪些慢查询")
    before = await harness.store.get(lookup=harness.lookup)
    audit_count = len(harness.state.audit_events[harness.task_id])
    evidence_count = len(await harness.ledger.load(task_id=harness.task_id))
    gateway_count = harness.gateway.invocations

    view = await harness.runtime.query_task(lookup=harness.lookup)

    after = await harness.store.get(lookup=harness.lookup)
    assert view.status is TaskStatus.SUCCEEDED
    assert view.render is not None and view.render.status is view.status
    assert after.version == before.version
    assert len(harness.state.audit_events[harness.task_id]) == audit_count
    assert len(await harness.ledger.load(task_id=harness.task_id)) == evidence_count
    assert harness.gateway.invocations == gateway_count


async def test_handle_and_query_share_the_terminal_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xiaowei_agent.application import runtime as runtime_module

    harness = RuntimeHarness(GOLDEN)
    first = await harness.handle("最近30分钟有哪些慢查询")
    original = runtime_module.project_terminal
    calls = 0

    def _counting_projection(**kwargs: object) -> RenderPayload:
        nonlocal calls
        calls += 1
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_module, "project_terminal", _counting_projection)
    queried = await harness.runtime.query_task(lookup=harness.lookup)
    repeated = await harness.handle("最近30分钟有哪些慢查询")

    assert queried.render == first
    assert repeated == first
    assert calls == 2


async def test_query_pending_returns_no_render_and_does_not_emit() -> None:
    harness = RuntimeHarness(GOLDEN, synthetic_write=True)
    await harness.handle("最近30分钟有哪些慢查询")
    event_count = len(harness.sink.events)
    view = await harness.runtime.query_task(lookup=harness.lookup)
    assert view.status is TaskStatus.AWAITING_APPROVAL
    assert view.render is None
    assert len(harness.sink.events) == event_count


async def test_query_persistence_failure_has_no_side_effects() -> None:
    harness = RuntimeHarness(GOLDEN)
    view = await harness.runtime.submit_task(
        submission=harness.submission("最近30分钟有哪些慢查询")
    )
    harness.task_id = view.task_id
    before = harness.state.tasks[view.task_id]
    audit_count = len(harness.state.audit_events.get(view.task_id, ()))
    evidence_count = len(harness.state.evidence.get(view.task_id, {}))
    original = harness.store.get

    async def _unavailable(*, lookup: TaskLookup) -> object:
        raise PersistenceUnavailableError(category=PersistenceUnavailableCategory.CONNECT)

    harness.store.get = _unavailable  # type: ignore[method-assign]
    try:
        with pytest.raises(PersistenceUnavailableError):
            await harness.runtime.query_task(lookup=harness.lookup)
    finally:
        harness.store.get = original  # type: ignore[method-assign]

    assert harness.state.tasks[view.task_id] == before
    assert len(harness.state.audit_events.get(view.task_id, ())) == audit_count
    assert len(harness.state.evidence.get(view.task_id, {})) == evidence_count


async def test_query_scope_mismatch_is_not_found() -> None:
    harness = RuntimeHarness(GOLDEN)
    view = await harness.runtime.submit_task(
        submission=harness.submission("最近30分钟有哪些慢查询")
    )
    with pytest.raises(Exception) as caught:
        await harness.runtime.query_task(
            lookup=TaskLookup(
                task_id=view.task_id,
                tenant_id="other-tenant",
                environment_id=harness.context.environment_id,
            )
        )
    assert type(caught.value).__name__ == "TaskNotFoundError"


async def test_handle_yields_to_a_live_idempotent_execution() -> None:
    harness = RuntimeHarness(GOLDEN)
    submission = harness.submission("最近30分钟有哪些慢查询")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None
    before = await harness.store.get(lookup=harness.lookup)
    runner = _RaisingRunner(AssertionError("runner must not be called"))
    harness.runtime._runner = runner

    with pytest.raises(TaskInProgressError) as caught:
        await harness.runtime.handle(
            envelope=submission.envelope,
            context=submission.context,
            as_of=submission.as_of,
        )

    after = await harness.store.get(lookup=harness.lookup)
    assert caught.value.task_id == view.task_id
    assert after.version == before.version
    assert runner.calls == 0
    assert harness.gateway.invocations == 0


async def test_async_deterministic_rejection_is_persisted_once() -> None:
    harness = RuntimeHarness(GOLDEN)
    submission = harness.submission("帮我重启一下集群")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None and attempt.submission is not None

    outcome = await harness.runtime.execute_task(grant=attempt.grant, submission=attempt.submission)

    record = await harness.store.get(lookup=harness.lookup)
    assert outcome.status is TaskStatus.REJECTED
    assert record.status is TaskStatus.REJECTED
    assert record.task_failure_count == 0
    assert harness.gateway.invocations == 0


async def test_conflicting_stored_plan_fails_before_gateway() -> None:
    harness = RuntimeHarness(GOLDEN)
    compiled = RunnerHarness(GOLDEN)
    submission = harness.submission("最近30分钟有哪些慢查询")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    conflicting = compiled.plan.model_copy(
        update={
            "budget": compiled.plan.budget.model_copy(
                update={"max_tool_calls": compiled.plan.budget.max_tool_calls + 1}
            )
        }
    )
    await harness.plan_store.save(
        task_id=view.task_id,
        plan=conflicting,
        target=compiled.target,
    )
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None and attempt.submission is not None

    outcome = await harness.runtime.execute_task(grant=attempt.grant, submission=attempt.submission)

    assert outcome.status is TaskStatus.FAILED
    assert outcome.terminal_reason == RECOVERY_DRIFT_REASON
    assert harness.gateway.invocations == 0


async def test_recomputed_plan_drift_on_resume_fails_before_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xiaowei_agent.application import runtime as runtime_module

    harness = RuntimeHarness(GOLDEN)
    compiled = RunnerHarness(GOLDEN)
    submission = harness.submission("最近30分钟有哪些慢查询")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    first = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert first.grant is not None
    await harness.plan_store.save(
        task_id=view.task_id,
        plan=compiled.plan,
        target=compiled.target,
    )
    current = await harness.store.get(lookup=harness.lookup)
    planning = await harness.store.transition(
        command=TransitionCommand(
            task_id=view.task_id,
            expected_version=current.version,
            to_status=TaskStatus.PLANNING,
            fencing_token=first.grant.fencing_token,
        )
    )
    assert planning.applied
    harness.clock.advance(seconds=61)
    resumed = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-2",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert resumed.grant is not None and resumed.submission is not None
    changed = compiled.plan.model_copy(
        update={
            "budget": compiled.plan.budget.model_copy(
                update={"max_tool_calls": compiled.plan.budget.max_tool_calls + 1}
            )
        }
    )
    monkeypatch.setattr(runtime_module, "compile_plan", lambda **_: changed)

    outcome = await harness.runtime.execute_task(
        grant=resumed.grant,
        submission=resumed.submission,
    )

    assert outcome.status is TaskStatus.FAILED
    assert outcome.terminal_reason == RECOVERY_DRIFT_REASON
    assert harness.gateway.invocations == 0


async def test_nonempty_new_execution_journal_is_terminal_invariant_failure() -> None:
    harness = RuntimeHarness(GOLDEN)
    submission = harness.submission("最近30分钟有哪些慢查询")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None and attempt.submission is not None
    original = harness.store.load_step_executions

    async def _nonempty(*, task_id: str) -> tuple[object, ...]:
        return (object(),)

    harness.store.load_step_executions = _nonempty  # type: ignore[method-assign]
    try:
        outcome = await harness.runtime.execute_task(
            grant=attempt.grant, submission=attempt.submission
        )
    finally:
        harness.store.load_step_executions = original  # type: ignore[method-assign]

    assert outcome.status is TaskStatus.FAILED
    assert outcome.terminal_reason == RECOVERY_DRIFT_REASON
    assert harness.gateway.invocations == 0


async def test_stale_finisher_cannot_borrow_the_winners_fencing_token() -> None:
    harness = RuntimeHarness(GOLDEN)
    harness.runtime._sink = DurableTraceSink(writer=harness.store, log_sink=harness.sink)
    submission = harness.submission("最近30分钟有哪些慢查询")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    first = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="old-worker",
            ttl_seconds=1,
            trace_id=submission.context.trace_id,
        )
    )
    assert first.grant is not None
    current = await harness.store.get(lookup=harness.lookup)
    moved = await harness.store.transition(
        command=TransitionCommand(
            task_id=view.task_id,
            expected_version=current.version,
            to_status=TaskStatus.PLANNING,
            fencing_token=first.grant.fencing_token,
        )
    )
    assert moved.applied
    running = await harness.store.transition(
        command=TransitionCommand(
            task_id=view.task_id,
            expected_version=moved.winner.version,
            to_status=TaskStatus.RUNNING,
            fencing_token=first.grant.fencing_token,
        )
    )
    assert running.applied
    harness.clock.advance(seconds=2)
    second = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="new-worker",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert second.grant is not None
    before = await harness.store.get(lookup=harness.lookup)
    audit_count = len(harness.state.audit_events.get(view.task_id, ()))

    with pytest.raises(LifecycleError):
        await harness.runtime._finish(
            record=before,
            outcome=TaskOutcome(
                task_id=view.task_id,
                status=TaskStatus.SUCCEEDED,
                terminal_reason=None,
                evidence_refs=(),
                render_ref=None,
            ),
            context=submission.context,
            grant=first.grant,
        )

    after = await harness.store.get(lookup=harness.lookup)
    assert after.status is TaskStatus.RUNNING
    assert after.version == before.version
    assert after.fencing_token == second.grant.fencing_token
    assert len(harness.state.audit_events.get(view.task_id, ())) == audit_count


class _RaisingRunner:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.calls = 0

    async def start(self, *args: object, **kwargs: object) -> TaskOutcome:
        self.calls += 1
        raise self.failure

    async def resume(self, *args: object, **kwargs: object) -> TaskOutcome:
        self.calls += 1
        raise self.failure


async def _execute_with_failure(failure: Exception) -> tuple[RuntimeHarness, _RaisingRunner]:
    harness = RuntimeHarness(GOLDEN)
    submission = harness.submission("最近30分钟有哪些慢查询")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None and attempt.submission is not None
    runner = _RaisingRunner(failure)
    harness.runtime._runner = runner
    await harness.runtime.execute_task(grant=attempt.grant, submission=attempt.submission)
    return harness, runner


@pytest.mark.parametrize("failure", [PermissionError(), SpecResolutionError("constant")])
async def test_deterministic_execution_rejection_is_terminal_once(
    failure: Exception,
) -> None:
    harness, runner = await _execute_with_failure(failure)
    record = await harness.store.get(lookup=harness.lookup)
    assert runner.calls == 1
    assert record.status is TaskStatus.REJECTED
    assert record.task_failure_count == 0


@pytest.mark.parametrize("failure", [DriftError("constant"), RuntimeError("ordinary failure")])
async def test_drift_and_unclassified_failures_take_different_paths(
    failure: Exception,
) -> None:
    harness = RuntimeHarness(GOLDEN)
    submission = harness.submission("最近30分钟有哪些慢查询")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None and attempt.submission is not None
    harness.runtime._runner = _RaisingRunner(failure)

    if isinstance(failure, DriftError):
        outcome = await harness.runtime.execute_task(
            grant=attempt.grant, submission=attempt.submission
        )
        assert outcome.status is TaskStatus.FAILED
        assert outcome.terminal_reason == RECOVERY_DRIFT_REASON
    else:
        with pytest.raises(RetryableTaskError) as caught:
            await harness.runtime.execute_task(grant=attempt.grant, submission=attempt.submission)
        assert caught.value.task_id == view.task_id
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert "ordinary failure" not in str(caught.value)
        assert "ordinary failure" not in repr(caught.value)


@pytest.mark.parametrize(
    "failure",
    [
        LookupError("adapter text"),
        PersistenceUnavailableError(category=PersistenceUnavailableCategory.CONNECT),
        PersistenceIntegrityError(category=PersistenceIntegrityCategory.SCHEMA),
    ],
)
async def test_fail_stop_and_persistence_errors_do_not_change_the_task(
    failure: Exception,
) -> None:
    harness = RuntimeHarness(GOLDEN)
    submission = harness.submission("最近30分钟有哪些慢查询")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None and attempt.submission is not None
    harness.runtime._runner = _RaisingRunner(failure)
    before = await harness.store.get(lookup=harness.lookup)
    audit_count = len(harness.state.audit_events.get(view.task_id, ()))
    evidence_count = len(await harness.ledger.load(task_id=view.task_id))

    with pytest.raises(type(failure)):
        await harness.runtime.execute_task(grant=attempt.grant, submission=attempt.submission)

    after = await harness.store.get(lookup=harness.lookup)
    assert after == before
    assert len(harness.state.audit_events.get(view.task_id, ())) == audit_count
    assert len(await harness.ledger.load(task_id=view.task_id)) == evidence_count


def test_task_errors_never_render_task_ids_or_original_failures() -> None:
    in_progress = TaskInProgressError(task_id="task-secret")
    retryable = RetryableTaskError(
        task_id="task-secret",
        reason=RetryReason.UNCLASSIFIED_ERROR,
    )
    for error in (in_progress, retryable):
        assert "task-secret" not in str(error)
        assert "task-secret" not in repr(error)


def test_task_attempt_rejection_import_is_used_as_a_closed_set_guard() -> None:
    assert TaskAttemptRejection.LIVE_LEASE.value == "live_lease"
