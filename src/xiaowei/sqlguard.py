"""SQLGuard：把模型给出的 SQL 收窄为一条可执行的只读 ``GuardedQuery``，或给出固定原因码。

``guard_explain_query`` 用同一套校验产出 ``ExplainQuery``，供 Adapter 以固定显式级别取执行
计划；两者是互不兼容的封存类型，唯一差别是执行计划不改写 LIMIT（第 5 步）。

同步纯函数，不查询元数据、不取连接、不访问网络或凭据。只接受单条 ``SELECT``（可带非递归
``WITH``）；未列入闭集的语法、节点、函数和参数一律拒绝。顺序：

1. 按 UTF-8 字节限长；先分词，再按词元拒绝注释、hint、多语句和非 ``SELECT``/``WITH`` 开头的
   语句。非 SELECT 语句在 sqlglot 中会回退为 ``Command`` 并把原文写进日志，因此必须在解析前挡住。
2. 解析后逐节点检查闭集、函数 allowlist、投影星号、LIMIT 与各节点的参数位置。
3. 按 sqlglot scope 区分 CTE 与物理表，物理表必须在 allowlist 内；未限定列在多个来源中都
   存在时判为歧义。
4. 用 sqlglot ``qualify`` 按“只含获准列”的 schema 完整限定所有列，解析失败即列未获准；再逐
   scope 复核物理列归属、拒绝相关子查询。输出别名引用替换为别名所指表达式，执行的 SQL 中不留
   任何需要由数据库再解析的未限定名。
5. 查询：顶层 LIMIT 缺失或超过 ``max_rows`` 时改为 ``max_rows + 1``（供 Adapter 判定截断）；
   执行计划：LIMIT 保持原样，因为 EXPLAIN 不返回数据行，改写会改变被解释的计划。再生成规范化
   SQL；规范化结果也要满足长度上限，且再过一次同一函数结果不变。

标识符按 sqlglot 的 StarRocks 方言语义（大小写敏感）与 allowlist 精确比较，大小写、全角或
同形字差异只会使匹配失败。函数名按 sqlglot 规范名（大写）比较，例如 ``DATE_TRUNC`` 解析为
``TIMESTAMP_TRUNC``。拒绝只带 ``QueryRejectionCode`` 与固定说明，不含 SQL、标识符、字面量或
解析器原文。

拒绝边界：sqlglot 的输入错误（``SqlglotError``）与过深嵌套（``RecursionError``）是预期的输入
失败，映射为固定原因码；拒绝总在下层 ``except`` 块结束后才抛出，``__cause__`` 与
``__context__`` 都为空，不挂带输入片段的下层异常。其余异常是程序缺陷，照常传播。原始 SQL 只
交给分词器，不交给解析器：解析器用它生成诊断和 ``Command`` 回退日志。
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass
from typing import Annotated, Final, NoReturn, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlglot import Token, TokenType, exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

DIALECT: Final = "starrocks"
_DIALECT = Dialect.get_or_raise(DIALECT)

_Name = Annotated[str, StringConstraints(min_length=1)]


class QueryRejectionCode(enum.StrEnum):
    INPUT_TOO_LARGE = "input_too_large"
    UNPARSABLE = "unparsable"
    MULTIPLE_STATEMENTS = "multiple_statements"
    UNSUPPORTED_SYNTAX = "unsupported_syntax"
    STAR_PROJECTION = "star_projection"
    OBJECT_NOT_ALLOWED = "object_not_allowed"
    COLUMN_NOT_ALLOWED = "column_not_allowed"
    FUNCTION_NOT_ALLOWED = "function_not_allowed"
    AMBIGUOUS_REFERENCE = "ambiguous_reference"
    UNSUPPORTED_LIMIT = "unsupported_limit"


_Code = QueryRejectionCode

REJECTION_MESSAGES: Final[Mapping[QueryRejectionCode, str]] = {
    _Code.INPUT_TOO_LARGE: "SQL 超过允许的长度",
    _Code.UNPARSABLE: "SQL 无法解析",
    _Code.MULTIPLE_STATEMENTS: "只允许一条语句",
    _Code.UNSUPPORTED_SYNTAX: (
        "只支持单条 SELECT（可带非递归 WITH）；不支持集合运算、相关子查询、窗口、变量、"
        "注释、hint、锁、导出和表函数等语法"
    ),
    _Code.STAR_PROJECTION: "不允许 * 投影，请列出具体列",
    _Code.OBJECT_NOT_ALLOWED: "引用了未获准的表或视图",
    _Code.COLUMN_NOT_ALLOWED: "引用了未获准或不存在的列",
    _Code.FUNCTION_NOT_ALLOWED: "使用了未获准的函数",
    _Code.AMBIGUOUS_REFERENCE: "列引用有歧义，请用表名或别名限定，排序时可直接引用输出别名",
    _Code.UNSUPPORTED_LIMIT: "LIMIT 只接受非负整数字面量，不支持 OFFSET",
}


class QueryRejectedError(Exception):
    """查询在任何 I/O 前被拒绝；消息只有固定说明。"""

    def __init__(self, code: QueryRejectionCode) -> None:
        super().__init__(REJECTION_MESSAGES[code])
        self.code = code


class QueryPolicy(BaseModel):
    """一个查询目标的可信 allowlist；来自静态配置，不来自模型或用户。

    ``allowed_columns`` 的键必须恰好等于 ``allowed_objects``，每个对象至少一列：没有列清单
    的对象不开放。函数名统一为大写。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: _Name
    default_database: _Name
    allowed_objects: frozenset[_Name]
    allowed_columns: Mapping[str, frozenset[_Name]]
    allowed_functions: frozenset[_Name]
    max_rows: int = Field(ge=1)
    max_sql_bytes: int = Field(ge=1)

    @field_validator("allowed_functions")
    @classmethod
    def _upper(cls, names: frozenset[str]) -> frozenset[str]:
        return frozenset(name.upper() for name in names)

    @model_validator(mode="after")
    def _complete(self) -> QueryPolicy:
        if set(self.allowed_columns) != self.allowed_objects:
            raise ValueError("allowed_columns 必须恰好覆盖 allowed_objects")
        if not all(self.allowed_columns.values()):
            raise ValueError("每个获准对象至少需要一列")
        return self


# 封存标记是只在构造时传入的 InitVar：``dataclasses.replace`` 必须显式给出它，不能连同字段复制。
_SEAL: Final = object()


@dataclass(frozen=True, slots=True)
class GuardedQuery:
    """SQLGuard 的唯一产物；Adapter 只执行 ``normalized_sql``。

    ``referenced_objects`` 为默认 database 中的对象名，``referenced_columns`` 为
    ``(对象, 列)``。``max_returned_rows`` 是最多交付的行数；超过即截断。
    """

    target_id: str
    normalized_sql: str
    referenced_objects: frozenset[str]
    referenced_columns: frozenset[tuple[str, str]]
    max_returned_rows: int
    _seal: InitVar[object] = None

    def __post_init__(self, _seal: object) -> None:
        if _seal is not _SEAL:
            raise TypeError("GuardedQuery 只能由 guard_readonly_query 构造")


@dataclass(frozen=True, slots=True)
class ExplainQuery:
    """``guard_explain_query`` 的唯一产物；Adapter 只以固定显式级别 EXPLAIN ``normalized_sql``。

    与 ``GuardedQuery`` 互不兼容：不能被当作查询执行，也没有返回行数。
    """

    target_id: str
    normalized_sql: str
    referenced_objects: frozenset[str]
    referenced_columns: frozenset[tuple[str, str]]
    _seal: InitVar[object] = None

    def __post_init__(self, _seal: object) -> None:
        if _seal is not _SEAL:
            raise TypeError("ExplainQuery 只能由 guard_explain_query 构造")


def guard_readonly_query(sql: str, policy: QueryPolicy) -> GuardedQuery:
    """校验并规范化一条只读查询。

    :raises QueryRejectedError: 任一规则不通过；只带原因码与固定说明。
    """
    root, objects, columns = _guard(sql, policy)
    returned = _cap_limit(root, policy.max_rows)
    return GuardedQuery(
        target_id=policy.target_id,
        normalized_sql=_normalize(root, policy),
        referenced_objects=objects,
        referenced_columns=columns,
        max_returned_rows=returned,
        _seal=_SEAL,
    )


def guard_explain_query(sql: str, policy: QueryPolicy) -> ExplainQuery:
    """按与查询相同的范围校验并规范化一条待取执行计划的 SQL；LIMIT 保持原样。

    :raises QueryRejectedError: 任一规则不通过；只带原因码与固定说明。
    """
    root, objects, columns = _guard(sql, policy)
    return ExplainQuery(
        target_id=policy.target_id,
        normalized_sql=_normalize(root, policy),
        referenced_objects=objects,
        referenced_columns=columns,
        _seal=_SEAL,
    )


def _guard(
    sql: str, policy: QueryPolicy
) -> tuple[exp.Select, frozenset[str], frozenset[tuple[str, str]]]:
    """两个公开函数共用的校验：返回完整限定后的语法树与引用的对象、列。"""
    if _utf8_size(sql) > policy.max_sql_bytes:
        _reject(_Code.INPUT_TOO_LARGE)
    root = _parse(sql)
    _check_nodes(root, policy)
    _check_sources(root, policy)
    qualified = _qualify(root, policy)
    objects, columns = _resolve_columns(qualified, policy)
    return qualified, objects, columns


def _normalize(root: exp.Select, policy: QueryPolicy) -> str:
    normalized = _attempt(
        lambda: root.sql(dialect=DIALECT, unsupported_level=ErrorLevel.RAISE, comments=False),
        _Code.UNSUPPORTED_SYNTAX,
    )
    if _utf8_size(normalized) > policy.max_sql_bytes:
        _reject(_Code.INPUT_TOO_LARGE)
    return normalized


def _reject(code: QueryRejectionCode) -> NoReturn:
    """不得在 ``except`` 块内调用：那样下层异常会成为 ``__context__``。"""
    raise QueryRejectedError(code)


_T = TypeVar("_T")


def _attempt(call: Callable[[], _T], code: QueryRejectionCode) -> _T:
    """执行一步 sqlglot 处理；预期的输入错误在离开 ``except`` 后映射为 ``code``。"""
    try:
        return call()
    except SqlglotError:
        pass
    except RecursionError:
        code = _Code.UNSUPPORTED_SYNTAX
    _reject(code)


def _utf8_size(sql: str) -> int:
    try:
        return len(sql.encode())
    except UnicodeEncodeError:  # 孤立代理项：JSON 可表达，UTF-8 不可编码
        pass
    _reject(_Code.UNPARSABLE)


# ---- 1. 词元与解析 ---------------------------------------------------------------------------


def _parse(sql: str) -> exp.Select:
    tokens: list[Token] = _attempt(lambda: _DIALECT.tokenize(sql), _Code.UNPARSABLE)
    # 尾部注释附着在最后的分号词元上：先检查全部词元，再去掉可选的尾部分号。
    if any(token.comments or token.token_type is TokenType.HINT for token in tokens):
        _reject(_Code.UNSUPPORTED_SYNTAX)
    if tokens and tokens[-1].token_type is TokenType.SEMICOLON:
        tokens = tokens[:-1]
    if not tokens:
        _reject(_Code.UNPARSABLE)
    if any(token.token_type is TokenType.SEMICOLON for token in tokens):
        _reject(_Code.MULTIPLE_STATEMENTS)
    if tokens[0].token_type not in {TokenType.SELECT, TokenType.WITH}:
        _reject(_Code.UNSUPPORTED_SYNTAX)
    parser = _DIALECT.parser(error_level=ErrorLevel.RAISE)
    statements = _attempt(lambda: parser.parse(tokens, ""), _Code.UNPARSABLE)
    if len(statements) != 1 or statements[0] is None:
        _reject(_Code.MULTIPLE_STATEMENTS)
    root = statements[0]
    if not isinstance(root, exp.Select):
        _reject(_Code.UNSUPPORTED_SYNTAX)
    return root


# ---- 2. 节点闭集 -----------------------------------------------------------------------------

_STRUCTURAL: Final[frozenset[type[exp.Expr]]] = frozenset(
    {
        exp.Select, exp.From, exp.Join, exp.Where, exp.Group, exp.Having, exp.Order,
        exp.Ordered, exp.Limit, exp.With, exp.CTE, exp.Subquery, exp.TableAlias, exp.Table,
        exp.Column, exp.Identifier, exp.Alias, exp.Literal, exp.Null, exp.Boolean, exp.Paren,
        exp.Distinct, exp.DataType, exp.DataTypeParam, exp.Interval,
        exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is,
        exp.In, exp.Between, exp.Like, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg,
        exp.Case, exp.Exists,
    }
)  # fmt: skip
"""不是函数调用的语法节点；``Case``/``Exists`` 虽是 sqlglot ``Func`` 子类，但属表达式语法。"""

_ALLOWED_ARGS: Final[Mapping[type[exp.Expr], frozenset[str]]] = {
    exp.Select: frozenset(
        {"expressions", "from_", "joins", "where", "group", "having", "order", "limit",
         "offset", "with_", "distinct"}
    ),
    exp.Table: frozenset({"this", "db", "catalog", "alias"}),
    exp.Join: frozenset({"this", "on", "side", "kind"}),
    exp.With: frozenset({"expressions"}),
    exp.CTE: frozenset({"this", "alias"}),
    exp.TableAlias: frozenset({"this"}),
    exp.Subquery: frozenset({"this", "alias"}),
    exp.Column: frozenset({"this", "table", "db"}),
    exp.Limit: frozenset({"expression"}),
    exp.Distinct: frozenset({"expressions"}),
    exp.Cast: frozenset({"this", "to"}),
}  # fmt: skip
"""节点允许出现的参数位置；其余位置（``NATURAL``/``USING``、``RECURSIVE``、表 hint、分区、
时间旅行、列别名列表、``DISTINCT ON``、``CAST ... FORMAT`` 等）即使子节点类型合法也拒绝。"""


def _check_nodes(root: exp.Select, policy: QueryPolicy) -> None:
    for node in root.walk():  # 广度优先：父节点先于子节点，窗口等外层结构先被拒绝
        allowed_args = _ALLOWED_ARGS.get(type(node))
        if allowed_args is not None and not {k for k, v in node.args.items() if v} <= allowed_args:
            _reject(_Code.UNSUPPORTED_SYNTAX)
        if isinstance(node, exp.Star):
            if not isinstance(node.parent, exp.Count):
                _reject(_Code.STAR_PROJECTION)
        elif isinstance(node, exp.Offset):
            _reject(_Code.UNSUPPORTED_LIMIT)
        elif isinstance(node, exp.Limit):
            _limit_value(node)
        elif isinstance(node, exp.Table):
            _check_table_node(node, policy)
        elif isinstance(node, exp.Column):
            if node.args.get("db") and node.text("db") != policy.default_database:
                _reject(_Code.UNSUPPORTED_SYNTAX)
        elif isinstance(node, exp.Var):
            if not isinstance(node.parent, exp.Interval | exp.Extract):
                _reject(_Code.UNSUPPORTED_SYNTAX)
        elif isinstance(node, exp.If) and node.arg_key == "ifs":
            pass  # CASE WHEN 分支，不是 IF() 函数
        elif type(node) in _STRUCTURAL:
            pass
        elif isinstance(node, exp.Func):
            if _function_name(node) not in policy.allowed_functions:
                _reject(_Code.FUNCTION_NOT_ALLOWED)
        else:
            _reject(_Code.UNSUPPORTED_SYNTAX)


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return node.name.upper()
    return node.sql_name()


def _check_table_node(table: exp.Table, policy: QueryPolicy) -> None:
    if not isinstance(table.this, exp.Identifier):
        _reject(_Code.UNSUPPORTED_SYNTAX)  # 表函数、FILES()、TABLE(...) 等
    if table.args.get("catalog") or table.text("db") not in {"", policy.default_database}:
        _reject(_Code.OBJECT_NOT_ALLOWED)


def _limit_value(limit: exp.Limit) -> int:
    literal = limit.expression
    if not isinstance(literal, exp.Literal) or literal.is_string or not literal.is_int:
        _reject(_Code.UNSUPPORTED_LIMIT)
    return int(literal.name)


# ---- 3. 来源：物理表与歧义 -------------------------------------------------------------------


def _scopes(root: exp.Expr) -> list[Scope]:
    return _attempt(lambda: traverse_scope(root), _Code.UNSUPPORTED_SYNTAX)


def _sources(scope: Scope) -> list[tuple[exp.Expr, exp.Table | Scope]]:
    """本 scope 的来源；同一别名指向两个来源时 sqlglot 抛 ``OptimizeError``。"""
    return _attempt(lambda: list(scope.selected_sources.values()), _Code.AMBIGUOUS_REFERENCE)


def _check_sources(root: exp.Select, policy: QueryPolicy) -> None:
    scopes = _scopes(root)
    seen: set[int] = set()
    for scope in scopes:
        for node, source in _sources(scope):
            seen.add(id(node))
            if isinstance(source, exp.Table) and source.name not in policy.allowed_objects:
                _reject(_Code.OBJECT_NOT_ALLOWED)
    # 每个表节点都必须是某个 scope 的来源（物理表或 CTE 引用），不留未归属的表。
    if any(id(table) not in seen for table in root.find_all(exp.Table)):
        _reject(_Code.UNSUPPORTED_SYNTAX)
    for scope in scopes:
        for column in scope.unqualified_columns:
            providers = sum(_provides(source, column.name, policy) for _, source in _sources(scope))
            if providers > 1:
                _reject(_Code.AMBIGUOUS_REFERENCE)


def _provides(source: exp.Table | Scope, name: str, policy: QueryPolicy) -> bool:
    if isinstance(source, exp.Table):
        return name in policy.allowed_columns[source.name]
    query = source.expression
    return isinstance(query, exp.Query) and name in query.named_selects


# ---- 4. 限定列并复核归属 ---------------------------------------------------------------------


def _qualify(root: exp.Select, policy: QueryPolicy) -> exp.Select:
    schema: dict[str, object] = {
        policy.default_database: {
            name: dict.fromkeys(sorted(columns), "VARCHAR")
            for name, columns in policy.allowed_columns.items()
        }
    }
    qualified = _attempt(
        lambda: qualify(
            root,
            schema=schema,
            db=policy.default_database,
            dialect=DIALECT,
            validate_qualify_columns=True,
            identify=True,
        ),
        _Code.COLUMN_NOT_ALLOWED,
    )
    if not isinstance(qualified, exp.Select):
        _reject(_Code.UNSUPPORTED_SYNTAX)
    return qualified


def _resolve_columns(
    root: exp.Select, policy: QueryPolicy
) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    objects: set[str] = set()
    columns: set[tuple[str, str]] = set()
    for scope in _scopes(root):
        if scope.external_columns:
            _reject(_Code.UNSUPPORTED_SYNTAX)  # 相关子查询
        sources = _sources(scope)
        for _, source in sources:
            if isinstance(source, exp.Table):
                _check_physical(source, policy)
                objects.add(source.name)
        # 不用 ``Scope.columns``：它省略 ORDER BY/HAVING 中与输出别名同名的未限定列。
        for column in [n for n in scope.walk() if isinstance(n, exp.Column)]:
            if not column.table:
                _inline_alias_reference(column, scope)
                continue
            owner = scope.sources.get(column.table)
            if isinstance(owner, exp.Table):
                if column.name not in policy.allowed_columns[owner.name]:
                    _reject(_Code.COLUMN_NOT_ALLOWED)
                columns.add((owner.name, column.name))
            elif not isinstance(owner, Scope):
                _reject(_Code.COLUMN_NOT_ALLOWED)
    return frozenset(objects), frozenset(columns)


def _check_physical(table: exp.Table, policy: QueryPolicy) -> None:
    if (
        table.args.get("catalog")
        or table.text("db") != policy.default_database
        or table.name not in policy.allowed_objects
    ):
        _reject(_Code.OBJECT_NOT_ALLOWED)


def _inline_alias_reference(column: exp.Column, scope: Scope) -> None:
    """``ORDER BY`` 中的输出别名替换为别名所指表达式。

    数据库对未限定名的解析顺序因子句而异（WHERE/GROUP BY 可能优先匹配同名物理列），
    因此执行的 SQL 中不保留任何未限定列；ORDER BY 以外的未限定名直接拒绝。
    """
    select = scope.expression
    aliased = {
        p.alias: p.this
        for p in (select.expressions if isinstance(select, exp.Select) else ())
        if isinstance(p, exp.Alias)
    }
    if column.name not in aliased:
        _reject(_Code.COLUMN_NOT_ALLOWED)  # qualify 不校验 HAVING/ORDER BY 中的未限定名
    order = column.find_ancestor(exp.Order, exp.Select)
    if order is None or order.parent is not select:
        _reject(_Code.AMBIGUOUS_REFERENCE)
    column.replace(aliased[column.name].copy())


# ---- 5. LIMIT --------------------------------------------------------------------------------


def _cap_limit(root: exp.Select, max_rows: int) -> int:
    limit = root.args.get("limit")
    requested = _limit_value(limit) if isinstance(limit, exp.Limit) else None
    if requested is not None and requested <= max_rows:
        return requested
    root.limit(max_rows + 1, copy=False)
    return max_rows
