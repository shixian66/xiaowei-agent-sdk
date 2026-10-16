"""integration 层的连接、schema 与 socket 放行。

**放行方式**：`pytest_collection_modifyitems` 只给**本目录**的 item 加
``allow_hosts`` marker，且 marker 只带 DSN 解析出的那一个 host。

不用 ``socket_enabled`` fixture：pytest-socket 0.8.1 的 ``pytest_runtest_setup`` 在命中
``socket_enabled`` 分支后**直接 return**，``_resolve_allow_hosts()`` 永不执行，
``connect`` 完全不受保护——那不是"放行到某个 host"，那是全开。

不用全局 ``--allow-hosts``：它会让 ``_resolve_allow_hosts`` 对**每一个** item 都返回
非空，于是 ``disable_socket()`` 对整个套件永不执行。一个为 integration 加的开关会
静默解除全套件的网络封锁。

**DSN 在 ``pytest_configure`` 读入并存进 ``config.stash``**，其余地方一律从 stash 取。
``tests/conftest.py::clean_xiaowei_env`` 是 autouse，每个用例前删除全部 ``XIAOWEI_*``；
更深的根因是 ``load_settings()`` 对任何未知 ``XIAOWEI_*`` 变量 fail-fast。因此这个
harness 变量**不带 ``XIAOWEI_`` 前缀**，且在任何 fixture 之前取值。
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from xiaowei_agent.persistence.migrations.runner import run_downgrade, run_upgrade
from xiaowei_agent.persistence.postgres import (
    PostgresEvidenceLedger,
    PostgresPlanStore,
    PostgresTaskStore,
)
from xiaowei_agent.persistence.schema import (
    ALL_TABLES,
    CREATED_SEQUENCE_NAME,
    FENCING_SEQUENCE_NAME,
)

DSN_ENV_VAR = "PYTEST_POSTGRES_DSN"
"""**不带 ``XIAOWEI_`` 前缀**：它是测试 harness 配置，不是应用配置。见模块 docstring。"""

_DSN_STASH: pytest.StashKey[str | None] = pytest.StashKey()
_INTEGRATION_DIR = Path(__file__).resolve().parent

SKIP_REASON = f"{DSN_ENV_VAR} 未设置：跳过需要真实 PostgreSQL 的用例"


def pytest_configure(config: pytest.Config) -> None:
    """在任何用例与任何 fixture 之前取值，因此不受任何清理逻辑影响。"""
    config.stash[_DSN_STASH] = os.environ.get(DSN_ENV_VAR) or None


def dsn_of(config: pytest.Config) -> str | None:
    return config.stash.get(_DSN_STASH, None)


def _is_integration_item(item: pytest.Item) -> bool:
    path = getattr(item, "path", None)
    if path is None:
        return False
    return _INTEGRATION_DIR in Path(path).parents or Path(path).parent == _INTEGRATION_DIR


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """只给本目录的 item 加 ``allow_hosts``，且只放行 DSN 的那一个 host。

    DSN 未设置时**不加 marker**——此时用例本就会跳过，加一个空放行只会让
    ``_resolve_allow_hosts`` 返回非空，反而绕开 ``disable_socket()``。
    """
    dsn = dsn_of(config)
    if dsn is None:
        return
    host = make_url(dsn).host
    if host is None:
        return
    for item in items:
        if _is_integration_item(item):
            item.add_marker(pytest.mark.allow_hosts([host]))


def unexpected_integration_skips(skipped_locations: list[str], *, dsn: str | None) -> list[str]:
    """DSN 已设置时，integration 目录里**不允许**有任何跳过。

    没有这条，CI 里 DSN 拼错、service 没起来、迁移失败等情况全都表现为"全绿"，
    而 M4 的全部退出标准都建立在这些用例真的跑过之上。跳过是静默的，这就是对价。

    写成纯函数是为了能在不真的制造一次跳过的前提下测试它本身。
    """
    if dsn is None:
        return []
    marker = f"tests{os.sep}integration{os.sep}"
    return [location for location in skipped_locations if marker in location]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    locations = [
        report.nodeid for report in reporter.stats.get("skipped", []) if hasattr(report, "nodeid")
    ]
    offenders = unexpected_integration_skips(locations, dsn=dsn_of(session.config))
    if offenders:
        reporter.write_line(
            f"{DSN_ENV_VAR} 已设置，但 integration 仍有 {len(offenders)} 条跳过：{offenders[:5]}",
            red=True,
        )
        session.exitstatus = 1


@pytest.fixture(scope="session")
def postgres_dsn(request: pytest.FixtureRequest) -> str:
    dsn = dsn_of(request.config)
    if dsn is None:
        pytest.skip(SKIP_REASON)
    return dsn


@pytest.fixture(scope="session")
async def migrated_engine(postgres_dsn: str) -> AsyncIterator[AsyncEngine]:
    """建库并跑迁移。**schema 由 Alembic 建，不由 ``create_all`` 建。**

    用 ``create_all`` 会让集成测试跑在一套**没人迁移过**的表上：迁移写错了照样全绿，
    而生产环境只会拿到迁移建出来的那套。
    """
    engine = create_async_engine(postgres_dsn, poolclass=sa.pool.NullPool)
    async with engine.begin() as connection:
        await connection.run_sync(run_upgrade)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def clean_database(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """每个用例前清空全部表并重置 fencing 序列。

    不用事务回滚做隔离：M4 的用例里有需要**真实提交**的并发场景，把整条用例包进一个
    未提交事务会让"另一个连接看得到吗"这类断言全部失真。
    """
    async with migrated_engine.begin() as connection:
        names = ", ".join(table.name for table in ALL_TABLES)
        await connection.execute(sa.text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        await connection.execute(sa.text(f"ALTER SEQUENCE {FENCING_SEQUENCE_NAME} RESTART WITH 1"))
        await connection.execute(sa.text(f"ALTER SEQUENCE {CREATED_SEQUENCE_NAME} RESTART WITH 1"))
    yield migrated_engine


@pytest.fixture
def store(clean_database: AsyncEngine, clock: Any) -> PostgresTaskStore:
    """覆盖根 conftest 的内存 ``store``。

    ``clock`` 仍是 ``ManualClock``：租约过期由注入时钟决定，不由服务端 ``now()``
    决定——否则六条过期/抢占用例只能靠 ``sleep`` 才能测。
    """
    return PostgresTaskStore(engine=clean_database, clock=clock)


@pytest.fixture
def plan_store(clean_database: AsyncEngine) -> PostgresPlanStore:
    """覆盖根 conftest 的内存 ``plan_store``。"""
    return PostgresPlanStore(engine=clean_database)


@pytest.fixture
def evidence_ledger(clean_database: AsyncEngine) -> PostgresEvidenceLedger:
    """覆盖根 conftest 的内存 ``evidence_ledger``。"""
    return PostgresEvidenceLedger(engine=clean_database)


@pytest.fixture
async def independent_stores(
    clean_database: AsyncEngine, postgres_dsn: str, clock: Any
) -> AsyncIterator[Any]:
    """按需创建**各自持有独立连接**的 store。

    并发用例必须用多个真实连接。``asyncio.gather`` 共用一个连接不算并发——它证明
    的是"同一个会话里两次顺序调用"，而 CAS 要防的恰恰是两个会话。这里每个 store
    拿一个独立的 ``AsyncEngine``（``NullPool``，每次调用开新连接），把"是不是真的
    并发"这件事变成结构上确定的，而不是靠推断连接池行为。
    """
    engines: list[AsyncEngine] = []

    def _make(count: int) -> list[PostgresTaskStore]:
        stores: list[PostgresTaskStore] = []
        for _ in range(count):
            engine = create_async_engine(postgres_dsn, poolclass=sa.pool.NullPool)
            engines.append(engine)
            stores.append(PostgresTaskStore(engine=engine, clock=clock))
        return stores

    try:
        yield _make
    finally:
        for engine in engines:
            await engine.dispose()


@pytest.fixture
def alembic_runners() -> Iterator[tuple[Any, Any]]:
    """迁移测试用的 upgrade / downgrade 可调用对象。"""
    yield (run_upgrade, run_downgrade)
