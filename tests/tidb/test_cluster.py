"""C：精确 tag 列的最小夹具验证锁方向、等待时间、缺列及部分结果；非模型能力证明。"""

from datetime import datetime

import pytest
from tests.p1b.test_starrocks_adapter import Result
from tests.tidb.test_adapter import NOW, WARNINGS_SQL, adapter, statements, target
from tests.tidb.test_diagnostics import DIAGNOSTICS, diagnostic_results, request

from xiaowei.governance import ToolRejectedError
from xiaowei.models import AUDIENCES
from xiaowei.starrocks_schema import SchemaCache
from xiaowei.starrocks_tools import starrocks_tools

# v6.5.5 infoschema/tables.go + session/txninfo/txn_info.go；
# v7.5.3 pkg/infoschema/tables.go + pkg/session/txninfo/txn_info.go。
# 只提供固定样例需要的真实列子集，完整列仍由运行时快照发现。
LOCK_FIELDS = (("TRX_ID", "bigint"), ("CURRENT_HOLDING_TRX_ID", "bigint"),
               ("SQL_DIGEST_TEXT", "text"))
TRX_FIELDS = (("INSTANCE", "varchar(64)"), ("ID", "bigint"), ("SESSION_ID", "bigint"),
              ("WAITING_START_TIME", "timestamp"), ("CURRENT_SQL_DIGEST_TEXT", "text"))
LOCK_SQL = (
    "SELECT w.TRX_ID AS waiting_trx, w.CURRENT_HOLDING_TRX_ID AS holding_trx, "
    "t.INSTANCE, t.WAITING_START_TIME, t.CURRENT_SQL_DIGEST_TEXT, NOW(6) AS sampled_at, "
    "UNIX_TIMESTAMP(NOW(6)) - UNIX_TIMESTAMP(t.WAITING_START_TIME) AS wait_seconds "
    "FROM information_schema.DATA_LOCK_WAITS AS w "
    "LEFT JOIN information_schema.CLUSTER_TIDB_TRX AS t ON t.ID = w.TRX_ID LIMIT 3"
)
LOCK_COLUMNS = ("waiting_trx", "holding_trx", "INSTANCE", "WAITING_START_TIME",
                "CURRENT_SQL_DIGEST_TEXT", "sampled_at", "wait_seconds")
REGION_SQL = "SELECT REGION_ID, TABLE_ID FROM information_schema.TIKV_REGION_STATUS LIMIT 3"


def cluster_results(version="7.5.3", *, missing_time=False):
    trx = TRX_FIELDS + ((("WAITING_TIME", "double"),) if version == "7.5.3" else ())
    region = (("REGION_ID", "bigint"), ("TABLE_ID", "bigint"))
    if version == "7.5.3":
        region += (("IS_PARTITION", "tinyint"), ("PARTITION_ID", "bigint"),
                   ("PARTITION_NAME", "varchar(64)"))
    outcomes = diagnostic_results(version, extra_sources={
        "DATA_LOCK_WAITS": LOCK_FIELDS, "CLUSTER_TIDB_TRX": trx,
        "TIKV_REGION_STATUS": region,
        "CLUSTER_INFO": (("TYPE", "varchar(64)"), ("INSTANCE", "varchar(64)"),
                         ("VERSION", "varchar(64)")),
    })
    start = None if missing_time else datetime(2026, 9, 30, 15, 59, 45)
    outcomes[LOCK_SQL] = Result(LOCK_COLUMNS, [
        (222, 111, "node-a", start, None, datetime(2026, 9, 30, 16, 0, 0),
         None if missing_time else 15.0),
    ])
    outcomes[REGION_SQL] = Result(("REGION_ID", "TABLE_ID"), [(8, 101)])
    return outcomes


async def prepared(version="7.5.3", **kwargs):
    policy = target().policy.model_copy(
        update={"allowed_functions": target().policy.allowed_functions | {"NOW", "UNIX_TIMESTAMP"}}
    )
    db, drv = adapter(cluster_results(version, **kwargs), policy=policy)
    cache = SchemaCache(db, clock=lambda: NOW)
    assert await cache.refresh()
    drv.connections.clear()
    drv.attempts = 0
    return drv, cache, starrocks_tools(db, dict.fromkeys(AUDIENCES, 60_000), schema=cache)


@pytest.mark.parametrize("version", ["6.5.5", "7.5.3"])
@pytest.mark.parametrize("missing_time", [False, True])
async def test_lock_evidence_preserves_waiting_direction_time_and_unknown_sql(
    version, missing_time
):
    drv, cache, tools = await prepared(version, missing_time=missing_time)
    execute = tools.executes[(DIAGNOSTICS, "tidb-a")]
    result = await execute.run(execute.check(request(LOCK_SQL)))
    row = result.payload["rows"][0]
    assert row["waiting_trx"] == 222 and row["holding_trx"] == 111
    assert row["wait_seconds"] == (None if missing_time else 15.0)
    assert row["CURRENT_SQL_DIGEST_TEXT"] is None
    assert statements(drv).count(LOCK_SQL) == 1
    assert {d.name for d in result.dependencies} == {"DATA_LOCK_WAITS", "CLUSTER_TIDB_TRX"}
    assert "INFORMATION_SCHEMA" not in cache.current().query_policy.tables


@pytest.mark.parametrize("version", ["6.5.5", "7.5.3"])
async def test_region_columns_follow_snapshot_without_version_matrix_in_code(version):
    drv, _, tools = await prepared(version)
    execute = tools.executes[(DIAGNOSTICS, "tidb-a")]
    sql = "SELECT PARTITION_ID FROM information_schema.TIKV_REGION_STATUS LIMIT 3"
    if version == "6.5.5":
        with pytest.raises(ToolRejectedError):
            execute.check(request(sql))
        assert drv.attempts == 0
    else:
        execute.check(request(sql))
        assert drv.attempts == 0


async def test_cluster_warning_is_returned_as_partial_evidence_not_silently_healthy():
    outcomes = cluster_results()
    outcomes[WARNINGS_SQL] = Result(("Level", "Code", "Message"), [
        ("Warning", 1105, "synthetic node-b missing"),
    ])
    db, drv = adapter(outcomes)
    cache = SchemaCache(db, clock=lambda: NOW)
    assert await cache.refresh()
    tools = starrocks_tools(db, dict.fromkeys(AUDIENCES, 60_000), schema=cache)
    execute = tools.executes[(DIAGNOSTICS, "tidb-a")]
    result = await execute.run(execute.check(request(REGION_SQL)))
    assert result.payload["rows"] == [{"REGION_ID": 8, "TABLE_ID": 101}]
    assert result.payload["warnings"][0]["message"] == "synthetic node-b missing"
    assert statements(drv).count(REGION_SQL) == 1
