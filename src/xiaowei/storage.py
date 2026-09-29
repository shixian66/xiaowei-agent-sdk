"""PostgreSQL 引擎生命周期、SDK Session 表的显式初始化与运行时就绪检查。

本模块只处理 SDK ``SQLAlchemySession`` 的表；应用表在后续任务加入，并使用独立命名。
数据库 URL 只来自私有配置，错误信息不携带 URL、主机或原始驱动异常。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agents.extensions.memory import SQLAlchemySession
from pydantic import SecretStr
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

SDK_SESSIONS_TABLE = "agent_sessions"
SDK_MESSAGES_TABLE = "agent_messages"
_SDK_TABLES = frozenset({SDK_SESSIONS_TABLE, SDK_MESSAGES_TABLE})

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
    """PostgreSQL 可用，但缺少 SDK Session 表；需要先执行显式初始化。"""


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
    """显式部署入口：通过 SDK 公开的 ``create_tables`` 路径建立 Session 表。

    ``SQLAlchemySession`` 只在构造参数 ``create_tables=True`` 时于首次读写前建表；这里用一个
    只读探针会话触发它，不写入任何会话数据。普通请求路径不调用本函数。
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
    except (OSError, SQLAlchemyError):
        raise StorageUnavailableError("PostgreSQL 不可用，初始化未完成") from None
    await check_storage(engine)


async def check_storage(engine: AsyncEngine) -> None:
    """运行时就绪检查：数据库可连接且 SDK Session 表存在，否则拒绝新轮次。"""
    try:
        async with engine.connect() as conn:
            present = await conn.run_sync(_table_names)
    except (OSError, SQLAlchemyError):
        raise StorageUnavailableError("PostgreSQL 不可用") from None
    if not _SDK_TABLES <= present:
        raise StorageNotInitializedError("PostgreSQL 未初始化 SDK Session 表")


def _table_names(conn: Connection) -> frozenset[str]:
    return frozenset(inspect(conn).get_table_names())
