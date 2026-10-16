"""迁移与 ``schema.py`` 必须描述同一套表 —— **离线比对，不需要数据库**。

M4 有意让 schema 出现在两处：``persistence/schema.py`` 是活的（``PostgresTaskStore``
用它构造语句），迁移是冻结的历史快照（回放它必须建出**当初**那套表，而不是今天的）。
两者都必要，因此漂移是必然风险，不是可以靠纪律避免的偶发错误。

漂移的后果不是报错而是**沉默的错位**：迁移建出的表少一个 CHECK，而代码以为它在，
于是"数据库层独立表达同一不变量"这条对价直接落空，且所有内存用例照常全绿。

本文件用 Alembic 的**离线模式**（``upgrade --sql``）拿到迁移真正会发出的 DDL，再把
``schema.py`` 的每张表编译成同一方言的 DDL，逐表比较。离线模式不连数据库，因此这条
检查在默认测试路径上就能跑——不必等到有 PostgreSQL 才发现两边不一致。
"""

import contextlib
import io
import re
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from xiaowei_agent.persistence.schema import (
    ALL_TABLES,
    CREATED_SEQUENCE_NAME,
    FENCING_SEQUENCE_NAME,
    TASK_STEP_EXECUTIONS,
    TASK_SUBMISSIONS,
    TASKS,
)

_ROOT = Path(__file__).resolve().parents[2]

# 迁移自己的记账表，不属于业务 schema，比较时排除。
_ALEMBIC_BOOKKEEPING = "alembic_version"


def _offline_upgrade_sql() -> str:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(_ROOT / config.get_main_option("script_location", ""))
    )
    # 离线模式不建立连接，URL 只用于选方言。此处必须是一个**不可路由的占位**，
    # 而不是任何真实实例——离线模式不连它，但把真实地址写进测试等于把连接串提交
    # 进仓库。
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline/offline")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


def _normalise(statement: str) -> str:
    """折叠空白与括号旁的空格，去掉尾分号——只比较结构，不比较排版。

    括号旁的空格纯属排版：Alembic 与 ``CreateTable`` 的换行位置不同，但那不是漂移。
    折叠范围**只限空白**，任何标识符、类型、约束文本的差异都会保留下来。
    """
    collapsed = re.sub(r"\s+", " ", statement).strip().rstrip(";").strip()
    return collapsed.replace("( ", "(").replace(" )", ")")


def _create_table_statements(sql: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.DOTALL):
        name = match.group(1)
        if name != _ALEMBIC_BOOKKEEPING:
            found[name] = _normalise(f"CREATE TABLE {name} ({match.group(2)})")
    return found


def test_migration_creates_exactly_the_tables_declared_in_schema() -> None:
    """表集合相等。少一张会让代码在运行时才发现表不存在，多一张则无人维护。"""
    assert set(_create_table_statements(_offline_upgrade_sql())) == {
        table.name for table in ALL_TABLES
    }


def test_each_table_ddl_matches_the_schema_module() -> None:
    """逐表比较完整 DDL：列、类型、可空性、主键、唯一约束、CHECK 全部在内。

    比 "表名对得上" 严格得多——漂移几乎总是发生在某一条约束上，而不是整张表上。
    """
    emitted = _create_table_statements(_offline_upgrade_sql())
    dialect = postgresql.dialect()
    for table in ALL_TABLES:
        if table is TASKS:
            continue
        expected = _normalise(str(CreateTable(table).compile(dialect=dialect)))
        assert emitted[table.name] == expected, f"{table.name} 的迁移 DDL 与 schema.py 不一致"


def test_task_head_has_all_declared_columns_and_constraints() -> None:
    """后续 revision 用 ALTER 演进 tasks；head 仍须与活 schema 的名称集合一致。"""
    sql = _offline_upgrade_sql()
    declared_columns = {column.name for column in TASKS.columns}
    for column in declared_columns:
        assert re.search(rf"\b{re.escape(column)}\b", sql), column
    declared_constraints = {item.name for item in TASKS.constraints if item.name is not None}
    for constraint in declared_constraints:
        assert constraint in sql


def test_m5_execution_columns_are_declared_with_expected_types() -> None:
    assert isinstance(TASKS.c.created_seq.type, sa.BigInteger)
    assert isinstance(TASKS.c.attempt_number.type, sa.BigInteger)
    assert isinstance(TASKS.c.task_failure_count.type, sa.BigInteger)
    assert isinstance(TASKS.c.next_attempt_at.type, sa.DateTime)
    assert TASKS.c.created_seq.nullable is False
    assert TASKS.c.attempt_number.nullable is False
    assert TASKS.c.task_failure_count.nullable is False
    assert TASKS.c.next_attempt_at.nullable is True


def test_m5_replaces_the_variable_width_idempotency_constraint() -> None:
    names = {item.name for item in TASKS.constraints if item.name is not None}
    assert "uq_tasks_idempotency_scope_digest" in names
    assert "uq_tasks_idempotency_scope" not in names
    assert isinstance(TASKS.c.idempotency_scope_digest.type, sa.CHAR)
    assert TASKS.c.idempotency_scope_digest.type.length == 64


def test_migration_creates_the_fencing_sequence() -> None:
    """fencing token 的单调性由序列提供；序列没建出来，acquire_lease 会在运行时才炸。"""
    assert f"CREATE SEQUENCE {FENCING_SEQUENCE_NAME}" in _offline_upgrade_sql()
    assert f"CREATE SEQUENCE {CREATED_SEQUENCE_NAME}" in _offline_upgrade_sql()


def test_rev_0002_has_the_expected_revision_chain() -> None:
    from xiaowei_agent.persistence.migrations.versions import (
        rev_0002_task_execution_columns as revision,
    )

    assert revision.revision == "0002_task_execution_columns"
    assert revision.down_revision == "0001_initial"


def test_rev_0003_has_the_expected_revision_chain() -> None:
    from xiaowei_agent.persistence.migrations.versions import (
        rev_0003_task_submissions as revision,
    )

    assert revision.revision == "0003_task_submissions"
    assert revision.down_revision == "0002_task_execution_columns"


def test_rev_0004_has_the_expected_revision_chain() -> None:
    from xiaowei_agent.persistence.migrations.versions import (
        rev_0004_retry_markers as revision,
    )

    assert revision.revision == "0004_retry_markers"
    assert revision.down_revision == "0003_task_submissions"


def test_retry_markers_are_nullable_but_pairwise_constrained() -> None:
    assert isinstance(TASKS.c.retry_scheduled_by_attempt.type, sa.BigInteger)
    assert isinstance(TASKS.c.retry_command_digest.type, sa.Text)
    assert TASKS.c.retry_scheduled_by_attempt.nullable is True
    assert TASKS.c.retry_command_digest.nullable is True
    names = {item.name for item in TASKS.constraints if item.name is not None}
    assert "ck_tasks_retry_markers_consistent" in names
    assert "ck_tasks_retry_attempt_not_future" in names


def test_rev_0005_has_the_expected_revision_chain() -> None:
    from xiaowei_agent.persistence.migrations.versions import (
        rev_0005_step_journal as revision,
    )

    assert revision.revision == "0005_step_journal"
    assert revision.down_revision == "0004_retry_markers"


def test_step_journal_has_the_complete_self_describing_shape() -> None:
    assert set(TASK_STEP_EXECUTIONS.columns.keys()) == {
        "task_id",
        "step_id",
        "attempt_count",
        "last_fencing_token",
        "result_status",
        "kind",
        "evidence_id",
        "commit_digest",
        "started_at",
        "committed_at",
    }
    assert TASK_STEP_EXECUTIONS.primary_key.columns.keys() == ["task_id", "step_id"]
    names = {item.name for item in TASK_STEP_EXECUTIONS.constraints if item.name is not None}
    assert names >= {
        "ck_task_steps_attempt_count_positive",
        "ck_task_steps_fencing_token_positive",
        "ck_task_steps_terminal_fields_consistent",
        "ck_task_steps_evidence_kind_consistent",
        "ck_task_steps_result_shape",
    }


def test_submission_table_has_one_row_per_task_and_complete_facts() -> None:
    assert TASK_SUBMISSIONS.primary_key.columns.keys() == ["task_id"]
    assert set(TASK_SUBMISSIONS.columns.keys()) == {
        "task_id",
        "envelope",
        "context",
        "as_of",
        "submission_digest",
    }
    assert TASK_SUBMISSIONS.c.envelope.nullable is False
    assert TASK_SUBMISSIONS.c.context.nullable is False
    assert TASK_SUBMISSIONS.c.as_of.nullable is False
    assert TASK_SUBMISSIONS.c.submission_digest.nullable is False


def test_rev_0002_backfills_before_enforcing_constraints() -> None:
    sql = _offline_upgrade_sql()
    backfill = sql.index("row_number() OVER (ORDER BY task_id)")
    not_null = sql.index("ALTER COLUMN created_seq SET NOT NULL")
    assert backfill < not_null
    assert "setval('task_created_seq'" in sql
    assert "uq_tasks_idempotency_scope_digest" in sql
    assert "DROP CONSTRAINT uq_tasks_idempotency_scope" in sql


def test_downgrade_drops_everything_upgrade_created() -> None:
    """``downgrade()`` 必须能把库还原干净。

    残留的表会让下一次 ``upgrade`` 在"已存在"上失败，把一次可回滚的迁移变成需要
    人工清理的死局。
    """
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(_ROOT / config.get_main_option("script_location", ""))
    )
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline/offline")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        command.downgrade(config, "head:base", sql=True)
    emitted = buffer.getvalue()
    for table in ALL_TABLES:
        assert f"DROP TABLE {table.name}" in emitted
    assert f"DROP SEQUENCE {FENCING_SEQUENCE_NAME}" in emitted


def test_task_id_columns_are_text_not_uuid() -> None:
    """``TaskRecord.task_id`` 是 ``StrictStr``；``uuid`` 列读回的是 ``UUID`` 对象。

    strict 模式会直接拒绝那个对象。写 DDL 时"主键当然用 uuid"的直觉在这里恰好是
    错的，因此钉死。
    """
    for table in ALL_TABLES:
        column = table.c["task_id"]
        assert isinstance(column.type, sa.Text), f"{table.name}.task_id 必须是 text"
