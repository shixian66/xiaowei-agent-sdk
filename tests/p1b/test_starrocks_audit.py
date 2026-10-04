"""P2 Task 6：审计慢查询的 Adapter 契约（recording 驱动替身，不连接任何服务）。

候选查询由代码模板与绑定值构成；每条候选原文在 Adapter 内经当前 SQLGuard 范围
（``guard_explain_query``）检查，未通过、无法读出或放不下的行整行丢弃，原文不离开 Adapter。
替身不能证明 AuditLoader 的实际写入（列、时区、截断），这些由 ``starrocks_audit_real`` 证明。
"""

# ruff: noqa: S608 —— 字符串拼接只用于构造测试中的审计原文与期望的模板文本。

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from asyncmy.errors import OperationalError, ProgrammingError
from pydantic import ValidationError
from tests.p1b.test_starrocks_adapter import (
    NOW,
    POLICY,
    SQL_POLICY,
    TARGET,
    Driver,
    Result,
    assert_safe,
    driver,
    failure,
    only,
)

from xiaowei import starrocks
from xiaowei.sqlguard import QueryPolicy, guard_explain_query
from xiaowei.starrocks import (
    AUDIT_COLUMNS,
    AUDIT_ORDER_COLUMNS,
    AuditSource,
    StarRocksAdapter,
    StarRocksErrorCode,
    StarRocksTarget,
    audit_candidate_bound,
)

Code = StarRocksErrorCode
CANARY = "canary-audit-7f3a"
# AuditLoader 审计表的列（Task 0 实测 5.0.0 DDL）；候选查询按这些名字读取。
SOURCE = (
    "queryId",
    "timestamp",
    "queryTime",
    "scanBytes",
    "scanRows",
    "returnRows",
    "cpuCostNs",
    "memCostBytes",
    "pendingTimeMs",
    "state",
    "digest",
    "db",
    "stmt",
)
AUDIT = AuditSource(
    database="starrocks_audit_db__",
    table="starrocks_audit_tbl__",
    time_zone="UTC",
    stmt_limit=1004,
    max_rows=3,
    candidate_rows=10,
)
AUDIT_TARGET = TARGET.model_copy(
    update={
        "max_result_bytes": 20_000,
        "max_value_bytes": 2000,
        "policy": SQL_POLICY.model_copy(update={"max_sql_bytes": 1000}),
        "audit": AUDIT,
    }
)
# 结构快照给出的 SQLGuard 范围（与 POLICY 相同的对象与列）；审计原文按它检查。
SCOPE = POLICY.model_copy(update={"max_sql_bytes": 1000})
STARTED = datetime(2026, 9, 30, 7, 30)  # 审计表中的无时区 DATETIME（按审计时区写入）


def target(**changes: Any) -> StarRocksTarget:
    """以 AUDIT_TARGET 为基础重新校验：``audit`` 中的键改写审计源，其余改写目标。"""
    data = AUDIT_TARGET.model_dump(mode="json")
    data["audit"].update(changes.pop("audit", {}))
    data["policy"].update(changes.pop("policy", {}))
    data.update(changes)
    return StarRocksTarget.model_validate(data)


def record(stmt: object, query_id: str = "q1", **metrics: object) -> tuple[object, ...]:
    values: dict[str, object] = {
        "queryId": query_id,
        "timestamp": STARTED,
        "queryTime": 6000,
        "scanBytes": 1024,
        "scanRows": 100,
        "returnRows": 5,
        "cpuCostNs": 2_000_000,
        "memCostBytes": 4096,
        "pendingTimeMs": 0,
        "state": "EOF",
        "digest": "abc",
        "db": "shop",  # 语句执行时的会话当前库
        "stmt": stmt,
    }
    values.update(metrics)
    return tuple(values[name] for name in SOURCE)


def audit_driver(*rows: tuple[object, ...], **conn: Any) -> Driver:
    return driver(Result(SOURCE, list(rows)), **conn)


def adapter(drv: Driver, t: StarRocksTarget = AUDIT_TARGET) -> StarRocksAdapter:
    return StarRocksAdapter(t, connect=drv, clock=lambda: NOW)


async def listed(*stmts: object, t: StarRocksTarget = AUDIT_TARGET) -> list[object]:
    rows = [record(s, query_id=f"q{i}") for i, s in enumerate(stmts)]
    result = await adapter(audit_driver(*rows), t).slow_queries(60, "query_time", SCOPE)
    return [row["sql"] for row in result.rows]


# ---- 查询由模板与绑定值构成 ----------------------------------------------------------------


@pytest.mark.parametrize("order_by", sorted(AUDIT_ORDER_COLUMNS))
async def test_slow_query_sql_is_a_template_with_bound_values(order_by: str) -> None:
    drv = audit_driver()
    result = await adapter(drv).slow_queries(90, order_by, SCOPE)

    sql, args = only(drv).executed[-1]
    column = AUDIT_ORDER_COLUMNS[order_by]
    assert sql == (
        "SELECT queryId, `timestamp`, queryTime, scanBytes, scanRows, returnRows, cpuCostNs, "
        "memCostBytes, pendingTimeMs, state, digest, db, "
        "CASE WHEN LENGTH(stmt) <= %s THEN stmt END AS stmt "
        "FROM `starrocks_audit_db__`.`starrocks_audit_tbl__` "
        "WHERE isQuery = 1 AND catalog = %s "
        "AND `timestamp` >= %s AND `timestamp` < %s "
        f"ORDER BY {column} DESC LIMIT %s"
    )
    # NOW 是 08:00 UTC；审计时区为 UTC：窗口为 [06:30, 08:00)，以无时区值绑定。不按库过滤（P2.5
    # Task 6）；多读一条候选，用来判断候选是否已读完。
    end = datetime(2026, 9, 30, 8, 0)
    assert args == (1000, "default_catalog", end - timedelta(minutes=90), end, 11)
    for excluded in (
        "QueriedRelations",
        "`user`",
        "authorizedUser",
        "clientIp",
        "feIp",
        "errorCode",
    ):
        assert excluded not in sql
    assert result.sql == sql and result.columns == AUDIT_COLUMNS


@pytest.mark.parametrize(
    "changes",
    [
        {"database": "a.b"},
        {"table": "t`x"},
        {"table": "t x"},
        {"database": ""},
        {"time_zone": "Mars/Base"},
        {"stmt_limit": 0},
        {"max_window_minutes": 0},
        {"max_rows": 0},
        {"candidate_rows": 0},
        {"max_rows": 20, "candidate_rows": 10},  # 候选少于输出上限
    ],
)
def test_audit_identifiers_are_validated_at_load(changes: dict[str, object]) -> None:
    # 配置值不回显由 ``runtime.load_config`` 统一保证（test_runtime 覆盖）。
    with pytest.raises(ValidationError):
        target(audit=changes)


def test_audit_source_requires_stmt_limit() -> None:
    data = AUDIT_TARGET.model_dump(mode="json")
    del data["audit"]["stmt_limit"]
    with pytest.raises(ValidationError, match="stmt_limit"):
        StarRocksTarget.model_validate(data)


def test_assembly_rejects_sql_limit_not_below_stmt_limit() -> None:
    """AuditLoader 截断后的长度落在 (stmt_limit - 4, stmt_limit]：能读出的原文必须短于该区间。"""
    assert target(audit={"stmt_limit": 1004}).audit is not None
    with pytest.raises(ValidationError, match="stmt_limit"):
        target(audit={"stmt_limit": 1003})


def test_candidate_bytes_must_hold_the_longest_candidate() -> None:
    bound = audit_candidate_bound(AUDIT_TARGET)
    assert target(audit={"candidate_bytes": bound}).audit is not None
    with pytest.raises(ValidationError, match="candidate_bytes"):
        target(audit={"candidate_bytes": bound - 1})


async def test_slow_queries_require_a_configured_audit_source() -> None:
    drv = audit_driver()
    plain = AUDIT_TARGET.model_copy(update={"audit": None})
    with pytest.raises(ValueError, match="审计源"):
        await adapter(drv, plain).slow_queries(60, "query_time", SCOPE)
    with pytest.raises(ValueError, match=r"窗口|排序"):
        await adapter(drv).slow_queries(AUDIT.max_window_minutes + 1, "query_time", SCOPE)
    with pytest.raises(ValueError, match=r"窗口|排序"):
        await adapter(drv).slow_queries(60, "user", SCOPE)
    assert drv.attempts == 0


# ---- 只列出原文通过当前 SQLGuard 范围的记录 -------------------------------------------------

ACCEPTED = [
    "SELECT region, SUM(total) FROM sales GROUP BY region",
    "SELECT name FROM regions",  # 获准视图与表同等
    "WITH t AS (SELECT region, total FROM sales) SELECT region FROM t WHERE total > 1",
    "SELECT region FROM sales WHERE region IN (SELECT region FROM regions)",
    "select region from shop.sales",  # 本库限定名；原文不规范化
]
REJECTED = [
    "SELECT secret FROM sales",  # 未获准列
    "SELECT region FROM customers",  # 未获准对象
    "SELECT region FROM other_db.sales",  # 其他库限定名
    "SELECT MAX(total) FROM sales",  # 未获准函数
    "WITH t AS (SELECT id FROM customers) SELECT id FROM t",  # CTE 引用未获准对象
    "SELECT region FROM sales WHERE region IN (SELECT region FROM customers)",
    "SELECT region FROM sales; SELECT 1",  # 多语句
    "SELECT /* x */ region FROM sales",  # 注释
    "SELECT /*+ SET_VAR(query_timeout=1) */ region FROM sales",  # hint
    "SELECT * FROM sales",
    "not sql at all (",
]
# 小维自身发出的语句：都不能出现在列表中。
OWN = [
    "EXPLAIN LOGICAL SELECT `shop`.`sales`.`region` FROM `shop`.`sales`",
    "SELECT @@query_timeout, @@query_mem_limit, @@time_zone, @@sql_mode",
    "SELECT TABLE_NAME AS name, TABLE_TYPE AS type FROM information_schema.tables "
    "WHERE TABLE_SCHEMA = 'shop' AND TABLE_NAME IN ('regions', 'sales') ORDER BY TABLE_NAME",
    "SELECT queryId, `timestamp` FROM `starrocks_audit_db__`.`starrocks_audit_tbl__` LIMIT 10",
]


async def test_only_statements_passing_sqlguard_are_listed() -> None:
    shown = await listed(*ACCEPTED[:3], t=target(audit={"max_rows": 10}))
    assert shown == ACCEPTED[:3]
    shown = await listed(*ACCEPTED[3:], *REJECTED, t=target(audit={"max_rows": 10}))
    assert shown == ACCEPTED[3:]


async def test_xiaowei_own_statements_are_filtered() -> None:
    ran = guard_explain_query("SELECT region FROM sales", POLICY).normalized_sql
    shown = await listed(*OWN, ran, t=target(audit={"max_rows": 10}))
    # 小维经 run_readonly_query 执行的获准查询是真实的查询记录，照常列出。
    assert shown == [ran]


async def test_rejected_statements_never_leave_the_adapter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    hostile = f"SELECT region FROM customers WHERE note = '{CANARY}'"
    drv = audit_driver(record(hostile, query_id="q-bad"), record(ACCEPTED[0], query_id="q-ok"))
    result = await adapter(drv).slow_queries(60, "query_time", SCOPE)

    assert [row["query_id"] for row in result.rows] == ["q-ok"]
    assert CANARY not in result.model_dump_json() and CANARY not in repr(result)
    assert CANARY not in caplog.text

    broken = audit_driver(record(hostile), record(ACCEPTED[0], cpuCostNs="x"))
    error = await failure(adapter(broken).slow_queries(60, "query_time", SCOPE))
    assert error.code is Code.RESULT_CONTRACT
    assert_safe(error)
    assert CANARY not in repr(error) and CANARY not in caplog.text


# ---- 跨库：未限定名按每条记录自身的会话当前库解析（P2.5 Task 6） --------------------------------

# 三个库：shop 与 archive 都有 sales；crm 只有 customers。
MULTI = QueryPolicy(
    target_id="sr-test",
    tables={
        "shop": {"sales": ("region", "total", "note"), "regions": ("name", "region")},
        "archive": {"sales": ("region", "total")},
        "crm": {"customers": ("id", "tier")},
    },
    allowed_functions=frozenset({"SUM", "COUNT"}),
    max_rows=5,
    max_sql_bytes=1000,
    max_result_columns=10,
)


async def by_database(*rows: tuple[str, str], database: str | None = None) -> list[Any]:
    """每条 (会话当前库, 原文) 一条记录；返回列出的 (query_id, database)。"""
    drv = audit_driver(*(record(sql, query_id=f"q{i}", db=db) for i, (db, sql) in enumerate(rows)))
    t = target(audit={"max_rows": 10})
    result = await adapter(drv, t).slow_queries(60, "query_time", MULTI, database=database)
    return [(row["query_id"], row["database"]) for row in result.rows]


async def test_unqualified_names_resolve_in_each_records_own_database() -> None:
    assert await by_database(
        ("shop", "SELECT region FROM sales"),
        ("archive", "SELECT region FROM sales"),  # 同名表：按本条记录的库解析
        ("crm", "SELECT region FROM sales"),  # crm 没有 sales：不改用别的库，不列出
        ("crm", "SELECT region FROM shop.sales"),  # 显式写出其他库：按快照与权限检查
        ("shop", "SELECT s.region FROM sales s JOIN archive.sales a ON s.region = a.region"),
        ("shop", "SELECT region FROM crm.payroll"),  # 引用未获准对象
        ("archive", "SELECT note FROM sales"),  # archive.sales 没有 note 列
    ) == [("q0", "shop"), ("q1", "archive"), ("q3", "crm"), ("q4", "shop")]


async def test_records_without_a_current_database_need_qualified_names() -> None:
    # 无默认库的会话审计为 ''：未限定表名在数据库中执行即失败（Task 0 §9.7），这里同样不列出；
    # 输出的库为 null。未知库（快照中没有）同样只接受限定名。
    assert await by_database(
        ("", "SELECT region FROM sales"),
        ("", "SELECT region FROM shop.sales"),
        ("nowhere", "SELECT region FROM sales"),
        ("nowhere", "SELECT region FROM archive.sales"),
    ) == [("q1", None), ("q3", "nowhere")]


async def test_database_filter_is_a_bound_value() -> None:
    drv = audit_driver(record("SELECT region FROM sales", db="archive"))
    result = await adapter(drv).slow_queries(60, "query_time", MULTI, database="archive")
    sql, args = only(drv).executed[-1]
    assert "AND db = %s AND `timestamp`" in sql and "archive" not in sql
    assert args is not None and args[2] == "archive"
    assert [row["database"] for row in result.rows] == ["archive"]


async def test_candidates_read_to_the_limit_are_marked_truncated() -> None:
    # 候选恰好读满 candidate_rows 且还有下一条：可能还有未检查的记录，标记截断。
    t = target(audit={"candidate_rows": 10, "max_rows": 3})
    stmts = [*["SELECT secret FROM sales"] * 10, ACCEPTED[0]]
    rows = [record(s, query_id=f"q{i}") for i, s in enumerate(stmts)]
    drv = audit_driver(*rows)
    result = await adapter(drv, t).slow_queries(60, "query_time", SCOPE)
    assert result.rows == () and result.truncated
    assert only(drv).rows_read == 11


# ---- 有界读取 -------------------------------------------------------------------------------


def long_stmt(size: int, tag: str = "") -> str:
    """恰好 ``size`` 字节、能通过 SQLGuard 的获准查询。"""
    head = f"SELECT region FROM sales WHERE note = '{tag}"
    return head + "x" * (size - len(head) - 1) + "'"


async def test_candidates_are_bounded_by_rows_and_bytes() -> None:
    bound = audit_candidate_bound(AUDIT_TARGET)
    t = target(audit={"candidate_rows": 100, "candidate_bytes": bound, "max_rows": 100})
    rows = [record(long_stmt(900, f"{i:03d}"), query_id=f"q{i}") for i in range(80)]
    drv = audit_driver(*rows)
    result = await adapter(drv, t).slow_queries(60, "scan_rows", SCOPE)

    conn = only(drv)
    assert conn.executed[-1][1][-1] == 101  # LIMIT 绑定 candidate_rows + 1
    # 读到 candidate_bytes 即停止并断开；已读候选照常过滤，结果标记截断（可能还有未检查的候选）。
    assert result.truncated and (conn.aborted, conn.closed) == (True, False)
    assert 0 < conn.rows_read < len(rows)
    # 最终结果另受 max_result_bytes 约束。
    assert len(json.dumps([dict(r) for r in result.rows], ensure_ascii=False).encode()) <= (
        t.max_result_bytes
    )


async def test_long_statement_does_not_crowd_out_other_metrics() -> None:
    # 规范化会补全库名：原文须留出余量，使规范化结果仍不超过 max_sql_bytes。
    long = long_stmt(900)
    drv = audit_driver(
        record(long, "q-long"), record(ACCEPTED[0], "q-a"), record(ACCEPTED[1], "q-b")
    )
    result = await adapter(drv).slow_queries(60, "query_time", SCOPE)
    assert [row["query_id"] for row in result.rows] == ["q-long", "q-a", "q-b"]
    assert result.rows[0]["sql"] == long and not result.truncated
    conn = only(drv)
    assert (conn.closed, conn.aborted) == (True, False)


async def test_unreadable_or_oversized_statements_drop_the_row() -> None:
    small = target(max_value_bytes=300)
    shown = await listed(None, long_stmt(400), ACCEPTED[0], t=small)
    # 超过 max_sql_bytes 的原文没有读出（NULL）；JSON 放不下单值上限的原文同样丢弃；
    # 不出现“SQL 未显示”的行。
    assert shown == [ACCEPTED[0]]


async def test_a_permitted_row_too_large_to_show_marks_the_list_truncated() -> None:
    small = target(max_value_bytes=300)
    rows = [record(long_stmt(400), "q-long"), record(ACCEPTED[0], "q-a")]
    result = await adapter(audit_driver(*rows), small).slow_queries(60, "query_time", SCOPE)
    # 获准却放不下单值上限的行不列出，结果因此不完整：标记截断。
    assert [row["query_id"] for row in result.rows] == ["q-a"] and result.truncated
    # 原文不可读（NULL）只是无法确认获准，与上限无关：不标记截断。
    rows = [record(None, "q-null"), record(ACCEPTED[0], "q-a")]
    result = await adapter(audit_driver(*rows), small).slow_queries(60, "query_time", SCOPE)
    assert [row["query_id"] for row in result.rows] == ["q-a"] and not result.truncated


async def test_list_can_be_shorter_than_max_rows() -> None:
    t = target(audit={"candidate_rows": 10, "max_rows": 3})
    stmts = [*REJECTED[:9], ACCEPTED[0]]
    rows = [record(s, query_id=f"q{i}") for i, s in enumerate(stmts)]
    result = await adapter(audit_driver(*rows), t).slow_queries(60, "query_time", SCOPE)
    assert [row["sql"] for row in result.rows] == [ACCEPTED[0]]
    assert not result.truncated


@pytest.mark.parametrize(
    ("stmts", "truncated"),
    [
        # 合格行多于 max_rows：只列前 3 条，后面还有未检查的候选，结果不完整。
        ([ACCEPTED[i % 3] for i in range(6)], True),
        # 恰好 max_rows 条且候选已读完：完整。
        (ACCEPTED[:3], False),
        # 达到 max_rows 时仍有候选未检查（即使它们最终都不获准）：保守标记截断，不逐条解析到底。
        ([*ACCEPTED[:3], *REJECTED[:4]], True),
    ],
    ids=["合格行超过上限", "恰好上限且读完", "上限之后仍有未检查候选"],
)
async def test_reaching_max_rows_with_candidates_left_is_truncated(
    stmts: list[str], truncated: bool
) -> None:
    rows = [record(s, query_id=f"q{i}") for i, s in enumerate(stmts)]
    result = await adapter(audit_driver(*rows)).slow_queries(60, "query_time", SCOPE)
    assert [row["query_id"] for row in result.rows] == ["q0", "q1", "q2"]
    assert result.truncated is truncated


# ---- 时区与指标 -----------------------------------------------------------------------------


async def test_audit_window_uses_the_audit_time_zone() -> None:
    """目标连接时区为 Asia/Shanghai，审计按 UTC 写入：窗口与 started_at 都按审计时区。"""
    late = datetime(2026, 9, 30, 23, 50, tzinfo=UTC)  # 上海已是 10-01
    drv = audit_driver(record(ACCEPTED[0], timestamp=datetime(2026, 9, 30, 23, 40)))
    ada = StarRocksAdapter(AUDIT_TARGET, connect=drv, clock=lambda: late)
    result = await ada.slow_queries(30, "query_time", SCOPE)

    args = only(drv).executed[-1][1]
    assert args is not None
    assert args[2:4] == (datetime(2026, 9, 30, 23, 20), datetime(2026, 9, 30, 23, 50))
    assert result.rows[0]["started_at"] == "2026-09-30T23:40:00+00:00"

    shanghai = target(audit={"time_zone": "Asia/Shanghai"})
    drv = audit_driver(record(ACCEPTED[0], timestamp=datetime(2026, 10, 1, 7, 40)))
    ada = StarRocksAdapter(shanghai, connect=drv, clock=lambda: late)
    result = await ada.slow_queries(30, "query_time", SCOPE)
    args = only(drv).executed[-1][1]
    assert args is not None
    assert args[2:4] == (datetime(2026, 10, 1, 7, 20), datetime(2026, 10, 1, 7, 50))
    assert result.rows[0]["started_at"] == "2026-10-01T07:40:00+08:00"


@pytest.mark.parametrize("unknown", [None, "", -1])
async def test_unknown_metrics_are_null_not_zero(unknown: object) -> None:
    metrics = {
        "queryTime": unknown,
        "scanBytes": unknown,
        "scanRows": unknown,
        "returnRows": unknown,
        "cpuCostNs": unknown,
        "memCostBytes": unknown,
        "pendingTimeMs": unknown,
        "state": "" if unknown != -1 else "EOF",
        "digest": "" if unknown != -1 else "d",
    }
    drv = audit_driver(record(ACCEPTED[0], **metrics), record(ACCEPTED[1], pendingTimeMs=0))
    first, second = (await adapter(drv).slow_queries(60, "query_time", SCOPE)).rows
    for name in ("query_time_ms", "scan_bytes", "scan_rows", "return_rows", "cpu_ms", "mem_bytes"):
        assert first[name] is None, name
    assert first["pending_ms"] is None
    if unknown != -1:
        assert (first["state"], first["digest"]) == (None, None)
    assert second["pending_ms"] == 0  # 0 是实测值，不是未知


@pytest.mark.parametrize(
    ("ns", "ms"),
    [
        (1_234_567, "1.235"),
        (1_234_499, "1.234"),
        (0, "0.000"),
        (999, "0.001"),
        (9_223_372_036_854_775_807, "9223372036854.776"),
    ],
)
async def test_cpu_nanoseconds_are_converted_to_milliseconds_exactly(ns: int, ms: str) -> None:
    drv = audit_driver(record(ACCEPTED[0], cpuCostNs=ns))
    (row,) = (await adapter(drv).slow_queries(60, "cpu", SCOPE)).rows
    assert row["cpu_ms"] == ms


async def test_output_columns_are_fixed_and_exclude_identity_fields() -> None:
    drv = audit_driver(record(ACCEPTED[0]))
    (row,) = (await adapter(drv).slow_queries(60, "query_time", SCOPE)).rows
    assert (
        tuple(row)
        == AUDIT_COLUMNS
        == (
            "query_id",
            "started_at",
            "query_time_ms",
            "scan_bytes",
            "scan_rows",
            "return_rows",
            "cpu_ms",
            "mem_bytes",
            "pending_ms",
            "state",
            "digest",
            "database",
            "sql",
        )
    )
    assert row["sql"] == ACCEPTED[0] and row["query_time_ms"] == 6000 and row["cpu_ms"] == "2.000"


@pytest.mark.parametrize(
    "result",
    [
        Result((*SOURCE, "user"), [(*record(ACCEPTED[0]), CANARY)]),
        Result(SOURCE[:-1], [record(ACCEPTED[0])[:-1]]),
        Result(SOURCE, [record(ACCEPTED[0], scanRows="12")]),
        Result(SOURCE, [record(ACCEPTED[0], queryId=7)]),
        Result(SOURCE, [record(b"SELECT region FROM sales")]),
    ],
    ids=["多出身份列", "缺少原文列", "指标是文本", "queryId 不是文本", "原文是 bytes"],
)
async def test_audit_result_outside_the_contract_fails_closed(result: Result) -> None:
    drv = driver(result)
    error = await failure(adapter(drv).slow_queries(60, "query_time", SCOPE))
    assert error.code is Code.RESULT_CONTRACT
    assert_safe(error)
    assert CANARY not in repr(error)


# ---- 执行失败可区分，不重试 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ProgrammingError(5203, f"Access denied {CANARY}"), Code.PERMISSION_DENIED),
        (ProgrammingError(5502, f"Unknown table {CANARY}"), Code.OBJECT_MISSING),
        (ProgrammingError(1146, f"Table doesn't exist {CANARY}"), Code.OBJECT_MISSING),
        (OperationalError(5024, f"timeout {CANARY}"), Code.SERVER_TIMEOUT),
    ],
)
async def test_audit_failures_are_distinguished(error: BaseException, code: Code) -> None:
    drv = audit_driver()
    drv.make = driver(error).make
    failed = await failure(adapter(drv).slow_queries(60, "query_time", SCOPE))
    assert failed.code is code
    assert_safe(failed)
    conn = only(drv)
    assert conn.aborted


async def test_parsing_stops_once_the_client_deadline_has_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选在线程中逐行检查；期限到达时本次调用按超时失败，线程也随即停止，不在后台检查完剩余候选。"""
    checked: list[str] = []
    real = starrocks._within_scope

    def slow(sql: str, policy: Any, database: str) -> bool:
        checked.append(sql)
        time.sleep(0.05)
        return bool(real(sql, policy, database))

    monkeypatch.setattr(starrocks, "_within_scope", slow)
    rows = [record("SELECT secret FROM sales", query_id=f"q{i}") for i in range(100)]
    t = target(audit={"candidate_rows": 100})  # 客户端期限 1 秒，约可检查 20 行
    failed = await failure(adapter(audit_driver(*rows), t).slow_queries(60, "query_time", SCOPE))
    assert failed.code is Code.TIMEOUT
    at_deadline = len(checked)
    await asyncio.sleep(0.5)
    assert at_deadline < 100 and len(checked) <= at_deadline + 1  # 至多正在检查的那一行


async def test_audit_client_deadline_disconnects_without_retry() -> None:
    drv = audit_driver(record(ACCEPTED[0]), record(ACCEPTED[1]), hang_on="fetch")
    failed = await failure(adapter(drv).slow_queries(60, "query_time", SCOPE))
    assert failed.code is Code.TIMEOUT
    assert only(drv).aborted
