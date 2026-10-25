"""TiDB 两版适配；复用 MySQL 传输、限额、读取与连接关闭，数据库语义在本模块落实。"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import ClassVar, Final, Literal, cast

from asyncmy.errors import MySQLError
from sqlglot import exp

from xiaowei.sqlguard import (
    ExplainQuery,
    QueryPolicy,
    QueryRejectedError,
    _parse,
    _scopes,
    _sources,
)
from xiaowei.starrocks import (
    _SQL_IO_TRACE,
    ERROR_MESSAGES,
    Connection,
    Connector,
    ObjectVersion,
    QueryResult,
    Scalar,
    StarRocksAdapter,
    StarRocksError,
    StarRocksErrorCode,
    StarRocksTarget,
    _error_number,
    _ReadLimits,
    _ResultContractError,
    _ResultMetadata,
    _SessionMismatchError,
    mysql_connector,
    probe_sql,
)

SQL_MODE: Final = (
    "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_AUTO_CREATE_USER,NO_ENGINE_SUBSTITUTION"
)
VERSION_SQL: Final = "SELECT tidb_version()"
SESSION_READ: Final = (
    "SELECT @@SESSION.max_execution_time, @@SESSION.tidb_mem_quota_query, "
    "@@SESSION.time_zone, @@SESSION.sql_mode, @@SESSION.character_set_client, "
    "@@SESSION.character_set_connection, @@SESSION.character_set_results, "
    "@@SESSION.collation_connection, DATABASE()"
)
PROTECTION_SET: Final = "SET SESSION tidb_opt_enable_non_eval_scalar_subquery = ON"
PROTECTION_READ: Final = "SELECT @@SESSION.tidb_opt_enable_non_eval_scalar_subquery"

SYSTEM_DATABASES: Final = ("information_schema", "performance_schema", "metrics_schema", "mysql")
_BASE_SCHEMA_OBJECTS_SQL: Final = (
    "SELECT TABLE_SCHEMA AS db, TABLE_NAME AS name, TABLE_TYPE AS type, "
    "LEFT(TABLE_COMMENT, %s) AS comment, CREATE_TIME AS created "
    "FROM information_schema.tables WHERE LOWER(TABLE_SCHEMA) NOT IN (%s, %s, %s, %s) "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME LIMIT %s"
)
_BASE_SCHEMA_COLUMNS_SQL: Final = (
    "SELECT TABLE_SCHEMA AS db, TABLE_NAME AS name, COLUMN_NAME AS col, COLUMN_TYPE AS type, "
    "IS_NULLABLE AS nullable, LEFT(COLUMN_COMMENT, %s) AS comment "
    "FROM information_schema.columns WHERE LOWER(TABLE_SCHEMA) NOT IN (%s, %s, %s, %s) "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION LIMIT %s"
)
_BASE_SCHEMA_IDS_SQL: Final = (
    "SELECT TABLE_SCHEMA AS db, TABLE_NAME AS name, TIDB_TABLE_ID AS id "
    "FROM information_schema.tables WHERE LOWER(TABLE_SCHEMA) NOT IN (%s, %s, %s, %s) "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME LIMIT %s"
)
OBJECT_ID_SQL: Final = (
    "SELECT TIDB_TABLE_ID AS id FROM information_schema.tables "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)
VIEW_DEFINITION_SQL: Final = (
    "SELECT VIEW_DEFINITION AS definition FROM information_schema.views "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)
DDL_IDENTITY_SQL: Final = (
    "SELECT TIDB_TABLE_ID AS id, ENGINE AS engine, TABLE_TYPE AS type "
    "FROM information_schema.tables WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)

INDEX_SQL: Final = (
    "SELECT INDEX_NAME AS index_name, NON_UNIQUE AS non_unique, "
    "SEQ_IN_INDEX AS seq_in_index, COLUMN_NAME AS column_name "
    "FROM information_schema.statistics WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
    "ORDER BY INDEX_NAME, SEQ_IN_INDEX LIMIT %s"
)


DIAGNOSTIC_TABLES: Final = frozenset({"SLOW_QUERY", "CLUSTER_SLOW_QUERY", "TIFLASH_REPLICA"})
DIAGNOSTIC_SOURCES: Final = frozenset(
    ("information_schema", name.casefold()) for name in DIAGNOSTIC_TABLES
)
# 只登记表名；列及当前可读性仍由原结构快照在运行时读取。
_DIAGNOSTIC_FILTER = (
    "(LOWER(TABLE_SCHEMA) NOT IN (%s, %s, %s, %s) OR "
    "(LOWER(TABLE_SCHEMA) = 'information_schema' AND UPPER(TABLE_NAME) IN ("
    + ", ".join("'" + name + "'" for name in sorted(DIAGNOSTIC_TABLES)) + ")))"
)
SCHEMA_OBJECTS_SQL: Final = _BASE_SCHEMA_OBJECTS_SQL.replace(
    "LOWER(TABLE_SCHEMA) NOT IN (%s, %s, %s, %s)", _DIAGNOSTIC_FILTER
)
SCHEMA_COLUMNS_SQL: Final = _BASE_SCHEMA_COLUMNS_SQL.replace(
    "LOWER(TABLE_SCHEMA) NOT IN (%s, %s, %s, %s)", _DIAGNOSTIC_FILTER
)
SCHEMA_IDS_SQL: Final = _BASE_SCHEMA_IDS_SQL.replace(
    "LOWER(TABLE_SCHEMA) NOT IN (%s, %s, %s, %s)", _DIAGNOSTIC_FILTER
)


class TiDBTarget(StarRocksTarget):
    """共用连接/容量配置；TiDB 不使用 StarRocks AuditLoader 配置，不配置服务端版本。"""

    database_type: ClassVar[Literal["starrocks", "tidb"]] = "tidb"
    audit: None = None


class TiDBError(StarRocksError):
    """复用共用错误分类及治理路径；TiDB 不开放 StarRocks 解析错误恢复。"""

    def __init__(
        self, code: StarRocksErrorCode, *, slot_wait: bool = False, parse_failed: bool = False
    ) -> None:
        super().__init__(code, slot_wait=slot_wait)
        self.args = (ERROR_MESSAGES[code].replace("StarRocks", "TiDB"),)


class TiDBAdapter(StarRocksAdapter):
    error_type = TiDBError
    system_databases = SYSTEM_DATABASES
    diagnostic_sources = DIAGNOSTIC_SOURCES
    schema_objects_sql = SCHEMA_OBJECTS_SQL
    schema_columns_sql = SCHEMA_COLUMNS_SQL
    schema_ids_sql = SCHEMA_IDS_SQL
    object_id_sql = OBJECT_ID_SQL
    ddl_identity_sql = DDL_IDENTITY_SQL
    internal_engines = frozenset({"InnoDB"})
    probe_denied = frozenset({1044, 1049, 1142, 1143, 1146, 1227, 1370, 1356})

    def __init__(
        self, target: TiDBTarget, *, connect: Connector, clock: Callable[[], datetime]
    ) -> None:
        super().__init__(target, connect=connect, clock=clock)
        self._versions: dict[int, str | None] = {}
        self.server_version: str | None = None

    @property
    def target(self) -> TiDBTarget:
        return cast(TiDBTarget, self._target)

    @staticmethod
    def _parse_failure(error: MySQLError) -> bool:
        return False

    async def _prepare_session(self, conn: Connection) -> None:
        t = self.target
        trace = _SQL_IO_TRACE.get()
        if trace is not None:
            trace.started = True
        if len(await conn.execute(VERSION_SQL, None)) != 1:
            raise _SessionMismatchError
        row, extra = await conn.fetch_row(), await conn.fetch_row()
        if row is None or len(row) != 1 or not isinstance(row[0], str) or extra is not None:
            raise _SessionMismatchError
        release = re.search(r"(?m)^Release Version:\s+v(\d+\.\d+\.\d+)\s*$", row[0])
        version = release[1] if release is not None else None
        self._versions[id(conn)] = version
        self.server_version = version
        await conn.execute(
            f"SET SESSION max_execution_time = {t.query_timeout_seconds * 1000}, "
            f"tidb_mem_quota_query = {t.query_mem_limit_bytes}, "
            f"time_zone = '{t.time_zone}', sql_mode = '{SQL_MODE}', "
            "character_set_client = 'utf8mb4', character_set_connection = 'utf8mb4', "
            "character_set_results = 'utf8mb4', collation_connection = 'utf8mb4_bin'",
            None,
        )
        while await conn.fetch_row() is not None:
            pass
        expected = (
            str(t.query_timeout_seconds * 1000),
            str(t.query_mem_limit_bytes),
            t.time_zone,
            SQL_MODE,
            "utf8mb4",
            "utf8mb4",
            "utf8mb4",
            "utf8mb4_bin",
            t.database,
        )
        if len(await conn.execute(SESSION_READ, None)) != len(expected):
            raise _SessionMismatchError
        row, extra = await conn.fetch_row(), await conn.fetch_row()
        if row is None or len(row) != len(expected) or extra is not None:
            raise _SessionMismatchError
        actual = tuple(str(value) for value in row)
        modes = frozenset(actual[3].split(","))
        if (
            actual[:3] != expected[:3]
            or actual[4:] != expected[4:]
            or modes != frozenset(SQL_MODE.split(","))
        ):
            raise _SessionMismatchError
        if version == "7.5.3":
            await conn.execute(PROTECTION_SET, None)
            while await conn.fetch_row() is not None:
                pass
            if len(await conn.execute(PROTECTION_READ, None)) != 1:
                raise _SessionMismatchError
            protected, extra = await conn.fetch_row(), await conn.fetch_row()
            if (
                protected is None
                or len(protected) != 1
                or extra is not None
                or str(protected[0]).upper() not in {"1", "ON"}
            ):
                raise _SessionMismatchError

    async def _result_metadata(
        self, conn: Connection, max_rows: int, truncated: bool
    ) -> _ResultMetadata:
        if truncated:
            # 所有调用均已有 LIMIT。先在同一客户端期限内有界读到 EOF，避免驱动隐式无限排空。
            for _ in range(max_rows + 2):
                if await conn.fetch_row() is None:
                    break
            else:
                raise _ResultContractError
        limit = self.target.policy.max_rows
        # TiDB 不支持 SHOW WARNINGS LIMIT；仍以非缓冲游标按行数/字节/期限有界读取。
        columns = await conn.execute("SHOW WARNINGS", None)
        if columns != ("Level", "Code", "Message"):
            raise _ResultContractError
        rows, clipped = await self._read(conn, columns, limit, False, self._limits)
        warnings: list[dict[str, Scalar]] = []
        for row in rows:
            if (
                not isinstance(row["Level"], str)
                or type(row["Code"]) is not int
                or not isinstance(row["Message"], str)
            ):
                raise _ResultContractError
            warnings.append({"level": row["Level"], "code": row["Code"], "message": row["Message"]})
        return {"warnings": tuple(warnings), "warnings_truncated": clipped}

    async def verify_objects(
        self, objects: Sequence[tuple[str, str]], *, versioned: frozenset[tuple[str, str]]
    ) -> dict[tuple[str, str], ObjectVersion | bool | None]:
        # 历史/重发也要受当前登记范围约束，不能仅凭数据库仍可读就交付已撤销的诊断源。
        for database, name in objects:
            if (database.casefold() in self.system_databases
                    and (database.casefold(), name.casefold()) not in self.diagnostic_sources):
                raise TiDBError(StarRocksErrorCode.OBJECT_NOT_ALLOWED)
        return await super().verify_objects(objects, versioned=versioned)

    async def _probe_one(self, conn: Connection, database: str, name: str) -> bool | None:
        limits = _ReadLimits(
            max_bytes=self.target.schema_limits.max_bytes,
            max_value=self.target.schema_limits.max_bytes,
            zone=self._zone,
        )
        kinds = await self._rows(conn, self.object_type_sql, (database, name), ("type",), limits)
        if not kinds:
            return False
        if kinds[0]["type"] != "VIEW":
            return await super()._probe_one(conn, database, name)
        try:
            if self._versions.get(id(conn)) == "7.5.3":
                sql = "EXPLAIN " + probe_sql(database, name)
                columns = await conn.execute(sql, None)
                rows, clipped = await self._read(
                    conn, columns, self.target.max_plan_lines, False, self._limits
                )
                if not columns or not rows or clipped:
                    raise _ResultContractError
                return True
            columns = await conn.execute(VIEW_DEFINITION_SQL, (database, name))
            if columns != ("definition",):
                raise _ResultContractError
            row, extra = await conn.fetch_row(), await conn.fetch_row()
            if extra is not None:
                raise _ResultContractError
            if row is None or len(row) != 1 or not isinstance(row[0], str):
                return None
            tables = self._legacy_view_tables(row[0], database)
            if tables is None:
                return None
            for key in tables:
                source = await self._rows(conn, self.object_type_sql, key, ("type",), limits)
                if not source or source[0]["type"] == "VIEW":
                    return None
            return await super()._probe_one(conn, database, name)
        except MySQLError as error:
            number = _error_number(error)
            if number == 1345:
                return None
            if self._versions.get(id(conn)) != "7.5.3" and number in {1044, 1142, 1143, 1227, 1370}:
                return None
            if number in self.probe_denied:
                return False
            raise

    def _legacy_view_tables(self, sql: str, database: str) -> frozenset[tuple[str, str]] | None:
        if len(sql.encode()) > self.target.policy.max_sql_bytes:
            return None
        policy = QueryPolicy(
            target_id=self.target.target_id,
            database_type="tidb",
            default_database=database,
            tables={},
            allowed_functions=self.target.policy.allowed_functions,
            max_rows=self.target.policy.max_rows,
            max_sql_bytes=self.target.policy.max_sql_bytes,
            max_result_columns=self.target.policy.max_result_columns,
        )
        try:
            root, _ = _parse(sql, policy)
            if root.find(exp.Exists) is not None or any(
                not (
                    isinstance(node.parent, exp.From | exp.Join)
                    or (isinstance(node.parent, exp.In) and node.arg_key == "query")
                )
                for node in root.find_all(exp.Subquery)
            ):
                return None
            sources = [
                source
                for scope in _scopes(root)
                for _, source in _sources(scope)
                if isinstance(source, exp.Table)
            ]
            if any(source.catalog for source in sources):
                return None
            return frozenset((source.db or database, source.name) for source in sources)
        except QueryRejectedError:
            return None

    async def describe_layout(
        self, database: str, name: str, columns: frozenset[str]
    ) -> QueryResult:
        result = await self._run(
            INDEX_SQL,
            (database, name, self.target.schema_limits.max_columns + 1),
            self.target.schema_limits.max_columns,
        )
        if result.columns != ("index_name", "non_unique", "seq_in_index", "column_name"):
            raise TiDBError(StarRocksErrorCode.RESULT_CONTRACT)
        if result.truncated:
            return result.model_copy(update={"rows": (), "row_count": 0})
        hidden = {row["index_name"] for row in result.rows if row["column_name"] not in columns}
        kept = tuple(row for row in result.rows if row["index_name"] not in hidden)
        return result.model_copy(
            update={"rows": kept, "row_count": len(kept), "truncated": bool(hidden)}
        )

    def can_explain(self, query: ExplainQuery, *, has_view: bool = False) -> bool:
        return self.server_version == "7.5.3" or (
            not has_view
            and self._legacy_view_tables(query.executed_sql, self.target.database) is not None
        )

    async def explain(self, query: ExplainQuery) -> QueryResult:
        if not isinstance(query, ExplainQuery):
            raise TypeError("只对 SQLGuard 产生的 ExplainQuery 取执行计划")
        if query.target_id != self.target.target_id or not self.can_explain(query):
            raise TiDBError(StarRocksErrorCode.OBJECT_NOT_ALLOWED)
        result = await self._run("EXPLAIN " + query.executed_sql, None, self.target.max_plan_lines)
        if result.columns != ("id", "estRows", "task", "access object", "operator info"):
            raise TiDBError(StarRocksErrorCode.RESULT_CONTRACT)
        return result

    async def _execute_statement(
        self, conn: Connection, sql: str, args: tuple[object, ...] | None
    ) -> tuple[str, ...]:
        if sql.startswith("EXPLAIN ") and self._versions.get(id(conn)) != "7.5.3":
            # 所连节点可能不同于快照采集节点；在实际连接上复核旧版预求值边界。
            tables = self._legacy_view_tables(sql[len("EXPLAIN "):], self.target.database)
            if tables is None:
                raise _SessionMismatchError
            for key in tables:
                kinds = await self._rows(conn, self.object_type_sql, key, ("type",), self._limits)
                if not kinds or kinds[0]["type"] == "VIEW":
                    raise _SessionMismatchError
        return await conn.execute(sql, args)

    async def _release(self, conn: Connection, *, complete: bool) -> None:
        try:
            await super()._release(conn, complete=complete)
        finally:
            self._versions.pop(id(conn), None)


def open_tidb(target: TiDBTarget, *, clock: Callable[[], datetime]) -> TiDBAdapter:
    return TiDBAdapter(target, connect=mysql_connector(target), clock=clock)
