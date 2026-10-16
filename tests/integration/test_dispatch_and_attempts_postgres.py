"""M5 分派、任务尝试与重试 —— PostgreSQL 行锁、事务与并发证明。"""

import asyncio
import datetime as dt
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.conftest import make_submission
from tests.suites.task_store import (
    DISPATCH_ATTEMPT_CASES,
    _attempt_command,
    _retry_command,
    bind,
)

from xiaowei_agent.contracts import AttemptIntent, TaskAttemptRejection, TaskStatus, TraceEvent
from xiaowei_agent.persistence.rows import load_contract
from xiaowei_agent.persistence.schema import TASK_AUDIT_EVENTS, TASK_SUBMISSIONS, TASKS
from xiaowei_agent.persistence.store import TaskAttemptCommand

pytestmark = pytest.mark.security

bind(globals(), DISPATCH_ATTEMPT_CASES)


async def test_concurrent_attempt_claim_has_exactly_one_grant(
    store: Any, context: Any, independent_stores: Any
) -> None:
    task = await store.create_task(submission=make_submission(context))
    writers = independent_stores(8)
    results = await asyncio.gather(
        *(
            writer.begin_task_attempt(
                command=TaskAttemptCommand(
                    task_id=task.task_id,
                    intent=AttemptIntent.DISPATCH,
                    owner=f"worker-{index}",
                    ttl_seconds=60,
                    trace_id="1" * 32,
                )
            )
            for index, writer in enumerate(writers)
        )
    )
    winners = [result for result in results if result.applied]
    losers = [result for result in results if not result.applied]
    assert len(winners) == 1
    assert {result.rejection for result in losers} == {TaskAttemptRejection.LIVE_LEASE}
    assert {result.winner for result in losers} == {winners[0].winner}


@pytest.mark.parametrize("damage", ["missing", "stored_digest", "request_digest", "scope"])
async def test_submission_corruption_terminalizes_with_the_same_transaction_audit(
    store: Any,
    context: Any,
    clean_database: AsyncEngine,
    damage: str,
) -> None:
    task = await store.create_task(submission=make_submission(context))
    async with clean_database.begin() as connection:
        if damage == "missing":
            await connection.execute(
                sa.delete(TASK_SUBMISSIONS).where(TASK_SUBMISSIONS.c.task_id == task.task_id)
            )
        elif damage == "stored_digest":
            await connection.execute(
                sa.update(TASK_SUBMISSIONS)
                .where(TASK_SUBMISSIONS.c.task_id == task.task_id)
                .values(submission_digest="f" * 64)
            )
        elif damage == "request_digest":
            await connection.execute(
                sa.update(TASKS)
                .where(TASKS.c.task_id == task.task_id)
                .values(request_digest="f" * 64)
            )
        else:
            await connection.execute(
                sa.update(TASKS).where(TASKS.c.task_id == task.task_id).values(actor="mallory")
            )

    result = await store.begin_task_attempt(command=_attempt_command(task.task_id))
    assert result.rejection is TaskAttemptRejection.SUBMISSION_INVARIANT_VIOLATION
    assert result.winner.status is TaskStatus.FAILED
    assert result.winner.version == task.version + 1
    assert result.grant is None
    assert len(result.committed_audit_events) == 1
    async with clean_database.connect() as connection:
        stored = (
            (
                await connection.execute(
                    sa.select(TASK_AUDIT_EVENTS.c.event).where(
                        TASK_AUDIT_EVENTS.c.task_id == task.task_id
                    )
                )
            )
            .mappings()
            .all()
        )
    assert tuple(load_contract(TraceEvent, row["event"]) for row in stored) == (
        result.committed_audit_events
    )


async def test_concurrent_retry_exhaustion_has_one_terminalizing_winner(
    store: Any,
    task: Any,
    clock: Any,
    independent_stores: Any,
    clean_database: AsyncEngine,
) -> None:
    for attempt_number in range(1, 4):
        started = await store.begin_task_attempt(
            command=_attempt_command(task.task_id, owner=f"worker-{attempt_number}")
        )
        assert started.grant is not None
        await store.schedule_retry(
            command=_retry_command(
                started.grant,
                next_attempt_at=clock() + dt.timedelta(seconds=1),
                event_id=f"retry-{attempt_number}",
            )
        )
        clock.advance(seconds=1)

    writers = independent_stores(8)
    results = await asyncio.gather(
        *(
            writer.begin_task_attempt(
                command=_attempt_command(task.task_id, owner=f"final-{index}")
            )
            for index, writer in enumerate(writers)
        )
    )
    exhausted = [
        result for result in results if result.rejection is TaskAttemptRejection.RETRY_EXHAUSTED
    ]
    protected = [
        result for result in results if result.rejection is TaskAttemptRejection.TERMINAL_PROTECTED
    ]
    assert len(exhausted) == 1
    assert len(protected) == 7
    assert {result.winner for result in results} == {exhausted[0].winner}

    async with clean_database.connect() as connection:
        audit_count = (
            await connection.execute(
                sa.select(sa.func.count())
                .select_from(TASK_AUDIT_EVENTS)
                .where(TASK_AUDIT_EVENTS.c.task_id == task.task_id)
            )
        ).scalar_one()
    assert audit_count == 4
