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
        assert "explain_query" not in env.scripts.tools_seen(message)[0]
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
        assert "explain_query" not in seen
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
