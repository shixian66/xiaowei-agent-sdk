"""StarRocks Adapter 的离线契约（P1-B Task 2）：recording 驱动替身，不连接任何数据库。

替身只替换最底层的连接对象（执行、逐行读取、正常关闭、中止）；Adapter 的并发槽位、期限、
会话变量设置与回读、结果限量、类型规范化和错误映射都走真实代码。替身不能证明 asyncmy 与
StarRocks 的协议行为，那部分在 ``test_starrocks_real.py``。
"""

import asyncio
import json
import math
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from asyncmy.errors import OperationalError, ProgrammingError
from pydantic import ValidationError

from xiaowei.config import SecretRefError
from xiaowei.sqlguard import QueryPolicy, QueryRejectedError, guard_readonly_query
from xiaowei.starrocks import (
    ERROR_MESSAGES,
    QueryResult,
    StarRocksAdapter,
    StarRocksError,
    StarRocksErrorCode,
    StarRocksTarget,
    open_starrocks,
    tls_context,
)

Code = StarRocksErrorCode
NOW = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
CANARY = "canary-5e1b"

POLICY = QueryPolicy(
    target_id="sr-test",
    default_database="shop",
    allowed_objects=frozenset({"sales", "regions"}),
    allowed_columns={
        "sales": frozenset({"region", "total", "note"}),
        "regions": frozenset({"name", "region"}),
    },
    allowed_functions=frozenset({"SUM", "COUNT"}),
    max_rows=5,
    max_sql_bytes=4000,
)
TARGET = StarRocksTarget(
    target_id="sr-test",
    host="starrocks.internal",
    port=9030,
    database="shop",
    tls=True,
    tls_ca_file=None,
    user="xiaowei_ro",
    password_ref="env:XW_TEST_SR_PASSWORD",  # noqa: S106 —— 引用，不是凭据
    connect_timeout_seconds=0.2,
    query_timeout_seconds=1,
    client_timeout_seconds=1,
    pool_size=2,
    time_zone="Asia/Shanghai",
    query_mem_limit_bytes=2**30,
    max_result_bytes=1000,
    max_value_bytes=200,
    policy=POLICY,
)
SESSION_SET = "SET query_timeout = 1, query_mem_limit = 1073741824, time_zone = 'Asia/Shanghai'"
SESSION_READ = "SELECT @@query_timeout, @@query_mem_limit, @@time_zone"
SESSION_VALUES: tuple[object, ...] = (1, 1073741824, "Asia/Shanghai")


# ---- recording 驱动替身 ---------------------------------------------------------------------


@dataclass
class Result:
    columns: tuple[str, ...]
    rows: Sequence[tuple[object, ...]] = ()


@dataclass
class FakeConnection:
    """按 SQL 返回脚本化结果；记录执行、关闭方式，以及当前语句已读取的行数。

    读取失败与挂起只作用于会话设置之后的查询本身。
    """

    results: dict[str, Result | BaseException]
    fail_read_at: int | None = None
    read_error: BaseException | None = None
    hang_on: str | None = None
    executed: list[tuple[str, tuple[object, ...] | None]] = field(default_factory=list)
    rows_read: int = 0
    closed: bool = False
    aborted: bool = False
    _rows: list[tuple[object, ...]] = field(default_factory=list)
    _in_query: bool = False

    async def execute(self, sql: str, args: tuple[object, ...] | None) -> tuple[str, ...]:
        self.executed.append((sql, args))
        self.rows_read = 0
        self._in_query = sql not in {SESSION_SET, SESSION_READ}
        if sql == self.hang_on:
            await asyncio.Event().wait()
        outcome = self.results.get(sql)
        if outcome is None:
            outcome = self.results["*"]
        if isinstance(outcome, BaseException):
            raise outcome
        self._rows = list(outcome.rows)
        return outcome.columns

    async def fetch_row(self) -> tuple[object, ...] | None:
        if self._in_query and self.fail_read_at == self.rows_read:
            assert self.read_error is not None
            raise self.read_error
        if self._in_query and self.hang_on == "fetch" and self.rows_read == 1:
            await asyncio.Event().wait()
        if not self._rows:
            return None
        self.rows_read += 1
        return self._rows.pop(0)

    async def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.aborted = True


@dataclass
class Driver:
    """Adapter 的连接工厂替身；``connect_error``/``connect_hang`` 模拟连接阶段失败。"""

    make: Callable[[], FakeConnection]
    connect_error: BaseException | None = None
    connect_hang: bool = False
    connections: list[FakeConnection] = field(default_factory=list)
    attempts: int = 0

    async def __call__(self) -> FakeConnection:
        self.attempts += 1
        if self.connect_hang:
            await asyncio.Event().wait()
        if self.connect_error is not None:
            raise self.connect_error
        conn = self.make()
        self.connections.append(conn)
        return conn


def driver(query: Result | BaseException | None = None, **conn: Any) -> Driver:
    def make() -> FakeConnection:
        results: dict[str, Result | BaseException] = {
            SESSION_SET: Result(()),
            SESSION_READ: Result(("q", "m", "t"), [SESSION_VALUES]),
            "*": query if query is not None else Result(("region",), [("east",)]),
        }
        results.update(conn.pop("results", {}))
        return FakeConnection(results=results, **conn)

    return Driver(make)


def adapter(drv: Driver, target: StarRocksTarget = TARGET) -> StarRocksAdapter:
    return StarRocksAdapter(target, connect=drv, clock=lambda: NOW)


def guarded(sql: str = "SELECT region, total FROM sales"):  # type: ignore[no-untyped-def]
    return guard_readonly_query(sql, POLICY)


async def failure(call: Any) -> StarRocksError:
    with pytest.raises(StarRocksError) as info:
        await call
    return info.value


def assert_safe(error: StarRocksError) -> None:
    assert str(error) == ERROR_MESSAGES[error.code]
    assert (error.__cause__, error.__context__) == (None, None)
    assert CANARY not in repr(error)


def only(drv: Driver) -> FakeConnection:
    assert drv.attempts == 1, "失败后不得自动重连或重试"
    (conn,) = drv.connections
    return conn


# ---- 成功路径 -------------------------------------------------------------------------------


async def test_query_runs_the_guarded_sql_after_verified_session_setup() -> None:
    query = guarded("SELECT region, total FROM sales WHERE region = 'a%b'")
    drv = driver(Result(("region", "total"), [("east", 100), ("west", 50)]))

    result = await adapter(drv).run_query(query)

    conn = only(drv)
    assert conn.executed == [
        (SESSION_SET, None),
        (SESSION_READ, None),
        (query.normalized_sql, None),  # 无参数：驱动不做 % 插值
    ]
    assert result == QueryResult(
        target_id="sr-test",
        sql=query.normalized_sql,
        columns=("region", "total"),
        rows=({"region": "east", "total": 100}, {"region": "west", "total": 50}),
        truncated=False,
        collected_at=NOW,
        elapsed_ms=result.elapsed_ms,
    )
    assert result.row_count == 2
    assert result.elapsed_ms >= 0
    assert (conn.closed, conn.aborted) == (True, False)


async def test_every_query_gets_its_own_connection() -> None:
    drv = driver()
    ada = adapter(drv)
    await ada.run_query(guarded())
    await ada.run_query(guarded())

    assert drv.attempts == 2
    assert all(c.closed and not c.aborted for c in drv.connections)


async def test_empty_result_is_complete() -> None:
    drv = driver(Result(("region",), []))
    result = await adapter(drv).run_query(guarded())
    assert (result.rows, result.truncated) == ((), False)
    assert only(drv).closed


# ---- 有界读取 -------------------------------------------------------------------------------


async def test_reads_at_most_one_row_past_the_limit_and_discards_the_connection() -> None:
    query = guarded("SELECT region FROM sales")  # LIMIT 改写为 max_rows + 1
    rows = [(f"r{i}",) for i in range(50)]
    drv = driver(Result(("region",), rows))

    result = await adapter(drv).run_query(query)

    conn = only(drv)
    assert query.max_returned_rows == POLICY.max_rows
    assert conn.rows_read == POLICY.max_rows + 1
    assert result.row_count == POLICY.max_rows and result.truncated
    assert (conn.closed, conn.aborted) == (False, True)


async def test_a_smaller_limit_that_is_filled_exactly_is_not_truncated() -> None:
    query = guarded("SELECT region FROM sales LIMIT 3")
    drv = driver(Result(("region",), [("a",), ("b",), ("c",)]))
    result = await adapter(drv).run_query(query)
    assert (result.row_count, result.truncated) == (3, False)
    assert only(drv).closed


def rows_size(rows: Sequence[dict[str, object]]) -> int:
    return len(json.dumps(list(rows), ensure_ascii=False, allow_nan=False).encode())


async def test_total_bytes_use_the_production_json_encoding() -> None:
    """控制字符、引号、反斜杠按 JSON 转义后计数；放不下的行整行丢弃并标记截断。"""
    note = '\x01"\\区' * 10  # 每组原始 6 字节，转义后 13 字节
    rows = [(note,)] * 5
    target = TARGET.model_copy(update={"max_result_bytes": 500})
    drv = driver(Result(("note",), rows))

    result = await adapter(drv, target).run_query(guarded("SELECT note FROM sales"))

    assert result.truncated
    assert 0 < result.row_count < len(rows)
    assert rows_size(result.rows) <= 500
    assert rows_size([*result.rows, {"note": note}]) > 500
    assert all(row == {"note": note} for row in result.rows)
    assert only(drv).aborted


async def test_an_oversized_value_stops_reading_without_a_partial_row() -> None:
    big = "x" * (TARGET.max_value_bytes - 1)  # 加上引号即超过单值上限
    drv = driver(Result(("region", "note"), [("a", "ok"), ("b", big), ("c", "ok")]))

    result = await adapter(drv).run_query(guarded("SELECT region, note FROM sales"))

    assert result.rows == ({"region": "a", "note": "ok"},)
    assert result.truncated
    assert only(drv).aborted


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (7, 7),
        (1.5, 1.5),
        ("区", "区"),
        (Decimal("12.50"), "12.50"),
        (Decimal("1E+3"), "1000"),
        (Decimal("-0.00012"), "-0.00012"),
        (date(2026, 9, 1), "2026-09-01"),
        (datetime(2026, 9, 1, 8, 30), "2026-09-01T08:30:00+08:00"),  # 会话时区即目标时区
        (datetime(2026, 9, 1, 0, 30, tzinfo=UTC), "2026-09-01T08:30:00+08:00"),
        (
            datetime(2026, 9, 1, 8, 30, tzinfo=timezone(timedelta(hours=8))),
            "2026-09-01T08:30:00+08:00",
        ),
    ],
)
async def test_values_are_normalized_to_json_scalars(value: object, expected: object) -> None:
    drv = driver(Result(("v",), [(value,)]))
    result = await adapter(drv).run_query(guarded("SELECT total FROM sales"))
    assert result.rows == ({"v": expected},)


@pytest.mark.parametrize(
    "value",
    [
        b"\x00raw",
        bytearray(b"raw"),
        math.nan,
        math.inf,
        Decimal("NaN"),
        Decimal("Infinity"),
        timedelta(hours=1),
        object(),
    ],
)
async def test_unsupported_values_break_the_result_contract(value: object) -> None:
    drv = driver(Result(("v",), [("ok",), (value,)]))
    error = await failure(adapter(drv).run_query(guarded()))
    assert error.code is Code.RESULT_CONTRACT
    assert_safe(error)
    assert only(drv).aborted


async def test_duplicate_column_names_are_rejected_before_reading_rows() -> None:
    drv = driver(Result(("region", "region"), [("a", "b")]))
    error = await failure(adapter(drv).run_query(guarded()))
    assert error.code is Code.RESULT_CONTRACT
    conn = only(drv)
    assert conn.rows_read == 0 and conn.aborted


async def test_row_width_must_match_the_columns() -> None:
    drv = driver(Result(("region", "total"), [("a",)]))
    assert (await failure(adapter(drv).run_query(guarded()))).code is Code.RESULT_CONTRACT


# ---- 会话变量 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        (300, 1073741824, "Asia/Shanghai"),
        (1, 0, "Asia/Shanghai"),
        (1, 1073741824, "UTC"),
        ("1", "1073741824", "Asia/Shanghai "),
    ],
)
async def test_session_readback_mismatch_stops_before_the_query(values: tuple[object, ...]) -> None:
    drv = driver(results={SESSION_READ: Result(("q", "m", "t"), [values])})
    query = guarded()

    error = await failure(adapter(drv).run_query(query))

    conn = only(drv)
    assert error.code is Code.SESSION_SETUP_FAILED
    assert all(sql != query.normalized_sql for sql, _ in conn.executed)
    assert conn.aborted


async def test_readback_of_string_typed_values_is_accepted() -> None:
    drv = driver(
        results={SESSION_READ: Result(("q", "m", "t"), [("1", "1073741824", "Asia/Shanghai")])}
    )
    assert (await adapter(drv).run_query(guarded())).row_count == 1


@pytest.mark.parametrize(
    "results",
    [
        {SESSION_SET: OperationalError(1227, f"Access denied {CANARY}")},
        {SESSION_READ: Result(("q", "m", "t"), [])},
        {SESSION_READ: Result(("q", "m", "t"), [SESSION_VALUES, SESSION_VALUES])},
        {SESSION_READ: Result(("q", "m"), [(1, 1073741824)])},
    ],
)
async def test_session_setup_failures_never_run_the_query(
    results: dict[str, Result | BaseException],
) -> None:
    drv = driver(results=results)
    query = guarded()
    error = await failure(adapter(drv).run_query(query))

    conn = only(drv)
    assert error.code is Code.SESSION_SETUP_FAILED
    assert_safe(error)
    assert all(sql != query.normalized_sql for sql, _ in conn.executed)
    assert conn.aborted


# ---- 错误映射、期限、取消与并发 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OperationalError(1045, f"Access denied for user {CANARY}"), Code.AUTH_FAILED),
        (OperationalError(2003, f"Can't connect to {CANARY}"), Code.CONNECT_FAILED),
        (ConnectionRefusedError(61, CANARY), Code.CONNECT_FAILED),
        (ssl.SSLCertVerificationError(1, CANARY), Code.CONNECT_FAILED),
        (TimeoutError(), Code.CONNECT_FAILED),
    ],
)
async def test_connect_failures_are_mapped(error: BaseException, code: Code) -> None:
    drv = driver()
    drv.connect_error = error
    result = await failure(adapter(drv).run_query(guarded()))
    assert result.code is code
    assert_safe(result)
    assert drv.attempts == 1


async def test_connect_is_bounded_by_the_connect_timeout() -> None:
    drv = driver()
    drv.connect_hang = True
    async with asyncio.timeout(5):  # 缺少连接期限时快速失败，而不是挂起
        error = await failure(adapter(drv).run_query(guarded()))
    assert error.code is Code.CONNECT_FAILED
    assert drv.attempts == 1


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ProgrammingError(1064, f"syntax error near '{CANARY}'"), Code.QUERY_FAILED),
        (OperationalError(1142, f"SELECT command denied {CANARY}"), Code.PERMISSION_DENIED),
        (OperationalError(5203, f"Access denied {CANARY}"), Code.PERMISSION_DENIED),
        (OperationalError(5024, f"Query reached its timeout {CANARY}"), Code.SERVER_TIMEOUT),
        (OperationalError(1064, f"query timeout {CANARY}"), Code.QUERY_FAILED),
        (OperationalError(2013, f"Lost connection {CANARY}"), Code.CONNECTION_LOST),
        (ConnectionResetError(54, CANARY), Code.CONNECTION_LOST),
    ],
)
async def test_query_failures_are_mapped_without_the_server_text(
    error: BaseException, code: Code
) -> None:
    drv = driver(error)
    result = await failure(adapter(drv).run_query(guarded()))
    assert result.code is code
    assert_safe(result)
    assert only(drv).aborted


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OperationalError(2013, f"Lost connection {CANARY}"), Code.CONNECTION_LOST),
        (OSError(CANARY), Code.CONNECTION_LOST),
        (OperationalError(1064, f"mem limit exceeded {CANARY}"), Code.QUERY_FAILED),
    ],
)
async def test_failures_while_reading_discard_the_connection(
    error: BaseException, code: Code
) -> None:
    drv = driver(Result(("region",), [("a",), ("b",)]), fail_read_at=1, read_error=error)
    result = await failure(adapter(drv).run_query(guarded()))
    assert result.code is code
    assert_safe(result)
    assert only(drv).aborted


async def test_client_deadline_covers_reading_rows() -> None:
    drv = driver(Result(("region",), [("a",), ("b",)]), hang_on="fetch")
    async with asyncio.timeout(5):  # 缺少客户端期限时快速失败，而不是挂起
        error = await failure(adapter(drv).run_query(guarded()))
    assert error.code is Code.TIMEOUT
    assert only(drv).aborted


async def test_cancellation_discards_the_connection_and_frees_the_slot() -> None:
    drv = driver(Result(("region",), [("a",), ("b",)]), hang_on="fetch")
    one_slot = TARGET.model_copy(update={"pool_size": 1})
    ada = adapter(drv, one_slot)

    task = asyncio.create_task(ada.run_query(guarded()))
    while not drv.connections or drv.connections[0].rows_read < 1:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert drv.connections[0].aborted
    drv.make = driver().make
    assert (await ada.run_query(guarded())).row_count == 1  # 槽位已释放


async def test_waiting_for_a_slot_is_bounded() -> None:
    blocking = driver(Result(("region",), [("a",), ("b",)]), hang_on="fetch")
    one_slot = TARGET.model_copy(update={"pool_size": 1})
    ada = adapter(blocking, one_slot)
    holder = asyncio.create_task(ada.run_query(guarded()))
    while not blocking.connections:
        await asyncio.sleep(0)

    error = await failure(ada.run_query(guarded()))

    assert error.code is Code.POOL_TIMEOUT
    assert blocking.attempts == 1
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder


async def test_close_failure_after_a_complete_read_falls_back_to_abort() -> None:
    class Sticky(FakeConnection):
        async def close(self) -> None:
            raise OSError(CANARY)

    drv = driver()
    base = drv.make
    drv.make = lambda: Sticky(**{k: v for k, v in vars(base()).items() if not k.startswith("_")})
    result = await adapter(drv).run_query(guarded())
    assert result.row_count == 1
    assert only(drv).aborted


# ---- 只执行 SQLGuard 产物与代码生成的元数据查询 ----------------------------------------------


async def test_rejected_sql_never_reaches_the_driver() -> None:
    drv = driver()
    ada = adapter(drv)
    with pytest.raises(QueryRejectedError):
        await ada.run_query(guarded("DELETE FROM sales"))
    assert drv.attempts == 0


async def test_only_guarded_queries_for_this_target_are_executed() -> None:
    drv = driver()
    other = guard_readonly_query(
        "SELECT region FROM sales", POLICY.model_copy(update={"target_id": "sr-other"})
    )
    with pytest.raises(TypeError):
        await adapter(drv).run_query("SELECT region FROM sales")  # type: ignore[arg-type]
    with pytest.raises(StarRocksError) as info:
        await adapter(drv).run_query(other)
    assert info.value.code is Code.OBJECT_NOT_ALLOWED
    assert drv.attempts == 0


async def test_list_tables_is_code_generated_and_bound() -> None:
    drv = driver(Result(("name", "type"), [("regions", "VIEW"), ("sales", "BASE TABLE")]))
    result = await adapter(drv).list_tables()

    sql, args = only(drv).executed[-1]
    assert sql == (
        "SELECT TABLE_NAME AS name, TABLE_TYPE AS type FROM information_schema.tables "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN (%s, %s) ORDER BY TABLE_NAME"
    )
    assert args == ("shop", "regions", "sales")
    assert result.rows == (
        {"name": "regions", "type": "VIEW"},
        {"name": "sales", "type": "BASE TABLE"},
    )
    assert not result.truncated


async def test_describe_table_only_exposes_allowed_columns() -> None:
    drv = driver(Result(("name", "type", "nullable"), [("region", "varchar", "YES")]))
    result = await adapter(drv).describe_table("sales")

    sql, args = only(drv).executed[-1]
    assert sql == (
        "SELECT COLUMN_NAME AS name, DATA_TYPE AS type, IS_NULLABLE AS nullable "
        "FROM information_schema.columns WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
        "AND COLUMN_NAME IN (%s, %s, %s) ORDER BY ORDINAL_POSITION"
    )
    assert args == ("shop", "sales", "note", "region", "total")
    assert result.columns == ("name", "type", "nullable")


@pytest.mark.parametrize("name", ["customers", "Sales", "sales; DROP TABLE x", ""])
async def test_describe_table_rejects_objects_outside_the_allowlist_before_io(name: str) -> None:
    drv = driver()
    error = await failure(adapter(drv).describe_table(name))
    assert error.code is Code.OBJECT_NOT_ALLOWED
    assert drv.attempts == 0


# ---- 可信配置与装配 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"target_id": "sr-other"},  # 与 QueryPolicy 不一致
        {"database": "other"},
        {"time_zone": "Mars/Base"},
        {"time_zone": "Asia/Shanghai'; SET x = 1; --"},
        {"password_ref": "plain-password"},
        {"tls": False, "tls_ca_file": "/etc/ca.pem"},
        {"client_timeout_seconds": 0.5},  # 早于服务端期限
        {"max_value_bytes": 2000},  # 大于结果总上限
        {"port": 0},
        {"pool_size": 0},
        {"query_mem_limit_bytes": 0},
        {"extra": 1},
    ],
)
def test_target_rejects_inconsistent_configuration(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        StarRocksTarget.model_validate({**TARGET.model_dump(), **changes})


def test_tls_context_verifies_certificate_and_host() -> None:
    context = tls_context(TARGET)
    assert context is not None
    assert context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname
    assert tls_context(TARGET.model_copy(update={"tls": False})) is None


def test_opening_requires_the_password_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XW_TEST_SR_PASSWORD", raising=False)
    with pytest.raises(SecretRefError):
        open_starrocks(TARGET, clock=lambda: NOW)


def test_opening_does_no_io_and_keeps_the_password_out_of_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XW_TEST_SR_PASSWORD", CANARY)
    ada = open_starrocks(TARGET, clock=lambda: NOW)  # 默认禁网：若此处连接会直接失败
    assert CANARY not in repr(ada)
    assert CANARY not in repr(vars(ada))
    assert CANARY not in TARGET.model_dump_json()


def test_every_code_has_a_fixed_message() -> None:
    assert set(ERROR_MESSAGES) == set(Code)
    assert all("{" not in m for m in ERROR_MESSAGES.values())
