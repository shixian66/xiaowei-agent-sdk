"""P1-B Task 4：应用表 v2 的显式初始化与升级、版本双向拒绝和单实例锁。真实 PostgreSQL。

普通初始化与就绪检查不升级 v1；只有显式 ``upgrade_storage`` 在实例锁与事务中把 v1 升到 v2，
失败整体回滚。``serve`` 用专用连接持有会话级实例锁，初始化与升级必须独占同一把锁。
"""

import asyncio
from importlib.resources import files

import pytest
from agents.extensions.memory import SQLAlchemySession
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.sdk_core.synthetic_tools import secret

from xiaowei.storage import (
    APP_SCHEMA_VERSION,
    APP_TABLES,
    SDK_MESSAGES_TABLE,
    SDK_SESSIONS_TABLE,
    InstanceLock,
    Readiness,
    StorageBusyError,
    StorageNotInitializedError,
    StorageUnavailableError,
    StorageVersionMismatchError,
    check_storage,
    hold_instance_lock,
    initialize_storage,
    open_engine,
    upgrade_storage,
)

pytestmark = pytest.mark.loopback

_V2_TABLES = {"xiaowei_channel_session", "xiaowei_request"}


def _migration(name: str) -> str:
    return files("xiaowei").joinpath(f"migrations/{name}").read_text(encoding="utf-8")


async def _install_v1(engine: AsyncEngine, *, through: str = "001_initial.sql") -> None:
    """模拟旧部署：SDK 表与按顺序执行到 ``through`` 的应用表迁移（默认只有 001，即 P1-A）。"""
    probe = SQLAlchemySession("probe", engine=engine, create_tables=True)
    await probe.get_items(limit=0)
    names = ["001_initial.sql", "002_p1b_channels.sql"]
    async with engine.begin() as conn:
        for name in names[: names.index(through) + 1]:
            for chunk in _migration(name).split(";\n"):
                if chunk.strip():
                    await conn.execute(text(chunk.strip()))


async def _version(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT version FROM xiaowei_schema_version"))).one()[0]
        )


async def _tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))


def test_v2_migration_only_touches_application_tables() -> None:
    for name in ("002_p1b_channels.sql", "003_delivery_attempt.sql"):
        assert "agent_" not in _migration(name)
    assert APP_SCHEMA_VERSION == 3
    assert _V2_TABLES <= APP_TABLES
    assert all(name.startswith("xiaowei_") for name in APP_TABLES)


async def test_fresh_initialization_installs_the_current_version(postgres_url: URL) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await initialize_storage(engine)
        await initialize_storage(engine)  # 重复执行不重复建表
        await check_storage(engine)
        assert await _version(engine) == 3
        assert await _tables(engine) == {SDK_SESSIONS_TABLE, SDK_MESSAGES_TABLE, *APP_TABLES}
        assert await upgrade_storage(engine) == 3  # 已是当前版本：显式升级无变化


async def test_v1_is_rejected_until_explicit_upgrade(postgres_url: URL) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine)
        with pytest.raises(StorageVersionMismatchError):
            await check_storage(engine)
        # 普通初始化不把 v1 升级：版本与表都不变。
        with pytest.raises(StorageVersionMismatchError):
            await initialize_storage(engine)
        assert await _version(engine) == 1
        assert not _V2_TABLES & await _tables(engine)

        assert await upgrade_storage(engine) == 3
        assert await upgrade_storage(engine) == 3  # 重复命令无变化
        await check_storage(engine)
        assert _V2_TABLES <= await _tables(engine)


async def test_unknown_or_missing_versions_are_never_upgraded(postgres_url: URL) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        with pytest.raises(StorageNotInitializedError):
            await upgrade_storage(engine)
        await initialize_storage(engine)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE xiaowei_schema_version SET version = 99"))
        for operation in (check_storage, initialize_storage, upgrade_storage):
            with pytest.raises(StorageVersionMismatchError):
                await operation(engine)
        assert await _version(engine) == 99


async def test_initialization_advances_the_schema_version(postgres_url: URL) -> None:
    """全新初始化把版本推进到 3。旧程序只接受各自的版本由基线源码保证，这里不复制旧实现。"""
    async with open_engine(secret(postgres_url)) as engine:
        await initialize_storage(engine)
        assert await _version(engine) == APP_SCHEMA_VERSION == 3


async def test_failed_upgrade_rolls_back_completely(postgres_url: URL) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine)
        # 迁移的第二个表名被占用：第一张表已建立后失败，整个事务回滚。
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE xiaowei_request (x int)"))
        with pytest.raises(StorageUnavailableError):
            await upgrade_storage(engine)
        assert await _version(engine) == 1
        assert "xiaowei_channel_session" not in await _tables(engine)


async def test_concurrent_upgrades_run_the_migration_once(postgres_url: URL) -> None:
    async with (
        open_engine(secret(postgres_url)) as first,
        open_engine(secret(postgres_url)) as second,
    ):
        await _install_v1(first)
        results = await asyncio.gather(
            upgrade_storage(first), upgrade_storage(second), return_exceptions=True
        )
        # 实例锁只让一个进程执行；另一个要么看到已完成的升级，要么因锁被占用而拒绝。
        assert any(r == 3 for r in results)
        assert all(r == 3 or isinstance(r, StorageBusyError) for r in results)
        assert await _version(first) == 3


async def test_v2_upgrade_keeps_requests_and_leaves_legacy_sending_to_recovery(
    postgres_url: URL,
) -> None:
    """v2 → v3 只加投递尝试列与约束：已有请求不变；v2 遗留的 sending 没有尝试标识。"""
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="002_p1b_channels.sql")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO xiaowei_request VALUES ('web', 'k', 'alice', 'c', 's', 't',"
                    " 'query', 'd', 'failed', NULL, 'busy', 'sending', now(), now(), now())"
                )
            )
        with pytest.raises(StorageVersionMismatchError):
            await check_storage(engine)
        assert await upgrade_storage(engine) == 3
        await check_storage(engine)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT delivery, delivery_attempt, delivery_owner_pid FROM xiaowei_request"
                    )
                )
            ).one()
        assert tuple(row) == ("sending", None, None)


async def test_instance_lock_is_exclusive_with_serve_and_maintenance(postgres_url: URL) -> None:
    async with (
        open_engine(secret(postgres_url)) as engine,
        open_engine(secret(postgres_url)) as other,
    ):
        await initialize_storage(engine)
        readiness = Readiness()
        async with hold_instance_lock(engine, readiness) as lock:
            await lock.verify()
            # 第二个 serve 与维护命令都不能与持锁实例并存。
            with pytest.raises(StorageBusyError):
                async with hold_instance_lock(other, Readiness()):
                    pass
            with pytest.raises(StorageBusyError):
                await initialize_storage(other)
            with pytest.raises(StorageBusyError):
                await upgrade_storage(other)
        # 释放后可再次取得。
        async with hold_instance_lock(other, Readiness()):
            pass
        assert readiness.ok


async def test_busy_initialization_leaves_an_empty_database_untouched(postgres_url: URL) -> None:
    async with (
        open_engine(secret(postgres_url)) as engine,
        open_engine(secret(postgres_url)) as other,
    ):
        async with hold_instance_lock(engine, Readiness()):
            with pytest.raises(StorageBusyError):
                await initialize_storage(other)
            # 被拒绝的命令没有建立任何表（含 SDK 表）。
            assert await _tables(engine) == set()
        await initialize_storage(other)
        await check_storage(other)


async def test_concurrent_first_initializations_have_one_writer(postgres_url: URL) -> None:
    """并发首次初始化：取不到锁的进程直接拒绝（不建表），不会因锁外建表竞争而失败。"""
    async with (
        open_engine(secret(postgres_url)) as first,
        open_engine(secret(postgres_url)) as second,
    ):
        for _ in range(3):
            results = await asyncio.gather(
                initialize_storage(first), initialize_storage(second), return_exceptions=True
            )
            assert any(r is None for r in results)
            assert all(r is None or isinstance(r, StorageBusyError) for r in results)
        await check_storage(first)
        assert await _tables(first) == {SDK_SESSIONS_TABLE, SDK_MESSAGES_TABLE, *APP_TABLES}


async def test_losing_the_lock_connection_locks_readiness(postgres_url: URL) -> None:
    async with (
        open_engine(secret(postgres_url)) as engine,
        open_engine(secret(postgres_url)) as admin,
    ):
        await initialize_storage(engine)
        readiness = Readiness()
        async with hold_instance_lock(engine, readiness) as lock:
            async with admin.begin() as conn:
                terminated = await conn.scalar(
                    text(
                        "SELECT count(pg_terminate_backend(pid)) FROM pg_locks"
                        " WHERE locktype = 'advisory' AND granted"
                    )
                )
            assert terminated == 1
            with pytest.raises(StorageUnavailableError):
                await lock.verify()
            assert not readiness.ok
            # 锁已随连接释放：另一个实例此时可以取得，原实例必须停止服务。
            async with hold_instance_lock(admin, Readiness()):
                pass


async def test_lock_loss_is_signalled_without_a_check(postgres_url: URL) -> None:
    """持锁连接被终止时，驱动的终止通知立即锁低 readiness，不需要等下一次 ``verify``。"""
    async with (
        open_engine(secret(postgres_url)) as engine,
        open_engine(secret(postgres_url)) as admin,
    ):
        await initialize_storage(engine)
        readiness = Readiness()
        async with hold_instance_lock(engine, readiness) as lock:
            assert not lock.lost.is_set()
            async with admin.begin() as conn:
                await conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_locks"
                        " WHERE locktype = 'advisory' AND granted"
                    )
                )
            await asyncio.wait_for(lock.lost.wait(), 2)
            assert readiness.reason == "instance_lock_lost"


async def test_releasing_the_lock_is_not_a_loss(postgres_url: URL) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await initialize_storage(engine)
        readiness = Readiness()
        async with hold_instance_lock(engine, readiness) as lock:
            await lock.verify()
        await asyncio.sleep(0.1)  # 终止通知经事件循环回调，给它运行的机会
        assert readiness.ok and not lock.lost.is_set()


async def test_a_lock_connection_closed_before_watching_is_a_lock_loss(postgres_url: URL) -> None:
    """连接在取得锁之后、注册终止通知之前断开：通知已经错过，注册时必须立即按丢锁处理并拒绝，
    不能等周期核对。"""
    async with (
        open_engine(secret(postgres_url)) as engine,
        open_engine(secret(postgres_url)) as admin,
    ):
        await initialize_storage(engine)
        readiness = Readiness()
        conn = await engine.connect()
        try:
            row = (
                await conn.execute(
                    text(
                        "SELECT pid, backend_start FROM pg_stat_activity"
                        " WHERE pid = pg_backend_pid()"
                    )
                )
            ).one()
            await conn.commit()
            driver = (await conn.get_raw_connection()).driver_connection
            assert driver is not None
            async with admin.begin() as other:
                await other.execute(text("SELECT pg_terminate_backend(:p)"), {"p": row.pid})
            async with asyncio.timeout(2):
                while not driver.is_closed():
                    await asyncio.sleep(0.01)
            lock = InstanceLock(conn, readiness, row.pid, row.backend_start)
            with pytest.raises(StorageUnavailableError):
                async with lock.watching():
                    pytest.fail("已关闭的持锁连接不能进入服务")
            assert lock.lost.is_set() and readiness.reason == "instance_lock_lost"
        finally:
            await conn.invalidate()
            await conn.close()
