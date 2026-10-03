"""StarRocks 受治理工具：五个（配置审计源时六个）工具的契约、参数与投影策略，以及绑定 Adapter
的执行函数。

``local/list_tables``、``local/describe_table`` 与 ``local/describe_table_layout`` 只执行代码
生成、参数绑定的元数据查询；
``local/run_readonly_query`` 只执行 SQLGuard 产生的 ``GuardedQuery``；``local/explain_query``
只以 Adapter 的固定显式级别 EXPLAIN SQLGuard 产生的 ``ExplainQuery``，不执行被解释的查询。
两个 ``describe`` 工具的对象核对与两种 SQL 的 SQLGuard 都是 ``Prechecked`` 的同步前置检查：在
当前授权之后、预算预留与任何 StarRocks I/O 之前运行，拒绝时模型只收到固定原因码与说明，
可在本轮剩余轮次内修正。

``local/list_slow_queries`` 只在目标配置了审计源时登记：读已有审计表中本目标库、原文通过当前
SQLGuard 范围的慢查询（见 ``StarRocksAdapter.slow_queries``），时间窗与排序方式在 I/O 前检查。

诊断用途能看到元数据工具（含表布局）与 ``explain_query``，看不到 ``run_readonly_query``。计划证据的
固定说明（``PLAN_NOTE``，审计为 ``AUDIT_NOTE``）登记在策略上，由交付时的代码附加在事实区，
不来自证据记录或模型。

四种用途投影的字段相同且都必须完整保留（``required``）：模型、Session、Web 与飞书得到同一组
获准列与有限行，只有容量不同。装配时按声明上限构造序列化后最大的合成结果，直接交给证据的
真实投影器检查 Web 与飞书两条路径，任一路径放不下即拒绝启动；运行时 ``EvidenceStore`` 仍会
在必需字段放不下时中止本轮。

全部策略共用 ``data_scope_digest``：证据只在装配时的数据范围下可读，以收窄（或任何改动）
范围的配置启动后，已保存的 StarRocks 证据不再进入模型、Session 或渠道。

多个目标各自装配一次：契约、策略（``starrocks.<目标>.<工具>``）与执行函数都按目标登记，执行函数
以 ``(tool_id, target_id)`` 为键。每个参数模型都以必填的 ``cluster`` 选择目标，对模型只有一套
同名工具（见 ``tools.routed_function_tool``）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from xiaowei.evidence import check_projection_capacity
from xiaowei.governance import Execute, Prechecked, Projection, ToolPolicy, ToolRejectedError
from xiaowei.models import AUDIENCES, Audience, ToolContract, ToolObservation, ToolRequest
from xiaowei.sqlguard import (
    REJECTION_MESSAGES,
    ExplainQuery,
    GuardedQuery,
    QueryRejectedError,
    QueryRejectionCode,
    guard_explain_query,
    guard_readonly_query,
)
from xiaowei.starrocks import (
    AUDIT_ORDER_COLUMNS,
    EXPLAIN_PREFIX,
    QueryResult,
    StarRocksAdapter,
    StarRocksError,
    StarRocksErrorCode,
    StarRocksTarget,
    audit_sql_bytes,
    metadata_sql_bytes,
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


def starrocks_tools(adapter: StarRocksAdapter, max_bytes: Mapping[Audience, int]) -> StarRocksTools:
    """按目标配置与四种用途的容量装配五个工具；容量容不下最坏结果时拒绝装配。"""
    target = adapter.target
    if set(max_bytes) != set(AUDIENCES):
        raise ValueError("StarRocks 工具：必须且只能给出四种用途的容量")
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

    list_policy = policy("list_tables", ListTablesArgs)
    describe_policy = policy("describe_table", DescribeTableArgs)
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

    async def list_tables(request: ToolRequest) -> ToolObservation:
        return _observation(await adapter.list_tables(), bounds)

    def check_table(request: ToolRequest) -> str:
        name = str(request.arguments["table"])
        if name not in target.policy.allowed_objects:
            raise ToolRejectedError("对象不在允许范围内，未执行；请先用 list_tables 查看可用对象")
        return name

    async def describe_table(name: str) -> ToolObservation:
        return _observation(await adapter.describe_table(name), bounds)

    async def describe_layout(name: str) -> ToolObservation:
        return _observation(await adapter.describe_layout(name), bounds)

    def check_query(request: ToolRequest) -> GuardedQuery:
        code: QueryRejectionCode | None = None
        try:
            return guard_readonly_query(str(request.arguments["sql"]), target.policy)
        except QueryRejectedError as exc:
            code = exc.code
        # 在 except 之外抛出：拒绝只带固定原因码与说明，不带 SQLGuard 异常的上下文。
        raise ToolRejectedError(f"查询未执行（{code}）：{REJECTION_MESSAGES[code]}")

    async def run_query(query: GuardedQuery) -> ToolObservation:
        return _observation(await adapter.run_query(query), bounds)

    def check_explain(request: ToolRequest) -> ExplainQuery:
        code: QueryRejectionCode | None = None
        try:
            return guard_explain_query(str(request.arguments["sql"]), target.policy)
        except QueryRejectedError as exc:
            code = exc.code
        raise ToolRejectedError(f"执行计划未获取（{code}）：{REJECTION_MESSAGES[code]}")

    async def explain(query: ExplainQuery) -> ToolObservation:
        return _observation(await adapter.explain(query), bounds)

    contracts = [
        contract(LIST_TABLES, list_policy, "列出所选集群允许查询的表与视图。"),
        contract(
            DESCRIBE_TABLE,
            describe_policy,
            "查看一张允许查询的表或视图的可用列、类型与是否可空。",
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
            "执行一条只读 SELECT。只能引用允许的表、列与函数，必须列出具体列；"
            "结果行数有上限，超过时返回的结果标记为截断。",
        ),
        contract(
            EXPLAIN_QUERY,
            explain_policy,
            "获取一条只读 SELECT 的执行计划（优化器估算），不执行该查询。只接受与查询相同"
            "范围的表、视图、列与函数；结果不是实际运行数据。",
        ),
    ]
    policies = [list_policy, describe_policy, query_policy, explain_policy, layout_policy]
    key = target.target_id
    executes: dict[tuple[str, str], Execute] = {
        (LIST_TABLES, key): list_tables,
        (DESCRIBE_TABLE, key): Prechecked(check=check_table, run=describe_table),
        (LAYOUT_TOOL, key): Prechecked(check=check_table, run=describe_layout),
        (RUN_QUERY, key): Prechecked(check=check_query, run=run_query),
        (EXPLAIN_QUERY, key): Prechecked(check=check_explain, run=explain),
    }
    if target.audit is not None:
        limit = target.audit.max_window_minutes

        def check_window(request: ToolRequest) -> tuple[int, str]:
            window, order = request.arguments["window_minutes"], request.arguments["order_by"]
            if not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= limit:
                raise ToolRejectedError(f"慢查询未读取：时间窗须在 1 到 {limit} 分钟之间")
            # 参数模型已把排序方式限定为枚举；这里再按 Adapter 的固定映射复核。
            if order not in AUDIT_ORDER_COLUMNS:
                raise ToolRejectedError("慢查询未读取：排序方式不在允许范围内")
            return window, str(order)

        async def slow_queries(args: tuple[int, str]) -> ToolObservation:
            return _observation(await adapter.slow_queries(*args), bounds)

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


def data_scope_digest(target: StarRocksTarget) -> str:
    """目标数据范围的稳定摘要：允许的对象、列与函数，以及决定可读数据多少的上限。

    集合排序、键排序后再序列化：与配置的书写顺序和进程的哈希种子无关，同一范围重启后摘要
    不变。函数名已由 ``QueryPolicy`` 统一为大写。连接端点与账号（``host``、``port``、``user``）
    进入摘要：同一份 allowlist 换了集群或账号可能对应另一套数据与权限。只决定期限、时区表示
    与密码引用的字段不进入摘要。审计源配置（未配置为 ``null``）整体进入摘要。
    """
    policy = target.policy
    body = {
        "target_id": target.target_id,
        "host": target.host,
        "port": target.port,
        "user": target.user,
        "default_database": policy.default_database,
        "allowed_objects": sorted(policy.allowed_objects),
        "allowed_columns": {name: sorted(cols) for name, cols in policy.allowed_columns.items()},
        "allowed_functions": sorted(policy.allowed_functions),
        "max_rows": policy.max_rows,
        "max_sql_bytes": policy.max_sql_bytes,
        "max_result_bytes": target.max_result_bytes,
        "max_value_bytes": target.max_value_bytes,
        "max_plan_lines": target.max_plan_lines,
        "audit": None if target.audit is None else target.audit.model_dump(mode="json"),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def worst_case_observation(adapter: StarRocksAdapter) -> ToolObservation:
    """按目标声明的上限构造序列化后最大的合成结果，供启动容量检查交给真实投影器。

    实际 SQL 不超过“EXPLAIN 前缀 + SQLGuard 字节上限”与元数据模板中的较大者（查询的 SQL
    不带前缀，被前者覆盖）；列名来自服务端，按同一上限
    约束（``_observation`` 在运行时执行）。两者都以 JSON 转义膨胀最大的字符填满。结果行的
    序列化大小不超过 ``max_result_bytes``（Adapter 按同一编码计数）；投影只看序列化大小，
    因此这里用同样大小的字符串代表行列表。
    """
    target = adapter.target
    sql_bytes = max(
        target.policy.max_sql_bytes + len(EXPLAIN_PREFIX),
        metadata_sql_bytes(target.policy),
        audit_sql_bytes(target),
    )
    return ToolObservation(
        payload={
            "sql": _WIDEST_CHAR * sql_bytes,
            "columns": [_WIDEST_CHAR * sql_bytes],
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


def _observation(result: QueryResult, bounds: _Bounds) -> ToolObservation:
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
    )


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())
