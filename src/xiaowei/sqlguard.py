"""SQLGuard：只读 SQL 的纯函数守卫；执行原文，不按 AST 重打印。

``guard_readonly_query`` 与 ``guard_explain_query`` 产生互不兼容的封存类型。范围来自
``QueryPolicy.tables``（当前可读快照）；不查询元数据、不取连接。先按 UTF-8 限长、分词并拒绝
注释、hint、多语句及非 SELECT/WITH 开头，再用锁定的 sqlglot 检查语法闭集、函数、对象、列和
物理依赖。StarRocks 物理表须写明库；TiDB 可继承可信目标的默认库。拒绝可附当前可读快照中的
有限同名候选，不回显输入 SQL。
AST 的限定列名与星号展开只用于权限和依赖分析，不作为执行文本。审计原文不执行，仍按记录的
会话当前库解析，沿用原来的可打印性过滤。

查询在去尾分号后，只可追加顶层 LIMIT 或下调其数字，并在编辑后重过守卫；EXPLAIN 保留原 LIMIT。
查询列数、顶层重名和估算列头字节在 I/O 前受限，Adapter 再核对 StarRocks 实际返回列头。
函数名按 sqlglot 规范名（大写）与允许的别名比较。解析器输入异常映射成固定原因码；程序异常
照常传播。拒绝不含 SQL 字面量或底层异常链，除可读快照候选外不回显标识符。

拒绝边界：sqlglot 的输入错误（``SqlglotError``）与过深嵌套（``RecursionError``）是预期的输入
失败；解析器对畸形函数抛出的其他 ``Exception`` 同样映射为不可解析。拒绝总在下层 ``except``
块结束后才抛出，``__cause__`` 与 ``__context__`` 都为空，不挂带输入片段的下层异常。解析
以外的程序缺陷照常传播。原始 SQL 只交给分词器，不交给解析器：解析器用它生成诊断和
``Command`` 回退日志。
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass
from itertools import pairwise
from typing import Annotated, Final, Literal, NoReturn, TypeVar

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

_Name = Annotated[str, StringConstraints(min_length=1)]


class QueryRejectionCode(enum.StrEnum):
    INPUT_TOO_LARGE = "input_too_large"
    UNPARSABLE = "unparsable"
    MULTIPLE_STATEMENTS = "multiple_statements"
    UNSUPPORTED_SYNTAX = "unsupported_syntax"
    STAR_PROJECTION = "star_projection"
    OBJECT_NOT_ALLOWED = "object_not_allowed"
    UNQUALIFIED_TABLE = "unqualified_table"
    COLUMN_NOT_ALLOWED = "column_not_allowed"
    FUNCTION_NOT_ALLOWED = "function_not_allowed"
    AMBIGUOUS_REFERENCE = "ambiguous_reference"
    COMMENT_NOT_ALLOWED = "comment_not_allowed"
    EXECUTABLE_COMMENT = "executable_comment"
    COMMENT_SYNTAX = "comment_syntax"
    HINT_NOT_ALLOWED = "hint_not_allowed"
    INVALID_LIMIT = "invalid_limit"
    TOO_MANY_COLUMNS = "too_many_columns"
    UNSUPPORTED_LIMIT = "unsupported_limit"


_Code = QueryRejectionCode

REJECTION_MESSAGES: Final[Mapping[QueryRejectionCode, str]] = {
    _Code.INPUT_TOO_LARGE: "SQL 超过允许的长度",
    _Code.UNPARSABLE: "SQL 无法解析",
    _Code.MULTIPLE_STATEMENTS: "只允许一条语句",
    _Code.UNSUPPORTED_SYNTAX: (
        "只支持单条 SELECT 或 UNION/UNION ALL（可带非递归 WITH、子查询与窗口函数）；不支持 "
        "INTERSECT/EXCEPT、窗口框架与命名窗口、变量、注释、hint、锁、导出和表函数等语法"
    ),
    _Code.STAR_PROJECTION: "* 只能用于查询列表（含 别名.*）或 COUNT(*)",
    _Code.OBJECT_NOT_ALLOWED: "引用了不存在或当前账号不可读的表或视图",
    _Code.UNQUALIFIED_TABLE: "表名须写成 库名.表名",
    _Code.COLUMN_NOT_ALLOWED: "引用了不存在或不可读的列",
    _Code.FUNCTION_NOT_ALLOWED: "使用了未获准的函数",
    _Code.AMBIGUOUS_REFERENCE: "列引用或结果列名有歧义，请用表名限定列，或为输出列设置唯一别名",
    _Code.COMMENT_NOT_ALLOWED: "SQL 含注释，请删除注释后重试",
    _Code.EXECUTABLE_COMMENT: "SQL 含可执行注释，无法在只读边界内安全支持",
    _Code.COMMENT_SYNTAX: "注释的空白或换行写法与 TiDB 不一致，未执行",
    _Code.HINT_NOT_ALLOWED: "hint 不在允许的计划类清单中，未执行",
    _Code.INVALID_LIMIT: "LIMIT 与 OFFSET 只接受非负整数字面量",
    _Code.TOO_MANY_COLUMNS: "结果列数或列名总长超过上限，请只选择需要的列",
    _Code.UNSUPPORTED_LIMIT: "LIMIT 只接受非负整数字面量，不支持 OFFSET",
}


class QueryRejectedError(Exception):
    """查询在任何 I/O 前被拒绝；只携带固定说明与获准快照候选。"""

    def __init__(
        self,
        code: QueryRejectionCode,
        *,
        unqualified_tables: tuple[dict[str, object], ...] = (),
        missing_tables: int = 0,
        more: bool = False,
    ) -> None:
        super().__init__(REJECTION_MESSAGES[code])
        self.code = code
        self.unqualified_tables = unqualified_tables
        self.missing_tables = missing_tables
        self.more = more


class QueryPolicy(BaseModel):
    """一个查询目标的可信范围；来自结构快照与静态配置，不来自模型或用户。

    ``tables`` 是 库 → 对象 → 按序的列（星号按此顺序展开）；每个对象至少一列，同一对象的列名
    不区分大小写也不重复。StarRocks 业务查询拒绝未限定物理表；TiDB 仅按可信
    ``default_database`` 解析，不跨库猜测。审计原文另按记录的当前库，见 ``audit_references``。
    函数名统一为大写。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: _Name
    database_type: Literal["starrocks", "tidb"] = "starrocks"
    default_database: _Name | None = None
    tables: Mapping[_Name, Mapping[_Name, tuple[_Name, ...]]]
    allowed_functions: frozenset[_Name]
    max_rows: int = Field(ge=1)
    max_sql_bytes: int = Field(ge=1)
    max_result_columns: int = Field(ge=1)

    @field_validator("allowed_functions")
    @classmethod
    def _upper(cls, names: frozenset[str]) -> frozenset[str]:
        return frozenset(name.upper() for name in names)

    @model_validator(mode="after")
    def _complete(self) -> QueryPolicy:
        for objects in self.tables.values():
            for columns in objects.values():
                if not columns:
                    raise ValueError("每个对象至少需要一列")
                if len({c.casefold() for c in columns}) != len(columns):
                    raise ValueError("同一对象的列名不区分大小写也不得重复")
        return self

    def columns(self, database: str, name: str) -> tuple[str, ...] | None:
        return self.tables.get(database, {}).get(name)


# 封存标记是只在构造时传入的 InitVar：``dataclasses.replace`` 必须显式给出它，不能连同字段复制。
_SEAL: Final = object()


@dataclass(frozen=True, slots=True)
class GuardedQuery:
    """SQLGuard 的查询产物；Adapter 只执行 ``executed_sql``。

    ``referenced_objects`` 为 ``(库, 对象)``，``referenced_columns`` 为 ``(库, 对象, 列)``，列名是
    快照中的写法。``max_returned_rows`` 是最多交付的行数；超过即截断。
    """

    target_id: str
    executed_sql: str
    referenced_objects: frozenset[tuple[str, str]]
    referenced_columns: frozenset[tuple[str, str, str]]
    max_returned_rows: int
    requested_limit: int | None
    applied_limit: int
    _seal: InitVar[object] = None

    def __post_init__(self, _seal: object) -> None:
        if _seal is not _SEAL:
            raise TypeError("GuardedQuery 只能由 guard_readonly_query 构造")


@dataclass(frozen=True, slots=True)
class ExplainQuery:
    """``guard_explain_query`` 的产物；Adapter 只以固定级别 EXPLAIN ``executed_sql``。

    与 ``GuardedQuery`` 互不兼容：不能被当作查询执行，也没有返回行数。
    """

    target_id: str
    executed_sql: str
    referenced_objects: frozenset[tuple[str, str]]
    referenced_columns: frozenset[tuple[str, str, str]]
    _seal: InitVar[object] = None

    def __post_init__(self, _seal: object) -> None:
        if _seal is not _SEAL:
            raise TypeError("ExplainQuery 只能由 guard_explain_query 构造")


def guard_readonly_query(sql: str, policy: QueryPolicy) -> GuardedQuery:
    """校验一条只读查询；执行原文只可能去尾分号并编辑顶层 LIMIT。

    :raises QueryRejectedError: 任一规则不通过；只带原因码与固定说明。
    """
    root, objects, columns, tokens, header_extra = _guard(sql, policy)
    _check_result_columns(root, policy, header_extra)
    executed, requested, applied = _edit_limit(sql, tokens, root, policy)
    if executed != _execution_sql(sql, tokens, policy):
        checked, checked_objects, checked_columns, _, checked_header_extra = _guard(
            executed, policy
        )
        _check_result_columns(checked, policy, checked_header_extra)
        if (
            (checked_objects, checked_columns) != (objects, columns)
            or _functions(checked) != _functions(root)
            or _inner_limits(checked) != _inner_limits(root)
            or _root_limit(checked) != applied
        ):
            _reject(_Code.UNSUPPORTED_SYNTAX)
    return GuardedQuery(
        target_id=policy.target_id,
        executed_sql=executed,
        referenced_objects=objects,
        referenced_columns=columns,
        max_returned_rows=(
            min(requested, policy.max_rows) if requested is not None else policy.max_rows
        ),
        requested_limit=requested,
        applied_limit=applied,
        _seal=_SEAL,
    )


def guard_explain_query(sql: str, policy: QueryPolicy) -> ExplainQuery:
    """按与查询相同的范围校验原文，执行计划保留原 LIMIT。

    :raises QueryRejectedError: 任一规则不通过；只带原因码与固定说明。
    """
    _, objects, columns, tokens, _ = _guard(sql, policy)
    return ExplainQuery(
        target_id=policy.target_id,
        executed_sql=_execution_sql(sql, tokens, policy),
        referenced_objects=objects,
        referenced_columns=columns,
        _seal=_SEAL,
    )


_Objects = frozenset[tuple[str, str]]
_Columns = frozenset[tuple[str, str, str]]


def audit_references(sql: str, policy: QueryPolicy, database: str) -> tuple[_Objects, _Columns]:
    """审计原文（只展示、从不执行）引用的 ``(库, 对象)`` 与 ``(库, 对象, 列)``。

    ``database`` 是该条记录执行时的会话当前库（``''`` 为没有当前库）：未限定的对象名只在它之中
    解析，与数据库执行这条语句时相同，不按唯一匹配改到别的库。

    :raises QueryRejectedError: 不在范围内或无法可靠判定；调用方据此不列出该条原文。
    """
    root, objects, columns, _, _ = _guard(sql, policy, current_database=database)
    _normalize(root, policy)
    return objects, columns


def _guard(
    sql: str,
    policy: QueryPolicy,
    *,
    current_database: str | None = None,
) -> tuple[exp.Query, _Objects, _Columns, list[Token], int]:
    """三个公开函数共用的校验：返回完整限定后的语法树与引用的对象、列。

    ``current_database`` 来自审计原文；TiDB 业务 SQL 使用策略中的可信默认库。
    """
    if _utf8_size(sql) > policy.max_sql_bytes:
        _reject(_Code.INPUT_TOO_LARGE)
    root, tokens = _parse(sql, policy)
    _check_nodes(root, policy)
    # 无 AS 的表达式由数据库命名。sqlglot 在 qualify 时会生成短 _col_N，不能拿它
    # 低估原文表达式的列头；使用原始解析树表达式的长度，运行时仍复核数据库实返列头。
    leftmost = _leftmost(root)
    header_extra = sum(
        _expression_header_bytes(projection, policy)
        for projection in (leftmost.expressions if leftmost is not None else ())
        if not isinstance(projection, exp.Alias | exp.Column | exp.Star)
    )
    if current_database is None and policy.database_type == "tidb":
        current_database = policy.default_database
    _bind_tables(root, policy, current_database)
    # 名字推导与 sqlglot 一样按作用域递归；过深的嵌套是预期的输入失败，映射为固定原因码。
    _attempt(lambda: _bind_columns(root, policy), _Code.UNSUPPORTED_SYNTAX)
    qualified = _qualify(root, policy)
    _order_unions(qualified)
    objects, columns = _resolve_columns(qualified, policy)
    return qualified, objects, columns, tokens, header_extra


def _dialect_name(policy: QueryPolicy) -> str:
    return "mysql" if policy.database_type == "tidb" else DIALECT


def _expression_header_bytes(projection: exp.Expression, policy: QueryPolicy) -> int:
    return len(
        _attempt(lambda: projection.sql(dialect=_dialect_name(policy)), _Code.UNSUPPORTED_SYNTAX)
        .encode()
    )


def _normalize(root: exp.Query, policy: QueryPolicy) -> str:
    # sqlglot 的 StarRocks 生成器会把 DATEDIFF 改写成 DATE_DIFF('DAY', ...)，
    # 后者的返回类型和跨日边界语义不能代替前者。日期加减的数值区间也要保持 INT。
    def canonical_date(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.DateDiff):
            return exp.Anonymous(this="DATEDIFF", expressions=[node.this, node.expression])
        if isinstance(node, exp.DateAdd | exp.DateSub):
            number = int(node.expression.this)
            if node.args["unit"].name.upper() == "WEEK":
                number *= 7  # 4.1.4 原写法也可执行；常量周与七倍 DAY 实测等价。
                node.set("unit", exp.Var(this="DAY"))
            node.set("expression", exp.Literal.number(number))
        return node

    printable = root.transform(canonical_date, copy=True)
    normalized = _attempt(
        lambda: printable.sql(
            dialect=_dialect_name(policy), unsupported_level=ErrorLevel.RAISE, comments=False
        ),
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

_DATE_CALLS: Final = frozenset(
    {"DATE_ADD", "DATE_SUB", "ADDDATE", "SUBDATE", "DATE_TRUNC", "DATEDIFF"}
)
_FUNCTION_NAME_TOKENS: Final = frozenset({TokenType.VAR, TokenType.IDENTIFIER})


# 只检查词元之间的实际注释；字符串和标识符中的注释字样不参与。
_COMMENTS: Final = re.compile(r"/\*[\s\S]*?\*/|--[^\n]*|#[^\n]*")
_TIDB_PLAN_HINTS: Final = frozenset(
    {"USE_INDEX", "IGNORE_INDEX", "HASH_JOIN", "MERGE_JOIN", "INL_JOIN", "READ_FROM_STORAGE"}
)


def _tidb_comments(sql: str, tokens: list[Token]) -> None:
    start = 0
    for token in [*tokens, None]:
        end = token.start if token is not None else len(sql)
        gap = sql[start:end]
        for match in _COMMENTS.finditer(gap):
            comment = match.group()
            if comment.startswith(("/*!", "/*T!")):
                _reject(_Code.EXECUTABLE_COMMENT)
            if comment.startswith(("--", "#")):
                # TiDB 的 -- 后须空白；两种行注释均到 LF 结束。SQLGlot 也会认 --1 / CR。
                if (comment.startswith("--") and len(comment) > 2
                        and comment[2] not in " \t\r\n\v\f"):
                    _reject(_Code.COMMENT_SYNTAX)
                if end < len(sql) and "\n" not in gap[match.end():]:
                    _reject(_Code.COMMENT_SYNTAX)
        if token is not None:
            start = token.end + 1


def _tidb_hints(root: exp.Query, tokens: list[Token]) -> None:
    hints = list(root.find_all(exp.Hint))
    if len(hints) != sum(token.token_type is TokenType.HINT for token in tokens):
        _reject(_Code.HINT_NOT_ALLOWED)
    for hint in hints:
        if not isinstance(hint.parent, exp.Select) or hint.arg_key != "hint":
            _reject(_Code.HINT_NOT_ALLOWED)
        for expression in hint.expressions:
            if (
                not isinstance(expression, exp.Anonymous)
                or expression.name.upper() not in _TIDB_PLAN_HINTS
            ):
                _reject(_Code.HINT_NOT_ALLOWED)
            for node in expression.walk():
                if type(node) not in {exp.Anonymous, exp.Column, exp.Identifier, exp.Bracket}:
                    _reject(_Code.HINT_NOT_ALLOWED)
                if isinstance(node, exp.Anonymous) and node is not expression:
                    _reject(_Code.HINT_NOT_ALLOWED)
        # hint 参数是优化器对象，不是业务列/函数；原 SQL 中仍完整保留。
        hint.parent.set("hint", None)


def _parse(sql: str, policy: QueryPolicy) -> tuple[exp.Query, list[Token]]:
    dialect = Dialect.get_or_raise(_dialect_name(policy))
    tokens: list[Token] = _attempt(lambda: dialect.tokenize(sql), _Code.UNPARSABLE)
    original_tokens = tokens
    tidb = policy.database_type == "tidb"
    if tidb:
        _tidb_comments(sql, tokens)
    # 尾部注释附着在最后的分号词元上：先检查全部词元，再去掉可选的尾部分号。
    if not tidb and any(token.token_type is TokenType.HINT for token in tokens):
        _reject(_Code.UNSUPPORTED_SYNTAX)
    if not tidb and any(token.comments for token in tokens):
        hint = sql.find("/*+")
        while hint >= 0 and any(token.start <= hint <= token.end for token in tokens):
            hint = sql.find("/*+", hint + 3)
        _reject(_Code.UNSUPPORTED_SYNTAX if hint >= 0 else _Code.COMMENT_NOT_ALLOWED)
    # sqlglot 把 ``a``b`` 解成 a`b，4.1.4 却把该别名解成 ab；不让检查与执行绑定不同名字。
    if any(token.token_type is TokenType.IDENTIFIER and "`" in token.text for token in tokens):
        _reject(_Code.UNSUPPORTED_SYNTAX)
    if tokens and tokens[-1].token_type is TokenType.SEMICOLON:
        tokens = tokens[:-1]
    if not tokens:
        _reject(_Code.UNPARSABLE)
    if any(token.token_type is TokenType.SEMICOLON for token in tokens):
        _reject(_Code.MULTIPLE_STATEMENTS)
    if tokens[0].token_type not in {TokenType.SELECT, TokenType.WITH, TokenType.L_PAREN}:
        _reject(_Code.UNSUPPORTED_SYNTAX)
    # sqlglot 把 DATE_DIFF('DAY', ...) 与 DATEDIFF(...) 合成同一种节点；只开放后者。
    if any(
        token.token_type in _FUNCTION_NAME_TOKENS
        and token.text.upper() == "DATE_DIFF"
        and next_token.token_type is TokenType.L_PAREN
        for token, next_token in pairwise(tokens)
    ):
        _reject(_Code.FUNCTION_NOT_ALLOWED)
    _check_date_arity(tokens)
    parser = dialect.parser(error_level=ErrorLevel.RAISE)
    parse_error: QueryRejectionCode | None = None
    try:
        statements = parser.parse(tokens, "")
    except RecursionError:
        parse_error = _Code.UNSUPPORTED_SYNTAX
    except Exception:
        # 锁定版 sqlglot 对部分不完整函数会抛 AttributeError；解析器内部错误也不得越过输入边界。
        parse_error = _Code.UNPARSABLE
    if parse_error is not None:
        _reject(parse_error)
    if len(statements) != 1 or statements[0] is None:
        _reject(_Code.MULTIPLE_STATEMENTS)
    root = _query_root(statements[0])
    if tidb:
        _tidb_hints(root, tokens)
    return root, original_tokens if tidb else tokens


def _check_date_arity(tokens: list[Token]) -> None:
    """在 sqlglot 丢弃多余实参前，数指定日期函数最外层的实参。"""
    counts: list[int | None] = []
    for index, token in enumerate(tokens):
        if token.token_type is TokenType.L_PAREN:
            previous = tokens[index - 1] if index else None
            date_call = (
                previous is not None
                and previous.token_type in _FUNCTION_NAME_TOKENS
                and previous.text.upper() in _DATE_CALLS
            )
            counts.append(1 if date_call else None)
        elif token.token_type is TokenType.COMMA:
            if counts and counts[-1] is not None:
                counts[-1] += 1
        elif token.token_type is TokenType.R_PAREN and counts:
            count = counts.pop()
            if count is not None and count != 2:
                _reject(_Code.UNSUPPORTED_SYNTAX)


def _query_root(root: object) -> exp.Query:
    """只接受 ``SELECT`` 或 ``UNION`` 根节点；括号包住的整条语句、INTERSECT/EXCEPT 等拒绝。"""
    if type(root) is exp.Select or type(root) is exp.Union:
        return root
    _reject(_Code.UNSUPPORTED_SYNTAX)


# ---- 2. 节点闭集与输出名 -----------------------------------------------------------------------

_STRUCTURAL: Final[frozenset[type[exp.Expr]]] = frozenset(
    {
        exp.Select, exp.Union, exp.From, exp.Join, exp.Where, exp.Group, exp.Having, exp.Order,
        exp.Ordered, exp.Limit, exp.With, exp.CTE, exp.Subquery, exp.TableAlias, exp.Table,
        exp.Column, exp.Identifier, exp.Alias, exp.Literal, exp.Null, exp.Boolean, exp.Paren,
        exp.Distinct, exp.DataType, exp.DataTypeParam, exp.Interval, exp.Window,
        exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is,
        exp.In, exp.Between, exp.Like, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg,
        exp.Case, exp.Exists,
    }
)  # fmt: skip
"""不是函数调用的语法节点；``Case``/``Exists`` 虽是 sqlglot ``Func`` 子类，但属表达式语法。
窗口函数本身（``ROW_NUMBER``、``SUM`` 等）仍按函数 allowlist 检查。"""

_DATE_UNITS: Final = frozenset({"SECOND", "MINUTE", "HOUR", "DAY", "WEEK", "MONTH", "YEAR"})
_FUNCTION_ALIASES: Final[Mapping[str, frozenset[str]]] = {
    "UNIX_TO_TIME": frozenset({"FROM_UNIXTIME"}),
    "TIME_TO_STR": frozenset({"DATE_FORMAT"}),
    "TIMESTAMP_TRUNC": frozenset({"DATE_TRUNC"}),
    "STR_TO_TIME": frozenset({"STR_TO_DATE"}),
    "STR_TO_DATE": frozenset({"STR_TO_TIME"}),
}

_ALLOWED_ARGS: Final[Mapping[type[exp.Expr], frozenset[str]]] = {
    exp.Select: frozenset(
        {"expressions", "from_", "joins", "where", "group", "having", "order", "limit",
         "offset", "with_", "distinct"}
    ),
    exp.Union: frozenset({"this", "expression", "distinct", "order", "limit", "offset", "with_"}),
    exp.Window: frozenset({"this", "partition_by", "order", "over"}),
    exp.Star: frozenset(),
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
    exp.DateAdd: frozenset({"this", "expression", "unit"}),
    exp.DateSub: frozenset({"this", "expression", "unit"}),
    exp.DateDiff: frozenset({"this", "expression", "unit"}),
    exp.TimestampTrunc: frozenset({"this", "unit"}),
}  # fmt: skip
"""节点允许出现的参数位置；其余位置（``NATURAL``/``USING``、``RECURSIVE``、表 hint、分区、
时间旅行、列别名列表、``DISTINCT ON``、``CAST ... FORMAT``、``UNION BY NAME``、窗口框架与命名
窗口、``* EXCEPT`` 等）即使子节点类型合法也拒绝。"""


def _check_nodes(root: exp.Query, policy: QueryPolicy) -> None:
    for node in root.walk():  # 广度优先：父节点先于子节点，外层结构先被拒绝
        allowed_args = _ALLOWED_ARGS.get(type(node))
        if allowed_args is not None and not {k for k, v in node.args.items() if v} <= allowed_args:
            _reject(_Code.UNSUPPORTED_SYNTAX)
        if isinstance(node, exp.Star):
            if not _star_allowed(node):
                _reject(_Code.STAR_PROJECTION)
        elif isinstance(node, exp.Offset):
            if policy.database_type != "tidb":
                _reject(_Code.UNSUPPORTED_LIMIT)
            _limit_value(node, _Code.INVALID_LIMIT)
        elif isinstance(node, exp.Limit):
            _limit_value(
                node,
                _Code.INVALID_LIMIT if policy.database_type == "tidb" else _Code.UNSUPPORTED_LIMIT,
            )
        elif isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier):
                _reject(_Code.UNSUPPORTED_SYNTAX)  # 表函数、FILES()、TABLE(...) 等
            if node.args.get("catalog"):
                _reject(_Code.OBJECT_NOT_ALLOWED)
        elif isinstance(node, exp.Var):
            if not (
                isinstance(node.parent, exp.Interval | exp.Extract)
                or (
                    isinstance(
                        node.parent, exp.DateAdd | exp.DateSub | exp.DateDiff | exp.TimestampTrunc
                    )
                    and node.arg_key == "unit"
                )
            ):
                _reject(_Code.UNSUPPORTED_SYNTAX)
        elif isinstance(node, exp.DateAdd | exp.DateSub | exp.DateDiff | exp.TimestampTrunc):
            _check_date_function(node, policy)
        elif isinstance(node, exp.If) and node.arg_key == "ifs":
            pass  # CASE WHEN 分支，不是 IF() 函数
        elif type(node) in _STRUCTURAL:
            pass
        elif isinstance(node, exp.Func):
            if not _function_allowed(node, policy):
                _reject(_Code.FUNCTION_NOT_ALLOWED)
        else:
            _reject(_Code.UNSUPPORTED_SYNTAX)


def _star_allowed(star: exp.Star) -> bool:
    """``COUNT(*)``，或查询列表中的 ``*`` / ``别名.*``（按快照列序展开）。"""
    if isinstance(star.parent, exp.Count):
        return True
    holder: exp.Expr = star.parent if isinstance(star.parent, exp.Column) else star
    return isinstance(holder.parent, exp.Select) and holder.arg_key == "expressions"


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return node.name.upper()
    return node.sql_name()


def _function_allowed(node: exp.Func, policy: QueryPolicy) -> bool:
    name = _function_name(node)
    allowed = policy.allowed_functions
    if name in allowed or _FUNCTION_ALIASES.get(name, frozenset()) & allowed:
        return True
    # DATE_FORMAT 的锁版解析树自动插入此 CAST；显式嵌套调用仍须单独获准。
    return (
        isinstance(node, exp.TsOrDsToTimestamp)
        and isinstance(node.parent, exp.TimeToStr)
        and "DATE_FORMAT" in allowed
    )


def _check_date_function(
    node: exp.DateAdd | exp.DateSub | exp.DateDiff | exp.TimestampTrunc,
    policy: QueryPolicy,
) -> None:
    if not _function_allowed(node, policy):
        _reject(_Code.FUNCTION_NOT_ALLOWED)
    unit = node.args.get("unit")
    if not isinstance(unit, exp.Var) or unit.name.upper() not in _DATE_UNITS:
        _reject(_Code.UNSUPPORTED_SYNTAX)
    if isinstance(node, exp.DateDiff) and unit.name.upper() != "DAY":
        _reject(_Code.UNSUPPORTED_SYNTAX)
    if isinstance(node, exp.DateAdd | exp.DateSub):
        value = node.expression
        if not isinstance(value, exp.Literal):
            _reject(_Code.UNSUPPORTED_SYNTAX)
        digits = value.this.removeprefix("-")
        if not digits.isascii() or not digits.isdigit() or len(digits) > 10:
            _reject(_Code.UNSUPPORTED_SYNTAX)
        number = int(value.this)
        if not -(2**31) <= number <= 2**31 - 1 or (
            unit.name.upper() == "WEEK" and not -(2**31) <= number * 7 <= 2**31 - 1
        ):
            _reject(_Code.UNSUPPORTED_SYNTAX)


def _limit_value(
    limit: exp.Limit | exp.Offset, code: QueryRejectionCode = _Code.UNSUPPORTED_LIMIT
) -> int:
    literal = limit.expression
    if not isinstance(literal, exp.Literal) or literal.is_string or not literal.is_int:
        _reject(code)
    return int(literal.name)


def _named_bodies(root: exp.Query) -> list[exp.Select]:
    """对外给出列名的查询体（最外层、CTE、派生表）的最左 SELECT。"""
    bodies: list[exp.Expr] = [root]
    bodies += [cte.this for cte in root.find_all(exp.CTE)]
    bodies += [
        sub.this
        for sub in root.find_all(exp.Subquery)
        if isinstance(sub.parent, exp.From | exp.Join)
    ]
    return [select for body in bodies if (select := _leftmost(body)) is not None]


def _leftmost(body: exp.Expr) -> exp.Select | None:
    while isinstance(body, exp.Union | exp.Subquery):
        body = body.this
    return body if isinstance(body, exp.Select) else None


# ---- 3. 来源：物理表、列名写法与歧义 -----------------------------------------------------------


def _scopes(root: exp.Expr) -> list[Scope]:
    return _attempt(lambda: traverse_scope(root), _Code.UNSUPPORTED_SYNTAX)


def _sources(scope: Scope) -> list[tuple[exp.Expr, exp.Table | Scope]]:
    """本 scope 的来源；同一别名指向两个来源时 sqlglot 抛 ``OptimizeError``。"""
    return list(_named_sources(scope).values())


def _named_sources(scope: Scope) -> dict[str, tuple[exp.Expr, exp.Table | Scope]]:
    return _attempt(lambda: dict(scope.selected_sources), _Code.AMBIGUOUS_REFERENCE)


def _bind_tables(root: exp.Query, policy: QueryPolicy, current_database: str | None) -> None:
    """业务 SQL 拒绝未限定物理表；审计原文仍按记录的当前库解析。"""
    seen: set[int] = set()
    missing: set[str] = set()
    candidates: dict[str, list[str]] = {}
    for scope in _scopes(root):
        for node, source in _sources(scope):
            seen.add(id(node))
            if isinstance(source, exp.Table):
                if current_database is None and not source.text("db"):
                    name = source.name
                    found = sorted(db for db, objects in policy.tables.items() if name in objects)
                    if found:
                        candidates[name] = found
                    else:
                        missing.add(name)
                else:
                    _bind_table(source, policy, current_database)
    # 每个表节点都必须是某个 scope 的来源（物理表或 CTE 引用），不留未归属的表。
    if any(id(table) not in seen for table in root.find_all(exp.Table)):
        _reject(_Code.UNSUPPORTED_SYNTAX)
    if candidates or missing:
        chosen = sorted(candidates.items())[:5]
        raise QueryRejectedError(
            _Code.UNQUALIFIED_TABLE,
            unqualified_tables=tuple(
                {"table": name, "databases": databases[:20]} for name, databases in chosen
            ),
            missing_tables=len(missing),
            more=len(candidates) > 5
            or any(len(databases) > 20 for databases in candidates.values()),
        )


def _bind_table(table: exp.Table, policy: QueryPolicy, current_database: str | None) -> None:
    """显式库名按快照校验；审计未限定表仅按记录的会话当前库查找。"""
    name, database = table.name, table.text("db")
    if policy.database_type == "tidb":
        # TiDB 保留库名大小写但按不区分大小写查找；只规范依赖 AST，不改执行原文。
        requested = database or current_database
        matched = next(
            (db for db in policy.tables if db.casefold() == (requested or "").casefold()), None
        )
        if matched is not None:
            database = matched
            table.set("db", exp.to_identifier(matched))
    if database:
        if policy.columns(database, name) is None:
            _reject(_Code.OBJECT_NOT_ALLOWED)
        return
    found = (
        [current_database] if current_database and policy.columns(current_database, name) else []
    )
    if not found:
        _reject(_Code.OBJECT_NOT_ALLOWED)
    table.set("db", exp.to_identifier(found[0]))


def _bind_columns(root: exp.Query, policy: QueryPolicy) -> None:
    """列名改写为来源中的写法，按 StarRocks 各子句的规则区分物理列与输出别名，并拒绝歧义。

    对外给出列名的查询体（最外层、CTE、派生表）中的裸列先显式写出原名作为别名：规范化会把
    列名改写为快照写法，别名使列头与外层按名引用保持用户的写法。
    """
    aliases: dict[int, dict[str, list[exp.Alias]]] = {}
    for select in root.find_all(exp.Select):
        named = aliases.setdefault(id(select), {})
        for p in select.expressions:
            if isinstance(p, exp.Alias):
                named.setdefault(p.alias.casefold(), []).append(p)
    for select in _named_bodies(root):
        for projection in list(select.expressions):
            if isinstance(projection, exp.Column) and not isinstance(projection.this, exp.Star):
                projection.replace(exp.alias_(projection.copy(), projection.name, quoted=True))
    names = _Names(policy)
    for scope in _scopes(root):  # 内层 scope 先于外层：来源的输出名先于引用它的 scope 求得
        _check_star_sources(scope, names)
        sources = {name: source for name, (_, source) in _named_sources(scope).items()}
        shadows = aliases.get(id(scope.expression), {})
        for column in _own_columns(scope):
            if column.table:
                _bind_qualified(column, scope, names)
                continue
            if isinstance(column.this, exp.Star):
                continue
            if _clause(column, scope.expression) == "order" and (
                outputs := _outputs_named(scope.expression, column.name)
            ):
                if _bind_order_output(column, outputs):
                    if column.table:
                        _bind_qualified(column, scope, names)
                    continue
            shadowing = shadows.get(column.name.casefold())
            providers = [(n, s) for n, s in sources.items() if names.provides(s, column.name)]
            if len(providers) > 1:
                _reject(_Code.AMBIGUOUS_REFERENCE)
            if shadowing is not None:
                _bind_shadowed(column, scope, providers, names)
            elif providers:
                _rename(column, providers[0][1], names)
        names.remember(scope)


_PHYSICAL_FIRST: Final = frozenset({"expressions", "group", "having"})


def _bind_qualified(column: exp.Column, scope: Scope, names: _Names) -> None:
    owner = (
        scope.sources.get(column.table)
        if isinstance(column.this, exp.Star)  # 星号只展开本层来源
        else _lookup(scope, column.table)
    )
    if column.text("db"):
        _check_column_db(column, owner)
    if not isinstance(column.this, exp.Star) and isinstance(owner, exp.Table | Scope):
        _rename(column, owner, names)


def _outputs_named(select: exp.Expr, name: str) -> list[exp.Expr]:
    """本层 SELECT 中输出名与 ``name`` 相同（不区分大小写）的投影：显式别名，与裸列（括号不计）的
    隐式列名。"""
    if not isinstance(select, exp.Select):
        return []
    folded = name.casefold()
    return [
        p
        for p in select.expressions
        if (isinstance(p, exp.Alias) and p.alias.casefold() == folded)
        or (
            isinstance(c := _unparen(p), exp.Column)
            and _is_named_column(c)
            and c.name.casefold() == folded
        )
    ]


def _bind_order_output(column: exp.Column, outputs: list[exp.Expr]) -> bool:
    """ORDER BY 中与本层输出名同名的未限定名：保留为对该输出名的引用，交给数据库绑定。

    StarRocks 4.1.4 的排序名先在输出名中解析，再按投影、分组与窗口中出现的列映射：可能落到输出
    列，也可能落到同名物理列（``SELECT a AS x, x AS x2 ... ORDER BY x`` 按物理列 ``x``）。规范化
    只限定其余部分、不改变这些结构，所以原样保留引用即得到与原文相同的绑定；依赖由
    ``_order_output_columns`` 计入两种可能。多个同名输出时数据库报歧义，这里同样拒绝。

    显式或补写的别名：名字改写为别名的写法（``qualify`` 视为别名引用，不再解析）。未补写别名的
    裸列（标量、IN、EXISTS 子查询中）与排序名指向同一列：限定的改写为同一列，未限定的返回
    ``False`` 交给常规的来源绑定。
    """
    if len(outputs) != 1:
        _reject(_Code.AMBIGUOUS_REFERENCE)
    (output,) = outputs
    if isinstance(output, exp.Alias):
        column.set("this", exp.to_identifier(output.alias, quoted=True))
        return True
    target = _unparen(output)
    if not isinstance(target, exp.Column) or not target.table:
        return False
    for part in ("this", "table", "db"):
        if target.args.get(part) is not None:
            column.set(part, target.args[part].copy())
    return True


def _unparen(node: exp.Expr) -> exp.Expr:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _is_named_column(node: exp.Expr) -> bool:
    return isinstance(node, exp.Column) and not isinstance(node.this, exp.Star)


def _bind_shadowed(
    column: exp.Column,
    scope: Scope,
    providers: list[tuple[str, exp.Table | Scope]],
    names: _Names,
) -> None:
    """ORDER BY 以外与本层输出别名同名的未限定列，按 StarRocks 4.1.4 实测的子句规则绑定。

    - 本层有物理来源：一律是物理列（投影、GROUP BY、HAVING 也先物理列）；
    - WHERE、JOIN ON、窗口：看不到本层别名，按词法作用域向外层查找（相关子查询）；恰好一个外层
      来源时绑定，多个即歧义，都没有即列不存在；
    - 投影、GROUP BY、HAVING：本层没有时是别名（交给 ``qualify`` 展开）；外层也有同名列时数据库
      会取外层列（且多半不支持在这些位置相关引用），不猜，拒绝。

    物理列直接写上来源名，使 ``qualify`` 不再把它当作别名展开。
    """
    if not providers:
        outer = _outer_providers(scope, column.name, names)
        if _clause(column, scope.expression) in _PHYSICAL_FIRST:
            if outer:
                _reject(_Code.AMBIGUOUS_REFERENCE)
            return
        if not outer:
            _reject(_Code.COLUMN_NOT_ALLOWED)
        if len(outer) > 1:
            _reject(_Code.AMBIGUOUS_REFERENCE)
        providers = outer
        if _lookup(scope, providers[0][0]) is not providers[0][1]:
            _reject(_Code.AMBIGUOUS_REFERENCE)  # 中间层同名的来源别名会截住按名字的引用
    name, source = providers[0]
    column.set("table", exp.to_identifier(name))
    _rename(column, source, names)


def _clause(column: exp.Column, select: exp.Expr) -> str:
    """列所在的子句（``Select`` 的参数名）；位于窗口的 PARTITION BY/ORDER BY 中时为 ``window``。"""
    node: exp.Expr = column
    in_window = False
    while node.parent is not None and node.parent is not select:
        if isinstance(node.parent, exp.Window) and node.arg_key != "this":
            in_window = True
        node = node.parent
    return "window" if in_window else node.arg_key or ""


def _outer_providers(scope: Scope, name: str, names: _Names) -> list[tuple[str, exp.Table | Scope]]:
    """各外层 scope 中提供该列的来源；跨层合计，多于一个即视为歧义（保守，不按就近选择）。"""
    found: list[tuple[str, exp.Table | Scope]] = []
    outer = scope.parent
    while outer is not None:
        found += [(n, s) for n, (_, s) in _named_sources(outer).items() if names.provides(s, name)]
        outer = outer.parent
    return found


def _check_star_sources(scope: Scope, names: _Names) -> None:
    """星号展开到的 CTE/派生表若有重名输出列，展开后无法唯一引用：要求先起唯一别名。

    按展开后的真实列名检查（星号先展开为来源的列），不把未展开的 ``*`` 当作列名。
    """
    select = scope.expression
    if not isinstance(select, exp.Select):
        return
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            targets = [s for _, s in _sources(scope)]
        elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            found = scope.sources.get(projection.table)
            targets = [found] if isinstance(found, exp.Table | Scope) else []
        else:
            continue
        for source in targets:
            if isinstance(source, Scope):
                folded = [n.casefold() for n in names.of(source)]
                if len(set(folded)) != len(folded):
                    _reject(_Code.AMBIGUOUS_REFERENCE)


class _Names:
    """来源展开星号后的输出列名（UNION 取最左分支），与 ``qualify`` 的展开顺序一致。

    按 scope 缓存：``_bind_columns`` 由内向外遍历，每个 scope 处理完即记下，外层只读一层，深层
    星号 CTE 链不递归。
    """

    def __init__(self, policy: QueryPolicy) -> None:
        self._policy = policy
        self._known: dict[int, list[str]] = {}

    def remember(self, scope: Scope) -> None:
        self._known[id(scope)] = self._compute(scope)

    def of(self, source: exp.Table | Scope) -> list[str]:
        if isinstance(source, exp.Table):
            return list(_table_columns(source, self._policy))
        known = self._known.get(id(source))
        return known if known is not None else self._compute(source)

    def provides(self, source: exp.Table | Scope, name: str) -> bool:
        if isinstance(source, Scope) and not isinstance(source.expression, exp.Query):
            return False
        folded = name.casefold()
        return any(n.casefold() == folded for n in self.of(source))

    def _compute(self, scope: Scope) -> list[str]:
        while scope.union_scopes:
            scope = scope.union_scopes[0]
        select = scope.expression
        found: list[str] = []
        for projection in select.expressions if isinstance(select, exp.Select) else ():
            if isinstance(projection, exp.Star):
                for _, inner in _sources(scope):
                    found += self.of(inner)
            elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                star_source = scope.sources.get(projection.table)
                if isinstance(star_source, exp.Table | Scope):
                    found += self.of(star_source)
            else:
                found.append(projection.alias_or_name)
        return found


def _own_columns(scope: Scope) -> list[exp.Column]:
    """本 scope 自身的列（不进入子 scope）。不用 ``Scope.columns``：它省略 ORDER BY/HAVING 中与
    输出别名同名的未限定列。"""
    return [n for n in scope.walk() if isinstance(n, exp.Column)]


def _lookup(scope: Scope, name: str) -> exp.Table | Scope | None:
    """按别名找来源：先本层，再沿外层 scope（相关子查询引用外层的表）。"""
    current: Scope | None = scope
    while current is not None:
        source = current.sources.get(name)
        if isinstance(source, exp.Table | Scope):
            return source
        current = current.parent
    return None


def _check_column_db(column: exp.Column, owner: exp.Table | Scope | None) -> None:
    """``库.表.列``：表名必须是本层未起别名的物理表，且库名一致。"""
    if not isinstance(owner, exp.Table) or owner.alias or owner.text("db") != column.text("db"):
        _reject(_Code.COLUMN_NOT_ALLOWED)


def _table_columns(table: exp.Table, policy: QueryPolicy) -> tuple[str, ...]:
    columns = policy.columns(table.text("db"), table.name)
    if columns is None:  # _bind_tables 已保证物理表在范围内
        _reject(_Code.OBJECT_NOT_ALLOWED)
    return columns


def _rename(column: exp.Column, source: exp.Table | Scope, names: _Names) -> None:
    """改写为来源中的写法（物理表按快照，CTE/派生表按其输出名）；不唯一时留给后续检查。"""
    folded = column.name.casefold()
    matches = [n for n in names.of(source) if n.casefold() == folded]
    if len(matches) == 1 and matches[0] != column.name:
        column.set("this", exp.to_identifier(matches[0], quoted=True))


# ---- 4. 限定列、展开星号并复核归属 -------------------------------------------------------------


def _qualify(root: exp.Query, policy: QueryPolicy) -> exp.Query:
    # qualify 会把 ORDER BY 序号换为输出名；重名时会丢失位置。保留每个 SELECT/UNION 的原序号，
    # 在它完成列限定后恢复。不能提前复制投影：未限定列可能被同名输出别名再次解释，
    # 常量投影也可能被误作新序号。Ordered 节点及方向由锁定版本原地保留。
    # 有 GROUP BY 时，qualify 还会把与某个投影表达式相同的排序项改写为该投影的别名
    # （``SELECT id AS region ... GROUP BY id, region ORDER BY region, id`` 的 ``id`` 变成
    # ``region``），而别名在 StarRocks 的排序绑定可能指向同名物理列。先用括号隔开排序项（括号内的
    # 输出名引用与列限定不受影响），完成后去掉括号，排序项保持原文。
    ordinals: list[tuple[exp.Query, exp.Ordered, exp.Literal]] = []
    shielded: list[exp.Ordered] = []
    for query in root.find_all(exp.Select, exp.Union):
        order = query.args.get("order")
        for ordered in order.expressions if isinstance(order, exp.Order) else ():
            value = ordered.this
            if not isinstance(ordered, exp.Ordered):
                continue
            if isinstance(value, exp.Literal) and value.is_int:
                ordinals.append((query, ordered, value.copy()))
            elif query.args.get("group"):
                ordered.set("this", exp.Paren(this=value))
                shielded.append(ordered)
    schema: dict[str, object] = {}
    for table in root.find_all(exp.Table):
        database = table.text("db")
        if database:  # 物理表已由 _bind_tables 补全库名；CTE 引用没有库名
            objects = schema.setdefault(database, {})
            assert isinstance(objects, dict)  # noqa: S101 —— 只由本循环写入
            objects[table.name] = dict.fromkeys(_table_columns(table, policy), "VARCHAR")
    qualified = _attempt(
        lambda: qualify(
            root,
            schema=schema,
            dialect=_dialect_name(policy),
            validate_qualify_columns=True,
            identify=True,
        ),
        _Code.COLUMN_NOT_ALLOWED,
    )
    qualified = _query_root(qualified)
    for ordered in shielded:
        shield = ordered.this
        if not isinstance(shield, exp.Paren):
            _reject(_Code.UNSUPPORTED_SYNTAX)  # 锁定版本应原地保留括号；不符时不猜
        ordered.set("this", shield.this)
    for query, ordered, ordinal in ordinals:
        if not 1 <= int(ordinal.name) <= len(query.named_selects):
            _reject(_Code.COLUMN_NOT_ALLOWED)
        ordered.set("this", ordinal)
    if any(_star_projected(star) for star in qualified.find_all(exp.Star)):
        _reject(_Code.STAR_PROJECTION)  # 星号必须完全展开，不留给数据库
    return qualified


def _star_projected(star: exp.Star) -> bool:
    return not isinstance(star.parent, exp.Count)


def _order_unions(root: exp.Query) -> None:
    """UNION 的 ORDER BY 只能按结果列排序：输出名换为它在最左分支中的位置。"""
    for union in root.find_all(exp.Union):
        order = union.args.get("order")
        names = [name.casefold() for name in union.named_selects]
        for ordered in order.expressions if isinstance(order, exp.Order) else ():
            value = ordered.this
            if isinstance(value, exp.Literal) and value.is_int:
                continue  # 序号已在 _qualify 中核对范围
            value = _unparen(value)  # (x) 仍是名字；(1) 是常量，不是序号，不在此剥除
            if not isinstance(value, exp.Column) or value.table:
                _reject(_Code.UNSUPPORTED_SYNTAX)
            positions = [i for i, name in enumerate(names, 1) if name == value.name.casefold()]
            if not positions:
                _reject(_Code.COLUMN_NOT_ALLOWED)
            if len(positions) > 1:
                _reject(_Code.AMBIGUOUS_REFERENCE)
            ordered.set("this", exp.Literal.number(positions[0]))


def _resolve_columns(root: exp.Query, policy: QueryPolicy) -> tuple[_Objects, _Columns]:
    objects: set[tuple[str, str]] = set()
    columns: set[tuple[str, str, str]] = set()
    for scope in _scopes(root):
        for _, source in _sources(scope):
            if isinstance(source, exp.Table):
                _table_columns(source, policy)
                objects.add((source.text("db"), source.name))
        for column in _own_columns(scope):
            if not column.table:
                columns |= _order_output_columns(column, scope, policy)
                continue
            owner = _lookup(scope, column.table)
            if isinstance(owner, exp.Table):
                if column.name not in _table_columns(owner, policy):
                    _reject(_Code.COLUMN_NOT_ALLOWED)
                columns.add((owner.text("db"), owner.name, column.name))
            elif isinstance(owner, Scope):
                _check_derived(owner, column.name)
            else:
                _reject(_Code.COLUMN_NOT_ALLOWED)
    return frozenset(objects), frozenset(columns)


def _check_derived(source: Scope, name: str) -> None:
    """引用 CTE/派生表的列：该名字必须恰好出现一次（重名时数据库与 sqlglot 的选择可能不同）。"""
    query = source.expression
    names = query.named_selects if isinstance(query, exp.Query) else []
    matches = sum(n.casefold() == name.casefold() for n in names)
    if matches == 0:
        _reject(_Code.COLUMN_NOT_ALLOWED)
    if matches > 1:
        _reject(_Code.AMBIGUOUS_REFERENCE)


def _order_output_columns(
    column: exp.Column, scope: Scope, policy: QueryPolicy
) -> set[tuple[str, str, str]]:
    """``qualify`` 后仍未限定的名字只能是 SELECT 的 ORDER BY 对本层唯一输出别名的引用（原样执行）。

    数据库可能把它绑定到输出表达式（其列已计入依赖），也可能绑定到本层来源中的同名物理列：后者
    属于本层可读对象，一并计入依赖。CTE/派生表来源的同名列由其内层 scope 计入。``qualify`` 不校验
    HAVING/ORDER BY 中的未限定名，其余位置的未限定名在这里拒绝。
    """
    select = scope.expression
    outputs = [
        p
        for p in (select.expressions if isinstance(select, exp.Select) else ())
        if isinstance(p, exp.Alias) and p.alias == column.name
    ]
    if not outputs:
        _reject(_Code.COLUMN_NOT_ALLOWED)
    if len(outputs) != 1 or _clause(column, select) != "order":
        _reject(_Code.AMBIGUOUS_REFERENCE)
    folded = column.name.casefold()
    return {
        (source.text("db"), source.name, name)
        for _, source in _sources(scope)
        if isinstance(source, exp.Table)
        for name in _table_columns(source, policy)
        if name.casefold() == folded
    }


# ---- 5. LIMIT --------------------------------------------------------------------------------


def _without_terminator(sql: str) -> str:
    stripped = sql.rstrip()
    return stripped[:-1] if stripped.endswith(";") else sql


def _execution_sql(sql: str, tokens: list[Token], policy: QueryPolicy) -> str:
    if policy.database_type != "tidb":
        return _without_terminator(sql)
    if tokens and tokens[-1].token_type is TokenType.SEMICOLON:
        terminal = tokens[-1]
        return sql[:terminal.start] + sql[terminal.end + 1:]
    return sql


def _root_limit(root: exp.Query) -> int | None:
    limit = root.args.get("limit")
    return _limit_value(limit) if isinstance(limit, exp.Limit) else None


def _edit_limit(
    sql: str, tokens: list[Token], root: exp.Query, policy: QueryPolicy
) -> tuple[str, int | None, int]:
    """只定位顶层 LIMIT 的数字词元，不重新打印其余 SQL。"""
    depth = 0
    limit_token: Token | None = None
    number: Token | None = None
    for index, token in enumerate(tokens):
        if token.token_type is TokenType.L_PAREN:
            depth += 1
        elif token.token_type is TokenType.R_PAREN:
            depth -= 1
        elif depth == 0 and token.token_type is TokenType.LIMIT:
            if limit_token is not None or index + 1 == len(tokens):
                _reject(_Code.UNSUPPORTED_SYNTAX)
            limit_token, number = token, tokens[index + 1]
            if (policy.database_type == "tidb" and index + 3 < len(tokens)
                    and tokens[index + 2].token_type is TokenType.COMMA):
                number = tokens[index + 3]
    requested = _root_limit(root)
    if (limit_token is None) != (requested is None):
        _reject(_Code.UNSUPPORTED_SYNTAX)
    base = _execution_sql(sql, tokens, policy)
    applied = min(requested, policy.max_rows) if requested is not None else policy.max_rows + 1
    if requested is not None:
        if (
            number is None
            or number.token_type is not TokenType.NUMBER
            or not number.text.isascii()
            or not number.text.isdigit()
            or int(number.text) != requested
        ):
            _reject(_Code.UNSUPPORTED_SYNTAX)
        if requested > policy.max_rows:
            applied = policy.max_rows + 1
            base = base[: number.start] + str(applied) + base[number.end + 1 :]
    elif policy.database_type == "tidb":
        body_tokens = tokens[:-1] if tokens[-1].token_type is TokenType.SEMICOLON else tokens
        end = body_tokens[-1].end + 1
        base = base[:end] + f" LIMIT {applied}" + base[end:]
    else:
        base = f"{base.rstrip()} LIMIT {applied}"
    if _utf8_size(base) > policy.max_sql_bytes:
        _reject(_Code.INPUT_TOO_LARGE)
    return base, requested, applied


def _functions(root: exp.Query) -> tuple[str, ...]:
    return tuple(_function_name(node) for node in root.find_all(exp.Func))


def _inner_limits(root: exp.Query) -> tuple[int, ...]:
    outer = root.args.get("limit")
    return tuple(_limit_value(node) for node in root.find_all(exp.Limit) if node is not outer)


# ---- 6. 结果列 -------------------------------------------------------------------------------


def _check_result_columns(root: exp.Query, policy: QueryPolicy, header_extra: int) -> None:
    names = root.named_selects
    # 只交付最外层结果列。内部 CTE/派生表即使有同名列，只要外层选出的列唯一且
    # 引用解析无歧义，就不因内部名字而拒绝整条查询。
    if len({name.casefold() for name in names}) != len(names):
        _reject(_Code.AMBIGUOUS_REFERENCE)
    if len(names) > policy.max_result_columns:
        _reject(_Code.TOO_MANY_COLUMNS)
    if sum(len(name.encode()) for name in names) + header_extra > policy.max_sql_bytes:
        _reject(_Code.TOO_MANY_COLUMNS)
