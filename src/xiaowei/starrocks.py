"""StarRocks 只读 Adapter：只执行 SQLGuard 产物与代码生成的元数据查询，结果有界。

执行计划只以代码常量 ``EXPLAIN_PREFIX``（固定显式级别）加 ``ExplainQuery`` 的规范化 SQL
发出，不使用裸 ``EXPLAIN``：其级别取自 FE 可变配置 ``query_explain_level``，可被改为
ANALYZE 而实际执行查询。显式级别 ``LOGICAL`` 由 P2 Task 0 在 StarRocks 4.1.4 上实测选定。

连接：每次请求一条新连接，用完即关，不复用。``pool_size`` 是并发槽位，等待槽位与建立连接
共用 ``connect_timeout_seconds`` 期限。asyncmy 自带连接池没有获取期限，且按 ``connected``
决定是否回收，``close()`` 后连接仍可能回到空闲队列；每次新建连接可以保证中断、截断或结果
不明的连接绝不被下一次请求使用，也不会让会话变量跨请求残留。

每条连接先用可信值设置 ``query_timeout``、``query_mem_limit``、``time_zone`` 并回读核对，
任一步失败都不执行查询。读取使用驱动的非缓冲游标，最多读 ``max_returned_rows + 1`` 行；
总字节按生产 JSON 编码（``ensure_ascii=False``、默认分隔符）后的 UTF-8 大小计算，单值同样
如此。超过行数、总字节或单值上限时不保留该行，标记截断并中止连接。只有完整读完的连接
才发送 ``QUIT`` 正常关闭；截断、错误、超时、取消一律直接断开，不重试。客户端取消或超时
不代表服务端已停止，服务端由 ``query_timeout`` 兜底。

表布局只读 ``information_schema.tables_config`` 的表模型、分区、分桶、排序与主键，不读
``PROPERTIES``（存储卷、副本等部署信息）。键字段中的每个名字都须是该对象的获准列，否则整段
替换为 ``LAYOUT_HIDDEN``：不显示未获准列名，也不留下获准的一部分让人误以为是完整的键。

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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
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
from xiaowei.sqlguard import ExplainQuery, GuardedQuery, QueryPolicy

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

Scalar = None | bool | int | float | str

_Name = Annotated[str, StringConstraints(min_length=1)]
_TIME_ZONE = re.compile(r"[A-Za-z0-9_+\-/]+")


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
}


class StarRocksError(Exception):
    """StarRocks 访问失败；消息只有固定说明。"""

    def __init__(self, code: StarRocksErrorCode) -> None:
        super().__init__(ERROR_MESSAGES[code])
        self.code = code


class StarRocksTarget(BaseModel):
    """唯一查询目标的可信静态配置；凭据只以 ``env:NAME`` 引用出现。

    ``policy`` 是同一目标的 SQLGuard allowlist，目标 ID 与默认 database 必须一致。
    ``client_timeout_seconds`` 覆盖会话设置、执行与读取，不早于服务端 ``query_timeout``。
    ``max_plan_lines`` 是执行计划最多返回的行数；字节与单值上限与查询共用。
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
    max_plan_lines: int = Field(default=500, ge=1)
    policy: QueryPolicy

    @field_validator("password_ref")
    @classmethod
    def _secret_ref(cls, ref: str) -> str:
        if not is_secret_ref(ref):
            raise ValueError("password_ref 必须是 env:NAME 形式")
        return ref

    @field_validator("time_zone")
    @classmethod
    def _zone(cls, name: str) -> str:
        # 会话设置语句内联该值，因此先限定字符集，再确认是可加载的时区名。
        if not _TIME_ZONE.fullmatch(name):
            raise ValueError("time_zone 不是合法的时区名")
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("time_zone 不是合法的时区名") from None
        return name

    @model_validator(mode="after")
    def _consistent(self) -> StarRocksTarget:
        if (self.policy.target_id, self.policy.default_database) != (self.target_id, self.database):
            raise ValueError("policy 必须属于同一目标与默认 database")
        if self.tls_ca_file is not None and not self.tls:
            raise ValueError("tls_ca_file 只能在启用 TLS 时设置")
        if self.client_timeout_seconds < self.query_timeout_seconds:
            raise ValueError("client_timeout_seconds 不能早于服务端 query_timeout_seconds")
        if self.max_value_bytes > self.max_result_bytes:
            raise ValueError("max_value_bytes 不能大于 max_result_bytes")
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


class Connection(Protocol):
    """Adapter 使用的最小驱动接口；``abort`` 同步断开，不再读写。"""

    async def execute(self, sql: str, args: tuple[object, ...] | None) -> tuple[str, ...]: ...
    async def fetch_row(self) -> tuple[object, ...] | None: ...
    async def close(self) -> None: ...
    def abort(self) -> None: ...


Connector = Callable[[], Awaitable[Connection]]


class _ResultContractError(Exception):
    pass


class _SessionMismatchError(Exception):
    pass


_AUTH_ERRORS: Final = frozenset({1045})
# MySQL 协议通用码，加上 StarRocks 4.1.4 实测：5203 拒绝访问，5024 超过 query_timeout。
_PERMISSION_ERRORS: Final = frozenset({1044, 1142, 1143, 1227, 1370, 5203})
_SERVER_TIMEOUT_ERRORS: Final = frozenset({5024})
_LOST_ERRORS: Final = frozenset({2006, 2013, 2055})

_LIST_TABLES: Final = (
    "SELECT TABLE_NAME AS name, TABLE_TYPE AS type FROM information_schema.tables "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN ({}) ORDER BY TABLE_NAME"
)
_DESCRIBE_TABLE: Final = (
    "SELECT COLUMN_NAME AS name, DATA_TYPE AS type, IS_NULLABLE AS nullable "
    "FROM information_schema.columns WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
    "AND COLUMN_NAME IN ({}) ORDER BY ORDINAL_POSITION"
)
# Task 0 在 4.1.4 上实测：视图也有一行（TABLE_ENGINE='VIEW'、键为空、桶数 0），这里排除。
_DESCRIBE_LAYOUT: Final = (
    "SELECT TABLE_MODEL AS model, PARTITION_KEY AS partition_key, "
    "DISTRIBUTE_TYPE AS distribute_type, DISTRIBUTE_KEY AS distribute_key, "
    "DISTRIBUTE_BUCKET AS buckets, SORT_KEY AS sort_key, PRIMARY_KEY AS primary_key "
    "FROM information_schema.tables_config "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND TABLE_ENGINE <> %s"
)
_SESSION_READ: Final = "SELECT @@query_timeout, @@query_mem_limit, @@time_zone"


class StarRocksAdapter:
    def __init__(
        self, target: StarRocksTarget, *, connect: Connector, clock: Callable[[], datetime]
    ) -> None:
        self._target = target
        self._connect = connect
        self._clock = clock
        self._zone = ZoneInfo(target.time_zone)
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

    async def list_tables(self) -> QueryResult:
        objects = tuple(sorted(self._target.policy.allowed_objects))
        sql = _list_tables_sql(len(objects))
        return await self._run(sql, (self._target.database, *objects), len(objects))

    async def describe_table(self, name: str) -> QueryResult:
        policy = self._target.policy
        if name not in policy.allowed_objects:
            raise StarRocksError(_Code.OBJECT_NOT_ALLOWED)
        columns = tuple(sorted(policy.allowed_columns[name]))
        sql = _describe_table_sql(len(columns))
        return await self._run(sql, (self._target.database, name, *columns), len(columns))

    async def describe_layout(self, name: str) -> QueryResult:
        """一张获准表的布局：至多一行，列为 ``LAYOUT_COLUMNS``；视图没有行。

        多于一行、列不符、键不是文本或结果被截断都按 ``RESULT_CONTRACT`` 失败：布局只有完整的
        一行才有意义。键字段按获准列过滤（见模块说明）。
        """
        policy = self._target.policy
        if name not in policy.allowed_objects:
            raise StarRocksError(_Code.OBJECT_NOT_ALLOWED)
        result = await self._run(_DESCRIBE_LAYOUT, (self._target.database, name, "VIEW"), 1)
        allowed = policy.allowed_columns[name]
        rows = [_layout_row(row, allowed) for row in result.rows]
        if result.columns != LAYOUT_COLUMNS or result.truncated or None in rows:
            raise StarRocksError(_Code.RESULT_CONTRACT)
        return result.model_copy(update={"rows": tuple(r for r in rows if r is not None)})

    async def _run(
        self, sql: str, args: tuple[object, ...] | None, max_rows: int, *, plan: bool = False
    ) -> QueryResult:
        started = time.monotonic()
        # 等待槽位与建立连接共用一个绝对期限；超期的阶段决定错误码。
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
            if conn is None:
                raise StarRocksError(code or _Code.CONNECT_FAILED)
            rows, truncated, columns, code = await self._query(conn, sql, args, max_rows, plan)
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
                rows, truncated = await self._read(conn, columns, max_rows, text_only=plan)
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
        expected = (str(t.query_timeout_seconds), str(t.query_mem_limit_bytes), t.time_zone)
        await conn.execute(
            f"SET query_timeout = {expected[0]}, query_mem_limit = {expected[1]}, "
            f"time_zone = '{expected[2]}'",
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
        self, conn: Connection, columns: tuple[str, ...], max_rows: int, *, text_only: bool
    ) -> tuple[list[dict[str, Scalar]], bool]:
        rows: list[dict[str, Scalar]] = []
        size = 2  # "[]"
        while (raw := await conn.fetch_row()) is not None:
            if len(raw) != len(columns) or (text_only and not all(isinstance(v, str) for v in raw)):
                raise _ResultContractError
            if len(rows) == max_rows:
                return rows, True
            row = {name: self._scalar(value) for name, value in zip(columns, raw, strict=True)}
            if any(_json_size(v) > self._target.max_value_bytes for v in row.values()):
                return rows, True
            added = _json_size(row) + (2 if rows else 0)  # 行之间的 ", "
            if size + added > self._target.max_result_bytes:
                return rows, True
            rows.append(row)
            size += added
        return rows, False

    def _scalar(self, value: object) -> Scalar:
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
            # 会话 time_zone 已设为目标时区，驱动返回的无时区值即目标时区的本地时间。
            aware = value.replace(tzinfo=self._zone) if value.tzinfo is None else value
            return aware.astimezone(self._zone).isoformat()
        if isinstance(value, date):
            return value.isoformat()
        raise _ResultContractError

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


def _list_tables_sql(objects: int) -> str:
    return _LIST_TABLES.format(", ".join(["%s"] * objects))


def _describe_table_sql(columns: int) -> str:
    return _DESCRIBE_TABLE.format(", ".join(["%s"] * columns))


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


def metadata_sql_bytes(policy: QueryPolicy) -> int:
    """元数据查询结果中 ``sql``（代码生成的模板，不含绑定值）的最大 UTF-8 字节数。"""
    widest = max(len(columns) for columns in policy.allowed_columns.values())
    return max(
        len(_list_tables_sql(len(policy.allowed_objects)).encode()),
        len(_describe_table_sql(widest).encode()),
        len(_DESCRIBE_LAYOUT.encode()),
    )


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
