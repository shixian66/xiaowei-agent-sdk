"""B2：合成诊断列独立于业务范围，复用快照、SQLGuard、结构和 Evidence 依赖。"""

import pytest
from tests.p1b.test_starrocks_adapter import Result
from tests.tidb.test_adapter import NOW, adapter, statements
from tests.tidb.test_schema import COLUMNS_SQL, IDS_SQL, OBJECTS_SQL, schema_results

from xiaowei.governance import ToolRejectedError
from xiaowei.models import AUDIENCES, ToolRequest
from xiaowei.starrocks import OBJECT_COLUMNS_SQL, OBJECT_TYPE_SQL
from xiaowei.starrocks_schema import SchemaCache
from xiaowei.starrocks_tools import DESCRIBE_TABLE, starrocks_tools
from xiaowei.tidb import OBJECT_ID_SQL

DIAGNOSTICS = "local/query_diagnostics"
SOURCES = ("CLUSTER_SLOW_QUERY", "SLOW_QUERY", "TIFLASH_REPLICA")
FIELDS = (("INSTANCE", "varchar(64)"), ("Time", "datetime"), ("Query_time", "double"),
          ("Query", "text"), ("Plan", "text"))
SQL = (
    "SELECT INSTANCE, Query_time, Query FROM information_schema.CLUSTER_SLOW_QUERY "
    "WHERE Query_time > 1 ORDER BY Query_time DESC LIMIT 3"
)


def diagnostic_results(version="7.5.3", *, extra_sources=None):
    outcomes = schema_results(version)
    source_fields = {name: FIELDS for name in SOURCES} | (extra_sources or {})
    names = tuple(source_fields)
    for i, name in enumerate(names):
        outcomes[OBJECTS_SQL].rows.append(("INFORMATION_SCHEMA", name, "SYSTEM VIEW", None, None))
        outcomes[IDS_SQL].rows.append(("INFORMATION_SCHEMA", name, 9000 + i))
        outcomes[COLUMNS_SQL].rows.extend(
            ("INFORMATION_SCHEMA", name, column, kind, "YES", None)
            for column, kind in source_fields[name]
        )
        probe = f"SELECT 1 FROM `INFORMATION_SCHEMA`.`{name}` WHERE 1 = 0"  # noqa: S608 — 常量夹具
        outcomes[probe] = Result(("1",))
    original_type, original_columns = outcomes[OBJECT_TYPE_SQL], outcomes[OBJECT_COLUMNS_SQL]
    original_id = outcomes[OBJECT_ID_SQL]
    outcomes[OBJECT_TYPE_SQL] = lambda args: (
        Result(("type",), [("SYSTEM VIEW",)]) if args[0] == "INFORMATION_SCHEMA"
        else original_type(args)
    )
    outcomes[OBJECT_COLUMNS_SQL] = lambda args: (
        Result(("col", "type"), source_fields[args[1]]) if args[0] == "INFORMATION_SCHEMA"
        else original_columns(args)
    )
    outcomes[OBJECT_ID_SQL] = lambda args: (
        Result(("id",), [(9000 + names.index(args[1]),)]) if args[0] == "INFORMATION_SCHEMA"
        else original_id(args)
    )
    outcomes[SQL] = Result(("INSTANCE", "Query_time", "Query"), [
        ("node-a", 9.0, "SELECT sensitive FROM other_business.orders"),
        ("node-b", 5.0, "SELECT 1"),
        ("node-a", 2.0, "SELECT 2"),
    ])
    return outcomes


def request(sql, *, tool_id=DIAGNOSTICS, **arguments):
    return ToolRequest(call_id="diagnostic", tool_name=tool_id.split("/")[-1], tool_id=tool_id,
                       target_id="tidb-a", arguments={"cluster": "tidb-a", "sql": sql, **arguments})


async def prepared():
    db, drv = adapter(diagnostic_results())
    cache = SchemaCache(db, clock=lambda: NOW)
    assert await cache.refresh()
    drv.connections.clear()
    drv.attempts = 0
    return db, drv, cache, starrocks_tools(db, dict.fromkeys(AUDIENCES, 60_000), schema=cache)


async def test_diagnostic_tables_do_not_enter_business_query_scope():
    _, _, cache, _ = await prepared()
    snapshot = cache.current()
    assert set(snapshot.objects) == {("app", "orders"), ("app", "v")}
    assert "INFORMATION_SCHEMA" not in snapshot.query_policy.tables
    assert set(snapshot.diagnostics) == {("INFORMATION_SCHEMA", source) for source in SOURCES}


async def test_diagnostic_query_uses_only_source_dependencies_and_preserves_log_sql():
    _, drv, _, tools = await prepared()
    execute = tools.executes[(DIAGNOSTICS, "tidb-a")]
    result = await execute.run(execute.check(request(SQL)))
    assert result.payload["rows"][0]["Query"] == "SELECT sensitive FROM other_business.orders"
    assert [(d.database, d.name, d.use) for d in result.dependencies] == [
        ("INFORMATION_SCHEMA", "CLUSTER_SLOW_QUERY", "read")
    ]
    assert statements(drv).count(SQL) == 1


@pytest.mark.parametrize("sql", [
    "SELECT id FROM app.orders",
    "SELECT q.Query_time FROM information_schema.CLUSTER_SLOW_QUERY AS q "
    "JOIN app.orders AS o ON 1=1 LIMIT 1",
    "SELECT * FROM SLOW_QUERY LIMIT 1",
    "WITH SLOW_QUERY AS (SELECT id FROM app.orders) SELECT * FROM SLOW_QUERY",
    "SELECT * FROM metrics_schema.tidb_query_duration",
])
async def test_business_and_unregistered_sources_are_rejected_before_io(sql):
    _, drv, _, tools = await prepared()
    execute = tools.executes[(DIAGNOSTICS, "tidb-a")]
    with pytest.raises(ToolRejectedError):
        execute.check(request(sql))
    assert drv.attempts == 0 and statements(drv) == []


async def test_registered_diagnostic_columns_use_existing_describe_table():
    _, drv, _, tools = await prepared()
    execute = tools.executes[(DESCRIBE_TABLE, "tidb-a")]
    req = request(None, tool_id=DESCRIBE_TABLE, database="INFORMATION_SCHEMA",
                  table="CLUSTER_SLOW_QUERY", cursor=None)
    arguments = {k: v for k, v in req.arguments.items() if k != "sql"}
    req = req.model_copy(update={"arguments": arguments})
    result = await execute.run(execute.check(req))
    assert [r["name"] for r in result.payload["rows"]] == [f[0] for f in FIELDS]
    assert not any(s == SQL for s in statements(drv))
