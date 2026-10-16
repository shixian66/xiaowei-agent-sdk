"""PostgreSQL 中 task 与 submission 的原子持久化。"""

import datetime as dt
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.conftest import make_envelope, make_submission

from xiaowei_agent.contracts import RequestContext, RequestEnvelope
from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityError,
    PersistenceWriteOutcome,
)
from xiaowei_agent.persistence.rows import load_contract
from xiaowei_agent.persistence.schema import TASK_SUBMISSIONS, TASKS


async def test_submission_round_trips_with_the_original_as_of(
    clean_database: AsyncEngine, store: Any, context: Any
) -> None:
    as_of = dt.datetime(2026, 9, 5, 18, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=7)))
    submission = make_submission(context, as_of=as_of)
    task = await store.create_task(submission=submission)

    async with clean_database.connect() as connection:
        row = (
            (
                await connection.execute(
                    sa.select(TASK_SUBMISSIONS).where(TASK_SUBMISSIONS.c.task_id == task.task_id)
                )
            )
            .mappings()
            .one()
        )

    assert load_contract(RequestEnvelope, row["envelope"]) == submission.envelope
    assert load_contract(RequestContext, row["context"]) == submission.context
    assert row["as_of"] == as_of


async def test_idempotent_retry_does_not_overwrite_the_first_submission(
    clean_database: AsyncEngine, store: Any, context: Any
) -> None:
    first_submission = make_submission(context)
    retry_submission = make_submission(
        context.model_copy(update={"trace_id": "1" * 32}),
        envelope=make_envelope(request_id="retry-request"),
        as_of=dt.datetime(2026, 9, 5, 12, 30, tzinfo=dt.UTC),
    )
    first = await store.create_task(submission=first_submission)
    retry = await store.create_task(submission=retry_submission)

    async with clean_database.connect() as connection:
        row = (
            (
                await connection.execute(
                    sa.select(TASK_SUBMISSIONS).where(TASK_SUBMISSIONS.c.task_id == first.task_id)
                )
            )
            .mappings()
            .one()
        )

    assert retry.task_id == first.task_id
    assert load_contract(RequestEnvelope, row["envelope"]) == first_submission.envelope
    assert load_contract(RequestContext, row["context"]) == first_submission.context
    assert row["as_of"] == first_submission.as_of


async def test_submission_insert_failure_rolls_back_the_task_row(
    clean_database: AsyncEngine, store: Any, context: Any
) -> None:
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "CREATE FUNCTION reject_task_submission() RETURNS trigger AS $$ "
                "BEGIN RAISE EXCEPTION 'REJECT_TEST_SUBMISSION'; END; $$ LANGUAGE plpgsql"
            )
        )
        await connection.execute(
            sa.text(
                "CREATE TRIGGER reject_task_submission_before_insert "
                "BEFORE INSERT ON task_submissions FOR EACH ROW "
                "EXECUTE FUNCTION reject_task_submission()"
            )
        )
    try:
        with pytest.raises(PersistenceIntegrityError) as caught:
            await store.create_task(
                submission=make_submission(
                    context,
                    envelope=make_envelope(idempotency_key="atomic-failure"),
                )
            )
        assert caught.value.write_outcome is PersistenceWriteOutcome.ROLLED_BACK
        assert caught.value.__context__ is None
        async with clean_database.connect() as connection:
            task_count = await connection.scalar(sa.select(sa.func.count()).select_from(TASKS))
            submission_count = await connection.scalar(
                sa.select(sa.func.count()).select_from(TASK_SUBMISSIONS)
            )
        assert task_count == 0
        assert submission_count == 0
    finally:
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text("DROP TRIGGER reject_task_submission_before_insert ON task_submissions")
            )
            await connection.execute(sa.text("DROP FUNCTION reject_task_submission()"))
