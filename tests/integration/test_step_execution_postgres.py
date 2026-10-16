"""M5 step journal —— PostgreSQL 共享行为与事务失败注入。"""

import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.conftest import lookup_for, make_submission
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET
from tests.fakes.sinks import make_event
from tests.suites.task_store import STEP_EXECUTION_CASES, bind

from xiaowei_agent.contracts import (
    AttemptIntent,
    EvidenceEnvelope,
    ExternalSource,
    PipelineStage,
    StepOutcomeKind,
    StepResultStatus,
    TaskStatus,
    evidence_id,
)
from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityError,
    PersistenceWriteOutcome,
)
from xiaowei_agent.persistence.store import (
    StepAttemptCommand,
    StepCommitCommand,
    TaskAttemptCommand,
    TransitionCommand,
)

bind(globals(), STEP_EXECUTION_CASES)


async def _running_grant(store, plan_store, context):
    task = await store.create_task(submission=make_submission(context))
    attempt = await store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=task.task_id,
            owner="worker-1",
            intent=AttemptIntent.DISPATCH,
            ttl_seconds=30,
            trace_id=context.trace_id,
        )
    )
    assert attempt.grant is not None
    await plan_store.save(task_id=task.task_id, plan=FIXTURE_PLAN, target=FIXTURE_TARGET)
    current = attempt.winner
    for status in (TaskStatus.PLANNING, TaskStatus.RUNNING):
        moved = await store.transition(
            command=TransitionCommand(
                task_id=task.task_id,
                expected_version=current.version,
                to_status=status,
                fencing_token=attempt.grant.fencing_token,
            )
        )
        assert moved.applied
        current = moved.winner
    return attempt.grant, current


def _event(grant, *, event_id: str):
    return make_event(
        stage=PipelineStage.GATEWAY,
        task_id=grant.task_id,
        event_id=event_id,
    ).model_copy(update={"attempt_number": grant.attempt_number, "step_id": "s1"})


def _evidence(grant, *, captured_at):
    return EvidenceEnvelope(
        evidence_id=evidence_id(task_id=grant.task_id, step_id="s1"),
        capability_id=FIXTURE_PLAN.capability_id,
        capability_version=FIXTURE_PLAN.capability_version,
        facts=({"query_id": "q1"},),
        source="starrocks-fake",
        source_kind=ExternalSource.TOOL,
        captured_at=captured_at,
        sampled=False,
        limitations=("deterministic fixture",),
    )


async def test_step_commit_rolls_back_journal_evidence_and_audit_together(
    clean_database: AsyncEngine, store, plan_store, context, clock
) -> None:
    grant, _ = await _running_grant(store, plan_store, context)
    begun = await store.begin_step_attempt(command=StepAttemptCommand(grant=grant, step_id="s1"))
    assert begun.record is not None

    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "CREATE FUNCTION reject_step_audit() RETURNS trigger AS $$ "
                "BEGIN RAISE EXCEPTION 'injected audit failure'; END; "
                "$$ LANGUAGE plpgsql"
            )
        )
        await connection.execute(
            sa.text(
                "CREATE TRIGGER reject_step_audit BEFORE INSERT ON task_audit_events "
                "FOR EACH ROW EXECUTE FUNCTION reject_step_audit()"
            )
        )

    command = StepCommitCommand(
        grant=grant,
        step_id="s1",
        kind=StepOutcomeKind.TOOL_RESULT,
        status=StepResultStatus.OK,
        evidence=_evidence(grant, captured_at=clock()),
        audit_events=(_event(grant, event_id="atomic-step"),),
    )
    try:
        with pytest.raises(PersistenceIntegrityError) as caught:
            await store.commit_step_result(command=command)
        assert caught.value.write_outcome is PersistenceWriteOutcome.ROLLED_BACK
        assert caught.value.__context__ is None
    finally:
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text("DROP TRIGGER IF EXISTS reject_step_audit ON task_audit_events")
            )
            await connection.execute(sa.text("DROP FUNCTION IF EXISTS reject_step_audit()"))

    rows = await store.load_step_executions(task_id=grant.task_id)
    assert len(rows) == 1
    assert rows[0].result_status is None
    async with clean_database.connect() as connection:
        evidence_count = await connection.scalar(
            sa.text("SELECT count(*) FROM task_evidence WHERE task_id = :task_id"),
            {"task_id": grant.task_id},
        )
        audit_count = await connection.scalar(
            sa.text("SELECT count(*) FROM task_audit_events WHERE task_id = :task_id"),
            {"task_id": grant.task_id},
        )
    assert evidence_count == 0
    assert audit_count == 0


async def test_transition_rolls_back_status_and_version_when_audit_fails(
    clean_database: AsyncEngine, store, task
) -> None:
    before = await store.get(lookup=lookup_for(task))
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "CREATE FUNCTION reject_transition_audit() RETURNS trigger AS $$ "
                "BEGIN RAISE EXCEPTION 'injected audit failure'; END; "
                "$$ LANGUAGE plpgsql"
            )
        )
        await connection.execute(
            sa.text(
                "CREATE TRIGGER reject_transition_audit BEFORE INSERT ON task_audit_events "
                "FOR EACH ROW EXECUTE FUNCTION reject_transition_audit()"
            )
        )
    event = make_event(
        stage=PipelineStage.LIFECYCLE,
        task_id=task.task_id,
        event_id="atomic-transition",
    )
    try:
        with pytest.raises(PersistenceIntegrityError) as caught:
            await store.transition(
                command=TransitionCommand(
                    task_id=task.task_id,
                    expected_version=task.version,
                    to_status=TaskStatus.PLANNING,
                    audit_events=(event,),
                )
            )
        assert caught.value.write_outcome is PersistenceWriteOutcome.ROLLED_BACK
        assert caught.value.__context__ is None
    finally:
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text("DROP TRIGGER IF EXISTS reject_transition_audit ON task_audit_events")
            )
            await connection.execute(sa.text("DROP FUNCTION IF EXISTS reject_transition_audit()"))

    after = await store.get(lookup=lookup_for(task))
    assert after == before


async def test_command_and_public_audit_writers_share_one_sequence_lock(
    clean_database: AsyncEngine, independent_stores, task
) -> None:
    command_store, public_store = independent_stores(2)
    command_event = make_event(
        stage=PipelineStage.LIFECYCLE,
        task_id=task.task_id,
        event_id="command-event",
    )
    public_event = make_event(
        stage=PipelineStage.ADMISSION,
        task_id=task.task_id,
        event_id="public-event",
    )
    transition, public_seq = await asyncio.gather(
        command_store.transition(
            command=TransitionCommand(
                task_id=task.task_id,
                expected_version=task.version,
                to_status=TaskStatus.PLANNING,
                audit_events=(command_event,),
            )
        ),
        public_store.record_audit_event(event=public_event),
    )
    assert transition.applied
    assert public_seq in {1, 2}
    async with clean_database.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT seq, event ->> 'event_id' FROM task_audit_events "
                    "WHERE task_id = :task_id ORDER BY seq"
                ),
                {"task_id": task.task_id},
            )
        ).all()
    assert [row[0] for row in rows] == [1, 2]
    assert {row[1] for row in rows} == {"command-event", "public-event"}
