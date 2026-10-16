"""两条 upgrade 路径都要验证（DEVELOPMENT_PLAN §7 M4 测试门明文）。

空库能建起来只证明了一半：真正会出问题的是**已有数据**的库——一条加了 NOT NULL
但没给默认值的列、一条与既有行冲突的 CHECK，都只在有数据时才炸，而那正是生产
环境的形态。
"""

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei_agent.persistence.migrations.guards import MigrationSafetyError
from xiaowei_agent.persistence.schema import (
    ALL_TABLES,
    CREATED_SEQUENCE_NAME,
    FENCING_SEQUENCE_NAME,
)


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        )
        return {row[0] for row in rows}


async def test_upgrade_on_an_empty_database_creates_every_table(
    clean_database: AsyncEngine,
) -> None:
    present = await _table_names(clean_database)
    assert {table.name for table in ALL_TABLES} <= present


async def test_upgrade_creates_the_fencing_sequence(clean_database: AsyncEngine) -> None:
    async with clean_database.connect() as connection:
        rows = await connection.execute(
            sa.text("SELECT sequencename FROM pg_sequences WHERE schemaname = current_schema()")
        )
        names = {row[0] for row in rows}
        assert FENCING_SEQUENCE_NAME in names
        assert CREATED_SEQUENCE_NAME in names


async def test_rev_0002_round_trips_m4_data_and_restores_the_old_conflict_target(
    clean_database: AsyncEngine, alembic_runners: tuple[Any, Any], store: Any, context: Any
) -> None:
    """降到 M4 后，旧代码使用的约束名与 SQL 形状都必须真的可用。"""
    from tests.conftest import make_submission

    await store.create_task(submission=make_submission(context))
    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0001_initial", True)
        constraints = await connection.execute(
            sa.text("SELECT conname FROM pg_constraint WHERE conrelid = 'tasks'::regclass")
        )
        assert "uq_tasks_idempotency_scope" in {row[0] for row in constraints}
        await connection.execute(
            sa.text(
                "INSERT INTO tasks "
                "(task_id, tenant_id, environment_id, actor, idempotency_key, "
                "request_digest, status, version) "
                "VALUES (:task_id, :tenant_id, :environment_id, :actor, :key, "
                ":digest, 'created', 0) "
                "ON CONFLICT ON CONSTRAINT uq_tasks_idempotency_scope DO NOTHING"
            ),
            {
                "task_id": "m4-shape-task",
                "tenant_id": context.tenant_id,
                "environment_id": context.environment_id,
                "actor": context.actor,
                "key": "m4-shape-key",
                "digest": "d" * 64,
            },
        )
        await connection.run_sync(run_upgrade, "head")

    async with clean_database.connect() as connection:
        row = await connection.execute(
            sa.text(
                "SELECT created_seq, attempt_number, task_failure_count, next_attempt_at "
                "FROM tasks WHERE task_id = 'm4-shape-task'"
            )
        )
        values = row.one()
        assert values[0] > 0
        assert values[1:] == (0, 0, None)


async def test_downgrade_then_upgrade_over_a_database_with_data(
    clean_database: AsyncEngine, alembic_runners: tuple[Any, Any], store: Any, context: Any
) -> None:
    """有数据的库：先写入真实数据，再 downgrade 到底，再 upgrade 回来。

    ``downgrade`` 会连数据一起删除——这是**预期**行为，本条断言的是"回滚不会卡在
    残留对象上"，不是"数据能存活"。残留的表会让下一次 upgrade 在"已存在"上失败，
    把一次可回滚的迁移变成需要人工清理的死局。
    """
    from tests.conftest import make_submission

    await store.create_task(submission=make_submission(context))
    async with clean_database.connect() as connection:
        count = await connection.execute(sa.text("SELECT count(*) FROM tasks"))
        assert count.scalar_one() == 1

    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "base", True)
    assert not ({table.name for table in ALL_TABLES} & await _table_names(clean_database))

    async with clean_database.begin() as connection:
        await connection.run_sync(run_upgrade)
    assert {table.name for table in ALL_TABLES} <= await _table_names(clean_database)


async def test_lease_check_constraint_is_enforced_by_the_database(
    clean_database: AsyncEngine, store: Any, context: Any
) -> None:
    """数据库层的 CHECK 与契约层的校验器是**两层独立表达**。

    绕过 ``TaskStore`` 直接写半置位的租约必须被数据库拒绝——只有这样"两层"才不是
    一句空话。测试可以够到存储细节，生产代码不行。
    """
    from tests.conftest import make_submission

    record = await store.create_task(submission=make_submission(context))
    with pytest.raises(sa.exc.IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text("UPDATE tasks SET lease_owner = 'w1' WHERE task_id = :task_id"),
                {"task_id": record.task_id},
            )


async def test_rev_0003_settles_only_non_terminal_m4_tasks_without_audit(
    clean_database: AsyncEngine, alembic_runners: tuple[Any, Any]
) -> None:
    """历史提交事实不可伪造；迁移只终态化仍可能执行的任务。"""
    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0002_task_execution_columns")
        for task_id, status, version, created_seq in (
            ("legacy-active", "created", 3, 1001),
            ("legacy-terminal", "succeeded", 5, 1002),
        ):
            await connection.execute(
                sa.text(
                    "INSERT INTO tasks "
                    "(task_id, tenant_id, environment_id, actor, idempotency_key, "
                    "request_digest, status, version, created_seq, "
                    "idempotency_scope_digest) VALUES "
                    "(:task_id, 'tenant-a', 'dev', 'alice', :key, :digest, :status, "
                    ":version, :created_seq, :scope_digest)"
                ),
                {
                    "task_id": task_id,
                    "key": f"key-{task_id}",
                    "digest": "d" * 64,
                    "status": status,
                    "version": version,
                    "created_seq": created_seq,
                    "scope_digest": ("a" if task_id == "legacy-active" else "b") * 64,
                },
            )
        await connection.run_sync(run_upgrade)

    async with clean_database.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT task_id, status, version, terminal_reason FROM tasks "
                    "WHERE task_id LIKE 'legacy-%' ORDER BY task_id"
                )
            )
        ).all()
        audit_count = await connection.scalar(
            sa.text(
                "SELECT count(*) FROM task_audit_events "
                "WHERE task_id IN ('legacy-active', 'legacy-terminal')"
            )
        )
    assert rows == [
        ("legacy-active", "failed", 4, "legacy_task_without_submission"),
        ("legacy-terminal", "succeeded", 5, None),
    ]
    assert audit_count == 0


async def test_rev_0003_downgrade_rejects_submission_data_by_default(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    store: Any,
    context: Any,
) -> None:
    from tests.conftest import make_submission

    await store.create_task(submission=make_submission(context))
    _, run_downgrade = alembic_runners
    with pytest.raises(MigrationSafetyError):
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0002_task_execution_columns")

    async with clean_database.connect() as connection:
        assert await connection.scalar(sa.text("SELECT count(*) FROM task_submissions")) == 1
        revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert revision == "0005_step_journal"


async def test_explicit_rev_0003_downgrade_settles_active_m5_data(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    store: Any,
    context: Any,
) -> None:
    from tests.conftest import make_submission

    task = await store.create_task(submission=make_submission(context))
    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0002_task_execution_columns", True)
        row = (
            await connection.execute(
                sa.text("SELECT status, terminal_reason FROM tasks WHERE task_id = :task_id"),
                {"task_id": task.task_id},
            )
        ).one()
        assert row == ("failed", "m5_downgrade_discarded")
        await connection.run_sync(run_upgrade)
        restored = (
            await connection.execute(
                sa.text("SELECT status, terminal_reason FROM tasks WHERE task_id = :task_id"),
                {"task_id": task.task_id},
            )
        ).one()
    assert restored == ("failed", "m5_downgrade_discarded")


async def test_rev_0005_downgrade_requires_authorization_and_settles_active_data(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    store: Any,
    context: Any,
) -> None:
    """A step journal row cannot be discarded while its task still looks runnable."""
    from tests.conftest import make_submission

    task = await store.create_task(submission=make_submission(context))
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO task_step_executions "
                "(task_id, step_id, attempt_count, last_fencing_token, started_at) "
                "VALUES (:task_id, 's1', 1, 1, now())"
            ),
            {"task_id": task.task_id},
        )

    run_upgrade, run_downgrade = alembic_runners
    with pytest.raises(MigrationSafetyError):
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0004_retry_markers")

    async with clean_database.connect() as connection:
        revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        step_count = await connection.scalar(sa.text("SELECT count(*) FROM task_step_executions"))
    assert revision == "0005_step_journal"
    assert step_count == 1

    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0004_retry_markers", True)
        settled = (
            await connection.execute(
                sa.text("SELECT status, terminal_reason FROM tasks WHERE task_id = :task_id"),
                {"task_id": task.task_id},
            )
        ).one()
        await connection.run_sync(run_upgrade)
    assert settled == ("failed", "m5_downgrade_discarded")

    async with clean_database.connect() as connection:
        assert (
            await connection.scalar(
                sa.text("SELECT count(*) FROM task_step_executions WHERE task_id = :task_id"),
                {"task_id": task.task_id},
            )
            == 0
        )


async def test_rev_0004_retry_columns_round_trip_without_changing_task_facts(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    task: Any,
) -> None:
    """rev4 的 downgrade/upgrade 只移除重试幂等标记，不改既有任务事实。"""
    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        before = (
            await connection.execute(
                sa.text(
                    "SELECT task_id, status, version, attempt_number, task_failure_count "
                    "FROM tasks WHERE task_id = :task_id"
                ),
                {"task_id": task.task_id},
            )
        ).one()
        await connection.run_sync(run_downgrade, "0003_task_submissions")
        columns_after_down = {
            row[0]
            for row in (
                await connection.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'tasks'"
                    )
                )
            ).all()
        }
        assert "retry_scheduled_by_attempt" not in columns_after_down
        assert "retry_command_digest" not in columns_after_down

        await connection.run_sync(run_upgrade)
        after = (
            await connection.execute(
                sa.text(
                    "SELECT task_id, status, version, attempt_number, task_failure_count "
                    "FROM tasks WHERE task_id = :task_id"
                ),
                {"task_id": task.task_id},
            )
        ).one()
    assert after == before
