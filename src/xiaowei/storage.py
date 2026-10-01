"""PostgreSQL 引擎生命周期、SDK Session 表与应用表的显式初始化/升级、单实例锁及就绪检查。

SDK 表由 ``SQLAlchemySession`` 的公开建表路径管理；应用表以 ``xiaowei_`` 为前缀，由包内
版本化 SQL 建立并记录版本，二者互不改写。全新初始化顺序执行全部迁移；已有旧版本只由显式
``upgrade_storage`` 升级，普通初始化、就绪检查与请求路径从不升级或降级。

``serve`` 用一条专用连接在进程生命周期内持有会话级实例锁（``hold_instance_lock``）；初始化在
任何建表前取得同一把会话级锁，升级在事务内尝试取得同一键的事务级锁，取不到即拒绝，因此
不能与在线实例或彼此并发修改数据库。数据库
URL 只来自私有配置，错误信息不携带 URL、主机或原始驱动异常。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.resources import files

from agents.extensions.memory import SQLAlchemySession
from pydantic import SecretStr
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

SDK_SESSIONS_TABLE = "agent_sessions"
SDK_MESSAGES_TABLE = "agent_messages"
_SDK_TABLES = frozenset({SDK_SESSIONS_TABLE, SDK_MESSAGES_TABLE})

APP_SCHEMA_VERSION = 2
APP_TABLES = frozenset(
    {
        "xiaowei_schema_version",
        "xiaowei_evidence",
        "xiaowei_session",
        "xiaowei_channel_session",
        "xiaowei_request",
    }
)
# 版本 n 由第 n 个迁移建立；升级只按顺序执行当前版本之后的迁移。
_MIGRATIONS = ("migrations/001_initial.sql", "migrations/002_p1b_channels.sql")
# 实例锁：会话级（serve）与事务级（初始化、升级）共用同一个键，二者互斥。锁按数据库区分。
_INSTANCE_LOCK_KEY = 0x7869_6177_6569_0001

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


class StorageBusyError(StorageError):
    """另一个小维进程持有该数据库的实例锁；不能并存运行或在线维护。"""

    def __init__(self) -> None:
        super().__init__("另一个小维进程正在使用该数据库，请先停止它")


class Readiness:
    """进程就绪状态：任一关键状态无法持久化或实例锁丢失时永久锁低，只能停止并重启进程。"""

    def __init__(self) -> None:
        self._reason: str | None = None

    @property
    def ok(self) -> bool:
        return self._reason is None

    @property
    def reason(self) -> str | None:
        return self._reason

    def lock(self, reason: str) -> None:
        if self._reason is None:
            self._reason = reason


@dataclass
class InstanceLock:
    """持有实例锁的专用连接。只有持锁的 ``serve`` 启动阶段用它执行一致性恢复。

    连接一旦终止，锁即随之释放、另一进程可以取得：``lost`` 由驱动的连接终止通知立即设置并同时
    锁低 readiness，不等下一次 ``verify``。
    """

    connection: AsyncConnection
    readiness: Readiness
    lost: asyncio.Event = field(default_factory=asyncio.Event)

    def mark_lost(self) -> None:
        self.readiness.lock("instance_lock_lost")
        self.lost.set()

    async def verify(self) -> None:
        """确认持锁连接仍然有效；连接丢失时锁已随之释放，锁低 readiness 并拒绝继续服务。"""
        try:
            await self.connection.execute(text("SELECT 1"))
            await self.connection.commit()
        except (OSError, SQLAlchemyError):
            self.mark_lost()
            raise StorageUnavailableError("实例锁连接已断开，进程必须停止") from None


@asynccontextmanager
async def hold_instance_lock(
    engine: AsyncEngine, readiness: Readiness
) -> AsyncIterator[InstanceLock]:
    """取得会话级实例锁并在退出时释放；锁被占用时拒绝。"""
    try:
        conn = await engine.connect()
    except (OSError, SQLAlchemyError):
        raise StorageUnavailableError("PostgreSQL 不可用") from None
    try:
        try:
            acquired = await conn.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _INSTANCE_LOCK_KEY}
            )
            # 会话级锁不随事务结束释放；提交只是结束本条查询的隐式事务。
            await conn.commit()
        except (OSError, SQLAlchemyError):
            raise StorageUnavailableError("PostgreSQL 不可用") from None
        if not acquired:
            raise StorageBusyError
        lock = InstanceLock(conn, readiness)
        try:
            driver = (await conn.get_raw_connection()).driver_connection
        except (OSError, SQLAlchemyError):
            driver = None
        if driver is None:
            raise StorageUnavailableError("PostgreSQL 不可用")

        def terminated(_: object) -> None:
            lock.mark_lost()

        # asyncpg 公开的连接终止通知：服务端终止或网络断开时立即回调。主动释放前先注销，
        # 正常关闭不算丢锁。
        driver.add_termination_listener(terminated)
        try:
            yield lock
        finally:
            driver.remove_termination_listener(terminated)
    finally:
        # 关闭连接即释放会话级锁；连接已断开时关闭也不会再持有它。
        try:
            await conn.invalidate()
        except (OSError, SQLAlchemyError):
            pass
        await conn.close()


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
    """显式部署入口：取得实例锁后建立 SDK Session 表与应用表，再做就绪检查。

    任何建表前先在专用连接上取得会话级实例锁并持有到建表结束：锁被在线实例或另一个维护命令
    持有时拒绝，且不修改数据库。``SQLAlchemySession`` 只在构造参数 ``create_tables=True`` 时
    于首次读写前建表；这里用一个只读探针会话触发它，不写入任何会话数据。应用表在持锁连接上
    的一个事务内、只在尚未安装时顺序执行全部迁移；已安装但版本不符时由就绪检查拒绝，不自动
    升级。普通请求路径不调用本函数。
    """
    probe = SQLAlchemySession(
        _INIT_PROBE_SESSION_ID,
        engine=engine,
        create_tables=True,
        sessions_table=SDK_SESSIONS_TABLE,
        messages_table=SDK_MESSAGES_TABLE,
    )
    async with hold_instance_lock(engine, Readiness()) as lock:
        try:
            await probe.get_items(limit=0)
            await _install_app_schema(lock.connection)
        except (OSError, SQLAlchemyError):
            raise StorageUnavailableError("PostgreSQL 不可用，初始化未完成") from None
    await check_storage(engine)


async def upgrade_storage(engine: AsyncEngine) -> int:
    """显式升级入口：在实例锁与一个事务内把应用表从旧版本升到当前版本，返回升级后的版本。

    已是当前版本时不做任何修改；未初始化或版本未知（含高于本程序）时拒绝。任一语句失败整体
    回滚，版本号保持不变。升级前的备份由操作者负责。
    """
    try:
        async with engine.begin() as conn:
            await _lock_for_maintenance(conn)
            if await conn.scalar(text("SELECT to_regclass('xiaowei_schema_version')")) is None:
                raise StorageNotInitializedError("PostgreSQL 未初始化应用表，请先执行初始化")
            version = await conn.scalar(text("SELECT version FROM xiaowei_schema_version"))
            if not isinstance(version, int) or not 1 <= version <= APP_SCHEMA_VERSION:
                raise StorageVersionMismatchError("应用表版本未知，不能升级")
            await _apply_migrations(conn, after=version)
    except (OSError, SQLAlchemyError):
        raise StorageUnavailableError("PostgreSQL 不可用或升级失败，已回滚") from None
    await check_storage(engine)
    return APP_SCHEMA_VERSION


async def _install_app_schema(conn: AsyncConnection) -> None:
    """在已持有会话级实例锁的连接上用一个事务顺序执行全部迁移；已安装则不做任何修改。"""
    async with conn.begin():
        installed = await conn.scalar(text("SELECT to_regclass('xiaowei_schema_version')"))
        if installed is None:
            await _apply_migrations(conn, after=0)


async def _lock_for_maintenance(conn: AsyncConnection) -> None:
    """事务级尝试取得实例锁：在线 serve 或另一个维护命令持有时拒绝，不排队等待。"""
    acquired = await conn.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _INSTANCE_LOCK_KEY}
    )
    if not acquired:
        raise StorageBusyError


async def _apply_migrations(conn: AsyncConnection, *, after: int) -> None:
    """按顺序执行版本 ``after`` 之后的迁移。

    SQLAlchemy 的 asyncpg 适配层按预编译语句执行、不支持一次多语句，因此按行尾分号拆分；
    迁移文件因此不使用函数体等内含分号的语句。PostgreSQL 的 DDL 可回滚，失败不留半套表。
    """
    for name in _MIGRATIONS[after:]:
        script = files("xiaowei").joinpath(name).read_text(encoding="utf-8")
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
            if "xiaowei_schema_version" in present:
                result = await conn.execute(text("SELECT version FROM xiaowei_schema_version"))
                version = result.scalar_one_or_none()
    except (OSError, SQLAlchemyError):
        raise StorageUnavailableError("PostgreSQL 不可用") from None
    # 先判断版本：旧版本缺少新表属于“需要显式升级”，不是“未初始化”。
    if version is not None and version != APP_SCHEMA_VERSION:
        raise StorageVersionMismatchError("应用表版本与程序不符，需按升级说明处理")
    if not (_SDK_TABLES | APP_TABLES) <= present or version is None:
        raise StorageNotInitializedError("PostgreSQL 未初始化 SDK Session 表或应用表")


def _table_names(conn: Connection) -> frozenset[str]:
    return frozenset(inspect(conn).get_table_names())
