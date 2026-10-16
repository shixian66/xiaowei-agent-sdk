"""``TaskStore`` 的行为用例：**一份定义，多处绑定**。

M4 要让 ``InMemoryTaskStore`` 与 ``PostgresTaskStore`` 通过同一套行为断言。此前这些
用例散在三个文件里、直接依赖 ``store`` fixture 指向内存实现；要让 PostgreSQL 也跑
一遍，有两条路：

1. **把 ``store`` fixture 参数化成"内存 + PostgreSQL"。** 不行——这三个文件里有两个
   位于 ``tests/security/``，而 socket 放行只对 ``tests/integration/`` 生效。参数化会
   让 ``tests/security/`` 的每条用例都尝试连库，与"安全测试不碰网络"直接冲突。
2. **把用例抽出来，由绑定方提供 ``store``。** 本模块走这条。

本模块**不被 pytest 收集**（文件名不匹配 ``test_*.py``）。用例函数在这里定义，由各
绑定模块调用 ``bind()`` 挂进自己的命名空间；``store`` / ``clock`` / ``task`` 三个
fixture 由绑定方所在目录的 conftest 提供。

搬运是**纯搬运**：一行行为断言都没改。这些用例本来就写得实现无关——断言的是
``second.fencing_token > first.fencing_token``，不是某个具体取值。

**新增用例必须同时加进下面某个分组**，否则它在所有绑定里都收不到，看起来像"写了
测试"，实际一次都没跑。这条由 ``tests/contract/test_task_store_bindings.py`` 承重。
"""

import datetime as _dt
from collections.abc import Callable, MutableMapping, Sequence
from typing import Any

import pytest
from pydantic import ValidationError
from tests.conftest import (
    drive_to_terminal,
    lookup_for,
    make_envelope,
    make_lookup,
    make_submission,
)
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET
from tests.fakes.sinks import make_event

from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    ApprovalRequest,
    ApprovalState,
    AttemptIntent,
    EvidenceEnvelope,
    ExternalSource,
    PipelineStage,
    StepAttemptDecision,
    StepCommitRejection,
    StepOutcomeKind,
    StepResultStatus,
    TaskStatus,
    TransitionRejection,
)
from xiaowei_agent.persistence import (
    ContextMismatchError,
    IdempotencyConflictError,
    TaskNotFoundError,
    UnscopedAuditEventError,
)
from xiaowei_agent.persistence.store import (
    DispatchQuery,
    RetryCommand,
    RetryDecision,
    RetryReason,
    StaleLeaseQuery,
    StepAttemptCommand,
    StepCommitCommand,
    TaskAttemptCommand,
    TaskAttemptRejection,
    TransitionCommand,
)

_APPROVAL_AT = _dt.datetime(2026, 9, 3, 12, 0, tzinfo=_dt.UTC)


async def _transition(store: Any, **values: Any) -> Any:
    """Keep the shared cases focused on outcomes while exercising the command DTO."""
    return await store.transition(command=TransitionCommand(**values))


async def _list_stale(store: Any, *, limit: object) -> Any:
    """Construct the public query DTO so its strict validation stays under test."""
    return await store.list_stale_leases(query=StaleLeaseQuery(limit=limit))


def make_approval(task_id: str, *, step_id: str = "s1") -> ApprovalRequest:
    """一条形状合法的审批请求。内容与本组用例无关——被断言的只有 ``seq``。"""
    return ApprovalRequest(
        task_id=task_id,
        step_id=step_id,
        plan_hash="a" * 64,
        target_fingerprint="b" * 64,
        policy_revision="policy-2026-09-01",
        subject="alice",
        expires_at=_APPROVAL_AT,
        state=ApprovalState.PENDING,
    )


def bind(namespace: MutableMapping[str, Any], cases: Sequence[Callable[..., Any]]) -> None:
    """把共享用例挂进绑定模块的命名空间，供 pytest 按普通模块级函数收集。

    绑定模块自己的 ``pytestmark`` 照常生效：marker 在收集期按模块读取，不写回函数
    对象，因此同一个函数可以在 ``tests/security/`` 带 ``security`` marker、在
    ``tests/contract/`` 不带，互不影响。
    """
    for case in cases:
        namespace[case.__name__] = case


# --- 幂等作用域与创建 ---------------------------------------------------------


async def test_create_is_idempotent_by_key(store, context) -> None:
    first = await store.create_task(submission=make_submission(context))
    second = await store.create_task(submission=make_submission(context))
    assert first.task_id == second.task_id
    assert first.version == second.version


async def test_new_task_has_initial_execution_accounting(store, context) -> None:
    """M5 的调度字段由存储层初始化，调用方不能自行猜默认值。"""
    record = await store.create_task(submission=make_submission(context))
    assert record.created_seq > 0
    assert record.attempt_number == 0
    assert record.task_failure_count == 0
    assert record.next_attempt_at is None


async def test_created_sequence_is_strictly_increasing(store, context) -> None:
    """created_seq 是稳定近似公平顺序；至少必须唯一且随创建推进。"""
    first = await store.create_task(
        submission=make_submission(context, envelope=make_envelope(idempotency_key="created-seq-1"))
    )
    second = await store.create_task(
        submission=make_submission(context, envelope=make_envelope(idempotency_key="created-seq-2"))
    )
    assert second.created_seq > first.created_seq


async def test_retry_with_a_new_request_id_reuses_the_same_task(store, context) -> None:
    """同键同语义的正常重试必须复用任务。

    request_id 每次重试都不同，若把它算进去重摘要，合法重试会被误判为"同键不同
    请求"而被拒。
    """
    first = await store.create_task(
        submission=make_submission(context, envelope=make_envelope(request_id="r1"))
    )
    second = await store.create_task(
        submission=make_submission(context, envelope=make_envelope(request_id="r2"))
    )
    assert first.task_id == second.task_id
    assert first.version == second.version


async def test_idempotency_key_is_scoped_per_tenant(store, context) -> None:
    """全局键表会让跨租户同键共用一个任务。"""
    a = await store.create_task(submission=make_submission(context))
    other = context.model_copy(update={"tenant_id": "other-tenant"})
    b = await store.create_task(
        submission=make_submission(other, envelope=make_envelope(tenant_id="other-tenant"))
    )
    assert a.task_id != b.task_id


async def test_idempotency_key_is_scoped_per_environment(store, context) -> None:
    a = await store.create_task(submission=make_submission(context))
    other = context.model_copy(update={"environment_id": "staging"})
    b = await store.create_task(
        submission=make_submission(other, envelope=make_envelope(environment_id="staging"))
    )
    assert a.task_id != b.task_id


async def test_same_key_with_a_different_request_is_rejected(store, context) -> None:
    """否则第二个不同的请求会静默搭上第一个任务的结果。"""
    await store.create_task(submission=make_submission(context))
    with pytest.raises(IdempotencyConflictError):
        await store.create_task(
            submission=make_submission(
                context,
                envelope=make_envelope(text="a completely different question"),
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("tenant_id", "other-tenant"), ("actor", "mallory"), ("environment_id", "prod")],
)
async def test_envelope_context_mismatch_is_rejected(store, context, field, value) -> None:
    """伪造信封上下文必须拒绝，否则任务会记在未经解析确认的租户/环境下。"""
    with pytest.raises(ContextMismatchError):
        await store.create_task(
            submission=make_submission(context, envelope=make_envelope(**{field: value}))
        )


async def test_envelope_without_environment_id_is_accepted(store, context) -> None:
    """environment_id 在信封中可选；缺省时以 context 解析结果为准（ADR-007 D2）。"""
    record = await store.create_task(
        submission=make_submission(context, envelope=make_envelope(environment_id=None))
    )
    assert record.environment_id == context.environment_id


async def test_get_unknown_task_raises(store, context) -> None:
    with pytest.raises(TaskNotFoundError):
        await store.get(lookup=make_lookup("no-such-task", context))


async def test_get_rejects_the_wrong_tenant_without_leaking_existence(store, context) -> None:
    record = await store.create_task(submission=make_submission(context))
    lookup = lookup_for(record).model_copy(update={"tenant_id": "other-tenant"})
    with pytest.raises(TaskNotFoundError):
        await store.get(lookup=lookup)


async def test_get_rejects_the_wrong_environment_without_leaking_existence(store, context) -> None:
    record = await store.create_task(submission=make_submission(context))
    lookup = lookup_for(record).model_copy(update={"environment_id": "prod"})
    with pytest.raises(TaskNotFoundError):
        await store.get(lookup=lookup)


async def test_bare_task_id_is_not_a_supported_read_shape(store, task) -> None:
    with pytest.raises(TypeError):
        await store.get(task.task_id)


# --- CAS 与迁移合法性 ---------------------------------------------------------


async def test_cas_success_bumps_version(store, task) -> None:
    result = await _transition(
        store, task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.PLANNING
    )
    assert result.applied is True
    assert result.rejection is None
    assert result.winner.version == task.version + 1


async def test_cas_failure_returns_the_storage_winner(store, task) -> None:
    """调用方必须能拿到 winner，而不是只知道"失败了"。"""
    await _transition(
        store, task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.PLANNING
    )
    stale = await _transition(
        store, task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.RUNNING
    )
    assert stale.applied is False
    assert stale.rejection is TransitionRejection.VERSION_MISMATCH
    assert stale.winner.status is TaskStatus.PLANNING
    assert stale.winner.version == task.version + 1


async def test_illegal_transition_is_rejected(store, task) -> None:
    result = await _transition(
        store, task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.SUCCEEDED
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.ILLEGAL_TRANSITION


async def test_terminal_reason_is_cleared_when_a_transition_omits_it(store, task) -> None:
    """``terminal_reason`` **无条件写入，包括写 ``None``**。

    T1 变异反证发现的覆盖缺口：把 ``apply_transition`` 改成"有值才写"，全部 1236 条
    用例仍然全绿。那个改法看起来无害，实际会让"先失败置上原因、再重试转为非终态"
    的记录残留旧原因——调用方拿到一条状态与原因互相矛盾的记录，而没有任何断言拦它。

    这条同时是 M4 的跨实现约束：PostgreSQL 实现必须同样无条件 ``SET``，包括
    ``SET terminal_reason = NULL``，不得写成"有值才进 SET 子句"。
    """
    marked = await _transition(
        store,
        task_id=task.task_id,
        expected_version=task.version,
        to_status=TaskStatus.PLANNING,
        terminal_reason="transient.upstream_timeout",
    )
    assert marked.applied
    assert marked.winner.terminal_reason == "transient.upstream_timeout"

    resumed = await _transition(
        store,
        task_id=task.task_id,
        expected_version=marked.winner.version,
        to_status=TaskStatus.RUNNING,
    )
    assert resumed.applied
    assert resumed.winner.terminal_reason is None
    assert (await store.get(lookup=lookup_for(task))).terminal_reason is None


# --- 租约与 fencing -----------------------------------------------------------


async def test_second_holder_is_refused_while_lease_is_live(store, task) -> None:
    assert await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    assert await store.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30) is None


async def test_expired_lease_can_be_taken_over_with_a_higher_token(store, task, clock) -> None:
    first = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    second = await store.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30)
    assert second is not None
    assert second.fencing_token > first.fencing_token


async def test_renew_keeps_the_same_token(store, task) -> None:
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    renewed = await store.renew_lease(
        task_id=task.task_id,
        owner="w1",
        fencing_token=granted.fencing_token,
        ttl_seconds=30,
    )
    assert renewed is not None
    assert renewed.fencing_token == granted.fencing_token


async def test_renew_by_a_non_owner_is_refused(store, task) -> None:
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    assert (
        await store.renew_lease(
            task_id=task.task_id,
            owner="w2",
            fencing_token=granted.fencing_token,
            ttl_seconds=30,
        )
        is None
    )


async def test_renew_with_a_wrong_token_is_refused(store, task) -> None:
    """续租必须同时匹配 owner **与** fencing token。

    T1 变异反证发现的覆盖缺口：把 ``may_renew_lease`` 改成只比 owner，全部 1236 条
    用例仍然全绿。只比 owner 的后果是同名 worker 的**旧进程**可以续上**新进程**的
    租约——owner 名字通常是主机名或角色名，进程重启后重名是常态，而那正是 fencing
    token 存在的理由。既有用例只覆盖了"换个 owner 续租被拒"，没有覆盖"同 owner、
    错 token"。
    """
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    assert (
        await store.renew_lease(
            task_id=task.task_id,
            owner="w1",
            fencing_token=granted.fencing_token + 1,
            ttl_seconds=30,
        )
        is None
    )


async def test_renew_after_expiry_is_refused(store, task, clock) -> None:
    """过期后必须重新 acquire，否则旧 worker 可以复活一个已被抢占的租约。"""
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    assert (
        await store.renew_lease(
            task_id=task.task_id,
            owner="w1",
            fencing_token=granted.fencing_token,
            ttl_seconds=30,
        )
        is None
    )


async def test_stale_token_write_is_rejected(store, task, clock) -> None:
    old = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    await store.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30)
    current = await store.get(lookup=lookup_for(task))
    result = await _transition(
        store,
        task_id=task.task_id,
        expected_version=current.version,
        to_status=TaskStatus.PLANNING,
        fencing_token=old.fencing_token,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.STALE_FENCING_TOKEN


async def test_write_without_token_under_live_lease_is_rejected(store, task) -> None:
    """不传 token 即跳过 fencing 校验，等于没有 fencing。"""
    await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    current = await store.get(lookup=lookup_for(task))
    result = await _transition(
        store,
        task_id=task.task_id,
        expected_version=current.version,
        to_status=TaskStatus.PLANNING,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.LEASE_NOT_HELD


async def test_token_without_any_live_lease_is_rejected(store, task) -> None:
    """持有一个不存在的租约同样是违规。"""
    result = await _transition(
        store,
        task_id=task.task_id,
        expected_version=task.version,
        to_status=TaskStatus.PLANNING,
        fencing_token=1,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.LEASE_NOT_HELD


async def test_transition_without_any_lease_is_allowed(store, task) -> None:
    """created → planning 发生在取租约之前，必须放行，否则任务无法启动。"""
    result = await _transition(
        store,
        task_id=task.task_id,
        expected_version=task.version,
        to_status=TaskStatus.PLANNING,
    )
    assert result.applied is True


async def test_terminal_task_cannot_acquire_lease(store, task) -> None:
    """已终态的任务不应再被任何 worker 领走。"""
    await _transition(
        store, task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.CANCELED
    )
    assert await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30) is None


async def test_stale_token_is_reported_before_version_mismatch(store, task, clock) -> None:
    """两者同时违反时，报出的必须是更具体的 fencing 原因。"""
    old = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    await store.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30)
    result = await _transition(
        store,
        task_id=task.task_id,
        expected_version=999,
        to_status=TaskStatus.PLANNING,
        fencing_token=old.fencing_token,
    )
    assert result.rejection is TransitionRejection.STALE_FENCING_TOKEN


async def test_expired_lease_cannot_be_bypassed_by_omitting_the_token(store, task, clock) -> None:
    """租约过期后，stale worker 不带 token 也不得写入。

    把规则锚定在"租约此刻是否 live"会留下这个缺口：过期后 live 为假，"有租约缺
    token"的分支就不再命中，省略 token 反而畅通。正确锚点是"任务是否曾被租出"。
    """
    await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    current = await store.get(lookup=lookup_for(task))
    result = await _transition(
        store,
        task_id=task.task_id,
        expected_version=current.version,
        to_status=TaskStatus.PLANNING,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.LEASE_NOT_HELD


async def test_expired_lease_cannot_be_used_with_its_old_token_either(store, task, clock) -> None:
    """过期后必须重新 acquire，带着旧 token 同样不行。"""
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    current = await store.get(lookup=lookup_for(task))
    result = await _transition(
        store,
        task_id=task.task_id,
        expected_version=current.version,
        to_status=TaskStatus.PLANNING,
        fencing_token=granted.fencing_token,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.LEASE_NOT_HELD


# --- public 入参的隐式转换（Protocol 标注在运行时不拦任何东西） ---------------


async def test_expected_version_rejects_bool(store, task) -> None:
    """False 会匹配版本 0，True 会匹配版本 1。"""
    with pytest.raises(ValidationError):
        await _transition(
            store, task_id=task.task_id, expected_version=False, to_status=TaskStatus.PLANNING
        )


async def test_fencing_token_rejects_bool(store, task) -> None:
    """True 会匹配 token 1。"""
    with pytest.raises(ValidationError):
        await _transition(
            store,
            task_id=task.task_id,
            expected_version=task.version,
            to_status=TaskStatus.PLANNING,
            fencing_token=True,
        )


async def test_to_status_rejects_a_plain_string(store, task) -> None:
    with pytest.raises(ValidationError):
        await _transition(
            store, task_id=task.task_id, expected_version=task.version, to_status="planning"
        )


@pytest.mark.parametrize("bad_ttl", [0, -10])
async def test_acquire_lease_rejects_non_positive_ttl(store, task, bad_ttl: int) -> None:
    with pytest.raises(ValidationError):
        await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=bad_ttl)


async def test_acquire_lease_rejects_bytes_owner(store, task) -> None:
    with pytest.raises(ValidationError):
        await store.acquire_lease(task_id=task.task_id, owner=b"w1", ttl_seconds=30)


@pytest.mark.parametrize(
    ("owner", "ttl"), [(b"w1", 30), ("w1", -10), ("", 30)], ids=["bytes", "ttl", "blank"]
)
async def test_invalid_lease_arguments_do_not_mutate_the_store(
    store, task, owner: object, ttl: int
) -> None:
    """非法入参必须在**写入之前**失败。

    先污染 store 再抛 ValidationError 会留下一个带着无效租约字段的任务记录，
    之后所有 fencing 判定都建立在这条脏记录上。
    """
    before = await store.get(lookup=lookup_for(task))
    with pytest.raises(ValidationError):
        await store.acquire_lease(task_id=task.task_id, owner=owner, ttl_seconds=ttl)
    after = await store.get(lookup=lookup_for(task))
    assert after == before


# --- stale lease 的**发现**（不是接管） ---------------------------------------


async def _leased_task(store, context, key: str, owner: str):
    """新建一个任务并给它一份租约，返回 ``(record, grant)``。"""
    record = await store.create_task(
        submission=make_submission(context, envelope=make_envelope(idempotency_key=key))
    )
    grant = await store.acquire_lease(task_id=record.task_id, owner=owner, ttl_seconds=30)
    return record, grant


async def test_a_task_that_was_never_leased_is_not_stale(store, task) -> None:
    """从未租出的任务没有"接管"可言——它还没开始。"""
    assert await _list_stale(store, limit=10) == ()


async def test_a_live_lease_is_not_stale(store, task) -> None:
    """仍在有效期内的任务有活着的持有者，列出它等于邀请抢占。"""
    await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    assert await _list_stale(store, limit=10) == ()


async def test_an_expired_lease_is_stale(store, task, clock) -> None:
    await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    stale = await _list_stale(store, limit=10)
    assert [record.task_id for record in stale] == [task.task_id]


async def test_a_terminal_task_is_never_stale(store, task, clock) -> None:
    """去掉"未终态"这条过滤，每个正常结束的任务都会被永久列为可恢复。

    因为租约三字段在过期后**不清空**——``fencing_token IS NOT NULL`` 正是"曾被
    租出"的判据，清空它会重开 fencing 的缺口。所以终态必须单独挡。
    """
    await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    current = await store.get(lookup=lookup_for(task))
    result = await _transition(
        store,
        task_id=task.task_id,
        expected_version=current.version,
        to_status=TaskStatus.CANCELED,
    )
    assert result.applied is False  # 过期租约下写入被拒，任务仍非终态
    taken = await store.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30)
    assert taken is not None
    done = await _transition(
        store,
        task_id=task.task_id,
        expected_version=current.version,
        to_status=TaskStatus.CANCELED,
        fencing_token=taken.fencing_token,
    )
    assert done.applied is True
    clock.advance(seconds=31)
    assert await _list_stale(store, limit=10) == ()


async def test_stale_leases_are_ordered_by_expiry_then_task_id(store, context, clock) -> None:
    """排序必须稳定且全序，否则两次调用可能返回不同的前 N 条。"""
    first, _ = await _leased_task(store, context, "idem-a", "w1")
    clock.advance(seconds=5)
    second, _ = await _leased_task(store, context, "idem-b", "w2")
    clock.advance(seconds=31)
    stale = await _list_stale(store, limit=10)
    assert [record.task_id for record in stale] == [first.task_id, second.task_id]


async def test_stale_leases_with_the_same_expiry_are_ordered_by_task_id(
    store, context, clock
) -> None:
    """同刻过期时必须由 ``task_id`` 决胜。

    T4 变异反证发现的覆盖缺口：把排序键的第二项改成常量，全部用例仍然全绿——因为
    上一条用例里两个任务的过期时间**不同**，决胜位根本没被用到。同刻过期是常态而
    非边界：同一批 worker 用同一 TTL 领走的任务，过期时间逐条相同。

    没有决胜位，返回的前 N 条在两次调用之间可能不同，而调用方会以为自己看到的是
    "最该处理的那些"。
    """
    first, _ = await _leased_task(store, context, "idem-a", "w1")
    second, _ = await _leased_task(store, context, "idem-b", "w2")
    clock.advance(seconds=31)
    stale = await _list_stale(store, limit=10)
    assert len(stale) == 2
    assert [record.lease_expires_at for record in stale] == [
        stale[0].lease_expires_at,
        stale[0].lease_expires_at,
    ], "前提不成立：两条租约的过期时间应当相同"
    assert [record.task_id for record in stale] == sorted([first.task_id, second.task_id])


async def test_stale_leases_respect_the_limit(store, context, clock) -> None:
    await _leased_task(store, context, "idem-a", "w1")
    clock.advance(seconds=5)
    await _leased_task(store, context, "idem-b", "w2")
    clock.advance(seconds=31)
    assert len(await _list_stale(store, limit=10)) == 2
    assert len(await _list_stale(store, limit=1)) == 1


async def test_a_terminal_task_cannot_crowd_out_a_stale_one(store, context, clock) -> None:
    """**终态任务不得占用 limit 名额。**

    终态任务的租约字段按设计**不清空**（清空会重开 fencing 的缺口），因此它永久满足
    "曾被租出 + 已过期"，并且按过期时间升序排在最前。若实现先按这个较宽的条件
    ``LIMIT``、再在内存里排除终态，真正需要恢复的任务就会被挤出窗口——而且终态任务
    只增不减，**返回量会随系统运行单调衰减到零**。

    上面几条用例挡不住它：它们要么只有一个任务，要么 ``limit`` 大到窗口装得下全部。
    这条把"更早过期的终态"与"较晚过期的非终态"放在一起并把 ``limit`` 收到 1。
    """
    finished, grant = await _leased_task(store, context, "idem-finished", "w1")
    clock.advance(seconds=5)
    pending, _ = await _leased_task(store, context, "idem-pending", "w2")

    current = await store.get(lookup=lookup_for(finished))
    done = await _transition(
        store,
        task_id=finished.task_id,
        expected_version=current.version,
        to_status=TaskStatus.CANCELED,
        fencing_token=grant.fencing_token,
    )
    assert done.applied is True
    clock.advance(seconds=31)

    # 前提必须成立，否则这条用例什么也没测：终态任务仍带着租约字段，且**更早过期**。
    settled = await store.get(lookup=lookup_for(finished))
    waiting = await store.get(lookup=lookup_for(pending))
    assert settled.status in TERMINAL_STATUSES
    assert settled.fencing_token is not None
    assert settled.lease_expires_at is not None
    assert waiting.lease_expires_at is not None
    assert settled.lease_expires_at < waiting.lease_expires_at, (
        "前提不成立：终态任务应当更早过期，否则它排不到前面，挤不掉任何东西"
    )

    assert [r.task_id for r in await _list_stale(store, limit=1)] == [pending.task_id]
    assert [r.task_id for r in await _list_stale(store, limit=10)] == [pending.task_id]


async def test_listing_stale_leases_claims_nothing(store, task, clock) -> None:
    """**只发现，不接管。** 列出之后记录必须逐字段不变，租约仍属原主。

    把"发现"和"接管"合成一个方法会让 TaskStore 长出调度能力，而调度属 M5。
    """
    await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    before = await store.get(lookup=lookup_for(task))
    assert await _list_stale(store, limit=10)
    assert await store.get(lookup=lookup_for(task)) == before


@pytest.mark.parametrize("bad_limit", [0, -1, True], ids=["zero", "negative", "bool"])
async def test_list_stale_leases_rejects_a_bad_limit(store, bad_limit: object) -> None:
    """``limit=True`` 在运行时会被当作 1；参数少不代表不会写错。"""
    with pytest.raises(ValidationError):
        await _list_stale(store, limit=bad_limit)


# --- 终态保护 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "terminal", sorted(TERMINAL_STATUSES, key=lambda s: s.value), ids=lambda s: s.value
)
async def test_no_transition_out_of_any_terminal_status(store, task, terminal) -> None:
    await drive_to_terminal(store, lookup_for(task), terminal)
    current = await store.get(lookup=lookup_for(task))
    for target in TaskStatus:
        result = await _transition(
            store, task_id=task.task_id, expected_version=current.version, to_status=target
        )
        assert result.applied is False
        assert result.rejection is TransitionRejection.TERMINAL_PROTECTED
        assert result.winner.status is terminal


async def test_terminal_protection_wins_over_version_mismatch(store, task) -> None:
    """拒绝原因必须指向最具体的违规，否则真实原因会被掩盖。"""
    await _transition(
        store, task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.CANCELED
    )
    result = await _transition(
        store, task_id=task.task_id, expected_version=999, to_status=TaskStatus.RUNNING
    )
    assert result.rejection is TransitionRejection.TERMINAL_PROTECTED


# --- append-only 台账：审批与审计 ---------------------------------------------
#
# 这两张表在 M4 之前是**只写不查**的：``record_approval`` 被 Runner 调用，但没有任何
# 用例断言它真的落了盘，两个实现也可以对序号给出完全不同的答案而全绿；
# ``record_audit_event`` 更是连方法都没有，只有一张空表。
#
# 断言对象是写入返回的 ``seq``，不是"读回来的内容"：读回属于消费路径（M8），而序号
# 是本次写入自己的回执，两个实现必须给出同一个答案。


async def test_first_approval_of_a_task_is_seq_one(store, task) -> None:
    assert await store.record_approval(request=make_approval(task.task_id)) == 1


async def test_approval_seq_increases_within_a_task(store, task) -> None:
    """同一步骤可以被多次请求审批（超时后重新发起），第二次不得覆盖第一次。"""
    seqs = [await store.record_approval(request=make_approval(task.task_id)) for _ in range(3)]
    assert seqs == [1, 2, 3]


async def test_approval_seq_is_scoped_per_task(store, context) -> None:
    """序号按任务分桶。用全局计数器同样能让上一条通过，但两个任务会共享号段。"""
    first = await store.create_task(submission=make_submission(context))
    second = await store.create_task(
        submission=make_submission(context, envelope=make_envelope(idempotency_key="idem-2"))
    )
    assert await store.record_approval(request=make_approval(first.task_id)) == 1
    assert await store.record_approval(request=make_approval(second.task_id)) == 1
    assert await store.record_approval(request=make_approval(first.task_id)) == 2


async def test_first_audit_event_of_a_task_is_seq_one(store, task) -> None:
    event = make_event(stage=PipelineStage.ADMISSION, task_id=task.task_id)
    assert await store.record_audit_event(event=event) == 1


async def test_audit_seq_increases_within_a_task(store, task) -> None:
    """append-only：同一任务的第二条事件不得覆盖第一条。

    ``event_id`` 每次都不同，但序号不能由它派生——它是调用方给的，不是存储层分配的。
    """
    seqs = [
        await store.record_audit_event(
            event=make_event(stage=PipelineStage.ADMISSION, task_id=task.task_id, event_id=f"e{n}")
        )
        for n in range(3)
    ]
    assert seqs == [1, 2, 3]


async def test_audit_seq_is_scoped_per_task(store, context) -> None:
    first = await store.create_task(submission=make_submission(context))
    second = await store.create_task(
        submission=make_submission(context, envelope=make_envelope(idempotency_key="idem-2"))
    )
    stage = PipelineStage.ADMISSION
    assert await store.record_audit_event(event=make_event(stage=stage, task_id=first.task_id)) == 1
    assert (
        await store.record_audit_event(event=make_event(stage=stage, task_id=second.task_id)) == 1
    )
    assert await store.record_audit_event(event=make_event(stage=stage, task_id=first.task_id)) == 2


async def test_approval_and_audit_seq_are_independent(store, task) -> None:
    """两张表各自编号。共用一个计数器时，前面几条按任务分桶的用例照样全绿。"""
    assert await store.record_approval(request=make_approval(task.task_id)) == 1
    assert (
        await store.record_audit_event(
            event=make_event(stage=PipelineStage.ADMISSION, task_id=task.task_id)
        )
        == 1
    )
    assert await store.record_approval(request=make_approval(task.task_id)) == 2


async def test_audit_event_without_a_task_id_is_rejected(store) -> None:
    """``TraceEvent.task_id`` 可为 ``None``，而审计表要求 NOT NULL。

    落差必须在入口显式拒绝：交给数据库会变成一条指向连接层的 IntegrityError，
    而内存实现根本不会报错——两个实现就此分叉。
    """
    event = make_event(stage=PipelineStage.INTENT, task_id=None)
    with pytest.raises(UnscopedAuditEventError):
        await store.record_audit_event(event=event)
    # 拒绝之后编号必须照常从 1 开始：把这条并进来，是因为单独写一条"拒绝不消耗
    # 序号"的用例**无法转红**——没有 task_id 就没有桶可污染，任何"先分配再校验"
    # 的实现都只会往一个占位桶里写，从 Protocol 上完全观察不到。落盘层面的证据在
    # tests/integration/test_audit_and_approval_ledgers.py 直接读表，调用顺序本身
    # 由 tests/security/test_audit_ledger_guards.py 按 AST 钉死。
    assert (
        await store.record_audit_event(
            event=make_event(stage=PipelineStage.ADMISSION, task_id="task-after-reject")
        )
        == 1
    )


async def test_audit_events_do_not_require_the_task_to_exist(store) -> None:
    """台账**不校验任务存在性**，这是选择，不是疏漏。

    代价是拼错的 task_id 会静默写进去。收益是审计写入不依赖任务行，因而不需要在
    ``tasks`` 上取行锁——PostgreSQL 实现的 advisory lock 正是基于这一点（见
    ``postgres._serialise_on_task``）。改成校验存在性会让两个实现都要引入
    ``TaskNotFoundError`` 路径，且与 ``record_approval`` 的既有契约不一致。
    """
    event = make_event(stage=PipelineStage.ADMISSION, task_id="no-such-task")
    assert await store.record_audit_event(event=event) == 1


# --- M5 分派、任务尝试与重试 -------------------------------------------------


def _attempt_command(
    task_id: str,
    *,
    owner: str = "worker-1",
    intent: AttemptIntent = AttemptIntent.DISPATCH,
    ttl_seconds: int = 60,
) -> TaskAttemptCommand:
    return TaskAttemptCommand(
        task_id=task_id,
        intent=intent,
        owner=owner,
        ttl_seconds=ttl_seconds,
        trace_id="1" * 32,
    )


def _retry_command(grant, *, next_attempt_at: _dt.datetime, event_id: str = "retry-1"):
    event = make_event(
        stage=PipelineStage.LIFECYCLE,
        task_id=grant.task_id,
        event_id=event_id,
    ).model_copy(
        update={
            "attempt_number": grant.attempt_number,
            "capability_id": None,
            "step_id": None,
        }
    )
    return RetryCommand(
        grant=grant,
        next_attempt_at=next_attempt_at,
        reason=RetryReason.UNCLASSIFIED_ERROR,
        audit_events=(event,),
    )


async def _move(store, record, *statuses: TaskStatus):
    current = record
    for status in statuses:
        result = await _transition(
            store,
            task_id=current.task_id,
            expected_version=current.version,
            to_status=status,
        )
        assert result.applied, result.rejection
        current = result.winner
    return current


async def test_dispatch_filters_before_applying_the_limit(store, context) -> None:
    excluded = await store.create_task(
        submission=make_submission(
            context, envelope=make_envelope(idempotency_key="dispatch-excluded")
        )
    )
    await _move(
        store,
        excluded,
        TaskStatus.PLANNING,
        TaskStatus.RUNNING,
        TaskStatus.AWAITING_APPROVAL,
    )
    expected = await store.create_task(
        submission=make_submission(
            context, envelope=make_envelope(idempotency_key="dispatch-expected")
        )
    )
    found = await store.list_dispatchable_tasks(
        query=DispatchQuery(
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
            limit=1,
        )
    )
    assert [record.task_id for record in found] == [expected.task_id]


async def test_dispatch_is_scope_isolated_and_stably_ordered(store, context) -> None:
    first = await store.create_task(
        submission=make_submission(
            context, envelope=make_envelope(idempotency_key="dispatch-first")
        )
    )
    second = await store.create_task(
        submission=make_submission(
            context, envelope=make_envelope(idempotency_key="dispatch-second")
        )
    )
    other_context = context.model_copy(update={"tenant_id": "other-tenant"})
    await store.create_task(
        submission=make_submission(
            other_context,
            envelope=make_envelope(tenant_id="other-tenant", idempotency_key="dispatch-other"),
        )
    )
    found = await store.list_dispatchable_tasks(
        query=DispatchQuery(
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
            limit=10,
        )
    )
    assert [record.task_id for record in found] == [first.task_id, second.task_id]


async def test_begin_attempt_returns_the_original_submission_and_one_grant(store, context) -> None:
    submission = make_submission(context)
    task = await store.create_task(submission=submission)
    result = await store.begin_task_attempt(command=_attempt_command(task.task_id))
    assert result.applied
    assert result.submission == submission
    assert result.grant is not None
    assert result.grant.attempt_number == 1
    assert result.winner.attempt_number == 1
    assert result.winner.version == task.version
    assert result.committed_audit_events == ()

    loser = await store.begin_task_attempt(command=_attempt_command(task.task_id, owner="worker-2"))
    assert loser.applied is False
    assert loser.rejection is TaskAttemptRejection.LIVE_LEASE
    assert loser.submission is None
    assert loser.winner.attempt_number == 1


async def test_attempt_intents_have_disjoint_status_sets(store, task) -> None:
    wrong_resume = await store.begin_task_attempt(
        command=_attempt_command(task.task_id, intent=AttemptIntent.APPROVAL_RESUME)
    )
    assert wrong_resume.rejection is TaskAttemptRejection.NOT_DISPATCHABLE

    awaiting = await _move(
        store,
        task,
        TaskStatus.PLANNING,
        TaskStatus.RUNNING,
        TaskStatus.AWAITING_APPROVAL,
    )
    wrong_dispatch = await store.begin_task_attempt(command=_attempt_command(awaiting.task_id))
    assert wrong_dispatch.rejection is TaskAttemptRejection.NOT_DISPATCHABLE

    resumed = await store.begin_task_attempt(
        command=_attempt_command(awaiting.task_id, intent=AttemptIntent.APPROVAL_RESUME)
    )
    assert resumed.applied
    assert resumed.grant is not None


async def test_schedule_retry_is_idempotent_and_immediately_fences_the_grant(
    store, task, clock
) -> None:
    started = await store.begin_task_attempt(command=_attempt_command(task.task_id))
    assert started.grant is not None
    command = _retry_command(started.grant, next_attempt_at=clock() + _dt.timedelta(seconds=10))
    first = await store.schedule_retry(command=command)
    assert first.decision is RetryDecision.SCHEDULED
    assert first.winner.task_failure_count == 1
    assert first.winner.fencing_token > started.grant.fencing_token
    assert first.winner.lease_expires_at == command.next_attempt_at
    stale_write = await _transition(
        store,
        task_id=task.task_id,
        expected_version=first.winner.version,
        to_status=TaskStatus.PLANNING,
        fencing_token=started.grant.fencing_token,
    )
    assert stale_write.applied is False
    assert stale_write.rejection is TransitionRejection.STALE_FENCING_TOKEN

    replay = await store.schedule_retry(command=command)
    assert replay.decision is RetryDecision.ALREADY_SCHEDULED
    assert replay.winner == first.winner

    changed = command.model_copy(
        update={"next_attempt_at": command.next_attempt_at + _dt.timedelta(seconds=1)}
    )
    mismatch = await store.schedule_retry(command=changed)
    assert mismatch.decision is RetryDecision.COMMAND_MISMATCH
    assert mismatch.winner == first.winner


@pytest.mark.parametrize("delay", [30, 60, 90], ids=["shorter", "equal", "longer"])
async def test_retry_is_not_due_until_the_exact_boundary(store, task, clock, delay) -> None:
    started = await store.begin_task_attempt(command=_attempt_command(task.task_id))
    assert started.grant is not None
    due = clock() + _dt.timedelta(seconds=delay)
    scheduled = await store.schedule_retry(
        command=_retry_command(started.grant, next_attempt_at=due)
    )
    assert scheduled.decision is RetryDecision.SCHEDULED

    early = await store.begin_task_attempt(command=_attempt_command(task.task_id, owner="worker-2"))
    assert early.rejection is TaskAttemptRejection.RETRY_NOT_DUE
    clock.advance(seconds=delay)
    next_attempt = await store.begin_task_attempt(
        command=_attempt_command(task.task_id, owner="worker-2")
    )
    assert next_attempt.applied
    assert next_attempt.grant is not None
    assert next_attempt.grant.attempt_number == 2

    stale_replay = await store.schedule_retry(
        command=_retry_command(started.grant, next_attempt_at=due)
    )
    assert stale_replay.decision is RetryDecision.STALE_FENCING


async def test_retry_accepts_a_grant_that_failed_during_planning(store, task, clock) -> None:
    started = await store.begin_task_attempt(command=_attempt_command(task.task_id))
    assert started.grant is not None
    planning = await _transition(
        store,
        task_id=task.task_id,
        expected_version=started.winner.version,
        to_status=TaskStatus.PLANNING,
        fencing_token=started.grant.fencing_token,
    )
    assert planning.applied
    retried = await store.schedule_retry(
        command=_retry_command(started.grant, next_attempt_at=clock() + _dt.timedelta(seconds=1))
    )
    assert retried.decision is RetryDecision.SCHEDULED
    assert retried.winner.task_failure_count == 1


async def test_expired_grant_cannot_schedule_retry(store, task, clock) -> None:
    started = await store.begin_task_attempt(command=_attempt_command(task.task_id))
    assert started.grant is not None
    clock.advance(seconds=60)
    result = await store.schedule_retry(
        command=_retry_command(started.grant, next_attempt_at=clock() + _dt.timedelta(seconds=1))
    )
    assert result.decision is RetryDecision.STALE_FENCING
    assert result.winner.task_failure_count == 0


async def test_retry_exhaustion_terminalizes_before_allocating_a_fourth_grant(
    store, task, clock
) -> None:
    current_task = task
    for attempt_number in range(1, 4):
        started = await store.begin_task_attempt(
            command=_attempt_command(current_task.task_id, owner=f"worker-{attempt_number}")
        )
        assert started.grant is not None
        assert started.grant.attempt_number == attempt_number
        scheduled = await store.schedule_retry(
            command=_retry_command(
                started.grant,
                next_attempt_at=clock() + _dt.timedelta(seconds=1),
                event_id=f"retry-{attempt_number}",
            )
        )
        assert scheduled.decision is RetryDecision.SCHEDULED
        current_task = scheduled.winner
        clock.advance(seconds=1)

    exhausted = await store.begin_task_attempt(
        command=_attempt_command(current_task.task_id, owner="worker-4")
    )
    assert exhausted.applied is False
    assert exhausted.rejection is TaskAttemptRejection.RETRY_EXHAUSTED
    assert exhausted.winner.status is TaskStatus.FAILED
    assert exhausted.winner.terminal_reason == "retry_exhausted"
    assert exhausted.winner.attempt_number == 3
    assert len(exhausted.committed_audit_events) == 1


async def test_terminal_attempt_rejection_never_mutates_the_winner(store, task) -> None:
    terminal = await _move(store, task, TaskStatus.CANCELED)
    result = await store.begin_task_attempt(command=_attempt_command(task.task_id))
    assert result.rejection is TaskAttemptRejection.TERMINAL_PROTECTED
    assert result.winner == terminal
    assert result.submission is None


# --- M5 步骤 journal ----------------------------------------------------------


async def _running_attempt(store, plan_store, context, *, plan=FIXTURE_PLAN):
    task = await store.create_task(submission=make_submission(context))
    started = await store.begin_task_attempt(command=_attempt_command(task.task_id))
    assert started.grant is not None
    await plan_store.save(task_id=task.task_id, plan=plan, target=FIXTURE_TARGET)
    planning = await _transition(
        store,
        task_id=task.task_id,
        expected_version=started.winner.version,
        to_status=TaskStatus.PLANNING,
        fencing_token=started.grant.fencing_token,
    )
    assert planning.applied
    running = await _transition(
        store,
        task_id=task.task_id,
        expected_version=planning.winner.version,
        to_status=TaskStatus.RUNNING,
        fencing_token=started.grant.fencing_token,
    )
    assert running.applied
    return started.grant, running.winner


def _step_event(grant, *, event_id: str = "step-event-1"):
    return make_event(
        stage=PipelineStage.GATEWAY,
        task_id=grant.task_id,
        event_id=event_id,
    ).model_copy(
        update={
            "attempt_number": grant.attempt_number,
            "step_id": "s1",
        }
    )


def _step_evidence(grant, *, value: str = "q1") -> EvidenceEnvelope:
    return EvidenceEnvelope(
        evidence_id=f"{grant.task_id}:s1",
        capability_id=FIXTURE_PLAN.capability_id,
        capability_version=FIXTURE_PLAN.capability_version,
        facts=({"query_id": value},),
        source="starrocks-fake",
        source_kind=ExternalSource.TOOL,
        captured_at=_APPROVAL_AT,
        sampled=False,
        limitations=("deterministic fixture",),
    )


async def test_step_begin_is_idempotent_within_one_grant(store, plan_store, context) -> None:
    grant, _ = await _running_attempt(store, plan_store, context)
    command = StepAttemptCommand(grant=grant, step_id="s1")
    first = await store.begin_step_attempt(command=command)
    second = await store.begin_step_attempt(command=command)
    assert first.decision is StepAttemptDecision.PROCEED
    assert second.decision is StepAttemptDecision.PROCEED
    assert first.record == second.record
    assert first.attempts_used == second.attempts_used == 1


async def test_unknown_step_and_missing_plan_create_no_journal_rows(
    store, plan_store, context
) -> None:
    task = await store.create_task(submission=make_submission(context))
    started = await store.begin_task_attempt(command=_attempt_command(task.task_id))
    assert started.grant is not None
    planning = await _transition(
        store,
        task_id=task.task_id,
        expected_version=started.winner.version,
        to_status=TaskStatus.PLANNING,
        fencing_token=started.grant.fencing_token,
    )
    running = await _transition(
        store,
        task_id=task.task_id,
        expected_version=planning.winner.version,
        to_status=TaskStatus.RUNNING,
        fencing_token=started.grant.fencing_token,
    )
    assert running.applied
    missing = await store.begin_step_attempt(
        command=StepAttemptCommand(grant=started.grant, step_id="s1")
    )
    assert missing.decision is StepAttemptDecision.NOT_RUNNABLE
    assert await store.load_step_executions(task_id=task.task_id) == ()

    await plan_store.save(task_id=task.task_id, plan=FIXTURE_PLAN, target=FIXTURE_TARGET)
    unknown = await store.begin_step_attempt(
        command=StepAttemptCommand(grant=started.grant, step_id="unknown")
    )
    assert unknown.decision is StepAttemptDecision.UNKNOWN_STEP
    assert await store.load_step_executions(task_id=task.task_id) == ()


async def test_step_commit_atomically_exposes_evidence_and_is_idempotent(
    store, plan_store, evidence_ledger, context
) -> None:
    grant, _ = await _running_attempt(store, plan_store, context)
    await store.begin_step_attempt(command=StepAttemptCommand(grant=grant, step_id="s1"))
    command = StepCommitCommand(
        grant=grant,
        step_id="s1",
        kind=StepOutcomeKind.TOOL_RESULT,
        status=StepResultStatus.OK,
        evidence=_step_evidence(grant),
        audit_events=(_step_event(grant),),
    )
    committed = await store.commit_step_result(command=command)
    replay = await store.commit_step_result(command=command)
    assert committed.committed
    assert replay.committed
    assert replay.record == committed.record
    assert await evidence_ledger.load(task_id=grant.task_id) == (command.evidence,)

    changed = command.model_copy(
        update={"audit_events": (_step_event(grant, event_id="different"),)}
    )
    conflict = await store.commit_step_result(command=changed)
    assert conflict.rejection is StepCommitRejection.ALREADY_COMMITTED_DIFFERENT
    assert await evidence_ledger.load(task_id=grant.task_id) == (command.evidence,)


async def test_malformed_adapter_commit_is_terminal_without_evidence(
    store, plan_store, evidence_ledger, context
) -> None:
    grant, _ = await _running_attempt(store, plan_store, context)
    await store.begin_step_attempt(command=StepAttemptCommand(grant=grant, step_id="s1"))
    result = await store.commit_step_result(
        command=StepCommitCommand(
            grant=grant,
            step_id="s1",
            kind=StepOutcomeKind.MALFORMED_ADAPTER,
            status=StepResultStatus.FAILED,
            evidence=None,
            audit_events=(_step_event(grant),),
        )
    )
    assert result.committed
    assert result.record is not None
    assert result.record.kind is StepOutcomeKind.MALFORMED_ADAPTER
    assert result.record.evidence_id is None
    assert await evidence_ledger.load(task_id=grant.task_id) == ()


async def test_commit_without_a_matching_begin_is_rejected(store, plan_store, context) -> None:
    grant, _ = await _running_attempt(store, plan_store, context)
    result = await store.commit_step_result(
        command=StepCommitCommand(
            grant=grant,
            step_id="s1",
            kind=StepOutcomeKind.TOOL_RESULT,
            status=StepResultStatus.OK,
            evidence=_step_evidence(grant),
            audit_events=(_step_event(grant),),
        )
    )
    assert result.rejection is StepCommitRejection.NO_ATTEMPT_IN_FLIGHT
    assert result.record is None


async def test_step_budget_is_derived_from_the_stored_plan(
    store, plan_store, context, two_step_plan
) -> None:
    plan = two_step_plan.model_copy(
        update={"budget": two_step_plan.budget.model_copy(update={"max_tool_calls": 1})}
    )
    grant, _ = await _running_attempt(store, plan_store, context, plan=plan)
    first = await store.begin_step_attempt(command=StepAttemptCommand(grant=grant, step_id="s1"))
    exhausted = await store.begin_step_attempt(
        command=StepAttemptCommand(grant=grant, step_id="s2")
    )
    assert first.decision is StepAttemptDecision.PROCEED
    assert exhausted.decision is StepAttemptDecision.BUDGET_EXHAUSTED
    assert exhausted.attempts_used == 1
    assert [
        record.step_id for record in await store.load_step_executions(task_id=grant.task_id)
    ] == ["s1"]


async def test_new_grant_takeover_consumes_budget_and_old_grant_is_stale(
    store, plan_store, context, clock
) -> None:
    grant, _ = await _running_attempt(store, plan_store, context)
    first = await store.begin_step_attempt(command=StepAttemptCommand(grant=grant, step_id="s1"))
    assert first.record is not None
    clock.advance(seconds=61)
    takeover = await store.begin_task_attempt(
        command=_attempt_command(grant.task_id, owner="worker-2")
    )
    assert takeover.grant is not None
    resumed = await store.begin_step_attempt(
        command=StepAttemptCommand(grant=takeover.grant, step_id="s1")
    )
    assert resumed.decision is StepAttemptDecision.PROCEED
    assert resumed.attempts_used == 2
    assert resumed.record is not None
    assert resumed.record.attempt_count == 2

    stale = await store.commit_step_result(
        command=StepCommitCommand(
            grant=grant,
            step_id="s1",
            kind=StepOutcomeKind.TOOL_RESULT,
            status=StepResultStatus.OK,
            evidence=_step_evidence(grant),
            audit_events=(_step_event(grant),),
        )
    )
    assert stale.rejection is StepCommitRejection.STALE_FENCING


async def test_stale_fencing_and_not_runnable_can_return_no_step_row(
    store, plan_store, context
) -> None:
    grant, running = await _running_attempt(store, plan_store, context)
    forged = grant.model_copy(
        update={"lease": grant.lease.model_copy(update={"fencing_token": grant.fencing_token + 1})}
    )
    stale_begin = await store.begin_step_attempt(
        command=StepAttemptCommand(grant=forged, step_id="s1")
    )
    stale_commit = await store.commit_step_result(
        command=StepCommitCommand(
            grant=forged,
            step_id="s1",
            kind=StepOutcomeKind.TOOL_RESULT,
            status=StepResultStatus.OK,
            evidence=_step_evidence(forged),
            audit_events=(_step_event(forged),),
        )
    )
    assert stale_begin.decision is StepAttemptDecision.STALE_FENCING
    assert stale_begin.record is None
    assert stale_commit.rejection is StepCommitRejection.STALE_FENCING
    assert stale_commit.record is None

    terminal = await _transition(
        store,
        task_id=grant.task_id,
        expected_version=running.version,
        to_status=TaskStatus.FAILED,
        fencing_token=grant.fencing_token,
    )
    assert terminal.applied
    not_runnable_begin = await store.begin_step_attempt(
        command=StepAttemptCommand(grant=grant, step_id="s1")
    )
    not_runnable_commit = await store.commit_step_result(
        command=StepCommitCommand(
            grant=grant,
            step_id="s1",
            kind=StepOutcomeKind.TOOL_RESULT,
            status=StepResultStatus.OK,
            evidence=_step_evidence(grant),
            audit_events=(_step_event(grant),),
        )
    )
    assert not_runnable_begin.decision is StepAttemptDecision.NOT_RUNNABLE
    assert not_runnable_begin.record is None
    assert not_runnable_commit.rejection is StepCommitRejection.NOT_RUNNABLE
    assert not_runnable_commit.record is None
    assert await store.load_step_executions(task_id=grant.task_id) == ()


async def test_full_budget_allows_paid_reentry_and_commit(
    store, plan_store, context, two_step_plan
) -> None:
    plan = two_step_plan.model_copy(
        update={"budget": two_step_plan.budget.model_copy(update={"max_tool_calls": 1})}
    )
    grant, _ = await _running_attempt(store, plan_store, context, plan=plan)
    command = StepAttemptCommand(grant=grant, step_id="s1")
    first = await store.begin_step_attempt(command=command)
    reentry = await store.begin_step_attempt(command=command)
    assert first.decision is StepAttemptDecision.PROCEED
    assert reentry.decision is StepAttemptDecision.PROCEED
    assert reentry.attempts_used == 1

    committed = await store.commit_step_result(
        command=StepCommitCommand(
            grant=grant,
            step_id="s1",
            kind=StepOutcomeKind.TOOL_RESULT,
            status=StepResultStatus.OK,
            evidence=_step_evidence(grant),
            audit_events=(_step_event(grant),),
        )
    )
    assert committed.committed


async def test_full_budget_blocks_a_new_grant_takeover(
    store, plan_store, context, clock, two_step_plan
) -> None:
    plan = two_step_plan.model_copy(
        update={"budget": two_step_plan.budget.model_copy(update={"max_tool_calls": 1})}
    )
    grant, _ = await _running_attempt(store, plan_store, context, plan=plan)
    first = await store.begin_step_attempt(command=StepAttemptCommand(grant=grant, step_id="s1"))
    assert first.record is not None
    clock.advance(seconds=61)
    takeover = await store.begin_task_attempt(
        command=_attempt_command(grant.task_id, owner="worker-2")
    )
    assert takeover.grant is not None
    exhausted = await store.begin_step_attempt(
        command=StepAttemptCommand(grant=takeover.grant, step_id="s1")
    )
    assert exhausted.decision is StepAttemptDecision.BUDGET_EXHAUSTED
    assert exhausted.attempts_used == 1
    assert exhausted.record == first.record


async def test_committed_step_replays_and_rejects_each_semantic_change(
    store, plan_store, evidence_ledger, context
) -> None:
    grant, _ = await _running_attempt(store, plan_store, context)
    await store.begin_step_attempt(command=StepAttemptCommand(grant=grant, step_id="s1"))
    command = StepCommitCommand(
        grant=grant,
        step_id="s1",
        kind=StepOutcomeKind.TOOL_RESULT,
        status=StepResultStatus.OK,
        evidence=_step_evidence(grant),
        audit_events=(
            _step_event(grant, event_id="step-event-a"),
            _step_event(grant, event_id="step-event-b"),
        ),
    )
    committed = await store.commit_step_result(command=command)
    assert committed.committed

    changes = (
        command.model_copy(update={"status": StepResultStatus.FAILED}),
        command.model_copy(update={"evidence": _step_evidence(grant, value="q2")}),
        command.model_copy(update={"audit_events": command.audit_events[:1]}),
        command.model_copy(
            update={
                "audit_events": (
                    *command.audit_events,
                    _step_event(grant, event_id="step-event-c"),
                )
            }
        ),
    )
    for changed in changes:
        replay = await store.commit_step_result(command=changed)
        assert replay.rejection is StepCommitRejection.ALREADY_COMMITTED_DIFFERENT
        assert replay.record == committed.record

    already = await store.begin_step_attempt(command=StepAttemptCommand(grant=grant, step_id="s1"))
    assert already.decision is StepAttemptDecision.ALREADY_COMMITTED
    assert already.record == committed.record
    assert await evidence_ledger.load(task_id=grant.task_id) == (command.evidence,)


async def test_timeout_is_a_distinct_persisted_step_outcome(store, plan_store, context) -> None:
    grant, _ = await _running_attempt(store, plan_store, context)
    await store.begin_step_attempt(command=StepAttemptCommand(grant=grant, step_id="s1"))
    result = await store.commit_step_result(
        command=StepCommitCommand(
            grant=grant,
            step_id="s1",
            kind=StepOutcomeKind.TOOL_RESULT,
            status=StepResultStatus.TIMEOUT,
            evidence=_step_evidence(grant),
            audit_events=(_step_event(grant),),
        )
    )
    assert result.committed
    assert result.record is not None
    assert result.record.result_status is StepResultStatus.TIMEOUT


# --- 分组 ---------------------------------------------------------------------
#
# 分组决定用例落在哪个绑定模块，从而决定它带不带 ``security`` marker。分组之间不得
# 重叠，并集必须覆盖本模块定义的全部用例——两条都由 test_task_store_bindings.py
# 承重，因为"漏进分组"的用例在所有绑定里都收不到，看起来像写了测试，实际一次没跑。

CONTRACT_CASES = (
    test_create_is_idempotent_by_key,
    test_new_task_has_initial_execution_accounting,
    test_created_sequence_is_strictly_increasing,
    test_retry_with_a_new_request_id_reuses_the_same_task,
    test_idempotency_key_is_scoped_per_tenant,
    test_idempotency_key_is_scoped_per_environment,
    test_same_key_with_a_different_request_is_rejected,
    test_envelope_context_mismatch_is_rejected,
    test_envelope_without_environment_id_is_accepted,
    test_get_unknown_task_raises,
    test_get_rejects_the_wrong_tenant_without_leaking_existence,
    test_get_rejects_the_wrong_environment_without_leaking_existence,
    test_bare_task_id_is_not_a_supported_read_shape,
    test_cas_success_bumps_version,
    test_cas_failure_returns_the_storage_winner,
    test_illegal_transition_is_rejected,
    test_terminal_reason_is_cleared_when_a_transition_omits_it,
    test_first_approval_of_a_task_is_seq_one,
    test_approval_seq_increases_within_a_task,
    test_approval_seq_is_scoped_per_task,
    test_first_audit_event_of_a_task_is_seq_one,
    test_audit_seq_increases_within_a_task,
    test_audit_seq_is_scoped_per_task,
    test_approval_and_audit_seq_are_independent,
    test_audit_event_without_a_task_id_is_rejected,
    test_audit_events_do_not_require_the_task_to_exist,
)

LEASE_FENCING_CASES = (
    test_second_holder_is_refused_while_lease_is_live,
    test_expired_lease_can_be_taken_over_with_a_higher_token,
    test_renew_keeps_the_same_token,
    test_renew_by_a_non_owner_is_refused,
    test_renew_with_a_wrong_token_is_refused,
    test_renew_after_expiry_is_refused,
    test_stale_token_write_is_rejected,
    test_write_without_token_under_live_lease_is_rejected,
    test_token_without_any_live_lease_is_rejected,
    test_transition_without_any_lease_is_allowed,
    test_terminal_task_cannot_acquire_lease,
    test_stale_token_is_reported_before_version_mismatch,
    test_expired_lease_cannot_be_bypassed_by_omitting_the_token,
    test_expired_lease_cannot_be_used_with_its_old_token_either,
    test_expected_version_rejects_bool,
    test_fencing_token_rejects_bool,
    test_to_status_rejects_a_plain_string,
    test_acquire_lease_rejects_non_positive_ttl,
    test_acquire_lease_rejects_bytes_owner,
    test_invalid_lease_arguments_do_not_mutate_the_store,
    test_a_task_that_was_never_leased_is_not_stale,
    test_a_live_lease_is_not_stale,
    test_an_expired_lease_is_stale,
    test_a_terminal_task_is_never_stale,
    test_stale_leases_are_ordered_by_expiry_then_task_id,
    test_stale_leases_with_the_same_expiry_are_ordered_by_task_id,
    test_stale_leases_respect_the_limit,
    test_a_terminal_task_cannot_crowd_out_a_stale_one,
    test_listing_stale_leases_claims_nothing,
    test_list_stale_leases_rejects_a_bad_limit,
)

TERMINAL_PROTECTION_CASES = (
    test_no_transition_out_of_any_terminal_status,
    test_terminal_protection_wins_over_version_mismatch,
)

DISPATCH_ATTEMPT_CASES = (
    test_dispatch_filters_before_applying_the_limit,
    test_dispatch_is_scope_isolated_and_stably_ordered,
    test_begin_attempt_returns_the_original_submission_and_one_grant,
    test_attempt_intents_have_disjoint_status_sets,
    test_schedule_retry_is_idempotent_and_immediately_fences_the_grant,
    test_retry_is_not_due_until_the_exact_boundary,
    test_retry_accepts_a_grant_that_failed_during_planning,
    test_expired_grant_cannot_schedule_retry,
    test_retry_exhaustion_terminalizes_before_allocating_a_fourth_grant,
    test_terminal_attempt_rejection_never_mutates_the_winner,
)

STEP_EXECUTION_CASES = (
    test_step_begin_is_idempotent_within_one_grant,
    test_unknown_step_and_missing_plan_create_no_journal_rows,
    test_step_commit_atomically_exposes_evidence_and_is_idempotent,
    test_malformed_adapter_commit_is_terminal_without_evidence,
    test_commit_without_a_matching_begin_is_rejected,
    test_step_budget_is_derived_from_the_stored_plan,
    test_new_grant_takeover_consumes_budget_and_old_grant_is_stale,
    test_stale_fencing_and_not_runnable_can_return_no_step_row,
    test_full_budget_allows_paid_reentry_and_commit,
    test_full_budget_blocks_a_new_grant_takeover,
    test_committed_step_replays_and_rejects_each_semantic_change,
    test_timeout_is_a_distinct_persisted_step_outcome,
)

ALL_GROUPS = {
    "contract": CONTRACT_CASES,
    "lease_fencing": LEASE_FENCING_CASES,
    "terminal_protection": TERMINAL_PROTECTION_CASES,
    "dispatch_attempt": DISPATCH_ATTEMPT_CASES,
    "step_execution": STEP_EXECUTION_CASES,
}
