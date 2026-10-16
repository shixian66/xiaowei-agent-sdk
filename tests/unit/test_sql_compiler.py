"""确定性 SQL 编译器。

两条模板共用同一个"目标范围"谓词构造器。这不是风格问题：s2 要回答的是「目标
范围内有没有任何查询」，若它只按同表同窗口计数，窗口里存在其他库/用户的流量时，
「目标范围无审计数据」就会被误报成「目标范围无慢查询」——一个自信但错误的结论。
"""

import ast
import datetime as dt
from pathlib import Path

import pytest
from sqlglot import exp, parse_one

from xiaowei_agent.capabilities.specs import SLOW_QUERY_SURFACE as SURFACE
from xiaowei_agent.planning.starrocks import compiler
from xiaowei_agent.planning.starrocks.compiler import (
    COUNT_V1,
    LIST_V1,
    TEMPLATE_IDS,
    TEMPLATE_PARAM_KEYS,
    compile_sql,
)
from xiaowei_agent.planning.starrocks.params import SlowQueryParams

_START = dt.datetime(2026, 9, 2, 11, 30, tzinfo=dt.UTC)
_END = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)

_PARAMS = SlowQueryParams(
    window_start=_START, window_end=_END, min_query_time_ms=10_000, row_limit=20
)

_LIST_GOLDEN = (
    "SELECT `queryId`, `timestamp`, `queryTime`, `scanRows`, `returnRows`, "
    "`scanBytes`, `memCostBytes`, `pendingTimeMs`, `cpuCostNs`, `state`, "
    "`errorCode`, `db`, `user` "
    "FROM `starrocks_audit_db__`.`starrocks_audit_tbl__` "
    "WHERE `timestamp` >= '2026-09-02 11:30:00' "
    "AND `timestamp` < '2026-09-02 12:00:00' AND `queryTime` >= 10000 "
    "ORDER BY `queryTime` DESC, `timestamp` DESC, `queryId` ASC LIMIT 20"
)

_COUNT_GOLDEN = (
    "SELECT COUNT(*) AS `query_count` "
    "FROM `starrocks_audit_db__`.`starrocks_audit_tbl__` "
    "WHERE `timestamp` >= '2026-09-02 11:30:00' "
    "AND `timestamp` < '2026-09-02 12:00:00' LIMIT 1"
)


def _conjuncts(sql: str) -> set[str]:
    """WHERE 的合取项集合。

    从 AST 取而不是切字符串：把"两条模板的范围相同"表述成集合等式，才不会被
    括号、空白或谓词顺序的变化蒙混过去。
    """
    where = parse_one(sql, read="starrocks").args.get("where")
    assert where is not None, sql
    return {node.sql(dialect="starrocks") for node in where.this.flatten()}


def test_list_template_is_byte_stable() -> None:
    """golden：逐字节固定，任何序列化变化都必须被看见。"""
    assert compile_sql(template_id=LIST_V1, params=_PARAMS, surface=SURFACE) == _LIST_GOLDEN


def test_count_template_is_byte_stable() -> None:
    assert compile_sql(template_id=COUNT_V1, params=_PARAMS, surface=SURFACE) == _COUNT_GOLDEN


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"database": "sales"},
        {"database": "sales", "user_name": "app"},
        {"database": "sales", "user_name": "app", "query_id": "q1"},
    ],
    ids=["none", "db", "db_user", "db_user_query"],
)
def test_count_scope_is_exactly_list_scope_minus_the_threshold(
    filters: dict[str, str],
) -> None:
    """两条模板的 WHERE 合取项只差一个 queryTime 谓词。

    这不是"s2 也加上过滤"的局部检查，而是把「s2 的范围 = s1 的范围」写成集合
    等式：将来任何一条模板单独演化，这条断言都会转红。
    """
    params = _PARAMS.model_copy(update=filters)
    list_where = _conjuncts(compile_sql(template_id=LIST_V1, params=params, surface=SURFACE))
    count_where = _conjuncts(compile_sql(template_id=COUNT_V1, params=params, surface=SURFACE))
    assert list_where - count_where == {"`queryTime` >= 10000"}
    assert count_where - list_where == set()


def test_template_param_keys_differ_only_by_the_threshold() -> None:
    """同一条不变量在参数层的表述——两处都钉住，才不会一处改了另一处没改。"""
    assert set(TEMPLATE_PARAM_KEYS[LIST_V1]) - set(TEMPLATE_PARAM_KEYS[COUNT_V1]) == {
        "min_query_time_ms"
    }
    assert set(TEMPLATE_PARAM_KEYS[COUNT_V1]) - set(TEMPLATE_PARAM_KEYS[LIST_V1]) == set()


def test_template_ids_are_exactly_the_two_declared_templates() -> None:
    assert TEMPLATE_IDS == (LIST_V1, COUNT_V1)
    assert set(TEMPLATE_PARAM_KEYS) == set(TEMPLATE_IDS)


@pytest.mark.parametrize("template_id", [LIST_V1, COUNT_V1])
def test_compilation_is_idempotent(template_id: str) -> None:
    first = compile_sql(template_id=template_id, params=_PARAMS, surface=SURFACE)
    second = compile_sql(template_id=template_id, params=_PARAMS, surface=SURFACE)
    assert first == second


def test_optional_filters_are_appended_in_a_fixed_order() -> None:
    """db → user → queryId 顺序固定；顺序不定会让同一组参数产生两个 plan_hash。"""
    params = _PARAMS.model_copy(update={"database": "sales", "user_name": "app", "query_id": "q1"})
    sql = compile_sql(template_id=LIST_V1, params=params, surface=SURFACE)
    assert sql.index("`db` =") < sql.index("`user` =") < sql.index("`queryId` =")


@pytest.mark.parametrize(
    ("slot", "column"),
    [("database", "db"), ("user_name", "user"), ("query_id", "queryId")],
)
def test_each_optional_filter_adds_exactly_one_predicate(slot: str, column: str) -> None:
    plain = _conjuncts(compile_sql(template_id=LIST_V1, params=_PARAMS, surface=SURFACE))
    filtered = _conjuncts(
        compile_sql(
            template_id=LIST_V1,
            params=_PARAMS.model_copy(update={slot: "sales"}),
            surface=SURFACE,
        )
    )
    assert filtered - plain == {f"`{column}` = 'sales'"}


def test_unset_filters_produce_no_predicate() -> None:
    """未设置的过滤是"不按该维度过滤"，不得编译成 IS NULL 之类的谓词。"""
    conjuncts = _conjuncts(compile_sql(template_id=LIST_V1, params=_PARAMS, surface=SURFACE))
    assert not any("NULL" in item.upper() for item in conjuncts)


def test_row_limit_reaches_the_list_template_but_not_the_count_template() -> None:
    """count 只需要知道"有没有"，恒为 LIMIT 1；它消费 row_limit 仅为键集一致。"""
    params = _PARAMS.model_copy(update={"row_limit": 7})
    assert compile_sql(template_id=LIST_V1, params=params, surface=SURFACE).endswith("LIMIT 7")
    assert compile_sql(template_id=COUNT_V1, params=params, surface=SURFACE).endswith("LIMIT 1")


@pytest.mark.parametrize("template_id", [LIST_V1, COUNT_V1])
def test_compiled_sql_only_touches_declared_columns_and_tables(template_id: str) -> None:
    tree = parse_one(
        compile_sql(template_id=template_id, params=_PARAMS, surface=SURFACE),
        read=SURFACE.dialect,
    )
    for column in (n for n in tree.walk() if isinstance(n, exp.Column)):
        assert column.name in SURFACE.allowed_columns
    for table in (n for n in tree.walk() if isinstance(n, exp.Table)):
        assert f"{table.db}.{table.name}" in SURFACE.allowed_tables


@pytest.mark.parametrize("template_id", [LIST_V1, COUNT_V1])
def test_compiled_sql_is_a_single_statement_without_comments(template_id: str) -> None:
    tree = parse_one(
        compile_sql(template_id=template_id, params=_PARAMS, surface=SURFACE),
        read=SURFACE.dialect,
    )
    assert isinstance(tree, exp.Select)
    assert not any(node.comments for node in tree.walk())


def test_unknown_template_id_fails_closed() -> None:
    with pytest.raises(ValueError, match="template"):
        compile_sql(template_id="starrocks.slow_query.evil.v1", params=_PARAMS, surface=SURFACE)


def test_surface_with_an_unknown_dialect_fails_closed() -> None:
    """编译器不得为未知方言生成 SQL——那等于让声明决定我们发出的语法。"""
    hostile = SURFACE.model_copy(update={"dialect": "postgres"})
    with pytest.raises(ValueError, match="dialect"):
        compile_sql(template_id=LIST_V1, params=_PARAMS, surface=hostile)


def test_compiler_never_interpolates_raw_strings() -> None:
    """AST 扫描：编译器模块内不得出现 f-string 生成 SQL 的写法。"""
    tree = ast.parse(Path(compiler.__file__).read_text(encoding="utf-8"))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)]
