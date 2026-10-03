"""审计慢查询在真实 StarRocks + 官方 AuditLoader 上的行为（显式运行）::

    SDK_TEST_STARROCKS_ADMIN_URL=mysql://root@127.0.0.1:59030 \\
      uv run --locked --extra dev python -m pytest tests/p1b/test_starrocks_audit_real.py \\
      -m starrocks_audit_real -q

前提：可丢弃实例已按官方文档建好 ``starrocks_audit_db__.starrocks_audit_tbl__`` 并安装 AuditLoader
（``plugin.conf`` 中 ``max_stmt_length`` 与下方 ``STMT_LIMIT`` 一致，导入间隔不超过 10 秒）。
fixture 复用 ``test_starrocks_real`` 的随机合成库与只读账号，并给只读账号授予审计表的 SELECT。
它证明审计表的实际列、时区与截断在锁定版本上与 Adapter 的假设一致，以及真实记录经 SQLGuard
过滤后的结果；不证明用户环境的 AuditLoader 版本与配置（P3 复核）。
"""

# ruff: noqa: S608 —— 合成库名由 fixture 随机生成，语句是测试数据。

import asyncio
import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from tests.p1b.conftest import admin_address
from tests.p1b.test_starrocks_real import Instance, admin, admin_rows, target
from tests.p1b.test_starrocks_real import instance as instance  # pytest fixture

from xiaowei.sqlguard import QueryPolicy, guard_explain_query, guard_readonly_query
from xiaowei.starrocks import (
    AUDIT_COLUMNS,
    AuditSource,
    StarRocksError,
    StarRocksErrorCode,
    StarRocksTarget,
    open_starrocks,
)
from xiaowei.starrocks_schema import SchemaCache

pytestmark = pytest.mark.starrocks_audit_real

AUDIT_DB, AUDIT_TABLE = "starrocks_audit_db__", "starrocks_audit_tbl__"
STMT_LIMIT = 1000  # 与实例中 AuditLoader 的 max_stmt_length 一致
FLUSH_SECONDS = 60


@pytest.fixture
async def audited(instance: Instance) -> AsyncIterator[Instance]:
    host, port, user = admin_address()
    plugins = await admin_rows(host, port, user, "SHOW PLUGINS")
    if not any("AuditLoader" in str(row) for row in plugins):
        pytest.fail("实例未安装 AuditLoader：见本文件说明")
    db = instance.database
    await admin(
        host,
        port,
        user,
        f"CREATE VIEW {db}.sales_view AS SELECT id, region, note FROM {db}.sales",
        f"GRANT SELECT ON VIEW {db}.sales_view TO USER '{instance.ro_user}'@'%'",
        f"GRANT SELECT ON TABLE {AUDIT_DB}.{AUDIT_TABLE} TO USER '{instance.ro_user}'@'%'",
    )
    yield instance


def audit_target(inst: Instance, **audit: object) -> StarRocksTarget:
    base = target(inst)
    policy = base.policy.model_copy(update={"max_sql_bytes": STMT_LIMIT - 4})
    source = AuditSource.model_validate(
        {
            "database": AUDIT_DB,
            "table": AUDIT_TABLE,
            "time_zone": "UTC",  # 本实例 FE 系统时区（Task 0 实测）
            "stmt_limit": STMT_LIMIT,
            "max_rows": 50,
            "candidate_rows": 500,
            **audit,
        }
    )
    return base.model_copy(
        update={
            "policy": policy,
            "audit": source,
            "max_result_bytes": 200_000,
            "max_value_bytes": 8000,
        }
    )


async def snapshot_scope(t: StarRocksTarget) -> QueryPolicy:
    """产品路径的 SQLGuard 范围：在真实实例上刷新一次结构快照。审计表本身不进入快照。"""
    cache = SchemaCache(
        open_starrocks(t, clock=lambda: datetime.now(UTC)), clock=lambda: datetime.now(UTC)
    )
    assert await cache.refresh()
    snapshot = cache.current()
    # 只排除配置的审计源表；配置指向别的表时，真实审计表若可读就是普通可读对象。
    configured = t.audit is not None and (t.audit.database, t.audit.table) == (
        AUDIT_DB,
        AUDIT_TABLE,
    )
    assert ((AUDIT_DB, AUDIT_TABLE) in snapshot.objects) is not configured
    return snapshot.query_policy


async def flushed(marker: str, expected: int) -> None:
    """等 AuditLoader 把含 ``marker`` 的语句导入审计表（批量导入，有延迟）。"""
    host, port, user = admin_address()
    deadline = time.monotonic() + FLUSH_SECONDS
    while time.monotonic() < deadline:
        rows = await admin_rows(
            host,
            port,
            user,
            # 拆开标记：轮询语句自身也会进入审计表，不能被自己计入。
            f"SELECT count(*) FROM {AUDIT_DB}.{AUDIT_TABLE} "
            f"WHERE stmt LIKE CONCAT('%{marker[:3]}', '{marker[3:]}%')",
        )
        if rows[0][0] >= expected:
            return
        await asyncio.sleep(2)
    pytest.fail(f"{FLUSH_SECONDS} 秒内审计表没有出现全部 {expected} 条记录")


async def test_real_audit_rows_are_filtered_by_the_current_scope(audited: Instance) -> None:
    host, port, user = admin_address()
    db, mk = audited.database, f"mk{secrets.token_hex(3)}"
    other = f"{db}_other"
    accepted = [
        f"SELECT region, total FROM sales WHERE note <> '{mk}_1'",
        f"SELECT region FROM sales_view WHERE note <> '{mk}_2'",
        f"WITH t AS (SELECT region, note FROM sales) SELECT region FROM t WHERE note <> '{mk}_3'",
        f"SELECT region FROM sales WHERE note <> '{mk}_4' "
        "AND region IN (SELECT region FROM sales_view)",
        # 4.1.4 不支持列级授权、配置也不再限列：可读表的任意列都在范围内（P2.5 R2）。
        f"SELECT secret FROM sales WHERE note <> '{mk}_5'",
    ]
    rejected = [
        f"SELECT id FROM hidden WHERE v <> '{mk}_6'",  # 账号不可读的对象
        f"SELECT id FROM {other}.t WHERE v <> '{mk}_7'",  # 其他库
        f"SELECT region FROM sales WHERE note <> '{mk}_8' AND id IN (SELECT id FROM hidden)",
        f"SELECT /* {mk}_9 */ region FROM sales",  # 注释
        # 超过 max_sql_bytes（ASCII）与超过插件上限被截断（多字节）：原文都不读出。
        f"SELECT region FROM sales WHERE note <> '{mk}_10' AND note <> '{'x' * 1000}'",
        f"SELECT region FROM sales WHERE note <> '{mk}_11' AND note <> '{'区' * 400}'",
    ]
    statements = [*accepted, *rejected]
    await admin(
        host,
        port,
        user,
        f"CREATE DATABASE {other}",
        f"CREATE TABLE {other}.t (id INT, v VARCHAR(8)) DUPLICATE KEY(id) "
        "DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES('replication_num'='1')",
    )
    try:
        # 以管理账号在合成库中执行：审计记录的 db 是该库；被拒绝的语句也真实执行过。
        await admin(host, port, user, f"USE {db}", *statements)
        t = audit_target(audited)
        ada = open_starrocks(t, clock=lambda: datetime.now(UTC))
        scope = await snapshot_scope(t)
        assert {(d, n) for d, objects in scope.tables.items() for n in objects} == {
            (db, "sales"),
            (db, "sales_view"),
        }
        # 小维自身发出的语句：查询照常列出；EXPLAIN、元数据查询与会话回读都不列出。
        ran = await ada.run_query(
            guard_readonly_query(f"SELECT region FROM sales WHERE note <> '{mk}_12'", scope)
        )
        await ada.explain(
            guard_explain_query(f"SELECT region FROM sales WHERE note <> '{mk}_13'", scope)
        )
        await ada.probe([(db, "sales")])
        await flushed(mk, len(statements) + 2)

        started = time.monotonic()
        result = await ada.slow_queries(60, "query_time", scope)
        assert time.monotonic() - started < 30
        assert result.columns == AUDIT_COLUMNS and not result.truncated
        shown = {row["sql"] for row in result.rows if mk in str(row["sql"])}
        assert shown == {*accepted, ran.sql}
        for row in result.rows:
            assert str(row["started_at"]).endswith("+00:00")
            assert row["query_time_ms"] is None or isinstance(row["query_time_ms"], int)
            assert row["state"] in {"EOF", "OK", "ERR", None}

        # 审计时区配错（按上海解释 UTC 写入的时间）：窗口整体偏移 8 小时，刚才的记录不在窗口内。
        shifted = audit_target(audited, time_zone="Asia/Shanghai")
        wrong = await open_starrocks(shifted, clock=lambda: datetime.now(UTC)).slow_queries(
            60, "query_time", scope
        )
        assert not any(mk in str(row["sql"]) for row in wrong.rows)
    finally:
        await admin(host, port, user, f"DROP DATABASE {other} FORCE")


async def test_real_audit_failures_are_distinguished(audited: Instance) -> None:
    missing = audit_target(audited, table="no_such_tbl")
    with pytest.raises(StarRocksError) as info:
        await open_starrocks(missing, clock=lambda: datetime.now(UTC)).slow_queries(
            60, "cpu", await snapshot_scope(missing)
        )
    # 4.1.4 实测：只读账号查询不存在的审计表同样得到 5502，不先报无权限。
    assert info.value.code is StarRocksErrorCode.OBJECT_MISSING

    scope = await snapshot_scope(audit_target(audited))
    host, port, user = admin_address()
    await admin(
        host,
        port,
        user,
        f"REVOKE SELECT ON TABLE {AUDIT_DB}.{AUDIT_TABLE} FROM USER '{audited.ro_user}'@'%'",
    )
    with pytest.raises(StarRocksError) as info:
        await open_starrocks(audit_target(audited), clock=lambda: datetime.now(UTC)).slow_queries(
            60, "cpu", scope
        )
    assert info.value.code is StarRocksErrorCode.PERMISSION_DENIED
