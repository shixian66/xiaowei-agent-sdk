"""StarRocks Adapter 的离线契约（P1-B Task 2；执行计划为 P2 Task 3）：recording 驱动替身，
不连接任何数据库。

替身只替换最底层的连接对象（执行、逐行读取、正常关闭、中止）；Adapter 的并发槽位、期限、
会话变量设置与回读、结果限量、类型规范化和错误映射都走真实代码。替身不能证明 asyncmy 与
StarRocks 的协议行为，那部分在 ``test_starrocks_real.py``。
"""

import asyncio
import dataclasses
import inspect
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
from xiaowei.sqlguard import (
    ExplainQuery,
    QueryPolicy,
    QueryRejectedError,
    guard_explain_query,
    guard_readonly_query,
)
from xiaowei.starrocks import (
    ERROR_MESSAGES,
    EXPLAIN_LEVEL,
    EXPLAIN_PREFIX,
    LAYOUT_COLUMNS,
    LAYOUT_HIDDEN,
    PLAN_COLUMN,
    SCHEMA_COLUMNS_SQL,
    SCHEMA_IDS_SQL,
    SCHEMA_OBJECTS_SQL,
    SYSTEM_DATABASES,
    QueryResult,
    SchemaLimits,
    SqlPolicy,
    StarRocksAdapter,
    StarRocksError,
    StarRocksErrorCode,
    StarRocksTarget,
    metadata_sql_bytes,
    open_starrocks,
    probe_sql,
    safe_identifier,
    tls_context,
)
from xiaowei.starrocks_schema import SchemaCache

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
# SQLGuard 范围与结构替身一致：shop.sales(region, total, note)、shop.regions(name, region)。
SQL_POLICY = SqlPolicy(
    allowed_functions=frozenset({"SUM", "COUNT"}), max_rows=5, max_sql_bytes=4000
)
LIMITS = SchemaLimits(max_objects=50, max_columns=500, max_bytes=100_000, max_comment_chars=100)
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
    policy=SQL_POLICY,
    schema_limits=LIMITS,
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
    connect_delay: float = 0.0
    connections: list[FakeConnection] = field(default_factory=list)
    attempts: int = 0

    async def __call__(self) -> FakeConnection:
        self.attempts += 1
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.connect_hang:
            await asyncio.Event().wait()
        if self.connect_error is not None:
            raise self.connect_error
        conn = self.make()
        self.connections.append(conn)
        return conn


# ---- 结构快照的替身数据（P2.5 Task 2）：与 POLICY 同样的两张表，另有一张无权表 -------------

CREATED = datetime(2026, 9, 1, 8, 0)
SCHEMA_TABLES: dict[tuple[str, str], tuple[tuple[str, str, str, str | None], ...]] = {
    ("shop", "sales"): (
        ("region", "varchar", "YES", "地区"),
        ("total", "decimal", "YES", None),
        ("note", "varchar", "YES", None),
    ),
    ("shop", "regions"): (("name", "varchar", "NO", None), ("region", "varchar", "YES", None)),
}


def schema_results(
    tables: dict[tuple[str, str], tuple[tuple[str, str, str, str | None], ...]] | None = None,
    denied: frozenset[tuple[str, str]] = frozenset(),
) -> dict[str, Result | BaseException]:
    """``read_schema`` 三条读取与每个对象零行探测的脚本结果；``denied`` 中的对象探测为 5203。"""
    tables = SCHEMA_TABLES if tables is None else tables
    objects = [(db, name, "BASE TABLE", None, CREATED) for db, name in sorted(tables)]
    columns = [
        (db, name, col, kind, nullable, comment)
        for (db, name), cols in sorted(tables.items())
        for col, kind, nullable, comment in cols
    ]
    ids = [(db, name, 100 + i) for i, (db, name) in enumerate(sorted(tables))]
    results: dict[str, Result | BaseException] = {
        SCHEMA_OBJECTS_SQL: Result(("db", "name", "type", "comment", "created"), objects),
        SCHEMA_COLUMNS_SQL: Result(("db", "name", "col", "type", "nullable", "comment"), columns),
        SCHEMA_IDS_SQL: Result(("db", "name", "id"), ids),
    }
    for db, name in tables:
        if not (safe_identifier(db) and safe_identifier(name)):
            continue  # 产品不会为这类名字生成探测语句
        results[probe_sql(db, name)] = (
            ProgrammingError(5203, f"Access denied {CANARY}")
            if (db, name) in denied
            else Result(("1",))
        )
    return results


def driver(query: Result | BaseException | None = None, **conn: Any) -> Driver:
    def make() -> FakeConnection:
        results: dict[str, Result | BaseException] = {
            SESSION_SET: Result(()),
            SESSION_READ: Result(("q", "m", "t"), [SESSION_VALUES]),
            **schema_results(),
            "*": query if query is not None else Result(("region",), [("east",)]),
        }
        results.update(conn.pop("results", {}))
        return FakeConnection(results=results, **conn)

    return Driver(make)


def adapter(drv: Driver, target: StarRocksTarget = TARGET) -> StarRocksAdapter:
    return StarRocksAdapter(target, connect=drv, clock=lambda: NOW)


async def ready_schema(ada: StarRocksAdapter, drv: Driver | None = None) -> SchemaCache:
    """刷新一次结构快照（时钟固定，不会到期）；给出 ``drv`` 时清掉刷新留下的连接记录。"""
    cache = SchemaCache(ada, clock=lambda: NOW)
    assert await cache.refresh()
    if drv is not None:
        drv.connections.clear()
        drv.attempts = 0
    return cache


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
        row_count=2,
    )
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


async def test_cancellation_while_closing_a_complete_read_still_aborts() -> None:
    closing = asyncio.Event()

    class Stuck(FakeConnection):
        async def close(self) -> None:
            closing.set()
            await asyncio.Event().wait()

    drv = driver()
    base = drv.make
    drv.make = lambda: Stuck(**{k: v for k, v in vars(base()).items() if not k.startswith("_")})
    ada = adapter(drv, TARGET.model_copy(update={"pool_size": 1}))

    task = asyncio.create_task(ada.run_query(guarded()))
    async with asyncio.timeout(5):
        await closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    conn = only(drv)
    assert conn.rows_read == 1  # 已完整读完，取消发生在正常关闭阶段
    assert (conn.closed, conn.aborted) == (False, True)
    drv.make = driver().make
    assert (await ada.run_query(guarded())).row_count == 1  # 槽位已释放
    assert drv.attempts == 2  # 取消后没有自动重试


# ---- 槽位等待与建连共用一个期限 --------------------------------------------------------------

SHARED = TARGET.model_copy(update={"pool_size": 1, "connect_timeout_seconds": 0.3})


async def hold_slot(ada: StarRocksAdapter, drv: Driver) -> asyncio.Task[Any]:
    holder = asyncio.create_task(ada.run_query(guarded()))
    while not drv.connections:
        await asyncio.sleep(0)
    return holder


async def release_after(holder: asyncio.Task[Any], delay: float) -> None:
    await asyncio.sleep(delay)
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder


async def test_slot_wait_and_connect_share_one_deadline() -> None:
    drv = driver(Result(("region",), [("a",), ("b",)]), hang_on="fetch")
    ada = adapter(drv, SHARED)
    holder = await hold_slot(ada, drv)
    drv.make = driver().make
    drv.connect_delay = 0.2  # 单独看槽位等待 0.2s 与建连 0.2s 都小于 0.3s，合计超期

    async with asyncio.timeout(5):
        error, _ = await asyncio.gather(
            failure(ada.run_query(guarded())), release_after(holder, 0.2)
        )

    assert error.code is Code.CONNECT_FAILED  # 已取得槽位，超期发生在建连阶段
    assert_safe(error)
    assert drv.attempts == 2  # 持有者一次 + 本次只尝试连接一次


async def test_short_slot_wait_and_timely_connect_still_succeed() -> None:
    drv = driver(Result(("region",), [("a",), ("b",)]), hang_on="fetch")
    ada = adapter(drv, SHARED)
    holder = await hold_slot(ada, drv)
    drv.make = driver().make
    drv.connect_delay = 0.05

    async with asyncio.timeout(5):
        result, _ = await asyncio.gather(ada.run_query(guarded()), release_after(holder, 0.05))

    assert result.row_count == 1
    assert drv.attempts == 2


async def test_an_immediate_slot_leaves_the_whole_deadline_to_connect() -> None:
    drv = driver()
    drv.connect_delay = 0.2
    result = await adapter(drv, SHARED).run_query(guarded())
    assert result.row_count == 1
    assert only(drv).closed


# ---- 返回契约 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "expected", "truncated"),
    [
        ([], 0, False),
        ([("a",), ("b",)], 2, False),
        ([(f"r{i}",) for i in range(50)], POLICY.max_rows, True),
    ],
)
async def test_row_count_is_part_of_the_serialized_result(
    rows: list[tuple[object, ...]], expected: int, truncated: bool
) -> None:
    result = await adapter(driver(Result(("region",), rows))).run_query(guarded())

    dumped = json.loads(result.model_dump_json())
    assert dumped["row_count"] == len(dumped["rows"]) == len(result.rows) == expected
    assert result.model_dump()["row_count"] == expected
    assert result.truncated is truncated


def test_row_count_is_in_the_result_schema() -> None:
    for mode in ("validation", "serialization"):
        schema = QueryResult.model_json_schema(mode=mode)
        assert "row_count" in schema["properties"]
        assert "row_count" in schema["required"]


@pytest.mark.parametrize("row_count", [0, 2, -1])
def test_a_result_cannot_claim_a_different_row_count(row_count: int) -> None:
    with pytest.raises(ValidationError):
        QueryResult(
            target_id="sr-test",
            sql="SELECT 1",
            columns=("region",),
            rows=({"region": "a"},),
            truncated=False,
            collected_at=NOW,
            elapsed_ms=0,
            row_count=row_count,
        )


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


async def test_read_schema_is_code_generated_and_bound() -> None:
    drv = driver()
    rows = await adapter(drv).read_schema()

    statements = [(sql, args) for conn in drv.connections for sql, args in conn.executed[2:]]
    assert statements == [
        (SCHEMA_OBJECTS_SQL, (100, *SYSTEM_DATABASES, 51)),
        (SCHEMA_COLUMNS_SQL, (100, *SYSTEM_DATABASES, 501)),
        (SCHEMA_IDS_SQL, (*SYSTEM_DATABASES, 51)),
    ]
    # 每条读取一条新连接、会话限额先设置并回读；模板没有可注入的字符串拼接。
    assert all(
        conn.executed[:2] == [(SESSION_SET, None), (SESSION_READ, None)] for conn in drv.connections
    )
    assert len(rows.objects) == 2 and len(rows.columns) == 5 and len(rows.ids) == 2
    assert rows.objects[0]["created"] == "2026-09-01T08:00:00+08:00"
    assert not rows.truncated


async def test_read_schema_marks_any_overflow_as_truncated() -> None:
    small = TARGET.model_copy(
        update={"schema_limits": LIMITS.model_copy(update={"max_columns": 4})}
    )
    rows = await adapter(driver(), small).read_schema()
    assert rows.truncated and len(rows.columns) == 4


async def test_probe_runs_zero_row_selects_on_one_connection() -> None:
    drv = driver(results=schema_results(denied=frozenset({("shop", "regions")})))
    verdicts = await adapter(drv).probe([("shop", "regions"), ("shop", "sales")])

    conn = only(drv)
    assert [sql for sql, _ in conn.executed] == [
        SESSION_SET,
        SESSION_READ,
        "SELECT 1 FROM `shop`.`regions` WHERE 1 = 0",
        "SELECT 1 FROM `shop`.`sales` WHERE 1 = 0",
    ]
    # 拒绝之后同一连接继续探测下一个对象；完整读完才正常关闭。
    assert verdicts == {("shop", "regions"): False, ("shop", "sales"): True}
    assert conn.closed and not conn.aborted


@pytest.mark.parametrize("number", [1142, 5203, 5502, 1146, 5501, 1049])
async def test_probe_denied_or_missing_is_a_definite_no(number: int) -> None:
    error = ProgrammingError(number, f"denied {CANARY}")
    drv = driver(results={probe_sql("shop", "sales"): error})
    assert await adapter(drv).probe([("shop", "sales")]) == {("shop", "sales"): False}


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (OperationalError(1105, f"internal {CANARY}"), Code.QUERY_FAILED),
        (OperationalError(2013, "lost"), Code.CONNECTION_LOST),
        (Result(("1",), [(1,)]), Code.RESULT_CONTRACT),  # 零行探测返回了行
    ],
    ids=["unknown-error", "connection-lost", "returned-rows"],
)
async def test_probe_that_cannot_decide_fails_the_whole_batch(
    outcome: Result | BaseException, code: StarRocksErrorCode
) -> None:
    drv = driver(results={probe_sql("shop", "regions"): outcome})
    error = await failure(adapter(drv).probe([("shop", "sales"), ("shop", "regions")]))
    assert error.code is code
    assert_safe(error)
    assert only(drv).aborted  # 未完成的连接直接断开


@pytest.mark.parametrize(
    "obj", [("shop", "sa`les"), ("sh`op", "sales"), ("shop", ""), ("shop", "a\nb")]
)
async def test_probe_rejects_unsafe_names_before_io(obj: tuple[str, str]) -> None:
    drv = driver()
    error = await failure(adapter(drv).probe([("shop", "sales"), obj]))
    assert error.code is Code.OBJECT_NOT_ALLOWED
    assert drv.attempts == 0


# ---- 表布局（P2 Task 5）---------------------------------------------------------------------

# Task 0 在 4.1.4 上实测：键字段带反引号、以 ", " 分隔，无值时为空串；桶数是 INT。
LAYOUT_SQL = (
    "SELECT TABLE_MODEL AS model, PARTITION_KEY AS partition_key, "
    "DISTRIBUTE_TYPE AS distribute_type, DISTRIBUTE_KEY AS distribute_key, "
    "DISTRIBUTE_BUCKET AS buckets, SORT_KEY AS sort_key, PRIMARY_KEY AS primary_key "
    "FROM information_schema.tables_config "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND TABLE_ENGINE <> %s"
)
SERVER_LAYOUT = (
    "model",
    "partition_key",
    "distribute_type",
    "distribute_key",
    "buckets",
    "sort_key",
    "primary_key",
)


SALES_COLUMNS = frozenset({"region", "total", "note"})


def layout(*rows: tuple[object, ...]) -> Result:
    return Result(SERVER_LAYOUT, list(rows))


async def test_describe_layout_is_code_generated_bound_and_has_no_properties() -> None:
    drv = driver(layout(("DUP_KEYS", "`region`", "HASH", "`region`, `total`", 8, "`region`", "")))
    result = await adapter(drv).describe_layout("shop", "sales", SALES_COLUMNS)

    sql, args = only(drv).executed[-1]
    assert sql == LAYOUT_SQL
    assert args == ("shop", "sales", "VIEW")  # 视图没有布局：不返回行
    assert "PROPERTIES" not in sql and "TABLE_ID" not in sql
    assert result.columns == LAYOUT_COLUMNS == SERVER_LAYOUT
    assert result.rows == (
        {
            "model": "DUP_KEYS",
            "partition_key": "region",
            "distribute_type": "HASH",
            "distribute_key": "region, total",
            "buckets": 8,
            "sort_key": "region",
            "primary_key": "",
        },
    )
    assert not result.truncated and result.sql == LAYOUT_SQL


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("`region`", "region"),
        ("`region`, `note`", "region, note"),
        ("region,total", "region, total"),
        ("", ""),
        (None, None),
        ("`secret`", LAYOUT_HIDDEN),  # 未获准列
        ("`region`, `secret`", LAYOUT_HIDDEN),  # 任一未获准即整段替换，不留下获准部分
        ("`Region`", LAYOUT_HIDDEN),  # 与 allowlist 精确匹配
        ("date_trunc('day', `region`)", LAYOUT_HIDDEN),  # 表达式不能拆成列名
        ("`reg`ion`", LAYOUT_HIDDEN),
        ("`region`,", LAYOUT_HIDDEN),
        ("`name`", LAYOUT_HIDDEN),  # 其他对象的获准列
    ],
)
async def test_layout_keys_show_only_allowed_columns(raw: object, shown: object) -> None:
    keys = ("partition_key", "distribute_key", "sort_key", "primary_key")
    for key in keys:
        row = dict.fromkeys(SERVER_LAYOUT, "")
        row.update(model="DUP_KEYS", distribute_type="HASH", buckets=1, **{key: raw})
        drv = driver(layout(tuple(row.values())))
        (result_row,) = (await adapter(drv).describe_layout("shop", "sales", SALES_COLUMNS)).rows
        assert result_row[key] == shown, key
        assert all(result_row[k] == "" for k in keys if k != key)


async def test_view_has_no_layout_rows() -> None:
    drv = driver(layout())
    result = await adapter(drv).describe_layout("shop", "regions", frozenset({"name"}))
    assert result.rows == () and result.columns == LAYOUT_COLUMNS and not result.truncated


@pytest.mark.parametrize(
    "result",
    [
        Result(SERVER_LAYOUT, [("DUP_KEYS", "", "HASH", "`region`", 1, "", "")] * 2),
        Result(SERVER_LAYOUT[:-1], [("DUP_KEYS", "", "HASH", "`region`", 1, "")]),
        Result((*SERVER_LAYOUT[:-1], "PROPERTIES"), [("DUP_KEYS", "", "HASH", "", 1, "", "x")]),
        layout(("DUP_KEYS", 5, "HASH", "`region`", 1, "", "")),
        layout(("DUP_KEYS", "", "HASH", b"`region`", 1, "", "")),
    ],
    ids=["多于一行", "少一列", "列名不符", "键不是文本", "键是 bytes"],
)
async def test_layout_outside_the_contract_fails_closed(result: Result) -> None:
    drv = driver(result)
    error = await failure(adapter(drv).describe_layout("shop", "sales", SALES_COLUMNS))
    assert error.code is Code.RESULT_CONTRACT
    assert_safe(error)


def test_metadata_sql_bound_covers_the_layout_template() -> None:
    assert metadata_sql_bytes() >= len(LAYOUT_SQL.encode())


# ---- 可信配置与装配 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
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


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        ({"refresh_seconds": 400}, "refresh_seconds 不能大于 max_age_seconds"),
        ({"refresh_timeout_seconds": 90}, "refresh_timeout_seconds 不能大于 refresh_seconds"),
        ({"max_age_seconds": 0}, "greater than 0"),
        ({"max_objects": None}, "max_objects"),
    ],
)
def test_schema_limits_are_bounded(limits: dict[str, object], reason: str) -> None:
    data = TARGET.model_dump()
    data["schema_limits"] = {**data["schema_limits"], **limits}
    with pytest.raises(ValidationError, match=reason):
        StarRocksTarget.model_validate(data)


def test_hand_written_allowlist_reports_the_migration() -> None:
    data = TARGET.model_dump()
    data["policy"] = {**data["policy"], "allowed_objects": ["sales"], "allowed_columns": {}}
    with pytest.raises(ValidationError, match="实际 SELECT 权限"):
        StarRocksTarget.model_validate(data)


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


# ---- 执行计划（P2 Task 3） -----------------------------------------------------------------

SERVER_PLAN_COLUMN = "Explain String"  # Task 0 在 4.1.4 上实测的服务端列名


def explained(sql: str = "SELECT region, total FROM sales"):  # type: ignore[no-untyped-def]
    return guard_explain_query(sql, POLICY)


def plan_result(*lines: object, columns: tuple[str, ...] = (SERVER_PLAN_COLUMN,)) -> Result:
    return Result(columns, [(line,) if len(columns) == 1 else (line, line) for line in lines])


async def test_explain_sends_exactly_the_explicit_level_and_normalized_sql() -> None:
    plan = explained(
        "SELECT region, SUM(total) AS s FROM sales WHERE region = 'a%b' GROUP BY region"
    )
    drv = driver(plan_result("- Output => [1:region]", "    - SCAN [sales]"))

    result = await adapter(drv).explain(plan)

    conn = only(drv)
    assert EXPLAIN_LEVEL == "LOGICAL"  # Task 0 选定；不得是裸 EXPLAIN、ANALYZE 或 SCHEDULER
    assert EXPLAIN_PREFIX == "EXPLAIN LOGICAL "
    assert conn.executed == [
        (SESSION_SET, None),
        (SESSION_READ, None),
        ("EXPLAIN LOGICAL " + plan.normalized_sql, None),  # 无参数：驱动不做 % 插值
    ]
    assert result.sql == "EXPLAIN LOGICAL " + plan.normalized_sql
    assert (conn.closed, conn.aborted) == (True, False)


def test_explain_level_cannot_be_chosen_by_the_caller() -> None:
    """级别是代码常量：``explain`` 只接受 ``ExplainQuery``，没有级别或前缀参数。"""
    assert list(inspect.signature(StarRocksAdapter.explain).parameters) == ["self", "query"]
    assert not {"level", "prefix", "sql"} & {f.name for f in dataclasses.fields(ExplainQuery)}


async def test_explain_result_is_renamed_to_a_single_plan_column() -> None:
    drv = driver(plan_result("- Output => [1:region]", "", "    - SCAN [sales]"))

    result = await adapter(drv).explain(explained())

    assert PLAN_COLUMN == "plan"
    assert result.columns == (PLAN_COLUMN,)
    assert result.rows == (
        {"plan": "- Output => [1:region]"},
        {"plan": ""},
        {"plan": "    - SCAN [sales]"},
    )
    assert (result.row_count, result.truncated, result.target_id) == (3, False, "sr-test")
    assert SERVER_PLAN_COLUMN not in result.model_dump_json()


@pytest.mark.parametrize(
    "outcome",
    [
        plan_result("a", columns=("Explain String", "extra")),
        Result((), []),
        plan_result(1),
        plan_result(None),
        plan_result(b"bytes"),
        plan_result(Decimal("1.5")),
        plan_result("a", 2),  # 第二行才出现非文本
    ],
    ids=["two-columns", "no-columns", "int", "null", "bytes", "decimal", "late-non-text"],
)
async def test_explain_rejects_multi_column_or_non_text_plans(outcome: Result) -> None:
    drv = driver(outcome)
    error = await failure(adapter(drv).explain(explained()))
    assert error.code is Code.RESULT_CONTRACT
    assert_safe(error)
    assert (only(drv).closed, only(drv).aborted) == (False, True)


@pytest.mark.parametrize(
    ("changes", "lines", "kept"),
    [
        ({"max_plan_lines": 3}, ["l0", "l1", "l2", "l3", "l4"], 3),
        # 每行 {"plan": "xxxxxxxx"} 20 字节：2 + 20 + (2 + 20) = 44 <= 50，第三行超出
        ({"max_result_bytes": 50, "max_value_bytes": 50}, ["x" * 8] * 5, 2),
        ({"max_value_bytes": 10}, ["short", "x" * 20, "after"], 1),
    ],
    ids=["lines", "total-bytes", "single-value"],
)
async def test_explain_truncates_at_max_plan_lines_and_bytes(
    changes: dict[str, int], lines: list[str], kept: int
) -> None:
    drv = driver(plan_result(*lines))
    limited = StarRocksTarget.model_validate({**TARGET.model_dump(), **changes})
    result = await adapter(drv, limited).explain(explained())

    assert result.truncated and result.row_count == kept
    assert [r["plan"] for r in result.rows] == lines[:kept]
    assert (only(drv).closed, only(drv).aborted) == (False, True)  # 截断即断开，不 QUIT


async def test_plan_limit_does_not_follow_the_query_row_limit() -> None:
    """计划行数只受 ``max_plan_lines`` 约束；查询的 ``max_rows`` 与用户 LIMIT 不截断计划。"""
    lines = [f"l{i}" for i in range(POLICY.max_rows + 3)]
    drv = driver(plan_result(*lines))
    result = await adapter(drv).explain(explained("SELECT region FROM sales LIMIT 1"))
    assert (result.row_count, result.truncated) == (len(lines), False)


async def test_explain_and_run_query_reject_each_others_products() -> None:
    drv = driver()
    ada = adapter(drv)
    with pytest.raises(TypeError):
        await ada.explain(guarded())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await ada.run_query(explained())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await ada.explain("SELECT region FROM sales")  # type: ignore[arg-type]
    other = guard_explain_query(
        "SELECT region FROM sales", POLICY.model_copy(update={"target_id": "sr-other"})
    )
    with pytest.raises(StarRocksError) as info:
        await ada.explain(other)
    assert info.value.code is Code.OBJECT_NOT_ALLOWED
    assert drv.attempts == 0


async def test_explain_session_readback_mismatch_never_sends_explain() -> None:
    drv = driver(results={SESSION_READ: Result(("q", "m", "t"), [(1, 1, "UTC")])})
    error = await failure(adapter(drv).explain(explained()))
    assert error.code is Code.SESSION_SETUP_FAILED
    conn = only(drv)
    assert [sql for sql, _ in conn.executed] == [SESSION_SET, SESSION_READ]
    assert conn.aborted


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OperationalError(5203, f"Access denied {CANARY}"), Code.PERMISSION_DENIED),
        (OperationalError(5024, f"Query reached its timeout {CANARY}"), Code.SERVER_TIMEOUT),
        (OperationalError(2013, f"Lost connection {CANARY}"), Code.CONNECTION_LOST),
        (ProgrammingError(1064, f"syntax error near '{CANARY}'"), Code.QUERY_FAILED),
    ],
)
async def test_explain_errors_map_like_queries(error: BaseException, code: Code) -> None:
    drv = driver(error)
    result = await failure(adapter(drv).explain(explained()))
    assert result.code is code
    assert_safe(result)
    assert "EXPLAIN" not in repr(result) and "sales" not in str(result)
    assert only(drv).aborted


async def test_explain_client_deadline_and_cancellation_discard_the_connection() -> None:
    drv = driver(plan_result("a", "b"), hang_on="fetch")
    async with asyncio.timeout(5):
        error = await failure(adapter(drv).explain(explained()))
    assert error.code is Code.TIMEOUT
    assert only(drv).aborted

    drv = driver(plan_result("a", "b"), hang_on="fetch")
    ada = adapter(drv, TARGET.model_copy(update={"pool_size": 1}))
    task = asyncio.create_task(ada.explain(explained()))
    while not drv.connections or drv.connections[0].rows_read < 1:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drv.connections[0].aborted and drv.attempts == 1
    drv.make = driver(plan_result("a")).make
    assert (await ada.explain(explained())).row_count == 1  # 槽位已释放，未自动重试


def test_max_plan_lines_must_be_positive() -> None:
    assert TARGET.max_plan_lines == 500
    with pytest.raises(ValidationError):
        StarRocksTarget.model_validate({**TARGET.model_dump(), "max_plan_lines": 0})
