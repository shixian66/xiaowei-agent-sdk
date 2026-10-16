"""单进程持久化适配器必须共享一份事实状态。"""

import datetime as dt

import pytest
from tests.conftest import make_envelope, make_submission

from xiaowei_agent.contracts import RequestContext
from xiaowei_agent.persistence import ContextMismatchError
from xiaowei_agent.persistence.evidence import InMemoryEvidenceLedger
from xiaowei_agent.persistence.fake import InMemoryTaskStore
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.plans import InMemoryPlanStore

_AS_OF = dt.datetime(2026, 9, 5, 10, 0, tzinfo=dt.UTC)


def _context(**updates: object) -> RequestContext:
    values: dict[str, object] = {
        "tenant_id": "dev-local",
        "actor": "alice",
        "environment_id": "dev",
        "trace_id": "0" * 32,
        "policy_revision": "policy-1",
    }
    return RequestContext(**(values | updates))


def test_all_in_memory_adapters_share_the_supplied_state(clock) -> None:
    state = InMemoryPersistenceState()
    store = InMemoryTaskStore(clock=clock, state=state)
    plans = InMemoryPlanStore(state=state)
    evidence = InMemoryEvidenceLedger(state=state)

    assert store._state is state
    assert plans._state is state
    assert evidence._state is state


async def test_task_and_submission_become_visible_together(clock) -> None:
    state = InMemoryPersistenceState()
    store = InMemoryTaskStore(clock=clock, state=state)
    context = _context()

    task = await store.create_task(submission=make_submission(context, as_of=_AS_OF))

    assert set(state.tasks) == set(state.submissions) == {task.task_id}
    assert state.submissions[task.task_id].as_of == _AS_OF


async def test_failed_submission_validation_writes_nothing(clock) -> None:
    state = InMemoryPersistenceState()
    store = InMemoryTaskStore(clock=clock, state=state)
    context = _context()
    bad = make_submission(
        context,
        envelope=make_envelope(tenant_id="other-tenant"),
    )

    with pytest.raises(ContextMismatchError):
        await store.create_task(submission=bad)

    assert state.tasks == {}
    assert state.submissions == {}


async def test_idempotent_retry_keeps_the_first_submission(clock) -> None:
    state = InMemoryPersistenceState()
    store = InMemoryTaskStore(clock=clock, state=state)
    context = _context()
    first_submission = make_submission(context, as_of=_AS_OF)
    retry_submission = make_submission(
        context.model_copy(update={"trace_id": "1" * 32}),
        envelope=make_envelope(request_id="request-2"),
        as_of=_AS_OF + dt.timedelta(minutes=10),
    )

    first = await store.create_task(submission=first_submission)
    retry = await store.create_task(submission=retry_submission)

    assert retry.task_id == first.task_id
    assert state.submissions[first.task_id] == first_submission
