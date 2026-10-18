"""StarRocks SQL 解析依赖的方言与 AST 行为；样本只解析，不执行查询。"""

import pytest
from sqlglot import exp, parse, parse_one

# 审计查询样本用于验证方言解析；不作为产品执行模板。
_LIST_SQL = (
    "SELECT `queryId`, `timestamp`, `queryTime`, `scanRows`, `returnRows`, "
    "`scanBytes`, `memCostBytes`, `pendingTimeMs`, `cpuCostNs`, `state`, "
    "`errorCode`, `db`, `user` "
    "FROM `starrocks_audit_db__`.`starrocks_audit_tbl__` "
    "WHERE `timestamp` >= '2026-09-02 11:30:00' "
    "AND `timestamp` < '2026-09-02 12:00:00' AND `queryTime` >= 10000 "
    "ORDER BY `queryTime` DESC, `timestamp` DESC, `queryId` ASC LIMIT 20"
)

_COUNT_SQL = (
    "SELECT COUNT(*) AS `query_count` "
    "FROM `starrocks_audit_db__`.`starrocks_audit_tbl__` "
    "WHERE `timestamp` >= '2026-09-02 11:30:00' "
    "AND `timestamp` < '2026-09-02 12:00:00' LIMIT 1"
)


@pytest.mark.parametrize("sql", [_LIST_SQL, _COUNT_SQL], ids=["list", "count"])
def test_starrocks_dialect_exists_and_parses_the_templates(sql: str) -> None:
    parsed = parse(sql, read="starrocks")
    assert len(parsed) == 1
    assert isinstance(parsed[0], exp.Select)


def test_multi_statement_yields_more_than_one_expression() -> None:
    """规则 3 的地基。"""
    assert len(parse("SELECT 1 FROM t; DROP TABLE t", read="starrocks")) == 2


def test_comments_live_on_node_attributes_not_as_ast_nodes() -> None:
    """规则 9 的地基：注释不是节点，必须遍历 .comments。"""
    tree = parse_one("SELECT a /* inject */ FROM t", read="starrocks")
    assert not any(type(node).__name__ == "Comment" for node in tree.walk())
    assert any(node.comments for node in tree.walk())


@pytest.mark.parametrize(
    "sql",
    ["SELECT * FROM t LIMIT 1", "SELECT COUNT(*) FROM t LIMIT 1"],
    ids=["bare_star", "count_star"],
)
def test_bare_star_and_count_star_both_produce_star_nodes(sql: str) -> None:
    """规则 8 的地基：Star 必须带父节点约束，否则 SELECT * 会被放行。"""
    tree = parse_one(sql, read="starrocks")
    assert any(isinstance(node, exp.Star) for node in tree.walk())


def test_count_star_parent_is_count_but_bare_star_parent_is_not() -> None:
    """规则 8 的父节点约束必须可判定：两种 Star 的 parent 必须能区分开。"""
    bare = parse_one("SELECT * FROM t LIMIT 1", read="starrocks")
    counted = parse_one("SELECT COUNT(*) FROM t LIMIT 1", read="starrocks")
    bare_star = next(n for n in bare.walk() if isinstance(n, exp.Star))
    counted_star = next(n for n in counted.walk() if isinstance(n, exp.Star))
    assert not isinstance(bare_star.parent, exp.Count)
    assert isinstance(counted_star.parent, exp.Count)


@pytest.mark.parametrize(
    "sql",
    ["SELECT a FROM t FOR UPDATE", "SELECT @x := 1 FROM t"],
    ids=["for_update", "variable_assignment"],
)
def test_side_effecting_reads_parse_as_select(sql: str) -> None:
    """加锁读与变量赋值都解析成 Select——根类型检查挡不住，必须靠节点闭集。"""
    assert isinstance(parse_one(sql, read="starrocks"), exp.Select)


@pytest.mark.parametrize(
    ("sql", "node_type"),
    [
        ("SELECT a FROM t FOR UPDATE", exp.Lock),
        ("SELECT @x := 1 FROM t", exp.PropertyEQ),
        ("SELECT @x := 1 FROM t", exp.Parameter),
    ],
    ids=["lock", "property_eq", "parameter"],
)
def test_side_effecting_reads_are_distinguishable_by_node_type(
    sql: str, node_type: type[exp.Expression]
) -> None:
    """规则 5 的地基：加锁读与赋值必须在**节点类型**上留下痕迹（A10/A11）。"""
    tree = parse_one(sql, read="starrocks")
    assert any(isinstance(node, node_type) for node in tree.walk())


@pytest.mark.parametrize(
    ("sql", "catalog", "db"),
    [
        ("SELECT 1 FROM `d`.`t`", "", "d"),
        ("SELECT 1 FROM `t`", "", ""),
        ("SELECT 1 FROM `c`.`d`.`t`", "c", "d"),
    ],
    ids=["two_part", "bare", "three_part"],
)
def test_table_parts_expose_catalog_and_db(sql: str, catalog: str, db: str) -> None:
    """规则 6 的地基：三段名与裸表名必须能被区分出来（A31/A32）。"""
    table = next(n for n in parse_one(sql, read="starrocks").walk() if isinstance(n, exp.Table))
    assert (table.catalog, table.db) == (catalog, db)


@pytest.mark.parametrize(
    ("sql", "is_string"),
    [
        ("SELECT `a` FROM `d`.`t` LIMIT 20", False),
        ("SELECT `a` FROM `d`.`t` LIMIT '20'", True),
    ],
    ids=["integer", "string"],
)
def test_limit_literal_type_is_observable(sql: str, is_string: bool) -> None:
    """规则 10 的地基：LIMIT '20' 能解析，必须靠 is_string 区分（A34）。"""
    tree = parse_one(sql, read="starrocks")
    limit = next(n for n in tree.walk() if isinstance(n, exp.Limit))
    expression = limit.expression
    assert isinstance(expression, exp.Literal)
    assert expression.is_string is is_string


def test_union_parses_with_a_non_select_root() -> None:
    """规则 4 的地基：UNION 的根节点不是 Select（A6）。"""
    tree = parse_one("SELECT 1 FROM t UNION SELECT 2 FROM u", read="starrocks")
    assert not isinstance(tree, exp.Select)
    assert isinstance(tree, exp.Union)


def test_semicolon_inside_a_string_literal_is_not_a_statement_separator() -> None:
    """A22 的地基：字符串内的分号不得被当成多语句边界。"""
    parsed = parse("SELECT `a` FROM `d`.`t` WHERE `a` = 'x;y' LIMIT 1", read="starrocks")
    assert len(parsed) == 1
