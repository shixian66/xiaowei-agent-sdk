"""StarRocks 受治理工具：五个（配置审计源时六个）工具的契约、参数与投影策略，以及绑定 Adapter
的执行函数。

数据范围来自每个目标的结构快照（``starrocks_schema.SchemaCache``），不来自配置的表列清单。
所有工具的前置检查都先取当前快照：没有快照或已到期时在任何 I/O 前拒绝。

``local/list_tables`` 与 ``local/describe_table`` 的结构取自快照，交付前对要列出的每个对象
重新做零行权限探测（快照中的“可读”只是采集时的结论）：列表跳过已不可读的对象，表结构遇到
已不可读的对象整体失败。列表按目录顺序分批探测，每次最多尝试 ``2 × max_rows`` 个对象（按
已尝试的对象数计，与可读对象多少无关），还有未检查的候选时标记截断。
``local/describe_table_layout`` 同样先探测，再读该表的布局；两个 ``describe`` 工具按
``database`` 与 ``table`` 定位快照中的对象。``local/run_readonly_query`` 只执行 SQLGuard
产生的 ``GuardedQuery``，``local/explain_query`` 只以 Adapter 的固定显式级别 EXPLAIN
``ExplainQuery``，不执行被解释的查询；二者的 SQLGuard 范围是快照中默认库的可读对象与列，
执行时由数据库自己的权限检查最终把关。对象核对与 SQLGuard 都是 ``Prechecked`` 的同步前置
检查：在当前授权之后、预算预留与任何 StarRocks I/O 之前运行，拒绝时模型只收到固定原因码与
说明，可在本轮剩余轮次内修正。

``local/list_slow_queries`` 只在目标配置了审计源时登记：读已有审计表中本目标库、原文通过当前
快照 SQLGuard 范围的慢查询（见 ``StarRocksAdapter.slow_queries``），时间窗与排序方式在 I/O 前检查。
交付前再对各行原文引用的对象做一次零行探测，引用了已不可读对象的行不列出。

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
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final, Literal

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
    bounded_rows,
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
    "审计记录由 AuditLoader 批量导入，最近约一个导入周期内的查询可能尚未出现；只显示本目标数据库中"
    "能确认引用对象全部获准的查询，列表可能少于上限，为空也不代表没有慢查询。"
    "指标为 StarRocks 实测值，空值表示未知。"
)
SCHEMA_NOTE: Final = (
    "表结构来自定期采集的元数据快照（采集时间见来源），只含只读账号可 SELECT 的对象；"
    "列出的对象已在交付前逐个确认当前仍可读。"
)
LIST_COLUMNS: Final = ("database", "name", "type", "comment")
DESCRIBE_COLUMNS: Final = ("name", "type", "nullable", "comment")
PLAN_NOTE: Final = (
    "执行计划是优化器按当前统计信息给出的估算；获取时未执行原查询，不包含实际耗时与资源消耗。"
    "没有对应审计记录时，不能确认实际运行慢的原因。"
)

RESULT_FIELDS: Final = ("sql", "columns", "rows", "row_count", "elapsed_ms")
# JSON 转义膨胀最大的单字节字符：控制字符写作 \u00XX，一个字节变六个。
_WIDEST_CHAR: Final = "\x01"
# 有符号 64 位整数中十进制写法最长的值。
_WIDEST_INT: Final = -(2**63)


# 交给模型的 schema 只限长度：受测模型端点是否接受正则 ``pattern`` 未经验证；集群 ID 的格式由配置
# 校验，未登记的取值在路由时于 I/O 前拒绝。
ClusterArg = Annotated[str, StringConstraints(min_length=1, max_length=32)]


class ListTablesArgs(BaseModel):
    """只有目标集群：不接受过滤条件、catalog 或任何查询参数。"""

    model_config = ConfigDict(extra="forbid")

    cluster: ClusterArg


class DescribeTableArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster: ClusterArg
    database: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    table: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class RunQueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster: ClusterArg
    sql: Annotated[str, StringConstraints(min_length=1)]


class ListSlowQueriesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster: ClusterArg
    window_minutes: int
    order_by: Literal["query_time", "scan_bytes", "scan_rows", "cpu", "memory", "pending"]


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
    projections: dict[str, Projection] = {
        a: Projection(fields=RESULT_FIELDS, max_bytes=max_bytes[a]) for a in AUDIENCES
    }

    scope = data_scope_digest(target)

    def policy(name: str, arguments: type[BaseModel], fact_note: str | None = None) -> ToolPolicy:
        return ToolPolicy(
            policy_id=f"starrocks.{target.target_id}.{name}",
            arguments=arguments,
            projections=projections,
            required=RESULT_FIELDS,
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

    list_policy = policy("list_tables", ListTablesArgs, SCHEMA_NOTE)
    describe_policy = policy("describe_table", DescribeTableArgs, SCHEMA_NOTE)
    layout_policy = policy("describe_table_layout", DescribeTableArgs)
    query_policy = policy("run_readonly_query", RunQueryArgs)
    explain_policy = policy("explain_query", ExplainQueryArgs, PLAN_NOTE)
    # 五个策略的投影相同：检查一次即覆盖全部工具。
    try:
        check_projection_capacity(query_policy, worst)
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

    def check_snapshot(request: ToolRequest) -> SchemaSnapshot:
        return current()

    async def list_tables(snapshot: SchemaSnapshot) -> ToolObservation:
        started = time.monotonic()
        limit = target.policy.max_rows
        cap = 2 * limit
        pending = sorted(snapshot.objects)
        readable: list[tuple[str, str]] = []
        attempted = 0
        # 按目录顺序分批探测：凑满一页再多一个即停，且按已尝试的对象数硬性封顶（与可读对象
        # 多少无关）；未探测的对象不列出，只标记截断。
        while pending and len(readable) <= limit and attempted < cap:
            size = min(limit, cap - attempted)
            batch, pending = pending[:size], pending[size:]
            attempted += len(batch)
            verdicts = await adapter.probe(batch)
            readable += [key for key in batch if verdicts[key]]
        rows = [_listed(snapshot.objects[key]) for key in readable]
        kept, truncated = bounded_rows(rows, limit, target)
        result = _from_snapshot(
            snapshot, SCHEMA_OBJECTS_SQL, LIST_COLUMNS, kept, truncated or bool(pending), started
        )
        shown = readable[: len(kept)]
        return _observation(result, bounds, [listed_dependency(*key) for key in shown])

    def check_table(request: ToolRequest) -> tuple[SchemaSnapshot, ObjectInfo]:
        snapshot = current()
        found = snapshot.object(str(request.arguments["database"]), str(request.arguments["table"]))
        if found is None:
            raise ToolRejectedError(
                "对象不在可读的表结构中，未执行；请先用 list_tables 查看可用的库与表"
            )
        return snapshot, found

    async def readable(found: ObjectInfo) -> None:
        verdicts = await adapter.probe([(found.database, found.name)])
        if not verdicts[(found.database, found.name)]:
            raise StarRocksError(StarRocksErrorCode.OBJECT_UNREADABLE)

    async def describe_table(checked: tuple[SchemaSnapshot, ObjectInfo]) -> ToolObservation:
        started = time.monotonic()
        snapshot, found = checked
        await readable(found)
        rows: list[dict[str, Scalar]] = [
            {"name": c.name, "type": c.type, "nullable": c.nullable, "comment": c.comment}
            for c in found.columns
        ]
        kept, truncated = bounded_rows(rows, len(rows), target)
        result = _from_snapshot(
            snapshot, SCHEMA_COLUMNS_SQL, DESCRIBE_COLUMNS, kept, truncated, started
        )
        return _observation(result, bounds, [read_dependency(found)])

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
            "列出所选集群中只读账号可查询的库、表与视图及其注释（按库表名排序，行数有上限）。",
        ),
        contract(
            DESCRIBE_TABLE,
            describe_policy,
            "查看一张可查询的表或视图的列、类型、是否可空与注释；"
            "database 与 table 取自 list_tables。",
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
        (LIST_TABLES, key): Prechecked(check=check_snapshot, run=list_tables),
        (DESCRIBE_TABLE, key): Prechecked(check=check_table, run=describe_table),
        (LAYOUT_TOOL, key): Prechecked(check=check_table, run=describe_layout),
        (RUN_QUERY, key): Prechecked(check=check_query, run=run_query),
        (EXPLAIN_QUERY, key): Prechecked(check=check_explain, run=explain),
    }
    if target.audit is not None:
        limit = target.audit.max_window_minutes

        audit = target.audit

        def check_window(request: ToolRequest) -> tuple[int, str, SchemaSnapshot]:
            window, order = request.arguments["window_minutes"], request.arguments["order_by"]
            if not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= limit:
                raise ToolRejectedError(f"慢查询未读取：时间窗须在 1 到 {limit} 分钟之间")
            # 参数模型已把排序方式限定为枚举；这里再按 Adapter 的固定映射复核。
            if order not in AUDIT_ORDER_COLUMNS:
                raise ToolRejectedError("慢查询未读取：排序方式不在允许范围内")
            return window, str(order), current()

        async def slow_queries(args: tuple[int, str, SchemaSnapshot]) -> ToolObservation:
            window, order, snapshot = args
            # 审计只读取目标默认库的记录：原文中未限定的对象名按该库解析（Task 6 改为按每条
            # 记录自身的库）；显式写出的其他库对象同样按快照与当前权限检查。
            policy = snapshot.query_policy.model_copy(update={"default_database": target.database})
            result = await adapter.slow_queries(window, order, policy)
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
                "列出最近一段时间内所选集群默认数据库实际运行过的慢查询（来自审计表），按所选指标"
                "降序；只列出引用对象全部获准的查询，给出实测耗时、扫描量、CPU、内存、排队"
                f"时间与 SQL 原文。时间窗为 1 到 {limit} 分钟。",
            )
        )
        policies.append(audit_policy)
        executes[(SLOW_QUERIES, key)] = Prechecked(check=check_window, run=slow_queries)

    return StarRocksTools(contracts=tuple(contracts), policies=tuple(policies), executes=executes)


DATA_SCOPE_FORMAT: Final = "xiaowei.data_scope.starrocks/4"
"""摘要公式的显式版本：纳入或排除的字段改变时更新，使旧公式下保存的证据一次性失效。

/2（P2.5 Task 2 审查）：加入数据库类型、TLS、服务端时间与内存限额、客户端期限与时区。
/3（D1/D2）：固定 SQL 语义，纳入 sql_mode，并使修复前的 StarRocks 证据一次性失效。
/4（P2.5 Task 3）：跨库、UNION/窗口/星号展开与列名大小写规则改变查询语义；纳入 max_result_columns。
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
    return [audit_references(str(row["sql"]), policy) for row in rows]


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
    result: QueryResult, bounds: _Bounds, dependencies: Iterable[ObjectDependency]
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
        },
        captured_at=result.collected_at,
        truncated=result.truncated,
        dependencies=tuple(dependencies),
    )


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())
