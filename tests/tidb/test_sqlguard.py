"""TiDB SQLGuard 离线边界；预期由固定 SQL 原文和手工对象集合给出。"""

import pytest

from xiaowei.sqlguard import (
    QueryPolicy,
    QueryRejectedError,
    guard_explain_query,
    guard_readonly_query,
)


def tidb_policy() -> QueryPolicy:
    return QueryPolicy(
        target_id="tidb-a",
        database_type="tidb",
        default_database="app",
        tables={"app": {"orders": ("id", "status", "amount")}, "other": {"orders": ("id",)}},
        allowed_functions=frozenset({"SUM", "COUNT", "ROW_NUMBER"}),
        max_rows=1000,
        max_sql_bytes=5000,
        max_result_columns=10,
    )


@pytest.fixture
def policy() -> QueryPolicy:
    return tidb_policy()


def test_unqualified_table_uses_only_trusted_default_database() -> None:
    policy = tidb_policy()
    query = guard_readonly_query("SELECT id FROM orders", policy)
    assert query.executed_sql == "SELECT id FROM orders LIMIT 1001"
    assert query.referenced_objects == frozenset({("app", "orders")})
    assert query.referenced_columns == frozenset({("app", "orders", "id")})


@pytest.mark.parametrize(
    ("sql", "executed", "requested", "applied"),
    [
        ("SELECT id FROM app.orders", "SELECT id FROM app.orders LIMIT 1001", None, 1001),
        ("SELECT id FROM app.orders LIMIT 0", "SELECT id FROM app.orders LIMIT 0", 0, 0),
        ("SELECT id FROM app.orders LIMIT 20", "SELECT id FROM app.orders LIMIT 20", 20, 20),
        (
            "SELECT id FROM app.orders LIMIT 1000",
            "SELECT id FROM app.orders LIMIT 1000",
            1000,
            1000,
        ),
        (
            "SELECT id FROM app.orders LIMIT 1001",
            "SELECT id FROM app.orders LIMIT 1001",
            1001,
            1001,
        ),
        (
            "SELECT id FROM app.orders LIMIT 1000000000",
            "SELECT id FROM app.orders LIMIT 1001",
            1000000000,
            1001,
        ),
        (
            "SELECT id FROM app.orders LIMIT 1000000000 OFFSET 4",
            "SELECT id FROM app.orders LIMIT 1001 OFFSET 4",
            1000000000,
            1001,
        ),
        (
            "SELECT id FROM app.orders LIMIT 4, 1000000000",
            "SELECT id FROM app.orders LIMIT 4, 1001",
            1000000000,
            1001,
        ),
        ("SELECT id FROM app.orders LIMIT 4, 20", "SELECT id FROM app.orders LIMIT 4, 20", 20, 20),
        (
            "SELECT id FROM app.orders /* tail */",
            "SELECT id FROM app.orders LIMIT 1001 /* tail */",
            None,
            1001,
        ),
        (
            "SELECT id FROM app.orders -- tail",
            "SELECT id FROM app.orders LIMIT 1001 -- tail",
            None,
            1001,
        ),
        (
            "SELECT id FROM app.orders; -- tail",
            "SELECT id FROM app.orders LIMIT 1001 -- tail",
            None,
            1001,
        ),
        (
            "SELECT id FROM app.orders /* tail */;",
            "SELECT id FROM app.orders LIMIT 1001 /* tail */",
            None,
            1001,
        ),
        (
            "SELECT id FROM app.orders LIMIT 1000000000 /* tail */;",
            "SELECT id FROM app.orders LIMIT 1001 /* tail */",
            1000000000,
            1001,
        ),
        (
            "SELECT /* note */ id FROM app.orders",
            "SELECT /* note */ id FROM app.orders LIMIT 1001",
            None,
            1001,
        ),
        (
            "SELECT '/*! FOR UPDATE */' AS note",
            "SELECT '/*! FOR UPDATE */' AS note LIMIT 1001",
            None,
            1001,
        ),
        (
            "SELECT id FROM (SELECT id FROM app.orders LIMIT 1000000000) AS x",
            "SELECT id FROM (SELECT id FROM app.orders LIMIT 1000000000) AS x LIMIT 1001",
            None,
            1001,
        ),
        (
            "WITH x AS (SELECT id FROM app.orders) "
            "SELECT id FROM x UNION ALL SELECT id FROM app.orders LIMIT 1000000000 OFFSET 2",
            "WITH x AS (SELECT id FROM app.orders) "
            "SELECT id FROM x UNION ALL SELECT id FROM app.orders LIMIT 1001 OFFSET 2",
            1000000000,
            1001,
        ),
    ],
)
def test_only_outer_limit_is_edited(
    policy: QueryPolicy, sql: str, executed: str, requested: int | None, applied: int
) -> None:
    query = guard_readonly_query(sql, policy)
    assert query.executed_sql == executed
    assert query.requested_limit == requested
    assert query.applied_limit == applied


@pytest.mark.parametrize(
    "hint", ["USE_INDEX(o, idx_id)", "HASH_JOIN(o, p)", "READ_FROM_STORAGE(TIFLASH[o])"]
)
def test_plan_hints_survive_verbatim(policy: QueryPolicy, hint: str) -> None:
    sql = f"SELECT /*+ {hint} */ o.id FROM app.orders AS o LIMIT 1000000000 OFFSET 2"  # noqa: S608 -- 固定离线输入，仅测试纯函数守卫
    query = guard_readonly_query(sql, policy)
    expected = f"SELECT /*+ {hint} */ o.id FROM app.orders AS o LIMIT 1001 OFFSET 2"  # noqa: S608 -- 离线输入
    assert query.executed_sql == expected
    assert query.referenced_objects == frozenset({("app", "orders")})
    assert query.referenced_columns == frozenset({("app", "orders", "id")})


@pytest.mark.parametrize(
    "hint",
    [
        "MAX_EXECUTION_TIME(1)",
        "MEMORY_QUOTA(100 MB)",
        "SET_VAR(sql_mode='ANSI_QUOTES')",
        "RESOURCE_GROUP(rg)",
        "UNKNOWN_HINT(o)",
        "USE_INDEX(o, SET_VAR(x=1))",
    ],
)
def test_resource_or_unknown_hints_are_rejected(policy: QueryPolicy, hint: str) -> None:
    with pytest.raises(QueryRejectedError):
        guard_readonly_query(f"SELECT /*+ {hint} */ id FROM app.orders", policy)  # noqa: S608 -- 固定离线输入，仅测试纯函数守卫


@pytest.mark.parametrize(
    "comment",
    [
        "/*!50000 FOR UPDATE */",
        "/*! FOR UPDATE */",
        "/*T! FOR UPDATE */",
        "/*T![feature_id] FOR UPDATE */",
    ],
)
def test_executable_comment_is_rejected_before_sql_is_returned(
    policy: QueryPolicy, comment: str
) -> None:
    with pytest.raises(QueryRejectedError) as found:
        guard_readonly_query(f"SELECT id FROM app.orders {comment}", policy)  # noqa: S608 -- 固定离线输入，仅测试纯函数守卫
    assert found.value.code.value == "executable_comment"
    assert found.value.__context__ is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM app.orders FOR UPDATE",
        "SELECT id FROM app.orders LOCK IN SHARE MODE",
        "SELECT id INTO OUTFILE '/tmp/result' FROM app.orders",
        "SELECT NEXTVAL(seq)",
        "SELECT SETVAL(seq, 1)",
        "SELECT GET_LOCK('x', 1)",
        "SELECT RELEASE_LOCK('x')",
        "SELECT LAST_INSERT_ID(1)",
        "SELECT SLEEP(1)",
        "SELECT 1; SELECT 2",
        "UPDATE app.orders SET status='x'",
        "DELETE FROM app.orders",
        "SELECT id FROM app.orders LIMIT -1",
        "SELECT id FROM app.orders LIMIT 1000 OFFSET -1",
        "SELECT 1--1 FROM app.orders FOR UPDATE",
        "SELECT id FROM app.orders -- tail\r LIMIT 1000000000",
        "SELECT id FROM app.orders # tail\r LIMIT 1000000000",
    ],
)
def test_dangerous_or_unsupported_sql_is_rejected(policy: QueryPolicy, sql: str) -> None:
    with pytest.raises(QueryRejectedError):
        guard_readonly_query(sql, policy)


def test_plain_comments_do_not_turn_strings_into_execution(policy: QueryPolicy) -> None:
    sql = "SELECT 'x\\' FROM app.orders FOR UPDATE -- ' AS note"
    query = guard_readonly_query(sql, policy)
    assert not query.referenced_objects
    assert query.executed_sql == sql + " LIMIT 1001"


def test_explain_preserves_limit_offset_and_hint(policy: QueryPolicy) -> None:
    sql = (
        "SELECT /*+ USE_INDEX(o, idx_id) */ o.id FROM app.orders AS o "
        "LIMIT 1000000000 OFFSET 2; -- tail"
    )
    query = guard_explain_query(sql, policy)
    assert query.executed_sql == sql.replace(";", "")


def test_cte_name_cannot_hide_a_physical_dependency(policy: QueryPolicy) -> None:
    query = guard_readonly_query(
        "WITH orders AS (SELECT id FROM other.orders) SELECT id FROM orders", policy
    )
    assert query.referenced_objects == frozenset({("other", "orders")})


def test_default_database_never_falls_back_to_another_schema(policy: QueryPolicy) -> None:
    missing = policy.model_copy(update={"default_database": "missing"})
    with pytest.raises(QueryRejectedError):
        guard_readonly_query("SELECT id FROM orders", missing)


def test_starrocks_keeps_existing_unqualified_table_rejection(policy: QueryPolicy) -> None:
    starrocks = policy.model_copy(update={"database_type": "starrocks", "default_database": None})
    with pytest.raises(QueryRejectedError) as found:
        guard_readonly_query("SELECT id FROM orders", starrocks)
    assert found.value.code.value == "unqualified_table"


def test_grouping_and_window_columns_keep_physical_dependencies(policy: QueryPolicy) -> None:
    aggregate = guard_readonly_query(
        "SELECT status, SUM(amount) AS total FROM orders GROUP BY status ORDER BY total DESC",
        policy,
    )
    assert aggregate.referenced_columns == frozenset(
        {("app", "orders", "status"), ("app", "orders", "amount")}
    )
    window = guard_readonly_query(
        "SELECT ROW_NUMBER() OVER (PARTITION BY status ORDER BY id) AS n FROM orders", policy
    )
    assert window.referenced_columns == frozenset(
        {("app", "orders", "status"), ("app", "orders", "id")}
    )


def test_mysql_double_quotes_are_strings(policy: QueryPolicy) -> None:
    query = guard_readonly_query('SELECT "status" AS value', policy)
    assert query.executed_sql == 'SELECT "status" AS value LIMIT 1001'
    assert not query.referenced_objects


@pytest.mark.parametrize("space", ["\u00a0", "\x1c"])
def test_non_tidb_dash_comment_whitespace_is_rejected(policy: QueryPolicy, space: str) -> None:
    with pytest.raises(QueryRejectedError):
        guard_readonly_query(
            "SELECT 1--" + space + "x FROM app.orders FOR UPDATE", policy  # noqa: S608
        )
