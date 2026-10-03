"""P2.5 Task 3：复杂只读 SQL、跨库与星号展开（SQLGuard，纯函数，离线）。

范围是结构快照给出的全部库的可读对象与按序的列（``QueryPolicy.tables``）。能证明：R3 语法的
规范化保持语义（排序序号、UNION 的整体 LIMIT、星号按快照列序展开）、依赖覆盖每个分支与子句、
未限定表名只按唯一匹配补全、展开后的 SQL 大小与结果列数在任何 I/O 前受限。不能证明 StarRocks
对同一 SQL 的实际结果（见 ``tests/p1b/test_starrocks_real.py`` 的对照用例）。
"""

# ruff: noqa: S608 —— 本文件的 SQL 是被检样本，拼接是有意的。

import pytest
import sqlglot
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

TABLES: dict[str, dict[str, tuple[str, ...]]] = {
    "shop": {
        "sales": ("id", "region", "total", "orders", "dt"),
        "regions": ("region", "name"),
    },
    "hr": {
        "staff": ("id", "name", "region"),
        "sales": ("id", "amount"),
    },
    "cn": {"订单": ("金额", "地区")},
}
POLICY = QueryPolicy(
    target_id="sr-test",
    tables=TABLES,
    allowed_functions=frozenset(
        {"SUM", "COUNT", "MAX", "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD"}
        | {"CAST", "TRIM", "COALESCE"}
    ),
    max_rows=100,
    max_sql_bytes=4000,
    max_result_columns=6,
)
CAP = POLICY.max_rows + 1


def narrowed(**changes: object) -> QueryPolicy:
    return POLICY.model_copy(update=changes)


def query(sql: str, policy: QueryPolicy = POLICY) -> GuardedQuery:
    guarded = guard_readonly_query(sql, policy)
    assert guard_readonly_query(guarded.normalized_sql, policy) == guarded  # 往返幂等
    assert_no_unqualified_column(guarded.normalized_sql)
    return guarded


def plan(sql: str, policy: QueryPolicy = POLICY) -> ExplainQuery:
    guarded = guard_explain_query(sql, policy)
    assert guard_explain_query(guarded.normalized_sql, policy) == guarded
    return guarded


def refused(sql: str, policy: QueryPolicy = POLICY, *, check=guard_readonly_query) -> Code:  # type: ignore[no-untyped-def]
    with pytest.raises(QueryRejectedError) as info:
        check(sql, policy)
    assert str(info.value) == REJECTION_MESSAGES[info.value.code]
    assert (info.value.__cause__, info.value.__context__) == (None, None)
    return info.value.code


def assert_no_unqualified_column(sql: str) -> None:
    tree = sqlglot.parse_one(sql, read="starrocks")
    assert all(c.table or isinstance(c.this, exp.Star) for c in tree.find_all(exp.Column))


def tail(sql: str) -> str:
    """最外层（整个语句）之后的 ORDER BY / LIMIT 子句。"""
    tree = sqlglot.parse_one(sql, read="starrocks")
    return " ".join(
        tree.args[key].sql(dialect="starrocks") for key in ("order", "limit") if tree.args.get(key)
    )


def sales(*columns: str) -> set[tuple[str, str, str]]:
    return {("shop", "sales", c) for c in columns}


def staff(*columns: str) -> set[tuple[str, str, str]]:
    return {("hr", "staff", c) for c in columns}


# ---- 跨库与未限定对象 -----------------------------------------------------------------------


def test_cross_database_join_records_qualified_objects_and_columns() -> None:
    guarded = query(
        "SELECT s.region, t.name FROM shop.sales s JOIN hr.staff t ON s.region = t.region "
        "WHERE s.total > 1"
    )
    assert guarded.referenced_objects == {("shop", "sales"), ("hr", "staff")}
    assert guarded.referenced_columns == sales("region", "total") | staff("name", "region")
    assert "FROM `shop`.`sales` AS `s` JOIN `hr`.`staff` AS `t`" in guarded.normalized_sql


def test_same_table_name_in_two_databases_stays_apart() -> None:
    guarded = query("SELECT a.id, b.amount FROM shop.sales a JOIN hr.sales b ON a.id = b.id")
    assert guarded.referenced_objects == {("shop", "sales"), ("hr", "sales")}
    assert guarded.referenced_columns == sales("id") | {
        ("hr", "sales", "id"),
        ("hr", "sales", "amount"),
    }


def test_unqualified_table_is_completed_only_by_a_unique_match() -> None:
    guarded = query("SELECT name FROM staff")
    assert guarded.referenced_objects == {("hr", "staff")}
    assert "FROM `hr`.`staff` AS `staff`" in guarded.normalized_sql
    # 两个库都有 sales：不猜库，交回给 Agent 澄清。
    assert refused("SELECT id FROM sales") is Code.AMBIGUOUS_OBJECT
    assert refused("SELECT id FROM sales", check=guard_explain_query) is Code.AMBIGUOUS_OBJECT


def test_audit_context_resolves_unqualified_tables_in_its_own_database() -> None:
    audit = narrowed(default_database="hr")
    assert guard_explain_query("SELECT id FROM sales", audit).referenced_objects == {
        ("hr", "sales")
    }
    assert refused("SELECT region FROM regions", audit, check=guard_explain_query) is (
        Code.OBJECT_NOT_ALLOWED
    )


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT id FROM nope.sales", Code.OBJECT_NOT_ALLOWED),
        ("SELECT id FROM shop.Sales", Code.OBJECT_NOT_ALLOWED),  # 表名大小写敏感
        ("SELECT id FROM SHOP.sales", Code.OBJECT_NOT_ALLOWED),  # 库名大小写敏感
        ("SELECT id FROM default_catalog.shop.sales", Code.OBJECT_NOT_ALLOWED),
        ("SELECT id FROM payroll", Code.OBJECT_NOT_ALLOWED),
        ("SELECT s.region FROM shop.sales s JOIN hr.payroll p ON 1 = 1", Code.OBJECT_NOT_ALLOWED),
        ("SELECT t.secret FROM hr.staff t", Code.COLUMN_NOT_ALLOWED),
        ("SELECT S.region FROM shop.sales s", Code.COLUMN_NOT_ALLOWED),  # 别名大小写敏感
        ("SELECT nope.sales.id FROM shop.sales", Code.COLUMN_NOT_ALLOWED),
    ],
)
def test_unknown_objects_and_columns_are_rejected(sql: str, code: Code) -> None:
    assert refused(sql) is code
    assert refused(sql, check=guard_explain_query) is code


def test_column_names_resolve_case_insensitively_against_the_snapshot() -> None:
    """4.1.4 的列名不区分大小写（Task 0 §9.4）；库、表与别名仍区分。列头保留原写法。"""
    guarded = query("SELECT REGION, s.Total FROM shop.sales s WHERE ORDERS > 1 ORDER BY Dt")
    assert guarded.normalized_sql == (
        "SELECT `s`.`region` AS `REGION`, `s`.`total` AS `Total` FROM `shop`.`sales` AS `s` "
        f"WHERE `s`.`orders` > 1 ORDER BY `s`.`dt` LIMIT {CAP}"
    )
    assert guarded.referenced_columns == sales("region", "total", "orders", "dt")


def test_cte_shadows_a_physical_table_of_the_same_name() -> None:
    guarded = query("WITH sales AS (SELECT name AS region FROM hr.staff) SELECT region FROM sales")
    assert guarded.referenced_objects == {("hr", "staff")}
    assert guarded.referenced_columns == staff("name")


# ---- UNION ------------------------------------------------------------------------------------


def test_union_all_keeps_ordinals_and_limits_the_whole_set() -> None:
    guarded = query(
        "SELECT region FROM shop.sales UNION ALL SELECT name FROM hr.staff ORDER BY 1 DESC LIMIT 5"
    )
    assert guarded.referenced_objects == {("shop", "sales"), ("hr", "staff")}
    assert guarded.referenced_columns == sales("region") | staff("name")
    assert tail(guarded.normalized_sql) == "ORDER BY 1 DESC LIMIT 5"
    assert guarded.max_returned_rows == 5


def test_union_without_limit_is_capped_as_a_whole_not_per_branch() -> None:
    guarded = query(
        "(SELECT region FROM shop.sales LIMIT 1) UNION (SELECT name FROM hr.staff LIMIT 2)"
    )
    tree = sqlglot.parse_one(guarded.normalized_sql, read="starrocks")
    assert isinstance(tree, exp.Union)
    assert tree.args["limit"].sql(dialect="starrocks") == f"LIMIT {CAP}"
    branches = [s.args["limit"].sql(dialect="starrocks") for s in tree.find_all(exp.Select)]
    assert sorted(branches) == ["LIMIT 1", "LIMIT 2"]


def test_union_order_by_output_name_becomes_its_position() -> None:
    guarded = query(
        "SELECT region AS r, total AS t FROM shop.sales UNION SELECT name, id FROM hr.staff "
        "ORDER BY t DESC, r"
    )
    assert tail(guarded.normalized_sql) == f"ORDER BY 2 DESC, 1 LIMIT {CAP}"


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        # 第二个分支同样受范围约束：不能借 UNION 绕过对象或列检查。
        (
            "SELECT region FROM shop.sales UNION SELECT secret FROM hr.staff",
            Code.COLUMN_NOT_ALLOWED,
        ),
        (
            "SELECT region FROM shop.sales UNION ALL SELECT x FROM hr.payroll",
            Code.OBJECT_NOT_ALLOWED,
        ),
        (
            "SELECT region FROM shop.sales UNION ALL SELECT name FROM hr.staff WHERE secret = 1",
            Code.COLUMN_NOT_ALLOWED,
        ),
        (
            "SELECT region FROM shop.sales UNION SELECT name FROM hr.staff ORDER BY x",
            Code.COLUMN_NOT_ALLOWED,
        ),
        (
            "SELECT region FROM shop.sales UNION SELECT name FROM hr.staff ORDER BY region || 'x'",
            Code.UNSUPPORTED_SYNTAX,
        ),
        (
            "SELECT region FROM shop.sales UNION SELECT name FROM hr.staff ORDER BY 2",
            Code.COLUMN_NOT_ALLOWED,
        ),
        (
            "SELECT region FROM shop.sales INTERSECT SELECT name FROM hr.staff",
            Code.UNSUPPORTED_SYNTAX,
        ),
        ("SELECT region FROM shop.sales EXCEPT SELECT name FROM hr.staff", Code.UNSUPPORTED_SYNTAX),
    ],
)
def test_union_branches_cannot_bypass_the_scope(sql: str, code: Code) -> None:
    assert refused(sql) is code


def test_dependencies_cover_every_union_branch_inside_a_derived_table() -> None:
    guarded = query(
        "SELECT u.k FROM (SELECT region AS k FROM shop.sales UNION ALL "
        "SELECT name FROM hr.staff WHERE id > 1) u"
    )
    assert guarded.referenced_columns == sales("region") | staff("name", "id")


# ---- 窗口、CTE、子查询 ------------------------------------------------------------------------


def test_window_partition_and_order_columns_are_dependencies() -> None:
    guarded = query(
        "SELECT region, ROW_NUMBER() OVER (PARTITION BY dt ORDER BY orders DESC) AS rn, "
        "RANK() OVER (ORDER BY total) AS rk, DENSE_RANK() OVER (ORDER BY total) AS dr, "
        "LAG(total, 1) OVER (ORDER BY dt) AS prev, "
        "SUM(total) OVER (PARTITION BY region) AS s FROM shop.sales"
    )
    assert guarded.referenced_columns == sales("region", "dt", "orders", "total")
    assert "PARTITION BY `sales`.`dt` ORDER BY `sales`.`orders` DESC" in guarded.normalized_sql


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        (
            "SELECT ROW_NUMBER() OVER (PARTITION BY secret) AS n FROM shop.sales",
            Code.COLUMN_NOT_ALLOWED,
        ),
        ("SELECT LEAD(id) OVER (ORDER BY secret) AS n FROM shop.sales", Code.COLUMN_NOT_ALLOWED),
        (
            "SELECT SUM(total) OVER (ORDER BY dt ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS s "
            "FROM shop.sales",
            Code.UNSUPPORTED_SYNTAX,
        ),
        ("SELECT NTILE(2) OVER (ORDER BY dt) AS n FROM shop.sales", Code.FUNCTION_NOT_ALLOWED),
    ],
)
def test_window_references_and_frames_are_checked(sql: str, code: Code) -> None:
    assert refused(sql) is code


def test_window_functions_follow_the_function_allowlist() -> None:
    policy = narrowed(allowed_functions=frozenset({"SUM"}))
    assert refused("SELECT RANK() OVER (ORDER BY dt) AS r FROM shop.sales", policy) is (
        Code.FUNCTION_NOT_ALLOWED
    )


def test_nested_ctes_and_builtin_functions() -> None:
    guarded = query(
        "WITH a AS (SELECT region, total, dt FROM shop.sales), "
        "b AS (SELECT TRIM(region) AS region, SUM(CAST(total AS DECIMAL(10, 2))) AS s, "
        "COUNT(DISTINCT dt) AS days FROM a GROUP BY TRIM(region)) "
        "SELECT DISTINCT b.region, COALESCE(b.s, 0) AS s, "
        "CASE WHEN b.days > 1 THEN 'many' ELSE 'one' END AS k FROM b"
    )
    assert guarded.referenced_columns == sales("region", "total", "dt")


def test_correlated_subqueries_record_outer_and_inner_columns() -> None:
    guarded = query(
        "SELECT s.region, (SELECT MAX(t.id) FROM hr.staff t WHERE t.region = s.region) AS m "
        "FROM shop.sales s WHERE EXISTS (SELECT 1 FROM shop.regions r "
        "WHERE r.region = s.region AND r.name > s.orders)"
    )
    assert guarded.referenced_columns == (
        sales("region", "orders")
        | staff("id", "region")
        | {("shop", "regions", "region"), ("shop", "regions", "name")}
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT s.region FROM shop.sales s WHERE EXISTS "
        "(SELECT 1 FROM hr.staff t WHERE t.region = s.secret)",
        "SELECT s.region FROM shop.sales s WHERE EXISTS "
        "(SELECT 1 FROM hr.staff t WHERE t.secret = s.region)",
    ],
)
def test_correlated_references_to_unknown_columns_are_rejected(sql: str) -> None:
    assert refused(sql) is Code.COLUMN_NOT_ALLOWED


# ---- 星号展开 ---------------------------------------------------------------------------------


def test_star_expands_in_snapshot_column_order() -> None:
    guarded = query("SELECT * FROM shop.sales")
    assert guarded.normalized_sql == (
        "SELECT `sales`.`id` AS `id`, `sales`.`region` AS `region`, `sales`.`total` AS `total`, "
        "`sales`.`orders` AS `orders`, `sales`.`dt` AS `dt` FROM `shop`.`sales` AS `sales` "
        f"LIMIT {CAP}"
    )
    assert guarded.referenced_columns == sales("id", "region", "total", "orders", "dt")


def test_alias_star_and_stars_inside_ctes_and_derived_tables() -> None:
    guarded = query(
        "WITH r AS (SELECT * FROM shop.regions) "
        "SELECT t.*, q.* FROM hr.staff t JOIN (SELECT r.name AS label FROM r) q ON t.name = q.label"
    )
    assert guarded.referenced_columns == staff("id", "name", "region") | {
        ("shop", "regions", "region"),
        ("shop", "regions", "name"),
    }
    tree = sqlglot.parse_one(guarded.normalized_sql, read="starrocks")
    assert tree.named_selects == ["id", "name", "region", "label"]


def test_count_star_keeps_the_object_dependency_without_columns() -> None:
    guarded = query("SELECT COUNT(*) AS n FROM hr.staff")
    assert guarded.referenced_objects == {("hr", "staff")}
    assert guarded.referenced_columns == frozenset()


def test_expanded_result_columns_are_bounded_before_any_io() -> None:
    assert query("SELECT * FROM shop.sales", narrowed(max_result_columns=5))
    assert refused("SELECT * FROM shop.sales", narrowed(max_result_columns=4)) is (
        Code.TOO_MANY_COLUMNS
    )
    # 执行计划不返回这些列，只受展开后的 SQL 大小约束。
    assert plan("SELECT * FROM shop.sales", narrowed(max_result_columns=4))


@pytest.mark.parametrize("check", [guard_readonly_query, guard_explain_query])
@pytest.mark.parametrize("sql", ["SELECT * FROM shop.sales", "SELECT * FROM cn.`订单`"])
def test_expanded_sql_size_is_checked_in_utf8_bytes(check, sql: str) -> None:  # type: ignore[no-untyped-def]
    size = len(check(sql, POLICY).normalized_sql.encode())
    assert size > len(sql.encode()) * 2  # 展开后明显变长：限长必须作用于展开结果
    assert check(sql, narrowed(max_sql_bytes=size))
    assert refused(sql, narrowed(max_sql_bytes=size - 1), check=check) is Code.INPUT_TOO_LARGE


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT COUNT(t.*) AS n FROM hr.staff t", Code.STAR_PROJECTION),
        (
            "SELECT region FROM shop.sales WHERE id IN (SELECT COUNT(t.*) FROM hr.staff t)",
            Code.STAR_PROJECTION,
        ),
    ],
)
def test_star_outside_projections_is_rejected(sql: str, code: Code) -> None:
    assert refused(sql) is code


# ---- 结果列名 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT region AS x, total AS x FROM shop.sales",
        "SELECT region AS x, total AS X FROM shop.sales",
        "SELECT * FROM shop.regions r JOIN hr.staff t ON r.region = t.region",
        "SELECT s.id, t.id FROM shop.sales s JOIN hr.staff t ON s.id = t.id",
    ],
)
def test_duplicate_result_names_need_unique_aliases(sql: str) -> None:
    """结果列名重复时 Adapter 无法交付；在任何 I/O 前要求唯一别名，不悄悄改名。"""
    assert refused(sql) is Code.AMBIGUOUS_REFERENCE
    assert plan(sql)  # 执行计划不交付行，按数据库语义保留


def test_star_over_a_derived_table_with_duplicate_names_is_ambiguous() -> None:
    sql = "SELECT * FROM (SELECT s.id, t.id FROM shop.sales s JOIN hr.staff t ON s.id = t.id) q"
    assert refused(sql) is Code.AMBIGUOUS_REFERENCE
    assert refused(sql, check=guard_explain_query) is Code.AMBIGUOUS_REFERENCE


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(total) FROM shop.sales",
        "SELECT 1",
        "SELECT region, COUNT(*) FROM shop.sales GROUP BY region",
        "WITH c AS (SELECT COUNT(*) FROM shop.sales) SELECT * FROM c",
        "SELECT q.region FROM (SELECT region, MAX(total) FROM shop.sales GROUP BY region) q",
        "SELECT COUNT(*) FROM shop.sales UNION ALL SELECT id AS n FROM hr.staff",
    ],
)
def test_expression_outputs_need_an_explicit_alias(sql: str) -> None:
    """sqlglot 会把无别名的表达式列改名为 ``_col_N``：交付的列头会与 StarRocks 不同。执行计划
    与审计原文不交付列头，规范化 SQL 内部的名字前后一致，照常接受（诊断常见的无别名聚合）。"""
    assert refused(sql) is Code.UNNAMED_COLUMN
    assert plan(sql)
    assert audit_references(sql, POLICY)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT region FROM shop.sales WHERE total > (SELECT MAX(total) FROM shop.sales)",
        "SELECT region FROM shop.sales WHERE id IN (SELECT COUNT(*) FROM hr.staff)",
        "SELECT region AS k FROM shop.sales UNION ALL SELECT 'x' FROM hr.staff",
    ],
)
def test_unnamed_expressions_are_fine_where_no_name_is_exposed(sql: str) -> None:
    assert query(sql)


# ---- 仍拒绝的语法 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("INSERT INTO shop.sales (id) VALUES (1)", Code.UNSUPPORTED_SYNTAX),
        ("SELECT id FROM shop.sales; DELETE FROM shop.sales", Code.MULTIPLE_STATEMENTS),
        ("SELECT id FROM shop.sales INTO OUTFILE '/tmp/x'", Code.UNPARSABLE),
        ("SELECT id FROM shop.sales FOR UPDATE", Code.UNSUPPORTED_SYNTAX),
        ("SELECT FOO(id) AS x FROM shop.sales", Code.FUNCTION_NOT_ALLOWED),
        ("SELECT * FROM FILES('path' = 's3://bucket/x')", Code.UNSUPPORTED_SYNTAX),
        ("SELECT * FROM TABLE(GENERATE_SERIES(1, 3)) t", Code.UNSUPPORTED_SYNTAX),
        ("WITH RECURSIVE t AS (SELECT 1 AS n) SELECT n FROM t", Code.UNSUPPORTED_SYNTAX),
        ("SELECT region FROM shop.sales WINDOW w AS (ORDER BY dt)", Code.UNSUPPORTED_SYNTAX),
        ("SELECT * EXCEPT (id) FROM shop.sales", Code.UNSUPPORTED_SYNTAX),
    ],
)
def test_dangerous_or_unsupported_syntax_stays_rejected(sql: str, code: Code) -> None:
    assert refused(sql) is code
