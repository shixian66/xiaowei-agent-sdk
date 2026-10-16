"""SQLGuard：闭集 AST 校验 + 重编译比对。

§9.4 的攻击矩阵逐条落成参数化用例。两条贯穿全篇的要求：

1. **闸门不能是恒拒。** 恒拒的闸门在攻击矩阵下也全绿——先证明 happy path 通过，
   攻击用例的绿才有意义。
2. **拒绝码必须是最具体的那一个。** 顺序即拒绝原因的优先级；顺序错了，审计里
   看到的会是更宽泛的原因，错误归因随之失真。

**恶意 SQL 一律用 ``_sql()`` 拼接而不是 f-string**：f-string 拼 SQL 会被 ruff 的
S608 拦下，而那条规则在生产代码里是对的（编译器侧由
``test_compiler_never_interpolates_raw_strings`` 用 AST 扫描更严格地承重）。
这里不豁免规则，改用不触发它的写法。
"""

import datetime as dt

import pytest
from sqlglot import exp, parse_one

from xiaowei_agent.capabilities.specs import SLOW_QUERY_SURFACE as SURFACE
from xiaowei_agent.contracts import SqlGuardRejection
from xiaowei_agent.governance.sqlguard import SqlGuardError, table_key, verify_sql
from xiaowei_agent.planning.starrocks.compiler import COUNT_V1, LIST_V1, compile_sql
from xiaowei_agent.planning.starrocks.params import SlowQueryParams

pytestmark = pytest.mark.security

_PARAMS = SlowQueryParams(
    window_start=dt.datetime(2026, 9, 2, 11, 30, tzinfo=dt.UTC),
    window_end=dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC),
    min_query_time_ms=10_000,
    row_limit=20,
)

_FROM = "FROM `starrocks_audit_db__`.`starrocks_audit_tbl__`"
_WHERE = "WHERE `timestamp` >= '2026-09-02 11:30:00' AND `timestamp` < '2026-09-02 12:00:00'"
_SELECT_ID = "SELECT `queryId`"
_LIMIT_1 = "LIMIT 1"


def _sql(*parts: str) -> str:
    """把片段拼成一条 SQL。仅供构造**被检**的恶意样本使用。"""
    return " ".join(part for part in parts if part)


def _check(sql: str, *, template_id: str = LIST_V1, params: SlowQueryParams = _PARAMS) -> None:
    verify_sql(sql=sql, params=params, surface=SURFACE, template_id=template_id)


def _rejection(
    sql: str, *, template_id: str = LIST_V1, params: SlowQueryParams = _PARAMS
) -> SqlGuardRejection:
    with pytest.raises(SqlGuardError) as err:
        _check(sql, template_id=template_id, params=params)
    return err.value.rejection


# --- 先证明闸门不是恒拒 -------------------------------------------------------


@pytest.mark.parametrize("template_id", [LIST_V1, COUNT_V1])
def test_the_happy_path_passes(template_id: str) -> None:
    """恒拒的闸门在攻击矩阵下也全绿，因此必须先证明正例通过。"""
    _check(
        compile_sql(template_id=template_id, params=_PARAMS, surface=SURFACE),
        template_id=template_id,
    )


@pytest.mark.parametrize(
    "filters",
    [
        {"database": "sales"},
        {"database": "sales", "user_name": "app_user_1"},
        {"database": "sales", "user_name": "app_user_1", "query_id": "q1"},
    ],
    ids=["db", "db_user", "db_user_query"],
)
@pytest.mark.parametrize("template_id", [LIST_V1, COUNT_V1])
def test_the_happy_path_passes_with_optional_filters(
    template_id: str, filters: dict[str, str]
) -> None:
    params = _PARAMS.model_copy(update=filters)
    _check(
        compile_sql(template_id=template_id, params=params, surface=SURFACE),
        template_id=template_id,
        params=params,
    )


def test_table_key_is_the_single_comparison_source() -> None:
    """声明侧与校验侧共用同一个键函数。

    surface 里的表名必须能由 table_key() 对某个解析结果产出，否则 happy path 会
    静默不匹配——而这种不匹配在攻击矩阵下表现为"全部拒绝"，很容易被误读成
    "闸门很严"。
    """
    tree = parse_one(
        compile_sql(template_id=LIST_V1, params=_PARAMS, surface=SURFACE),
        read=SURFACE.dialect,
    )
    table = next(node for node in tree.walk() if isinstance(node, exp.Table))
    assert table_key(table) in SURFACE.allowed_tables


# --- A2-A11：语句形状与节点闭集 ------------------------------------------------


def test_a2_multiple_statements() -> None:
    sql = _sql(_SELECT_ID, _FROM, _WHERE, "LIMIT 1; DROP TABLE t")
    assert _rejection(sql) is SqlGuardRejection.MULTIPLE_STATEMENTS


def test_a22_semicolon_inside_a_string_literal_is_not_multiple_statements() -> None:
    """反向用例：字符串内的分号不得被误判为多语句。

    没有这条，一个"把带分号的 SQL 全拒掉"的实现也能让 A2 变绿。
    """
    sql = _sql(_SELECT_ID, _FROM, _WHERE, "AND `db` = 'x;y'", _LIMIT_1)
    assert _rejection(sql) is not SqlGuardRejection.MULTIPLE_STATEMENTS


@pytest.mark.parametrize(
    "parts",
    [
        (_SELECT_ID, _FROM, _WHERE, "LIMIT 1 -- inject"),
        (_SELECT_ID, "/* inject */", _FROM, _WHERE, _LIMIT_1),
        (_SELECT_ID, _FROM, _WHERE, "/* x */", _LIMIT_1),
    ],
    ids=["a3_line", "a4_block", "a4_block_tail"],
)
def test_a3_a4_comments_are_refused(parts: tuple[str, ...]) -> None:
    assert _rejection(_sql(*parts)) is SqlGuardRejection.COMMENT_PRESENT


def test_a5_or_predicate_is_a_forbidden_node() -> None:
    sql = _sql(_SELECT_ID, _FROM, _WHERE, "OR 1 = 1", _LIMIT_1)
    assert _rejection(sql) is SqlGuardRejection.FORBIDDEN_NODE


def test_a6_union_has_a_non_select_root() -> None:
    sql = _sql(_SELECT_ID, _FROM, _WHERE, _LIMIT_1, "UNION", _SELECT_ID, _FROM, _WHERE, _LIMIT_1)
    assert _rejection(sql) is SqlGuardRejection.NON_SELECT


@pytest.mark.parametrize(
    "parts",
    [
        ("INSERT INTO", _FROM.removeprefix("FROM "), "(`queryId`) VALUES ('x')"),
        ("UPDATE", _FROM.removeprefix("FROM "), "SET `queryId` = 'x'"),
        ("DELETE", _FROM),
        ("DROP TABLE", _FROM.removeprefix("FROM ")),
        ("ALTER TABLE", _FROM.removeprefix("FROM "), "ADD COLUMN `x` INT"),
        ("TRUNCATE TABLE", _FROM.removeprefix("FROM ")),
    ],
    ids=["insert", "update", "delete", "drop", "alter", "truncate"],
)
def test_a7_write_statements_are_refused(parts: tuple[str, ...]) -> None:
    assert _rejection(_sql(*parts)) in {
        SqlGuardRejection.NON_SELECT,
        SqlGuardRejection.UNPARSABLE,
    }


@pytest.mark.parametrize(
    "parts",
    [
        (_SELECT_ID, _FROM, _WHERE, _LIMIT_1, "INTO OUTFILE '/tmp/x'"),
        (_SELECT_ID, "INTO @var", _FROM, _WHERE, _LIMIT_1),
    ],
    ids=["a8_outfile", "a9_into_var"],
)
def test_a8_a9_into_is_refused(parts: tuple[str, ...]) -> None:
    assert _rejection(_sql(*parts)) in {
        SqlGuardRejection.UNPARSABLE,
        SqlGuardRejection.FORBIDDEN_NODE,
    }


@pytest.mark.parametrize("suffix", ["FOR UPDATE", "FOR SHARE", "LOCK IN SHARE MODE"])
def test_a10_locking_reads_are_forbidden_nodes(suffix: str) -> None:
    sql = _sql(_SELECT_ID, _FROM, _WHERE, _LIMIT_1, suffix)
    assert _rejection(sql) in {
        SqlGuardRejection.FORBIDDEN_NODE,
        SqlGuardRejection.UNPARSABLE,
    }


def test_a11_variable_assignment_is_a_forbidden_node() -> None:
    sql = _sql("SELECT @x := 1", _FROM, _WHERE, _LIMIT_1)
    assert _rejection(sql) is SqlGuardRejection.FORBIDDEN_NODE


def test_node_allowlist_is_a_closed_set_not_a_denylist() -> None:
    """新增一种节点类型时默认应被拒，而不是默认放行。"""
    sql = _sql("SELECT ABS(`queryTime`)", _FROM, _WHERE, _LIMIT_1)
    assert _rejection(sql) is SqlGuardRejection.FORBIDDEN_NODE


@pytest.mark.parametrize(
    "parts",
    [
        (_SELECT_ID, "FROM (", _SELECT_ID, _FROM, ") AS `t`", _WHERE, _LIMIT_1),
        ("WITH `c` AS (", _SELECT_ID, _FROM, ")", _SELECT_ID, "FROM `c`", _LIMIT_1),
        ("SELECT `a`.`queryId`", _FROM, "AS `a`", _WHERE, _LIMIT_1),
    ],
    ids=["subquery", "cte", "table_alias"],
)
def test_subqueries_ctes_and_aliases_are_forbidden(parts: tuple[str, ...]) -> None:
    assert _rejection(_sql(*parts)) in {
        SqlGuardRejection.FORBIDDEN_NODE,
        SqlGuardRejection.NON_SELECT,
        SqlGuardRejection.COLUMN_NOT_ALLOWED,
        SqlGuardRejection.TABLE_NOT_ALLOWED,
        SqlGuardRejection.UNPARSABLE,
    }


# --- A12：方言 ---------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["postgres", "oracle", "mysql"])
def test_a12_unknown_dialect_is_refused(dialect: str) -> None:
    hostile = SURFACE.model_copy(update={"dialect": dialect})
    with pytest.raises(SqlGuardError) as err:
        verify_sql(
            sql=compile_sql(template_id=LIST_V1, params=_PARAMS, surface=SURFACE),
            params=_PARAMS,
            surface=hostile,
            template_id=LIST_V1,
        )
    assert err.value.rejection is SqlGuardRejection.UNKNOWN_DIALECT


def test_empty_dialect_is_unrepresentable_at_the_contract_layer() -> None:
    """空方言在契约层就构造不出来——这条边界由 StrictStr 承重，不靠 Guard。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SURFACE.model_copy(update={"dialect": ""})


# --- A13/A34：LIMIT ----------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        ("LIMIT 999", SqlGuardRejection.LIMIT_EXCEEDED),
        ("LIMIT 0", SqlGuardRejection.LIMIT_EXCEEDED),
        ("", SqlGuardRejection.LIMIT_MISSING),
        ("LIMIT '20'", SqlGuardRejection.LIMIT_MISSING),
    ],
    ids=["a13_too_large", "a13_zero", "a13_missing", "a34_string_literal"],
)
def test_a13_a34_limit_rules(limit: str, expected: SqlGuardRejection) -> None:
    assert _rejection(_sql(_SELECT_ID, _FROM, _WHERE, limit)) is expected


def test_a13_negative_limit_is_refused() -> None:
    sql = _sql(_SELECT_ID, _FROM, _WHERE, "LIMIT -1")
    assert _rejection(sql) in {
        SqlGuardRejection.LIMIT_EXCEEDED,
        SqlGuardRejection.LIMIT_MISSING,
        SqlGuardRejection.FORBIDDEN_NODE,
        SqlGuardRejection.UNPARSABLE,
    }


# --- A14/A16/A17：时间窗 ------------------------------------------------------


@pytest.mark.parametrize(
    "where",
    [
        "WHERE `timestamp` < '2026-09-02 12:00:00'",
        "WHERE `timestamp` >= '2026-09-02 11:30:00'",
        "",
    ],
    ids=["a16_no_lower", "a16_no_upper", "a16_no_where"],
)
def test_a16_unbounded_windows_are_refused(where: str) -> None:
    assert (
        _rejection(_sql(_SELECT_ID, _FROM, where, _LIMIT_1)) is SqlGuardRejection.WINDOW_UNBOUNDED
    )


@pytest.mark.parametrize(
    "where",
    [
        ("WHERE `timestamp` >= '2026-09-02 10:00:00' AND `timestamp` < '2026-09-02 12:00:00'"),
        ("WHERE `timestamp` >= '2026-09-02 11:30:00' AND `timestamp` < '2026-09-02 13:00:00'"),
    ],
    ids=["start_moved", "end_moved"],
)
def test_a17_window_literals_must_equal_the_params(where: str) -> None:
    assert _rejection(_sql(_SELECT_ID, _FROM, where, _LIMIT_1)) is SqlGuardRejection.WINDOW_MISMATCH


def test_a14_window_wider_than_the_cap_is_refused_at_the_sql_layer() -> None:
    """参数层已经挡过一次；这一层必须独立承重，否则它是死代码。

    直接构造一份超宽窗口的 params 是不可能的（参数层拒绝），因此这里把 surface
    的上限调窄——它模拟的是上限收紧、而计划是在旧上限下编译的情形。
    """
    narrow = SURFACE.model_copy(update={"max_window_minutes": 10})
    sql = compile_sql(template_id=LIST_V1, params=_PARAMS, surface=SURFACE)
    with pytest.raises(SqlGuardError) as err:
        verify_sql(sql=sql, params=_PARAMS, surface=narrow, template_id=LIST_V1)
    assert err.value.rejection is SqlGuardRejection.WINDOW_TOO_WIDE


# --- A18/A31/A32/A33：表 -----------------------------------------------------


@pytest.mark.parametrize(
    "from_clause",
    [
        "FROM `information_schema`.`tables`",
        "FROM `cat`.`starrocks_audit_db__`.`starrocks_audit_tbl__`",
        "FROM `starrocks_audit_tbl__`",
        "FROM `starrocks_audit_db__`.`STARROCKS_AUDIT_TBL__`",
    ],
    ids=["a18_not_allowlisted", "a31_three_part", "a32_bare", "a33_case_variant"],
)
def test_table_rules(from_clause: str) -> None:
    assert (
        _rejection(_sql(_SELECT_ID, from_clause, _WHERE, _LIMIT_1))
        is SqlGuardRejection.TABLE_NOT_ALLOWED
    )


# --- A19/A20/A33：列与 Star ---------------------------------------------------


@pytest.mark.parametrize(
    "projection",
    ["SELECT `stmt`", "SELECT `QUERYID`", "SELECT `digest`", "SELECT `clientIp`"],
    ids=["a19_stmt", "a33_case_variant", "excluded_digest", "excluded_client_ip"],
)
def test_column_rules(projection: str) -> None:
    assert (
        _rejection(_sql(projection, _FROM, _WHERE, _LIMIT_1))
        is SqlGuardRejection.COLUMN_NOT_ALLOWED
    )


def test_a20_bare_star_is_refused() -> None:
    sql = _sql("SELECT *", _FROM, _WHERE, _LIMIT_1)
    assert _rejection(sql) is SqlGuardRejection.STAR_NOT_ALLOWED


def test_count_star_is_allowed() -> None:
    """反例配对：规则 8 的父节点约束不得把 COUNT(*) 一起拒掉。"""
    _check(
        compile_sql(template_id=COUNT_V1, params=_PARAMS, surface=SURFACE),
        template_id=COUNT_V1,
    )


def test_output_alias_outside_the_allowlist_is_refused() -> None:
    sql = _sql("SELECT COUNT(*) AS `leaked`", _FROM, _WHERE, _LIMIT_1)
    assert _rejection(sql, template_id=COUNT_V1) is SqlGuardRejection.COLUMN_NOT_ALLOWED


# --- A21：歧义字符 ------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ["​", "‘", "！", "‮"],
    ids=["zero_width", "smart_quote", "fullwidth", "rtl_override"],
)
def test_a21_ambiguous_characters_are_refused(hostile: str) -> None:
    """§9.3 第二层在设计上不可达（SQL 由我们自己的模板生成）。

    不单独测就是死代码：这条用例绕过参数层直接调 verify_sql，证明它确实承重。
    """
    predicate = "AND `db` = '" + hostile + "'"
    sql = _sql(_SELECT_ID, _FROM, _WHERE, predicate, _LIMIT_1)
    assert _rejection(sql) is SqlGuardRejection.AMBIGUOUS_CHARACTER


def test_ambiguous_character_layer_carries_weight_on_its_own() -> None:
    """歧义字符必须在**解析之前**被拒，不能依赖解析恰好失败。"""
    sql = _sql(_SELECT_ID + "​", _FROM, _WHERE, _LIMIT_1)
    assert _rejection(sql) is SqlGuardRejection.AMBIGUOUS_CHARACTER


# --- A36 与重编译比对 ---------------------------------------------------------


def test_a36_count_sql_stripped_of_the_scope_filters_is_refused() -> None:
    """B1 的回归用例：去掉目标过滤的 count SQL 必须被重编译比对挡下。"""
    params = _PARAMS.model_copy(update={"database": "sales"})
    stripped = compile_sql(template_id=COUNT_V1, params=_PARAMS, surface=SURFACE)
    assert (
        _rejection(stripped, template_id=COUNT_V1, params=params)
        is SqlGuardRejection.RECOMPILE_MISMATCH
    )


def test_sql_that_passes_every_ast_rule_but_is_not_our_output_is_refused() -> None:
    """重编译比对是最后一道闸：阈值被改小，但每条 AST 规则都过得去。"""
    sql = _sql(
        "SELECT `queryId`, `timestamp`, `queryTime`, `scanRows`, `returnRows`,",
        "`scanBytes`, `memCostBytes`, `pendingTimeMs`, `cpuCostNs`, `state`,",
        "`errorCode`, `db`, `user`",
        _FROM,
        _WHERE,
        "AND `queryTime` >= 9999",
        "ORDER BY `queryTime` DESC, `timestamp` DESC, `queryId` ASC LIMIT 20",
    )
    assert _rejection(sql) is SqlGuardRejection.RECOMPILE_MISMATCH


def test_recompile_comparison_runs_last_so_specific_reasons_survive() -> None:
    """顺序即拒绝原因的优先级。

    若重编译比对排在最前，**每一条**攻击用例都会得到 RECOMPILE_MISMATCH——
    审计里看到的会是最宽泛的那个原因，而 AST 规则全部沦为不可达的死代码。
    """
    sql = _sql("SELECT *", _FROM, _WHERE, _LIMIT_1)
    assert _rejection(sql) is SqlGuardRejection.STAR_NOT_ALLOWED


def test_unknown_template_id_fails_closed() -> None:
    sql = compile_sql(template_id=LIST_V1, params=_PARAMS, surface=SURFACE)
    assert (
        _rejection(sql, template_id="starrocks.slow_query.evil.v1")
        is SqlGuardRejection.UNKNOWN_TEMPLATE
    )


# --- 拒绝路径卫生 -------------------------------------------------------------


def test_rejection_message_never_echoes_the_sql() -> None:
    canary = "canary" + "-marker-9137"
    projection = "SELECT `" + canary + "`"
    with pytest.raises(SqlGuardError) as err:
        _check(_sql(projection, _FROM, _WHERE, _LIMIT_1))
    assert canary not in str(err.value)
    assert canary not in repr(err.value)


def test_every_rejection_code_is_a_closed_set_member() -> None:
    """拒绝码闭集：新增一种拒绝必须改枚举并过评审。"""
    assert _rejection(_sql("SELECT *", _FROM, _WHERE, _LIMIT_1)) in set(SqlGuardRejection)


def test_unparsable_sql_is_structured_not_a_raw_parse_error() -> None:
    """sqlglot 的 ParseError 携带被检 SQL 片段，绝不能原样冒泡。"""
    canary = "canary" + "-parse-5521"
    broken = _sql("SELECT SELECT FROM FROM", "`" + canary + "`", "WHERE")
    with pytest.raises(SqlGuardError) as err:
        _check(broken)
    assert err.value.rejection is SqlGuardRejection.UNPARSABLE
    assert canary not in str(err.value)
