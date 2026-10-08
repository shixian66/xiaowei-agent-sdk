"""SQLGuard：单条只读 SELECT 的闭集校验与规范化（P1-B Task 1）及执行计划产物（P2 Task 2）。

正例先证明闸门不是恒拒；反例逐类断言稳定原因码、固定说明，且说明、异常链与 sqlglot 日志都
不含输入 SQL 的任何片段。SQLGuard 是纯函数：本文件在默认禁网下运行，不需要任何替身。
"""

# ruff: noqa: S608 —— 本文件的 SQL 是被检样本，拼接是有意的。

import copy
import dataclasses
import logging

import pytest
import sqlglot
from pydantic import ValidationError
from sqlglot import exp

from xiaowei.sqlguard import (
    REJECTION_MESSAGES,
    ExplainQuery,
    GuardedQuery,
    QueryPolicy,
    QueryRejectedError,
    QueryRejectionCode,
    audit_references,
    guard_explain_query,
    guard_readonly_query,
)

Code = QueryRejectionCode

POLICY = QueryPolicy(
    target_id="sr-test",
    tables={
        "shop": {
            "sales": ("region", "total", "orders", "dt"),
            "regions": ("name", "region"),
        }
    },
    allowed_functions=frozenset({"sum", "COUNT", "Max", "CAST", "IF", "COALESCE"}),
    max_rows=100,
    max_sql_bytes=4000,
    max_result_columns=20,
)
CAP = POLICY.max_rows + 1


def guard(sql: str, policy: QueryPolicy = POLICY) -> GuardedQuery:
    return guard_readonly_query(sql, policy)


def rejection(sql: str, policy: QueryPolicy = POLICY) -> QueryRejectedError:
    with pytest.raises(QueryRejectedError) as info:
        guard(sql, policy)
    return info.value


def narrowed(**changes: object) -> QueryPolicy:
    return POLICY.model_copy(update=changes)


DATE_POLICY = narrowed(
    allowed_functions=POLICY.allowed_functions
    | {"ABS", "DATE_ADD", "DATE_SUB", "DATEDIFF", "TIMESTAMP_TRUNC", "CURRENT_DATE"}
)


@pytest.mark.parametrize(
    ("expression", "normalized"),
    [
        ("DATE_SUB(dt, INTERVAL 7 DAY)", "DATE_SUB(`sales`.`dt`, INTERVAL 7 DAY)"),
        ("DATE_ADD(dt, INTERVAL 1 MONTH)", "DATE_ADD(`sales`.`dt`, INTERVAL 1 MONTH)"),
        ("DATE_ADD(dt, INTERVAL 1 WEEK)", "DATE_ADD(`sales`.`dt`, INTERVAL 7 DAY)"),
        ("DATE_SUB(dt, INTERVAL -2 WEEK)", "DATE_SUB(`sales`.`dt`, INTERVAL -14 DAY)"),
        ("ADDDATE(dt, 1)", "DATE_ADD(`sales`.`dt`, INTERVAL 1 DAY)"),
        ("SUBDATE(dt, 1)", "DATE_SUB(`sales`.`dt`, INTERVAL 1 DAY)"),
        ("DATEDIFF(dt, '2026-01-01')", "DATEDIFF(`sales`.`dt`, '2026-01-01')"),
        ("DATE_TRUNC('day', dt)", "DATE_TRUNC('DAY', `sales`.`dt`)"),
    ],
)
def test_configured_date_functions_preserve_starrocks_spelling(
    expression: str, normalized: str
) -> None:
    query = guard(f"SELECT {expression} AS d FROM sales", DATE_POLICY)
    assert normalized in query.normalized_sql
    assert guard(query.normalized_sql, DATE_POLICY).normalized_sql == query.normalized_sql
    assert query.referenced_columns == {("shop", "sales", "dt")}


@pytest.mark.parametrize(
    "expression",
    [
        "DATE_SUB(dt, INTERVAL orders DAY)",
        "DATE_ADD(dt, INTERVAL (SELECT 1) DAY)",
        "DATE_ADD(dt, INTERVAL ABS(-1) DAY)",
        "DATE_ADD(dt, INTERVAL 1 FORTNIGHT)",
        "DATE_ADD(dt, INTERVAL 2147483648 DAY)",
        "DATE_ADD(dt, INTERVAL 306783379 WEEK)",
        "DATE_TRUNC('fortnight', dt)",
    ],
)
def test_date_interval_rejects_dynamic_or_unknown_units(expression: str) -> None:
    assert rejection(f"SELECT {expression} AS d FROM sales", DATE_POLICY).code in {
        Code.UNSUPPORTED_SYNTAX,
        Code.FUNCTION_NOT_ALLOWED,
    }


def test_date_function_still_requires_explicit_allowlist() -> None:
    assert (
        rejection("SELECT DATE_SUB(dt, INTERVAL 7 DAY) AS d FROM sales").code
        == Code.FUNCTION_NOT_ALLOWED
    )


def test_date_diff_spelling_is_not_implicitly_allowed_as_datediff() -> None:
    for spelling in ("DATE_DIFF", "`DATE_DIFF`"):
        assert (
            rejection(f"SELECT {spelling}('DAY', dt, dt) AS d FROM sales", DATE_POLICY).code
            == Code.FUNCTION_NOT_ALLOWED
        )
    assert (
        "DATE_DIFF(x)"
        in guard("SELECT 'DATE_DIFF(x)' AS literal FROM sales", DATE_POLICY).normalized_sql
    )


@pytest.mark.parametrize(
    "expression",
    [
        "DATEDIFF('day', dt, '2026-01-01')",
        "DATEDIFF(dt, '2026-01-01', dt)",
        "DATEDIFF('day', dt, '2026-01-01', 4)",
        "DATE_ADD(dt, INTERVAL 1 DAY, 5)",
        "`DATE_ADD`(dt, INTERVAL 1 DAY, 5)",
        "DATE_SUB(dt, INTERVAL 1 DAY, dt)",
        "ADDDATE(dt, 1, 2)",
        "SUBDATE(dt, 1, 2)",
        "DATE_TRUNC('day', dt, 'x')",
        "DATE_ADD(dt)",
        "DATE_SUB(dt)",
    ],
)
def test_date_functions_reject_wrong_top_level_arity(expression: str) -> None:
    error = rejection(f"SELECT {expression} AS d FROM sales", DATE_POLICY)
    assert error.code == Code.UNSUPPORTED_SYNTAX
    assert error.__cause__ is None and error.__context__ is None


@pytest.mark.parametrize("expression", ["DATE_ADD(dt, )", "DATE_SUB(dt, )"])
def test_date_parser_unexpected_errors_become_unparsable(expression: str) -> None:
    error = rejection(f"SELECT {expression} AS d FROM sales", DATE_POLICY)
    assert error.code == Code.UNPARSABLE
    assert error.__cause__ is None and error.__context__ is None


def test_date_arity_ignores_nested_function_and_subquery_commas() -> None:
    for expression in (
        "DATE_ADD(COALESCE(dt, dt), INTERVAL 1 DAY)",
        "DATE_ADD(DATE_SUB(dt, INTERVAL 1 DAY), INTERVAL 2 DAY)",
        "DATEDIFF(COALESCE(dt, dt), dt)",
    ):
        assert "AS `d`" in guard(f"SELECT {expression} AS d FROM sales", DATE_POLICY).normalized_sql

    policy = DATE_POLICY.model_copy(
        update={"allowed_functions": DATE_POLICY.allowed_functions | {"MIN"}}
    )
    subquery = (
        "SELECT DATE_ADD((SELECT COALESCE(MAX(dt), MIN(dt)) FROM sales), "
        "INTERVAL 1 DAY) AS d FROM sales"
    )
    assert "DATE_ADD((SELECT" in guard(subquery, policy).normalized_sql
    assert (
        "DATE_ADD(1, 2, 3)"
        in guard("SELECT 'DATE_ADD(1, 2, 3)' AS text FROM sales", DATE_POLICY).normalized_sql
    )


def test_date_arity_applies_to_explain_and_audit_paths() -> None:
    invalid = "SELECT DATE_ADD(dt, INTERVAL 1 DAY, 5) AS d FROM sales"
    with pytest.raises(QueryRejectedError) as explain:
        guard_explain_query(invalid, DATE_POLICY)
    with pytest.raises(QueryRejectedError) as audit:
        audit_references(invalid, DATE_POLICY, "shop")
    assert explain.value.code == audit.value.code == Code.UNSUPPORTED_SYNTAX

    valid = "SELECT DATE_ADD(dt, INTERVAL 1 DAY) AS d FROM sales"
    assert "DATE_ADD" in guard_explain_query(valid, DATE_POLICY).normalized_sql
    objects, columns = audit_references(valid, DATE_POLICY, "shop")
    assert objects == {("shop", "sales")}
    assert columns == {("shop", "sales", "dt")}


# ---- 正例 ----------------------------------------------------------------------------------


def test_single_table_query_is_fully_qualified_and_keeps_a_smaller_limit() -> None:
    query = guard(
        "SELECT region, SUM(total) AS s FROM sales WHERE dt >= '2026-01-01' "
        "GROUP BY region HAVING SUM(total) > 1 ORDER BY s DESC LIMIT 5"
    )

    assert query.normalized_sql == (
        "SELECT `sales`.`region` AS `region`, SUM(`sales`.`total`) AS `s` "
        "FROM `shop`.`sales` AS `sales` WHERE `sales`.`dt` >= '2026-01-01' "
        "GROUP BY `sales`.`region` HAVING SUM(`sales`.`total`) > 1 "
        "ORDER BY `s` DESC LIMIT 5"
    )
    assert query.target_id == "sr-test"
    assert query.referenced_objects == frozenset({("shop", "sales")})
    assert query.referenced_columns == frozenset(
        {("shop", "sales", "region"), ("shop", "sales", "total"), ("shop", "sales", "dt")}
    )
    assert query.max_returned_rows == 5


@pytest.mark.parametrize(
    ("sql", "objects", "columns"),
    [
        (
            "SELECT s.region, r.name FROM sales s JOIN regions r ON s.region = r.region",
            {"sales", "regions"},
            {("sales", "region"), ("regions", "name"), ("regions", "region")},
        ),
        (
            "SELECT s.region FROM sales s LEFT JOIN regions r ON s.region = r.region "
            "WHERE r.name IS NULL",
            {"sales", "regions"},
            {("sales", "region"), ("regions", "name"), ("regions", "region")},
        ),
        (
            "WITH t AS (SELECT region, total FROM sales) SELECT region FROM t",
            {"sales"},
            {("sales", "region"), ("sales", "total")},
        ),
        (
            "SELECT region FROM sales WHERE region IN (SELECT region FROM regions)",
            {"sales", "regions"},
            {("sales", "region"), ("regions", "region")},
        ),
        (
            "SELECT q.s FROM (SELECT region AS s FROM sales) q",
            {"sales"},
            {("sales", "region")},
        ),
        (
            "SELECT region FROM sales WHERE total > (SELECT MAX(total) FROM sales)",
            {"sales"},
            {("sales", "region"), ("sales", "total")},
        ),
        # CTE 名遮蔽物理表名：只引用 CTE 内部的物理表。
        (
            "WITH regions AS (SELECT region AS name FROM sales) SELECT name FROM regions",
            {"sales"},
            {("sales", "region")},
        ),
        # CTE 名与未获准物理表同名也不误判。
        (
            "WITH customers AS (SELECT region FROM sales) SELECT region FROM customers",
            {"sales"},
            {("sales", "region")},
        ),
        # ORDER BY 输出别名不是物理列，即使与未获准列同名。
        ("SELECT region AS secret FROM sales ORDER BY secret", {"sales"}, {("sales", "region")}),
        ("SELECT COUNT(*) AS n FROM sales", {"sales"}, set()),
        ("SELECT COUNT(DISTINCT region) AS n FROM sales", {"sales"}, {("sales", "region")}),
        (
            "SELECT CAST(total AS DECIMAL(10, 2)) AS t, IF(total > 1, 'big', 'small') AS k, "
            "CASE WHEN orders > 1 THEN 'many' ELSE 'one' END AS c, COALESCE(region, '-') AS r "
            "FROM sales",
            {"sales"},
            {("sales", "total"), ("sales", "orders"), ("sales", "region")},
        ),
        ("SELECT `region` FROM `shop`.`sales`", {"sales"}, {("sales", "region")}),
        ("SELECT shop.sales.region FROM shop.sales", {"sales"}, {("sales", "region")}),
        ("SELECT DISTINCT region FROM sales ORDER BY 1", {"sales"}, {("sales", "region")}),
        ("SELECT 'a;b' AS v, 1 AS one", set(), set()),
    ],
)
def test_allowed_queries(sql: str, objects: set[str], columns: set[tuple[str, str]]) -> None:
    query = guard(sql)

    assert query.referenced_objects == {("shop", o) for o in objects}
    assert query.referenced_columns == {("shop", t, c) for t, c in columns}
    assert guard(query.normalized_sql).normalized_sql == query.normalized_sql  # 往返幂等
    assert_every_column_is_qualified(query)


def assert_every_column_is_qualified(query: GuardedQuery) -> None:
    """执行的 SQL 只有 ORDER BY 对本层输出别名的引用不限定（交给数据库按原文绑定）。"""
    tree = sqlglot.parse_one(query.normalized_sql, read="starrocks")
    for column in tree.find_all(exp.Column):
        if column.table:
            continue
        order = column.find_ancestor(exp.Order, exp.Select)
        assert isinstance(order, exp.Order) and isinstance(order.parent, exp.Select)
        assert column.name in {
            p.alias for p in order.parent.expressions if isinstance(p, exp.Alias)
        }


@pytest.mark.parametrize(
    ("limit", "expected_sql_limit", "expected_rows"),
    [
        ("", CAP, POLICY.max_rows),
        ("LIMIT 1000", CAP, POLICY.max_rows),
        ("LIMIT 99999999999999999999", CAP, POLICY.max_rows),
        (f"LIMIT {POLICY.max_rows + 1}", CAP, POLICY.max_rows),
        (f"LIMIT {POLICY.max_rows}", POLICY.max_rows, POLICY.max_rows),
        ("LIMIT 7", 7, 7),
        ("LIMIT 0", 0, 0),
    ],
)
def test_limit_is_capped_for_truncation_detection(
    limit: str, expected_sql_limit: int, expected_rows: int
) -> None:
    query = guard(f"SELECT region FROM sales {limit}")

    assert query.normalized_sql.endswith(f" LIMIT {expected_sql_limit}")
    assert query.max_returned_rows == expected_rows
    assert guard(query.normalized_sql) == query


def test_with_query_limit_lands_on_the_final_body() -> None:
    query = guard("WITH t AS (SELECT region FROM sales LIMIT 3) SELECT region FROM t")

    assert query.normalized_sql == (
        "WITH `t` AS (SELECT `sales`.`region` AS `region` FROM `shop`.`sales` AS `sales` LIMIT 3) "
        f"SELECT `t`.`region` AS `region` FROM `t` AS `t` LIMIT {CAP}"
    )
    assert guard(query.normalized_sql) == query


@pytest.mark.parametrize("check", [guard_readonly_query, guard_explain_query])
@pytest.mark.parametrize(
    ("sql", "orders"),
    [
        ("SELECT region AS x, total AS x FROM sales ORDER BY 1", ["ORDER BY 1"]),
        (
            "SELECT s.region, r.region FROM sales s JOIN regions r ON s.region = r.region "
            "ORDER BY 1, 2 DESC",
            ["ORDER BY 1, 2 DESC"],
        ),
        (
            "SELECT region AS x, total AS x, COUNT(*) AS n FROM sales "
            "GROUP BY 1, 2 ORDER BY 3 DESC, 1",
            ["ORDER BY 3 DESC, 1"],
        ),
        # 提前复制未限定的投影会被交叉别名重新解释；常量也不能变成另一个序号。
        ("SELECT total AS orders, orders AS total FROM sales ORDER BY 1", ["ORDER BY 1"]),
        ("SELECT 2 AS x, total AS y FROM sales ORDER BY 1", ["ORDER BY 1"]),
        (
            "WITH q AS (SELECT total AS x, orders AS x, region FROM sales ORDER BY 1 LIMIT 1) "
            "SELECT q.region AS label FROM q ORDER BY 1 DESC",
            ["ORDER BY 1 DESC", "ORDER BY 1"],
        ),
        (
            "SELECT q.region FROM (SELECT total AS x, orders AS x, region FROM sales "
            "ORDER BY 2 DESC LIMIT 1) q",
            ["ORDER BY 2 DESC"],
        ),
        ("SELECT DISTINCT region FROM sales ORDER BY 1", ["ORDER BY 1"]),
    ],
)
def test_order_ordinals_keep_their_projection_position(check, sql, orders) -> None:  # type: ignore[no-untyped-def]
    top = sqlglot.parse_one(sql, read="starrocks")
    names = [p.alias_or_name for p in top.expressions]
    if check is guard_readonly_query and len(set(names)) != len(names):
        # 结果列重名无法交付（P2.5 Task 3）：查询路径在 I/O 前要求唯一别名；序号由计划路径证明。
        assert rejection(sql).code is Code.AMBIGUOUS_REFERENCE
        return
    query = check(sql, POLICY)
    tree = sqlglot.parse_one(query.normalized_sql, read="starrocks")
    assert [order.sql(dialect="starrocks") for order in tree.find_all(exp.Order)] == orders
    assert check(query.normalized_sql, POLICY) == query
    assert_every_column_is_qualified(query)
    if "GROUP BY" in sql:
        assert tree.args["group"].sql(dialect="starrocks") == (
            "GROUP BY `sales`.`region`, `sales`.`total`"
        )


@pytest.mark.parametrize("check", [guard_readonly_query, guard_explain_query])
@pytest.mark.parametrize("order", ["x", "x + 1"])
def test_ambiguous_output_alias_is_not_silently_bound_to_the_last_column(check, order) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(QueryRejectedError) as refused:
        check(f"SELECT region AS x, total AS x FROM sales ORDER BY {order}", POLICY)
    assert refused.value.code is Code.AMBIGUOUS_REFERENCE


@pytest.mark.parametrize("check", [guard_readonly_query, guard_explain_query])
@pytest.mark.parametrize("ordinal", [0, 3])
def test_invalid_order_ordinal_is_rejected(check, ordinal) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(QueryRejectedError):
        check(f"SELECT region, total FROM sales ORDER BY {ordinal}", POLICY)


def test_function_names_are_matched_case_insensitively() -> None:
    assert guard("SELECT Sum(total) AS s, max(total) AS m FROM sales").referenced_objects == {
        ("shop", "sales")
    }


# ---- 列：每个子句与子查询 --------------------------------------------------------------------


COLUMN_REJECTED: list[str] = [
    "SELECT secret FROM sales",
    "SELECT sales.secret FROM sales",
    "SELECT region FROM sales WHERE secret = 1",
    "SELECT s.region FROM sales s JOIN regions r ON s.secret = r.region",
    "SELECT region FROM sales GROUP BY secret",
    "SELECT region FROM sales GROUP BY region HAVING MAX(secret) > 1",
    "SELECT region FROM sales ORDER BY secret",
    "SELECT region FROM sales WHERE region IN (SELECT secret FROM regions)",
    "SELECT q.region FROM (SELECT region, secret FROM sales) q",
    "WITH t AS (SELECT secret FROM sales) SELECT secret FROM t",
    "WITH t AS (SELECT region FROM sales) SELECT secret FROM t",
    "SELECT COUNT(DISTINCT secret) AS n FROM sales",
    "SELECT x.region FROM sales",
]


@pytest.mark.parametrize("sql", COLUMN_REJECTED)
def test_columns_outside_the_allowlist_are_rejected_in_every_clause(sql: str) -> None:
    assert rejection(sql).code is Code.COLUMN_NOT_ALLOWED


IDENTIFIER_REJECTED: list[tuple[str, Code]] = [
    # 列名按快照不区分大小写（P2.5 Task 3，见 tests/p25/test_sql_scope.py）；库、表与别名区分
    # 大小写。全角、同形字与不可见字符不是大小写差异，只会失败关闭。
    ("SELECT region FROM Sales", Code.OBJECT_NOT_ALLOWED),
    ("SELECT `ｒegion` FROM sales", Code.COLUMN_NOT_ALLOWED),  # 全角
    ("SELECT `rеgion` FROM sales", Code.COLUMN_NOT_ALLOWED),  # 西里尔 е
    ("SELECT `re​gion` FROM sales", Code.COLUMN_NOT_ALLOWED),  # 零宽空格
    ("SELECT region FROM `sаles`", Code.OBJECT_NOT_ALLOWED),  # 西里尔 а
]


@pytest.mark.parametrize(("sql", "code"), IDENTIFIER_REJECTED)
def test_identifier_variants_cannot_bypass_the_allowlist(sql: str, code: Code) -> None:
    assert rejection(sql).code is code


@pytest.mark.parametrize(
    "sql",
    [
        # 数据库在 WHERE/GROUP BY/HAVING 中可能把未限定名解析为同名物理列，而不是输出别名。
        "SELECT region AS secret FROM sales WHERE secret = 'x'",
        "SELECT region AS secret FROM sales GROUP BY secret",
        "SELECT SUM(total) AS s FROM sales GROUP BY region HAVING s > 1",
    ],
)
def test_output_alias_outside_order_by_is_never_left_for_the_database(sql: str) -> None:
    try:
        query = guard(sql)
    except QueryRejectedError as error:
        assert error.code in {Code.AMBIGUOUS_REFERENCE, Code.COLUMN_NOT_ALLOWED}
    else:  # 别名已展开为获准表达式
        assert_every_column_is_qualified(query)


def test_order_by_alias_is_kept_as_an_output_reference() -> None:
    """ORDER BY 的输出别名原样保留，由数据库按原文规则绑定；不在范围内的同名列不进入依赖。"""
    query = guard(
        "SELECT region AS secret, SUM(total) AS s FROM sales GROUP BY region ORDER BY s, secret"
    )
    assert query.normalized_sql.split(" ORDER BY ", 1)[1] == f"`s`, `secret` LIMIT {CAP}"
    assert query.referenced_columns == {("shop", "sales", "region"), ("shop", "sales", "total")}


def test_unqualified_column_present_in_two_sources_is_ambiguous() -> None:
    sql = "SELECT region FROM sales JOIN regions ON sales.region = regions.region"
    assert rejection(sql).code is Code.AMBIGUOUS_REFERENCE


# ---- 函数：按节点类型的闭集 ------------------------------------------------------------------


FUNCTION_REJECTED: list[tuple[str, QueryPolicy]] = [
    ("SELECT FOO(total) AS x FROM sales", POLICY),  # Anonymous
    ("SELECT MIN(total) AS x FROM sales", POLICY),  # 已知函数但未获准
    ("SELECT SUM(ABS(total)) AS x FROM sales", POLICY),  # 嵌套在获准函数内
    ("SELECT region FROM sales WHERE LOWER(region) = 'x'", POLICY),
    ("SELECT region FROM sales ORDER BY LENGTH(region)", POLICY),
    ("SELECT CAST(total AS INT) AS x FROM sales", narrowed(allowed_functions=frozenset())),
    (
        "SELECT region FROM sales WHERE dt >= DATE '2026-01-01'",
        narrowed(allowed_functions=frozenset({"SUM"})),
    ),
    ("SELECT IF(total > 1, 1, 0) AS x FROM sales", narrowed(allowed_functions=frozenset())),
    ("SELECT COUNT(*) AS n FROM sales", narrowed(allowed_functions=frozenset({"SUM"}))),
    ("SELECT DATE_TRUNC('day', dt) AS d FROM sales", POLICY),
]


@pytest.mark.parametrize(("sql", "policy"), FUNCTION_REJECTED)
def test_functions_outside_the_allowlist_are_rejected(sql: str, policy: QueryPolicy) -> None:
    assert rejection(sql, policy).code is Code.FUNCTION_NOT_ALLOWED


def test_case_expression_does_not_need_the_if_function() -> None:
    policy = narrowed(allowed_functions=frozenset())
    assert guard("SELECT CASE WHEN total > 1 THEN 'a' END AS c FROM sales", policy)


# ---- 其余关键反例 ----------------------------------------------------------------------------

REJECTED: list[tuple[str, Code]] = [
    ("", Code.UNPARSABLE),
    ("   \n\t", Code.UNPARSABLE),
    (";", Code.UNPARSABLE),
    ("SELECT FROM WHERE", Code.UNPARSABLE),
    ("SELECT region FROM", Code.UNPARSABLE),
    ("SELECT 'unterminated", Code.UNPARSABLE),
    ("SELECT region FROM sales INTO OUTFILE '/tmp/x'", Code.UNPARSABLE),
    ("SELECT region FROM sales; DROP TABLE sales", Code.MULTIPLE_STATEMENTS),
    ("SELECT 1; SELECT 2", Code.MULTIPLE_STATEMENTS),
    ("SELECT 1;;", Code.MULTIPLE_STATEMENTS),
    ("DROP TABLE sales", Code.UNSUPPORTED_SYNTAX),
    ("CREATE TABLE x AS SELECT region FROM sales", Code.UNSUPPORTED_SYNTAX),
    ("INSERT INTO sales (region) VALUES ('x')", Code.UNSUPPORTED_SYNTAX),
    ("UPDATE sales SET total = 0", Code.UNSUPPORTED_SYNTAX),
    ("DELETE FROM sales", Code.UNSUPPORTED_SYNTAX),
    ("SET CATALOG other", Code.UNSUPPORTED_SYNTAX),
    ("SET query_timeout = 1", Code.UNSUPPORTED_SYNTAX),
    ("USE other", Code.UNSUPPORTED_SYNTAX),
    ("SHOW TABLES", Code.UNSUPPORTED_SYNTAX),
    ("EXPLAIN SELECT region FROM sales", Code.UNSUPPORTED_SYNTAX),
    ("(SELECT region FROM sales)", Code.UNSUPPORTED_SYNTAX),
    # UNION、相关子查询、窗口与星号展开自 P2.5 Task 3 起支持，见 tests/p25/test_sql_scope.py。
    ("SELECT region FROM sales INTERSECT SELECT region FROM regions", Code.UNSUPPORTED_SYNTAX),
    ("SELECT region FROM sales EXCEPT SELECT region FROM regions", Code.UNSUPPORTED_SYNTAX),
    ("WITH RECURSIVE t AS (SELECT 1 AS n) SELECT n FROM t", Code.UNSUPPORTED_SYNTAX),
    ("SELECT region FROM sales -- note", Code.UNSUPPORTED_SYNTAX),
    ("SELECT region /* note */ FROM sales", Code.UNSUPPORTED_SYNTAX),
    ("SELECT region FROM sales # note", Code.UNSUPPORTED_SYNTAX),
    ("SELECT /*+ SET_VAR(query_timeout = 999) */ region FROM sales", Code.UNSUPPORTED_SYNTAX),
    ("SELECT region FROM sales FOR UPDATE", Code.UNSUPPORTED_SYNTAX),
    ("SELECT region FROM sales LOCK IN SHARE MODE", Code.UNSUPPORTED_SYNTAX),
    ("SELECT @x", Code.UNSUPPORTED_SYNTAX),
    ("SELECT @@query_timeout", Code.UNSUPPORTED_SYNTAX),
    ("SELECT @x := 1", Code.UNSUPPORTED_SYNTAX),
    ("SELECT region FROM sales WHERE total = ?", Code.UNSUPPORTED_SYNTAX),
    ("SELECT region FROM sales WHERE region = 0x41", Code.UNSUPPORTED_SYNTAX),
    # sqlglot 把 COLLATE 建模为函数节点，按函数闭集拒绝。
    ("SELECT region FROM sales WHERE region = 'a' COLLATE utf8_bin", Code.FUNCTION_NOT_ALLOWED),
    ("SELECT region FROM sales TABLESAMPLE (10 PERCENT)", Code.UNSUPPORTED_SYNTAX),
    ("SELECT region FROM sales PARTITION (p1)", Code.UNSUPPORTED_SYNTAX),
    ("SELECT region FROM sales USE INDEX (i)", Code.UNSUPPORTED_SYNTAX),
    ("SELECT region FROM sales FOR SYSTEM_TIME AS OF '2020-01-01'", Code.UNSUPPORTED_SYNTAX),
    ("SELECT sales.region FROM sales NATURAL JOIN regions", Code.UNSUPPORTED_SYNTAX),
    ("SELECT sales.region FROM sales JOIN regions USING (region)", Code.UNSUPPORTED_SYNTAX),
    ("SELECT n FROM LATERAL (SELECT 1 AS n) x", Code.UNSUPPORTED_SYNTAX),
    ("SELECT q.x FROM (SELECT region FROM sales) AS q(x)", Code.UNSUPPORTED_SYNTAX),
    ("SELECT n FROM TABLE(GENERATE_SERIES(1, 3)) t", Code.UNSUPPORTED_SYNTAX),
    ("SELECT region FROM FILES('path' = 's3://bucket/x')", Code.UNSUPPORTED_SYNTAX),
    ("SELECT other.sales.region FROM shop.sales", Code.COLUMN_NOT_ALLOWED),
    ("SELECT region FROM customers", Code.OBJECT_NOT_ALLOWED),
    ("SELECT region FROM other.sales", Code.OBJECT_NOT_ALLOWED),
    ("SELECT region FROM ext_catalog.shop.sales", Code.OBJECT_NOT_ALLOWED),
    ("SELECT s.region FROM sales s JOIN shop.customers c ON 1 = 1", Code.OBJECT_NOT_ALLOWED),
    ("WITH t AS (SELECT region FROM customers) SELECT region FROM t", Code.OBJECT_NOT_ALLOWED),
    (
        "SELECT region FROM sales WHERE region IN (SELECT region FROM other.x)",
        Code.OBJECT_NOT_ALLOWED,
    ),
    ("SELECT COUNT(sales.*) AS n FROM sales", Code.STAR_PROJECTION),
    ("SELECT region FROM sales LIMIT '5'", Code.UNSUPPORTED_LIMIT),
    ("SELECT region FROM sales LIMIT -1", Code.UNSUPPORTED_LIMIT),
    ("SELECT region FROM sales LIMIT 1 + 1", Code.UNSUPPORTED_LIMIT),
    ("SELECT region FROM sales LIMIT 1.5", Code.UNSUPPORTED_LIMIT),
    ("SELECT region FROM sales LIMIT 1 OFFSET 2", Code.UNSUPPORTED_LIMIT),
    ("SELECT region FROM sales LIMIT 2, 3", Code.UNSUPPORTED_LIMIT),
    ("SELECT q.region FROM (SELECT region FROM sales LIMIT 1 OFFSET 1) q", Code.UNSUPPORTED_LIMIT),
]


@pytest.mark.parametrize(("sql", "code"), REJECTED)
def test_rejections_use_stable_codes(sql: str, code: Code) -> None:
    assert rejection(sql).code is code


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT region FROM sales; -- note",
        "SELECT region FROM sales; /* note */",
        "SELECT region FROM sales; # note",
        "SELECT region FROM sales; /*+ SET_VAR(query_timeout = 999) */",
        "SELECT region FROM sales /*+ SET_VAR(query_timeout = 999) */;",
    ],
)
def test_comments_and_hints_after_the_final_semicolon_are_rejected(sql: str) -> None:
    """注释附着在尾部分号词元上：必须在去掉可选尾部分号之前检查。"""
    assert rejection(sql).code is Code.UNSUPPORTED_SYNTAX


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT region FROM sales;",
        "SELECT region FROM sales ;  \n",
        "WITH t AS (SELECT region FROM sales) SELECT region FROM t;",
    ],
)
def test_a_single_trailing_semicolon_is_allowed(sql: str) -> None:
    assert guard(sql).normalized_sql == guard(sql.rstrip().rstrip(";")).normalized_sql


LOWER_LEVEL_REJECTED: list[tuple[str, Code]] = [
    # 非 SELECT 语句藏在 WITH 之后：sqlglot 回退为 Command 时会把原文写进日志。
    # sqlglot 回退后仍报解析错误（SHOW），或得到非 SELECT 根节点（DELETE）。
    ("WITH c AS (SELECT 1 AS n) SHOW TABLES", Code.UNPARSABLE),
    ("WITH c AS (SELECT 1 AS n) DELETE FROM sales", Code.UNSUPPORTED_SYNTAX),
    # 重复的来源别名：sqlglot scope 在读取来源时抛 OptimizeError。
    (
        "SELECT c.region FROM sales c JOIN regions c ON c.region = c.region",
        Code.AMBIGUOUS_REFERENCE,
    ),
    (
        "WITH t AS (SELECT region FROM sales) SELECT t.region FROM t JOIN sales t ON 1 = 1",
        Code.AMBIGUOUS_REFERENCE,
    ),
    # JSON 可表达、UTF-8 不可编码的孤立代理项。
    ("\ud800", Code.UNPARSABLE),
    ("SELECT '\udfff' AS v", Code.UNPARSABLE),
]


@pytest.mark.parametrize(("sql", "code"), LOWER_LEVEL_REJECTED)
def test_lower_level_input_errors_become_stable_rejections(sql: str, code: Code) -> None:
    assert rejection(sql).code is code


def test_oversized_input_is_rejected_before_parsing() -> None:
    sql = "SELECT region FROM sales WHERE region = '" + "x" * 4000 + "'"
    assert rejection(sql).code is Code.INPUT_TOO_LARGE


def test_size_limit_counts_utf8_bytes() -> None:
    policy = narrowed(max_sql_bytes=60)
    sql = "SELECT region FROM sales WHERE region = '" + "区" * 7 + "'"  # 41 + 21 + 1 字节
    assert len(sql) < policy.max_sql_bytes < len(sql.encode())
    assert rejection(sql, policy).code is Code.INPUT_TOO_LARGE


def test_normalized_sql_must_also_fit() -> None:
    sql = "SELECT region FROM sales"
    policy = narrowed(max_sql_bytes=len(sql))
    assert rejection(sql, policy).code is Code.INPUT_TOO_LARGE


def test_deep_nesting_fails_closed() -> None:
    depth = 900
    sql = "SELECT " + "(" * depth + "1" + ")" * depth + " AS v"
    assert rejection(sql, narrowed(max_sql_bytes=10_000)).code in {
        Code.UNPARSABLE,
        Code.UNSUPPORTED_SYNTAX,
    }


CANARY = "canary7f3a"


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT {CANARY} FROM sales",
        f"SELECT region FROM {CANARY}",
        f"SELECT {CANARY}(total) AS x FROM sales",
        f"SELECT region FROM sales WHERE region = '{CANARY}' OR '",
        f"SELECT region FROM sales; DROP TABLE {CANARY}",
        f"SET CATALOG {CANARY}",
        f"SHOW {CANARY}",
        f"SELECT region FROM sales -- {CANARY}",
        f"SELECT region FROM sales LIMIT '{CANARY}'",
        f"SELECT region FROM sales WHERE region = 'x' COLLATE {CANARY}",
        f"SELECT region FROM sales TABLESAMPLE ({CANARY} PERCENT)",
        f"SELECT '{CANARY}",
        f"SELECT region FROM sales; -- {CANARY}",
        f"WITH c AS (SELECT 1 AS n) SHOW {CANARY}",
        f"WITH c AS (SELECT 1 AS n) SET {CANARY} = 1",
        f"SELECT {CANARY}.region FROM sales {CANARY} JOIN regions {CANARY} "
        f"ON {CANARY}.region = {CANARY}.region",
        f"SELECT '{CANARY}\ud800' AS v",
    ],
)
def test_rejections_never_echo_the_input(sql: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    error = rejection(sql)

    assert str(error) == REJECTION_MESSAGES[error.code]
    assert error.args == (REJECTION_MESSAGES[error.code],)
    assert CANARY not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None  # 不只是隐藏：下层异常（带输入片段）不得挂在异常链上
    assert not [r for r in caplog.records if CANARY in r.getMessage()]


@pytest.mark.parametrize(("sql", "code"), REJECTED)
def test_no_rejection_keeps_a_lower_level_exception(sql: str, code: Code) -> None:
    error = rejection(sql)
    assert (error.__cause__, error.__context__) == (None, None)


@pytest.mark.parametrize("sql", ["DROP TABLE sales", "SET CATALOG other", "SHOW TABLES"])
def test_non_select_statements_never_reach_the_parser(
    sql: str, caplog: pytest.LogCaptureFixture
) -> None:
    """解析前按首个词元拒绝：不触发 sqlglot 的 Command 回退与告警日志。"""
    caplog.set_level(logging.DEBUG)
    assert rejection(sql).code is Code.UNSUPPORTED_SYNTAX
    assert not [r for r in caplog.records if r.name.startswith("sqlglot")]


def test_every_code_has_a_fixed_message_without_placeholders() -> None:
    assert set(REJECTION_MESSAGES) == set(Code)
    assert all("{" not in message for message in REJECTION_MESSAGES.values())


# ---- 执行凭据与策略 --------------------------------------------------------------------------


def test_guarded_query_cannot_be_built_outside_the_guard() -> None:
    with pytest.raises(TypeError):
        GuardedQuery(
            target_id="sr-test",
            normalized_sql="DROP TABLE sales",
            referenced_objects=frozenset(),
            referenced_columns=frozenset(),
            max_returned_rows=1,
        )


def test_guarded_query_is_immutable() -> None:
    query = guard("SELECT region FROM sales")
    with pytest.raises(AttributeError):
        query.normalized_sql = "DROP TABLE sales"  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"tables": {"shop": {"sales": ()}}},  # 对象没有列
        {"tables": {"shop": {"sales": ("region", "Region")}}},  # 列名不区分大小写重复
        {"tables": {"": {"sales": ("region",)}}},
        {"tables": {"shop": {"": ("region",)}}},
        {"tables": {"shop": {"sales": ("",)}}},
        {"max_rows": 0},
        {"max_sql_bytes": 0},
        {"max_result_columns": 0},
        {"extra": 1},
    ],
)
def test_policy_rejects_incomplete_allowlists(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        QueryPolicy.model_validate({**POLICY.model_dump(), **changes})


def test_policy_normalizes_function_names() -> None:
    assert POLICY.allowed_functions == frozenset({"SUM", "COUNT", "MAX", "CAST", "IF", "COALESCE"})


# ---- 执行计划产物（P2 Task 2） -----------------------------------------------------------------


def explain(sql: str, policy: QueryPolicy = POLICY) -> ExplainQuery:
    return guard_explain_query(sql, policy)


@pytest.mark.parametrize(
    ("limit", "query_limit"),
    [("", CAP), ("LIMIT 5000", CAP), ("LIMIT 7", 7), ("LIMIT 0", 0)],
)
def test_explain_keeps_the_user_limit_and_does_not_add_one(limit: str, query_limit: int) -> None:
    """EXPLAIN 不返回数据行；改写 LIMIT 会改变被解释的计划。查询路径作为对照。"""
    sql = f"SELECT region FROM sales {limit}".strip()
    plan = explain(sql)

    if limit:
        assert plan.normalized_sql.endswith(f" {limit}")
    else:
        assert " LIMIT " not in plan.normalized_sql
    assert guard(sql).normalized_sql.endswith(f" LIMIT {query_limit}")
    assert plan.normalized_sql.removesuffix(f" {limit}") == guard(sql).normalized_sql.removesuffix(
        f" LIMIT {query_limit}"
    )


def test_explain_keeps_a_nested_limit_and_the_final_body_without_one() -> None:
    plan = explain("WITH t AS (SELECT region FROM sales LIMIT 3) SELECT region FROM t")

    assert plan.normalized_sql == (
        "WITH `t` AS (SELECT `sales`.`region` AS `region` FROM `shop`.`sales` AS `sales` LIMIT 3) "
        "SELECT `t`.`region` AS `region` FROM `t` AS `t`"
    )


def _all_rejections() -> list[tuple[str, QueryPolicy]]:
    samples = [(sql, POLICY) for sql, _ in REJECTED + IDENTIFIER_REJECTED + LOWER_LEVEL_REJECTED]
    samples += [(sql, POLICY) for sql in COLUMN_REJECTED]
    samples += FUNCTION_REJECTED
    samples += [
        ("SELECT region FROM sales JOIN regions ON sales.region = regions.region", POLICY),
        ("SELECT region FROM sales WHERE region = '" + "x" * 4000 + "'", POLICY),
        ("SELECT region FROM sales WHERE region = '" + "区" * 7 + "'", narrowed(max_sql_bytes=60)),
        ("SELECT region FROM sales", narrowed(max_sql_bytes=len("SELECT region FROM sales"))),
    ]
    return samples


@pytest.mark.parametrize(("sql", "policy"), _all_rejections())
def test_explain_rejects_everything_the_query_guard_rejects(sql: str, policy: QueryPolicy) -> None:
    expected = rejection(sql, policy)
    with pytest.raises(QueryRejectedError) as info:
        explain(sql, policy)

    assert info.value.code is expected.code
    assert str(info.value) == REJECTION_MESSAGES[expected.code]
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


VIEW_POLICY = narrowed(
    tables={
        "shop": {
            "sales": POLICY.tables["shop"]["sales"],
            "v_region_totals": ("region", "amount"),
        }
    }
)


def test_explain_accepts_allowed_views_like_tables() -> None:
    plan = explain(
        "SELECT s.region, v.amount FROM sales s JOIN v_region_totals v ON s.region = v.region",
        VIEW_POLICY,
    )

    assert plan.referenced_objects == frozenset({("shop", "sales"), ("shop", "v_region_totals")})
    assert plan.referenced_columns == frozenset(
        {
            ("shop", "sales", "region"),
            ("shop", "v_region_totals", "region"),
            ("shop", "v_region_totals", "amount"),
        }
    )
    assert plan.target_id == POLICY.target_id
    for sql in (
        "SELECT region FROM v_secret_totals",
        "SELECT s.region FROM sales s JOIN v_secret_totals v ON s.region = v.region",
    ):
        with pytest.raises(QueryRejectedError) as info:
            explain(sql, VIEW_POLICY)
        assert info.value.code is Code.OBJECT_NOT_ALLOWED
    with pytest.raises(QueryRejectedError) as info:
        explain("SELECT secret FROM v_region_totals", VIEW_POLICY)
    assert info.value.code is Code.COLUMN_NOT_ALLOWED


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN SELECT region FROM sales",
        "EXPLAIN ANALYZE SELECT region FROM sales",
        "EXPLAIN LOGICAL SELECT region FROM sales",
        "EXPLAIN VERBOSE SELECT region FROM sales",
        "EXPLAIN COSTS SELECT region FROM sales",
        "EXPLAIN SCHEDULER SELECT region FROM sales",
        "DESC sales",
        "DESCRIBE SELECT region FROM sales",
        "TRACE TIMES SELECT region FROM sales",
        "ANALYZE TABLE sales",
    ],
)
def test_explain_prefixed_input_is_unsupported(sql: str) -> None:
    """级别只由代码固定；模型或用户给出的任何前缀都在解析前被拒绝。"""
    with pytest.raises(QueryRejectedError) as info:
        explain(sql)
    assert info.value.code is Code.UNSUPPORTED_SYNTAX


def test_explain_query_is_sealed_and_distinct() -> None:
    with pytest.raises(TypeError):
        ExplainQuery(
            target_id="sr-test",
            normalized_sql="SELECT 1",
            referenced_objects=frozenset(),
            referenced_columns=frozenset(),
        )
    plan = explain("SELECT region FROM sales")
    query = guard("SELECT region FROM sales")

    assert not isinstance(plan, GuardedQuery)
    assert not isinstance(query, ExplainQuery)
    assert not issubclass(ExplainQuery, GuardedQuery)
    assert not issubclass(GuardedQuery, ExplainQuery)
    with pytest.raises(AttributeError):
        plan.normalized_sql = "SELECT 1"  # type: ignore[misc]


@pytest.mark.parametrize("sealed", ["query", "plan"])
def test_sealed_products_cannot_be_copied_with_other_sql(sealed: str) -> None:
    """``dataclasses.replace`` 不能复制封存产物再换掉 SQL（原先会连同封存标记一起复制）。"""
    product = (
        guard("SELECT region FROM sales")
        if sealed == "query"
        else explain("SELECT region FROM sales")
    )
    with pytest.raises((TypeError, ValueError)):
        dataclasses.replace(product, normalized_sql="DELETE FROM sales")
    assert copy.copy(product) == product  # 原样复制不改变内容


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT region, SUM(total) AS s FROM sales GROUP BY region ORDER BY s DESC LIMIT 5",
        "SELECT region FROM sales",
        "SELECT region FROM sales LIMIT 1000",
        "WITH t AS (SELECT region FROM sales LIMIT 3) SELECT region FROM t",
        "SELECT s.region, r.name FROM sales s JOIN regions r ON s.region = r.region",
        "SELECT region AS secret FROM sales ORDER BY secret",
        "SELECT CAST(total AS DECIMAL(10, 2)) AS t FROM sales WHERE dt >= '2026-01-01'",
    ],
)
def test_explain_normalization_is_idempotent_including_previous_guarded_sql(sql: str) -> None:
    plan = explain(sql)
    previous = guard(sql)

    assert explain(plan.normalized_sql) == plan
    assert explain(previous.normalized_sql).normalized_sql == previous.normalized_sql
    assert explain(previous.normalized_sql).referenced_columns == previous.referenced_columns
