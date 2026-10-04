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

from xiaowei import sqlguard
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
    """唯一允许的未限定名：SELECT 的 ORDER BY 对本层输出别名的引用（交给数据库按原文绑定）。"""
    tree = sqlglot.parse_one(sql, read="starrocks")
    for column in tree.find_all(exp.Column):
        if column.table or isinstance(column.this, exp.Star):
            continue
        order = column.find_ancestor(exp.Order, exp.Select)
        assert isinstance(order, exp.Order) and isinstance(order.parent, exp.Select), sql
        assert column.name in {
            p.alias for p in order.parent.expressions if isinstance(p, exp.Alias)
        }


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
    def audited(sql: str, policy: QueryPolicy) -> object:
        return audit_references(sql, policy, "hr")

    assert audit_references("SELECT id FROM sales", POLICY, "hr")[0] == {("hr", "sales")}
    assert refused("SELECT region FROM regions", check=audited) is Code.OBJECT_NOT_ALLOWED
    # 没有当前库（''）：未限定名一律不在范围内；业务查询的唯一匹配补全不适用于审计原文。
    assert refused("SELECT name FROM staff", check=lambda s, p: audit_references(s, p, "")) is (
        Code.OBJECT_NOT_ALLOWED
    )
    assert audit_references("SELECT name FROM hr.staff", POLICY, "")[0] == {("hr", "staff")}


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
    assert audit_references(sql, POLICY, "")


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


# ---- 名字解析保持 StarRocks 语义 ---------------------------------------------------------------
# StarRocks 4.1.4 实测（可丢弃实例）：WHERE、JOIN ON 与窗口只解析物理列，看不到输出别名；投影、
# GROUP BY 与 HAVING 先物理列、后输出别名；ORDER BY 先输出名（显式别名与裸列的隐式列名），但该
# 输出本身是列、投影另有来自同一来源的同名列时（括号与限定写法不计，``SELECT a AS x, (x) AS z
# ... ORDER BY x``）改按那一列排序，这种交叉别名拒绝。WHERE 中本层没有的名字按外层来源解析
# （相关子查询）。别名与列名都不区分大小写。

BOTH = pytest.mark.parametrize("check", [guard_readonly_query, guard_explain_query])


def normalized(check, sql: str, policy: QueryPolicy = POLICY) -> str:  # type: ignore[no-untyped-def]
    guarded = check(sql, policy)
    assert check(guarded.normalized_sql, policy) == guarded
    assert_no_unqualified_column(guarded.normalized_sql)
    return str(guarded.normalized_sql)


@BOTH
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (  # 窗口 ORDER BY 中与别名同名的名字是物理列，不是别名所指的 total
            "SELECT total AS ORDERS, ROW_NUMBER() OVER (ORDER BY ORDERS) AS n FROM shop.sales",
            "ROW_NUMBER() OVER (ORDER BY `sales`.`orders`) AS `n`",
        ),
        (
            "SELECT total AS Orders, RANK() OVER (PARTITION BY orders ORDER BY id) AS n "
            "FROM shop.sales",
            "OVER (PARTITION BY `sales`.`orders` ORDER BY `sales`.`id`)",
        ),
        ("SELECT total AS Orders FROM shop.sales WHERE Orders > 1", "WHERE `sales`.`orders` > 1"),
        (
            "SELECT s.total AS region FROM shop.sales s JOIN cn.`订单` o "
            "ON s.id = o.`金额` AND region = 'x'",
            "AND `s`.`region` = 'x'",
        ),
        (
            "SELECT total AS orders, COUNT(*) AS n FROM shop.sales GROUP BY orders",
            "GROUP BY `sales`.`orders`",
        ),
        (
            "SELECT region AS orders, COUNT(*) AS n FROM shop.sales GROUP BY region, orders "
            "HAVING orders > 1",
            "HAVING `sales`.`orders` > 1",
        ),
        ("SELECT total AS orders, orders + 0 AS y FROM shop.sales", "`sales`.`orders` + 0 AS `y`"),
    ],
)
def test_names_shadowed_by_an_output_alias_stay_physical_columns(check, sql, expected) -> None:  # type: ignore[no-untyped-def]
    normalized_sql = normalized(check, sql)
    assert expected in normalized_sql
    deps = check(sql, POLICY).referenced_columns
    assert ("shop", "sales", "orders") in deps or ("shop", "sales", "region") in deps


@BOTH
@pytest.mark.parametrize(
    ("sql", "code"),
    [
        (  # 两个来源都有 region：StarRocks 报歧义，不能改按别名执行
            "SELECT s.id AS region FROM shop.sales s JOIN shop.regions r "
            "ON s.region = r.region WHERE region = 'x'",
            Code.AMBIGUOUS_REFERENCE,
        ),
        (
            "SELECT s.id AS region, COUNT(*) AS n FROM shop.sales s JOIN shop.regions r "
            "ON s.region = r.region GROUP BY region",
            Code.AMBIGUOUS_REFERENCE,
        ),
        # WHERE、ON 与窗口看不到输出别名：StarRocks 报列不存在，不能悄悄展开为别名表达式。
        ("SELECT total AS x FROM shop.sales WHERE x > 1", Code.COLUMN_NOT_ALLOWED),
        (
            "SELECT total AS x, ROW_NUMBER() OVER (ORDER BY x) AS n FROM shop.sales",
            Code.COLUMN_NOT_ALLOWED,
        ),
        (
            "SELECT s.total AS x FROM shop.sales s JOIN shop.regions r ON x = r.region",
            Code.COLUMN_NOT_ALLOWED,
        ),
        (
            "SELECT s.total AS orders, (shop.s.orders) AS o2 FROM shop.sales s ORDER BY orders",
            Code.COLUMN_NOT_ALLOWED,  # 库.表.列 的表名是别名：先按库名检查拒绝
        ),
        # 两个同名输出：数据库报歧义。
        (
            "SELECT total AS region, region FROM shop.sales ORDER BY region",
            Code.AMBIGUOUS_REFERENCE,
        ),
        (
            "SELECT s.total AS region, r.region FROM shop.sales s JOIN shop.regions r "
            "ON s.region = r.region ORDER BY region",
            Code.AMBIGUOUS_REFERENCE,
        ),
        (  # 子查询中两个同名输出（不受结果列名检查约束）：同样报歧义
            "SELECT name FROM shop.regions WHERE EXISTS (SELECT s.total AS region, t.region "
            "FROM shop.sales s JOIN hr.staff t ON s.region = t.region ORDER BY region LIMIT 1)",
            Code.AMBIGUOUS_REFERENCE,
        ),
        (  # 相关子查询：两个外层来源都有 region
            "SELECT s.id FROM shop.sales s JOIN shop.regions r ON s.region = r.region "
            "WHERE EXISTS (SELECT h.id AS region FROM hr.sales h WHERE region = 'x')",
            Code.AMBIGUOUS_REFERENCE,
        ),
        (  # 中间层的同名来源别名会截住外层的 s：不能按名字绑定
            "SELECT s.id FROM shop.sales s WHERE EXISTS "
            "(SELECT s.id AS orders FROM hr.staff s WHERE orders > 1)",
            Code.AMBIGUOUS_REFERENCE,
        ),
        (  # 本层、外层都没有物理列：只有别名
            "SELECT s.id FROM shop.sales s WHERE EXISTS "
            "(SELECT h.id AS zz FROM hr.sales h WHERE zz = 1)",
            Code.COLUMN_NOT_ALLOWED,
        ),
        (  # 子查询的别名与外层物理列同名：不猜数据库在相关引用与别名之间的选择
            "SELECT s.id FROM shop.sales s WHERE EXISTS "
            "(SELECT t.id AS total FROM hr.staff t GROUP BY total)",
            Code.AMBIGUOUS_REFERENCE,
        ),
    ],
)
def test_alias_and_column_conflicts_are_not_resolved_by_guessing(check, sql, code) -> None:  # type: ignore[no-untyped-def]
    assert refused(sql, check=check) is code


@BOTH
@pytest.mark.parametrize(
    ("sql", "order"),
    [
        ("SELECT total AS x FROM shop.sales ORDER BY x DESC", "`x` DESC"),
        ("SELECT region AS region FROM shop.sales ORDER BY region", "`region`"),
        ("SELECT s.region AS REGION FROM shop.sales s ORDER BY region", "`REGION`"),
        # 唯一输出别名与物理列同名：保留输出名引用（数据库按别名排序，见 tests/p1b 的对照）。
        ("SELECT total AS orders FROM shop.sales ORDER BY orders", "`orders`"),
        ("SELECT SUM(total) AS orders FROM shop.sales ORDER BY orders", "`orders`"),
        ("SELECT total AS Orders FROM shop.sales ORDER BY orders + 0", "`Orders` + 0"),
        ("SELECT total AS X FROM shop.sales ORDER BY x DESC", "`X` DESC"),
        (
            "SELECT total AS orders, orders + 0 AS o2 FROM shop.sales ORDER BY orders",
            "`orders`",
        ),
        (  # 两个来源都有 region，输出别名唯一
            "SELECT s.id AS region FROM shop.sales s JOIN shop.regions r "
            "ON s.region = r.region ORDER BY region",
            "`region`",
        ),
        # 裸列的隐式输出名（补写为别名）同样是输出名引用：两个来源都有 region 时按输出 s.region。
        (
            "SELECT s.region FROM shop.sales s JOIN shop.regions r "
            "ON s.region = r.region ORDER BY region",
            "`region`",
        ),
        (
            "SELECT (s.region) AS region, r.name FROM shop.sales s JOIN shop.regions r "
            "ON s.region = r.region ORDER BY region DESC",
            "`region` DESC",
        ),
        # 另一同名列来自其他来源，或输出是表达式。
        (
            "SELECT s.id AS region, r.region AS rr FROM shop.sales s JOIN shop.regions r "
            "ON s.region = r.region ORDER BY region",
            "`region`",
        ),
        (
            "SELECT (r.region) AS rr, s.id AS region FROM shop.sales s JOIN shop.regions r "
            "ON s.region = r.region ORDER BY region",
            "`region`",
        ),
        (
            "SELECT a.total AS orders, b.orders AS o2 FROM shop.sales a JOIN shop.sales b "
            "ON a.id = b.id ORDER BY orders",
            "`orders`",
        ),
        (
            "SELECT total + 0 AS orders, (orders) AS o2 FROM shop.sales ORDER BY orders",
            "`orders`",
        ),
        (
            "SELECT CAST(total AS INT) AS orders, orders AS o2 FROM shop.sales ORDER BY orders",
            "`orders`",
        ),
        ("SELECT total AS x, COUNT(*) AS n FROM shop.sales GROUP BY x ORDER BY 1", "1"),
    ],
)
def test_order_by_and_group_by_aliases_without_a_conflict_keep_working(check, sql, order) -> None:  # type: ignore[no-untyped-def]
    assert normalized(check, sql).split(" ORDER BY ", 1)[1].split(" LIMIT ")[0] == order


@BOTH
@pytest.mark.parametrize(
    ("sql", "expected", "outer"),
    [
        (  # 本层没有 orders、WHERE 看不到本层别名：按外层 s.orders 解析
            "SELECT s.id FROM shop.sales s WHERE EXISTS "
            "(SELECT t.id AS orders FROM hr.staff t WHERE orders > t.id)",
            "WHERE `s`.`orders` > `t`.`id`",
            ("shop", "sales", "orders"),
        ),
        (  # 显式外层限定列对照
            "SELECT s.id FROM shop.sales s WHERE EXISTS "
            "(SELECT t.id AS orders FROM hr.staff t WHERE s.orders > t.id)",
            "WHERE `s`.`orders` > `t`.`id`",
            ("shop", "sales", "orders"),
        ),
        (  # 本层物理列优先于外层
            "SELECT s.id FROM shop.sales s WHERE EXISTS "
            "(SELECT t.id AS region FROM hr.staff t WHERE region = 'x')",
            "WHERE `t`.`region` = 'x'",
            ("hr", "staff", "region"),
        ),
        (  # 隔两层的外层列
            "SELECT s.id FROM shop.sales s WHERE EXISTS (SELECT 1 AS k FROM hr.staff t "
            "WHERE EXISTS (SELECT h.id AS orders FROM hr.sales h WHERE orders > h.id))",
            "WHERE `s`.`orders` > `h`.`id`",
            ("shop", "sales", "orders"),
        ),
    ],
)
def test_correlated_names_shadowed_by_an_inner_alias_resolve_outward(  # type: ignore[no-untyped-def]
    check, sql, expected, outer
) -> None:
    assert expected in normalized(check, sql)
    assert outer in check(sql, POLICY).referenced_columns


def test_deep_star_ctes_do_not_exhaust_the_stack() -> None:
    """约 1,200 层星号 CTE（36 KB）：输出名按 scope 缓存、由内向外求得，不递归到栈深。"""
    depth = 1200
    ctes = ["c0 AS (SELECT * FROM shop.regions)"]
    ctes += [f"c{i} AS (SELECT * FROM c{i - 1})" for i in range(1, depth)]
    sql = f"WITH {', '.join(ctes)} SELECT * FROM c{depth - 1}"
    policy = narrowed(max_sql_bytes=200_000)
    for check in (guard_readonly_query, guard_explain_query):
        tree = sqlglot.parse_one(check(sql, policy).normalized_sql, read="starrocks")
        assert tree.named_selects == ["region", "name"]


def test_recursion_while_binding_names_is_a_fixed_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """名字绑定中过深的递归是预期的输入失败：固定原因码，不挂下层异常。"""

    def too_deep(*_: object) -> list[str]:
        raise RecursionError

    monkeypatch.setattr(sqlguard._Names, "_compute", too_deep)
    for check in (guard_readonly_query, guard_explain_query):
        sql = "SELECT * FROM (SELECT * FROM shop.regions) q"
        assert refused(sql, check=check) is Code.UNSUPPORTED_SYNTAX


@BOTH
@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT nope.sales.* FROM shop.sales", Code.COLUMN_NOT_ALLOWED),
        ("SELECT hr.sales.* FROM shop.sales", Code.COLUMN_NOT_ALLOWED),
        ("SELECT shop.s.* FROM shop.sales s", Code.COLUMN_NOT_ALLOWED),
        (
            "WITH q AS (SELECT region FROM shop.sales) SELECT shop.q.* FROM q",
            Code.COLUMN_NOT_ALLOWED,
        ),
        ("SELECT shop.q.* FROM (SELECT region FROM shop.sales) q", Code.COLUMN_NOT_ALLOWED),
        ("SELECT nope.sales.id FROM shop.sales", Code.COLUMN_NOT_ALLOWED),  # 普通列对照
    ],
)
def test_qualified_star_checks_its_database_like_a_column(check, sql, code) -> None:  # type: ignore[no-untyped-def]
    assert refused(sql, check=check) is code


@BOTH
@pytest.mark.parametrize(
    ("sql", "names"),
    [
        ("SELECT shop.sales.* FROM shop.sales", ["id", "region", "total", "orders", "dt"]),
        ("SELECT shop.regions.* FROM regions", ["region", "name"]),
    ],
)
def test_qualified_star_with_the_right_database_expands(check, sql, names) -> None:  # type: ignore[no-untyped-def]
    tree = sqlglot.parse_one(normalized(check, sql), read="starrocks")
    assert tree.named_selects == names


WIDE = narrowed(max_result_columns=7)


@BOTH
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT s.*, o.* FROM shop.sales s JOIN cn.`订单` o "
        "ON s.region = o.`地区`) q",
        "SELECT * FROM (SELECT * FROM shop.sales s JOIN cn.`订单` o ON s.region = o.`地区`) q",
        "WITH a AS (SELECT * FROM shop.sales), b AS (SELECT a.*, o.* FROM a JOIN cn.`订单` o "
        "ON a.region = o.`地区`) SELECT * FROM b",
    ],
)
def test_stars_over_sources_with_distinct_columns_expand_to_unique_names(check, sql) -> None:  # type: ignore[no-untyped-def]
    tree = sqlglot.parse_one(normalized(check, sql, WIDE), read="starrocks")
    assert tree.named_selects == ["id", "region", "total", "orders", "dt", "金额", "地区"]


@BOTH
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT s.*, t.* FROM shop.sales s JOIN hr.staff t ON s.id = t.id) q",
        "SELECT * FROM (SELECT * FROM shop.sales s JOIN hr.staff t ON s.id = t.id) q",
        "WITH a AS (SELECT * FROM shop.sales) "
        "SELECT * FROM (SELECT a.*, t.name AS ID FROM a JOIN hr.staff t ON a.id = t.id) q",
    ],
)
def test_stars_that_expand_to_duplicate_names_stay_ambiguous(check, sql) -> None:  # type: ignore[no-untyped-def]
    assert refused(sql, WIDE, check=check) is Code.AMBIGUOUS_REFERENCE


def test_stars_over_distinct_sources_still_respect_the_column_cap() -> None:
    sql = "SELECT * FROM (SELECT s.*, o.* FROM shop.sales s JOIN cn.`订单` o ON s.id = o.`金额`) q"
    assert refused(sql) is Code.TOO_MANY_COLUMNS  # 7 列，上限 6


@BOTH
@pytest.mark.parametrize(
    ("sql", "names"),
    [
        ("WITH q AS (SELECT REGION FROM shop.sales) SELECT REGION FROM q", ["REGION"]),
        ("WITH q AS (SELECT REGION FROM shop.sales) SELECT region FROM q", ["region"]),
        ("WITH q AS (SELECT REGION FROM shop.sales) SELECT q.Region FROM q", ["Region"]),
        ("WITH q AS (SELECT REGION FROM shop.sales) SELECT * FROM q", ["REGION"]),
        ("WITH q AS (SELECT REGION FROM shop.sales) SELECT q.* FROM q", ["REGION"]),
        ("SELECT * FROM (SELECT * FROM (SELECT Total FROM shop.sales) a) b", ["Total"]),
        (
            "WITH q AS (SELECT Region FROM shop.sales UNION ALL SELECT name FROM hr.staff) "
            "SELECT * FROM q",
            ["Region"],
        ),
        (
            "WITH q AS (SELECT s.REGION, t.Name FROM shop.sales s JOIN hr.staff t ON s.id = t.id) "
            "SELECT region, NAME FROM q WHERE Region > 'a'",
            ["region", "NAME"],
        ),
    ],
)
def test_derived_output_names_keep_their_spelling_through_normalization(check, sql, names) -> None:  # type: ignore[no-untyped-def]
    """StarRocks 的 CTE/派生表输出名沿用写法，引用时不区分大小写；规范化不能改写它们。"""
    tree = sqlglot.parse_one(normalized(check, sql), read="starrocks")
    assert tree.named_selects == names


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


@BOTH
def test_order_by_an_implicit_output_name_inside_a_subquery(check) -> None:  # type: ignore[no-untyped-def]
    """标量子查询的裸列不补别名：ORDER BY 的名字改写为同一列，不按两个来源报歧义。"""
    sql = (
        "SELECT (SELECT s.region FROM shop.sales s JOIN shop.regions r "
        "ON s.region = r.region ORDER BY region LIMIT 1) AS v"
    )
    assert "ORDER BY `s`.`region` LIMIT 1" in normalized(check, sql)
    # 未起别名的括号列同样以列名作为输出名（4.1.4 实测）
    assert "ORDER BY `s`.`region` LIMIT 1" in normalized(
        check, sql.replace("s.region F", "((s.region)) F")
    )


# ORDER BY 中与输出名同名的引用原样保留（只改为带引号的输出名），由数据库按自己的规则绑定：
# 4.1.4 按投影、分组与窗口中出现的列决定排序名指向输出列还是同名物理列（见 tests/p1b 的真实
# 对照）。把输出表达式内联进 ORDER BY 会改变这一绑定，内联标量子查询还会成为数据库拒绝的语句。
@BOTH
@pytest.mark.parametrize(
    ("sql", "order"),
    [
        # 表达式、分组与窗口：数据库可能按物理列 orders 排序，不能改写为别名表达式
        (
            "SELECT total AS orders, orders + 0 AS o2 FROM shop.sales ORDER BY orders + 0, id",
            "`orders` + 0, `sales`.`id`",
        ),
        (
            "SELECT total AS orders, COUNT(*) AS n FROM shop.sales GROUP BY total, orders "
            "ORDER BY orders",
            "`orders`",
        ),
        (
            "SELECT id, total AS orders, ROW_NUMBER() OVER (PARTITION BY region ORDER BY orders) "
            "AS n FROM shop.sales ORDER BY orders, id",
            "`orders`, `id`",  # id 也是输出名
        ),
        (  # 原文在数据库报错（排序引用非分组列）：保留引用，同样报错，不被“修正”为成功
            "SELECT total AS orders, SUM(orders) AS n FROM shop.sales GROUP BY total "
            "ORDER BY orders",
            "`orders`",
        ),
        # 标量子查询别名：相关、非相关、被 CASE/COALESCE 包裹，以及排序表达式
        (
            "SELECT s.id, (SELECT MAX(r.name) FROM shop.regions r WHERE r.region = s.region) "
            "AS z FROM shop.sales s ORDER BY z, s.id",
            "`z`, `s`.`id`",
        ),
        (
            "SELECT (SELECT MAX(r.name) FROM shop.regions r) AS z FROM shop.sales ORDER BY z",
            "`z`",
        ),
        (
            "SELECT s.id, COALESCE((SELECT MAX(r.name) FROM shop.regions r "
            "WHERE r.region = s.region), 'x') AS z FROM shop.sales s ORDER BY z, s.id",
            "`z`, `s`.`id`",
        ),
        (
            "SELECT s.id, CASE WHEN s.id > 2 THEN (SELECT COUNT(*) FROM shop.regions r) "
            "ELSE 0 END AS z FROM shop.sales s ORDER BY z + 0, s.id",
            "`z` + 0, `s`.`id`",
        ),
        # 别名与同名列交叉（此前拒绝）：保留引用即保持数据库原本的选择
        ("SELECT id AS region, region AS id FROM shop.sales ORDER BY id", "`id`"),
        ("SELECT total AS orders, (orders) AS o2 FROM shop.sales ORDER BY -orders", "-`orders`"),
        (
            "SELECT * FROM (SELECT total AS orders, (orders) AS o2 FROM shop.sales "
            "ORDER BY orders LIMIT 1) d",
            "",
        ),
        # 分组查询中与投影表达式相同的排序项不被 sqlglot 改写为该投影的别名
        (
            "SELECT id AS region, COUNT(*) AS n FROM shop.sales GROUP BY id, region "
            "ORDER BY region, id",
            "`region`, `sales`.`id`",
        ),
        (
            "SELECT total AS orders, COUNT(*) AS n FROM shop.sales GROUP BY total, orders "
            "ORDER BY total DESC, orders",
            "`sales`.`total` DESC, `orders`",
        ),
        # 同一 CTE 的两个关系别名
        (
            "WITH c AS (SELECT * FROM shop.sales) SELECT a.total AS orders, b.orders AS o2 "
            "FROM c a JOIN c b ON a.id = b.id ORDER BY orders, a.id",
            "`orders`, `a`.`id`",
        ),
    ],
)
def test_order_by_output_references_are_left_for_the_database(check, sql, order) -> None:  # type: ignore[no-untyped-def]
    normalized_sql = normalized(check, sql)
    tree = sqlglot.parse_one(normalized_sql, read="starrocks")
    clause = tree.args.get("order")
    ordered = ", ".join(o.sql(dialect="starrocks") for o in clause.expressions) if clause else ""
    assert ordered == order
    assert "SELECT MAX" not in ordered and "SELECT COUNT" not in ordered


@BOTH
def test_union_order_by_a_parenthesized_name_uses_its_position(check) -> None:  # type: ignore[no-untyped-def]
    sql = "SELECT region FROM shop.sales UNION SELECT name FROM hr.staff ORDER BY (region) DESC"
    assert tail(normalized(check, sql)).startswith("ORDER BY 1 DESC")


@BOTH
def test_order_by_output_reference_depends_on_both_possible_bindings(check) -> None:  # type: ignore[no-untyped-def]
    """数据库可能把排序名绑定到输出表达式或本层同名物理列：两者都计入依赖（都在可读对象中）。"""
    deps = check(
        "SELECT total AS orders FROM shop.sales ORDER BY orders", POLICY
    ).referenced_columns
    assert deps == sales("total", "orders")
    # 子查询里的输出名引用同样计入，且只计本层来源
    deps = check(
        "SELECT name FROM shop.regions WHERE EXISTS "
        "(SELECT h.id AS amount FROM hr.sales h ORDER BY amount LIMIT 1)",
        POLICY,
    ).referenced_columns
    assert ("hr", "sales", "amount") in deps and ("hr", "sales", "id") in deps
