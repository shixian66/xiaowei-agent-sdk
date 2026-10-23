"""安全采集：真实 Adapter + recording 驱动；不证明公司 3.5.14 谓词语义。"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from asyncmy.errors import ProgrammingError
from tests.p1b.test_starrocks_adapter import Result, driver, failure, only
from tests.p1b.test_starrocks_audit import AUDIT_TARGET, adapter, target
from tests.p1b.test_starrocks_security import SIGNAL_CASES, analyze, batch

from xiaowei.starrocks import (
    SECURITY_DELETE_PATTERN,
    SECURITY_MULTIPLE_PATTERN,
    SECURITY_OPERATION_PATTERN,
    SECURITY_OR_PATTERN,
    StarRocksErrorCode,
    audit_candidate_bound,
)

START = datetime(2026, 10, 22, 10, tzinfo=UTC)
END = START + timedelta(hours=1)
SOURCE = (
    "timestamp", "queryId", "stmt", "state", "queryTime", "user", "clientIp", "db",
    "pendingTimeMs", "stmt_present",
)
TOTALS = ("window_rows", "window_failed_rows", "window_oversize_rows")


def record(stmt="GRANT SELECT ON shop.t TO u", **updates):
    row = dict(zip(SOURCE, (
        START.replace(tzinfo=None), "q1", stmt, "OK", 5, "etl", "192.0.2.1", "shop", 0, 1,
    ), strict=True))
    row.update(updates)
    return tuple(row[k] for k in SOURCE)


def collected(*rows, totals=(237398, 3, 135), aggregate=None, columns=SOURCE, **conn):
    agg = aggregate if aggregate is not None else Result(TOTALS, [totals])
    return driver(lambda args: agg if len(args) == 3 else Result(columns, rows), **conn)


async def test_security_uses_one_prepared_connection_and_two_bounded_templates():
    drv = collected(record())
    result = await adapter(drv).security_audit(START, END)
    conn = only(drv)
    aggregate, candidates = conn.executed[2:]
    assert len(conn.executed) == 4  # SET、会话回读、聚合、候选。
    assert "COUNT(*)" in aggregate[0] and "LIMIT" not in aggregate[0]
    assert "REGEXP" not in aggregate[0] and "LIKE" not in aggregate[0]
    assert aggregate[1] == (1000, START.replace(tzinfo=None), END.replace(tzinfo=None))
    assert candidates[1][:3] == aggregate[1]
    assert candidates[1][-1] == AUDIT_TARGET.audit.candidate_rows + 1
    assert "CASE WHEN LENGTH(stmt) <= %s THEN stmt END" in candidates[0]
    assert "ORDER BY `timestamp`, queryId" in candidates[0]
    assert all("isQuery" not in sql and "catalog" not in sql for sql, _ in (aggregate, candidates))
    assert all(not sql.startswith("EXPLAIN") for sql, _ in conn.executed)
    assert (result.window_rows, result.window_failed_rows, result.window_oversize_rows) == (
        237398, 3, 135,
    )
    assert result.row_count == 1 and result.rows[0]["stmt"].startswith("GRANT")
    assert result.rows[0]["timestamp"] == START.isoformat()
    assert not result.truncated and conn.closed and not conn.aborted


async def test_zero_candidates_preserves_window_totals():
    result = await adapter(collected()).security_audit(START, END)
    assert result.row_count == 0 and result.rows == ()
    assert (result.window_rows, result.window_failed_rows) == (237398, 3)


async def test_candidate_cap_does_not_cap_or_recompute_global_counts():
    drv = collected(*(record(queryId=f"q{i}") for i in range(11)))
    result = await adapter(drv).security_audit(START, END)
    assert result.row_count == 10 and result.truncated
    assert result.window_rows == 237398 and result.window_failed_rows == 3
    assert only(drv).aborted


async def test_candidate_bytes_can_limit_before_candidate_row_cap():
    t = target(audit={
        "candidate_rows": 200, "candidate_bytes": audit_candidate_bound(AUDIT_TARGET),
    })
    drv = collected(*(record("x" * 1000, user="u" * 1000, clientIp="i" * 1000, db="d" * 1000)
                      for _ in range(30)))
    result = await adapter(drv, t).security_audit(START, END)
    assert result.truncated and 0 < result.row_count < 30
    assert result.window_rows == 237398 and only(drv).aborted


@pytest.mark.parametrize("aggregate", [
    Result(("wrong",), [(0,)]), Result(TOTALS, []), Result(TOTALS, [(0, 0, 0), (0, 0, 0)]),
    Result(TOTALS, [(1, "0", 0)]), Result(TOTALS, [(1, -1, 0)]),
    ProgrammingError(5502, "secret-raw-audit-canary"),
])
async def test_bad_aggregate_cannot_fall_back_to_candidate_counts(aggregate):
    drv = collected(record(), aggregate=aggregate)
    error = await failure(adapter(drv).security_audit(START, END))
    assert error.code in {StarRocksErrorCode.RESULT_CONTRACT, StarRocksErrorCode.OBJECT_MISSING}
    assert len(only(drv).executed) == 3 and only(drv).aborted
    assert "secret-raw" not in str(error)


async def test_wrong_candidate_columns_fail_instead_of_fabricating_coverage():
    drv = collected(columns=("wrong",))
    error = await failure(adapter(drv).security_audit(START, END))
    assert error.code == StarRocksErrorCode.RESULT_CONTRACT and only(drv).aborted


async def test_collection_timeout_aborts_without_retry():
    drv = collected(record(), hang_on="fetch")
    t = AUDIT_TARGET.model_copy(update={"client_timeout_seconds": 0.01})
    error = await failure(adapter(drv, t).security_audit(START, END))
    assert error.code == StarRocksErrorCode.TIMEOUT and only(drv).aborted


@pytest.mark.parametrize(("start", "end"), [
    (END, START), (START, START), (START.replace(tzinfo=None), END),
    (START, START + timedelta(days=10)),
])
async def test_invalid_window_is_rejected_before_connection(start, end):
    drv = collected()
    with pytest.raises(ValueError):
        await adapter(drv).security_audit(start, end)
    assert drv.attempts == 0


def regexp(pattern, text):
    # 仅离线包含关系探针：POSIX 空白类转为 Python 类，不证明 RE2/3.5.14 实际语义。
    pattern = pattern.replace("[^[:space:];]", r"[^\s;]").replace("[[:space:]]", r"\s")
    return re.search(pattern, text) is not None


def coarse(sql):
    upper = sql.upper()
    return (regexp(SECURITY_OR_PATTERN, upper) or "SLEEP" in upper
            or regexp(SECURITY_MULTIPLE_PATTERN, sql)
            or regexp(SECURITY_OPERATION_PATTERN, upper)
            or (regexp(SECURITY_DELETE_PATTERN, upper) and "WHERE" not in upper))


@pytest.mark.parametrize("sql", [
    *[sql for sql, _, _ in SIGNAL_CASES if not sql.startswith("WITH")],
    "SELECT 1OR 1=1", "SELECT 0\nOR\t((1=1))", "SELECT 0 OR \"x\"=\"x\"",
    "/* head */ GRANT SELECT ON t TO u", "-- head\n/* x */ DELETE FROM t",
    "# head\nDROP DATABASE d", "CREATE/*x*/ROLE r", "INSERT/*x*/OVERWRITE t SELECT 1",
    "SELECT 1; /* head */ DROP TABLE t", "SELECT 1;;\nDELETE FROM t",
    "SELECT SLEEP /* head */ (1)",
])
def test_coarse_union_includes_declared_program_positives(sql):
    assert analyze(batch(sql)).matched_rows == 1
    assert coarse(sql)


@pytest.mark.parametrize("sql", [
    "SELECT floor(1)", "SELECT 1 OR column_a=1", "CREATE TABLE t AS SELECT 1",
    "ALTER TABLE t DROP PARTITION p", "INSERT INTO t SELECT 1", "SELECT 1;",
])
def test_business_keywords_do_not_flood_coarse_candidates(sql):
    assert not coarse(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 0 OR/**/(1=1)", "WITH x AS (SELECT 1) DELETE FROM t",
    "DELETE FROM t USING (SELECT 1 WHERE 1=1) x", "/* WHERE */ DELETE FROM t",
])
def test_known_exclusions_are_not_claimed_as_end_to_end_coverage(sql):
    assert analyze(batch(sql)).matched_rows == 1
    assert not coarse(sql)
