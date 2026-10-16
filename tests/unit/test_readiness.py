"""readiness 只做 DB ping 与 Alembic revision 比对。"""

import pytest

from xiaowei_agent.persistence import database
from xiaowei_agent.persistence.database import PostgresReadinessProbe


class _Connection:
    def __init__(self, *, revision: str, failure: Exception | None = None) -> None:
        self.revision = revision
        self.failure = failure
        self.statements: list[str] = []

    async def execute(self, statement: object) -> None:
        self.statements.append(str(statement))
        if self.failure is not None:
            raise self.failure

    async def run_sync(self, function: object) -> str:
        del function
        return self.revision


class _Connect:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self) -> _Connect:
        return _Connect(self.connection)


@pytest.mark.asyncio
async def test_readiness_requires_database_head_and_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database, "_current_revision", lambda _: "0005_step_journal")
    connection = _Connection(revision="0005_step_journal")
    probe = PostgresReadinessProbe(engine=_Engine(connection), assembled=True)
    report = await probe.check()
    assert report.database_ok is True
    assert report.revision_matches_head is True
    assert report.assembled is True
    assert connection.statements == ["SELECT 1"]


@pytest.mark.asyncio
async def test_readiness_failure_is_a_closed_negative_report() -> None:
    connection = _Connection(revision="", failure=RuntimeError("private db text"))
    report = await PostgresReadinessProbe(engine=_Engine(connection), assembled=True).check()
    assert report.database_ok is False
    assert report.revision_matches_head is False
    assert report.assembled is True
