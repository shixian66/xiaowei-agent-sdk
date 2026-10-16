"""并发裁决与崩溃恢复 —— **必须用多个真实连接**。

``asyncio.gather`` 共用一个连接不算并发：它证明的是"同一个会话里两次顺序调用"，
而 CAS 与幂等唯一键要防的恰恰是**两个会话**。因此这里每个参与者拿一个独立的
``AsyncEngine``（见 ``independent_stores``），把"是不是真的并发"变成结构上确定的
事，而不是靠推断连接池行为。

这一整个文件是 M4 判定标准 2、3、4 的唯一证据来源。内存实现在这些场景下**无话可
说**——它只有单进程保证，这正是 M4 存在的理由。
"""

import asyncio
import contextlib
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.conftest import lookup_for, make_envelope, make_submission

from xiaowei_agent.contracts import TaskStatus, TransitionRejection
from xiaowei_agent.persistence.store import TransitionCommand

pytestmark = pytest.mark.security

_CONCURRENCY = 8
"""并发度。

取 8 而不是 2：这些用例的反证要求"冲突**必然**发生"，而不是"大概率发生"。并发度
太低会让反证变成偶发失败，而一个偶发失败的反证和没有反证一样没用。
"""


async def test_exactly_one_writer_wins_a_concurrent_cas(
    store: Any, task: Any, independent_stores: Any
) -> None:
    """N 个连接同时 CAS 同一版本 → 恰一个 applied，其余全是 VERSION_MISMATCH。

    并且**所有失败者拿到的 winner 必须一致**——只知道"失败了"不够，调用方必须能
    拿到存储层裁定的那条记录，否则它只能拿本地旧对象继续跑（ARCHITECTURE §7.3
    明令禁止）。
    """
    writers = independent_stores(_CONCURRENCY)
    results = await asyncio.gather(
        *(
            writer.transition(
                command=TransitionCommand(
                    task_id=task.task_id,
                    expected_version=task.version,
                    to_status=TaskStatus.PLANNING,
                )
            )
            for writer in writers
        )
    )
    applied = [result for result in results if result.applied]
    rejected = [result for result in results if not result.applied]
    assert len(applied) == 1
    assert {result.rejection for result in rejected} == {TransitionRejection.VERSION_MISMATCH}
    assert {result.winner.version for result in rejected} == {applied[0].winner.version}
    assert {result.winner.status for result in rejected} == {TaskStatus.PLANNING}


async def test_concurrent_creates_with_one_key_produce_one_task(
    context: Any, independent_stores: Any
) -> None:
    """**并发重复请求只产生一个任务事实。**（判定标准 3）

    这是 DEVELOPMENT_PLAN §7 M4 退出标准的第一句。既有用例只覆盖**串行**重复创建；
    并发路径在 M4 之前没有任何覆盖。

    全部调用必须返回同一个 ``task_id``，且**没有一个**把数据库唯一键冲突抛给调用
    方——调用方该看到的是"你的任务已经在这儿了"，不是 IntegrityError。
    """
    creators = independent_stores(_CONCURRENCY)
    envelope = make_envelope(idempotency_key="concurrent-idem")
    submission = make_submission(context, envelope=envelope)
    records = await asyncio.gather(
        *(creator.create_task(submission=submission) for creator in creators)
    )
    assert len({record.task_id for record in records}) == 1


async def test_only_one_task_row_exists_after_concurrent_creates(
    clean_database: AsyncEngine, context: Any, independent_stores: Any
) -> None:
    """直接读表确认只有一行——返回值一致还可能是"各建各的、恰好返回了同一个"。"""
    creators = independent_stores(_CONCURRENCY)
    envelope = make_envelope(idempotency_key="concurrent-idem")
    submission = make_submission(context, envelope=envelope)
    await asyncio.gather(*(creator.create_task(submission=submission) for creator in creators))
    async with clean_database.connect() as connection:
        count = await connection.execute(sa.text("SELECT count(*) FROM tasks"))
        assert count.scalar_one() == 1


async def test_preempted_worker_cannot_write_with_its_old_token(
    store: Any, task: Any, clock: Any, independent_stores: Any
) -> None:
    """租约过期被另一 worker 抢占后，旧 token 的写入必须被拒。

    这条在内存实现上已有用例，但那里"另一个 worker"其实是同一个进程里的同一个对象。
    这里两个 worker 各持一个连接。
    """
    old_worker, new_worker = independent_stores(2)
    old = await old_worker.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    new = await new_worker.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30)
    assert new is not None
    assert new.fencing_token > old.fencing_token

    current = await store.get(lookup=lookup_for(task))
    rejected = await old_worker.transition(
        command=TransitionCommand(
            task_id=task.task_id,
            expected_version=current.version,
            to_status=TaskStatus.PLANNING,
            fencing_token=old.fencing_token,
        )
    )
    assert rejected.applied is False
    assert rejected.rejection is TransitionRejection.STALE_FENCING_TOKEN


async def test_a_late_event_cannot_reopen_a_terminal_task(
    store: Any, task: Any, independent_stores: Any
) -> None:
    """终态之后到达的事件必须被拒，且终态不变。"""
    (latecomer,) = independent_stores(1)
    done = await store.transition(
        command=TransitionCommand(
            task_id=task.task_id,
            expected_version=task.version,
            to_status=TaskStatus.CANCELED,
        )
    )
    assert done.applied is True
    late = await latecomer.transition(
        command=TransitionCommand(
            task_id=task.task_id,
            expected_version=done.winner.version,
            to_status=TaskStatus.RUNNING,
        )
    )
    assert late.applied is False
    assert late.rejection is TransitionRejection.TERMINAL_PROTECTED
    assert (await store.get(lookup=lookup_for(task))).status is TaskStatus.CANCELED


async def test_a_killed_backend_leaves_no_intermediate_state(
    clean_database: AsyncEngine, store: Any, task: Any
) -> None:
    """事务执行中途断连 → 状态要么是事务前的值，要么是事务后的值。（判定标准 4）

    用 ``pg_terminate_backend`` 真的杀掉那个后端进程，而不是客户端侧 rollback：
    后者证明的是"我们记得回滚"，前者证明的是"我们不回滚也不会留下中间态"。生产
    里进程是被 OOM killer 和 pod 驱逐杀掉的，没人记得回滚。

    不存在"版本已增而状态未改"或"租约字段部分置位"的记录——后者由
    ``ck_tasks_lease_fields_consistent`` 与本条共同保证。
    """
    before = await store.get(lookup=lookup_for(task))
    victim = await clean_database.connect()
    try:
        # ``begin()`` 必须排在**第一个** execute 之前：SQLAlchemy 2.0 的 connection
        # 在首次 execute 时 autobegin，之后再调用 begin() 会抛 InvalidRequestError。
        # 这条用例此前把取 pid 排在前面，因此从写出来那天起就不可能跑通——本机没有
        # PostgreSQL，它一直是 skipped，直到第一次真跑 integration 才暴露。
        transaction = await victim.begin()
        pid = (await victim.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one()
        await victim.execute(
            sa.text(
                "UPDATE tasks SET status = 'planning', version = version + 1"
                " WHERE task_id = :task_id"
            ),
            {"task_id": task.task_id},
        )
        async with clean_database.connect() as killer:
            await killer.execute(sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        # 后端已被杀，提交必须失败。若它"成功"了，说明我们杀的不是那条连接，
        # 这条用例就什么也没证明——因此这里断言的是失败本身。
        with pytest.raises(sa.exc.SQLAlchemyError):
            await transaction.commit()
    finally:
        with contextlib.suppress(sa.exc.SQLAlchemyError):
            await victim.close()

    after = await store.get(lookup=lookup_for(task))
    assert after == before
    assert after.version == before.version
    assert after.status is before.status
