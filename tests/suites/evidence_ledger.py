"""``EvidenceLedger`` 的行为用例：**一份定义，两处绑定**。

与 ``tests/suites/plan_store.py`` 同一机制。fixture 名是 ``evidence_ledger``。
"""

import datetime as dt

import pytest
from tests.suites.task_store import bind

from xiaowei_agent.contracts import EvidenceEnvelope, ExternalSource
from xiaowei_agent.persistence.evidence import (
    EvidenceConflictError,
    EvidenceNotFoundError,
)

_AT = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)
_TASK = "task-1"

__all__ = ["ALL_GROUPS", "EVIDENCE_LEDGER_CASES", "bind"]


def _envelope(evidence_id: str, **overrides: object) -> EvidenceEnvelope:
    base: dict[str, object] = {
        "evidence_id": evidence_id,
        "capability_id": "starrocks.slow_query.diagnose",
        "capability_version": "1.0.0",
        "facts": ({"queryId": "q1"},),
        "source": "starrocks-fake",
        "source_kind": ExternalSource.TOOL,
        "captured_at": _AT,
        "sampled": False,
        "limitations": ("window 30m",),
    }
    return EvidenceEnvelope(**(base | overrides))


async def test_append_returns_the_addressable_id(evidence_ledger) -> None:
    returned = await evidence_ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    assert returned == "task-1:s1"


async def test_load_returns_entries_in_write_order(evidence_ledger) -> None:
    await evidence_ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    await evidence_ledger.append(task_id=_TASK, envelope=_envelope("task-1:s2"))
    assert [e.evidence_id for e in await evidence_ledger.load(task_id=_TASK)] == [
        "task-1:s1",
        "task-1:s2",
    ]


async def test_ledger_returns_empty_tuple_for_a_task_without_evidence(evidence_ledger) -> None:
    """无证据是一个正常状态，不是错误：一次尚未取数的任务就是这样。"""
    assert await evidence_ledger.load(task_id="never-written") == ()


async def test_get_resolves_a_written_reference(evidence_ledger) -> None:
    await evidence_ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    assert (await evidence_ledger.get(task_id=_TASK, evidence_id="task-1:s1")).evidence_id == (
        "task-1:s1"
    )


async def test_dangling_evidence_reference_raises(evidence_ledger) -> None:
    await evidence_ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    with pytest.raises(EvidenceNotFoundError):
        await evidence_ledger.get(task_id=_TASK, evidence_id="task-1:s99")


async def test_ledger_is_append_only(evidence_ledger) -> None:
    """同一 id 写入不同内容必须拒绝——覆盖等于篡改审计。"""
    await evidence_ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    with pytest.raises(EvidenceConflictError):
        await evidence_ledger.append(
            task_id=_TASK,
            envelope=_envelope("task-1:s1", facts=({"queryId": "tampered"},)),
        )
    assert len(await evidence_ledger.load(task_id=_TASK)) == 1


async def test_rewriting_identical_content_is_idempotent(evidence_ledger) -> None:
    """幂等重试不应被当成篡改：内容相同即无事发生。"""
    await evidence_ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    await evidence_ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    assert len(await evidence_ledger.load(task_id=_TASK)) == 1


async def test_tasks_are_isolated_from_each_other(evidence_ledger) -> None:
    await evidence_ledger.append(task_id="a", envelope=_envelope("a:s1"))
    assert await evidence_ledger.load(task_id="b") == ()
    with pytest.raises(EvidenceNotFoundError):
        await evidence_ledger.get(task_id="b", evidence_id="a:s1")


async def test_errors_do_not_echo_the_task_id(evidence_ledger) -> None:
    canary = "task-canary-3319"
    with pytest.raises(EvidenceNotFoundError) as err:
        await evidence_ledger.get(task_id=canary, evidence_id="x")
    assert canary not in str(err.value)
    assert err.value.task_id == canary


EVIDENCE_LEDGER_CASES = (
    test_append_returns_the_addressable_id,
    test_load_returns_entries_in_write_order,
    test_ledger_returns_empty_tuple_for_a_task_without_evidence,
    test_get_resolves_a_written_reference,
    test_dangling_evidence_reference_raises,
    test_ledger_is_append_only,
    test_rewriting_identical_content_is_idempotent,
    test_tasks_are_isolated_from_each_other,
    test_errors_do_not_echo_the_task_id,
)

ALL_GROUPS = {"evidence_ledger": EVIDENCE_LEDGER_CASES}
