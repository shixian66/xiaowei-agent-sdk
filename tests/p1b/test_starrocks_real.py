"""StarRocks Adapter 的真实协议验证（显式运行）::

    SDK_TEST_STARROCKS_ADMIN_URL=mysql://root@127.0.0.1:59030 \\
      uv run --locked --extra dev python -m pytest tests/p1b/test_starrocks_real.py \\
      -m starrocks_real -q

fixture 以管理账号建一个随机名的合成数据库与只读账号（只授予其中一张表的 SELECT），用例
结束后删除。Adapter 只用只读账号连接。它证明锁定的 asyncmy 与该实例在会话变量、非缓冲读取、
类型、截断、权限、期限与取消上的实际行为；不证明用户环境的 TLS 证书、grants 或资源组。
"""

# ruff: noqa: S608 —— 合成库名由 fixture 随机生成，建表与插数语句是测试数据。

import asyncio
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

from xiaowei.sqlguard import QueryPolicy, guard_readonly_query
from xiaowei.starrocks import (
    StarRocksAdapter,
    StarRocksError,
    StarRocksErrorCode,
    StarRocksTarget,
    open_starrocks,
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
