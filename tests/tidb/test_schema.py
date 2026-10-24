"""TiDB 结构/视图探测协议夹具，包含逐对象失败与历史权限复核。"""

from importlib import import_module

import pytest
from asyncmy.errors import ProgrammingError
from tests.p1b.test_starrocks_adapter import Result
from tests.tidb.test_adapter import NOW, adapter, results, statements

from xiaowei.models import AUDIENCES, ToolRequest
from xiaowei.starrocks import OBJECT_COLUMNS_SQL, OBJECT_TYPE_SQL, StarRocksErrorCode
from xiaowei.starrocks_schema import SchemaCache
from xiaowei.starrocks_tools import LIST_TABLES, starrocks_tools

SYSTEMS = ("information_schema", "performance_schema", "metrics_schema", "mysql")
OBJECTS_SQL = (
    "SELECT TABLE_SCHEMA AS db, TABLE_NAME AS name, TABLE_TYPE AS type, "
    "LEFT(TABLE_COMMENT, %s) AS comment, CREATE_TIME AS created "
    "FROM information_schema.tables WHERE LOWER(TABLE_SCHEMA) NOT IN (%s, %s, %s, %s) "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME LIMIT %s"
)
COLUMNS_SQL = (
    "SELECT TABLE_SCHEMA AS db, TABLE_NAME AS name, COLUMN_NAME AS col, COLUMN_TYPE AS type, "
    "IS_NULLABLE AS nullable, LEFT(COLUMN_COMMENT, %s) AS comment "
    "FROM information_schema.columns WHERE LOWER(TABLE_SCHEMA) NOT IN (%s, %s, %s, %s) "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION LIMIT %s"
)
IDS_SQL = (
    "SELECT TABLE_SCHEMA AS db, TABLE_NAME AS name, TIDB_TABLE_ID AS id "
    "FROM information_schema.tables WHERE LOWER(TABLE_SCHEMA) NOT IN (%s, %s, %s, %s) "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME LIMIT %s"
)
OBJECT_ID_SQL = (
    "SELECT TIDB_TABLE_ID AS id FROM information_schema.tables "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)
VIEW_SQL = (
    "SELECT VIEW_DEFINITION AS definition FROM information_schema.views "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)
VIEW_PROBE = "EXPLAIN SELECT 1 FROM `app`.`v` WHERE 1 = 0"
TABLE_PROBE = "SELECT 1 FROM `app`.`orders` WHERE 1 = 0"
PLANS = Result(
    ("id", "estRows", "task", "access object", "operator info"),
    [("TableDual_1", "0.00", "root", "", "rows:0")],
)


def schema_results(version="7.5.3", *, view_probe=PLANS, definition="SELECT id FROM app.orders"):
    outcomes = results(version)
    outcomes[OBJECTS_SQL] = Result(
        ("db", "name", "type", "comment", "created"),
        [
            ("app", "orders", "BASE TABLE", "订单", NOW),
            ("app", "v", "VIEW", None, NOW),
        ],
    )
    outcomes[COLUMNS_SQL] = Result(
        ("db", "name", "col", "type", "nullable", "comment"),
        [
            ("app", "orders", "id", "bigint", "NO", None),
            ("app", "orders", "customer_id", "bigint", "NO", None),
            ("app", "v", "id", "bigint", "NO", None),
        ],
    )
    outcomes[IDS_SQL] = Result(("db", "name", "id"), [("app", "orders", 101), ("app", "v", 201)])
    outcomes[OBJECT_TYPE_SQL] = lambda args: Result(
        ("type",), [("VIEW" if args[1] == "v" else "BASE TABLE",)]
    )
    outcomes[OBJECT_ID_SQL] = lambda args: Result(("id",), [(201 if args[1] == "v" else 101,)])
    outcomes[OBJECT_COLUMNS_SQL] = lambda args: Result(
        ("col", "type"),
        [("id", "bigint")] if args[1] == "v" else [("id", "bigint"), ("customer_id", "bigint")],
    )
    outcomes[VIEW_SQL] = Result(("definition",), [(definition,)])
    outcomes[VIEW_PROBE] = view_probe
    outcomes[TABLE_PROBE] = Result(("1",))
    outcomes["SELECT 1 FROM `app`.`v` WHERE 1 = 0"] = Result(("1",))
    return outcomes


@pytest.mark.parametrize("code", [1345, 1356])
async def test_view_probe_denial_excludes_only_that_object(code):
    db, drv = adapter(schema_results(view_probe=ProgrammingError(code, "view unavailable")))
    cache = SchemaCache(db, clock=lambda: NOW)
    assert await cache.refresh()
    snapshot = cache.current()
    assert set(snapshot.objects) == {("app", "orders")}
    verdicts = await db.probe([("app", "orders"), ("app", "v")])
    assert verdicts == {("app", "orders"): True, ("app", "v"): None if code == 1345 else False}
    assert "SELECT 1 FROM `app`.`v` WHERE 1 = 0" not in statements(drv)
    for conn in drv.connections:
        for sql, args in conn.executed:
            if sql in (OBJECTS_SQL, COLUMNS_SQL):
                assert args[1:5] == SYSTEMS
            elif sql == IDS_SQL:
                assert args[:4] == SYSTEMS


async def test_unknown_view_error_still_fails_entire_refresh():
    db, drv = adapter(schema_results(view_probe=ProgrammingError(1105, "unknown")))
    cache = SchemaCache(db, clock=lambda: NOW)
    assert not await cache.refresh()
    assert VIEW_PROBE in statements(drv)


async def test_protected_view_keeps_its_actual_tidb_id_in_snapshot_and_verification():
    db, drv = adapter(schema_results())
    cache = SchemaCache(db, clock=lambda: NOW)
    assert await cache.refresh()
    view = cache.current().object("app", "v")
    assert view is not None and view.table_id == 201
    verified = await db.verify_objects([("app", "v")], versioned=frozenset({("app", "v")}))
    assert verified[("app", "v")].table_id == 201
    assert VIEW_PROBE in statements(drv)
    assert "SELECT 1 FROM `app`.`v` WHERE 1 = 0" not in statements(drv)


@pytest.mark.parametrize(
    "definition",
    [
        "SELECT (SELECT id FROM app.orders LIMIT 1) AS id",
        "SELECT EXISTS (SELECT id FROM app.orders) AS id",
        "SELECT id FROM app.v",
        None,
    ],
)
async def test_65_restricted_or_unknown_view_is_never_probed_with_select(definition):
    db, drv = adapter(schema_results("6.5.5", definition=definition))
    cache = SchemaCache(db, clock=lambda: NOW)
    assert await cache.refresh()
    assert set(cache.current().objects) == {("app", "orders")}
    assert not any(sql.startswith("EXPLAIN") or "FROM `app`.`v`" in sql for sql in statements(drv))


async def test_65_safe_view_still_requires_select_permission():
    outcomes = schema_results("6.5.5")
    outcomes["SELECT 1 FROM `app`.`v` WHERE 1 = 0"] = ProgrammingError(1142, "SELECT denied")
    db, _ = adapter(outcomes)
    cache = SchemaCache(db, clock=lambda: NOW)
    assert await cache.refresh()
    assert set(cache.current().objects) == {("app", "orders")}


async def test_tidb_snapshot_policy_uses_the_connected_default_database():
    db, _ = adapter(schema_results())
    cache = SchemaCache(db, clock=lambda: NOW)
    assert await cache.refresh()
    policy = cache.current().query_policy
    assert policy.database_type == "tidb" and policy.default_database == "app"


async def test_1345_fixture_goes_through_the_shared_list_tables_tool():
    db, _ = adapter(schema_results(view_probe=ProgrammingError(1345, "SHOW VIEW denied")))
    cache = SchemaCache(db, clock=lambda: NOW)
    assert await cache.refresh()
    tools = starrocks_tools(db, dict.fromkeys(AUDIENCES, 60_000), schema=cache)
    execute = tools.executes[(LIST_TABLES, "tidb-a")]
    request = ToolRequest(
        call_id="view-list",
        tool_name="list_tables",
        tool_id=LIST_TABLES,
        target_id="tidb-a",
        arguments={
            "cluster": "tidb-a",
            "database": "app",
            "keyword": None,
            "page_size": 5,
            "cursor": None,
        },
    )
    checked = execute.check(request)
    result = await execute.run(checked)
    assert [row["name"] for row in result.payload["rows"]] == ["orders"]
    assert result.payload["sql"] == OBJECTS_SQL
    assert result.payload["warnings"] == []


async def test_1345_history_probe_is_unverifiable_not_false_or_valid():
    tidb = import_module("xiaowei.tidb")
    db, _ = adapter(schema_results(view_probe=ProgrammingError(1345, "SHOW VIEW denied")))
    verdicts = await db.verify_objects([("app", "v")], versioned=frozenset({("app", "v")}))
    assert verdicts[("app", "v")] is None
    assert not tidb.TiDBError(StarRocksErrorCode.QUERY_FAILED).parse_failed


async def test_65_unavailable_view_definition_is_unknown():
    outcomes = schema_results("6.5.5")
    outcomes[VIEW_SQL] = ProgrammingError(1142, "definition access denied")
    db, _ = adapter(outcomes)
    assert (await db.probe([("app", "v")]))[("app", "v")] is None


INDEX_SQL = (
    "SELECT INDEX_NAME AS index_name, NON_UNIQUE AS non_unique, "
    "SEQ_IN_INDEX AS seq_in_index, COLUMN_NAME AS column_name "
    "FROM information_schema.statistics WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
    "ORDER BY INDEX_NAME, SEQ_IN_INDEX LIMIT %s"
)
IDENTITY_SQL = (
    "SELECT TIDB_TABLE_ID AS id, ENGINE AS engine, TABLE_TYPE AS type "
    "FROM information_schema.tables WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)


async def test_composite_primary_key_and_index_column_order_are_preserved():
    outcomes = schema_results()
    outcomes[INDEX_SQL] = Result(
        ("index_name", "non_unique", "seq_in_index", "column_name"),
        [("PRIMARY", 0, 1, "customer_id"), ("PRIMARY", 0, 2, "id"), ("idx_id", 1, 1, "id")],
    )
    db, _ = adapter(outcomes)
    result = await db.describe_layout("app", "orders", frozenset({"id", "customer_id"}))
    assert result.rows == (
        {"index_name": "PRIMARY", "non_unique": 0, "seq_in_index": 1, "column_name": "customer_id"},
        {"index_name": "PRIMARY", "non_unique": 0, "seq_in_index": 2, "column_name": "id"},
        {"index_name": "idx_id", "non_unique": 1, "seq_in_index": 1, "column_name": "id"},
    )
    assert not result.truncated


async def test_tidb_show_create_uses_tidb_identity_and_innodb_not_starrocks_layout():
    outcomes = schema_results()
    outcomes[IDENTITY_SQL] = Result(("id", "engine", "type"), [(101, "InnoDB", "BASE TABLE")])
    ddl = "CREATE TABLE orders (customer_id bigint, id bigint, PRIMARY KEY(customer_id,id))"
    outcomes["SHOW CREATE TABLE `app`.`orders`"] = Result(
        ("Table", "Create Table"), [("orders", ddl)]
    )
    db, drv = adapter(outcomes, max_ddl_bytes=500)
    result = await db.show_create_table("app", "orders", 101)
    assert result.rows == ({"ddl": ddl},)
    assert statements(drv).count(IDENTITY_SQL) == 2


async def test_view_permission_becomes_unknown_on_describe_uses_existing_auth_feedback():
    from xiaowei.governance import ToolRejectedError
    from xiaowei.starrocks_tools import DESCRIBE_TABLE

    outcomes = schema_results()
    db, _ = adapter(outcomes)
    cache = SchemaCache(db, clock=lambda: NOW)
    assert await cache.refresh()
    tools = starrocks_tools(db, dict.fromkeys(AUDIENCES, 60_000), schema=cache)
    execute = tools.executes[(DESCRIBE_TABLE, "tidb-a")]
    request = ToolRequest(
        call_id="describe-view",
        tool_name="describe_table",
        tool_id=DESCRIBE_TABLE,
        target_id="tidb-a",
        arguments={
            "cluster": "tidb-a",
            "database": "app",
            "table": "v",
            "cursor": None,
        },
    )
    checked = execute.check(request)
    outcomes[VIEW_PROBE] = ProgrammingError(1345, "SHOW VIEW denied")
    with pytest.raises(ToolRejectedError) as unknown:
        await execute.run(checked)
    assert unknown.value.code == "authorization_unavailable"


def test_tidb_scope_is_distinct_and_existing_starrocks_scope_is_unchanged():
    import json

    from tests.p1b.test_starrocks_adapter import TARGET as SR
    from tests.tidb.test_adapter import SQL_MODE, target

    from xiaowei.starrocks_tools import data_scope_digest, scope_canonical

    assert (
        data_scope_digest(SR)
        == "sha256:6859ae8d7c05cad07efead5fee2ddddda507a80472e7f6d94e22fa32c393bcfd"
    )
    scope = json.loads(scope_canonical(target()))
    assert scope["format"] == "xiaowei.data_scope.tidb/1"
    assert scope["database_type"] == "tidb" and scope["default_database"] == "app"
    assert scope["sql_mode"] == SQL_MODE and scope["collation_connection"] == "utf8mb4_bin"
