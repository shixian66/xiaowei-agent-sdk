"""StarRocks Adapter 的真实协议验证（显式运行）::

    SDK_TEST_STARROCKS_ADMIN_URL=mysql://root@127.0.0.1:59030 \\
    SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \\
      uv run --locked --extra dev python -m pytest tests/p1b/test_starrocks_real.py \\
      -m starrocks_real -q

受治理工具的端到端用例另需测试 PostgreSQL（``compose.sdk-test.yml``）保存证据。

fixture 以管理账号建一个随机名的合成数据库与只读账号（只授予其中一张表的 SELECT），用例
结束后删除。Adapter 只用只读账号连接。它证明锁定的 asyncmy 与该实例在会话变量、非缓冲读取、
类型、截断、权限、期限与取消上的实际行为；不证明用户环境的 TLS 证书、grants 或资源组。
"""

# ruff: noqa: S608 —— 合成库名由 fixture 随机生成，建表与插数语句是测试数据。

import asyncio
import os
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncmy
import pytest
from asyncmy.cursors import SSCursor
from asyncmy.errors import MySQLError
from tests.p1b.conftest import admin_address
from tests.sdk_core.postgres_harness import (
    HarnessMisconfiguredError,
    isolated_database,
    parse_admin_url,
)
from tests.sdk_core.synthetic_tools import Clock, Grants, ready_engine

from xiaowei.evidence import EvidenceStore
from xiaowei.governance import (
    GovernedTools,
    ToolCatalog,
    ToolExecutionError,
    ToolRejectedError,
)
from xiaowei.models import AgentAnswer, AnswerInference, Budget, Identity, RunContext, ToolRequest
from xiaowei.sqlguard import (
    QueryPolicy,
    QueryRejectedError,
    QueryRejectionCode,
    guard_explain_query,
    guard_readonly_query,
)
from xiaowei.starrocks import (
    EXPLAIN_PREFIX,
    LAYOUT_HIDDEN,
    SchemaLimits,
    SqlPolicy,
    StarRocksAdapter,
    StarRocksError,
    StarRocksErrorCode,
    StarRocksTarget,
    open_starrocks,
)
from xiaowei.starrocks_schema import (
    DependencyCheck,
    SchemaCache,
    listed_dependency,
    read_dependency,
)
from xiaowei.starrocks_tools import (
    DESCRIBE_TABLE,
    EXPLAIN_QUERY,
    LAYOUT_TOOL,
    LIST_TABLES,
    QUERY_TOOLS,
    RUN_QUERY,
    starrocks_tools,
)

pytestmark = pytest.mark.starrocks_real

Code = StarRocksErrorCode
PASSWORD_ENV = "XW_TEST_SR_RO_PASSWORD"  # noqa: S105 —— 环境变量名
SALES_ROWS = 40


@dataclass(frozen=True)
class Instance:
    host: str
    port: int
    database: str
    ro_user: str


async def admin_rows(host: str, port: int, user: str, sql: str) -> list[tuple[object, ...]]:
    conn = await asyncmy.connect(host=host, port=port, user=user, password="", autocommit=True)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(sql)
            return list(await cursor.fetchall())
    finally:
        await conn.ensure_closed()


async def admin(host: str, port: int, user: str, *statements: str) -> None:
    conn = await asyncmy.connect(host=host, port=port, user=user, password="", autocommit=True)
    try:
        async with conn.cursor() as cursor:
            for sql in statements:
                await cursor.execute(sql)
    finally:
        await conn.ensure_closed()


@pytest.fixture
async def instance(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Instance]:
    host, port, user = admin_address()
    suffix = secrets.token_hex(4)
    database, ro_user, password = f"xw_sr_test_{suffix}", f"xw_ro_{suffix}", secrets.token_hex(16)
    values = ", ".join(
        f"({i}, 'r{i % 4}', {i * 1.25}, '2026-09-{i % 28 + 1:02d}', "
        f"'2026-09-01 {i % 24:02d}:30:00', '说明{i}')"
        for i in range(SALES_ROWS)
    )
    table = "DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES('replication_num'='1')"
    await admin(
        host,
        port,
        user,
        f"CREATE DATABASE {database}",
        f"CREATE TABLE {database}.sales (id INT, region VARCHAR(16), total DECIMAL(10, 2), "
        f"day DATE, at DATETIME, note VARCHAR(64), secret VARCHAR(16)) {table}",
        f"INSERT INTO {database}.sales (id, region, total, day, at, note) VALUES {values}",
        f"CREATE TABLE {database}.hidden (id INT, v VARCHAR(8)) {table}",
        f"CREATE TABLE {database}.ungranted (id INT) {table}",
        f"CREATE USER '{ro_user}'@'%' IDENTIFIED BY '{password}'",
        f"GRANT SELECT ON TABLE {database}.sales TO USER '{ro_user}'@'%'",
    )
    monkeypatch.setenv(PASSWORD_ENV, password)
    try:
        yield Instance(host, port, database, ro_user)
    finally:
        await admin(
            host, port, user, f"DROP USER '{ro_user}'@'%'", f"DROP DATABASE {database} FORCE"
        )


def target(inst: Instance, **changes: object) -> StarRocksTarget:
    policy = SqlPolicy(
        allowed_functions=frozenset({"SUM", "COUNT", "SLEEP"}),
        max_rows=5,
        max_sql_bytes=4000,
        max_result_columns=10,
    )
    base = StarRocksTarget(
        target_id="sr-real",
        host=inst.host,
        port=inst.port,
        database=inst.database,
        tls=False,
        tls_ca_file=None,
        user=inst.ro_user,
        password_ref=f"env:{PASSWORD_ENV}",
        connect_timeout_seconds=5,
        query_timeout_seconds=30,
        client_timeout_seconds=30,
        pool_size=2,
        time_zone="Asia/Shanghai",
        query_mem_limit_bytes=2**30,
        max_result_bytes=4000,
        max_value_bytes=200,
        policy=policy,
        schema_limits=SchemaLimits(
            max_objects=200, max_columns=2000, max_bytes=500_000, max_comment_chars=100
        ),
    )
    return base.model_copy(update=changes) if changes else base


def scope(inst: Instance, t: StarRocksTarget, **extra: tuple[str, ...]) -> QueryPolicy:
    """Adapter 用例的 SQLGuard 范围：固定对象与列（含无权的 ungranted，用来验证数据库自己的拒绝），
    函数与上限取自 ``t``。结构快照给出的范围另由“自动结构快照”一节的用例验证。"""
    return QueryPolicy(
        target_id="sr-real",
        tables={
            inst.database: {
                "sales": ("id", "region", "total", "day", "at", "note"),
                "ungranted": ("id",),
                **extra,
            }
        },
        allowed_functions=t.policy.allowed_functions,
        max_rows=t.policy.max_rows,
        max_sql_bytes=t.policy.max_sql_bytes,
        max_result_columns=t.policy.max_result_columns,
    )


def adapter(inst: Instance, **changes: object) -> StarRocksAdapter:
    return open_starrocks(target(inst, **changes), clock=lambda: datetime.now(UTC))


def guarded(inst: Instance, sql: str, t: StarRocksTarget | None = None):  # type: ignore[no-untyped-def]
    return guard_readonly_query(sql, scope(inst, t or target(inst)))


async def test_types_and_session_limits(instance: Instance) -> None:
    query = guarded(
        instance,
        "SELECT id, region, total, day, at, note FROM sales WHERE id = 5 AND region LIKE 'r%'",
    )
    result = await adapter(instance).run_query(query)

    assert result.columns == ("id", "region", "total", "day", "at", "note")
    assert result.rows == (
        {
            "id": 5,
            "region": "r1",
            "total": "6.25",
            "day": "2026-09-06",
            "at": "2026-09-01T05:30:00+08:00",
            "note": "说明5",
        },
    )
    assert not result.truncated


async def test_row_limit_truncates_and_the_next_query_works(instance: Instance) -> None:
    ada = adapter(instance)
    first = await ada.run_query(guarded(instance, "SELECT id FROM sales ORDER BY id"))
    assert (first.row_count, first.truncated) == (5, True)
    assert [r["id"] for r in first.rows] == [0, 1, 2, 3, 4]
    assert "LIMIT 6" in first.sql

    second = await ada.run_query(guarded(instance, "SELECT COUNT(*) AS n FROM sales"))
    assert second.rows == ({"n": SALES_ROWS},)


async def test_byte_limit_stops_reading_mid_stream(instance: Instance) -> None:
    t = target(instance, max_result_bytes=60)
    ada = open_starrocks(t, clock=lambda: datetime.now(UTC))
    result = await ada.run_query(guarded(instance, "SELECT note FROM sales ORDER BY id", t))
    assert result.truncated and 0 < result.row_count < 5
    assert (
        await ada.run_query(guarded(instance, "SELECT id FROM sales LIMIT 1", t))
    ).row_count == 1


async def test_snapshot_holds_only_objects_the_account_can_select(instance: Instance) -> None:
    cache = SchemaCache(adapter(instance), clock=lambda: datetime.now(UTC))
    assert await cache.refresh()
    snapshot = cache.current()

    # 只授 SELECT 的 sales 进入快照；hidden 与 ungranted 没有任何授权。4.1.4 不支持列级授权：
    # 授权表的全部列（含 secret）都可读，列限制须由 DBA 用视图实现。
    assert set(snapshot.objects) == {(instance.database, "sales")}
    sales = snapshot.object(instance.database, "sales")
    assert sales is not None and sales.table_id and sales.created_at
    assert [c.name for c in sales.columns] == [
        "id",
        "region",
        "total",
        "day",
        "at",
        "note",
        "secret",
    ]
    assert snapshot.query_policy.tables == {
        instance.database: {"sales": ("id", "region", "total", "day", "at", "note", "secret")}
    }


async def test_ungranted_object_maps_to_permission_denied(instance: Instance) -> None:
    with pytest.raises(StarRocksError) as info:
        await adapter(instance).run_query(guarded(instance, "SELECT id FROM ungranted"))
    assert info.value.code is Code.PERMISSION_DENIED
    assert info.value.__context__ is None


async def test_read_only_account_cannot_write(instance: Instance) -> None:
    import os

    conn = await asyncmy.connect(
        host=instance.host,
        port=instance.port,
        user=instance.ro_user,
        password=os.environ[PASSWORD_ENV],
        database=instance.database,
        autocommit=True,
    )
    try:
        async with conn.cursor() as cursor:
            with pytest.raises(MySQLError):
                await cursor.execute("INSERT INTO sales (id) VALUES (999)")
    finally:
        await conn.ensure_closed()


async def test_server_query_timeout_stops_a_slow_query(instance: Instance) -> None:
    t = target(instance, query_timeout_seconds=1, client_timeout_seconds=10)
    ada = open_starrocks(t, clock=lambda: datetime.now(UTC))
    started = time.monotonic()
    with pytest.raises(StarRocksError) as info:
        await ada.run_query(guarded(instance, "SELECT SLEEP(5) AS s", t))
    assert info.value.code is Code.SERVER_TIMEOUT
    assert time.monotonic() - started < 5


async def test_client_deadline_and_cancellation_discard_the_connection(instance: Instance) -> None:
    t = target(instance, query_timeout_seconds=1, client_timeout_seconds=1, pool_size=1)
    ada = open_starrocks(t, clock=lambda: datetime.now(UTC))
    slow = guarded(instance, "SELECT SLEEP(3) AS s", t)

    task = asyncio.create_task(ada.run_query(slow))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 槽位已释放，新连接可用。
    assert (
        await ada.run_query(guarded(instance, "SELECT id FROM sales LIMIT 1", t))
    ).row_count == 1


async def test_tls_required_against_a_plaintext_server_fails_closed(instance: Instance) -> None:
    with pytest.raises(StarRocksError) as info:
        await adapter(instance, tls=True).run_query(guarded(instance, "SELECT id FROM sales"))
    assert info.value.code is Code.CONNECT_FAILED


async def test_wrong_password_maps_to_auth_failed(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PASSWORD_ENV, "wrong-" + secrets.token_hex(4))
    with pytest.raises(StarRocksError) as info:
        await adapter(instance).run_query(guarded(instance, "SELECT id FROM sales"))
    assert info.value.code is Code.AUTH_FAILED


async def test_driver_cursor_is_unbuffered(instance: Instance) -> None:
    """锁定版 asyncmy 的 SSCursor 逐行读取：取一行后中止，不会先缓存整个结果。"""
    import os

    conn = await asyncmy.connect(
        host=instance.host,
        port=instance.port,
        user=instance.ro_user,
        password=os.environ[PASSWORD_ENV],
        database=instance.database,
        autocommit=True,
    )
    cursor = conn.cursor(SSCursor)
    await cursor.execute("SELECT id FROM sales ORDER BY id")
    assert await cursor.fetchone() == (0,)
    assert cursor._result.unbuffered_active  # 结果仍在流上，未被整体读入
    conn.close()


@pytest.mark.parametrize(
    ("sql", "columns", "rows"),
    [
        (
            "SELECT id, total, at FROM sales WHERE id IN (3, 5) ORDER BY id",
            ("id", "total", "at"),
            (
                {"id": 3, "total": "3.75", "at": "2026-09-01T03:30:00+08:00"},
                {"id": 5, "total": "6.25", "at": "2026-09-01T05:30:00+08:00"},
            ),
        ),
        (
            "WITH q AS (SELECT id AS x, -id AS x, region AS label FROM sales "
            "ORDER BY 1 LIMIT 1) SELECT q.label FROM q",
            ("label",),
            ({"label": "r0"},),
        ),
        (
            "SELECT q.label FROM (SELECT id AS x, -id AS x, region AS label FROM sales "
            "ORDER BY 1 LIMIT 1) q",
            ("label",),
            ({"label": "r0"},),
        ),
    ],
    ids=["unique-control", "duplicate-in-cte", "duplicate-in-subquery"],
)
async def test_governed_query_tool_end_to_end(
    instance: Instance, sql: str, columns: tuple[str, ...], rows: tuple[dict[str, object], ...]
) -> None:
    """只读账号 → 治理 → SQLGuard → 真实驱动 → 证据（真实 PostgreSQL）→ Web 结构化事实。"""
    raw = os.environ.get("SDK_TEST_POSTGRES_URL")
    if not raw:
        pytest.fail("SDK_TEST_POSTGRES_URL 未设置：端到端用例需要测试 PostgreSQL 保存证据")
    try:
        parse_admin_url(raw)
    except HarnessMisconfiguredError as exc:
        pytest.fail(f"SDK_TEST_POSTGRES_URL {exc}")

    ada = adapter(instance)
    schema = SchemaCache(ada, clock=lambda: datetime.now(UTC))
    assert await schema.refresh()
    tools = starrocks_tools(
        ada, dict.fromkeys(("model", "session", "web", "feishu"), 200_000), schema=schema
    )
    grants = Grants()
    grants.grant("alice", *QUERY_TOOLS, target="sr-real")
    ctx = RunContext(
        identity=Identity(subject_id="alice", session_id="s1", turn_id="t1", channel="web"),
        target_scope=frozenset({"sr-real"}),
        tool_scope=QUERY_TOOLS,
        budget=Budget(max_turns=4, max_tool_calls=2, timeout_seconds=30.0, max_scope_checks=1000),
    )

    def call(sql: str) -> ToolRequest:
        return ToolRequest(
            tool_id=RUN_QUERY,
            target_id="sr-real",
            call_id=secrets.token_hex(4),
            tool_name="run_readonly_query",
            arguments={"cluster": "sr-real", "sql": sql},
        )

    async with isolated_database() as url, ready_engine(url) as engine:
        evidence = EvidenceStore(
            engine,
            ToolCatalog(tools.contracts, tools.policies),
            authorize=grants,
            clock=Clock(datetime.now(UTC)),
            retention_seconds=600,
            verify_dependencies=DependencyCheck({"sr-real": ada}),
        )
        governed = GovernedTools(evidence)
        # 不在快照中的对象（无权的 ungranted）在 I/O 前由 SQLGuard 拒绝。
        with pytest.raises(ToolRejectedError, match="object_not_allowed"):
            await governed.invoke(
                ctx, call("SELECT id FROM ungranted"), tools.executes[(RUN_QUERY, "sr-real")]
            )

        result = await governed.invoke(
            ctx,
            call(sql),
            tools.executes[(RUN_QUERY, "sr-real")],
        )
        answer = AgentAnswer(
            evidence_ids=(result.evidence_id,),
            inferences=[AnswerInference(text="查询结果", evidence_ids=(result.evidence_id,))],
            clarification=None,
        )
        delivery = await evidence.validate_answer(answer, ctx)

    (fact,) = delivery.facts
    assert fact.columns == columns
    assert fact.rows == rows
    assert fact.metadata["row_count"] == len(rows) and "`sales`" in str(fact.metadata["sql"])
    assert not fact.truncated


async def readonly_rows(
    inst: Instance, sql: str, *, mode: str | None = "ONLY_FULL_GROUP_BY"
) -> list[tuple[object, ...]]:
    """合成只读账号直接执行对照 SQL；不经过 Adapter，用来对比原文的服务器语义。"""
    conn = await asyncmy.connect(
        host=inst.host,
        port=inst.port,
        user=inst.ro_user,
        password=os.environ[PASSWORD_ENV],
        db=inst.database,
        autocommit=True,
    )
    try:
        async with conn.cursor() as cursor:
            if mode is not None:
                await cursor.execute("SET sql_mode = %s", (mode,))
            await cursor.execute(sql)
            return list(await cursor.fetchall())
    finally:
        await conn.ensure_closed()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id AS x, -id AS x FROM sales ORDER BY 1 LIMIT 3",
        "SELECT id AS total, total AS id FROM sales ORDER BY 1 DESC LIMIT 3",
        "SELECT region AS x, -id AS x, COUNT(*) AS n FROM sales "
        "GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2 LIMIT 3",
        "SELECT q.label FROM (SELECT id AS x, -id AS x, region AS label FROM sales "
        "ORDER BY 1 LIMIT 1) q",
        "SELECT id, region FROM sales ORDER BY 1 LIMIT 3",
    ],
    ids=["duplicate", "cross-alias", "grouped", "nested", "unique-control"],
)
async def test_order_ordinals_match_original_rows_and_plan(instance: Instance, sql: str) -> None:
    duplicate = "AS x" in sql and "q.label" not in sql
    query = explained(instance, sql) if duplicate else guarded(instance, sql)
    original = await readonly_rows(instance, sql)
    assert await readonly_rows(instance, query.normalized_sql) == original
    plan = await adapter(instance).explain(explained(instance, sql))
    expected = await readonly_rows(instance, "EXPLAIN LOGICAL " + sql)
    assert [row["plan"] for row in plan.rows] == [row[0] for row in expected]
    assert not plan.truncated
    if duplicate:  # 结果列重名无法交付：查询路径在任何 I/O 前要求唯一别名（P2.5 Task 3）
        with pytest.raises(QueryRejectedError) as refused:
            guarded(instance, sql)
        assert refused.value.code is QueryRejectionCode.AMBIGUOUS_REFERENCE


# ---- P2.5 Task 3：复杂 SQL、跨库与星号展开的服务器语义 ---------------------------------------

R3_FUNCTIONS = frozenset(
    {"SUM", "COUNT", "MAX", "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD"}
    | {"TRIM", "CAST", "COALESCE"}
)
R3_SAMPLES = {
    "union all": "SELECT region FROM {d}.sales WHERE id < 6 UNION ALL "
    "SELECT region FROM {h}.staff ORDER BY 1 DESC LIMIT 7",
    "union by name": "SELECT region AS r, id AS k FROM sales WHERE id < 5 UNION "
    "SELECT name, id FROM {h}.staff ORDER BY k DESC, r",
    "branch limits": "(SELECT id FROM sales ORDER BY id LIMIT 2) UNION ALL "
    "(SELECT id FROM {h}.staff ORDER BY id DESC LIMIT 2) ORDER BY 1",
    "windows": "SELECT id, region, ROW_NUMBER() OVER (PARTITION BY region ORDER BY id DESC) AS rn, "
    "RANK() OVER (ORDER BY region) AS rk, DENSE_RANK() OVER (ORDER BY region) AS dr, "
    "LAG(id, 1) OVER (PARTITION BY region ORDER BY id) AS prev, "
    "LEAD(id) OVER (ORDER BY id) AS nxt, "
    "SUM(total) OVER (PARTITION BY region) AS s FROM sales ORDER BY id LIMIT 12",
    "nested ctes": "WITH a AS (SELECT region, total FROM sales WHERE id < 20), "
    "b AS (SELECT region, SUM(total) AS s, COUNT(DISTINCT total) AS n FROM a GROUP BY region) "
    "SELECT b.region, b.s, b.n, CASE WHEN b.n > 3 THEN 'many' ELSE 'few' END AS k "
    "FROM b ORDER BY b.region",
    "correlated exists": "SELECT s.id FROM sales s WHERE EXISTS (SELECT 1 FROM {h}.staff t "
    "WHERE t.region = s.region AND t.id > s.id - 30) ORDER BY s.id LIMIT 8",
    "correlated scalar": "SELECT s.id, (SELECT MAX(t.id) FROM {h}.staff t "
    "WHERE t.region = s.region) AS m FROM sales s ORDER BY s.id LIMIT 8",
    "cross database": "SELECT s.id, t.name FROM sales s JOIN {h}.staff t ON s.region = t.region "
    "ORDER BY s.id, t.name LIMIT 9",
    "star": "SELECT * FROM {h}.staff ORDER BY id",
    "alias star": "SELECT s.*, t.name AS staff_name FROM sales s JOIN {h}.staff t "
    "ON s.region = t.region ORDER BY s.id, t.name LIMIT 4",
    "column case": "SELECT ID, Region FROM sales ORDER BY ID LIMIT 3",
    "functions": "SELECT DISTINCT TRIM(note) AS n, CAST(total AS INT) AS t, "
    "COALESCE(secret, '-') AS c FROM sales ORDER BY t LIMIT 3",
}


async def readonly_result(inst: Instance, sql: str) -> tuple[list[str], list[tuple[object, ...]]]:
    """只读账号直接执行（固定与小维相同的 sql_mode）：列头与行。"""
    conn = await asyncmy.connect(
        host=inst.host,
        port=inst.port,
        user=inst.ro_user,
        password=os.environ[PASSWORD_ENV],
        db=inst.database,
        autocommit=True,
    )
    try:
        async with conn.cursor() as cursor:
            await cursor.execute("SET sql_mode = 'ONLY_FULL_GROUP_BY'")
            await cursor.execute(sql)
            return [d[0] for d in cursor.description], list(await cursor.fetchall())
    finally:
        await conn.ensure_closed()


async def test_r3_sql_keeps_server_rows_headers_and_order(instance: Instance) -> None:
    """原 SQL 与规范化 SQL 在 4.1.4 上逐行（含顺序）与列头一致；范围来自真实结构快照。"""
    host, port, user = admin_address()
    hr = f"{instance.database}_hr"
    table = "DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES('replication_num'='1')"
    await admin(
        host,
        port,
        user,
        f"CREATE DATABASE {hr}",
        f"CREATE TABLE {hr}.staff (id INT, name VARCHAR(16), region VARCHAR(16)) {table}",
        f"INSERT INTO {hr}.staff VALUES (1, 'Ann', 'r1'), (2, 'Bob', 'r2'), (3, 'Cy', 'r1'), "
        "(4, 'Di', 'r9')",
        f"GRANT SELECT ON TABLE {hr}.staff TO USER '{instance.ro_user}'@'%'",
    )
    try:
        policy = SqlPolicy(
            allowed_functions=R3_FUNCTIONS, max_rows=50, max_sql_bytes=4000, max_result_columns=12
        )
        ada = adapter(instance, policy=policy)
        cache = SchemaCache(ada, clock=lambda: datetime.now(UTC))
        assert await cache.refresh()
        scope = cache.current().query_policy
        assert set(scope.tables) == {instance.database, hr}
        for name, template in R3_SAMPLES.items():
            sql = template.format(d=instance.database, h=hr)
            query = guard_readonly_query(sql, scope)
            original = await readonly_result(instance, sql)
            assert original[1], name  # 对照有数据，不是两个空结果相等
            assert await readonly_result(instance, query.normalized_sql) == original, name
        # 经 Adapter 的跨库查询：列头与行数与直接执行一致。
        cross = R3_SAMPLES["cross database"].format(h=hr)
        result = await ada.run_query(guard_readonly_query(cross, scope))
        headers, rows = await readonly_result(instance, cross)
        assert list(result.columns) == headers and result.row_count == len(rows)
    finally:
        await admin(host, port, user, f"DROP DATABASE {hr} FORCE")


# 名字解析：WHERE/ON/窗口只认物理列，投影/GROUP BY/HAVING 先物理列，顶层 ORDER BY 先别名；
# CTE/派生表的输出名沿用写法；星号按展开后的真实列名检查重名，库.表.* 检查库名。
R3_NAME_SAMPLES = {
    "window shadow": "SELECT id AS REGION, ROW_NUMBER() OVER (ORDER BY REGION, id DESC) AS n "
    "FROM sales ORDER BY 1 LIMIT 8",
    "where shadow": "SELECT id AS Region FROM sales WHERE Region = 'r1' ORDER BY 1 LIMIT 5",
    "having shadow": "SELECT region AS total, COUNT(*) AS n FROM sales GROUP BY region, total "
    "HAVING total > 10 ORDER BY 1, 2 LIMIT 5",
    "projection shadow": "SELECT id AS total, total + 0 AS t2 FROM sales ORDER BY 1 LIMIT 3",
    "order alias": "SELECT total AS x FROM sales ORDER BY x DESC LIMIT 3",
    "order same column": "SELECT s.region AS REGION, s.id FROM sales s "
    "ORDER BY region, s.id LIMIT 4",
    "distinct stars": "SELECT * FROM (SELECT s.*, b.* FROM sales s JOIN {h}.bonus b "
    "ON s.id = b.bid) q ORDER BY id",
    "cte names": "WITH q AS (SELECT ID, Region FROM sales) "
    "SELECT ID, region FROM q ORDER BY 1 LIMIT 3",
    "cte star": "WITH q AS (SELECT ID FROM sales) SELECT * FROM q ORDER BY 1 LIMIT 3",
    "nested stars": "SELECT * FROM (SELECT * FROM (SELECT Total FROM sales) a) b "
    "ORDER BY 1 LIMIT 3",
    "database star": "SELECT {d}.sales.* FROM sales ORDER BY id LIMIT 2",
    # 顶层 ORDER BY 的唯一输出别名：同名物理列存在（含两个来源都有）时仍按别名排序。
    "order aggregate alias": "SELECT SUM(id) AS total FROM sales GROUP BY region ORDER BY total",
    "order join alias": "SELECT s.id AS region FROM sales s JOIN {h}.staff t ON s.id = t.id "
    "ORDER BY region",
    "order alias case": "SELECT id AS Total FROM sales ORDER BY total DESC LIMIT 3",
    # 裸列的隐式输出名：两个来源都有 region，服务器按唯一输出 s.region 排序。
    "order implicit output": "SELECT s.region FROM sales s JOIN {h}.staff t ON s.id = t.id "
    "ORDER BY region DESC",
    # 另一同名列来自其他来源，或输出是表达式：服务器仍按输出列排序（LIMIT 截取的行不同于物理列）。
    "order other source": "SELECT s.id AS region, t.region AS tr FROM sales s "
    "JOIN {h}.staff t ON s.id = t.id ORDER BY region DESC LIMIT 1",
    "order self join": "SELECT a.id AS region, (b.region) AS br FROM sales a JOIN sales b "
    "ON a.id = b.id ORDER BY region DESC LIMIT 3",
    "order expression output": "SELECT id + 0 AS region, (region) AS r2 FROM sales "
    "ORDER BY region LIMIT 3",
    # 排序名在投影表达式、分组与窗口中出现：服务器按物理列 region 排序（复审 N1，原先被改写为
    # 按别名 id 排序后执行）；标量子查询别名（复审 N2，原先内联后被服务器拒绝）；同一 CTE 的两个
    # 关系别名（原先误拒）。
    "order expression match": "SELECT id AS region, TRIM(region) AS r2 FROM sales "
    "ORDER BY TRIM(region), id LIMIT 3",
    "order group match": "SELECT id AS region, COUNT(*) AS n FROM sales GROUP BY id, region "
    "ORDER BY region, id LIMIT 3",
    "order window match": "SELECT id, id AS region, ROW_NUMBER() OVER (PARTITION BY day "
    "ORDER BY region) AS n FROM sales ORDER BY region, id LIMIT 3",
    "order scalar correlated": "SELECT s.id, (SELECT MAX(b.amount) FROM {h}.bonus b "
    "WHERE b.bid = s.id) AS z FROM sales s ORDER BY z DESC, s.id LIMIT 3",
    "order scalar plain": "SELECT s.id, (SELECT COUNT(*) FROM {h}.bonus) AS z FROM sales s "
    "ORDER BY z, s.id LIMIT 2",
    "order scalar wrapped": "SELECT s.id, COALESCE((SELECT MAX(b.amount) FROM {h}.bonus b "
    "WHERE b.bid = s.id), 0) AS z FROM sales s ORDER BY z + 0 DESC, s.id LIMIT 3",
    "order cte self join": "WITH c AS (SELECT * FROM sales) SELECT a.id AS region, "
    "b.region AS br FROM c a JOIN c b ON a.id = b.id ORDER BY region, a.id LIMIT 3",
    # 相关子查询：本层没有 id、WHERE 看不到本层别名，id 是外层 s.id（只匹配 bonus 中的 1、2、5）。
    "correlated outer": "SELECT s.id FROM sales s WHERE EXISTS "
    "(SELECT b.bid AS id FROM {h}.bonus b WHERE id = b.bid) ORDER BY 1",
}
R3_NAME_REJECTED = {
    # 原 SQL 在服务器上报错：不能被规范化“修正”后执行。
    "star wrong database": (
        "SELECT nope.sales.* FROM sales",
        QueryRejectionCode.COLUMN_NOT_ALLOWED,
    ),
    "where alias": ("SELECT id AS x FROM sales WHERE x > 1", QueryRejectionCode.COLUMN_NOT_ALLOWED),
    "window alias": (
        "SELECT id AS x, ROW_NUMBER() OVER (ORDER BY x) AS n FROM sales",
        QueryRejectionCode.COLUMN_NOT_ALLOWED,
    ),
    "where ambiguous": (
        "SELECT s.id AS region FROM sales s JOIN {h}.staff t ON s.id = t.id WHERE region = 'r1'",
        QueryRejectionCode.AMBIGUOUS_REFERENCE,
    ),
    "correlated two outer": (
        "SELECT s.id FROM sales s JOIN {h}.staff t ON s.id = t.id WHERE EXISTS "
        "(SELECT b.bid AS region FROM {h}.bonus b WHERE region = 'r1')",
        QueryRejectionCode.AMBIGUOUS_REFERENCE,
    ),
}


async def test_r3_names_resolve_like_the_server(instance: Instance) -> None:
    """名字解析在 4.1.4 上与原 SQL 一致：查询路径比较列头、行与顺序，计划路径比较计划。"""
    host, port, user = admin_address()
    hr = f"{instance.database}_nm"
    table = "DUPLICATE KEY({}) DISTRIBUTED BY HASH({}) BUCKETS 1 PROPERTIES('replication_num'='1')"
    await admin(
        host,
        port,
        user,
        f"CREATE DATABASE {hr}",
        f"CREATE TABLE {hr}.staff (id INT, region VARCHAR(16)) {table.format('id', 'id')}",
        f"CREATE TABLE {hr}.bonus (bid INT, amount INT) {table.format('bid', 'bid')}",
        f"INSERT INTO {hr}.staff VALUES (1, 'r1'), (2, 'r2')",
        f"INSERT INTO {hr}.bonus VALUES (1, 10), (2, 20), (5, 50)",
        f"GRANT SELECT ON ALL TABLES IN DATABASE {hr} TO USER '{instance.ro_user}'@'%'",
    )
    try:
        policy = SqlPolicy(
            allowed_functions=R3_FUNCTIONS, max_rows=50, max_sql_bytes=4000, max_result_columns=12
        )
        ada = adapter(instance, policy=policy)
        cache = SchemaCache(ada, clock=lambda: datetime.now(UTC))
        assert await cache.refresh()
        scope = cache.current().query_policy
        for name, template in R3_NAME_SAMPLES.items():
            sql = template.format(d=instance.database, h=hr)
            original = await readonly_result(instance, sql)
            assert original[1], name
            query = guard_readonly_query(sql, scope)
            assert await readonly_result(instance, query.normalized_sql) == original, name
            plan = await ada.explain(guard_explain_query(sql, scope))
            expected = await readonly_rows(instance, "EXPLAIN LOGICAL " + sql)
            assert [row["plan"] for row in plan.rows] == [row[0] for row in expected], name
        for name, (template, code) in R3_NAME_REJECTED.items():
            sql = template.format(h=hr)
            with pytest.raises(MySQLError):
                await readonly_result(instance, sql)
            for check in (guard_readonly_query, guard_explain_query):
                with pytest.raises(QueryRejectedError) as refused:
                    check(sql, scope)
                assert refused.value.code is code, name
        # 交叉别名：输出是列、投影另有同一来源的同名列（括号、限定写法不计）时，服务器改按那一列
        # 排序，与按别名排序截取的行不同。排序名原样保留，规范化后的结果与原文一致。
        for crossed, by_alias in (
            ("SELECT id AS region, region AS id FROM sales ORDER BY id", None),
            ("SELECT id AS total, total AS t2 FROM sales ORDER BY total", None),
            (
                "SELECT id AS region, (region) AS r2 FROM sales ORDER BY region LIMIT 3",
                "SELECT id AS region, (region) AS r2 FROM sales ORDER BY id LIMIT 3",
            ),
            (
                "SELECT id AS region, ((s.region)) AS r2 FROM sales s ORDER BY region LIMIT 3",
                "SELECT id AS region, ((s.region)) AS r2 FROM sales s ORDER BY id LIMIT 3",
            ),
            (  # 同一来源 s；另一来源 b 也有 region
                "SELECT s.id AS region, s.region AS sr FROM sales s JOIN sales b "
                "ON s.id = b.id ORDER BY region LIMIT 3",
                "SELECT s.id AS region, s.region AS sr FROM sales s JOIN sales b "
                "ON s.id = b.id ORDER BY s.id LIMIT 3",
            ),
        ):
            server = await readonly_result(instance, crossed)
            assert server[1]
            if by_alias is not None:
                assert server != await readonly_result(instance, by_alias), crossed
            normalized = guard_readonly_query(crossed, scope).normalized_sql
            assert await readonly_result(instance, normalized) == server, crossed
            plan = await ada.explain(guard_explain_query(crossed, scope))
            expected = await readonly_rows(instance, "EXPLAIN LOGICAL " + crossed)
            assert [row["plan"] for row in plan.rows] == [row[0] for row in expected], crossed
        # 原文在服务器报错（排序名指向未分组的列）：规范化后同样报错，不被改写成功执行。
        invalid = "SELECT id AS total, SUM(total) AS s FROM sales GROUP BY id ORDER BY total"
        with pytest.raises(MySQLError):
            await readonly_result(instance, invalid)
        with pytest.raises(MySQLError):
            await readonly_result(instance, guard_readonly_query(invalid, scope).normalized_sql)
        with pytest.raises(MySQLError):
            await readonly_rows(
                instance, "EXPLAIN LOGICAL " + guard_explain_query(invalid, scope).normalized_sql
            )
        # 相关子查询的依赖记入外层列。
        correlated = R3_NAME_SAMPLES["correlated outer"].format(h=hr)
        assert (instance.database, "sales", "id") in guard_readonly_query(
            correlated, scope
        ).referenced_columns
    finally:
        await admin(host, port, user, f"DROP DATABASE {hr} FORCE")


# ORDER BY 绑定的产品路径：数据使排序名指向别名与指向同名物理列时取到不同的行（复审 N1/N2）。
ORDER_FACTS = {
    "expression": "SELECT t.a AS x, t.x + 0 AS xx FROM {o}.t t ORDER BY x + 0, t.id LIMIT 1",
    "group": "SELECT t.a AS x, COUNT(*) AS n FROM {o}.t t GROUP BY t.a, t.x ORDER BY x LIMIT 1",
    "group order term": "SELECT t.a AS x, COUNT(*) AS n FROM {o}.t t GROUP BY t.a, t.x "
    "ORDER BY t.x DESC, t.a LIMIT 1",
    "window": "SELECT t.id, t.a AS x, ROW_NUMBER() OVER (PARTITION BY t.g ORDER BY t.x) AS n "
    "FROM {o}.t t ORDER BY x, t.id LIMIT 1",
    "crossed": "SELECT t.a AS x, (t.x) AS xx FROM {o}.t t ORDER BY x LIMIT 1",
    "scalar": "SELECT t.id, (SELECT MAX(u.a) FROM {o}.u u WHERE u.id = t.id) AS z FROM {o}.t t "
    "ORDER BY z, t.id LIMIT 1",
    "scalar expression": "SELECT t.id, COALESCE((SELECT MAX(u.a) FROM {o}.u u "
    "WHERE u.id = t.id), 0) AS z FROM {o}.t t ORDER BY z + 0 DESC, t.id LIMIT 1",
    "cte self join": "WITH c AS (SELECT * FROM {o}.t) SELECT a.a AS x, b.x AS xx "
    "FROM c a JOIN c b ON a.id = b.id ORDER BY x, a.id LIMIT 1",
    "unique control": "SELECT t.a AS ax, t.x + 0 AS xx FROM {o}.t t ORDER BY ax, t.id LIMIT 1",
}


async def test_order_by_facts_keep_the_original_binding(instance: Instance) -> None:
    """治理 → SQLGuard → 真实驱动 → 证据（真实 PostgreSQL）→ 最终事实：查询的行与原文一致，
    计划与原文的 EXPLAIN LOGICAL 一致；原文报错的排序引用在两条路径都报错，不产生事实。"""
    raw = os.environ.get("SDK_TEST_POSTGRES_URL")
    if not raw:
        pytest.fail("SDK_TEST_POSTGRES_URL 未设置：端到端用例需要测试 PostgreSQL 保存证据")
    host, port, user = admin_address()
    ob = f"{instance.database}_ob"
    table = "DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES('replication_num'='1')"
    await admin(
        host,
        port,
        user,
        f"CREATE DATABASE {ob}",
        f"CREATE TABLE {ob}.t (id INT, a INT, x INT, g INT) {table}",
        f"CREATE TABLE {ob}.u (id INT, a INT, x INT, g INT) {table}",
        f"INSERT INTO {ob}.t VALUES (1, 3, 10, 1), (2, 1, 30, 1), (3, 2, 20, 2)",
        f"INSERT INTO {ob}.u VALUES (1, 30, 3, 1), (2, 10, 1, 2), (3, 20, 2, 2)",
        f"GRANT SELECT ON ALL TABLES IN DATABASE {ob} TO USER '{instance.ro_user}'@'%'",
    )
    try:
        policy = SqlPolicy(
            allowed_functions=R3_FUNCTIONS, max_rows=50, max_sql_bytes=4000, max_result_columns=12
        )
        ada = adapter(instance, policy=policy)
        schema = SchemaCache(ada, clock=lambda: datetime.now(UTC))
        assert await schema.refresh()
        tools = starrocks_tools(
            ada, dict.fromkeys(("model", "session", "web", "feishu"), 200_000), schema=schema
        )
        grants = Grants()
        grants.grant("alice", *QUERY_TOOLS, target="sr-real")
        async with isolated_database() as url, ready_engine(url) as engine:
            evidence = EvidenceStore(
                engine,
                ToolCatalog(tools.contracts, tools.policies),
                authorize=grants,
                clock=Clock(datetime.now(UTC)),
                retention_seconds=600,
                verify_dependencies=DependencyCheck({"sr-real": ada}),
            )
            governed = GovernedTools(evidence)
            calls = 0

            async def deliver(tool_id: str, sql: str) -> Any:
                nonlocal calls
                calls += 1
                ctx = RunContext(
                    identity=Identity(
                        subject_id="alice", session_id=f"s{calls}", turn_id="t1", channel="web"
                    ),
                    target_scope=frozenset({"sr-real"}),
                    tool_scope=QUERY_TOOLS,
                    budget=Budget(
                        max_turns=4, max_tool_calls=2, timeout_seconds=30.0, max_scope_checks=1000
                    ),
                )
                request = ToolRequest(
                    tool_id=tool_id,
                    target_id="sr-real",
                    call_id=secrets.token_hex(4),
                    tool_name=tool_id.split("/")[1],
                    arguments={"cluster": "sr-real", "sql": sql},
                )
                result = await governed.invoke(ctx, request, tools.executes[(tool_id, "sr-real")])
                answer = AgentAnswer(
                    evidence_ids=(result.evidence_id,),
                    inferences=[
                        AnswerInference(text="合成数据", evidence_ids=(result.evidence_id,))
                    ],
                    clarification=None,
                )
                (fact,) = (await evidence.validate_answer(answer, ctx)).facts
                return fact

            for name, template in ORDER_FACTS.items():
                sql = template.format(o=ob)
                headers, rows = await readonly_result(instance, sql)
                assert rows, name
                fact = await deliver(RUN_QUERY, sql)
                assert list(fact.columns) == headers, name
                assert [tuple(row.values()) for row in fact.rows] == rows, name
                plan = await deliver(EXPLAIN_QUERY, sql)
                expected = await readonly_rows(instance, "EXPLAIN LOGICAL " + sql)
                assert [row["plan"] for row in plan.rows] == [row[0] for row in expected], name
            # 排序数据确实能区分两种绑定：按别名排序会取到另一行。
            by_alias = f"SELECT t.a AS x, t.x + 0 AS xx FROM {ob}.t t ORDER BY t.a, t.id LIMIT 1"
            assert (await readonly_result(instance, by_alias))[1] != (
                await readonly_result(instance, ORDER_FACTS["expression"].format(o=ob))
            )[1]
            # 原文报错（排序名指向未分组的 t.x）：两条路径都在数据库报错，不被改写成功。
            invalid = f"SELECT t.a AS x, SUM(t.x) AS n FROM {ob}.t t GROUP BY t.a ORDER BY x"
            with pytest.raises(MySQLError):
                await readonly_result(instance, invalid)
            for tool_id in (RUN_QUERY, EXPLAIN_QUERY):
                with pytest.raises(ToolExecutionError):
                    await deliver(tool_id, invalid)
    finally:
        await admin(host, port, user, f"DROP DATABASE {ob} FORCE")


async def test_sql_mode_is_pinned_when_server_default_changes(instance: Instance) -> None:
    host, port, user = admin_address()
    ((previous,),) = await admin_rows(host, port, user, "SELECT @@GLOBAL.sql_mode")
    # 仅对本 fixture 的可丢弃实例临时设置全局值，并在 finally 恢复和回读。
    assert isinstance(previous, str) and all(c.isupper() or c in "_," for c in previous)
    await admin(host, port, user, "SET GLOBAL sql_mode = 'PIPES_AS_CONCAT'")
    try:
        assert await readonly_rows(instance, "SELECT 'a' || 'x'", mode=None) == [("ax",)]
        ada = adapter(instance)
        query = guarded(instance, "SELECT 'a' || 'x' AS value")
        assert (await ada.run_query(query)).rows == ({"value": None},)
        assert (await ada.explain(explained(instance, "SELECT 'a' || 'x' AS value"))).rows
        # 不固定模式时，这个缺少 GROUP BY 的 SQL 在临时全局模式下会成功；固定后两条路径均拒绝。
        bad = "SELECT region, COUNT(*) AS n FROM sales"
        assert await readonly_rows(instance, bad, mode=None)
        for plan in (False, True):
            with pytest.raises(StarRocksError) as refused:
                if plan:
                    await ada.explain(explained(instance, bad))
                else:
                    await ada.run_query(guarded(instance, bad))
            assert refused.value.code is Code.QUERY_FAILED
        # SQLGuard/Adapter 的设置没有修改全局值。
        assert await readonly_rows(instance, "SELECT @@sql_mode", mode=None) == [
            ("PIPES_AS_CONCAT",)
        ]
    finally:
        await admin(host, port, user, f"SET GLOBAL sql_mode = '{previous}'")
        assert await admin_rows(host, port, user, "SELECT @@GLOBAL.sql_mode") == [(previous,)]


# ---- 执行计划（P2 Task 3） -----------------------------------------------------------------


def explained(  # type: ignore[no-untyped-def]
    inst: Instance, sql: str, t: StarRocksTarget | None = None, **extra: tuple[str, ...]
):
    return guard_explain_query(sql, scope(inst, t or target(inst), **extra))


def plan_text(result: object) -> str:
    return "\n".join(str(row["plan"]) for row in result.rows)  # type: ignore[attr-defined]


async def test_explain_table_view_and_cte(instance: Instance) -> None:
    host, port, user = admin_address()
    await admin(
        host,
        port,
        user,
        f"CREATE VIEW {instance.database}.sales_view AS "
        f"SELECT id, region, total FROM {instance.database}.sales WHERE total > 10",
        f"GRANT SELECT ON VIEW {instance.database}.sales_view TO USER '{instance.ro_user}'@'%'",
    )
    t = target(instance)
    ada = open_starrocks(t, clock=lambda: datetime.now(UTC))
    view = {"sales_view": ("id", "region", "total")}

    for sql in (
        "SELECT region, SUM(total) AS s FROM sales WHERE id > 3 GROUP BY region",
        "SELECT v.region FROM sales_view v JOIN sales s ON v.id = s.id",
        "WITH t AS (SELECT region, total FROM sales) SELECT region FROM t WHERE total > 1",
    ):
        plan = explained(instance, sql, t, **view)
        result = await ada.explain(plan)
        text = plan_text(result)
        assert result.sql == EXPLAIN_PREFIX + plan.normalized_sql
        assert result.columns == ("plan",) and result.row_count > 0 and not result.truncated
        assert "SCAN [" in text and "sales" in text
        # LOGICAL 不含列统计值（COSTS）与资源组（VERBOSE），Task 0 的披露结论在本环境复核。
        assert "column statistics" not in text and "RESOURCE GROUP" not in text


async def test_explain_ungranted_object_maps_to_permission_denied(instance: Instance) -> None:
    with pytest.raises(StarRocksError) as info:
        await adapter(instance).explain(explained(instance, "SELECT id FROM ungranted"))
    assert info.value.code is Code.PERMISSION_DENIED
    assert info.value.__context__ is None


async def test_explain_under_tight_session_limits_and_plan_truncation(instance: Instance) -> None:
    tight = target(
        instance, query_timeout_seconds=1, client_timeout_seconds=10, query_mem_limit_bytes=2**20
    )
    ada = open_starrocks(tight, clock=lambda: datetime.now(UTC))
    sql = "SELECT a.region FROM sales a JOIN sales b ON a.region = b.region"
    full = await ada.explain(explained(instance, sql, tight))
    assert full.row_count > 2 and not full.truncated

    short = target(instance, max_plan_lines=2)
    cut = await open_starrocks(short, clock=lambda: datetime.now(UTC)).explain(
        explained(instance, sql, short)
    )
    assert (cut.row_count, cut.truncated) == (2, True)
    assert [r["plan"] for r in cut.rows] == [r["plan"] for r in full.rows[:2]]


async def _explain_level(host: str, port: int, user: str) -> str:
    rows = await admin_rows(
        host, port, user, "ADMIN SHOW FRONTEND CONFIG LIKE 'query_explain_level'"
    )
    (row,) = rows
    return str(row[-1] if len(row) < 3 else row[2])


async def _profiles(host: str, port: int, user: str) -> int:
    return len(await admin_rows(host, port, user, "SHOW PROFILELIST"))


async def test_explicit_level_does_not_execute_when_fe_default_is_analyze(
    instance: Instance,
) -> None:
    """管理账号把 FE 默认级别改为 ANALYZE：Adapter 的显式 LOGICAL 仍只取计划。

    反例：执行期必失败（``assert_true``）与必超时（``SLEEP``）的查询都正常返回计划，且不新增
    Profile；同一配置下裸 ``EXPLAIN`` 确实执行（阳性对照），证明本环境的检测手段有效。
    """
    host, port, user = admin_address()
    assert await _explain_level(host, port, user) == "NORMAL"
    base = target(instance, query_timeout_seconds=1, client_timeout_seconds=10)
    t = base.model_copy(
        update={
            "policy": base.policy.model_copy(
                update={"allowed_functions": base.policy.allowed_functions | {"ASSERT_TRUE"}}
            )
        }
    )
    ada = open_starrocks(t, clock=lambda: datetime.now(UTC))
    await admin(host, port, user, "ADMIN SET FRONTEND CONFIG ('query_explain_level' = 'ANALYZE')")
    try:
        assert await _explain_level(host, port, user) == "ANALYZE"
        before = await _profiles(host, port, user)
        started = time.monotonic()
        failing = await ada.explain(
            explained(instance, "SELECT ASSERT_TRUE(total < 0) AS a FROM sales", t)
        )
        sleeping = await ada.explain(explained(instance, "SELECT SLEEP(5) AS s FROM sales", t))
        assert time.monotonic() - started < 5
        assert failing.sql.startswith("EXPLAIN LOGICAL ") and sleeping.row_count > 0
        assert "assert_true" in plan_text(failing).lower()
        await asyncio.sleep(1)  # Profile 异步登记，留出与阳性对照相同的时间
        assert await _profiles(host, port, user) == before

        # 阳性对照：同一配置下裸 EXPLAIN 走 ANALYZE 执行路径。
        with pytest.raises(MySQLError) as executed:
            await admin(
                host,
                port,
                user,
                f"EXPLAIN SELECT assert_true(total < 0) FROM {instance.database}.sales",
            )
        assert "assert_true" in str(executed.value).lower()
        await admin_rows(
            host, port, user, f"EXPLAIN SELECT count(id) FROM {instance.database}.sales"
        )
        await asyncio.sleep(1)
        assert await _profiles(host, port, user) > before
    finally:
        await admin(
            host, port, user, "ADMIN SET FRONTEND CONFIG ('query_explain_level' = 'NORMAL')"
        )
        assert await _explain_level(host, port, user) == "NORMAL"


# ---- 表布局（P2 Task 5）---------------------------------------------------------------------


async def test_describe_layout_on_real_tables(instance: Instance) -> None:
    """``tables_config`` 在 4.1.4 上的实际取值：键过滤、表达式分区、主键表、视图与无权限表。"""
    host, port, user = admin_address()
    db, ro = instance.database, instance.ro_user
    props = "PROPERTIES('replication_num'='1')"
    await admin(
        host,
        port,
        user,
        f"CREATE TABLE {db}.events (ts DATETIME, region VARCHAR(16), amount INT, "
        f"secret VARCHAR(16)) DUPLICATE KEY(ts, region) PARTITION BY date_trunc('day', ts) "
        f"DISTRIBUTED BY HASH(region) BUCKETS 2 {props}",
        f"CREATE TABLE {db}.pk_orders (id INT NOT NULL, secret VARCHAR(16) NOT NULL, total INT) "
        f"PRIMARY KEY(id, secret) DISTRIBUTED BY HASH(id) BUCKETS 3 {props}",
        f"CREATE VIEW {db}.sales_view AS SELECT id, region FROM {db}.sales",
        f"GRANT SELECT ON TABLE {db}.events TO USER '{ro}'@'%'",
        f"GRANT SELECT ON TABLE {db}.pk_orders TO USER '{ro}'@'%'",
        f"GRANT SELECT ON VIEW {db}.sales_view TO USER '{ro}'@'%'",
    )
    ada = adapter(instance)
    # 键字段按给定的列过滤：这里故意不给 secret，验证含未给出列的键整段不显示。
    sales_cols = frozenset({"id", "region", "total", "day", "at", "note"})
    (sales,) = (await ada.describe_layout(db, "sales", sales_cols)).rows
    assert sales == {
        "model": "DUP_KEYS",
        "partition_key": "",
        "distribute_type": "HASH",
        "distribute_key": "id",
        "buckets": 1,
        "sort_key": "id",
        "primary_key": "",
    }
    # 表达式分区在 4.1.4 只显示列名；分桶键与排序键都是获准列。
    (events,) = (
        await ada.describe_layout(db, "events", frozenset({"ts", "region", "amount"}))
    ).rows
    assert events["partition_key"] == "ts"
    assert (events["distribute_key"], events["buckets"]) == ("region", 2)
    assert events["sort_key"] == "ts, region"
    # 主键含未获准列 secret：整段不显示，获准的 id 也不单独留下。
    (orders,) = (await ada.describe_layout(db, "pk_orders", frozenset({"id", "total"}))).rows
    assert orders["model"] == "PRIMARY_KEYS"
    assert orders["primary_key"] == LAYOUT_HIDDEN and orders["distribute_key"] == "id"
    assert "secret" not in str(orders)
    # 视图没有布局；无 SELECT 权限的表对只读账号不可见，同样没有行。
    assert (await ada.describe_layout(db, "sales_view", frozenset({"id", "region"}))).rows == ()
    assert (await ada.describe_layout(db, "ungranted", frozenset({"id"}))).rows == ()


# ---- 自动结构快照与当前权限（P2.5 Task 2）-------------------------------------------------------


def schema_tools(inst: Instance) -> tuple[SchemaCache, Any]:
    ada = adapter(inst)
    cache = SchemaCache(ada, clock=lambda: datetime.now(UTC))
    tools = starrocks_tools(
        ada, dict.fromkeys(("model", "session", "web", "feishu"), 200_000), schema=cache
    )
    return cache, tools.executes


async def call_tool(executes: Any, tool_id: str, **arguments: object) -> Any:
    execute = executes[(tool_id, "sr-real")]
    request = ToolRequest(
        tool_id=tool_id,
        target_id="sr-real",
        call_id=secrets.token_hex(4),
        tool_name=tool_id.removeprefix("local/"),
        arguments={"cluster": "sr-real", **arguments},
    )
    return await execute.run(execute.check(request))


def search_page(database: str | None, keyword: str = ".", **changes: object) -> dict[str, object]:
    """搜表参数（P2.5 Task 6）：默认第 1 页、页大小取 ``target`` 的 ``max_rows``。"""
    return {
        "keyword": keyword,
        "database": database,
        "page": 1,
        "page_size": 5,
        "snapshot": None,
        **changes,
    }


async def test_search_matches_real_comments_and_columns_and_pages_by_snapshot(
    instance: Instance,
) -> None:
    """搜表（P2.5 Task 6）：关键词匹配真实元数据中的表注释（按 ``max_comment_chars`` 截取后的
    内容）与列名；页按快照版本划分，刷新后旧版本在 I/O 前拒绝。"""
    host, port, user = admin_address()
    db, ro = instance.database, instance.ro_user
    comment = "订单流水" + "x" * 120 + "tailword"  # 采集时截取前 100 个字符
    await admin(
        host,
        port,
        user,
        f"CREATE TABLE {db}.orders_log (id INT, customer_ref VARCHAR(8)) DUPLICATE KEY(id) "
        f"COMMENT '{comment}' DISTRIBUTED BY HASH(id) BUCKETS 1 "
        "PROPERTIES('replication_num'='1')",
        f"GRANT SELECT ON TABLE {db}.orders_log TO USER '{ro}'@'%'",
    )
    cache, executes = schema_tools(instance)
    assert await cache.refresh()

    def names(observation: Any) -> list[str]:
        return [r["name"] for r in observation.payload["rows"]]

    found = await call_tool(executes, LIST_TABLES, **search_page(None, "订单流水"))
    assert names(found) == ["orders_log"]
    assert found.payload["rows"][0]["comment"] == comment[:100]
    # 截取之外的文字不在快照中，搜不到；列名不区分大小写匹配。
    assert names(await call_tool(executes, LIST_TABLES, **search_page(None, "tailword"))) == []
    by_column = await call_tool(executes, LIST_TABLES, **search_page(db, "CUSTOMER_REF"))
    assert names(by_column) == ["orders_log"]

    # 可读的两张表分两页；hidden、ungranted 不可读，不在快照中。
    first = await call_tool(executes, LIST_TABLES, **search_page(db, page_size=1))
    version = first.payload["snapshot"]
    second = await call_tool(
        executes, LIST_TABLES, **search_page(db, page=2, page_size=1, snapshot=version)
    )
    assert (names(first), names(second)) == (["orders_log"], ["sales"])
    assert (first.truncated, second.truncated) == (True, False)
    assert await cache.refresh()
    with pytest.raises(ToolRejectedError, match="表结构已刷新"):
        await call_tool(
            executes, LIST_TABLES, **search_page(db, page=2, page_size=1, snapshot=version)
        )


async def test_snapshot_follows_views_roles_and_insert_only_grants(instance: Instance) -> None:
    """可见不等于可读：只授 INSERT 的表、只经未激活角色授权的表都不进入快照；只授视图、不授
    底表时视图进入快照、底表不进入（Task 0 §9.2 的结论在产品刷新路径上复核）。"""
    host, port, user = admin_address()
    db, ro = instance.database, instance.ro_user
    role = f"xw_role_{secrets.token_hex(4)}"
    props = "DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES('replication_num'='1')"
    await admin(
        host,
        port,
        user,
        f"CREATE VIEW {db}.v_hidden AS SELECT id FROM {db}.hidden",
        f"GRANT SELECT ON VIEW {db}.v_hidden TO USER '{ro}'@'%'",
        f"CREATE TABLE {db}.ins_only (id INT) {props}",
        f"GRANT INSERT ON TABLE {db}.ins_only TO USER '{ro}'@'%'",
        f"CREATE TABLE {db}.role_t (id INT) {props}",
        f"CREATE ROLE {role}",
        f"GRANT SELECT ON TABLE {db}.role_t TO ROLE {role}",
        f"GRANT {role} TO USER '{ro}'@'%'",
    )
    try:
        cache, _ = schema_tools(instance)
        assert await cache.refresh()
        snapshot = cache.current()
        assert set(snapshot.objects) == {(db, "sales"), (db, "v_hidden")}
        view = snapshot.object(db, "v_hidden")
        assert view is not None and [c.name for c in view.columns] == ["id"]
        assert set(snapshot.query_policy.tables[db]) == {"sales", "v_hidden"}
    finally:
        await admin(host, port, user, f"DROP ROLE {role}")


async def test_grants_and_revocations_take_effect_at_the_next_boundary(instance: Instance) -> None:
    """新授权在下一次成功刷新后可用；撤权后列表与表结构在交付前的探测即拒绝，新查询由数据库
    拒绝，下一次刷新把它移出快照。"""
    host, port, user = admin_address()
    db, ro = instance.database, instance.ro_user
    cache, executes = schema_tools(instance)
    assert await cache.refresh()

    await admin(host, port, user, f"GRANT SELECT ON TABLE {db}.hidden TO USER '{ro}'@'%'")
    # 刷新之前：快照里还没有它，表结构在 I/O 前拒绝。
    with pytest.raises(ToolRejectedError, match="不在可读的表结构中"):
        await call_tool(executes, DESCRIBE_TABLE, database=db, table="hidden")
    started = time.monotonic()
    assert await cache.refresh()
    refresh_seconds = time.monotonic() - started
    assert refresh_seconds < target(instance).schema_limits.refresh_timeout_seconds
    described = await call_tool(executes, DESCRIBE_TABLE, database=db, table="hidden")
    assert [r["name"] for r in described.payload["rows"]] == ["id", "v"]

    await admin(host, port, user, f"REVOKE SELECT ON TABLE {db}.hidden FROM USER '{ro}'@'%'")
    # 快照尚未刷新：交付前的探测发现撤权。
    listed = await call_tool(executes, LIST_TABLES, **search_page(db))
    assert [r["name"] for r in listed.payload["rows"]] == ["sales"]
    with pytest.raises(StarRocksError) as unreadable:
        await call_tool(executes, DESCRIBE_TABLE, database=db, table="hidden")
    assert unreadable.value.code is Code.OBJECT_UNREADABLE
    with pytest.raises(StarRocksError) as layout:
        await call_tool(executes, LAYOUT_TOOL, database=db, table="hidden")
    assert layout.value.code is Code.OBJECT_UNREADABLE
    # 新查询：SQLGuard 仍按旧快照放行，数据库是最终防线。
    with pytest.raises(StarRocksError) as denied:
        await call_tool(executes, RUN_QUERY, sql="SELECT id FROM hidden")
    assert denied.value.code is Code.PERMISSION_DENIED
    assert await cache.refresh()
    assert (db, "hidden") not in cache.current().objects

    # 库级授权覆盖库中全部表；撤销后同样在下一边界失效。
    await admin(host, port, user, f"GRANT SELECT ON ALL TABLES IN DATABASE {db} TO USER '{ro}'@'%'")
    assert await cache.refresh()
    assert {(db, "hidden"), (db, "ungranted")} <= set(cache.current().objects)
    await admin(
        host, port, user, f"REVOKE SELECT ON ALL TABLES IN DATABASE {db} FROM USER '{ro}'@'%'"
    )
    assert await cache.refresh()
    assert not {(db, "hidden"), (db, "ungranted")} & set(cache.current().objects)


async def test_evidence_dependencies_follow_current_grants_and_object_versions(
    instance: Instance,
) -> None:
    """证据依赖复核（P2.5 Task 2 审查修订）：只读账号读得到依赖的当前版本；未变化为 valid，撤权、
    同名重建（重新授权后）为 invalid，别处加列仍 valid；视图依赖不可跨轮回放。

    同时固定 4.1.4 的元数据事实：带 ``TABLE_SCHEMA`` 条件读取的 ``CREATE_TIME`` 不随会话时区，
    与结构快照的全表读取不同，因此版本不比较创建时间。"""
    host, port, user = admin_address()
    db, ro = instance.database, instance.ro_user
    props = "DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES('replication_num'='1')"
    await admin(
        host,
        port,
        user,
        f"CREATE VIEW {db}.v_sales AS SELECT id, region FROM {db}.sales",
        f"GRANT SELECT ON VIEW {db}.v_sales TO USER '{ro}'@'%'",
        f"GRANT SELECT ON TABLE {db}.hidden TO USER '{ro}'@'%'",
    )
    ada = adapter(instance)
    cache = SchemaCache(ada, clock=lambda: datetime.now(UTC))
    assert await cache.refresh()
    snap = cache.current()
    check = DependencyCheck({"sr-real": ada})
    sales, hidden, view = (snap.object(db, n) for n in ("sales", "hidden", "v_sales"))
    assert sales is not None and hidden is not None and view is not None
    assert (sales.type, view.type, view.table_id) == ("BASE TABLE", "VIEW", 0)
    assert isinstance(sales.table_id, int) and sales.table_id > 0

    deps = [
        read_dependency(sales, {"region", "total"}),
        read_dependency(hidden),
        read_dependency(view, {"region"}),
        listed_dependency(db, "sales"),
    ]
    assert not deps[2].replayable and deps[0].replayable
    assert await check("sr-real", deps) == "valid"

    # 别处加列：被依赖列的名字与类型不变，仍然有效。
    await admin(host, port, user, f"ALTER TABLE {db}.sales ADD COLUMN extra INT")
    for _ in range(60):  # 加列是异步的轻量变更：等到列出现在元数据中
        cols = await admin_rows(
            host,
            port,
            user,
            f"SELECT COLUMN_NAME FROM information_schema.columns WHERE TABLE_SCHEMA = '{db}' "
            "AND TABLE_NAME = 'sales'",
        )
        if ("extra",) in cols:
            break
        await asyncio.sleep(0.5)
    assert await check("sr-real", deps[:1]) == "valid"

    # 撤权：确定失效。
    await admin(host, port, user, f"REVOKE SELECT ON TABLE {db}.hidden FROM USER '{ro}'@'%'")
    assert await check("sr-real", [deps[1]]) == "invalid"
    assert await check("sr-real", [listed_dependency(db, "hidden")]) == "invalid"

    # 同名重建并重新授权：权限探测通过，版本（TABLE_ID/CREATE_TIME）不同，确定失效。
    await admin(
        host,
        port,
        user,
        f"DROP TABLE {db}.hidden FORCE",
        f"CREATE TABLE {db}.hidden (id INT, v VARCHAR(8)) {props}",
        f"GRANT SELECT ON TABLE {db}.hidden TO USER '{ro}'@'%'",
    )
    assert await check("sr-real", [listed_dependency(db, "hidden")]) == "valid"
    assert await check("sr-real", [deps[1]]) == "invalid"


async def test_create_time_zone_depends_on_the_metadata_predicate(instance: Instance) -> None:
    """4.1.4 事实（P2.5 Task 2 审查修订）：同一会话、同一对象，``CREATE_TIME`` 在全表读取时按会话
    时区、带 ``TABLE_SCHEMA = …`` 条件时按服务器时区给出。证据依赖因此不比较创建时间。"""
    host, port, user = admin_address()
    conn = await asyncmy.connect(host=host, port=port, user=user, password="", autocommit=True)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute("SET time_zone = 'Asia/Shanghai'")
            await cursor.execute(
                "SELECT CREATE_TIME FROM information_schema.tables WHERE TABLE_SCHEMA NOT IN "
                "('information_schema', 'sys', '_statistics_') AND TABLE_NAME = 'sales' "
                f"AND TABLE_SCHEMA LIKE '{instance.database}'"
            )
            ((scanned,),) = await cursor.fetchall()
            await cursor.execute(
                "SELECT CREATE_TIME FROM information_schema.tables "
                f"WHERE TABLE_SCHEMA = '{instance.database}' AND TABLE_NAME = 'sales'"
            )
            ((filtered,),) = await cursor.fetchall()
    finally:
        await conn.ensure_closed()
    assert scanned != filtered
    assert (scanned - filtered).total_seconds() == 8 * 3600  # 会话时区 +08:00，服务器为 UTC
