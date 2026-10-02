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
from xiaowei.governance import GovernedTools, ToolCatalog, ToolRejectedError
from xiaowei.models import AgentAnswer, AnswerInference, Budget, Identity, RunContext, ToolRequest
from xiaowei.sqlguard import QueryPolicy, guard_explain_query, guard_readonly_query
from xiaowei.starrocks import (
    EXPLAIN_PREFIX,
    StarRocksAdapter,
    StarRocksError,
    StarRocksErrorCode,
    StarRocksTarget,
    open_starrocks,
)
from xiaowei.starrocks_tools import QUERY_TOOLS, RUN_QUERY, starrocks_tools

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
    policy = QueryPolicy(
        target_id="sr-real",
        default_database=inst.database,
        allowed_objects=frozenset({"sales", "ungranted"}),
        allowed_columns={
            "sales": frozenset({"id", "region", "total", "day", "at", "note"}),
            "ungranted": frozenset({"id"}),
        },
        allowed_functions=frozenset({"SUM", "COUNT", "SLEEP"}),
        max_rows=5,
        max_sql_bytes=4000,
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
    )
    return base.model_copy(update=changes) if changes else base


def adapter(inst: Instance, **changes: object) -> StarRocksAdapter:
    return open_starrocks(target(inst, **changes), clock=lambda: datetime.now(UTC))


def guarded(inst: Instance, sql: str, t: StarRocksTarget | None = None):  # type: ignore[no-untyped-def]
    return guard_readonly_query(sql, (t or target(inst)).policy)


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


async def test_metadata_only_lists_allowed_objects_and_columns(instance: Instance) -> None:
    ada = adapter(instance)
    tables = await ada.list_tables()
    described = await ada.describe_table("sales")

    # 只读账号看不到未授权表；hidden 不在 allowlist，ungranted 在 allowlist 但无权限。
    assert [r["name"] for r in tables.rows] == ["sales"]
    assert {r["name"] for r in described.rows} == {"id", "region", "total", "day", "at", "note"}


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


async def test_governed_query_tool_end_to_end(instance: Instance) -> None:
    """只读账号 → 治理 → SQLGuard → 真实驱动 → 证据（真实 PostgreSQL）→ Web 结构化事实。"""
    raw = os.environ.get("SDK_TEST_POSTGRES_URL")
    if not raw:
        pytest.fail("SDK_TEST_POSTGRES_URL 未设置：端到端用例需要测试 PostgreSQL 保存证据")
    try:
        parse_admin_url(raw)
    except HarnessMisconfiguredError as exc:
        pytest.fail(f"SDK_TEST_POSTGRES_URL {exc}")

    ada = adapter(instance)
    tools = starrocks_tools(ada, dict.fromkeys(("model", "session", "web", "feishu"), 200_000))
    grants = Grants()
    grants.grant("alice", *QUERY_TOOLS, target="sr-real")
    ctx = RunContext(
        identity=Identity(subject_id="alice", session_id="s1", turn_id="t1", channel="web"),
        target_scope=frozenset({"sr-real"}),
        tool_scope=QUERY_TOOLS,
        budget=Budget(max_turns=4, max_tool_calls=2, timeout_seconds=30.0),
    )

    def call(sql: str) -> ToolRequest:
        return ToolRequest(
            tool_id=RUN_QUERY,
            target_id="sr-real",
            call_id=secrets.token_hex(4),
            tool_name="run_readonly_query",
            arguments={"sql": sql},
        )

    async with isolated_database() as url, ready_engine(url) as engine:
        evidence = EvidenceStore(
            engine,
            ToolCatalog(tools.contracts, tools.policies),
            authorize=grants,
            clock=Clock(datetime.now(UTC)),
            retention_seconds=600,
        )
        governed = GovernedTools(evidence)
        with pytest.raises(ToolRejectedError, match="column_not_allowed"):
            await governed.invoke(ctx, call("SELECT secret FROM sales"), tools.executes[RUN_QUERY])

        result = await governed.invoke(
            ctx,
            call("SELECT id, total, at FROM sales WHERE id IN (3, 5) ORDER BY id"),
            tools.executes[RUN_QUERY],
        )
        answer = AgentAnswer(
            evidence_ids=(result.evidence_id,),
            inferences=[AnswerInference(text="两行", evidence_ids=(result.evidence_id,))],
            clarification=None,
        )
        delivery = await evidence.validate_answer(answer, ctx)

    (fact,) = delivery.facts
    assert fact.columns == ("id", "total", "at")
    assert fact.rows == (
        {"id": 3, "total": "3.75", "at": "2026-09-01T03:30:00+08:00"},
        {"id": 5, "total": "6.25", "at": "2026-09-01T05:30:00+08:00"},
    )
    assert fact.metadata["row_count"] == 2 and "`sales`" in str(fact.metadata["sql"])
    assert not fact.truncated


# ---- 执行计划（P2 Task 3） -----------------------------------------------------------------


def explained(inst: Instance, sql: str, t: StarRocksTarget | None = None):  # type: ignore[no-untyped-def]
    return guard_explain_query(sql, (t or target(inst)).policy)


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
    base = target(instance)
    policy = base.policy.model_copy(
        update={
            "allowed_objects": base.policy.allowed_objects | {"sales_view"},
            "allowed_columns": {
                **base.policy.allowed_columns,
                "sales_view": frozenset({"id", "region", "total"}),
            },
        }
    )
    t = base.model_copy(update={"policy": policy})
    ada = open_starrocks(t, clock=lambda: datetime.now(UTC))

    for sql in (
        "SELECT region, SUM(total) AS s FROM sales WHERE id > 3 GROUP BY region",
        "SELECT v.region FROM sales_view v JOIN sales s ON v.id = s.id",
        "WITH t AS (SELECT region, total FROM sales) SELECT region FROM t WHERE total > 1",
    ):
        plan = explained(instance, sql, t)
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
