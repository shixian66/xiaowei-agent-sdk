"""P2.5 Task 2：有界自动结构快照与交付前的当前权限复核（离线）。

真实 ``StarRocksAdapter`` + ``SchemaCache`` + ``starrocks_tools`` 的执行函数；只把最底层连接换成
P1-B 的 recording 驱动替身，时钟由用例控制。替身按语句全文返回脚本结果：它能证明刷新、到期、
单飞、完整替换、超限与取消的代码分支，以及工具在交付前确实重新探测、拒绝时没有业务 I/O；
不能证明 StarRocks 的 ``information_schema`` 与权限实际行为（见 ``tests/p1b/test_starrocks_real.py``
的结构与权限用例）。
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from asyncmy.errors import OperationalError, ProgrammingError
from tests.p1b.test_starrocks_adapter import (
    CANARY,
    LIMITS,
    NOW,
    SCHEMA_TABLES,
    TARGET,
    Driver,
    Result,
    driver,
    schema_results,
)

from xiaowei.governance import Prechecked, ToolRejectedError
from xiaowei.models import AUDIENCES, ToolObservation, ToolRequest
from xiaowei.sqlguard import QueryPolicy
from xiaowei.starrocks import (
    SCHEMA_COLUMNS_SQL,
    SCHEMA_OBJECTS_SQL,
    AuditSource,
    StarRocksAdapter,
    StarRocksError,
    StarRocksErrorCode,
    StarRocksTarget,
    probe_sql,
)
from xiaowei.starrocks_schema import SCHEMA_UNAVAILABLE, SchemaCache, SchemaUnavailableError
from xiaowei.starrocks_tools import (
    DESCRIBE_TABLE,
    EXPLAIN_QUERY,
    LAYOUT_TOOL,
    LIST_TABLES,
    RUN_QUERY,
    starrocks_tools,
)

Tables = dict[tuple[str, str], tuple[tuple[str, str, str, str | None], ...]]
CRM: Tables = {
    ("crm", "customers"): (("id", "bigint", "NO", None), ("tier", "varchar", "YES", None))
}
WIDE: Tables = {**SCHEMA_TABLES, **CRM}


@dataclass
class Clock:
    now: Any = NOW

    def __call__(self) -> Any:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class World:
    """可替换的结构替身：``tables`` 与 ``denied`` 随用例改变，之后新建的连接按新内容应答。"""

    tables: Tables = field(default_factory=lambda: dict(WIDE))
    denied: set[tuple[str, str]] = field(default_factory=set)
    overrides: dict[str, Result | BaseException] = field(default_factory=dict)
    query: Result = field(default_factory=lambda: Result(("region",), [("east",)]))

    def driver(self) -> Driver:
        def results() -> dict[str, Result | BaseException]:
            return {
                **schema_results(self.tables, frozenset(self.denied)),
                **self.overrides,
            }

        drv = driver(self.query)
        original = drv.make

        def make() -> Any:
            conn = original()
            conn.results.update(results())
            return conn

        drv.make = make
        return drv


def cache_for(
    drv: Driver, clock: Clock, target: StarRocksTarget = TARGET
) -> tuple[StarRocksAdapter, SchemaCache]:
    adapter = StarRocksAdapter(target, connect=drv, clock=clock)
    return adapter, SchemaCache(adapter, clock=clock)


def statements(drv: Driver) -> list[str]:
    """到达驱动的语句（不含会话设置与回读）。"""
    return [
        sql
        for conn in drv.connections
        for sql, _ in conn.executed
        if not sql.startswith(("SET ", "SELECT @@"))
    ]


def forget(drv: Driver) -> None:
    drv.connections.clear()
    drv.attempts = 0


# ---- 刷新：成功、首次失败、沿用、到期 -------------------------------------------------------


async def test_refresh_keeps_only_objects_the_account_can_select() -> None:
    world = World(denied={("shop", "regions")})
    drv, clock = world.driver(), Clock()
    _, cache = cache_for(drv, clock)

    assert await cache.refresh()
    snapshot = cache.current()
    # 可见但探测为无权（例如只授 INSERT）的对象不进入快照；其他库的可读对象进入。
    assert set(snapshot.objects) == {("shop", "sales"), ("crm", "customers")}
    sales = snapshot.object("shop", "sales")
    assert sales is not None
    assert [c.name for c in sales.columns] == ["region", "total", "note"]
    assert sales.columns[0].comment == "地区" and sales.table_id is not None
    assert sales.created_at == "2026-09-01T08:00:00+08:00"
    assert (snapshot.collected_at, snapshot.expires_at) == (NOW, NOW + timedelta(seconds=300))
    # SQLGuard 范围是全部库的可读对象（P2.5 Task 3），列按快照顺序，函数与上限来自配置。
    policy = snapshot.query_policy
    assert policy.tables == {
        "shop": {"sales": ("region", "total", "note")},
        "crm": {"customers": ("id", "tier")},
    }
    assert "default_database" not in QueryPolicy.model_fields  # 业务 SQL 不继承连接的默认库
    assert policy.allowed_functions == TARGET.policy.allowed_functions
    assert policy.max_result_columns == TARGET.policy.max_result_columns
    # 每个候选对象恰好探测一次，全部在同一条连接上。
    probes = [sql for sql in statements(drv) if sql.startswith("SELECT 1 FROM")]
    assert sorted(probes) == sorted(probe_sql(db, name) for db, name in WIDE)
    assert sum(1 for c in drv.connections if any("SELECT 1 FROM" in s for s, _ in c.executed)) == 1


async def test_without_a_successful_refresh_the_target_is_unavailable() -> None:
    drv, clock = World().driver(), Clock()
    drv.connect_error = OperationalError(2003, f"cannot connect {CANARY}")
    _, cache = cache_for(drv, clock)

    with pytest.raises(SchemaUnavailableError):
        cache.current()
    assert not await cache.refresh()
    with pytest.raises(SchemaUnavailableError) as raised:
        cache.current()
    assert str(raised.value) == SCHEMA_UNAVAILABLE and CANARY not in str(raised.value)


async def test_failed_refresh_keeps_the_old_snapshot_only_until_it_expires() -> None:
    world = World()
    drv, clock = world.driver(), Clock()
    _, cache = cache_for(drv, clock)
    assert await cache.refresh()
    first = cache.current()

    clock.advance(200)
    drv.connect_error = OperationalError(2003, "down")
    assert not await cache.refresh()
    assert cache.current() is first  # 期限内沿用完整的旧快照

    clock.advance(99)  # 采集后 299 秒
    assert cache.current() is first
    clock.advance(1)  # 恰好到期：失败的刷新不延长期限
    with pytest.raises(SchemaUnavailableError):
        cache.current()

    drv.connect_error = None
    assert await cache.refresh()
    assert cache.current().collected_at == NOW + timedelta(seconds=300)


async def test_a_new_snapshot_replaces_the_old_one_completely() -> None:
    world = World()
    drv, clock = world.driver(), Clock()
    _, cache = cache_for(drv, clock)
    assert await cache.refresh()
    assert ("crm", "customers") in cache.current().objects

    # 新授权、撤权与删表：下一次成功刷新后整体替换，不与旧快照合并。
    world.tables = {
        ("shop", "sales"): SCHEMA_TABLES[("shop", "sales")],
        ("mkt", "leads"): CRM[("crm", "customers")],
    }
    world.denied = {("shop", "sales")}
    clock.advance(60)
    assert await cache.refresh()
    assert set(cache.current().objects) == {("mkt", "leads")}
    assert cache.current().query_policy.tables == {"mkt": {"leads": ("id", "tier")}}


# ---- 不发布半份快照 -----------------------------------------------------------------------


def _limited(**limits: int) -> StarRocksTarget:
    return TARGET.model_copy(update={"schema_limits": LIMITS.model_copy(update=limits)})


@pytest.mark.parametrize(
    "target",
    [_limited(max_objects=2), _limited(max_columns=6), _limited(max_bytes=300)],
    ids=["too-many-objects", "too-many-columns", "too-many-bytes"],
)
async def test_a_schema_over_any_limit_is_never_published(target: StarRocksTarget) -> None:
    drv, clock = World().driver(), Clock()
    _, cache = cache_for(drv, clock, target)
    assert not await cache.refresh()
    with pytest.raises(SchemaUnavailableError):
        cache.current()
    # 超限在探测之前就已判定：没有为半份结构做权限探测。
    assert not any(sql.startswith("SELECT 1 FROM") for sql in statements(drv))


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {probe_sql("crm", "customers"): OperationalError(1105, f"internal {CANARY}")},
            id="probe-cannot-decide",
        ),
        pytest.param(
            {SCHEMA_COLUMNS_SQL: Result(("db", "name"), [("shop", "sales")])},
            id="unexpected-columns",
        ),
        pytest.param(
            {
                SCHEMA_OBJECTS_SQL: Result(
                    ("db", "name", "type", "comment", "created"), [(1, "x", "T", None, None)]
                )
            },
            id="non-text-name",
        ),
        pytest.param({SCHEMA_OBJECTS_SQL: ProgrammingError(5203, "denied")}, id="metadata-denied"),
    ],
)
async def test_a_failed_refresh_never_overwrites_the_published_snapshot(
    overrides: dict[str, Result | BaseException], caplog: pytest.LogCaptureFixture
) -> None:
    world = World()
    drv, clock = world.driver(), Clock()
    _, cache = cache_for(drv, clock)
    assert await cache.refresh()
    previous = cache.current()

    world.overrides = overrides
    clock.advance(60)
    caplog.set_level(logging.WARNING, logger="xiaowei.starrocks_schema")
    assert not await cache.refresh()
    assert cache.current() is previous
    assert "结构快照刷新失败" in caplog.text and CANARY not in caplog.text  # 只记录原因代码


async def test_refresh_past_its_deadline_publishes_nothing() -> None:
    world = World()
    drv, clock = world.driver(), Clock()
    drv.connect_delay = 0.5
    target = TARGET.model_copy(
        update={
            "schema_limits": LIMITS.model_copy(update={"refresh_timeout_seconds": 0.2}),
            "connect_timeout_seconds": 5,
        }
    )
    _, cache = cache_for(drv, clock, target)
    assert not await cache.refresh()
    with pytest.raises(SchemaUnavailableError):
        cache.current()


# ---- 单飞、取消与目标隔离 -------------------------------------------------------------------


async def test_concurrent_refreshes_share_one_round_of_io() -> None:
    world = World()
    drv, clock = world.driver(), Clock()
    drv.connect_delay = 0.01
    _, cache = cache_for(drv, clock)

    results = await asyncio.gather(*(cache.refresh() for _ in range(5)))
    assert results == [True] * 5
    # 一次刷新：三条元数据读取各一条连接，探测共用一条。
    assert drv.attempts == 4

    # 等待者被取消不会中止共享的刷新。
    forget(drv)
    waiter = asyncio.create_task(cache.refresh())
    await asyncio.sleep(0)
    waiter.cancel()
    assert await cache.refresh()
    assert drv.attempts == 4


async def test_close_cancels_an_inflight_refresh_and_discards_its_connection() -> None:
    drv, clock = World().driver(), Clock()
    _, cache = cache_for(drv, clock)

    def hang() -> Any:
        conn = original()
        conn.hang_on = SCHEMA_COLUMNS_SQL
        return conn

    original = drv.make
    drv.make = hang
    pending = asyncio.create_task(cache.refresh())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if any(sql == SCHEMA_COLUMNS_SQL for c in drv.connections for sql, _ in c.executed):
            break
    await cache.aclose()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert drv.connections[-1].aborted
    with pytest.raises(SchemaUnavailableError):
        cache.current()


async def test_targets_refresh_independently() -> None:
    clock = Clock()
    healthy, broken = World().driver(), World().driver()
    broken.connect_error = OperationalError(2003, "down")
    other = TARGET.model_copy(update={"target_id": "sr-other"})
    _, ok = cache_for(healthy, clock)
    _, bad = cache_for(broken, clock, other)

    assert await asyncio.gather(ok.refresh(), bad.refresh()) == [True, False]
    assert ok.current().target_id == TARGET.target_id
    with pytest.raises(SchemaUnavailableError):
        bad.current()


async def test_audit_table_and_unquotable_names_never_enter_the_snapshot() -> None:
    world = World()
    world.tables = {
        **SCHEMA_TABLES,
        ("starrocks_audit_db__", "starrocks_audit_tbl__"): (("stmt", "varchar", "YES", None),),
        ("shop", "odd`name"): (("id", "int", "NO", None),),
    }
    drv, clock = world.driver(), Clock()
    audited = TARGET.model_copy(
        update={
            "audit": AuditSource(
                database="starrocks_audit_db__",
                table="starrocks_audit_tbl__",
                time_zone="UTC",
                stmt_limit=TARGET.policy.max_sql_bytes + 4,
                candidate_bytes=16_000_000,
            ),
            "max_value_bytes": 1000,
            "max_result_bytes": 100_000,
        }
    )
    _, cache = cache_for(drv, clock, audited)
    assert await cache.refresh()
    assert set(cache.current().objects) == set(SCHEMA_TABLES)
    # 审计表与无法安全引用的名字也不会被探测。
    assert not any("starrocks_audit_tbl__" in sql or "odd" in sql for sql in statements(drv))


# ---- 工具：快照缺失在 I/O 前拒绝；交付前按当前权限重新探测 -------------------------------------


@dataclass
class Tools:
    drv: Driver
    clock: Clock
    cache: SchemaCache
    executes: Any
    world: World

    def request(self, tool_id: str, **arguments: object) -> ToolRequest:
        return ToolRequest(
            tool_id=tool_id,
            target_id=TARGET.target_id,
            call_id="c1",
            tool_name=tool_id.split("/", 1)[1],
            arguments={"cluster": TARGET.target_id, **arguments},
        )

    async def call(self, tool_id: str, **arguments: object) -> ToolObservation:
        execute = self.executes[(tool_id, TARGET.target_id)]
        assert isinstance(execute, Prechecked)
        return await execute.run(execute.check(self.request(tool_id, **arguments)))


async def tools(refreshed: bool = True) -> Tools:
    world = World()
    drv, clock = world.driver(), Clock()
    adapter, cache = cache_for(drv, clock)
    if refreshed:
        assert await cache.refresh()
    forget(drv)
    assembled = starrocks_tools(adapter, dict.fromkeys(AUDIENCES, 400_000), schema=cache)
    return Tools(drv, clock, cache, assembled.executes, world)


# “.” 出现在每个“库名.表名”中：第 1 页即目录的前 max_rows 个对象。
SEARCH_ALL: dict[str, object] = {
    "keyword": ".",
    "database": None,
    "page": 1,
    "page_size": TARGET.policy.max_rows,
    "snapshot": None,
}
ALL_CALLS: list[tuple[str, dict[str, object]]] = [
    (LIST_TABLES, SEARCH_ALL),
    (DESCRIBE_TABLE, {"database": "shop", "table": "sales"}),
    (LAYOUT_TOOL, {"database": "shop", "table": "sales"}),
    (RUN_QUERY, {"sql": "SELECT region FROM sales"}),
    (EXPLAIN_QUERY, {"sql": "SELECT region FROM sales"}),
]


@pytest.mark.parametrize(("tool_id", "arguments"), ALL_CALLS, ids=[t for t, _ in ALL_CALLS])
@pytest.mark.parametrize("state", ["never-refreshed", "expired"])
async def test_every_tool_is_rejected_before_io_without_a_current_snapshot(
    tool_id: str, arguments: dict[str, object], state: str
) -> None:
    t = await tools(refreshed=state == "expired")
    t.clock.advance(300)
    with pytest.raises(ToolRejectedError, match="表结构暂不可用"):
        await t.call(tool_id, **arguments)
    assert t.drv.attempts == 0


async def test_list_skips_objects_revoked_since_the_refresh() -> None:
    t = await tools()
    t.world.denied = {("crm", "customers")}  # 刷新之后撤权：快照仍有它
    observation = await t.call(LIST_TABLES, **SEARCH_ALL)
    rows = observation.payload["rows"]
    assert [(r["database"], r["name"]) for r in rows] == [("shop", "regions"), ("shop", "sales")]  # type: ignore[index, union-attr]
    # 列出前探测了快照中的每个对象（含刚撤权的那个），没有其他元数据读取。
    assert sorted(statements(t.drv)) == sorted(probe_sql(db, n) for db, n in WIDE)
    assert observation.captured_at == NOW and not observation.truncated


def numbered(count: int) -> Tables:
    """``count`` 张单列表 ``t00``…，按目录顺序排列。"""
    return {("shop", f"t{i:02d}"): (("id", "int", "NO", None),) for i in range(count)}


async def listed(
    world: World,
    revoked: set[tuple[str, str]] | None = None,
    overrides: dict[str, Result | BaseException] | None = None,
) -> tuple[ToolObservation, Driver]:
    """按 ``world`` 刷新快照，再撤销 ``revoked``、换上 ``overrides`` 后搜索第 1 页。

    返回结果与只含搜表本身 I/O 的驱动记录。
    """
    drv, clock = world.driver(), Clock()
    adapter, cache = cache_for(drv, clock)
    assert await cache.refresh()
    forget(drv)
    world.denied = revoked or set()
    world.overrides = overrides or {}
    executes = starrocks_tools(adapter, dict.fromkeys(AUDIENCES, 400_000), schema=cache).executes
    execute = executes[(LIST_TABLES, TARGET.target_id)]
    assert isinstance(execute, Prechecked)
    request = ToolRequest(
        tool_id=LIST_TABLES,
        target_id=TARGET.target_id,
        call_id="c1",
        tool_name="list_tables",
        arguments={"cluster": TARGET.target_id, **SEARCH_ALL},
    )
    return await execute.run(execute.check(request)), drv


def names(observation: ToolObservation) -> list[str]:
    return [row["name"] for row in observation.payload["rows"]]  # type: ignore[index, union-attr]


@pytest.mark.parametrize(
    ("count", "denied", "shown", "truncated"),
    [
        # 本页大多已撤权：只列出仍可读的，页不前移，后面还有匹配即标记截断（P2.5 Task 6）。
        (30, range(1, 5), [0], True),
        # 全部可读的成功对照。
        (30, (), range(5), True),
        # 恰好一页、全部可读：不截断。
        (5, (), range(5), False),
    ],
    ids=["本页大多撤权", "全部可读", "恰好一页"],
)
async def test_a_page_probes_only_its_own_objects(
    count: int, denied: Any, shown: Any, truncated: bool
) -> None:
    revoked = {("shop", f"t{i:02d}") for i in denied}
    observation, drv = await listed(World(tables=numbered(count)), revoked)
    assert names(observation) == [f"t{i:02d}" for i in shown]
    assert observation.truncated is truncated
    # 只探测本页（目录顺序），不探测整个目录。
    assert statements(drv) == [probe_sql("shop", f"t{i:02d}") for i in range(min(count, 5))]


async def test_a_probe_failure_while_listing_returns_nothing() -> None:
    lost = {probe_sql("shop", "t03"): OperationalError(2013, "lost")}  # 第 1 页中的对象
    with pytest.raises(StarRocksError) as raised:
        await listed(World(tables=numbered(30)), overrides=lost)
    assert raised.value.code is StarRocksErrorCode.CONNECTION_LOST


@pytest.mark.parametrize("tool_id", [DESCRIBE_TABLE, LAYOUT_TOOL])
async def test_describing_an_object_revoked_since_the_refresh_fails(tool_id: str) -> None:
    t = await tools()
    t.world.denied = {("shop", "sales")}
    with pytest.raises(StarRocksError) as raised:
        await t.call(tool_id, database="shop", table="sales")
    assert raised.value.code is StarRocksErrorCode.OBJECT_UNREADABLE
    # 只发出了探测：布局与表结构都没有读取或交付。
    assert statements(t.drv) == [probe_sql("shop", "sales")]


@pytest.mark.parametrize("tool_id", [LIST_TABLES, DESCRIBE_TABLE])
async def test_unverifiable_access_is_not_mistaken_for_revocation(tool_id: str) -> None:
    t = await tools()
    t.world.overrides = {probe_sql("shop", "sales"): OperationalError(2013, "lost")}
    arguments = {"database": "shop", "table": "sales"} if tool_id == DESCRIBE_TABLE else SEARCH_ALL
    with pytest.raises(StarRocksError) as raised:
        await t.call(tool_id, **arguments)
    assert raised.value.code is StarRocksErrorCode.CONNECTION_LOST


@pytest.mark.parametrize(
    ("database", "table"),
    [("shop", "orders"), ("crm", "Customers"), ("SHOP", "sales"), ("shop", "regions ")],
)
async def test_describing_an_object_outside_the_snapshot_is_rejected_before_io(
    database: str, table: str
) -> None:
    t = await tools()
    with pytest.raises(ToolRejectedError, match="不在可读的表结构中"):
        await t.call(DESCRIBE_TABLE, database=database, table=table)
    assert t.drv.attempts == 0


async def test_describe_serves_the_snapshot_columns_after_a_fresh_probe() -> None:
    t = await tools()
    observation = await t.call(DESCRIBE_TABLE, database="crm", table="customers")
    assert observation.payload["rows"] == [
        {"name": "id", "type": "bigint", "nullable": "NO", "comment": None},
        {"name": "tier", "type": "varchar", "nullable": "YES", "comment": None},
    ]
    assert observation.payload["sql"] == SCHEMA_COLUMNS_SQL
    assert statements(t.drv) == [probe_sql("crm", "customers")]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name FROM regions",  # 可见但无 SELECT 权限：不在快照中
        "SELECT secret FROM sales",
        "SELECT id FROM crm.payroll",  # 其他库中不在快照的对象
        "SELECT id FROM customers c JOIN crm.customers d ON c.id = d.id",  # 两个来源都有 id
    ],
)
async def test_queries_outside_the_snapshot_scope_never_reach_the_database(sql: str) -> None:
    world = World(denied={("shop", "regions")})
    drv, clock = world.driver(), Clock()
    adapter, cache = cache_for(drv, clock)
    assert await cache.refresh()
    forget(drv)
    executes = starrocks_tools(adapter, dict.fromkeys(AUDIENCES, 400_000), schema=cache).executes
    for tool_id in (RUN_QUERY, EXPLAIN_QUERY):
        execute = executes[(tool_id, TARGET.target_id)]
        assert isinstance(execute, Prechecked)
        request = ToolRequest(
            tool_id=tool_id,
            target_id=TARGET.target_id,
            call_id="c1",
            tool_name=tool_id.split("/", 1)[1],
            arguments={"cluster": TARGET.target_id, "sql": sql},
        )
        with pytest.raises(ToolRejectedError):
            execute.check(request)
    assert drv.attempts == 0


async def test_a_new_grant_becomes_queryable_after_the_next_refresh() -> None:
    world = World(denied={("shop", "regions")})
    drv, clock = world.driver(), Clock()
    adapter, cache = cache_for(drv, clock)
    assert await cache.refresh()
    executes = starrocks_tools(adapter, dict.fromkeys(AUDIENCES, 400_000), schema=cache).executes

    def run(sql: str) -> ToolRequest:
        return ToolRequest(
            tool_id=RUN_QUERY,
            target_id=TARGET.target_id,
            call_id="c1",
            tool_name="run_readonly_query",
            arguments={"cluster": TARGET.target_id, "sql": sql},
        )

    execute = executes[(RUN_QUERY, TARGET.target_id)]
    assert isinstance(execute, Prechecked)
    with pytest.raises(ToolRejectedError):
        execute.check(run("SELECT name FROM regions"))

    world.denied = set()  # DBA 授权
    clock.advance(60)
    assert await cache.refresh()
    forget(drv)
    observation = await execute.run(execute.check(run("SELECT name FROM regions")))
    assert observation.payload["rows"] == [{"region": "east"}]
    assert len(statements(drv)) == 1


async def test_runtime_cancelled_during_the_first_refresh_leaves_no_refresh_running() -> None:
    """启动时首次刷新期间被取消（例如停止信号）：进行中的共享刷新同样被取消，连接断开。"""
    from xiaowei.runtime import _refreshing

    drv, clock = World().driver(), Clock()
    original = drv.make

    def hang() -> Any:
        conn = original()
        conn.hang_on = SCHEMA_COLUMNS_SQL
        return conn

    drv.make = hang
    _, cache = cache_for(drv, clock)

    async def start() -> None:
        async with _refreshing({TARGET.target_id: cache}):
            pytest.fail("首次刷新挂起时不应进入服务阶段")

    task = asyncio.create_task(start())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if any(sql == SCHEMA_COLUMNS_SQL for c in drv.connections for sql, _ in c.executed):
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cache._inflight is not None and cache._inflight.done()
    assert drv.connections[-1].aborted
