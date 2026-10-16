"""迁移入口复用 AsyncEngine 桥，失败不泄漏连接信息。"""

import io
from collections.abc import Callable

import pytest
import sqlalchemy as sa
from alembic.util.exc import CommandError
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from xiaowei_agent.config import Settings
from xiaowei_agent.interfaces import migrate
from xiaowei_agent.persistence.migrations import runner
from xiaowei_agent.persistence.migrations.runner import (
    MigrationCompatibilityError,
    run_downgrade,
    run_upgrade,
)


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[Callable[..., None], tuple[object, ...]]] = []

    async def run_sync(self, function: Callable[..., None], *args: object) -> None:
        self.calls.append((function, args))


class _Begin:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class _Engine:
    def __init__(self) -> None:
        self.connection = _Connection()
        self.disposed = False

    def begin(self) -> _Begin:
        return _Begin(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_upgrade_and_downgrade_use_the_same_async_bridge() -> None:
    settings = Settings(environment_id="dev")
    upgrade_engine = _Engine()
    await migrate.execute_migration(
        settings=settings,
        revision="head",
        downgrade=False,
        allow_destructive=False,
        engine_factory=lambda _: upgrade_engine,
    )
    assert upgrade_engine.connection.calls == [(run_upgrade, ("head",))]
    assert upgrade_engine.disposed is True

    downgrade_engine = _Engine()
    await migrate.execute_migration(
        settings=settings,
        revision="0001_initial",
        downgrade=True,
        allow_destructive=True,
        engine_factory=lambda _: downgrade_engine,
    )
    assert downgrade_engine.connection.calls == [(run_downgrade, ("0001_initial", True))]
    assert downgrade_engine.disposed is True


def test_main_failure_is_constant_and_does_not_echo_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = "private-" + "database-value"

    def fail() -> Settings:
        raise RuntimeError(private)

    monkeypatch.setattr(migrate, "load_settings", fail)
    stderr = io.StringIO()
    assert migrate.main([], stderr=stderr) == 1
    assert stderr.getvalue() == "xiaowei-migrate: migration_failed\n"
    assert private not in stderr.getvalue()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            sa.exc.OperationalError("statement", {"value": "private"}, RuntimeError("private")),
            "xiaowei-migrate: database_unavailable\n",
        ),
        (
            sa.exc.ProgrammingError("statement", {}, RuntimeError()),
            "xiaowei-migrate: database_error\n",
        ),
        (CommandError("private"), "xiaowei-migrate: migration_command_error\n"),
        (PermissionError("private"), "xiaowei-migrate: io_error\n"),
    ],
)
def test_main_classifies_migration_failures_without_driver_text(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: str,
) -> None:
    async def fail(**_: object) -> None:
        raise error

    monkeypatch.setattr(migrate, "load_settings", lambda: Settings(environment_id="dev"))
    monkeypatch.setattr(migrate, "execute_migration", fail)
    stderr = io.StringIO()
    assert migrate.main([], stderr=stderr) == 1
    assert stderr.getvalue() == expected
    assert "private" not in expected


def test_m4_index_probe_precedes_downgrade_and_is_rolled_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class Nested:
        def rollback(self) -> None:
            order.append("rollback_probe")

    class Connection:
        def begin_nested(self) -> Nested:
            order.append("begin_probe")
            return Nested()

        def execute(self, statement: object) -> None:
            assert isinstance(statement, CreateIndex)
            compiled = str(statement.compile(dialect=postgresql.dialect()))
            assert "CREATE UNIQUE INDEX" in compiled
            assert " ON tasks " in compiled
            order.append("create_probe")

    monkeypatch.setattr(
        runner.command,
        "downgrade",
        lambda *_: order.append("alembic_downgrade"),
    )
    run_downgrade(Connection(), "0001_initial", True)
    assert order == [
        "begin_probe",
        "create_probe",
        "rollback_probe",
        "alembic_downgrade",
    ]


def test_failed_m4_index_probe_never_calls_alembic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    class Nested:
        rolled_back = False

        def rollback(self) -> None:
            self.rolled_back = True

    nested = Nested()

    class Connection:
        def begin_nested(self) -> Nested:
            return nested

        def execute(self, _: object) -> None:
            raise sa.exc.ProgrammingError("constant", {}, RuntimeError("constant"))

    def downgrade(*_: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(runner.command, "downgrade", downgrade)
    with pytest.raises(MigrationCompatibilityError) as caught:
        run_downgrade(Connection(), "0001_initial", True)
    assert str(caught.value) == "M4_DOWNGRADE_INCOMPATIBLE"
    assert caught.value.__context__ is None
    assert nested.rolled_back is True
    assert called is False
