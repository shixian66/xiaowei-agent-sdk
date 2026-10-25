"""B1：计划保持原 LIMIT；旧版受限结构与会话保护失败均不发 EXPLAIN。"""

import pytest
from asyncmy.errors import ProgrammingError
from tests.p1b.test_starrocks_adapter import Result
from tests.tidb.test_adapter import POLICY, PROTECTION_READ, PROTECTION_SET, adapter, statements
from tests.tidb.test_schema import PLANS, schema_results

from xiaowei.sqlguard import guard_explain_query
from xiaowei.starrocks import StarRocksErrorCode
from xiaowei.tidb import TiDBError


@pytest.mark.parametrize("version", ["6.5.5", "7.5.3"])
async def test_explain_retains_native_plan_columns_and_original_limit(version):
    sql = "SELECT id FROM app.orders LIMIT 123456"
    outcomes = schema_results(version)
    outcomes["EXPLAIN " + sql] = PLANS
    db, drv = adapter(outcomes)
    db.server_version = version
    result = await db.explain(guard_explain_query(sql, POLICY))
    assert result.columns == PLANS.columns
    assert result.rows[0]["id"] == "TableDual_1"
    assert result.sql == "EXPLAIN " + sql
    assert sql not in statements(drv)
    assert not any("ANALYZE" in sent or "EXPLAIN LOGICAL" in sent for sent in statements(drv))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT (SELECT id FROM app.orders) AS n",
        "SELECT EXISTS (SELECT id FROM app.orders) AS n",
        "WITH q AS (SELECT EXISTS(SELECT id FROM app.orders) AS n) SELECT n FROM q",
        "SELECT id FROM app.orders WHERE id IN (1, (SELECT id FROM app.orders))",
        "SELECT id FROM app.orders WHERE (SELECT id FROM app.orders) IN (1, 2)",
    ],
)
async def test_65_restricted_explain_is_refused_before_connect(sql):
    db, drv = adapter(schema_results("6.5.5"))
    db.server_version = "6.5.5"
    with pytest.raises(TiDBError):
        await db.explain(guard_explain_query(sql, POLICY))
    assert drv.attempts == 0


async def test_65_plain_in_explain_is_supported():
    sql = "SELECT id FROM app.orders WHERE id IN (SELECT id FROM app.orders)"
    outcomes = schema_results("6.5.5")
    outcomes["EXPLAIN " + sql] = PLANS
    db, drv = adapter(outcomes)
    db.server_version = "6.5.5"
    assert (await db.explain(guard_explain_query(sql, POLICY))).row_count == 1
    assert "EXPLAIN " + sql in statements(drv) and sql not in statements(drv)


@pytest.mark.parametrize(
    "sql",
    ["SELECT (SELECT id FROM app.orders) AS n", "SELECT EXISTS(SELECT id FROM app.orders) AS n"],
)
async def test_75_subquery_explain_requires_same_connection_protection(sql):
    outcomes = schema_results()
    outcomes["EXPLAIN " + sql] = PLANS
    db, drv = adapter(outcomes)
    db.server_version = "7.5.3"
    assert (await db.explain(guard_explain_query(sql, POLICY))).row_count == 1
    sent = statements(drv)
    assert sent.index(PROTECTION_SET) < sent.index(PROTECTION_READ) < sent.index("EXPLAIN " + sql)
    assert sql not in sent


async def test_protection_readback_failure_suppresses_explain():
    sql = "SELECT id FROM app.orders"
    outcomes = schema_results()
    outcomes[PROTECTION_READ] = Result(("v",), [(0,)])
    db, drv = adapter(outcomes)
    db.server_version = "7.5.3"
    with pytest.raises(TiDBError) as failure:
        await db.explain(guard_explain_query(sql, POLICY))
    assert failure.value.code is StarRocksErrorCode.SESSION_SETUP_FAILED
    assert not any(s.startswith("EXPLAIN ") for s in statements(drv))


async def test_version_changed_to_65_cannot_explain_scalar_subquery():
    sql = "SELECT (SELECT id FROM app.orders) AS n"
    db, drv = adapter(schema_results("6.5.5"))
    db.server_version = "7.5.3"
    with pytest.raises(TiDBError):
        await db.explain(guard_explain_query(sql, POLICY))
    assert not any(s.startswith("EXPLAIN ") for s in statements(drv))


async def test_65_view_is_not_explained_even_after_cached_new_version():
    sql = "SELECT id FROM app.v"
    policy = POLICY.model_copy(update={"tables": {"app": {"v": ("id",)}}})
    db, drv = adapter(schema_results("6.5.5"))
    db.server_version = "7.5.3"
    with pytest.raises(TiDBError):
        await db.explain(guard_explain_query(sql, policy))
    assert not any(s.startswith("EXPLAIN ") for s in statements(drv))


async def test_explain_database_error_is_terminal_and_not_replayed():
    sql = "SELECT id FROM app.orders"
    outcomes = schema_results()
    outcomes["EXPLAIN " + sql] = ProgrammingError(1105, "synthetic plan failure")
    db, drv = adapter(outcomes)
    db.server_version = "7.5.3"
    with pytest.raises(TiDBError) as failure:
        await db.explain(guard_explain_query(sql, POLICY))
    assert failure.value.code is StarRocksErrorCode.QUERY_FAILED
    assert statements(drv).count("EXPLAIN " + sql) == 1 and sql not in statements(drv)


@pytest.mark.parametrize("version", ["6.5.5", "7.5.3"])
async def test_explain_view_case_uses_snapshot_key_and_retains_version_boundary(version):
    from tests.tidb.test_diagnostics import prepared, request

    from xiaowei.governance import ToolRejectedError
    from xiaowei.starrocks_tools import EXPLAIN_QUERY

    sql = "SELECT id FROM app.V LIMIT 3"
    outcomes = schema_results(version)
    outcomes["EXPLAIN " + sql] = PLANS
    _, drv, _, tools = await prepared(outcomes)
    execute = tools.executes[(EXPLAIN_QUERY, "tidb-a")]
    req = request(sql, tool_id=EXPLAIN_QUERY)
    if version == "6.5.5":
        with pytest.raises(ToolRejectedError):
            execute.check(req)
        assert drv.attempts == 0 and statements(drv) == []
    else:
        checked = execute.check(req)
        assert checked[1].referenced_objects == frozenset({("app", "v")})
        result = await execute.run(checked)
        assert [(d.database, d.name) for d in result.dependencies] == [("app", "v")]
        assert statements(drv).count("EXPLAIN " + sql) == 1
        assert sql not in statements(drv)
