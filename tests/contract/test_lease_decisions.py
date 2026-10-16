"""``decisions.py`` 里几条**无法在存储层确定性验证**的判定。

存储层用例是首选：它同时约束两个实现。但有些性质在存储层只能概率性地被证伪——
``list_stale_leases`` 的 ``task_id`` 决胜位就是一例：``task_id`` 由 ``uuid4`` 生成，
插入顺序有一半概率**恰好已是**字典序，于是"排序键第二项被改成常量"这个变异有一半
概率不被发现。一个一半概率转红的反证等于没有反证。

因此这类性质在纯函数层用构造好的输入确定性地钉死，存储层保留契约断言。两层各司其
职，不是重复。
"""

import datetime as _dt

import pytest

from xiaowei_agent.contracts import (
    AttemptIntent,
    GrantRejection,
    LeaseGrant,
    TaskAttemptRejection,
    TaskRecord,
    TaskStatus,
)
from xiaowei_agent.persistence.decisions import (
    classify_task_attempt,
    dispatch_sort_key,
    grant_is_current,
    is_dispatchable,
    is_stale_lease,
    stale_lease_sort_key,
)
from xiaowei_agent.persistence.store import DispatchQuery, TaskAttemptGrant

_EXPIRY = _dt.datetime(2026, 9, 3, 10, 0, 0, tzinfo=_dt.UTC)
_LATER = _EXPIRY + _dt.timedelta(seconds=1)


def _leased(task_id: str, *, expires_at: _dt.datetime, status: TaskStatus) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        idempotency_key=f"idem-{task_id}",
        request_digest="a" * 64,
        status=status,
        version=1,
        created_seq=1,
        attempt_number=0,
        task_failure_count=0,
        next_attempt_at=None,
        lease_owner="w1",
        lease_expires_at=expires_at,
        fencing_token=1,
    )


def test_sort_key_breaks_ties_by_task_id() -> None:
    """同刻过期时按 ``task_id`` 决胜——这是全序的唯一来源。

    输入刻意构造成"插入顺序与字典序相反"，因此稳定排序帮不上忙：决胜位一旦失效，
    这条必定转红。
    """
    later_id = _leased("b", expires_at=_EXPIRY, status=TaskStatus.RUNNING)
    earlier_id = _leased("a", expires_at=_EXPIRY, status=TaskStatus.RUNNING)
    ordered = sorted([later_id, earlier_id], key=stale_lease_sort_key)
    assert [record.task_id for record in ordered] == ["a", "b"]


def test_sort_key_puts_the_oldest_expiry_first() -> None:
    """过期最久的排最前；时间优先于 ``task_id``。"""
    fresh = _leased("a", expires_at=_LATER, status=TaskStatus.RUNNING)
    old = _leased("z", expires_at=_EXPIRY, status=TaskStatus.RUNNING)
    ordered = sorted([fresh, old], key=stale_lease_sort_key)
    assert [record.task_id for record in ordered] == ["z", "a"]


def test_sort_key_rejects_a_record_that_was_never_leased() -> None:
    """反例：编造一个时间戳会让它悄悄排到最前面，看起来像"最该处理的任务"。"""
    never = _leased("a", expires_at=_EXPIRY, status=TaskStatus.RUNNING).model_copy(
        update={"lease_owner": None, "lease_expires_at": None, "fencing_token": None}
    )
    with pytest.raises(ValueError, match="ever leased"):
        stale_lease_sort_key(never)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(TaskStatus.RUNNING, True), (TaskStatus.SUCCEEDED, False), (TaskStatus.FAILED, False)],
    ids=lambda value: getattr(value, "value", str(value)),
)
def test_is_stale_lease_excludes_terminal_statuses(status: TaskStatus, expected: bool) -> None:
    """终态必须单独挡：租约三字段在过期后**不清空**，否则每个正常结束的任务都会被
    永久列为"可恢复"。"""
    record = _leased("a", expires_at=_EXPIRY, status=status)
    assert is_stale_lease(record, now=_LATER) is expected


def test_is_stale_lease_uses_a_strict_expiry_comparison() -> None:
    """恰好等于过期时刻的租约**已经**过期。

    ``lease_is_live`` 用的是 ``lease_expires_at > now``，边界归"已过期"一侧。这条
    钉住边界方向：翻成 ``>=`` 会让一个刚过期的租约在恰好那一刻仍被认为有效，而
    抢占逻辑正好在这个时刻发生。
    """
    record = _leased("a", expires_at=_EXPIRY, status=TaskStatus.RUNNING)
    assert is_stale_lease(record, now=_EXPIRY) is True
    assert is_stale_lease(record, now=_EXPIRY - _dt.timedelta(microseconds=1)) is False


def _grant(record: TaskRecord, *, expires_at: _dt.datetime | None = None) -> TaskAttemptGrant:
    return TaskAttemptGrant(
        lease=LeaseGrant(
            task_id=record.task_id,
            owner=record.lease_owner or "w1",
            expires_at=record.lease_expires_at if expires_at is None else expires_at,
            fencing_token=record.fencing_token or 1,
        ),
        attempt_number=record.attempt_number,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (TaskStatus.CREATED, True),
        (TaskStatus.PLANNING, True),
        (TaskStatus.RUNNING, True),
        (TaskStatus.AWAITING_APPROVAL, False),
        (TaskStatus.SUCCEEDED, False),
    ],
)
def test_dispatch_filter_uses_the_closed_status_set(status: TaskStatus, expected: bool) -> None:
    query = DispatchQuery(tenant_id="dev-local", environment_id="dev", limit=10)
    record = _leased("a", expires_at=_EXPIRY, status=status).model_copy(
        update={"lease_owner": None, "lease_expires_at": None, "fencing_token": None}
    )
    assert is_dispatchable(record, query, now=_LATER) is expected


def test_dispatch_filter_rejects_wrong_scope_future_retry_and_live_lease() -> None:
    query = DispatchQuery(tenant_id="dev-local", environment_id="dev", limit=10)
    available = _leased("a", expires_at=_EXPIRY, status=TaskStatus.RUNNING).model_copy(
        update={"lease_owner": None, "lease_expires_at": None, "fencing_token": None}
    )
    assert is_dispatchable(available, query, now=_LATER)
    assert not is_dispatchable(
        available.model_copy(update={"tenant_id": "other"}), query, now=_LATER
    )
    assert not is_dispatchable(
        available.model_copy(update={"next_attempt_at": _LATER + _dt.timedelta(seconds=1)}),
        query,
        now=_LATER,
    )
    live = available.model_copy(
        update={
            "lease_owner": "w1",
            "lease_expires_at": _LATER + _dt.timedelta(seconds=1),
            "fencing_token": 1,
        }
    )
    assert not is_dispatchable(live, query, now=_LATER)


def test_dispatch_sort_key_breaks_sequence_ties_by_task_id() -> None:
    first = _leased("b", expires_at=_EXPIRY, status=TaskStatus.RUNNING)
    second = first.model_copy(update={"task_id": "a"})
    assert [item.task_id for item in sorted([first, second], key=dispatch_sort_key)] == [
        "a",
        "b",
    ]


@pytest.mark.parametrize(
    ("status", "intent", "expected"),
    [
        (TaskStatus.SUCCEEDED, AttemptIntent.DISPATCH, TaskAttemptRejection.TERMINAL_PROTECTED),
        (
            TaskStatus.AWAITING_APPROVAL,
            AttemptIntent.DISPATCH,
            TaskAttemptRejection.NOT_DISPATCHABLE,
        ),
        (
            TaskStatus.CREATED,
            AttemptIntent.APPROVAL_RESUME,
            TaskAttemptRejection.NOT_DISPATCHABLE,
        ),
    ],
)
def test_attempt_rejection_order_starts_with_terminal_then_intent(
    status: TaskStatus,
    intent: AttemptIntent,
    expected: TaskAttemptRejection,
) -> None:
    record = _leased("a", expires_at=_EXPIRY, status=status)
    assert (
        classify_task_attempt(record, intent=intent, now=_LATER, task_failure_limit=3) is expected
    )


def test_retry_not_due_wins_over_the_live_lease_that_represents_it() -> None:
    record = _leased(
        "a", expires_at=_LATER + _dt.timedelta(seconds=10), status=TaskStatus.RUNNING
    ).model_copy(update={"next_attempt_at": _LATER + _dt.timedelta(seconds=10)})
    assert (
        classify_task_attempt(
            record,
            intent=AttemptIntent.DISPATCH,
            now=_LATER,
            task_failure_limit=3,
        )
        is TaskAttemptRejection.RETRY_NOT_DUE
    )


def test_grant_is_current_uses_storage_expiry_not_the_snapshot_expiry() -> None:
    current = _leased(
        "a", expires_at=_LATER + _dt.timedelta(seconds=30), status=TaskStatus.RUNNING
    ).model_copy(update={"attempt_number": 2})
    stale_expiry_snapshot = _grant(current, expires_at=_EXPIRY)
    assert (
        grant_is_current(
            current,
            stale_expiry_snapshot,
            now=_LATER,
            allowed_statuses=frozenset({TaskStatus.RUNNING}),
        )
        is None
    )


@pytest.mark.parametrize(
    ("updates", "allowed", "expected"),
    [
        ({"status": TaskStatus.SUCCEEDED}, {TaskStatus.RUNNING}, GrantRejection.TERMINAL_PROTECTED),
        ({"status": TaskStatus.PLANNING}, {TaskStatus.RUNNING}, GrantRejection.STATUS_NOT_ALLOWED),
        ({"lease_expires_at": _LATER}, {TaskStatus.RUNNING}, GrantRejection.LEASE_NOT_HELD),
        ({"lease_owner": "w2"}, {TaskStatus.RUNNING}, GrantRejection.STALE_FENCING),
        ({"fencing_token": 2}, {TaskStatus.RUNNING}, GrantRejection.STALE_FENCING),
        ({"attempt_number": 3}, {TaskStatus.RUNNING}, GrantRejection.STALE_FENCING),
    ],
)
def test_grant_is_current_fails_closed_on_every_identity_dimension(
    updates: dict[str, object],
    allowed: set[TaskStatus],
    expected: GrantRejection,
) -> None:
    base = _leased(
        "a", expires_at=_LATER + _dt.timedelta(seconds=30), status=TaskStatus.RUNNING
    ).model_copy(update={"attempt_number": 2})
    grant = _grant(base)
    current = base.model_copy(update=updates)
    assert (
        grant_is_current(current, grant, now=_LATER, allowed_statuses=frozenset(allowed))
        is expected
    )
