"""StarRocks 受治理工具：五个（配置审计源时六个）工具的契约、参数与投影策略，以及绑定 Adapter
的执行函数。

数据范围来自每个目标的结构快照（``starrocks_schema.SchemaCache``），不来自配置的表列清单。
所有工具的前置检查都先取当前快照：没有快照或已到期时在任何 I/O 前拒绝。

``local/list_tables`` 与 ``local/describe_table`` 的结构取自快照，交付前对要列出的每个对象
重新做零行权限探测（快照中的“可读”只是采集时的结论）：搜表跳过已不可读的对象，表结构遇到
已不可读的对象整体失败。搜表按非空关键词（不区分大小写匹配“库名.表名”、表注释与列名）与可空
的库过滤在快照中匹配，按库表名排序；表结构按快照中的列序。两者都分次交付：前置检查从游标位置起
按 ``page_size``（表结构不限个数）与结果的单值、总字节上限在 I/O 前规划本次的对象或列，结果带
``next_cursor``，从第一个未交付的位置继续，没有剩余时为 ``null``（此时才不标记截断）。搜表只
探测本页对象，已撤权的不列出但已越过，不重复也不遗漏。游标绑定快照版本与参数（搜表为关键词、库
过滤与页大小，表结构为库与表），快照刷新或参数改变后在 I/O 前拒绝；单个对象或单列就放不下一个
结果时同样在 I/O 前拒绝，不让模型反复缩小页。
``local/describe_table_layout`` 同样先探测，再读该表的布局；两个 ``describe`` 工具按
``database`` 与 ``table`` 定位快照中的对象。``local/run_readonly_query`` 只执行 SQLGuard
产生的 ``GuardedQuery``，``local/explain_query`` 只以 Adapter 的固定显式级别 EXPLAIN
``ExplainQuery``，不执行被解释的查询；二者的 SQLGuard 范围是快照中默认库的可读对象与列，
执行时由数据库自己的权限检查最终把关。对象核对与 SQLGuard 都是 ``Prechecked`` 的同步前置
检查：在当前授权之后、预算预留与任何 StarRocks I/O 之前运行，拒绝时模型只收到固定原因码与
说明，可在本轮剩余轮次内修正。

``local/list_slow_queries`` 只在目标配置了审计源时登记：读已有审计表中所有库（或可选的一个会话
当前库）、原文通过当前快照 SQLGuard 范围的慢查询（见 ``StarRocksAdapter.slow_queries``）；原文中
未限定的对象名按该条记录自身的会话当前库解析。时间窗与排序方式在 I/O 前检查。交付前再对各行
原文引用的对象做一次零行探测，引用了已不可读对象的行不列出。

诊断用途能看到元数据工具（含表布局）与 ``explain_query``，看不到 ``run_readonly_query``。计划证据的
固定说明（``PLAN_NOTE``，审计为 ``AUDIT_NOTE``）登记在策略上，由交付时的代码附加在事实区，
不来自证据记录或模型。

四种用途投影的字段相同且都必须完整保留（``required``）：模型、Session、Web 与飞书得到同一组
获准列与有限行，只有容量不同。装配时按声明上限构造序列化后最大的合成结果，直接交给证据的
真实投影器检查 Web 与飞书两条路径，任一路径放不下即拒绝启动；运行时 ``EvidenceStore`` 仍会
在必需字段放不下时中止本轮。

全部策略共用 ``data_scope_digest``：证据只在装配时的配置范围（摘要格式版本、数据库类型、连接身份、
函数、SQL/结果/计划上限、服务端时间与内存限额、客户端期限、时区、快照容量与审计源）下可读，改动
这些配置后已保存的 StarRocks 证据不再进入模型、Session 或渠道。结构快照本身不进入摘要。

每个观测都带可信的对象依赖（``starrocks_schema.read_dependency`` / ``listed_dependency``）：列表的
每个对象、表结构与布局的对象（全部列）、查询与计划引用的对象与列（来自 SQLGuard 产物）、审计源
与各行原文引用的对象与列。``EvidenceStore`` 在记录与每次交付时按当前权限与对象版本复核这些依赖
（``starrocks_schema.DependencyCheck``）。

多个目标各自装配一次：契约、策略（``starrocks.<目标>.<工具>``）与执行函数都按目标登记，执行函数
以 ``(tool_id, target_id)`` 为键。每个参数模型都以必填的 ``cluster`` 选择目标，对模型只有一套
同名工具（见 ``tools.routed_function_tool``）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, TypeGuard

from pydantic import BaseModel, ConfigDict, StringConstraints

from xiaowei.evidence import check_projection_capacity
from xiaowei.governance import Execute, Prechecked, Projection, ToolPolicy, ToolRejectedError
from xiaowei.models import (
    AUDIENCES,
    Audience,
    ObjectDependency,
    ToolContract,
    ToolObservation,
    ToolRequest,
)
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
from xiaowei.starrocks import (
    AUDIT_ORDER_COLUMNS,
    EXPLAIN_PREFIX,
    SCHEMA_COLUMNS_SQL,
    SCHEMA_OBJECTS_SQL,
    SQL_MODE,
    QueryResult,
    Scalar,
    StarRocksAdapter,
    StarRocksError,
    StarRocksErrorCode,
    StarRocksTarget,
    audit_sql_bytes,
    fitting_rows,
    metadata_sql_bytes,
)
from xiaowei.starrocks_schema import (
    SCHEMA_UNAVAILABLE,
    ObjectInfo,
    SchemaCache,
    SchemaSnapshot,
    SchemaUnavailableError,
    listed_dependency,
    read_dependency,
)

LIST_TABLES: Final = "local/list_tables"
DESCRIBE_TABLE: Final = "local/describe_table"
RUN_QUERY: Final = "local/run_readonly_query"
EXPLAIN_QUERY: Final = "local/explain_query"
LAYOUT_TOOL: Final = "local/describe_table_layout"
DIAGNOSE_TOOLS: Final = frozenset({LIST_TABLES, DESCRIBE_TABLE, LAYOUT_TOOL, EXPLAIN_QUERY})
"""诊断用途展示元数据与执行计划工具；实际查询工具不可见，强行调用仍由治理层拒绝。"""
QUERY_TOOLS: Final = DIAGNOSE_TOOLS | {RUN_QUERY}
SLOW_QUERIES: Final = "local/list_slow_queries"
AUDIT_TOOLS: Final = frozenset({SLOW_QUERIES})
"""只在配置了审计源时登记；查询与诊断用途都可见（读审计不执行被诊断的 SQL）。"""
AUDIT_NOTE: Final = (
    "审计记录由 AuditLoader 批量导入，最近约一个导入周期内的查询可能尚未出现；SQL 中未写库名的表"
    "按 database 列（语句执行时的当前库，空值为没有当前库）解析。只显示能确认引用对象全部获准的"
    "查询，且每次只检查有限条候选记录：列表可能少于上限，为空也不代表没有慢查询；标记截断时列表"
    "不完整，可能还有未检查或未列出的记录。指标为 StarRocks 实测值，空值表示未知。"
)
SCHEMA_NOTE: Final = (
    "表结构来自定期采集的元数据快照（采集时间见来源），只含只读账号可 SELECT 的对象；"
    "列出的对象已在交付前逐个确认当前仍可读。"
)
SNAPSHOT_STALE: Final = "表结构已刷新，旧的 cursor 已失效，未执行；请从头开始（cursor 传 null）"
CURSOR_INVALID: Final = (
    "cursor 无效，未执行；请原样传回上一次结果中的 next_cursor，或传 null 从头开始"
)
CURSOR_MISMATCH: Final = (
    "cursor 与本次参数不符，未执行：续取须保持与开始时相同的参数（搜表为关键词、database 与 "
    "page_size，表结构为库与表）；换参数请从头开始（cursor 传 null）"
)
LIST_COLUMNS: Final = ("database", "name", "type", "comment")
DESCRIBE_COLUMNS: Final = ("name", "type", "nullable", "comment")
PLAN_NOTE: Final = (
    "执行计划是优化器按当前统计信息给出的估算；获取时未执行原查询，不包含实际耗时与资源消耗。"
    "没有对应审计记录时，不能确认实际运行慢的原因。"
)

RESULT_FIELDS: Final = ("sql", "columns", "rows", "row_count", "elapsed_ms")
PAGED_FIELDS: Final = (*RESULT_FIELDS, "next_cursor")
"""搜表与表结构另带续取游标：还有未交付的对象或列时非空（见 ``_cursor``）。"""
# JSON 转义膨胀最大的单字节字符：控制字符写作 \u00XX，一个字节变六个。
_WIDEST_CHAR: Final = "\x01"
# 有符号 64 位整数中十进制写法最长的值。
_WIDEST_INT: Final = -(2**63)


# 交给模型的 schema 只限长度：受测模型端点是否接受正则 ``pattern`` 未经验证；集群 ID 的格式由配置
# 校验，未登记的取值在路由时于 I/O 前拒绝。
ClusterArg = Annotated[str, StringConstraints(min_length=1, max_length=32)]


CursorArg = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class ListTablesArgs(BaseModel):
    """按关键词搜表：参数全部必填，``database`` 与 ``cursor`` 不需要时显式写 ``null``。

    页大小的范围随目标配置，在前置检查中复核（同 ``window_minutes``）。
    """

    model_config = ConfigDict(extra="forbid")

    cluster: ClusterArg
    keyword: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    database: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None
    page_size: int
    cursor: CursorArg | None


class TableArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster: ClusterArg
    database: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    table: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class DescribeTableArgs(TableArgs):
    """表结构：列多时分次取得，``cursor`` 第一次传 ``null``，之后传上一次的 ``next_cursor``。"""

    cursor: CursorArg | None


class RunQueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster: ClusterArg
    sql: Annotated[str, StringConstraints(min_length=1)]


class ListSlowQueriesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster: ClusterArg
    window_minutes: int
    order_by: Literal["query_time", "scan_bytes", "scan_rows", "cpu", "memory", "pending"]
    database: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None


class ExplainQueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster: ClusterArg
    sql: Annotated[str, StringConstraints(min_length=1)]


@dataclass(frozen=True)
class StarRocksTools:
    """装配结果：交给 ``ToolCatalog`` 的契约与策略，交给 ``Application`` 的执行函数。"""

    contracts: tuple[ToolContract, ...]
    policies: tuple[ToolPolicy, ...]
    executes: Mapping[tuple[str, str], Execute]

    def __add__(self, other: StarRocksTools) -> StarRocksTools:
        """合并另一目标的装配结果；同一目标重复装配时拒绝。"""
        if {t for _, t in self.executes} & {t for _, t in other.executes}:
            raise ValueError("StarRocks 工具：同一目标重复装配")
        return StarRocksTools(
            contracts=self.contracts + other.contracts,
            policies=self.policies + other.policies,
            executes={**self.executes, **other.executes},
        )


def starrocks_tools(
    adapter: StarRocksAdapter, max_bytes: Mapping[Audience, int], *, schema: SchemaCache
) -> StarRocksTools:
    """按目标配置与四种用途的容量装配五个工具；容量容不下最坏结果时拒绝装配。

    ``schema`` 是同一目标的结构快照缓存；刷新由装配方负责，这里只读取当前快照。
    """
    target = adapter.target
    if set(max_bytes) != set(AUDIENCES):
        raise ValueError("StarRocks 工具：必须且只能给出四种用途的容量")
    if schema.target_id != target.target_id:
        raise ValueError("StarRocks 工具：结构快照必须属于同一目标")
    worst = worst_case_observation(adapter)
    scope = data_scope_digest(target)

    def policy(
        name: str,
        arguments: type[BaseModel],
        fact_note: str | None = None,
        fields: tuple[str, ...] = RESULT_FIELDS,
    ) -> ToolPolicy:
        return ToolPolicy(
            policy_id=f"starrocks.{target.target_id}.{name}",
            arguments=arguments,
            projections={a: Projection(fields=fields, max_bytes=max_bytes[a]) for a in AUDIENCES},
            required=fields,
            data_scope=scope,
            fact_note=fact_note,
        )

    def contract(tool_id: str, tool_policy: ToolPolicy, description: str) -> ToolContract:
        return ToolContract(
            tool_id=tool_id,
            target_id=target.target_id,
            input_schema=tool_policy.arguments.model_json_schema(),
            policy_id=tool_policy.policy_id,
            description=description,
        )

    list_policy = policy("list_tables", ListTablesArgs, SCHEMA_NOTE, PAGED_FIELDS)
    describe_policy = policy("describe_table", DescribeTableArgs, SCHEMA_NOTE, PAGED_FIELDS)
    layout_policy = policy("describe_table_layout", TableArgs)
    query_policy = policy("run_readonly_query", RunQueryArgs)
    explain_policy = policy("explain_query", ExplainQueryArgs, PLAN_NOTE)
    # 其余策略的投影与查询相同，检查一次即覆盖；搜表与表结构多一个续取游标，按它们的实际形态
    # 另查：SQL 与列头是固定的元数据模板，行与查询同受 ``max_result_bytes`` 约束，游标按最长的
    # 偏移（不超过快照的对象或列上限）构造。
    widest = _cursor("f" * _VERSION_CHARS, (), _max_offset(target))
    try:
        check_projection_capacity(query_policy, worst)
        for paged, sql, columns in (
            (list_policy, SCHEMA_OBJECTS_SQL, LIST_COLUMNS),
            (describe_policy, SCHEMA_COLUMNS_SQL, DESCRIBE_COLUMNS),
        ):
            payload = {**worst.payload, "sql": sql, "columns": list(columns), "next_cursor": widest}
            check_projection_capacity(paged, worst.model_copy(update={"payload": payload}))
    except ValueError as exc:
        raise ValueError(f"StarRocks 工具：{exc}") from None
    bounds = _Bounds(
        sql=_json_size(worst.payload["sql"]), columns=_json_size(worst.payload["columns"])
    )

    def current() -> SchemaSnapshot:
        try:
            return schema.current()
        except SchemaUnavailableError:
            pass
        raise ToolRejectedError(SCHEMA_UNAVAILABLE)

    page_rows = target.policy.max_rows

    def check_search(request: ToolRequest) -> _Page:
        snapshot = current()
        args = request.arguments
        keyword = str(args["keyword"]).strip()
        if not keyword:
            raise ToolRejectedError("搜表未执行：关键词不能为空")
        size = args["page_size"]
        if not _counting(size) or not 1 <= size <= page_rows:
            raise ToolRejectedError(f"搜表未执行：page_size 须在 1 到 {page_rows} 之间")
        database = None if args["database"] is None else str(args["database"])
        needle = keyword.casefold()
        scope = ("list_tables", needle, database, size)
        start = _resume(args["cursor"], snapshot, scope)
        matches = [
            key
            for key, obj in sorted(snapshot.objects.items())
            if database in (None, key[0]) and _matches(obj, needle)
        ]
        if start and start >= len(matches):
            raise ToolRejectedError(CURSOR_INVALID)  # 同快照同参数的游标不会越界
        # 页在 I/O 前按快照中的行规划：撤权只会让本页变短，规划出的页一定放得下。
        candidates = matches[start : start + size]
        count = fitting_rows([_listed(snapshot.objects[k]) for k in candidates], size, target)
        if candidates and count == 0:
            raise ToolRejectedError(
                "搜表未执行：下一个匹配对象的一行（含表注释）超过单值或结果字节上限，无法列出；"
                "请用 database 过滤或更具体的关键词避开它"
            )
        end = start + count
        following = _cursor(snapshot.version, scope, end) if end < len(matches) else None
        return _Page(snapshot, tuple(candidates[:count]), following)

    async def list_tables(page: _Page) -> ToolObservation:
        started = time.monotonic()
        snapshot = page.snapshot
        # 只探测本页对象；已不可读的不列出，但已越过，续页从本页之后开始，不重复也不遗漏。
        verdicts = await adapter.probe(page.keys) if page.keys else {}
        shown = [key for key in page.keys if verdicts[key]]
        rows = tuple(_listed(snapshot.objects[key]) for key in shown)
        result = _from_snapshot(
            snapshot, SCHEMA_OBJECTS_SQL, LIST_COLUMNS, rows, page.following is not None, started
        )
        return _observation(
            result,
            bounds,
            [listed_dependency(*key) for key in shown],
            extra={"next_cursor": page.following},
        )

    def check_table(request: ToolRequest) -> tuple[SchemaSnapshot, ObjectInfo]:
        snapshot = current()
        found = snapshot.object(str(request.arguments["database"]), str(request.arguments["table"]))
        if found is None:
            raise ToolRejectedError(
                "对象不在可读的表结构中，未执行；请先用 list_tables 查看可用的库与表"
            )
        return snapshot, found

    def check_describe(request: ToolRequest) -> _Columns:
        snapshot, found = check_table(request)
        scope = ("describe_table", found.database, found.name)
        start = _resume(request.arguments["cursor"], snapshot, scope)
        if start and start >= len(found.columns):
            raise ToolRejectedError(CURSOR_INVALID)
        rows: list[dict[str, Scalar]] = [
            {"name": c.name, "type": c.type, "nullable": c.nullable, "comment": c.comment}
            for c in found.columns[start:]
        ]
        count = fitting_rows(rows, len(rows), target)
        if rows and count == 0:
            raise ToolRejectedError(
                "表结构未获取：下一列的定义（含注释）超过单值或结果字节上限，无法显示"
            )
        end = start + count
        following = _cursor(snapshot.version, scope, end) if end < len(found.columns) else None
        return _Columns(snapshot, found, tuple(rows[:count]), following)

    async def readable(found: ObjectInfo) -> None:
        verdicts = await adapter.probe([(found.database, found.name)])
        if not verdicts[(found.database, found.name)]:
            raise StarRocksError(StarRocksErrorCode.OBJECT_UNREADABLE)

    async def describe_table(page: _Columns) -> ToolObservation:
        started = time.monotonic()
        await readable(page.found)
        result = _from_snapshot(
            page.snapshot,
            SCHEMA_COLUMNS_SQL,
            DESCRIBE_COLUMNS,
            page.rows,
            page.following is not None,
            started,
        )
        return _observation(
            result, bounds, [read_dependency(page.found)], extra={"next_cursor": page.following}
        )

    async def describe_layout(checked: tuple[SchemaSnapshot, ObjectInfo]) -> ToolObservation:
        _, found = checked
        await readable(found)
        layout = await adapter.describe_layout(found.database, found.name, found.column_names)
        return _observation(layout, bounds, [read_dependency(found)])

    def check_query(request: ToolRequest) -> tuple[SchemaSnapshot, GuardedQuery]:
        code: QueryRejectionCode | None = None
        snapshot = current()
        try:
            return snapshot, guard_readonly_query(
                str(request.arguments["sql"]), snapshot.query_policy
            )
        except QueryRejectedError as exc:
            code = exc.code
        # 在 except 之外抛出：拒绝只带固定原因码与说明，不带 SQLGuard 异常的上下文。
        raise ToolRejectedError(f"查询未执行（{code}）：{REJECTION_MESSAGES[code]}")

    async def run_query(checked: tuple[SchemaSnapshot, GuardedQuery]) -> ToolObservation:
        snapshot, query = checked
        dependencies = _referenced(snapshot, query.referenced_objects, query.referenced_columns)
        return _observation(await adapter.run_query(query), bounds, dependencies)

    def check_explain(request: ToolRequest) -> tuple[SchemaSnapshot, ExplainQuery]:
        code: QueryRejectionCode | None = None
        snapshot = current()
        try:
            return snapshot, guard_explain_query(
                str(request.arguments["sql"]), snapshot.query_policy
            )
        except QueryRejectedError as exc:
            code = exc.code
        raise ToolRejectedError(f"执行计划未获取（{code}）：{REJECTION_MESSAGES[code]}")

    async def explain(checked: tuple[SchemaSnapshot, ExplainQuery]) -> ToolObservation:
        snapshot, query = checked
        dependencies = _referenced(snapshot, query.referenced_objects, query.referenced_columns)
        return _observation(await adapter.explain(query), bounds, dependencies)

    contracts = [
        contract(
            LIST_TABLES,
            list_policy,
            "按关键词搜索所选集群中只读账号可查询的库、表与视图：不区分大小写匹配“库名.表名”、"
            "表注释与列名，按库表名排序分页返回库、表、类型与注释。database 为 null 时搜索全部库；"
            f"page_size 为 1 到 {page_rows}（结果字节放不下时一页会少于它）。第一页 cursor 传 "
            "null；结果的 next_cursor 非空表示还有未返回的匹配，原样传回它并保持 keyword、"
            "database 与 page_size 不变即可取得下一页，为 null 时已全部返回。表结构刷新后旧 "
            "cursor 失效，须从头搜索。",
        ),
        contract(
            DESCRIBE_TABLE,
            describe_policy,
            "查看一张可查询的表或视图的列、类型、是否可空与注释；"
            "database 与 table 取自 list_tables 的搜索结果。列多时分次返回：第一次 cursor 传 "
            "null，结果的 next_cursor 非空时原样传回它（库与表不变）取得后续列。",
        ),
        contract(
            LAYOUT_TOOL,
            layout_policy,
            "查看一张允许查询的表的布局：表模型、分区键、分桶方式与键、桶数、排序键与主键。"
            "含未获准列的键不显示；视图或当前账号看不到的表返回空结果。",
        ),
        contract(
            RUN_QUERY,
            query_policy,
            "执行一条只读 SELECT 或 UNION/UNION ALL（可带 WITH、子查询与窗口函数），可跨库 JOIN。"
            "表名写成 库名.表名；只在一个库中存在的表可省略库名。只能引用可读的表、视图与获准"
            "函数；* 按表结构展开，结果列数有上限。表达式列须用 AS 起别名，结果列名不能重复。"
            "结果行数有上限，超过时返回的结果标记为截断。",
        ),
        contract(
            EXPLAIN_QUERY,
            explain_policy,
            "获取一条只读 SELECT 或 UNION 的执行计划（优化器估算），不执行该查询。只接受与"
            "查询相同的语法与范围（表名写法同 run_readonly_query）；结果不是实际运行数据。",
        ),
    ]
    policies = [list_policy, describe_policy, query_policy, explain_policy, layout_policy]
    key = target.target_id
    executes: dict[tuple[str, str], Execute] = {
        (LIST_TABLES, key): Prechecked(check=check_search, run=list_tables),
        (DESCRIBE_TABLE, key): Prechecked(check=check_describe, run=describe_table),
        (LAYOUT_TOOL, key): Prechecked(check=check_table, run=describe_layout),
        (RUN_QUERY, key): Prechecked(check=check_query, run=run_query),
        (EXPLAIN_QUERY, key): Prechecked(check=check_explain, run=explain),
    }
    if target.audit is not None:
        limit = target.audit.max_window_minutes

        audit = target.audit

        def check_window(request: ToolRequest) -> _SlowQueries:
            args = request.arguments
            window, order = args["window_minutes"], args["order_by"]
            if not _counting(window) or not 1 <= window <= limit:
                raise ToolRejectedError(f"慢查询未读取：时间窗须在 1 到 {limit} 分钟之间")
            # 参数模型已把排序方式限定为枚举；这里再按 Adapter 的固定映射复核。
            if order not in AUDIT_ORDER_COLUMNS:
                raise ToolRejectedError("慢查询未读取：排序方式不在允许范围内")
            database = args["database"]
            return _SlowQueries(
                window=window,
                order=str(order),
                database=None if database is None else str(database),
                snapshot=current(),
            )

        async def slow_queries(args: _SlowQueries) -> ToolObservation:
            snapshot = args.snapshot
            # 原文中未限定的对象名按每条记录自身的会话当前库解析；显式写出的库对象同样按快照与
            # 当前权限检查（Adapter 与下面的引用重算用同一规则）。
            policy = snapshot.query_policy
            result = await adapter.slow_queries(args.window, args.order, policy, args.database)
            # 原文已通过同一范围的 SQLGuard：在线程中重算各行引用（同步 CPU），按当前权限过滤。
            references = await asyncio.to_thread(_audit_references, result.rows, policy)
            verdicts = await adapter.probe(sorted({o for found, _ in references for o in found}))
            kept = [
                (row, refs)
                for row, refs in zip(result.rows, references, strict=True)
                if all(verdicts[obj] for obj in refs[0])
            ]
            rows = tuple(row for row, _ in kept)
            dependencies = [
                listed_dependency(audit.database, audit.table),
                *_referenced(
                    snapshot,
                    frozenset(o for _, (objects, _) in kept for o in objects),
                    frozenset(c for _, (_, columns) in kept for c in columns),
                ),
            ]
            filtered = result.model_copy(update={"rows": rows, "row_count": len(rows)})
            return _observation(filtered, bounds, dependencies)

        audit_policy = policy("list_slow_queries", ListSlowQueriesArgs, AUDIT_NOTE)
        contracts.append(
            contract(
                SLOW_QUERIES,
                audit_policy,
                "列出最近一段时间内所选集群实际运行过的慢查询（来自审计表），按所选指标降序；只列出"
                "引用对象全部获准的查询，给出实测耗时、扫描量、CPU、内存、排队时间、执行时的当前库"
                f"与 SQL 原文。时间窗为 1 到 {limit} 分钟；database 为 null 时读取所有库的记录，"
                "否则只读执行时当前库为它的记录。",
            )
        )
        policies.append(audit_policy)
        executes[(SLOW_QUERIES, key)] = Prechecked(check=check_window, run=slow_queries)

    return StarRocksTools(contracts=tuple(contracts), policies=tuple(policies), executes=executes)


DATA_SCOPE_FORMAT: Final = "xiaowei.data_scope.starrocks/5"
"""摘要公式的显式版本：纳入或排除的字段改变时更新，使旧公式下保存的证据一次性失效。

/2（P2.5 Task 2 审查）：加入数据库类型、TLS、服务端时间与内存限额、客户端期限与时区。
/3（D1/D2）：固定 SQL 语义，纳入 sql_mode，并使修复前的 StarRocks 证据一次性失效。
/4（P2.5 Task 3）：跨库、UNION/窗口/星号展开与列名大小写规则改变查询语义；纳入 max_result_columns。
/5（P2.5 Task 6）：审计改为读取所有库、按每条记录的当前库解析原文并输出该库；列表改为关键词分页。
"""


def data_scope_digest(target: StarRocksTarget) -> str:
    """目标配置范围的稳定摘要（``scope_canonical`` 的 SHA-256）。"""
    return f"sha256:{hashlib.sha256(scope_canonical(target).encode()).hexdigest()}"


def scope_canonical(target: StarRocksTarget) -> str:
    """摘要的规范化内容：键排序、紧凑 JSON，集合先排序。

    与配置的书写顺序和进程的哈希种子无关，同一范围重启后摘要不变；函数名已由 ``SqlPolicy`` 统一
    为大写。纳入（计划 §2.6）：

    - 来源身份：集群 ID、数据库类型、``host``/``port``/``user``、是否 TLS、默认库。换了集群、账号
      或传输方式可能对应另一套数据与权限。
    - 资源限额：函数集合、``max_rows``/``max_sql_bytes``/``max_result_columns``、结果/单值/计划
      上限、服务端 ``query_timeout_seconds`` 与 ``query_mem_limit_bytes``、客户端
      ``client_timeout_seconds``、
      结构快照的容量与期限（``schema_limits``）。它们决定能读到多少数据、哪些查询能完成。
    - 事实形态：``time_zone``（时间值按它转换）、固定 ``sql_mode``、审计源（未配置为 ``null``）。

    不纳入：密码引用（凭据）、``tls_ca_file``（信任链文件位置，不改变数据）、
    ``connect_timeout_seconds`` 与 ``pool_size``（只影响能否、何时取得连接，不影响一次读取的范围与
    结果）。整轮期限、工具次数等预算属于应用的 ``Budget``，与目标无关，也不进入。可读对象与列来自
    结构快照，随权限变化，不进入（由 Evidence 的对象依赖逐次复核）。
    """
    policy = target.policy
    body = {
        "format": DATA_SCOPE_FORMAT,
        "database_type": "starrocks",
        "target_id": target.target_id,
        "host": target.host,
        "port": target.port,
        "user": target.user,
        "tls": target.tls,
        "default_database": target.database,
        "allowed_functions": sorted(policy.allowed_functions),
        "max_rows": policy.max_rows,
        "max_sql_bytes": policy.max_sql_bytes,
        "max_result_columns": policy.max_result_columns,
        "max_result_bytes": target.max_result_bytes,
        "max_value_bytes": target.max_value_bytes,
        "max_plan_lines": target.max_plan_lines,
        "query_timeout_seconds": target.query_timeout_seconds,
        "query_mem_limit_bytes": target.query_mem_limit_bytes,
        "client_timeout_seconds": target.client_timeout_seconds,
        "time_zone": target.time_zone,
        "sql_mode": SQL_MODE,
        "schema_limits": target.schema_limits.model_dump(mode="json"),
        "audit": None if target.audit is None else target.audit.model_dump(mode="json"),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def worst_case_observation(adapter: StarRocksAdapter) -> ToolObservation:
    """按目标声明的上限构造序列化后最大的合成结果，供启动容量检查交给真实投影器。

    实际 SQL 不超过“EXPLAIN 前缀 + SQLGuard 字节上限”与元数据模板中的较大者（查询的 SQL
    不带前缀，被前者覆盖；星号展开后的 SQL 同样受 SQLGuard 字节上限约束）。查询的列数不超过
    ``max_result_columns``，每个列名都以别名出现在规范化 SQL 中，因此列名合计不超过同一字节数；
    元数据、计划与审计的列头是固定的短名，也在此界内。最坏情形是列数取满、列名合计取满：除一列
    外每列一个字符（每多一列多出引号与分隔符）。运行时 ``_observation`` 按同一上限复核。SQL 与
    列名都以 JSON 转义膨胀最大的字符填满。结果行的序列化大小不超过 ``max_result_bytes``（Adapter
    按同一编码计数）；投影只看序列化大小，因此这里用同样大小的字符串代表行列表。
    """
    target = adapter.target
    sql_bytes = max(
        target.policy.max_sql_bytes + len(EXPLAIN_PREFIX),
        metadata_sql_bytes(),
        audit_sql_bytes(target),
    )
    names = min(target.policy.max_result_columns, sql_bytes)
    columns = [_WIDEST_CHAR] * (names - 1) + [_WIDEST_CHAR * (sql_bytes - names + 1)]
    return ToolObservation(
        payload={
            "sql": _WIDEST_CHAR * sql_bytes,
            "columns": columns,
            "rows": "x" * (target.max_result_bytes - 2),
            "row_count": _WIDEST_INT,
            "elapsed_ms": _WIDEST_INT,
        },
        captured_at=datetime(1, 1, 1, tzinfo=UTC),
        truncated=False,
    )


@dataclass(frozen=True)
class _Bounds:
    sql: int
    columns: int


# 快照版本（``secrets.token_hex(8)``，十六进制无转义）的字符数；启动容量检查按它构造游标。
_VERSION_CHARS: Final = 16
_CURSOR_FORMAT: Final = re.compile(r"([0-9a-f]{16})-([1-9][0-9]*)-([0-9a-f]{16})")


@dataclass(frozen=True)
class _SlowQueries:
    window: int
    order: str
    database: str | None
    snapshot: SchemaSnapshot


@dataclass(frozen=True)
class _Page:
    """前置检查规划好的一页搜表结果：要探测并列出的对象与续取游标。"""

    snapshot: SchemaSnapshot
    keys: tuple[tuple[str, str], ...]
    following: str | None


@dataclass(frozen=True)
class _Columns:
    """前置检查规划好的一段表结构：对象、要交付的列与续取游标。"""

    snapshot: SchemaSnapshot
    found: ObjectInfo
    rows: tuple[dict[str, Scalar], ...]
    following: str | None


def _cursor(version: str, scope: tuple[object, ...], offset: int) -> str:
    """续取游标：``<快照版本>-<偏移>-<校验>``，校验由版本、参数与偏移计算。

    游标只防误用（换了参数、跨快照或手写偏移），不是授权凭据：每页仍按当前权限探测。
    """
    return f"{version}-{offset}-{_cursor_tag(version, scope, offset)}"


def _cursor_tag(version: str, scope: tuple[object, ...], offset: int) -> str:
    body = json.dumps([version, *scope, offset], ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def _resume(cursor: object, snapshot: SchemaSnapshot, scope: tuple[object, ...]) -> int:
    """游标对应的偏移；``None`` 为从头开始。格式不符、快照已刷新或参数不同时在 I/O 前拒绝。"""
    if cursor is None:
        return 0
    found = _CURSOR_FORMAT.fullmatch(str(cursor))
    if found is None:
        raise ToolRejectedError(CURSOR_INVALID)
    version, offset, tag = found[1], int(found[2]), found[3]
    if version != snapshot.version:
        raise ToolRejectedError(SNAPSHOT_STALE)
    if tag != _cursor_tag(version, scope, offset):
        raise ToolRejectedError(CURSOR_MISMATCH)
    return offset


def _max_offset(target: StarRocksTarget) -> int:
    """游标偏移的上限：搜表不超过快照对象数，表结构不超过快照列数。"""
    limits = target.schema_limits
    return max(limits.max_objects, limits.max_columns)


def _counting(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _matches(obj: ObjectInfo, needle: str) -> bool:
    """关键词（已 casefold）是否出现在“库名.表名”、表注释或任一列名中。"""
    texts = (f"{obj.database}.{obj.name}", obj.comment or "", *(c.name for c in obj.columns))
    return any(needle in text.casefold() for text in texts)


def _referenced(
    snapshot: SchemaSnapshot,
    objects: Iterable[tuple[str, str]],
    columns: Iterable[tuple[str, str, str]],
) -> list[ObjectDependency]:
    """被引用对象（``(库, 对象)``）的 ``read`` 依赖：版本取自快照，列为被引用的列（COUNT(*) 时
    为空）。"""
    by_object: dict[tuple[str, str], set[str]] = {key: set() for key in objects}
    for database, name, column in columns:
        by_object.setdefault((database, name), set()).add(column)
    dependencies = []
    for (database, name), used in sorted(by_object.items()):
        found = snapshot.object(database, name)
        if found is None:  # SQLGuard 范围来自同一快照，不会发生；不能无依赖地交付
            raise StarRocksError(StarRocksErrorCode.RESULT_CONTRACT)
        dependencies.append(read_dependency(found, used))
    return dependencies


_References = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, str, str]]]


def _audit_references(
    rows: Iterable[Mapping[str, Scalar]], policy: QueryPolicy
) -> list[_References]:
    """各行原文引用的 ``(库, 对象)`` 与 ``(库, 对象, 列)``。"""
    return [audit_references(str(row["sql"]), policy, str(row["database"] or "")) for row in rows]


def _listed(obj: ObjectInfo) -> dict[str, Scalar]:
    return {"database": obj.database, "name": obj.name, "type": obj.type, "comment": obj.comment}


def _from_snapshot(
    snapshot: SchemaSnapshot,
    sql: str,
    columns: tuple[str, ...],
    rows: tuple[dict[str, Scalar], ...],
    truncated: bool,
    started: float,
) -> QueryResult:
    """快照中的结构：``sql`` 是采集它的元数据模板，采集时间是快照的采集时间。"""
    return QueryResult(
        target_id=snapshot.target_id,
        sql=sql,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        collected_at=snapshot.collected_at,
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )


def _observation(
    result: QueryResult,
    bounds: _Bounds,
    dependencies: Iterable[ObjectDependency],
    *,
    extra: Mapping[str, Scalar] | None = None,
) -> ToolObservation:
    """Adapter 结果转为工具观测；列名或 SQL 超过装配时假定的上限时按结果契约失败。"""
    columns = list(result.columns)
    if _json_size(result.sql) > bounds.sql or _json_size(columns) > bounds.columns:
        raise StarRocksError(StarRocksErrorCode.RESULT_CONTRACT)
    return ToolObservation(
        payload={
            "sql": result.sql,
            "columns": columns,
            "rows": [dict(row) for row in result.rows],
            "row_count": result.row_count,
            "elapsed_ms": result.elapsed_ms,
            **(extra or {}),
        },
        captured_at=result.collected_at,
        truncated=result.truncated,
        dependencies=tuple(dependencies),
    )


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())
