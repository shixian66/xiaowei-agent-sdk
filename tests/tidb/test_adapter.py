"""TiDB 协议夹具来自批准文档/源码；现场校准前不代表真实输出。"""

from importlib import import_module

import pytest
from asyncmy.errors import ProgrammingError
from tests.p1b.test_starrocks_adapter import NOW, Driver, FakeConnection, Result
from tests.p1b.test_starrocks_adapter import TARGET as SR

from xiaowei.sqlguard import QueryPolicy, guard_readonly_query
from xiaowei.starrocks import StarRocksErrorCode

SQL_MODE = (
    "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_AUTO_CREATE_USER,NO_ENGINE_SUBSTITUTION"
)
SESSION_SET = (
    "SET SESSION max_execution_time = 1000, tidb_mem_quota_query = 1073741824, "
    "time_zone = 'Asia/Shanghai', sql_mode = '" + SQL_MODE + "', "
    "character_set_client = 'utf8mb4', character_set_connection = 'utf8mb4', "
    "character_set_results = 'utf8mb4', collation_connection = 'utf8mb4_bin'"
)
SESSION_READ = (
    "SELECT @@SESSION.max_execution_time, @@SESSION.tidb_mem_quota_query, "
    "@@SESSION.time_zone, @@SESSION.sql_mode, @@SESSION.character_set_client, "
    "@@SESSION.character_set_connection, @@SESSION.character_set_results, "
    "@@SESSION.collation_connection, DATABASE()"
)
VERSION_SQL = "SELECT tidb_version()"
PROTECTION_SET = "SET SESSION tidb_opt_enable_non_eval_scalar_subquery = ON"
PROTECTION_READ = "SELECT @@SESSION.tidb_opt_enable_non_eval_scalar_subquery"
WARNINGS_SQL = "SHOW WARNINGS"  # 两版实测均不支持 LIMIT；由非缓冲读取和容量限制截断。
VALUES = (
    1000,
    1073741824,
    "Asia/Shanghai",
    SQL_MODE,
    "utf8mb4",
    "utf8mb4",
    "utf8mb4",
    "utf8mb4_bin",
    "app",
)
QUERY = "SELECT id FROM orders LIMIT 3"
POLICY = QueryPolicy(
    target_id="tidb-a",
    database_type="tidb",
    default_database="app",
    tables={"app": {"orders": ("id",)}},
    allowed_functions=frozenset(),
    max_rows=5,
    max_sql_bytes=4000,
    max_result_columns=10,
)


def target(**overrides):
    tidb = import_module("xiaowei.tidb")
    fields = SR.model_dump()
    fields.update(target_id="tidb-a", host="tidb.internal", port=4000, database="app")
    fields.update(overrides)
    return tidb.TiDBTarget(**fields)


def results(version="7.5.3", *, values=VALUES, protection=1, query=None, warnings=()):
    return {
        VERSION_SQL: Result(
            ("tidb_version()",), [("Release Version: v" + version + "\nEdition: Community",)]
        ),
        SESSION_SET: Result(()),
        SESSION_READ: Result(tuple("v" + str(i) for i in range(9)), [values]),
        PROTECTION_SET: Result(()),
        PROTECTION_READ: Result(("v",), [(protection,)]),
        QUERY: Result(("id",), [(7,), (8,)]) if query is None else query,
        WARNINGS_SQL: Result(("Level", "Code", "Message"), warnings),
    }


def adapter(outcomes, **overrides):
    tidb = import_module("xiaowei.tidb")
    drv = Driver(lambda: FakeConnection(outcomes.copy()))
    return tidb.TiDBAdapter(target(**overrides), connect=drv, clock=lambda: NOW), drv


def statements(drv):
    return [sql for conn in drv.connections for sql, _ in conn.executed]


@pytest.mark.parametrize("version", ["6.5.5", "7.5.3", "unknown"])
async def test_session_context_is_confirmed_before_business_sql(version):
    db, drv = adapter(results(version))
    result = await db.run_query(guard_readonly_query(QUERY, POLICY))
    sent = statements(drv)
    assert sent[0] == VERSION_SQL
    assert sent.index(SESSION_SET) < sent.index(SESSION_READ) < sent.index(QUERY)
    assert (PROTECTION_SET in sent) == (version == "7.5.3")
    assert result.rows == ({"id": 7}, {"id": 8})
    assert result.sql == QUERY and result.target_id == "tidb-a"
    assert all("GLOBAL" not in sql and "USE " not in sql for sql in sent)
    assert result.warnings == ()


@pytest.mark.parametrize("extra", ["NO_BACKSLASH_ESCAPES", "ANSI_QUOTES", "PIPES_AS_CONCAT"])
async def test_unsafe_sql_mode_never_reaches_business_query(extra):
    tidb = import_module("xiaowei.tidb")
    values = (*VALUES[:3], SQL_MODE + "," + extra, *VALUES[4:])
    db, drv = adapter(results(values=values))
    with pytest.raises(tidb.TiDBError) as failure:
        await db.run_query(guard_readonly_query(QUERY, POLICY))
    assert failure.value.code is StarRocksErrorCode.SESSION_SETUP_FAILED
    assert QUERY not in statements(drv)
    assert drv.connections[0].aborted


async def test_sql_mode_comparison_ignores_only_order():
    values = (*VALUES[:3], ",".join(reversed(SQL_MODE.split(","))), *VALUES[4:])
    db, _ = adapter(results(values=values))
    assert (await db.run_query(guard_readonly_query(QUERY, POLICY))).row_count == 2


async def test_connection_database_must_match_guard_default():
    tidb = import_module("xiaowei.tidb")
    db, drv = adapter(results(values=(*VALUES[:-1], "other")))
    with pytest.raises(tidb.TiDBError) as failure:
        await db.run_query(guard_readonly_query(QUERY, POLICY))
    assert failure.value.code is StarRocksErrorCode.SESSION_SETUP_FAILED
    assert QUERY not in statements(drv)


@pytest.mark.parametrize("protected", [0, "OFF", None])
async def test_unconfirmed_protection_never_reaches_workload(protected):
    tidb = import_module("xiaowei.tidb")
    db, drv = adapter(results(protection=protected))
    with pytest.raises(tidb.TiDBError) as failure:
        await db.run_query(guard_readonly_query(QUERY, POLICY))
    assert failure.value.code is StarRocksErrorCode.SESSION_SETUP_FAILED
    assert QUERY not in statements(drv)


async def test_old_server_misidentified_as_new_fails_before_workload():
    tidb = import_module("xiaowei.tidb")
    outcomes = results()
    outcomes[PROTECTION_SET] = ProgrammingError(1193, "Unknown system variable")
    db, drv = adapter(outcomes)
    with pytest.raises(tidb.TiDBError) as failure:
        await db.run_query(guard_readonly_query(QUERY, POLICY))
    assert failure.value.code is StarRocksErrorCode.SESSION_SETUP_FAILED
    assert QUERY not in statements(drv)
    assert failure.value.__context__ is None


@pytest.mark.parametrize(
    "error",
    [
        ProgrammingError(1064, "Getting syntax error at line 1, column 1"),
        ProgrammingError(8001, "memory limit exceeded"),
    ],
)
async def test_tidb_database_failure_never_enables_starrocks_parse_recovery(error):
    tidb = import_module("xiaowei.tidb")
    db, drv = adapter(results(query=error))
    with pytest.raises(tidb.TiDBError) as failure:
        await db.run_query(guard_readonly_query(QUERY, POLICY))
    assert not failure.value.parse_failed
    assert statements(drv).count(QUERY) == 1
    assert WARNINGS_SQL not in statements(drv)
    assert failure.value.__context__ is None


async def test_database_warnings_are_collected_on_the_same_connection():
    db, drv = adapter(results(warnings=[("Warning", 1105, "one node omitted")]))
    result = await db.run_query(guard_readonly_query(QUERY, POLICY))
    assert result.warnings == ({"level": "Warning", "code": 1105, "message": "one node omitted"},)
    assert statements(drv)[-2:] == [QUERY, WARNINGS_SQL]
    assert len(drv.connections) == 1


async def test_truncated_result_still_collects_bounded_warnings():
    outcomes = results(query=Result(("id",), [(i,) for i in range(6)]))
    sql = "SELECT id FROM orders LIMIT 6"
    outcomes[sql] = outcomes[QUERY]
    db, drv = adapter(outcomes)
    result = await db.run_query(guard_readonly_query("SELECT id FROM orders", POLICY))
    assert result.truncated and result.row_count == 5
    assert result.warnings == ()
    assert statements(drv)[-1] == WARNINGS_SQL


async def test_warning_read_failure_aborts_instead_of_returning_silent_success():
    tidb = import_module("xiaowei.tidb")
    outcomes = results()
    outcomes[WARNINGS_SQL] = ProgrammingError(1105, "warning read failed")
    db, drv = adapter(outcomes)
    with pytest.raises(tidb.TiDBError):
        await db.run_query(guard_readonly_query(QUERY, POLICY))
    assert statements(drv).count(QUERY) == 1


@pytest.mark.parametrize(
    "sql",
    [
        r"SELECT 'x\' FROM app.orders FOR UPDATE -- '",
        r"SELECT 'x\', SLEEP(1) -- '",
        r"SELECT 'x\', GET_LOCK(1,1) -- '",
    ],
)
async def test_escaped_literal_counterexample_is_stopped_by_session_mode_readback(sql):
    tidb = import_module("xiaowei.tidb")
    values = (*VALUES[:3], SQL_MODE + ",NO_BACKSLASH_ESCAPES", *VALUES[4:])
    db, drv = adapter(results(values=values))
    guarded = guard_readonly_query(sql, POLICY)
    assert not guarded.referenced_objects
    with pytest.raises(tidb.TiDBError) as failed:
        await db.run_query(guarded)
    assert failed.value.code is StarRocksErrorCode.SESSION_SETUP_FAILED
    assert guarded.executed_sql not in statements(drv)


async def test_database_warning_count_and_capacity_are_explicit():
    warnings = [("Warning", 1105, "notice " + str(i)) for i in range(6)]
    db, _ = adapter(results(warnings=warnings))
    result = await db.run_query(guard_readonly_query(QUERY, POLICY))
    assert len(result.warnings) == 5 and result.warnings_truncated
    db, _ = adapter(
        results(warnings=[("Warning", 1105, "x" * 300)]), max_result_bytes=100, max_value_bytes=20
    )
    limited = await db.run_query(guard_readonly_query(QUERY, POLICY))
    assert limited.warnings == () and limited.warnings_truncated


async def test_warning_read_honors_the_same_client_deadline_and_closes_connection():
    tidb = import_module("xiaowei.tidb")
    drv = Driver(lambda: FakeConnection(results(), hang_on=WARNINGS_SQL))
    db = tidb.TiDBAdapter(target(), connect=drv, clock=lambda: NOW)
    with pytest.raises(tidb.TiDBError) as failed:
        await db.run_query(guard_readonly_query(QUERY, POLICY))
    assert failed.value.code is StarRocksErrorCode.TIMEOUT
    assert drv.connections[0].aborted and statements(drv).count(QUERY) == 1
