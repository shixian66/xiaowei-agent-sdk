"""DeterministicStepRunner：生命周期宿主。

Runner 拥有租约、CAS 推进与暂停，但**不拥有领域安全规则**：分类、策略、SQL 与
审批一律经 ``admit_step``；证据一律经 ledger。
"""

import asyncio

import pytest
from tests.fakes.recordings import (
    EMPTY_IN_SCOPE_BUT_TRAFFIC_ELSEWHERE,
    EMPTY_WITH_TRAFFIC,
    GOLDEN,
    MALFORMED,
    TIMEOUT,
)
from tests.fakes.runner import RunnerHarness

from xiaowei_agent.capabilities.specs import OP_COUNT, OP_LIST
from xiaowei_agent.contracts import (
    AttemptIntent,
    PipelineStage,
    StageOutcome,
    StepAttemptDecision,
    StepCommitRejection,
    StepOutcomeKind,
    StepResultStatus,
    TaskStatus,
    ToolCallStatus,
    TraceEvent,
    TransitionRejection,
)
from xiaowei_agent.persistence.plans import PlanNotFoundError
from xiaowei_agent.persistence.store import (
    StepAttemptCommand,
    StepAttemptResult,
    StepCommitCommand,
    StepCommitResult,
    TaskAttemptCommand,
    TaskAttemptGrant,
    TransitionCommand,
)
from xiaowei_agent.runners.deterministic import (
    _STEP_RESULT_STATUS,
    LifecycleError,
    StepJournalInvariantError,
)


async def _begin(harness: RunnerHarness) -> TaskAttemptGrant:
    await harness.ensure_task()
    result = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=harness.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=harness.context.trace_id,
        )
    )
    assert result.grant is not None
    return result.grant


async def _prepare_status(harness: RunnerHarness, *statuses: TaskStatus) -> TaskAttemptGrant:
    """保存计划并用一个真实 grant 把任务推进到指定崩溃点。"""
    grant = await _begin(harness)
    await harness.plan_store.save(task_id=harness.task_id, plan=harness.plan, target=harness.target)
    record = await harness.store.get(lookup=harness.lookup)
    for status in statuses:
        result = await harness.store.transition(
            command=TransitionCommand(
                task_id=harness.task_id,
                expected_version=record.version,
                to_status=status,
                fencing_token=grant.fencing_token,
            )
        )
        assert result.applied
        record = result.winner
    return grant


def _step_event(
    harness: RunnerHarness,
    grant: TaskAttemptGrant,
    *,
    outcome: StageOutcome = StageOutcome.FAILED,
) -> TraceEvent:
    return TraceEvent(
        event_id=f"test:{harness.task_id}:{grant.attempt_number}",
        trace_id=harness.context.trace_id,
        task_id=harness.task_id,
        stage=PipelineStage.GATEWAY,
        outcome=outcome,
        occurred_at=harness.clock(),
        capability_id=harness.plan.capability_id,
        step_id="s1",
        policy_revision=harness.context.policy_revision,
        attempt_number=grant.attempt_number,
        error=None,
        detail={},
    )


async def test_golden_run_executes_only_the_first_step() -> None:
    """s1 有命中时，可选分支不执行——预算内的分支不是"总是跑"。"""
    harness = RunnerHarness(GOLDEN)
    outcome = await harness.start()
    assert [call.operation for call in harness.adapter.calls] == [OP_LIST]
    assert outcome.status is TaskStatus.SUCCEEDED
    assert len(outcome.evidence_refs) == 1


async def test_runner_validates_and_renews_only_the_supplied_grant() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    assert harness.grant is not None
    assert harness.store.renewals == [
        (
            harness.grant.task_id,
            harness.grant.lease.owner,
            harness.grant.fencing_token,
            60,
        )
    ]


async def test_expired_grant_is_rejected_before_plan_or_gateway_work() -> None:
    harness = RunnerHarness(GOLDEN)
    grant = await _begin(harness)
    harness.clock.advance(seconds=60)

    with pytest.raises(LifecycleError, match="no longer current"):
        await harness.runner.start(
            grant,
            plan=harness.plan,
            target=harness.target,
            context=harness.context,
        )

    assert harness.gateway.invocations == 0
    assert harness.store.transitions == []
    with pytest.raises(PlanNotFoundError):
        await harness.plan_store.load(task_id=harness.task_id)


async def test_heartbeat_loss_cancels_in_flight_gateway_without_committing() -> None:
    harness = RunnerHarness(GOLDEN)
    gateway_entered = asyncio.Event()
    never_finishes = asyncio.Event()

    class _BlockingGateway:
        cancelled = False

        async def invoke(self, *args: object, **kwargs: object) -> object:
            gateway_entered.set()
            try:
                await never_finishes.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

    blocking = _BlockingGateway()
    original_renew = harness.store.renew_lease
    renewals: list[tuple[str, str, int, int]] = []

    async def _lose_after_validation(
        *, task_id: str, owner: str, fencing_token: int, ttl_seconds: int
    ) -> object:
        renewals.append((task_id, owner, fencing_token, ttl_seconds))
        if len(renewals) == 1:
            return await original_renew(
                task_id=task_id,
                owner=owner,
                fencing_token=fencing_token,
                ttl_seconds=ttl_seconds,
            )
        return None

    async def _wake_heartbeat_after_gateway_starts(_: float) -> None:
        await gateway_entered.wait()

    harness.store.renew_lease = _lose_after_validation  # type: ignore[method-assign]
    harness.runner._gateway = blocking  # type: ignore[assignment]
    harness.runner._sleep = _wake_heartbeat_after_gateway_starts

    with pytest.raises(LifecycleError, match="heartbeat was lost"):
        await harness.start()

    assert blocking.cancelled
    assert len(renewals) == 2
    assert {item[1:3] for item in renewals} == {("worker-1", harness.grant.fencing_token)}
    journal = await harness.store.load_step_executions(task_id=harness.task_id)
    assert len(journal) == 1
    assert journal[0].result_status is None
    assert await harness.ledger.load(task_id=harness.task_id) == ()


async def test_cancelling_the_run_also_cancels_gateway_without_late_commit() -> None:
    harness = RunnerHarness(GOLDEN)
    gateway_entered = asyncio.Event()
    never_finishes = asyncio.Event()

    class _BlockingGateway:
        cancelled = False

        async def invoke(self, *args: object, **kwargs: object) -> object:
            gateway_entered.set()
            try:
                await never_finishes.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

    blocking = _BlockingGateway()
    harness.runner._gateway = blocking  # type: ignore[assignment]
    running = asyncio.create_task(harness.start())
    await gateway_entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert blocking.cancelled
    journal = await harness.store.load_step_executions(task_id=harness.task_id)
    assert len(journal) == 1
    assert journal[0].result_status is None
    assert await harness.ledger.load(task_id=harness.task_id) == ()


@pytest.mark.parametrize(
    ("recording", "s2_runs"),
    [(GOLDEN, False), (EMPTY_WITH_TRAFFIC, True)],
    ids=["s1_has_rows", "s1_empty"],
)
async def test_optional_branch_runs_only_when_s1_returns_no_rows(
    recording: object, s2_runs: bool
) -> None:
    harness = RunnerHarness(recording)
    await harness.start()
    operations = [call.operation for call in harness.adapter.calls]
    assert (OP_COUNT in operations) is s2_runs


async def test_condition_is_evaluated_from_the_ledger_not_local_state() -> None:
    """条件求值必须读 ledger。

    反例更强：把 ledger 换成永远返回空的实现，s2 必须**被执行**（因为 s1 的行数
    读回来是 0）。若 Runner 用的是本地变量，s2 会被跳过，这条就转红。
    """
    harness = RunnerHarness(GOLDEN, blind_ledger=True)
    await harness.start()
    assert OP_COUNT in [call.operation for call in harness.adapter.calls]


async def test_every_evidence_ref_in_the_outcome_resolves_in_the_ledger() -> None:
    harness = RunnerHarness(EMPTY_WITH_TRAFFIC)
    outcome = await harness.start()
    assert outcome.evidence_refs
    for ref in outcome.evidence_refs:
        await harness.ledger.get(task_id=harness.task_id, evidence_id=ref)


async def test_evidence_is_written_for_every_executed_step() -> None:
    harness = RunnerHarness(EMPTY_WITH_TRAFFIC)
    await harness.start()
    stored = await harness.ledger.load(task_id=harness.task_id)
    assert [e.evidence_id for e in stored] == [
        f"{harness.task_id}:s1",
        f"{harness.task_id}:s2",
    ]


async def test_runner_leaves_the_terminal_decision_to_the_runtime() -> None:
    """Runner 不写终态。

    终态由 Runtime 依 Answerability 的结构化结论确定性判定（ARCHITECTURE §4.3）。
    Runner 若先写 SUCCEEDED，终态保护会让 Runtime 再也无法降级为 indeterminate——
    "空证据不得渲染成成功"就在存储层被堵死了。
    """
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    assert (await harness.store.get(lookup=harness.lookup)).status is TaskStatus.RUNNING


async def test_plan_and_target_are_saved_before_execution() -> None:
    """resume 要重算指纹，就必须先拿得回当初那份计划。"""
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    stored = await harness.plan_store.load(task_id=harness.task_id)
    assert stored.plan == harness.plan
    assert stored.target == harness.target


async def test_created_task_with_an_identical_stored_plan_can_start() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.ensure_task()
    await harness.plan_store.save(task_id=harness.task_id, plan=harness.plan, target=harness.target)
    outcome = await harness.start()
    assert outcome.status is TaskStatus.SUCCEEDED


async def test_planning_task_must_resume_and_advances_exactly_one_edge() -> None:
    harness = RunnerHarness(GOLDEN)
    await _prepare_status(harness, TaskStatus.PLANNING)
    harness.clock.advance(seconds=61)
    grant = await _begin(harness)
    harness.store.transitions.clear()

    outcome = await harness.runner.resume(
        grant, plan=harness.plan, context=harness.context, target=harness.target
    )

    assert outcome.status is TaskStatus.SUCCEEDED
    assert [item.to_status for item in harness.store.transitions] == [TaskStatus.RUNNING]


async def test_planning_task_rejects_start_as_an_illegal_transition() -> None:
    harness = RunnerHarness(GOLDEN)
    await _prepare_status(harness, TaskStatus.PLANNING)
    harness.clock.advance(seconds=61)
    grant = await _begin(harness)
    harness.store.transitions.clear()

    with pytest.raises(LifecycleError) as error:
        await harness.runner.start(
            grant,
            plan=harness.plan,
            target=harness.target,
            context=harness.context,
        )

    assert error.value.rejection is TransitionRejection.ILLEGAL_TRANSITION
    assert harness.gateway.invocations == 0


async def test_running_task_resume_does_not_repeat_a_status_transition() -> None:
    harness = RunnerHarness(GOLDEN)
    await _prepare_status(harness, TaskStatus.PLANNING, TaskStatus.RUNNING)
    harness.clock.advance(seconds=61)
    grant = await _begin(harness)
    harness.store.transitions.clear()

    outcome = await harness.runner.resume(
        grant, plan=harness.plan, context=harness.context, target=harness.target
    )

    assert outcome.status is TaskStatus.SUCCEEDED
    assert harness.store.transitions == []


async def test_budget_exhaustion_produces_a_structured_failure() -> None:
    harness = RunnerHarness(EMPTY_WITH_TRAFFIC, max_tool_calls=1)
    outcome = await harness.start()
    assert outcome.status is TaskStatus.FAILED
    assert outcome.terminal_reason == "budget.tool_calls_exhausted"
    assert len(harness.adapter.calls) == 1


async def test_budget_exhaustion_is_rebuilt_from_the_durable_journal() -> None:
    harness = RunnerHarness(EMPTY_WITH_TRAFFIC, max_tool_calls=1)
    first = await harness.start()
    assert first.terminal_reason == "budget.tool_calls_exhausted"
    harness.clock.advance(seconds=61)
    grant = await _begin(harness)
    harness.reset_call_counters()

    resumed = await harness.runner.resume(
        grant, plan=harness.plan, context=harness.context, target=harness.target
    )

    assert resumed.status is TaskStatus.FAILED
    assert resumed.terminal_reason == "budget.tool_calls_exhausted"
    assert harness.gateway.invocations == 0


async def test_resume_rejects_a_recomputed_plan_drift_before_gateway() -> None:
    harness = RunnerHarness(GOLDEN)
    await _prepare_status(harness, TaskStatus.PLANNING)
    harness.clock.advance(seconds=61)
    grant = await _begin(harness)
    changed = harness.plan.model_copy(
        update={
            "budget": harness.plan.budget.model_copy(
                update={"max_tool_calls": harness.plan.budget.max_tool_calls + 1}
            )
        }
    )

    from xiaowei_agent.runners.deterministic import DriftError

    with pytest.raises(DriftError, match="plan_hash"):
        await harness.runner.resume(
            grant,
            plan=changed,
            context=harness.context,
            target=harness.target,
        )

    assert harness.gateway.invocations == 0


async def test_unknown_step_stops_as_recovery_drift_without_gateway_call() -> None:
    harness = RunnerHarness(GOLDEN)
    original = harness.store.begin_step_attempt

    async def _unknown(*, command: StepAttemptCommand) -> StepAttemptResult:
        current = await harness.store.get(lookup=harness.lookup)
        return StepAttemptResult(
            decision=StepAttemptDecision.UNKNOWN_STEP,
            winner=current,
            record=None,
            attempts_used=0,
        )

    harness.store.begin_step_attempt = _unknown  # type: ignore[method-assign]
    outcome = await harness.start()
    harness.store.begin_step_attempt = original  # type: ignore[method-assign]

    assert outcome.status is TaskStatus.FAILED
    assert outcome.terminal_reason == "recovery_drift"
    assert harness.gateway.invocations == 0


@pytest.mark.parametrize(
    "decision",
    [StepAttemptDecision.STALE_FENCING, StepAttemptDecision.NOT_RUNNABLE],
)
async def test_step_begin_ownership_rejection_never_calls_gateway(
    decision: StepAttemptDecision,
) -> None:
    harness = RunnerHarness(GOLDEN)

    async def _reject(*, command: StepAttemptCommand) -> StepAttemptResult:
        current = await harness.store.get(lookup=harness.lookup)
        return StepAttemptResult(
            decision=decision,
            winner=current,
            record=None,
            attempts_used=0,
        )

    harness.store.begin_step_attempt = _reject  # type: ignore[method-assign]
    with pytest.raises(LifecycleError):
        await harness.start()
    assert harness.gateway.invocations == 0


@pytest.mark.parametrize(
    ("rejection", "error_type"),
    [
        (StepCommitRejection.STALE_FENCING, LifecycleError),
        (StepCommitRejection.NOT_RUNNABLE, LifecycleError),
        (StepCommitRejection.NO_ATTEMPT_IN_FLIGHT, StepJournalInvariantError),
        (StepCommitRejection.ALREADY_COMMITTED_DIFFERENT, StepJournalInvariantError),
    ],
)
async def test_step_commit_rejections_have_closed_runner_semantics(
    rejection: StepCommitRejection, error_type: type[Exception]
) -> None:
    harness = RunnerHarness(GOLDEN)

    async def _reject(*, command: StepCommitCommand) -> StepCommitResult:
        current = await harness.store.get(lookup=harness.lookup)
        journal = await harness.store.load_step_executions(task_id=harness.task_id)
        return StepCommitResult(
            committed=False,
            winner=current,
            record=None if rejection is StepCommitRejection.NO_ATTEMPT_IN_FLIGHT else journal[0],
            rejection=rejection,
        )

    harness.store.commit_step_result = _reject  # type: ignore[method-assign]
    with pytest.raises(error_type):
        await harness.start()

    assert harness.gateway.invocations == 1
    journal = await harness.store.load_step_executions(task_id=harness.task_id)
    assert len(journal) == 1
    assert journal[0].result_status is None
    assert await harness.ledger.load(task_id=harness.task_id) == ()


async def test_failed_step_context_is_rebuilt_without_replaying_the_gateway() -> None:
    harness = RunnerHarness(GOLDEN)
    grant = await _prepare_status(harness, TaskStatus.PLANNING, TaskStatus.RUNNING)
    begun = await harness.store.begin_step_attempt(
        command=StepAttemptCommand(grant=grant, step_id="s1")
    )
    assert begun.decision is StepAttemptDecision.PROCEED
    committed = await harness.store.commit_step_result(
        command=StepCommitCommand(
            grant=grant,
            step_id="s1",
            kind=StepOutcomeKind.MALFORMED_ADAPTER,
            status=StepResultStatus.FAILED,
            evidence=None,
            audit_events=(_step_event(harness, grant),),
        )
    )
    assert committed.committed
    harness.clock.advance(seconds=61)
    resumed_grant = await _begin(harness)
    harness.reset_call_counters()

    outcome = await harness.runner.resume(
        resumed_grant,
        plan=harness.plan,
        context=harness.context,
        target=harness.target,
    )

    assert outcome.status is TaskStatus.INDETERMINATE
    assert harness.gateway.invocations == 0
    assert [
        item.step_id for item in await harness.store.load_step_executions(task_id=harness.task_id)
    ] == ["s1"]


async def test_ok_step_is_adopted_after_restart_without_replay() -> None:
    harness = RunnerHarness(GOLDEN)
    first = await harness.start()
    assert first.status is TaskStatus.SUCCEEDED
    harness.clock.advance(seconds=61)
    grant = await _begin(harness)
    harness.reset_call_counters()

    resumed = await harness.runner.resume(
        grant, plan=harness.plan, context=harness.context, target=harness.target
    )

    assert resumed.status is TaskStatus.SUCCEEDED
    assert harness.gateway.invocations == 0


async def test_start_rejects_a_nonempty_step_journal() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    harness.clock.advance(seconds=61)
    grant = await _begin(harness)
    harness.reset_call_counters()

    with pytest.raises(StepJournalInvariantError, match="already has a step journal"):
        await harness.runner.start(
            grant,
            plan=harness.plan,
            target=harness.target,
            context=harness.context,
        )

    assert harness.gateway.invocations == 0


@pytest.mark.parametrize("recording", [TIMEOUT, MALFORMED], ids=["timeout", "malformed"])
async def test_tool_failures_never_look_like_empty_success(recording: object) -> None:
    """失败不得表现为"取到零行"，也不得触发可选分支。

    EVIDENCE_ROW_COUNT_BELOW 在数值上无法区分"取到零行"与"根本没取到"，而这两者
    含义相反。s1 失败后仍去跑 s2，会让一次取数故障看起来像是在"确认范围内有没有
    流量"——正是本闭环要避免的、自信但错误的结论。
    """
    harness = RunnerHarness(recording)
    outcome = await harness.start()
    assert outcome.status is TaskStatus.INDETERMINATE
    assert [call.operation for call in harness.adapter.calls] == [OP_LIST]
    for envelope in await harness.ledger.load(task_id=harness.task_id):
        assert envelope.facts == ()


@pytest.mark.parametrize(
    ("recording", "expected_status", "expected_kind", "evidence_count"),
    [
        (TIMEOUT, StepResultStatus.TIMEOUT, StepOutcomeKind.TOOL_RESULT, 1),
        (MALFORMED, StepResultStatus.FAILED, StepOutcomeKind.MALFORMED_ADAPTER, 0),
    ],
    ids=["timeout_is_durable", "malformed_has_no_evidence"],
)
async def test_failure_kind_is_persisted_without_guessing(
    recording: object,
    expected_status: StepResultStatus,
    expected_kind: StepOutcomeKind,
    evidence_count: int,
) -> None:
    harness = RunnerHarness(recording)
    await harness.start()
    journal = await harness.store.load_step_executions(task_id=harness.task_id)
    assert len(journal) == 1
    assert journal[0].result_status is expected_status
    assert journal[0].kind is expected_kind
    assert len(await harness.ledger.load(task_id=harness.task_id)) == evidence_count


def test_every_tool_status_has_one_step_status() -> None:
    assert dict(_STEP_RESULT_STATUS) == {
        ToolCallStatus.OK: StepResultStatus.OK,
        ToolCallStatus.ERROR: StepResultStatus.FAILED,
        ToolCallStatus.TIMEOUT: StepResultStatus.TIMEOUT,
        ToolCallStatus.INDETERMINATE: StepResultStatus.FAILED,
    }


async def test_runner_does_not_misclassify_an_internal_type_error_as_malformed() -> None:
    harness = RunnerHarness(GOLDEN)

    class _BrokenGateway:
        calls = 0

        async def invoke(self, *args: object, **kwargs: object) -> object:
            self.calls += 1
            raise TypeError("internal runner dependency failed")

    broken = _BrokenGateway()
    harness.runner._gateway = broken  # type: ignore[assignment]

    with pytest.raises(TypeError, match="internal runner dependency failed"):
        await harness.start()

    assert broken.calls == 1
    journal = await harness.store.load_step_executions(task_id=harness.task_id)
    assert len(journal) == 1
    assert journal[0].result_status is None
    assert await harness.ledger.load(task_id=harness.task_id) == ()


async def test_empty_scope_recording_still_produces_evidence() -> None:
    """B1 语料：范围内无审计数据时，两步都执行且都留下证据。"""
    harness = RunnerHarness(EMPTY_IN_SCOPE_BUT_TRAFFIC_ELSEWHERE, database="sales")
    outcome = await harness.start()
    assert [call.operation for call in harness.adapter.calls] == [OP_LIST, OP_COUNT]
    assert len(outcome.evidence_refs) == 2


async def test_repeated_runs_issue_identical_tool_calls() -> None:
    """固定输入 → 逐字节相同的 ToolCall 与 tool_call_hash。"""
    from xiaowei_agent.planning import compute_tool_call_hash

    first = RunnerHarness(GOLDEN)
    second = RunnerHarness(GOLDEN)
    await first.start()
    await second.start()
    assert [compute_tool_call_hash(c) for c in first.adapter.calls] == [
        compute_tool_call_hash(c) for c in second.adapter.calls
    ]


async def test_every_transition_carries_expected_version_and_token() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    assert harness.store.transitions
    for command in harness.store.transitions:
        assert command.expected_version is not None
        assert command.fencing_token is not None


async def test_runner_adopts_the_store_winner_on_cas_failure() -> None:
    """CAS 失败是正常并发结果：必须采纳 winner，不得用本地旧对象继续。"""
    harness = RunnerHarness(GOLDEN)
    await harness.ensure_task()
    await harness.store.transition(
        command=TransitionCommand(
            task_id=harness.task_id,
            expected_version=(await harness.store.get(lookup=harness.lookup)).version,
            to_status=TaskStatus.CANCELED,
        )
    )
    with pytest.raises(RuntimeError):
        await harness.start()
    assert harness.adapter.call_count == 0


async def test_rejected_transition_stops_the_run_with_zero_calls() -> None:
    """CAS 被拒时必须停下，不得用本地旧对象继续推进。

    这条与"终态任务拿不到租约"是**两回事**：这里租约拿得到（任务并非终态），
    被拒的是状态迁移本身。少了这条用例，撤掉 ``result.applied`` 检查的变异会
    全绿——租约那条先挡住了，``applied`` 从未单独承重。
    """
    harness = RunnerHarness(GOLDEN)
    await harness.ensure_task()
    # 推到 AWAITING_APPROVAL：非终态（租约可取），但 → PLANNING 不是合法迁移。
    record = await harness.store.get(lookup=harness.lookup)
    for status in (TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL):
        result = await harness.store.transition(
            command=TransitionCommand(
                task_id=harness.task_id,
                expected_version=record.version,
                to_status=status,
            )
        )
        assert result.applied
        record = result.winner

    with pytest.raises(RuntimeError):
        await harness.start()
    assert harness.adapter.call_count == 0
    assert harness.gateway.invocations == 0


async def test_terminal_task_cannot_be_advanced() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.drive_to(TaskStatus.CANCELED)
    with pytest.raises(RuntimeError):
        await harness.start()
    assert harness.adapter.call_count == 0


async def test_a_task_held_by_another_worker_is_not_started() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.ensure_task()
    grant = await harness.store.acquire_lease(
        task_id=harness.task_id, owner="other-worker", ttl_seconds=60
    )
    assert grant is not None
    with pytest.raises(RuntimeError):
        await harness.start()
    assert harness.adapter.call_count == 0


async def test_gateway_results_reach_evidence_unchanged_in_shape() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    stored = await harness.ledger.load(task_id=harness.task_id)
    assert len(stored[0].facts) == 3
    assert harness.gateway.results[0].status is ToolCallStatus.OK
