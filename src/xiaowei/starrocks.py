"""StarRocks 只读 Adapter：只执行 SQLGuard 产物与代码生成的元数据查询，结果有界。

执行计划只以代码常量 ``EXPLAIN_PREFIX``（固定显式级别）加 ``ExplainQuery`` 的规范化 SQL
发出，不使用裸 ``EXPLAIN``：其级别取自 FE 可变配置 ``query_explain_level``，可被改为
ANALYZE 而实际执行查询。显式级别 ``LOGICAL`` 由 P2 Task 0 在 StarRocks 4.1.4 上实测选定。

连接：每次请求一条新连接，用完即关，不复用。``pool_size`` 是并发槽位，等待槽位与建立连接
共用 ``connect_timeout_seconds`` 期限。asyncmy 自带连接池没有获取期限，且按 ``connected``
决定是否回收，``close()`` 后连接仍可能回到空闲队列；每次新建连接可以保证中断、截断或结果
不明的连接绝不被下一次请求使用，也不会让会话变量跨请求残留。

每条连接先用可信值设置 ``query_timeout``、``query_mem_limit``、``time_zone``、``sql_mode``，
并回读核对，任一步失败都不执行查询。非缓冲游标最多读 ``max_returned_rows + 1`` 行；
总字节按生产 JSON 编码（``ensure_ascii=False``、默认分隔符）后的 UTF-8 大小计算，单值同样
如此。超过行数、总字节或单值上限时不保留该行，标记截断并中止连接。只有完整读完的连接
才发送 ``QUIT`` 正常关闭；截断、错误、超时、取消一律直接断开，不重试。客户端取消或超时
不代表服务端已停止，服务端由 ``query_timeout`` 兜底。

数据范围不来自配置的表列清单：``read_schema`` 以代码模板读 ``information_schema`` 中全部用户库的
对象、列与表 ID（有界，超限按截断报告，由调用方拒绝发布），``probe`` 在同一条连接上对每个对象
执行零行 ``SELECT 1 FROM `库`.`表` WHERE 1 = 0``，按数据库的实际 SELECT 权限给出可读/确定不可读；
P2.5 Task 0 在 4.1.4 上实测该探测不会被优化消除，撤权对同一连接立即生效。可见不等于可读，
元数据可见性本身从不作为权限证明。

表布局只读 ``information_schema.tables_config`` 的表模型、分区、分桶、排序与主键，不读
``PROPERTIES``（存储卷、副本等部署信息）。键字段中的每个名字都须是该对象的获准列，否则整段
替换为 ``LAYOUT_HIDDEN``：不显示未获准列名，也不留下获准的一部分让人误以为是完整的键。
独立授权的 ``show_create_table`` 读取普通内部 OLAP 表的完整建表原文（含默认值与属性），
读取前后核对对象 ID、类型和引擎。原文整条保留或因容量不足整条省略，不交付半段 DDL。

慢查询只读已有的 AuditLoader 审计表（``StarRocksTarget.audit``）：代码模板与绑定值按时间窗、
``isQuery`` 与可选的会话当前库（审计 ``db`` 列）取有界候选（独立的候选行数与字节上限，多读一条
判断是否读完），原文超过 ``max_sql_bytes`` 的不读出。每条候选原文在 Adapter 内经当前 SQLGuard
范围（``audit_references``）检查：未限定的对象名只在该条记录的会话当前库中解析（``''`` 为没有
当前库，未限定名不在范围内），与数据库执行时相同；未通过、无法读出或放不下的行整行丢弃；其原文
只在本函数的临时变量中，不进入结果、日志或错误。``max_sql_bytes`` 不超过 ``stmt_limit - 4``（配置
校验），因此读出的原文不可能被 AuditLoader 截断。审计时间按审计源时区解释。

驱动与网络错误映射为固定错误码；错误在下层 ``except`` 结束后才抛出，不带服务端原文、SQL、
地址或凭据，``__cause__`` 与 ``__context__`` 为空。
"""

from __future__ import annotations

import asyncio
import enum
import json
import math
import re
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Final, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncmy
from asyncmy.cursors import SSCursor
from asyncmy.errors import MySQLError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from xiaowei.config import is_secret_ref, resolve_secret_ref
from xiaowei.sqlguard import ExplainQuery, GuardedQuery, QueryPolicy, audit_references

# 不进入数据范围的系统库（4.1.4）：元数据视图、系统表与统计信息库。
SYSTEM_DATABASES: Final = ("information_schema", "sys", "_statistics_")

SQL_MODE: Final = "ONLY_FULL_GROUP_BY"
"""与 SQLGuard 的 StarRocks 方言一致：|| 为 OR；会话不继承全局 PIPES_AS_CONCAT 等模式。"""

EXPLAIN_LEVEL: Final = "LOGICAL"
"""P2 Task 0 在 4.1.4 上选定：FE 默认级别为 ANALYZE 时仍零执行，且不输出列统计值或资源组。"""
EXPLAIN_PREFIX: Final = f"EXPLAIN {EXPLAIN_LEVEL} "
PLAN_COLUMN: Final = "plan"
LAYOUT_COLUMNS: Final = (
    "model",
    "partition_key",
    "distribute_type",
    "distribute_key",
    "buckets",
    "sort_key",
    "primary_key",
)
LAYOUT_HIDDEN: Final = "（含未获准列，未显示）"
_LAYOUT_KEYS: Final = frozenset({"partition_key", "distribute_key", "sort_key", "primary_key"})
# 键字段中的一个名字：反引号引用（名字内不含反引号）或普通标识符。
_KEY_NAME: Final = re.compile(r"`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*)")

AUDIT_ORDER_COLUMNS: Final[Mapping[str, str]] = {
    "query_time": "queryTime",
    "scan_bytes": "scanBytes",
    "scan_rows": "scanRows",
    "cpu": "cpuCostNs",
    "memory": "memCostBytes",
    "pending": "pendingTimeMs",
}
AUDIT_COLUMNS: Final = (
    "query_id",
    "started_at",
    "query_time_ms",
    "scan_bytes",
    "scan_rows",
    "return_rows",
    "cpu_ms",
    "mem_bytes",
    "pending_ms",
    "state",
    "digest",
    "database",
    "sql",
)
# 审计表的列（AuditLoader 5.0.0，Task 0 实测）与输出列的对应；不读身份、地址与错误文本。
_AUDIT_SOURCE: Final = (
    "queryId",
    "timestamp",
    "queryTime",
    "scanBytes",
    "scanRows",
    "returnRows",
    "cpuCostNs",
    "memCostBytes",
    "pendingTimeMs",
    "state",
    "digest",
    "db",
    "stmt",
)
_AUDIT_METRICS: Final = {
    "queryTime": "query_time_ms",
    "scanBytes": "scan_bytes",
    "scanRows": "scan_rows",
    "returnRows": "return_rows",
    "memCostBytes": "mem_bytes",
    "pendingTimeMs": "pending_ms",
}
_AUDIT_TEXT: Final = {"queryId": "query_id", "state": "state", "digest": "digest"}
# AuditLoader 截断后的长度落在 (stmt_limit - 4, stmt_limit]（Task 0 实测）。
_TRUNCATION_MARGIN: Final = 4
# JSON 转义膨胀最大的单字节字符（控制字符）写作 \u00XX：一个字节变六个。
_JSON_WIDEST: Final = 6

Scalar = None | bool | int | float | str

_Name = Annotated[str, StringConstraints(min_length=1)]
_Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_]+$", max_length=64)]
_TIME_ZONE = re.compile(r"[A-Za-z0-9_+\-/]+")


def _valid_zone(name: str) -> str:
    # 会话设置语句内联目标时区，因此先限定字符集，再确认是可加载的时区名。
    if not _TIME_ZONE.fullmatch(name):
        raise ValueError("time_zone 不是合法的时区名")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("time_zone 不是合法的时区名") from None
    return name


class StarRocksErrorCode(enum.StrEnum):
    POOL_TIMEOUT = "pool_timeout"
    CONNECT_FAILED = "connect_failed"
    AUTH_FAILED = "auth_failed"
    SESSION_SETUP_FAILED = "session_setup_failed"
    PERMISSION_DENIED = "permission_denied"
    QUERY_FAILED = "query_failed"
    TIMEOUT = "timeout"
    SERVER_TIMEOUT = "server_timeout"
    CONNECTION_LOST = "connection_lost"
    RESULT_CONTRACT = "result_contract"
    OBJECT_NOT_ALLOWED = "object_not_allowed"
    OBJECT_MISSING = "object_missing"
    OBJECT_UNREADABLE = "object_unreadable"


_Code = StarRocksErrorCode

ERROR_MESSAGES: Final[Mapping[StarRocksErrorCode, str]] = {
    _Code.POOL_TIMEOUT: "StarRocks 并发查询已满，请稍后重试",
    _Code.CONNECT_FAILED: "无法连接 StarRocks",
    _Code.AUTH_FAILED: "StarRocks 认证失败",
    _Code.SESSION_SETUP_FAILED: "StarRocks 会话限额未能生效，查询未执行",
    _Code.PERMISSION_DENIED: "StarRocks 拒绝了该对象或操作的访问权限",
    _Code.QUERY_FAILED: "StarRocks 执行查询失败",
    _Code.TIMEOUT: "StarRocks 查询超时；服务端可能仍在运行，直到其查询期限到达",
    _Code.SERVER_TIMEOUT: "StarRocks 查询超过服务端期限，已被服务端终止",
    _Code.CONNECTION_LOST: "与 StarRocks 的连接中断，结果未知",
    _Code.RESULT_CONTRACT: "StarRocks 返回的结果不符合契约",
    _Code.OBJECT_NOT_ALLOWED: "该对象不在查询目标的允许范围内",
    _Code.OBJECT_MISSING: "StarRocks 中不存在该对象",
    _Code.OBJECT_UNREADABLE: "该对象当前不存在或只读账号已无权读取",
}


class StarRocksError(Exception):
    """StarRocks 访问失败；消息只有固定说明。"""

    def __init__(self, code: StarRocksErrorCode) -> None:
        super().__init__(ERROR_MESSAGES[code])
        self.code = code


class AuditSource(BaseModel):
    """已有 AuditLoader 审计表的只读来源；小维不安装插件、不改其配置。

    ``time_zone`` 是审计 ``timestamp`` 的写入时区（FE 系统时区）；``stmt_limit`` 是插件的
    ``max_stmt_length``（UTF-8 字节），必填：目标的 ``max_sql_bytes`` 不得超过 ``stmt_limit - 4``。
    ``candidate_rows`` / ``candidate_bytes`` 约束一次取出并检查的候选，与输出的 ``max_rows``
    及目标的结果上限相互独立。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    database: _Identifier
    table: _Identifier
    time_zone: str
    stmt_limit: int = Field(ge=1)
    max_window_minutes: int = Field(default=1440, ge=1, le=43200)
    max_rows: int = Field(default=20, ge=1, le=100)
    candidate_rows: int = Field(default=200, ge=1, le=2000)
    candidate_bytes: int = Field(default=2_097_152, ge=1, le=16_777_216)

    @field_validator("time_zone")
    @classmethod
    def _zone(cls, name: str) -> str:
        return _valid_zone(name)

    @model_validator(mode="after")
    def _enough_candidates(self) -> AuditSource:
        if self.candidate_rows < self.max_rows:
            raise ValueError("candidate_rows 不能少于 max_rows")
        return self


class SqlPolicy(BaseModel):
    """SQLGuard 的静态部分：函数闭集与行数、SQL 字节（星号展开后同样计算）、结果列数上限。

    对象与列不在配置中：它们来自结构快照中只读账号实际可 SELECT 的对象（``starrocks_schema``）。
    ``max_result_columns`` 约束查询结果的列数，与 schema 有多少列无关；列名总字节不超过展开后的
    SQL 字节上限（每个结果列名都以别名出现在规范化 SQL 中）。函数名统一为大写。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_functions: frozenset[_Name]
    max_rows: int = Field(ge=1)
    max_sql_bytes: int = Field(ge=1)
    max_result_columns: int = Field(ge=1)

    @model_validator(mode="before")
    @classmethod
    def _no_allowlist(cls, data: object) -> object:
        if isinstance(data, Mapping) and ({"allowed_objects", "allowed_columns"} & set(data)):
            raise ValueError(
                "allowed_objects/allowed_columns 已取消：数据范围来自只读账号的实际 SELECT 权限，"
                "请删除这两项（target_id、default_database 同样删除）"
            )
        return data

    @field_validator("allowed_functions")
    @classmethod
    def _upper(cls, names: frozenset[str]) -> frozenset[str]:
        return frozenset(name.upper() for name in names)


class SchemaLimits(BaseModel):
    """结构快照的刷新期限与容量上限。

    ``refresh_seconds`` 是后台刷新间隔，``max_age_seconds`` 是快照可用的最长时间（自采集开始计），
    ``refresh_timeout_seconds`` 是一次刷新（读取元数据与逐个探测）的总期限。三者都不能无限，且
    刷新间隔不超过最大年龄。``max_objects``/``max_columns`` 是全部用户库的对象与列上限，
    ``max_bytes`` 是每条元数据读取序列化后的字节上限，超过任一上限整份快照不发布。注释按
    ``max_comment_chars`` 个字符截取。容量没有通用默认值，按目标规模配置。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    refresh_seconds: float = Field(default=60, gt=0, le=3600)
    max_age_seconds: float = Field(default=300, gt=0, le=86_400)
    refresh_timeout_seconds: float = Field(default=10, gt=0, le=600)
    max_objects: int = Field(ge=1)
    max_columns: int = Field(ge=1)
    max_bytes: int = Field(ge=2)
    max_comment_chars: int = Field(ge=0, le=2000)

    @model_validator(mode="after")
    def _bounded(self) -> SchemaLimits:
        if self.refresh_seconds > self.max_age_seconds:
            raise ValueError("refresh_seconds 不能大于 max_age_seconds")
        if self.refresh_timeout_seconds > self.refresh_seconds:
            raise ValueError("refresh_timeout_seconds 不能大于 refresh_seconds")
        return self


class StarRocksTarget(BaseModel):
    """一个查询目标的可信静态配置；凭据只以 ``env:NAME`` 引用出现。

    ``policy`` 是 SQLGuard 的函数闭集与上限，``schema_limits`` 是结构快照的期限与容量；对象与列来自
    快照。``database`` 是连接的默认库；业务 SQL 不继承它：未限定对象名只按快照中的唯一匹配补全
    （``sqlguard``），审计原文按每条记录自身的会话当前库解析。
    ``client_timeout_seconds`` 覆盖会话设置、执行与读取，不早于服务端 ``query_timeout``。
    ``max_plan_lines`` 是执行计划最多返回的行数；字节与单值上限与查询共用。
    ``max_ddl_bytes`` 只覆盖内部表原始 DDL 的单值上限，按 JSON 编码字节计；未配置时沿用
    ``max_value_bytes``，且仍受 ``max_result_bytes`` 约束。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: _Name
    host: _Name
    port: int = Field(ge=1, le=65535)
    database: _Name
    tls: bool
    tls_ca_file: str | None
    user: _Name
    password_ref: str
    connect_timeout_seconds: float = Field(gt=0)
    query_timeout_seconds: int = Field(ge=1)
    client_timeout_seconds: float = Field(gt=0)
    pool_size: int = Field(ge=1)
    time_zone: str
    query_mem_limit_bytes: int = Field(ge=1)
    max_result_bytes: int = Field(ge=2)
    max_value_bytes: int = Field(ge=1)
    max_ddl_bytes: int | None = Field(default=None, ge=1)
    max_plan_lines: int = Field(default=500, ge=1)
    policy: SqlPolicy
    schema_limits: SchemaLimits
    audit: AuditSource | None = None

    @field_validator("password_ref")
    @classmethod
    def _secret_ref(cls, ref: str) -> str:
        if not is_secret_ref(ref):
            raise ValueError("password_ref 必须是 env:NAME 形式")
        return ref

    @field_validator("time_zone")
    @classmethod
    def _zone(cls, name: str) -> str:
        return _valid_zone(name)

    @model_validator(mode="after")
    def _consistent(self) -> StarRocksTarget:
        if self.tls_ca_file is not None and not self.tls:
            raise ValueError("tls_ca_file 只能在启用 TLS 时设置")
        if self.client_timeout_seconds < self.query_timeout_seconds:
            raise ValueError("client_timeout_seconds 不能早于服务端 query_timeout_seconds")
        if self.max_value_bytes > self.max_result_bytes:
            raise ValueError("max_value_bytes 不能大于 max_result_bytes")
        if self.max_ddl_bytes is not None and self.max_ddl_bytes > self.max_result_bytes:
            raise ValueError("max_ddl_bytes 不能大于 max_result_bytes")
        audit = self.audit
        if audit is not None:
            if self.policy.max_sql_bytes > audit.stmt_limit - _TRUNCATION_MARGIN:
                raise ValueError("policy.max_sql_bytes 不能超过 audit.stmt_limit - 4")
            if audit.candidate_bytes < audit_candidate_bound(self):
                raise ValueError("audit.candidate_bytes 容不下一条最长的候选记录")
        return self


class QueryResult(BaseModel):
    """一次有界读取的结果；``rows`` 按列名给出 JSON 标量。

    ``row_count`` 是已返回行数，始终等于 ``len(rows)``，不声称数据库中的总行数。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: str
    sql: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, Scalar], ...]
    truncated: bool
    collected_at: datetime
    elapsed_ms: int
    row_count: int

    @model_validator(mode="after")
    def _counted(self) -> QueryResult:
        if self.row_count != len(self.rows):
            raise ValueError("row_count 必须等于已返回行数")
        return self


@dataclass(frozen=True)
class SchemaRows:
    """``read_schema`` 的原始结果（已转为 JSON 标量）；``truncated`` 表示任一读取超限。"""

    objects: tuple[dict[str, Scalar], ...]
    columns: tuple[dict[str, Scalar], ...]
    ids: tuple[dict[str, Scalar], ...]
    truncated: bool


@dataclass(frozen=True)
class ObjectVersion:
    """``verify_objects`` 读到的一个可读对象的当前版本：类型、表 ID 与列（名字 → 类型）。

    视图的 ``table_id`` 为 0，``tables_config`` 中没有该对象时为 ``None``。不含创建时间：4.1.4 的
    ``information_schema.tables.CREATE_TIME`` 在带 ``TABLE_SCHEMA`` 条件时按服务器时区、全表读取时
    按会话时区给出（P2.5 Task 2 审查修订实测），同一对象的两种读法不可比较。
    """

    type: str
    table_id: int | None
    columns: Mapping[str, str]


class Connection(Protocol):
    """Adapter 使用的最小驱动接口；``abort`` 同步断开，不再读写。"""

    async def execute(self, sql: str, args: tuple[object, ...] | None) -> tuple[str, ...]: ...
    async def fetch_row(self) -> tuple[object, ...] | None: ...
    async def close(self) -> None: ...
    def abort(self) -> None: ...


Connector = Callable[[], Awaitable[Connection]]


class _ResultContractError(Exception):
    pass


@dataclass(frozen=True)
class _ReadLimits:
    """调用方给出的读取上限与时区；审计候选用独立上限，并按审计时区解释无时区时间。"""

    max_bytes: int
    max_value: int
    zone: ZoneInfo


class _SessionMismatchError(Exception):
    pass


_AUTH_ERRORS: Final = frozenset({1045})
# MySQL 协议通用码，加上 StarRocks 4.1.4 实测：5203 拒绝访问，5024 超过 query_timeout。
_PERMISSION_ERRORS: Final = frozenset({1044, 1142, 1143, 1227, 1370, 5203})
# 4.1.4 实测：表不存在为 5502；1146 是 MySQL 协议通用码。
_MISSING_ERRORS: Final = frozenset({1146, 5502})
_SERVER_TIMEOUT_ERRORS: Final = frozenset({5024})
_LOST_ERRORS: Final = frozenset({2006, 2013, 2055})

# 三个 NOT IN 占位符依次绑定 ``SYSTEM_DATABASES``。
SCHEMA_OBJECTS_SQL: Final = (
    "SELECT TABLE_SCHEMA AS db, TABLE_NAME AS name, TABLE_TYPE AS type, "
    "LEFT(TABLE_COMMENT, %s) AS comment, CREATE_TIME AS created "
    "FROM information_schema.tables WHERE TABLE_SCHEMA NOT IN (%s, %s, %s) "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME LIMIT %s"
)
SCHEMA_COLUMNS_SQL: Final = (
    "SELECT TABLE_SCHEMA AS db, TABLE_NAME AS name, COLUMN_NAME AS col, COLUMN_TYPE AS type, "
    "IS_NULLABLE AS nullable, LEFT(COLUMN_COMMENT, %s) AS comment "
    "FROM information_schema.columns WHERE TABLE_SCHEMA NOT IN (%s, %s, %s) "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION LIMIT %s"
)
SCHEMA_IDS_SQL: Final = (
    "SELECT TABLE_SCHEMA AS db, TABLE_NAME AS name, TABLE_ID AS id "
    "FROM information_schema.tables_config WHERE TABLE_SCHEMA NOT IN (%s, %s, %s) "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME LIMIT %s"
)
# 证据依赖复核：单个对象的当前类型、表 ID 与列，库表名全部绑定。
OBJECT_TYPE_SQL: Final = (
    "SELECT TABLE_TYPE AS type FROM information_schema.tables "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)
OBJECT_ID_SQL: Final = (
    "SELECT TABLE_ID AS id FROM information_schema.tables_config "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)
OBJECT_COLUMNS_SQL: Final = (
    "SELECT COLUMN_NAME AS col, COLUMN_TYPE AS type FROM information_schema.columns "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION LIMIT %s"
)
# 原始 DDL 只开放普通内部表；元数据可见性不替代 SHOW CREATE 自身和交付前的 SELECT 权限复核。
DDL_IDENTITY_SQL: Final = (
    "SELECT c.TABLE_ID AS id, c.TABLE_ENGINE AS engine, t.TABLE_TYPE AS type "
    "FROM information_schema.tables_config c JOIN information_schema.tables t "
    "ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME "
    "WHERE c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s"
)
_SCHEMA_OBJECT_COLUMNS: Final = ("db", "name", "type", "comment", "created")
_SCHEMA_COLUMN_COLUMNS: Final = ("db", "name", "col", "type", "nullable", "comment")
_SCHEMA_ID_COLUMNS: Final = ("db", "name", "id")
# 零行探测：Task 0 在 4.1.4 上实测不被优化消除；无权为权限错误，对象不存在为缺失错误。
_PROBE: Final = "SELECT 1 FROM `{db}`.`{name}` WHERE 1 = 0"
# 4.1.4 实测：库名错写为 5501；1049 是 MySQL 协议通用码。
_PROBE_DENIED: Final = _PERMISSION_ERRORS | _MISSING_ERRORS | frozenset({1049, 5501})
# Task 0 在 4.1.4 上实测：视图也有一行（TABLE_ENGINE='VIEW'、键为空、桶数 0），这里排除。
_DESCRIBE_LAYOUT: Final = (
    "SELECT TABLE_MODEL AS model, PARTITION_KEY AS partition_key, "
    "DISTRIBUTE_TYPE AS distribute_type, DISTRIBUTE_KEY AS distribute_key, "
    "DISTRIBUTE_BUCKET AS buckets, SORT_KEY AS sort_key, PRIMARY_KEY AS primary_key "
    "FROM information_schema.tables_config "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND TABLE_ENGINE <> %s"
)
_SESSION_READ: Final = "SELECT @@query_timeout, @@query_mem_limit, @@time_zone, @@sql_mode"
# 库表名来自配置并已按 [A-Za-z0-9_] 校验，排序列与库过滤条件来自固定文本；其余全部绑定。
_AUDIT_CANDIDATES: Final = (
    "SELECT queryId, `timestamp`, queryTime, scanBytes, scanRows, returnRows, cpuCostNs, "
    "memCostBytes, pendingTimeMs, state, digest, db, "
    "CASE WHEN LENGTH(stmt) <= %s THEN stmt END AS stmt "
    "FROM `{database}`.`{table}` "
    "WHERE isQuery = 1 AND catalog = %s{db_filter} "
    "AND `timestamp` >= %s AND `timestamp` < %s "
    "ORDER BY {order} DESC LIMIT %s"
)
_AUDIT_DB_FILTER: Final = " AND db = %s"


class StarRocksAdapter:
    def __init__(
        self, target: StarRocksTarget, *, connect: Connector, clock: Callable[[], datetime]
    ) -> None:
        self._target = target
        self._connect = connect
        self._clock = clock
        # 会话 time_zone 已设为目标时区，驱动返回的无时区值即目标时区的本地时间。
        self._zone = ZoneInfo(target.time_zone)
        self._limits = _ReadLimits(
            max_bytes=target.max_result_bytes, max_value=target.max_value_bytes, zone=self._zone
        )
        self._slots = asyncio.Semaphore(target.pool_size)

    def __repr__(self) -> str:
        return f"StarRocksAdapter(target_id={self._target.target_id!r})"

    @property
    def target(self) -> StarRocksTarget:
        return self._target

    async def run_query(self, query: GuardedQuery) -> QueryResult:
        """执行 SQLGuard 产物中的规范化 SQL；只接受本目标的 ``GuardedQuery``。"""
        if not isinstance(query, GuardedQuery):
            raise TypeError("只执行 SQLGuard 产生的 GuardedQuery")
        if query.target_id != self._target.target_id:
            raise StarRocksError(_Code.OBJECT_NOT_ALLOWED)
        return await self._run(query.normalized_sql, None, query.max_returned_rows)

    async def explain(self, query: ExplainQuery) -> QueryResult:
        """以固定显式级别 EXPLAIN SQLGuard 产物；只接受本目标的 ``ExplainQuery``。

        结果恰好一列文本，列名固定为 ``PLAN_COLUMN``，每行一行计划；``sql`` 是实际发出的完整
        语句。多列或非文本值不符合契约，按 ``RESULT_CONTRACT`` 断开连接。
        """
        if not isinstance(query, ExplainQuery):
            raise TypeError("只对 SQLGuard 产生的 ExplainQuery 取执行计划")
        if query.target_id != self._target.target_id:
            raise StarRocksError(_Code.OBJECT_NOT_ALLOWED)
        sql = EXPLAIN_PREFIX + query.normalized_sql
        return await self._run(sql, None, self._target.max_plan_lines, plan=True)

    async def read_schema(self) -> SchemaRows:
        """读取全部用户库的对象、列与表 ID（代码模板，绑定值只有上限与系统库名）。

        每条读取按 ``schema_limits`` 的行数与字节上限读取；``truncated`` 为真表示至少一项超限，
        调用方不得据此发布快照。列名与类型不符按结果契约失败。
        """
        limits = self._target.schema_limits
        read = _ReadLimits(max_bytes=limits.max_bytes, max_value=limits.max_bytes, zone=self._zone)
        chars = limits.max_comment_chars
        objects = await self._run(
            SCHEMA_OBJECTS_SQL,
            (chars, *SYSTEM_DATABASES, limits.max_objects + 1),
            limits.max_objects,
            limits=read,
        )
        columns = await self._run(
            SCHEMA_COLUMNS_SQL,
            (chars, *SYSTEM_DATABASES, limits.max_columns + 1),
            limits.max_columns,
            limits=read,
        )
        ids = await self._run(
            SCHEMA_IDS_SQL,
            (*SYSTEM_DATABASES, limits.max_objects + 1),
            limits.max_objects,
            limits=read,
        )
        if (objects.columns, columns.columns, ids.columns) != (
            _SCHEMA_OBJECT_COLUMNS,
            _SCHEMA_COLUMN_COLUMNS,
            _SCHEMA_ID_COLUMNS,
        ):
            raise StarRocksError(_Code.RESULT_CONTRACT)
        return SchemaRows(
            objects=objects.rows,
            columns=columns.rows,
            ids=ids.rows,
            truncated=objects.truncated or columns.truncated or ids.truncated,
        )

    async def probe(self, objects: Sequence[tuple[str, str]]) -> dict[tuple[str, str], bool]:
        """在同一条连接上逐个零行探测 ``(库, 对象)`` 的当前 SELECT 权限。

        ``True`` 可读；``False`` 是数据库给出的确定结论（无权，或对象/库已不存在）。其他任何
        失败（连接、会话设置、超时、未知错误、探测返回了行）整批抛出 ``StarRocksError``：权限
        暂时无法验证，不返回部分结论。库表名含反引号或控制字符时按 ``OBJECT_NOT_ALLOWED`` 拒绝，
        不拼入 SQL。整批共用一个客户端期限。
        """
        verdicts = await self.verify_objects(objects, versioned=frozenset())
        return {key: verdict is not False for key, verdict in verdicts.items()}

    async def verify_objects(
        self, objects: Sequence[tuple[str, str]], *, versioned: frozenset[tuple[str, str]]
    ) -> dict[tuple[str, str], ObjectVersion | bool]:
        """同 ``probe`` 逐个零行探测；``versioned`` 中可读的对象再读取当前版本。

        结论：``False`` 确定不可读（无权、对象或库不存在，或可读却已不在元数据中）；``True`` 可读
        （不在 ``versioned`` 中）；``ObjectVersion`` 可读及其当前版本。版本只经代码模板与绑定的
        库表名读取，单个对象的列数按 ``schema_limits.max_columns`` 限量，超限、类型不符等按结果契约
        失败。失败的含义与期限同 ``probe``。
        """
        for database, name in objects:
            if not (safe_identifier(database) and safe_identifier(name)):
                raise StarRocksError(_Code.OBJECT_NOT_ALLOWED)
        if not objects:
            return {}
        conn = await self._acquire()
        try:
            verdicts, code = await self._probe(conn, objects, versioned)
        finally:
            self._slots.release()
        if code is not None:
            raise StarRocksError(code)
        return verdicts

    async def describe_layout(
        self, database: str, name: str, columns: frozenset[str]
    ) -> QueryResult:
        """一张表的布局：至多一行，列为 ``LAYOUT_COLUMNS``；视图没有行。

        多于一行、列不符、键不是文本或结果被截断都按 ``RESULT_CONTRACT`` 失败：布局只有完整的
        一行才有意义。键字段按 ``columns``（该对象在快照中的可读列）过滤（见模块说明）。调用方
        负责在交付前证明当前权限。
        """
        result = await self._run(_DESCRIBE_LAYOUT, (database, name, "VIEW"), 1)
        rows = [_layout_row(row, columns) for row in result.rows]
        if result.columns != LAYOUT_COLUMNS or result.truncated or None in rows:
            raise StarRocksError(_Code.RESULT_CONTRACT)
        return result.model_copy(update={"rows": tuple(r for r in rows if r is not None)})

    async def show_create_table(self, database: str, name: str, table_id: int) -> QueryResult:
        """只读普通内部表的服务器建表原文；调用方提供当前快照中的表 ID。

        固定语句，不接受用户 SQL。前后身份不一致、非内部表或返回契约异常均失败；超出
        ``max_ddl_bytes``（未配置时沿用单值限额）或结果总限额则不保留任何 DDL，标记截断。
        文本不裁剪、不重新拼接；它是服务器当前定义，不保证等于历史 CREATE 输入。
        """
        sql = show_create_sql(database, name)
        if (
            type(table_id) is not int
            or table_id <= 0
            or len(sql.encode()) > self._target.policy.max_sql_bytes
        ):
            raise StarRocksError(_Code.OBJECT_NOT_ALLOWED)
        started = time.monotonic()

        async def verify_identity() -> None:
            identity = await self._run(DDL_IDENTITY_SQL, (database, name), 1)
            expected = {"id": table_id, "engine": "OLAP", "type": "BASE TABLE"}
            if (
                identity.columns != ("id", "engine", "type")
                or identity.truncated
                or identity.rows != (expected,)
                or type(identity.rows[0]["id"]) is not int
            ):
                raise StarRocksError(_Code.OBJECT_NOT_ALLOWED)

        await verify_identity()
        limits = _ReadLimits(
            max_bytes=self._target.max_result_bytes,
            max_value=self._target.max_ddl_bytes or self._target.max_value_bytes,
            zone=self._zone,
        )
        result = await self._run(sql, None, 1, limits=limits)
        # 即使内容超限也复核对象，不能把对象替换伪装成容量不足。
        await verify_identity()
        if result.columns != ("Table", "Create Table"):
            raise StarRocksError(_Code.RESULT_CONTRACT)
        rows: tuple[dict[str, Scalar], ...] = ()
        if not result.truncated:
            if len(result.rows) != 1:
                raise StarRocksError(_Code.RESULT_CONTRACT)
            row = result.rows[0]
            ddl = row["Create Table"]
            if row["Table"] != name or not isinstance(ddl, str) or not ddl.strip():
                raise StarRocksError(_Code.RESULT_CONTRACT)
            rows = ({"ddl": ddl},)
        elif result.rows:
            # 单条原文只有整行丢弃的容量截断；已读到一行还未结束代表服务端返回了多行。
            raise StarRocksError(_Code.RESULT_CONTRACT)
        return result.model_copy(
            update={
                "columns": ("ddl",),
                "rows": rows,
                "row_count": len(rows),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        )

    async def slow_queries(
        self,
        window_minutes: int,
        order_by: str,
        policy: QueryPolicy,
        database: str | None = None,
    ) -> QueryResult:
        """最近 ``window_minutes`` 分钟内的慢查询，按 ``order_by`` 降序，至多 ``max_rows`` 行。

        ``database`` 为 ``None`` 时读取所有库的记录，否则只读会话当前库（审计 ``db`` 列）为它的
        记录。只返回原文通过 ``policy``（当前结构快照给出的 SQLGuard 范围）的记录：未限定的对象名
        按该条记录自身的会话当前库解析（见模块说明）。``truncated`` 表示结果可能不完整：候选读满
        ``candidate_rows`` 且还有下一条、候选读取达到 ``candidate_bytes``、已列满 ``max_rows`` 而
        还有未检查的候选，或获准的行因单值或结果字节上限没有列出。
        """
        audit = self._target.audit
        if audit is None:
            raise ValueError("未配置审计源")
        if not 1 <= window_minutes <= audit.max_window_minutes or order_by not in (
            AUDIT_ORDER_COLUMNS
        ):
            raise ValueError("时间窗或排序方式不在允许范围内")
        zone = ZoneInfo(audit.time_zone)
        end = self._clock().astimezone(zone).replace(tzinfo=None)
        start = end - timedelta(minutes=window_minutes)
        sql = _audit_sql(audit, AUDIT_ORDER_COLUMNS[order_by], filtered=database is not None)
        # 多读一条：读到它说明候选还没有读完（``_run`` 据此标记截断）。
        args = (
            self._target.policy.max_sql_bytes,
            "default_catalog",
            *(() if database is None else (database,)),
            start,
            end,
            audit.candidate_rows + 1,
        )
        limits = _ReadLimits(
            max_bytes=audit.candidate_bytes, max_value=_audit_value_bound(self._target), zone=zone
        )
        started = time.monotonic()
        candidates = await self._run(sql, args, audit.candidate_rows, limits=limits)
        if candidates.columns != _AUDIT_SOURCE:
            raise StarRocksError(_Code.RESULT_CONTRACT)
        code: StarRocksErrorCode | None = None
        try:
            # SQL 解析是同步 CPU 工作：放到线程中，另计一个客户端期限（读取候选已用过一个，
            # 最坏总耗时约为两倍 client_timeout_seconds）。线程逐行核对同一期限，到期即停，
            # 不在本次调用失败后继续占用 CPU。
            timeout = self._target.client_timeout_seconds
            deadline = time.monotonic() + timeout
            async with asyncio.timeout(timeout):
                rows, over = await asyncio.to_thread(
                    self._listed, candidates.rows, audit.max_rows, deadline, policy
                )
        except TimeoutError:
            code = _Code.TIMEOUT
        except _ResultContractError:
            code = _Code.RESULT_CONTRACT
        if code is not None:
            raise StarRocksError(code)
        return QueryResult(
            target_id=self._target.target_id,
            sql=sql,
            columns=AUDIT_COLUMNS,
            rows=tuple(rows),
            row_count=len(rows),
            truncated=candidates.truncated or over,
            collected_at=candidates.collected_at,
            elapsed_ms=candidates.elapsed_ms + round((time.monotonic() - started) * 1000),
        )

    def _listed(
        self,
        candidates: tuple[dict[str, Scalar], ...],
        max_rows: int,
        deadline: float,
        policy: QueryPolicy,
    ) -> tuple[list[dict[str, Scalar]], bool]:
        """按原排序取通过检查的行；返回 (行, 是否可能不完整)。

        可能不完整：已有 ``max_rows`` 行而后面还有未检查的候选（不为确认它们是否获准而继续
        解析），获准的行因单值或结果字节上限没有列出。``deadline`` 为 ``time.monotonic()`` 时刻：
        每行检查前核对，到期抛出 ``TimeoutError``。
        """
        rows: list[dict[str, Scalar]] = []
        size = 2  # "[]"
        dropped = False
        for candidate in candidates:
            if len(rows) == max_rows:
                return rows, True
            if time.monotonic() >= deadline:
                raise TimeoutError
            row = _audit_row(candidate)
            if row is None or not _within_scope(row["sql"], policy, str(row["database"] or "")):
                continue
            if any(_json_size(v) > self._target.max_value_bytes for v in row.values()):
                dropped = True
                continue
            added = _json_size(row) + (2 if rows else 0)
            if size + added > self._target.max_result_bytes:
                return rows, True
            rows.append(row)
            size += added
        return rows, dropped

    async def _run(
        self,
        sql: str,
        args: tuple[object, ...] | None,
        max_rows: int,
        *,
        plan: bool = False,
        limits: _ReadLimits | None = None,
    ) -> QueryResult:
        started = time.monotonic()
        conn = await self._acquire()
        try:
            rows, truncated, columns, code = await self._query(
                conn, sql, args, max_rows, plan, limits or self._limits
            )
        finally:
            self._slots.release()
        if code is not None:
            raise StarRocksError(code)
        return QueryResult(
            target_id=self._target.target_id,
            sql=sql,
            columns=columns,
            rows=tuple(rows),
            row_count=len(rows),
            truncated=truncated,
            collected_at=self._clock(),
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )

    async def _acquire(self) -> Connection:
        """占用一个并发槽位并建立连接；失败时释放槽位并抛出。成功后由调用方释放槽位。

        等待槽位与建立连接共用一个绝对期限；超期的阶段决定错误码。
        """
        deadline = asyncio.get_running_loop().time() + self._target.connect_timeout_seconds
        code: StarRocksErrorCode | None = None
        try:
            async with asyncio.timeout_at(deadline):
                await self._slots.acquire()
        except TimeoutError:
            code = _Code.POOL_TIMEOUT
        if code is not None:
            raise StarRocksError(code)
        try:
            conn, code = await self._open(deadline)
        except BaseException:
            self._slots.release()
            raise
        if conn is None:
            self._slots.release()
            raise StarRocksError(code or _Code.CONNECT_FAILED)
        return conn

    async def _probe(
        self,
        conn: Connection,
        objects: Sequence[tuple[str, str]],
        versioned: frozenset[tuple[str, str]],
    ) -> tuple[dict[tuple[str, str], ObjectVersion | bool], StarRocksErrorCode | None]:
        """会话设置后逐个探测（并按需读取版本）；返回错误码而不是抛出（同 ``_query``）。"""
        complete = False
        stage = _Code.SESSION_SETUP_FAILED
        verdicts: dict[tuple[str, str], ObjectVersion | bool] = {}
        try:
            async with asyncio.timeout(self._target.client_timeout_seconds):
                await self._prepare_session(conn)
                stage = _Code.QUERY_FAILED
                for key in objects:
                    readable = await self._probe_one(conn, *key)
                    if readable and key in versioned:
                        verdicts[key] = await self._version(conn, *key) or False
                    else:
                        verdicts[key] = readable
            complete = True
            return verdicts, None
        except TimeoutError:
            return {}, _Code.TIMEOUT
        except _SessionMismatchError:
            return {}, _Code.SESSION_SETUP_FAILED
        except _ResultContractError:
            return {}, _Code.RESULT_CONTRACT
        except (MySQLError, OSError) as error:
            return {}, _query_code(error, stage)
        finally:
            await self._release(conn, complete=complete)

    async def _version(self, conn: Connection, database: str, name: str) -> ObjectVersion | None:
        """一个对象的当前版本；元数据中已没有该对象时为 ``None``。结果不合契约时抛出。"""
        limits = self._target.schema_limits
        read = _ReadLimits(max_bytes=limits.max_bytes, max_value=limits.max_bytes, zone=self._zone)
        kinds = await self._rows(conn, OBJECT_TYPE_SQL, (database, name), ("type",), read)
        if not kinds:
            return None
        ids = await self._rows(conn, OBJECT_ID_SQL, (database, name), ("id",), read)
        columns = await self._rows(
            conn,
            OBJECT_COLUMNS_SQL,
            (database, name, limits.max_columns + 1),
            ("col", "type"),
            read,
            max_rows=limits.max_columns,
        )
        # 每条模板至多一行（超出已按结果契约失败）。
        (kind,) = kinds
        table_id = ids[0]["id"] if ids else None
        if (
            not isinstance(kind["type"], str)
            or isinstance(table_id, bool)
            or not isinstance(table_id, int | None)
            or not all(isinstance(c["col"], str) and isinstance(c["type"], str) for c in columns)
        ):
            raise _ResultContractError
        return ObjectVersion(
            type=kind["type"],
            table_id=table_id,
            columns={cast(str, c["col"]): cast(str, c["type"]) for c in columns},
        )

    async def _rows(
        self,
        conn: Connection,
        sql: str,
        args: tuple[object, ...],
        expected: tuple[str, ...],
        limits: _ReadLimits,
        *,
        max_rows: int = 1,
    ) -> list[dict[str, Scalar]]:
        """在已准备的连接上读取一条代码模板的完整结果；列不符或超过 ``max_rows`` 行时按结果契约
        失败。"""
        if await conn.execute(sql, args) != expected:
            raise _ResultContractError
        rows, truncated = await self._read(conn, expected, max_rows, False, limits)
        if truncated:
            raise _ResultContractError
        return rows

    @staticmethod
    async def _probe_one(conn: Connection, database: str, name: str) -> bool:
        try:
            await conn.execute(_PROBE.format(db=database, name=name), None)
        except MySQLError as error:
            if _error_number(error) in _PROBE_DENIED:
                return False
            raise
        if await conn.fetch_row() is not None:
            raise _ResultContractError
        return True

    async def _open(self, deadline: float) -> tuple[Connection | None, StarRocksErrorCode | None]:
        try:
            async with asyncio.timeout_at(deadline):
                return await self._connect(), None
        except (MySQLError, OSError) as error:
            return None, _connect_code(error)

    async def _query(
        self,
        conn: Connection,
        sql: str,
        args: tuple[object, ...] | None,
        max_rows: int,
        plan: bool,
        limits: _ReadLimits,
    ) -> tuple[list[dict[str, Scalar]], bool, tuple[str, ...], StarRocksErrorCode | None]:
        """执行并读取；返回错误码而不是抛出，使错误在离开 ``except`` 后由调用方抛出。

        ``plan`` 为真时结果必须恰好一列文本，列名换为 ``PLAN_COLUMN``；服务端列名不外传。
        """
        complete = False
        stage = _Code.SESSION_SETUP_FAILED
        try:
            async with asyncio.timeout(self._target.client_timeout_seconds):
                await self._prepare_session(conn)
                stage = _Code.QUERY_FAILED
                columns = await conn.execute(sql, args)
                if len(set(columns)) != len(columns):
                    raise _ResultContractError
                if plan:
                    if len(columns) != 1:
                        raise _ResultContractError
                    columns = (PLAN_COLUMN,)
                rows, truncated = await self._read(conn, columns, max_rows, plan, limits)
            complete = not truncated
            return rows, truncated, columns, None
        except TimeoutError:
            return [], False, (), _Code.TIMEOUT
        except _SessionMismatchError:
            return [], False, (), _Code.SESSION_SETUP_FAILED
        except _ResultContractError:
            return [], False, (), _Code.RESULT_CONTRACT
        except (MySQLError, OSError) as error:
            return [], False, (), _query_code(error, stage)
        finally:
            await self._release(conn, complete=complete)

    async def _prepare_session(self, conn: Connection) -> None:
        t = self._target
        expected = (
            str(t.query_timeout_seconds),
            str(t.query_mem_limit_bytes),
            t.time_zone,
            SQL_MODE,
        )
        await conn.execute(
            f"SET query_timeout = {expected[0]}, query_mem_limit = {expected[1]}, "
            f"time_zone = '{expected[2]}', sql_mode = '{expected[3]}'",
            None,
        )
        while await conn.fetch_row() is not None:
            pass
        if len(await conn.execute(_SESSION_READ, None)) != len(expected):
            raise _SessionMismatchError
        first, extra = await conn.fetch_row(), await conn.fetch_row()
        if first is None or extra is not None or tuple(str(v) for v in first) != expected:
            raise _SessionMismatchError

    async def _read(
        self,
        conn: Connection,
        columns: tuple[str, ...],
        max_rows: int,
        text_only: bool,
        limits: _ReadLimits,
    ) -> tuple[list[dict[str, Scalar]], bool]:
        rows: list[dict[str, Scalar]] = []
        size = 2  # "[]"
        while (raw := await conn.fetch_row()) is not None:
            if len(raw) != len(columns) or (text_only and not all(isinstance(v, str) for v in raw)):
                raise _ResultContractError
            if len(rows) == max_rows:
                return rows, True
            row = {name: _scalar(v, limits.zone) for name, v in zip(columns, raw, strict=True)}
            if any(_json_size(v) > limits.max_value for v in row.values()):
                return rows, True
            added = _json_size(row) + (2 if rows else 0)  # 行之间的 ", "
            if size + added > limits.max_bytes:
                return rows, True
            rows.append(row)
            size += added
        return rows, False

    async def _release(self, conn: Connection, *, complete: bool) -> None:
        """完整读完才尝试正常关闭；关闭未完成（含失败、超时、取消）时一律同步断开。"""
        closed = False
        try:
            if complete:
                async with asyncio.timeout(self._target.connect_timeout_seconds):
                    await conn.close()
                closed = True
        except (MySQLError, OSError):  # TimeoutError 是 OSError 子类
            pass
        finally:
            if not closed:
                conn.abort()


def _scalar(value: object, zone: ZoneInfo) -> Scalar:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _ResultContractError
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise _ResultContractError
        return format(value, "f")
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=zone) if value.tzinfo is None else value
        return aware.astimezone(zone).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise _ResultContractError


def _audit_sql(audit: AuditSource, order: str, *, filtered: bool) -> str:
    return _AUDIT_CANDIDATES.format(
        database=audit.database,
        table=audit.table,
        order=order,
        db_filter=_AUDIT_DB_FILTER if filtered else "",
    )


def audit_sql_bytes(target: StarRocksTarget) -> int:
    """审计候选查询 ``sql``（模板，不含绑定值）的最大 UTF-8 字节数；未配置审计源为 0。"""
    audit = target.audit
    if audit is None:
        return 0
    return max(
        len(_audit_sql(audit, c, filtered=True).encode()) for c in AUDIT_ORDER_COLUMNS.values()
    )


def _audit_value_bound(target: StarRocksTarget) -> int:
    """候选读取的单值上限：容得下一条 ``max_sql_bytes`` 的原文（按最宽转义计）。"""
    return max(target.max_value_bytes, _JSON_WIDEST * target.policy.max_sql_bytes + 2)


def audit_candidate_bound(target: StarRocksTarget) -> int:
    """一条候选记录序列化后的最大字节数：每个值都按候选单值上限计，加上键与分隔符。"""
    keys = sum(_json_size(name) + 2 for name in _AUDIT_SOURCE)  # "name": 与 ", "
    return 2 + keys + len(_AUDIT_SOURCE) * _audit_value_bound(target)


def _within_scope(sql: Scalar, policy: QueryPolicy, database: str) -> bool:
    """原文在会话当前库 ``database``（``''`` 为没有当前库）下能否通过当前 SQLGuard 范围；任何
    失败（含解析器意外异常）都按未通过处理。

    审计原文来自全集群、不可信：检查失败的唯一后果是这一行不列出，异常与原文都不外传。
    """
    if not isinstance(sql, str):
        return False
    try:
        audit_references(sql, policy, database)
    except Exception:  # 失败即丢弃该行，不传播可能含原文的异常
        return False
    return True


def _audit_row(candidate: dict[str, Scalar]) -> dict[str, Scalar] | None:
    """候选记录转为输出列；原文未读出时返回 ``None``（整行丢弃），类型不符时违反结果契约。"""
    stmt = candidate["stmt"]
    if stmt is not None and not isinstance(stmt, str):
        raise _ResultContractError
    started = candidate["timestamp"]
    if not isinstance(started, str):
        raise _ResultContractError
    row: dict[str, Scalar] = {}
    for source, name in _AUDIT_TEXT.items():
        value = candidate[source]
        if value is not None and not isinstance(value, str):
            raise _ResultContractError
        row[name] = value or None
    database = candidate["db"]
    if database is not None and not isinstance(database, str):
        raise _ResultContractError
    row["database"] = database or None  # '' 是没有当前库
    row["started_at"] = started
    for source, name in _AUDIT_METRICS.items():
        row[name] = _metric(candidate[source])
    cpu = _metric(candidate["cpuCostNs"])
    row["cpu_ms"] = None if cpu is None else _milliseconds(cpu)
    row["sql"] = stmt
    if stmt is None:
        return None
    return {name: row[name] for name in AUDIT_COLUMNS}


def _metric(value: Scalar) -> int | None:
    """NULL、空串与负值表示未知，输出 ``None``；0 是实测值。其他文本或类型违反结果契约。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _ResultContractError
    return None if value < 0 else value


def _milliseconds(nanoseconds: int) -> str:
    exact = Decimal(nanoseconds) / Decimal(1_000_000)
    return format(exact.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP), "f")


def probe_sql(database: str, name: str) -> str:
    """零行权限探测语句；只接受 ``safe_identifier`` 通过的库表名。"""
    if not (safe_identifier(database) and safe_identifier(name)):
        raise ValueError("库表名不能含反引号或控制字符")
    return _PROBE.format(db=database, name=name)


def show_create_sql(database: str, name: str) -> str:
    """用已验证的标识符生成一条只读 SHOW CREATE TABLE。"""
    if not (safe_identifier(database) and safe_identifier(name)):
        raise StarRocksError(_Code.OBJECT_NOT_ALLOWED)
    return f"SHOW CREATE TABLE `{database}`.`{name}`"


def safe_identifier(name: object) -> bool:
    """可以放进反引号的库表名：1–256 个字符，不含反引号与控制字符。"""
    return (
        isinstance(name, str)
        and 0 < len(name) <= 256
        and "`" not in name
        and all(ch.isprintable() for ch in name)
    )


def _layout_row(row: dict[str, Scalar], allowed: frozenset[str]) -> dict[str, Scalar] | None:
    """按获准列过滤键字段；键不是文本或 NULL 时不符合契约，返回 ``None``。"""
    shown = dict(row)
    for key in _LAYOUT_KEYS:
        value = row.get(key)
        if value is not None and not isinstance(value, str):
            return None
        shown[key] = _shown_keys(value, allowed)
    return shown


def _shown_keys(value: str | None, allowed: frozenset[str]) -> str | None:
    """键字段规范为 ``a, b``；任一部分不是获准列名（含表达式）时整段替换为 ``LAYOUT_HIDDEN``。"""
    if not value:
        return value
    names: list[str] = []
    for part in value.split(","):
        match = _KEY_NAME.fullmatch(part.strip())
        name = match and (match.group(1) or match.group(2))
        if not name or name not in allowed:
            return LAYOUT_HIDDEN
        names.append(name)
    return ", ".join(names)


def metadata_sql_bytes() -> int:
    """元数据结果中 ``sql``（代码模板，不含绑定值）的最大 UTF-8 字节数。"""
    return max(
        len(SCHEMA_OBJECTS_SQL.encode()),
        len(SCHEMA_COLUMNS_SQL.encode()),
        len(_DESCRIBE_LAYOUT.encode()),
    )


def fitting_rows(rows: Sequence[dict[str, Scalar]], max_rows: int, target: StarRocksTarget) -> int:
    """开头有多少行能一起放进一个结果：规则与查询读取相同，行数、单值与总字节（生产 JSON 编码）
    任一超限即停。"""
    count, size = 0, 2  # "[]"
    for row in rows[:max_rows]:
        if any(_json_size(v) > target.max_value_bytes for v in row.values()):
            break
        added = _json_size(row) + (2 if count else 0)
        if size + added > target.max_result_bytes:
            break
        count += 1
        size += added
    return count


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())


def _error_number(error: BaseException) -> int | None:
    number = error.args[0] if isinstance(error, MySQLError) and error.args else None
    return number if isinstance(number, int) else None


def _connect_code(error: BaseException) -> StarRocksErrorCode:
    if _error_number(error) in _AUTH_ERRORS:
        return _Code.AUTH_FAILED
    return _Code.CONNECT_FAILED


def _query_code(error: BaseException, stage: StarRocksErrorCode) -> StarRocksErrorCode:
    number = _error_number(error)
    if number in _PERMISSION_ERRORS and stage is _Code.QUERY_FAILED:
        return _Code.PERMISSION_DENIED
    if number in _SERVER_TIMEOUT_ERRORS and stage is _Code.QUERY_FAILED:
        return _Code.SERVER_TIMEOUT
    if number in _MISSING_ERRORS and stage is _Code.QUERY_FAILED:
        return _Code.OBJECT_MISSING
    if number in _LOST_ERRORS or (number is None and isinstance(error, OSError)):
        return _Code.CONNECTION_LOST
    return stage


# ---- asyncmy 装配 ---------------------------------------------------------------------------


def tls_context(target: StarRocksTarget) -> ssl.SSLContext | None:
    """启用 TLS 时验证证书链与主机名；asyncmy 收到 context 后总会升级，不会静默降级。"""
    if not target.tls:
        return None
    context = ssl.create_default_context(cafile=target.tls_ca_file)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


@dataclass(eq=False)
class _AsyncmyConnection:
    _conn: asyncmy.Connection
    _cursor: SSCursor

    async def execute(self, sql: str, args: tuple[object, ...] | None) -> tuple[str, ...]:
        await self._cursor.execute(sql, args)
        return tuple(column[0] for column in self._cursor.description or ())

    async def fetch_row(self) -> tuple[object, ...] | None:
        row: tuple[object, ...] | None = await self._cursor.fetchone()
        return row

    async def close(self) -> None:
        await self._conn.ensure_closed()

    def abort(self) -> None:
        self._conn.close()


def open_starrocks(target: StarRocksTarget, *, clock: Callable[[], datetime]) -> StarRocksAdapter:
    """装配 Adapter：在此解析凭据（缺失即失败），不建立任何连接。"""
    password = resolve_secret_ref(target.password_ref)
    context = tls_context(target)

    async def connect() -> Connection:
        conn = await asyncmy.connect(
            host=target.host,
            port=target.port,
            user=target.user,
            password=password.get_secret_value(),
            database=target.database,
            charset="utf8mb4",
            ssl=context,
            connect_timeout=target.connect_timeout_seconds,
            read_timeout=target.client_timeout_seconds,
            autocommit=True,
            local_infile=False,
            read_default_file=None,
            init_command=None,
        )
        # asyncmy 的类型存根把 cursor() 标为基类；传入 SSCursor 时返回非缓冲游标。
        return _AsyncmyConnection(conn, cast(SSCursor, conn.cursor(SSCursor)))

    return StarRocksAdapter(target, connect=connect, clock=clock)
