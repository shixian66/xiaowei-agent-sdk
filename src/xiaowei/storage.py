"""PostgreSQL 引擎生命周期、SDK Session 表与应用表的显式初始化及运行时就绪检查。

SDK 表由 ``SQLAlchemySession`` 的公开建表路径管理；应用表以 ``xiaowei_`` 为前缀，由包内
版本化 SQL 建立并记录版本，二者互不改写。数据库 URL 只来自私有配置，错误信息不携带 URL、
主机或原始驱动异常。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files

from agents.extensions.memory import SQLAlchemySession
from pydantic import SecretStr
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

SDK_SESSIONS_TABLE = "agent_sessions"
SDK_MESSAGES_TABLE = "agent_messages"
_SDK_TABLES = frozenset({SDK_SESSIONS_TABLE, SDK_MESSAGES_TABLE})

APP_SCHEMA_VERSION = 1
APP_TABLES = frozenset({"xiaowei_schema_version", "xiaowei_evidence"})
_APP_MIGRATION = "migrations/001_initial.sql"

_DRIVER = "postgresql+asyncpg"
_CONNECT_TIMEOUT_SECONDS = 5.0
_COMMAND_TIMEOUT_SECONDS = 10.0
_POOL_SIZE = 5
_POOL_TIMEOUT_SECONDS = 5.0
_INIT_PROBE_SESSION_ID = "__xiaowei_storage_init__"


class StorageError(Exception):
    """存储边界错误；消息固定，不含连接信息。"""


class StorageUnavailableError(StorageError):
    """PostgreSQL 无法连接或无法完成检查。"""


class StorageNotInitializedError(StorageError):
    """PostgreSQL 可用，但缺少 SDK Session 表或应用表；需要先执行显式初始化。"""


class StorageVersionMismatchError(StorageError):
    """应用表版本与本程序不符；不在请求路径或初始化中自动升级、降级。"""


@asynccontextmanager
async def open_engine(database_url: SecretStr) -> AsyncIterator[AsyncEngine]:
    """创建有界连接池的异步引擎，退出时释放全部连接。"""
    try:
        url = make_url(database_url.get_secret_value())
    except ArgumentError:
        raise ValueError("数据库 URL 格式无效") from None
    if url.drivername != _DRIVER:
        raise ValueError(f"数据库 URL 必须使用 {_DRIVER}")

    engine = create_async_engine(
        url,
        pool_size=_POOL_SIZE,
        max_overflow=0,
        pool_timeout=_POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "timeout": _CONNECT_TIMEOUT_SECONDS,
            "command_timeout": _COMMAND_TIMEOUT_SECONDS,
        },
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def initialize_storage(engine: AsyncEngine) -> None:
    """显式部署入口：建立 SDK Session 表与应用表，再做就绪检查。

    ``SQLAlchemySession`` 只在构造参数 ``create_tables=True`` 时于首次读写前建表；这里用一个
    只读探针会话触发它，不写入任何会话数据。应用表只在尚未安装时执行 v1 SQL；已安装但版本
    不符时由就绪检查拒绝，不自动升级。普通请求路径不调用本函数。
    """
    probe = SQLAlchemySession(
        _INIT_PROBE_SESSION_ID,
        engine=engine,
        create_tables=True,
        sessions_table=SDK_SESSIONS_TABLE,
        messages_table=SDK_MESSAGES_TABLE,
    )
    try:
        await probe.get_items(limit=0)
        await _install_app_schema(engine)
    except (OSError, SQLAlchemyError):
        raise StorageUnavailableError("PostgreSQL 不可用，初始化未完成") from None
    await check_storage(engine)


async def _install_app_schema(engine: AsyncEngine) -> None:
    """在一个事务内逐条执行版本化 SQL；咨询锁串行化并发初始化，已安装则不重复执行。

    SQLAlchemy 的 asyncpg 适配层按预编译语句执行、不支持一次多语句，因此按行尾分号拆分；
    迁移文件因此不使用函数体等内含分号的语句。PostgreSQL 的 DDL 可回滚，失败不留半套表。
    """
    script = files("xiaowei").joinpath(_APP_MIGRATION).read_text(encoding="utf-8")
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('xiaowei_app_schema'))"))
        installed = await conn.scalar(text("SELECT to_regclass('xiaowei_schema_version')"))
        if installed is None:
            for statement in _statements(script):
                await conn.execute(text(statement))


def _statements(script: str) -> list[str]:
    return [chunk.strip() for chunk in script.split(";\n") if chunk.strip()]


async def check_storage(engine: AsyncEngine) -> None:
    """运行时就绪检查：数据库可连接、SDK 与应用表存在且应用表版本相符，否则拒绝新轮次。"""
    try:
        async with engine.connect() as conn:
            present = await conn.run_sync(_table_names)
            version = None
            if APP_TABLES <= present:
                result = await conn.execute(text("SELECT version FROM xiaowei_schema_version"))
                version = result.scalar_one_or_none()
    except (OSError, SQLAlchemyError):
        raise StorageUnavailableError("PostgreSQL 不可用") from None
    if not (_SDK_TABLES | APP_TABLES) <= present:
        raise StorageNotInitializedError("PostgreSQL 未初始化 SDK Session 表或应用表")
    if version != APP_SCHEMA_VERSION:
        raise StorageVersionMismatchError("应用表版本与程序不符，需按升级说明处理")


def _table_names(conn: Connection) -> frozenset[str]:
    return frozenset(inspect(conn).get_table_names())
