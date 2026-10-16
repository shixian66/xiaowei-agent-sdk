"""SQLAlchemy 异常必须先归成不含驱动原文的闭集结果。"""

import datetime as dt

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityCategory,
    PersistenceIntegrityError,
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
    PersistenceWriteOutcome,
    classify_persistence_exception,
)
from xiaowei_agent.persistence.postgres import (
    PostgresTaskStore,
    _persistence_boundary,
    _write_transaction,
)


def _statement_error(error_type: type[sa.exc.StatementError]) -> sa.exc.StatementError:
    return error_type("constant", "SELECT 1", {}, RuntimeError("constant"))


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (TimeoutError(), PersistenceUnavailableCategory.TIMEOUT),
        (sa.exc.TimeoutError("constant"), PersistenceUnavailableCategory.POOL),
        (
            _statement_error(sa.exc.OperationalError),
            PersistenceUnavailableCategory.CONNECT,
        ),
        (
            _statement_error(sa.exc.InterfaceError),
            PersistenceUnavailableCategory.CONNECT,
        ),
        (sa.exc.DisconnectionError("constant"), PersistenceUnavailableCategory.TRANSIENT),
    ],
)
def test_unavailable_errors_are_classified(
    error: BaseException, category: PersistenceUnavailableCategory
) -> None:
    mapped = classify_persistence_exception(
        error, write_outcome=PersistenceWriteOutcome.ROLLED_BACK
    )
    assert isinstance(mapped, PersistenceUnavailableError)
    assert mapped.category is category
    assert mapped.write_outcome is PersistenceWriteOutcome.ROLLED_BACK


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (_statement_error(sa.exc.IntegrityError), PersistenceIntegrityCategory.CONSTRAINT),
        (_statement_error(sa.exc.DataError), PersistenceIntegrityCategory.DATA),
        (_statement_error(sa.exc.ProgrammingError), PersistenceIntegrityCategory.SCHEMA),
        (sa.exc.InvalidRequestError("constant"), PersistenceIntegrityCategory.SCHEMA),
    ],
)
def test_systemic_errors_are_classified(
    error: BaseException, category: PersistenceIntegrityCategory
) -> None:
    mapped = classify_persistence_exception(error)
    assert isinstance(mapped, PersistenceIntegrityError)
    assert mapped.category is category


def test_connection_invalidated_wins_over_statement_error() -> None:
    error = sa.exc.DBAPIError(
        "SELECT 1",
        {},
        ConnectionError("constant"),
        connection_invalidated=True,
    )
    mapped = classify_persistence_exception(
        error, write_outcome=PersistenceWriteOutcome.NOT_CONFIRMED
    )
    assert isinstance(mapped, PersistenceUnavailableError)
    assert mapped.category is PersistenceUnavailableCategory.TRANSIENT
    assert mapped.write_outcome is PersistenceWriteOutcome.NOT_CONFIRMED


def test_unrelated_errors_are_not_claimed_by_the_persistence_boundary() -> None:
    assert classify_persistence_exception(ValueError("constant")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("acquire_lease", {"task_id": "t1", "owner": b"worker", "ttl_seconds": 30}),
        (
            "renew_lease",
            {
                "task_id": "t1",
                "owner": "worker",
                "fencing_token": 1,
                "ttl_seconds": 0,
            },
        ),
    ],
)
async def test_lease_input_validation_precedes_the_postgres_boundary(
    method: str, arguments: dict[str, object]
) -> None:
    """调用方形状错误仍是 ValidationError，且不能触碰数据库。"""
    store = PostgresTaskStore(
        engine=object(),  # type: ignore[arg-type]
        clock=lambda: dt.datetime(2026, 9, 5, tzinfo=dt.UTC),
    )

    with pytest.raises(ValidationError):
        await getattr(store, method)(**arguments)  # type: ignore[misc]


def test_mapped_errors_never_echo_driver_text() -> None:
    marker = "private-marker"
    mapped = classify_persistence_exception(
        sa.exc.ProgrammingError("statement", "SELECT :value", {"value": marker}, None)
    )
    assert isinstance(mapped, PersistenceIntegrityError)
    assert marker not in str(mapped)
    assert marker not in repr(mapped)


@pytest.mark.asyncio
async def test_postgres_write_boundary_cuts_the_driver_exception_chain() -> None:
    marker = "private-" + "statement-value"

    @_persistence_boundary(write=True)
    async def fail() -> None:
        raise sa.exc.OperationalError("SELECT :value", {"value": marker}, ConnectionError(marker))

    with pytest.raises(PersistenceUnavailableError) as caught:
        await fail()
    assert caught.value.write_outcome is PersistenceWriteOutcome.NOT_CONFIRMED
    assert caught.value.__context__ is None
    assert marker not in str(caught.value)
    assert marker not in repr(caught.value)


@pytest.mark.asyncio
async def test_postgres_read_boundary_has_no_write_outcome() -> None:
    @_persistence_boundary(write=False)
    async def fail() -> None:
        raise sa.exc.TimeoutError("constant")

    with pytest.raises(PersistenceUnavailableError) as caught:
        await fail()
    assert caught.value.write_outcome is None


class _TransactionContext:
    def __init__(
        self,
        *,
        enter_error: Exception | None = None,
        exit_error: Exception | None = None,
    ) -> None:
        self.enter_error = enter_error
        self.exit_error = exit_error

    async def __aenter__(self) -> object:
        if self.enter_error is not None:
            raise self.enter_error
        return object()

    async def __aexit__(self, *args: object) -> bool:
        if self.exit_error is not None:
            raise self.exit_error
        return False


class _Engine:
    def __init__(self, transaction: _TransactionContext) -> None:
        self.transaction = transaction

    def begin(self) -> _TransactionContext:
        return self.transaction


async def test_write_transaction_returns_after_a_successful_commit() -> None:
    engine = _Engine(_TransactionContext())

    async with _write_transaction(engine):  # type: ignore[arg-type]
        pass


async def test_write_transaction_reports_a_confirmed_rollback() -> None:
    engine = _Engine(_TransactionContext())

    @_persistence_boundary(write=True)
    async def operation() -> None:
        async with _write_transaction(engine):  # type: ignore[arg-type]
            raise sa.exc.OperationalError(
                "SELECT :value", {"value": "private"}, ConnectionError("private")
            )

    with pytest.raises(PersistenceUnavailableError) as caught:
        await operation()

    assert caught.value.write_outcome is PersistenceWriteOutcome.ROLLED_BACK
    assert caught.value.__context__ is None


async def test_write_transaction_reports_an_unknown_commit_result() -> None:
    engine = _Engine(_TransactionContext(exit_error=sa.exc.DisconnectionError("private")))

    @_persistence_boundary(write=True)
    async def operation() -> None:
        async with _write_transaction(engine):  # type: ignore[arg-type]
            pass

    with pytest.raises(PersistenceUnavailableError) as caught:
        await operation()

    assert caught.value.write_outcome is PersistenceWriteOutcome.NOT_CONFIRMED
    assert caught.value.__context__ is None


async def test_integrity_error_is_exposed_only_after_confirmed_rollback() -> None:
    engine = _Engine(_TransactionContext())

    @_persistence_boundary(write=True)
    async def operation() -> None:
        async with _write_transaction(engine):  # type: ignore[arg-type]
            raise sa.exc.ProgrammingError(
                "SELECT :value",
                {"value": "private"},
                RuntimeError("private"),
            )

    with pytest.raises(PersistenceIntegrityError) as caught:
        await operation()

    assert caught.value.write_outcome is PersistenceWriteOutcome.ROLLED_BACK
    assert caught.value.__context__ is None


async def test_rollback_connection_loss_overrides_the_original_integrity_error() -> None:
    engine = _Engine(_TransactionContext(exit_error=sa.exc.DisconnectionError("rollback-private")))

    @_persistence_boundary(write=True)
    async def operation() -> None:
        async with _write_transaction(engine):  # type: ignore[arg-type]
            raise sa.exc.IntegrityError(
                "INSERT", {"value": "body-private"}, RuntimeError("body-private")
            )

    with pytest.raises(PersistenceUnavailableError) as caught:
        await operation()

    assert caught.value.write_outcome is PersistenceWriteOutcome.NOT_CONFIRMED
    assert caught.value.__context__ is None
    assert "private" not in str(caught.value)
    assert "private" not in repr(caught.value)
