"""内部表原始 DDL：保留服务器文本；非内部对象、替换和不完整结果不可交付。"""

import json

import pytest
from asyncmy.errors import ProgrammingError
from pydantic import ValidationError
from tests.p1b.test_starrocks_adapter import (
    SESSION_READ,
    SESSION_SET,
    SESSION_VALUES,
    TARGET,
    Driver,
    FakeConnection,
    Result,
    adapter,
)

from xiaowei.starrocks import StarRocksError, StarRocksErrorCode, StarRocksTarget

IDENTITY_SQL = (
    "SELECT c.TABLE_ID AS id, c.TABLE_ENGINE AS engine, t.TABLE_TYPE AS type "
    "FROM information_schema.tables_config c JOIN information_schema.tables t "
    "ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME "
    "WHERE c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s"
)
SHOW_SQL = "SHOW CREATE TABLE `shop`.`sales`"
DDL = (
    'CREATE TABLE `sales` (\n  `id` bigint NOT NULL DEFAULT "0",\n'
    '  `note` varchar(64) NULL COMMENT "原文\\n<>"\n) ENGINE=OLAP\n'
    "PRIMARY KEY(`id`)\nDISTRIBUTED BY HASH(`id`) BUCKETS 1\n"
    'PROPERTIES (\n"replication_num" = "1",\n"compression" = "LZ4"\n);'
)


def ddl_driver(
    *,
    before: Result | None = None,
    after: Result | None = None,
    shown: Result | BaseException | None = None,
) -> Driver:
    checks = 0
    internal = Result(("id", "engine", "type"), [(101, "OLAP", "BASE TABLE")])

    def identity(args: tuple[object, ...]) -> Result:
        nonlocal checks
        assert args == ("shop", "sales")
        checks += 1
        return (before if checks == 1 else after) or internal

    return Driver(
        lambda: FakeConnection(
            results={
                SESSION_SET: Result(()),
                SESSION_READ: Result(("q", "m", "t", "s"), [SESSION_VALUES]),
                IDENTITY_SQL: identity,
                SHOW_SQL: shown or Result(("Table", "Create Table"), [("sales", DDL)]),
            }
        )
    )


def configured(**updates: object) -> StarRocksTarget:
    return StarRocksTarget.model_validate({**TARGET.model_dump(), **updates})


def statements(drv: Driver) -> list[str]:
    return [
        sql
        for conn in drv.connections
        for sql, _ in conn.executed
        if sql != SESSION_SET and sql != SESSION_READ
    ]


async def test_internal_ddl_is_exact_and_uses_bound_identity_checks() -> None:
    drv = ddl_driver()
    target = configured(max_ddl_bytes=800)
    result = await adapter(drv, target).show_create_table("shop", "sales", 101)
    assert result.sql == SHOW_SQL
    assert result.columns == ("ddl",)
    assert result.rows == ({"ddl": DDL},)
    assert result.row_count == 1 and not result.truncated
    assert statements(drv) == [IDENTITY_SQL, SHOW_SQL, IDENTITY_SQL]
    assert all(conn.closed and not conn.aborted for conn in drv.connections)
    assert target.max_value_bytes == TARGET.max_value_bytes


@pytest.mark.parametrize(
    "row",
    [
        (101, "MYSQL", "BASE TABLE"),
        (101, "HIVE", "BASE TABLE"),
        (101, "VIEW", "VIEW"),
        (101, "OLAP", "MATERIALIZED VIEW"),
        (102, "OLAP", "BASE TABLE"),
        ("101", "OLAP", "BASE TABLE"),
        (True, "OLAP", "BASE TABLE"),
    ],
)
async def test_ddl_rejects_other_object_before_show(row: tuple[object, ...]) -> None:
    drv = ddl_driver(before=Result(("id", "engine", "type"), [row]))
    with pytest.raises(StarRocksError) as error:
        await adapter(drv).show_create_table("shop", "sales", 101)
    assert error.value.code == StarRocksErrorCode.OBJECT_NOT_ALLOWED
    assert statements(drv) == [IDENTITY_SQL]


@pytest.mark.parametrize(
    "after",
    [
        Result(("id", "engine", "type"), [(102, "OLAP", "BASE TABLE")]),
        Result(("id", "engine", "type"), [(101, "MYSQL", "BASE TABLE")]),
        Result(("id", "engine", "type")),
    ],
)
async def test_ddl_rejects_object_changed_during_show(after: Result) -> None:
    drv = ddl_driver(after=after)
    with pytest.raises(StarRocksError):
        await adapter(drv, configured(max_ddl_bytes=800)).show_create_table("shop", "sales", 101)
    assert statements(drv) == [IDENTITY_SQL, SHOW_SQL, IDENTITY_SQL]


@pytest.mark.parametrize(
    ("database", "name", "table_id"),
    [
        ("shop`", "sales", 101),
        ("shop", "sales\n", 101),
        ("shop", "sales", 0),
        ("shop", "sales", True),
    ],
)
async def test_ddl_invalid_identity_has_no_io(database: str, name: str, table_id: int) -> None:
    drv = ddl_driver()
    with pytest.raises(StarRocksError):
        await adapter(drv).show_create_table(database, name, table_id)
    assert drv.attempts == 0


@pytest.mark.parametrize(
    "shown",
    [
        Result(("View", "Create View"), [("sales", DDL)]),
        Result(("Table", "Create Table")),
        Result(("Table", "Create Table"), [("other", DDL)]),
        Result(("Table", "Create Table"), [("sales", "")]),
        Result(("Table", "Create Table"), [("sales", None)]),
        Result(("Table", "Create Table"), [("sales", DDL), ("sales", DDL)]),
    ],
)
async def test_ddl_invalid_source_is_not_delivered(shown: Result) -> None:
    drv = ddl_driver(shown=shown)
    with pytest.raises(StarRocksError) as error:
        await adapter(drv, configured(max_ddl_bytes=800)).show_create_table("shop", "sales", 101)
    assert error.value.code == StarRocksErrorCode.RESULT_CONTRACT


@pytest.mark.parametrize("limit", [None, len(json.dumps(DDL, ensure_ascii=False).encode()) - 1])
async def test_ddl_value_overflow_returns_no_partial_statement(limit: int | None) -> None:
    drv = ddl_driver()
    result = await adapter(drv, configured(max_ddl_bytes=limit)).show_create_table(
        "shop", "sales", 101
    )
    assert result.truncated and result.row_count == 0 and result.rows == ()
    assert statements(drv) == [IDENTITY_SQL, SHOW_SQL, IDENTITY_SQL]
    assert drv.connections[1].aborted


async def test_ddl_total_overflow_returns_no_partial_statement() -> None:
    limit = len(json.dumps(DDL, ensure_ascii=False).encode())
    drv = ddl_driver()
    result = await adapter(
        drv, configured(max_result_bytes=limit, max_ddl_bytes=limit)
    ).show_create_table("shop", "sales", 101)
    assert result.truncated and result.rows == ()


async def test_ddl_permission_failure_has_no_raw_server_text_or_retry() -> None:
    drv = ddl_driver(shown=ProgrammingError(5203, "private-server-text"))
    with pytest.raises(StarRocksError) as error:
        await adapter(drv).show_create_table("shop", "sales", 101)
    assert error.value.code == StarRocksErrorCode.PERMISSION_DENIED
    assert "private-server-text" not in str(error.value)
    assert statements(drv) == [IDENTITY_SQL, SHOW_SQL]


@pytest.mark.parametrize("limit", [0, -1, TARGET.max_result_bytes + 1])
def test_ddl_capacity_must_fit_result_capacity(limit: int) -> None:
    with pytest.raises(ValidationError):
        configured(max_ddl_bytes=limit)


async def test_show_sql_limit_is_checked_before_io() -> None:
    drv = ddl_driver()
    policy = TARGET.policy.model_copy(update={"max_sql_bytes": 4})
    with pytest.raises(StarRocksError):
        await adapter(drv, configured(policy=policy)).show_create_table("shop", "sales", 101)
    assert drv.attempts == 0
