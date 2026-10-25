"""TiDB 正式装配：配置与生产入口保留真实治理，只替换外部 I/O。"""

import copy
import json

import pytest
from asyncmy.errors import ProgrammingError
from pydantic import ValidationError
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_runtime import Env, serve_config, target_config
from tests.sdk_core.test_runtime import env as env
from tests.tidb.test_adapter import QUERY, adapter, statements
from tests.tidb.test_schema import schema_results

from xiaowei.runtime import ServeConfig


def tidb_config(cluster: str = "tidb-a") -> dict:
    connection = copy.deepcopy(target_config()["starrocks"])
    connection.update(target_id=cluster, host="tidb.internal", port=4000, database="shop")
    connection["password_ref"] = "env:XW_TEST_TIDB_PASSWORD"  # noqa: S105 — reference only
    return {
        "type": "tidb",
        "description": "合成 TiDB；监控源 shared-prom，cluster=" + cluster,
        "business_context": None,
        "tidb": connection,
    }


def test_mixed_three_tidb_targets_and_starrocks_load_without_default_cluster() -> None:
    config = ServeConfig.model_validate(
        serve_config(8501, targets=[target_config(), *[tidb_config(f"tidb-{x}") for x in "abc"]])
    )
    assert [target.type for target in config.targets] == ["starrocks", "tidb", "tidb", "tidb"]
    assert [target.target_id for target in config.targets] == [
        "sr-test",
        "tidb-a",
        "tidb-b",
        "tidb-c",
    ]
    assert config.targets[1].tidb.database == "shop"
    assert "default_cluster" not in config.model_dump()


def test_tidb_version_cannot_be_configured_instead_of_discovered() -> None:
    target = tidb_config()
    target["tidb"]["version"] = "7.5.3"
    with pytest.raises(ValidationError):
        ServeConfig.model_validate(serve_config(8501, targets=[target]))


def test_type_and_connection_payload_must_agree() -> None:
    target = tidb_config()
    target["type"] = "starrocks"
    with pytest.raises(ValidationError):
        ServeConfig.model_validate(serve_config(8501, targets=[target]))


@pytest.mark.loopback
async def test_tidb_formal_web_query_success_preserves_sql_and_evidence(env: Env) -> None:
    outcomes = schema_results()
    _, drv = adapter(outcomes)
    env.drv = drv
    target = tidb_config()
    target["tidb"]["database"] = "app"
    config = env.config(targets=[target])
    async with env.running(config) as served:
        await served.page()
        message = env.scripts.add(
            "在 tidb-a 执行 SELECT id FROM orders LIMIT 3",
            tool_call("run_readonly_query", cluster="tidb-a", sql=QUERY),
            cite(),
        )
        body = (await served.turn(message, "query")).json()
        assert body["state"] == "completed"
        facts = [
            fact
            for fact in body["delivery"]["facts"]
            if fact["tool_id"] == "local/run_readonly_query"
        ]
        assert len(facts) == 1 and facts[0]["rows"] == [{"id": 7}, {"id": 8}]
        assert facts[0]["metadata"]["sql"] == QUERY
        assert statements(drv).count(QUERY) == 1
        assert not any(sql.startswith("EXPLAIN ") for sql in statements(drv))
        assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1


@pytest.mark.loopback
async def test_tidb_formal_web_database_failure_is_terminal_without_correction(env: Env) -> None:
    outcomes = schema_results()
    outcomes[QUERY] = ProgrammingError(1064, "Getting syntax error at line 1: private-canary")
    _, drv = adapter(outcomes)
    env.drv = drv
    target = tidb_config()
    target["tidb"]["database"] = "app"
    config = env.config(targets=[target])
    async with env.running(config) as served:
        await served.page()
        message = env.scripts.add(
            "在 tidb-a 查询订单",
            tool_call("run_readonly_query", cluster="tidb-a", sql=QUERY),
        )
        body = (await served.turn(message, "query")).json()
        assert body["state"] == "failed"
        assert "private-canary" not in json.dumps(body)
        assert statements(drv).count(QUERY) == 1
        assert len(env.scripts.calls[message]) == 1
        assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


@pytest.mark.loopback
async def test_tidb_formal_web_executable_comment_is_rejected_before_any_database_io(
    env: Env,
) -> None:
    from tests.sdk_core.test_app import clarify, last_output

    _, drv = adapter(schema_results())
    env.drv = drv
    target = tidb_config()
    target["tidb"]["database"] = "app"

    def rejected(call):
        assert last_output(call)["status"] == "rejected"
        return clarify("可执行注释未执行，请提供明确的只读 SQL")(call)

    async with env.running(env.config(targets=[target])) as served:
        await served.page()
        message = env.scripts.add(
            "在 tidb-a 查询订单",
            tool_call(
                "run_readonly_query",
                cluster="tidb-a",
                sql="SELECT id FROM orders /*! FOR UPDATE */",
            ),
            rejected,
        )
        body = (await served.turn(message, "query")).json()
        assert body["state"] == "completed"
        assert drv.attempts == 0 and statements(drv) == []
        assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


@pytest.mark.loopback
async def test_tidb_history_followup_rechecks_current_database_privilege(env: Env) -> None:
    from tests.p1b.test_starrocks_adapter import Result
    from tests.sdk_core.test_app import clarify, evidence_in
    from tests.tidb.test_schema import TABLE_PROBE

    canary = 4242424242
    outcomes = schema_results()
    outcomes[QUERY] = Result(("id",), [(canary,)])
    _, drv = adapter(outcomes)
    env.drv = drv
    target = tidb_config()
    target["tidb"]["database"] = "app"
    async with env.running(env.config(targets=[target])) as served:
        await served.page()
        message = env.scripts.add(
            "在 tidb-a 查询订单",
            tool_call("run_readonly_query", cluster="tidb-a", sql=QUERY),
            cite(),
        )
        body = (await served.turn(message, "query")).json()
        assert body["state"] == "completed" and str(canary) in json.dumps(body)
        outcomes[TABLE_PROBE] = ProgrammingError(1142, "SELECT denied private-canary")

        def no_old_facts(call):
            assert str(canary) not in json.dumps(call.input)
            assert not evidence_in(call)
            return clarify("原表当前不可读，不能沿用旧结果")(call)

        follow = env.scripts.add("解释刚才的订单结果", no_old_facts)
        again = (await served.turn(follow, "query", "r2")).json()
        assert again["state"] == "failed"
        assert follow not in env.scripts.calls
        assert str(canary) not in json.dumps(again) and "private-canary" not in json.dumps(again)
        assert statements(drv).count(QUERY) == 1
        assert statements(drv).count(TABLE_PROBE) >= 2
        assert await served.finish() == 0


@pytest.mark.loopback
async def test_tidb_diagnose_purpose_has_no_business_execution_tool(env: Env) -> None:
    from tests.sdk_core.test_app import clarify

    _, drv = adapter(schema_results())
    env.drv = drv
    target = tidb_config()
    target["tidb"]["database"] = "app"
    async with env.running(env.config(targets=[target])) as served:
        await served.page()
        message = env.scripts.add("解释 SQL，不取数据", clarify("请提供待解释 SQL"))
        body = (await served.turn(message, "diagnose")).json()
        assert body["state"] == "completed"
        seen = env.scripts.tools_seen(message)[0]
        assert "run_readonly_query" not in seen
        assert "explain_query" in seen
        assert not any("propose" in name or "action" in name for name in seen)
        assert drv.attempts == 0
        assert await served.finish() == 0


def test_documented_tidb_target_loads_in_formal_config() -> None:
    from pathlib import Path

    target = json.loads(
        (Path(__file__).resolve().parents[2] / "examples/tidb-target.example.json").read_text()
    )
    base = json.loads(
        (Path(__file__).resolve().parents[2] / "examples/xiaowei.example.json").read_text()
    )
    base["targets"].append(target)
    config = ServeConfig.model_validate(base)
    assert config.targets[-1].type == "tidb"
    assert config.targets[-1].tidb.tls
    assert config.targets[-1].tidb.audit is None


@pytest.mark.loopback
async def test_three_tidb_clusters_and_starrocks_route_only_to_the_selected_target(
    env: Env,
) -> None:
    from tests.p1b.test_starrocks_adapter import driver as sr_driver
    from tests.sdk_core.test_runtime import SALES

    targets = [tidb_config(f"tidb-{name}") for name in "abc"]
    for target in targets:
        target["tidb"]["database"] = "app"
    drivers = {
        "tidb-a": adapter(schema_results("7.5.3"))[1],
        "tidb-b": adapter(schema_results("6.5.5"))[1],
        "tidb-c": adapter(schema_results("7.5.3"))[1],
        "sr-test": sr_driver(SALES),
    }
    config = env.config(targets=[target_config(), *targets])
    async with env.running(config, starrocks_connect=drivers) as served:
        await served.page()
        message = env.scripts.add(
            "在 tidb-b 执行 SELECT id FROM orders LIMIT 3",
            tool_call("run_readonly_query", cluster="tidb-b", sql=QUERY),
            cite(),
        )
        body = (await served.turn(message, "query")).json()
        assert body["state"] == "completed"
        assert {fact["target_id"] for fact in body["delivery"]["facts"]} == {"tidb-b"}
        assert statements(drivers["tidb-b"]).count(QUERY) == 1
        assert all(drivers[name].attempts == 0 for name in ("tidb-a", "tidb-c", "sr-test"))
        assert await served.finish() == 0


@pytest.mark.security
@pytest.mark.parametrize("missing", [False, True])
def test_unknown_target_type_reports_only_the_real_field_without_echoing_its_value(
    tmp_path, missing
) -> None:
    from xiaowei.runtime import ConfigError, load_config

    target = tidb_config()
    if missing:
        target.pop("type")
    else:
        target["type"] = "canary-private-type"
    path = tmp_path / "xiaowei.json"
    path.write_text(json.dumps(serve_config(8501, targets=[target])))
    with pytest.raises(ConfigError) as rejected:
        load_config(path)
    assert "targets.0.type" in str(rejected.value)
    assert "canary" not in str(rejected.value)


def test_tidb_config_error_points_to_the_actual_connection_field(tmp_path) -> None:
    from xiaowei.runtime import ConfigError, load_config

    target = tidb_config()
    target["tidb"]["host"] = ""
    path = tmp_path / "xiaowei.json"
    path.write_text(json.dumps(serve_config(8501, targets=[target])))
    with pytest.raises(ConfigError) as rejected:
        load_config(path)
    assert "targets.0.tidb.host" in str(rejected.value)
    assert "targets.0.tidb.tidb" not in str(rejected.value)


@pytest.mark.loopback
@pytest.mark.parametrize("version", ["6.5.5", "7.5.3"])
async def test_tidb_formal_explain_keeps_limit_and_never_executes_original(env: Env, version):
    from tests.tidb.test_schema import PLANS

    sql = "SELECT id FROM app.orders LIMIT 123456"
    outcomes = schema_results(version)
    outcomes["EXPLAIN " + sql] = PLANS
    _, drv = adapter(outcomes)
    env.drv = drv
    target = tidb_config()
    target["tidb"]["database"] = "app"
    async with env.running(env.config(targets=[target])) as served:
        await served.page()
        message = env.scripts.add(
            "为什么这条 SQL 慢？不要执行它：" + sql,
            tool_call("explain_query", cluster="tidb-a", sql=sql),
            cite(),
        )
        body = (await served.turn(message, "diagnose")).json()
        assert body["state"] == "completed", body
        facts = [f for f in body["delivery"]["facts"] if f["tool_id"] == "local/explain_query"]
        assert len(facts) == 1 and facts[0]["metadata"]["sql"] == "EXPLAIN " + sql
        assert statements(drv).count("EXPLAIN " + sql) == 1 and sql not in statements(drv)
        assert "run_readonly_query" not in env.scripts.tools_seen(message)[0]
        assert await served.finish() == 0


@pytest.mark.loopback
@pytest.mark.parametrize(
    "sql", ["SELECT EXISTS(SELECT id FROM app.orders) AS n", "SELECT id FROM app.v"]
)
async def test_tidb_65_formal_unsafe_plan_refusal_has_no_database_io(env: Env, sql):
    from tests.sdk_core.test_app import clarify

    _, drv = adapter(schema_results("6.5.5"))
    env.drv = drv
    target = tidb_config()
    target["tidb"]["database"] = "app"
    async with env.running(env.config(targets=[target])) as served:
        await served.page()
        message = env.scripts.add(
            "只取计划，不执行 SQL：" + sql,
            tool_call("explain_query", cluster="tidb-a", sql=sql),
            clarify("该版本不能安全取该计划；请提供已有慢日志计划用于静态分析。"),
        )
        body = (await served.turn(message, "diagnose")).json()
        assert body["state"] == "completed", body
        assert drv.attempts == 0 and statements(drv) == []
        assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


@pytest.mark.loopback
async def test_tidb_formal_explain_database_error_stops_the_turn(env: Env):
    sql = "SELECT id FROM app.orders LIMIT 3"
    outcomes = schema_results()
    outcomes["EXPLAIN " + sql] = ProgrammingError(1105, "synthetic plan execution error")
    _, drv = adapter(outcomes)
    env.drv = drv
    target = tidb_config()
    target["tidb"]["database"] = "app"
    async with env.running(env.config(targets=[target])) as served:
        await served.page()
        message = env.scripts.add(
            "解释这条 SQL", tool_call("explain_query", cluster="tidb-a", sql=sql)
        )
        body = (await served.turn(message, "diagnose")).json()
        assert body["state"] == "failed", body
        assert len(env.scripts.calls[message]) == 1
        assert statements(drv).count("EXPLAIN " + sql) == 1 and sql not in statements(drv)
        assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


def diagnostic_config(env: Env):
    from tests.sdk_core.test_runtime import OPERATOR

    from xiaowei.starrocks_tools import DIAGNOSTICS_QUERY, QUERY_TOOLS

    allowed = sorted(QUERY_TOOLS | {DIAGNOSTICS_QUERY})
    target = tidb_config()
    target["tidb"]["database"] = "app"
    return env.config(
        targets=[target],
        data_policy={"input": {"max_bytes": 2000}, "model_tools": allowed},
        access={"policy_version": "p1", "grants": {OPERATOR: allowed, "alice": allowed}},
    )


@pytest.mark.loopback
@pytest.mark.parametrize("lowercase", [False, True])
async def test_tidb_formal_diagnostics_returns_slow_queries_with_source_evidence(
    env: Env, lowercase
):
    from tests.tidb.test_diagnostics import SQL, diagnostic_results

    sql = SQL.replace("CLUSTER_SLOW_QUERY", "cluster_slow_query") if lowercase else SQL
    outcomes = diagnostic_results()
    outcomes[sql] = outcomes[SQL]
    _, drv = adapter(outcomes)
    env.drv = drv
    async with env.running(diagnostic_config(env)) as served:
        await served.page()
        message = env.scripts.add(
            "在 tidb-a 查询 2026-09-30 15:00 到 16:00 超过一秒、最慢的三次执行，不重跑原 SQL",
            tool_call("query_diagnostics", cluster="tidb-a", sql=sql), cite(),
        )
        body = (await served.turn(message, "diagnose")).json()
        assert body["state"] == "completed", body
        facts = [f for f in body["delivery"]["facts"]
                 if f["tool_id"] == "local/query_diagnostics"]
        assert len(facts) == 1 and facts[0]["rows"][0]["Query_time"] == 9.0
        assert "run_readonly_query" not in env.scripts.tools_seen(message)[0]
        assert statements(drv).count(sql) == 1
        assert not any(s.startswith("SELECT sensitive") for s in statements(drv))
        assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1


@pytest.mark.loopback
async def test_tidb_diagnostic_history_revocation_prevents_model_and_old_delivery(env: Env):
    from tests.tidb.test_diagnostics import SQL, diagnostic_results

    outcomes = diagnostic_results()
    _, drv = adapter(outcomes)
    env.drv = drv
    async with env.running(diagnostic_config(env)) as served:
        await served.page()
        message = env.scripts.add(
            "取慢 SQL", tool_call("query_diagnostics", cluster="tidb-a", sql=SQL), cite()
        )
        body = (await served.turn(message, "diagnose")).json()
        assert body["state"] == "completed", body
        probe = "SELECT 1 FROM `INFORMATION_SCHEMA`.`CLUSTER_SLOW_QUERY` WHERE 1 = 0"
        outcomes[probe] = ProgrammingError(1142, "SELECT denied diagnostic private-canary")
        follow = env.scripts.add("继续解释刚才的慢 SQL")
        again = (await served.turn(follow, "diagnose", "r2")).json()
        assert again["state"] == "failed"
        assert follow not in env.scripts.calls
        assert "sensitive" not in json.dumps(again) and "private-canary" not in json.dumps(again)
        assert statements(drv).count(SQL) == 1
        assert await served.finish() == 0


@pytest.mark.loopback
async def test_tidb_diagnostics_database_error_is_terminal(env: Env):
    from tests.tidb.test_diagnostics import SQL, diagnostic_results

    outcomes = diagnostic_results()
    outcomes[SQL] = ProgrammingError(1105, "one node unreachable synthetic")
    _, drv = adapter(outcomes)
    env.drv = drv
    async with env.running(diagnostic_config(env)) as served:
        await served.page()
        message = env.scripts.add(
            "取慢 SQL", tool_call("query_diagnostics", cluster="tidb-a", sql=SQL)
        )
        body = (await served.turn(message, "diagnose")).json()
        assert body["state"] == "failed"
        assert len(env.scripts.calls[message]) == 1 and statements(drv).count(SQL) == 1
        assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


@pytest.mark.loopback
async def test_tidb_t8_select_assessment_reuses_protected_plan_and_cites_evidence(env: Env):
    from tests.p1b.test_starrocks_adapter import Result
    from tests.tidb.test_schema import COLUMNS_SQL, PLANS

    from xiaowei.starrocks import OBJECT_COLUMNS_SQL

    sql = "SELECT customer_id, SUM(amount) FROM app.orders GROUP BY customer_id"
    outcomes = schema_results()
    outcomes[COLUMNS_SQL].rows.append(("app", "orders", "amount", "decimal", "YES", None))
    original_columns = outcomes[OBJECT_COLUMNS_SQL]
    outcomes[OBJECT_COLUMNS_SQL] = lambda args: (
        Result(("col", "type"), [("id", "bigint"), ("customer_id", "bigint"),
                                  ("amount", "decimal")])
        if args[1] == "orders" else original_columns(args)
    )
    outcomes["EXPLAIN " + sql] = PLANS
    _, drv = adapter(outcomes)
    env.drv = drv
    target = tidb_config()
    target["tidb"]["database"] = "app"
    async with env.running(env.config(targets=[target])) as served:
        await served.page()
        message = env.scripts.add(
            "评估以下 SQL 的执行影响，不执行它：" + sql,
            tool_call("explain_query", cluster="tidb-a", sql=sql), cite(),
        )
        body = (await served.turn(message, "query")).json()
        assert body["state"] == "completed", body
        facts = body["delivery"]["facts"]
        assert len(facts) == 1 and facts[0]["tool_id"] == "local/explain_query"
        assert facts[0]["metadata"]["sql"] == "EXPLAIN " + sql
        assert sql not in statements(drv) and statements(drv).count("EXPLAIN " + sql) == 1
        assert await served.finish() == 0


@pytest.mark.loopback
async def test_tidb_lock_evidence_supports_t8_followup_without_business_execution(env: Env):
    from tests.sdk_core.test_app import answer
    from tests.tidb.test_cluster import LOCK_SQL, cluster_results

    _, drv = adapter(cluster_results())
    env.drv = drv
    values = diagnostic_config(env).model_dump(mode="json")
    values["targets"][0]["tidb"]["policy"]["allowed_functions"] += ["NOW", "UNIX_TIMESTAMP"]
    async with env.running(ServeConfig.model_validate(values)) as served:
        await served.page()
        initial = env.scripts.add(
            "评估 UPDATE app.orders SET status='closed' WHERE created_at < '2025-01-01'",
            lambda call: answer([], advice="尚无运行证据，需核实事务与锁等待。"),
        )
        assert (await served.turn(initial, "query")).json()["state"] == "completed"
        follow = env.scripts.add(
            "订单更新正卡住，当前等待会改变刚才的执行建议吗？集群仍为 tidb-a。",
            tool_call("query_diagnostics", cluster="tidb-a", sql=LOCK_SQL),
            cite("当前等待应结合事务确认后再决定执行时机；这不是生产执行批准。"),
        )
        body = (await served.turn(follow, "diagnose", "r2")).json()
        assert body["state"] == "completed", body
        row = body["delivery"]["facts"][0]["rows"][0]
        assert row["waiting_trx"] == 222 and row["holding_trx"] == 111
        assert row["wait_seconds"] == 15.0 and row["CURRENT_SQL_DIGEST_TEXT"] is None
        assert "run_readonly_query" not in env.scripts.tools_seen(follow)[0]
        assert statements(drv).count(LOCK_SQL) == 1
        assert not any(s.startswith("UPDATE") for s in statements(drv))
        assert await served.finish() == 0


@pytest.mark.loopback
async def test_tidb_cluster_partial_warning_reaches_formal_feishu_delivery(env: Env):
    from tests.p1b.test_starrocks_adapter import Result
    from tests.sdk_core.test_feishu_render import tables, text_parts
    from tests.sdk_core.test_runtime import FakeChannel, feishu_config, feishu_event, until
    from tests.tidb.test_adapter import WARNINGS_SQL
    from tests.tidb.test_cluster import REGION_SQL, cluster_results

    outcomes = cluster_results()
    outcomes[WARNINGS_SQL] = Result(("Level", "Code", "Message"), [
        ("Warning", 1105, "synthetic node-b missing"),
    ])
    _, drv = adapter(outcomes)
    env.drv = drv
    values = diagnostic_config(env).model_dump(mode="json")
    values["feishu"] = feishu_config()
    channel = FakeChannel()
    async with env.running(ServeConfig.model_validate(values), feishu_channel=channel) as served:
        message = env.scripts.add(
            "在 tidb-a 检查 Region，不能把缺节点当健康",
            tool_call("query_diagnostics", cluster="tidb-a", sql=REGION_SQL),
            cite("只有返回节点的部分证据，不能判断全群健康。"),
        )
        channel.emit_raw_from_sdk_thread(feishu_event(env, message, "om_tidb_cluster"))
        await until(lambda: len(channel.sends) >= 1)
        card = channel.sends[0][1]
        rendered = "\n".join(text_parts(card))
        assert "tidb-a" in rendered and "部分证据" in rendered
        assert tables(card)[0]["rows"] == [{"c0": "8", "c1": "101"}]
        assert statements(drv).count(REGION_SQL) == 1
        assert await served.finish() == 0
