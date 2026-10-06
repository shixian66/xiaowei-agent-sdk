"""PostgreSQL 引擎生命周期、SDK Session 表与应用表的显式初始化/升级、单实例锁及就绪检查。

SDK 表由 ``SQLAlchemySession`` 的公开建表路径管理；应用表以 ``xiaowei_`` 为前缀，由包内
版本化 SQL 建立并记录版本，二者互不改写。全新初始化顺序执行全部迁移；已有旧版本只由显式
``upgrade_storage`` 升级，普通初始化、就绪检查与请求路径从不升级或降级。

``serve`` 用一条专用连接在进程生命周期内持有会话级实例锁（``hold_instance_lock``）；初始化在
任何建表前取得同一把会话级锁，升级在事务内尝试取得同一键的事务级锁，取不到即拒绝，因此
不能与在线实例或彼此并发修改数据库。请求接收与接管恢复另用一个屏障键交接
（``InstanceLock.admits``）。数据库 URL 只来自私有配置，错误信息不携带 URL、主机或原始驱动异常。
"""

import asyncio
import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
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

APP_SCHEMA_VERSION = 6
APP_TABLES = frozenset(
    {
        "xiaowei_schema_version",
        "xiaowei_installation",
        "xiaowei_evidence",
        "xiaowei_session",
        "xiaowei_channel_session",
        "xiaowei_request",
    }
)
# 版本 n 由第 n 个迁移建立；升级只按顺序执行当前版本之后的迁移。
_MIGRATIONS = (
    "migrations/001_initial.sql",
    "migrations/002_p1b_channels.sql",
    "migrations/003_delivery_attempt.sql",
    "migrations/004_evidence_dependencies.sql",
    "migrations/005_group_ownership.sql",
    "migrations/006_digest_key_binding.sql",
)
# 实例锁：会话级（serve）与事务级（初始化、升级）共用同一个键，二者互斥。锁按数据库区分。
_INSTANCE_LOCK_KEY = 0x7869_6177_6569_0001
# 接收屏障：请求接收事务持共享锁，接管恢复持排他锁（见 ``InstanceLock.admits``）。
_ACCEPT_BARRIER_KEY = 0x7869_6177_6569_0002

_DRIVER = "postgresql+asyncpg"
_CONNECT_TIMEOUT_SECONDS = 5.0
_COMMAND_TIMEOUT_SECONDS = 10.0
_POOL_SIZE = 5
_POOL_TIMEOUT_SECONDS = 5.0
_INIT_PROBE_SESSION_ID = "__xiaowei_storage_init__"
_DIGEST_KEY_BINDING_LABEL = b"xiaowei:storage:digest-key:v1"
_DIGEST_KEY_BINDING_FORMAT = 1
_LOCK_CLOSE_TIMEOUT_SECONDS = 5.0
_ENGINE_DISPOSE_TIMEOUT_SECONDS = 10.0


class StorageError(Exception):
    """存储边界错误；消息固定，不含连接信息。"""


class StorageUnavailableError(StorageError):
    """PostgreSQL 无法连接或无法完成检查。"""


class StorageNotInitializedError(StorageError):
    """PostgreSQL 可用，但缺少 SDK Session 表或应用表；需要先执行显式初始化。"""


class StorageVersionMismatchError(StorageError):
    """应用表版本与本程序不符；不在请求路径或初始化中自动升级、降级。"""


class StorageDigestKeyMismatchError(StorageError):
    """数据库绑定的摘要密钥与当前配置不一致，或绑定记录已损坏。"""

    def __init__(self) -> None:
        super().__init__("摘要密钥与数据库不匹配")


class StorageDigestKeyBindingRequiredError(StorageError):
    """旧 schema 尚无可验证的摘要密钥绑定，需要操作者明确确认。"""

    def __init__(self) -> None:
        super().__init__("旧数据库需要明确确认旧摘要密钥后才能升级")


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


@dataclass(frozen=True)
class Backend:
    """一条数据库连接在服务端的身份：pid 与 backend_start（pid 复用时 backend_start 不同）。"""

    pid: int
    started: datetime


_SELF = text("SELECT pid, backend_start FROM pg_stat_activity WHERE pid = pg_backend_pid()")
_ALIVE = text("SELECT 1 FROM pg_stat_activity WHERE pid = :pid AND backend_start = :started")
_RELEASE_TIMEOUT_SECONDS = 5.0
_RELEASE_POLL_SECONDS = 0.05


class BackendNotReleasedError(StorageError):
    """专用连接已丢弃，但无法确认其数据库后端已退出。"""

    def __init__(self) -> None:
        super().__init__("无法确认数据库连接已释放")


@asynccontextmanager
async def hold_backend(engine: AsyncEngine) -> AsyncIterator[Backend]:
    """占用一条专用连接直到退出，返回它的身份。

    用于标记不持实例锁的工作（显式重发）仍在进行：连接存活即工作仍有所有者，进程退出或连接
    断开后身份随之消失。退出时丢弃物理连接（归还连接池不会结束后端，恢复会误以为工作仍在
    进行），并确认该身份已从 ``pg_stat_activity`` 消失；正常退出时无法确认即报错，异常或取消
    退出时原样传播原异常。
    """
    try:
        conn = await engine.connect()
    except (OSError, SQLAlchemyError):
        raise StorageUnavailableError("PostgreSQL 不可用") from None
    try:
        row = (await conn.execute(_SELF)).one()
        await conn.commit()
    except (OSError, SQLAlchemyError):
        await _discard(conn)
        raise StorageUnavailableError("PostgreSQL 不可用") from None
    except BaseException:
        await _discard(conn)
        raise
    backend = Backend(row.pid, row.backend_start)
    try:
        yield backend
    except BaseException:
        await _release(engine, conn, backend)
        raise
    if not await _release(engine, conn, backend):
        raise BackendNotReleasedError


async def _discard(conn: AsyncConnection) -> None:
    """丢弃物理连接：先作废再关闭，连接不回到连接池。"""
    try:
        await conn.invalidate()
    except (OSError, SQLAlchemyError):
        pass
    await conn.close()


async def _release(engine: AsyncEngine, conn: AsyncConnection, backend: Backend) -> bool:
    """丢弃专用连接，并在期限内确认其后端已退出；无法确认时返回 False。"""
    await _discard(conn)
    params = {"pid": backend.pid, "started": backend.started}
    try:
        async with asyncio.timeout(_RELEASE_TIMEOUT_SECONDS), engine.connect() as check:
            while (await check.scalar(_ALIVE, params)) is not None:
                await check.commit()  # pg_stat_activity 在事务内是快照，重新读取前先结束事务
                await asyncio.sleep(_RELEASE_POLL_SECONDS)
    except (TimeoutError, OSError, SQLAlchemyError):
        return False
    return True


@dataclass
class InstanceLock:
    """持有实例锁的专用连接。只有持锁的 ``serve`` 启动阶段用它执行一致性恢复。

    连接一旦终止，锁即随之释放、另一进程可以取得：``lost`` 由驱动的连接终止通知立即设置并同时
    锁低 readiness，不等下一次 ``verify``。``pid`` 与 ``started`` 是持锁后端在数据库中的身份，
    接收事务据此在数据库内确认本进程仍持有实例锁（``admits``）。
    """

    connection: AsyncConnection
    readiness: Readiness
    pid: int
    started: datetime
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
    async def watching(self) -> AsyncIterator[None]:
        """持锁期间接收 asyncpg 公开的连接终止通知；退出前先注销，正常关闭不算丢锁。

        连接若在注册之前已经关闭，通知已经错过：注册后立即复核关闭状态，按丢锁处理并拒绝启动，
        不留给周期核对。
        """
        try:
            driver = (await self.connection.get_raw_connection()).driver_connection
        except (OSError, SQLAlchemyError):
            driver = None
        if driver is None:
            self.mark_lost()
            raise StorageUnavailableError("实例锁连接已断开")

        def terminated(_: object) -> None:
            self.mark_lost()

        driver.add_termination_listener(terminated)
        try:
            if driver.is_closed():
                self.mark_lost()
                raise StorageUnavailableError("实例锁连接已断开")
            yield
        finally:
            driver.remove_termination_listener(terminated)

    async def admits(self, conn: AsyncConnection) -> bool:
        """在请求接收事务内调用：与接管恢复互斥，并确认实例锁仍由本进程的持锁后端持有。

        先取得接收屏障的事务级共享锁并保持到事务结束，再在数据库内核对锁的持有者。新实例的接管
        恢复在取得实例锁之后、读取请求之前取得同一屏障的排他锁（``exclude_accepts``），因此：
        核对时锁仍属本进程的接收事务，恢复会等它提交后再读取；核对晚于接管的，持有者已变，
        拒绝并按丢锁处理。只靠进程内 readiness 不够，检查与提交之间仍可能发生接管。
        """
        await conn.execute(_SHARE_ACCEPT_BARRIER, {"barrier": _ACCEPT_BARRIER_KEY})
        held = await conn.scalar(
            _HOLDS_INSTANCE_LOCK,
            {
                "classid": _INSTANCE_LOCK_KEY >> 32,
                "objid": _INSTANCE_LOCK_KEY & 0xFFFF_FFFF,
                "pid": self.pid,
                "started": self.started,
            },
        )
        if not held:
            self.mark_lost()
        return bool(held)

    async def exclude_accepts(self) -> None:
        """在持锁连接的恢复事务内调用：等待仍在提交的接收事务结束，并在本事务内阻止新的接收。"""
        await self.connection.execute(_EXCLUDE_ACCEPTS, {"barrier": _ACCEPT_BARRIER_KEY})


_ACQUIRE_INSTANCE_LOCK = text(
    """
    SELECT pg_try_advisory_lock(:key) AS acquired, pid, backend_start AS started
    FROM pg_stat_activity WHERE pid = pg_backend_pid()
    """
)
# bigint 键的 advisory 锁在 pg_locks 中拆为 classid（高 32 位）与 objid（低 32 位），objsubid = 1。
_HOLDS_INSTANCE_LOCK = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM pg_locks AS l JOIN pg_stat_activity AS a ON a.pid = l.pid
        WHERE l.locktype = 'advisory' AND l.granted AND l.mode = 'ExclusiveLock'
          AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND l.classid = CAST(:classid AS oid) AND l.objid = CAST(:objid AS oid)
          AND l.objsubid = 1 AND l.pid = :pid AND a.backend_start = :started
    )
    """
)
_SHARE_ACCEPT_BARRIER = text("SELECT pg_advisory_xact_lock_shared(:barrier)")
_EXCLUDE_ACCEPTS = text("SELECT pg_advisory_xact_lock(:barrier)")


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
            row = (
                (await conn.execute(_ACQUIRE_INSTANCE_LOCK, {"key": _INSTANCE_LOCK_KEY}))
                .mappings()
                .one()
            )
            # 会话级锁不随事务结束释放；提交只是结束本条查询的隐式事务。
            await conn.commit()
        except (OSError, SQLAlchemyError):
            raise StorageUnavailableError("PostgreSQL 不可用") from None
        if not row["acquired"]:
            raise StorageBusyError
        lock = InstanceLock(conn, readiness, row["pid"], row["started"])
        async with lock.watching():
            yield lock
    finally:
        # 关闭连接即释放会话级锁；连接已断开时关闭也不会再持有它。
        try:
            async with asyncio.timeout(_LOCK_CLOSE_TIMEOUT_SECONDS):
                try:
                    await conn.invalidate()
                except (OSError, SQLAlchemyError):
                    pass
                await conn.close()
        except TimeoutError:
            raise StorageUnavailableError("实例锁连接关闭超时") from None


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
        try:
            async with asyncio.timeout(_ENGINE_DISPOSE_TIMEOUT_SECONDS):
                await engine.dispose()
        except TimeoutError:
            raise StorageUnavailableError("PostgreSQL 连接池关闭超时") from None


async def initialize_storage(engine: AsyncEngine, *, digest_key: SecretStr) -> None:
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
            await _install_app_schema(lock.connection, digest_key=digest_key)
        except (OSError, SQLAlchemyError):
            raise StorageUnavailableError("PostgreSQL 不可用，初始化未完成") from None
    await check_storage(engine)
    await check_digest_key(engine, digest_key)


async def upgrade_storage(
    engine: AsyncEngine,
    *,
    digest_key: SecretStr,
    bind_existing_digest_key: bool = False,
) -> int:
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
            if version < APP_SCHEMA_VERSION:
                if not bind_existing_digest_key:
                    raise StorageDigestKeyBindingRequiredError
                await _apply_migrations(conn, after=version)
                await _write_digest_key_binding(conn, digest_key)
            else:
                await _check_digest_key_binding(conn, digest_key)
    except (OSError, SQLAlchemyError):
        raise StorageUnavailableError("PostgreSQL 不可用或升级失败，已回滚") from None
    await check_storage(engine)
    await check_digest_key(engine, digest_key)
    return APP_SCHEMA_VERSION


async def _install_app_schema(conn: AsyncConnection, *, digest_key: SecretStr) -> None:
    """在已持有会话级实例锁的连接上用一个事务顺序执行全部迁移；已安装则不做任何修改。"""
    async with conn.begin():
        installed = await conn.scalar(text("SELECT to_regclass('xiaowei_schema_version')"))
        if installed is None:
            await _apply_migrations(conn, after=0)
            await _write_digest_key_binding(conn, digest_key)
            return
        version = await conn.scalar(text("SELECT version FROM xiaowei_schema_version"))
        if version != APP_SCHEMA_VERSION:
            raise StorageVersionMismatchError("应用表版本与程序不符，需按升级说明处理")
        await _check_digest_key_binding(conn, digest_key)


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


async def check_digest_key(engine: AsyncEngine, digest_key: SecretStr) -> None:
    """核对数据库的摘要密钥绑定；只读取固定指纹，不读取或保存密钥。"""
    try:
        async with engine.connect() as conn:
            await _check_digest_key_binding(conn, digest_key)
    except (OSError, SQLAlchemyError):
        raise StorageUnavailableError("PostgreSQL 不可用") from None


async def _write_digest_key_binding(conn: AsyncConnection, digest_key: SecretStr) -> None:
    await conn.execute(
        text(
            "INSERT INTO xiaowei_installation (format_version, digest_key_fingerprint) "
            "VALUES (:format_version, :fingerprint)"
        ),
        {
            "format_version": _DIGEST_KEY_BINDING_FORMAT,
            "fingerprint": _digest_key_fingerprint(digest_key),
        },
    )


async def _check_digest_key_binding(conn: AsyncConnection, digest_key: SecretStr) -> None:
    installed = await conn.scalar(text("SELECT to_regclass('xiaowei_installation')"))
    if installed is None:
        raise StorageDigestKeyMismatchError
    rows = (
        await conn.execute(
            text("SELECT format_version, digest_key_fingerprint FROM xiaowei_installation")
        )
    ).all()
    expected = _digest_key_fingerprint(digest_key)
    if (
        len(rows) != 1
        or rows[0].format_version != _DIGEST_KEY_BINDING_FORMAT
        or not isinstance(rows[0].digest_key_fingerprint, str)
        or not hmac.compare_digest(rows[0].digest_key_fingerprint, expected)
    ):
        raise StorageDigestKeyMismatchError


def _digest_key_fingerprint(digest_key: SecretStr) -> str:
    return hmac.new(
        digest_key.get_secret_value().encode("utf-8"),
        _DIGEST_KEY_BINDING_LABEL,
        hashlib.sha256,
    ).hexdigest()


def _table_names(conn: Connection) -> frozenset[str]:
    return frozenset(inspect(conn).get_table_names())
