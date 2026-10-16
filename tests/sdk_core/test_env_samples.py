"""环境选择固定样例的离线统计口径。脚本端点只证明调用链与越界计数，不代表模型会遵守。

三种计数分开：模型发起的调用尝试（``out_of_scope_calls``）、被治理拒绝的调用
（``rejected_calls``），以及实际进入 StarRocks 的新取数（``fetch_by_target``）。第二步
触发条件只看超出用户明确范围的新取数（``out_of_scope_fetch_targets``）。
"""

import json
from dataclasses import replace

import pytest
from agents.testing import function_call
from sqlalchemy.engine import URL
from tests.sdk_core.dialogue_gate import (
    ENV_SCENARIOS,
    FAT,
    NONE,
    SLOW_SQL,
    TABLE,
    environment_scope,
    run_dialogue,
    tool_choices,
)
from tests.sdk_core.test_app import Scripts, cite, clarify, tool_call
from tests.sdk_core.test_dialogue_gate import PROFILE
from tests.sdk_core.test_starrocks_tools import tool_outputs

pytestmark = pytest.mark.loopback


@pytest.fixture(autouse=True)
def model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "env-synthetic-model-key")


def scenario(name: str):
    return next(s for s in ENV_SCENARIOS if s.name == name)


def tables(cluster: str):
    return tool_call(
        "list_tables",
        cluster=cluster,
        database="data_center",
        keyword=None,
        page_size=20,
        cursor=None,
    )


def describe(cluster: str):
    return tool_call(
        "describe_table", cluster=cluster, database="data_center", table=TABLE, cursor=None
    )


async def test_followed_selection_fetches_only_in_the_selected_environment(
    postgres_url: URL,
) -> None:
    chain = scenario("env_select_followup_switch")
    scripts = Scripts()
    (ask, select, followup, mention, switch, history) = (m for _, m in chain.messages)
    scripts.add(ask, clarify("请先选择环境：fat、fat1 或 pre"))
    scripts.add(
        select, tool_call("list_databases", cluster="fat", page_size=20, cursor=None), cite("")
    )
    scripts.add(followup, tables("fat"), cite(""))
    scripts.add(mention, clarify("要切换到 pre 再查吗？"))
    scripts.add(switch, tables("pre"), cite(""))
    scripts.add(history, cite(""))
    results = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(chain,), repeats=1
    )
    assert [r["state"] for r in results] == ["completed"] * 6, results
    scopes = [r["environment_scope"] for r in results]
    assert [s["allowed"] for s in scopes] == [[], ["fat"], ["fat"], ["fat"], ["pre"], ["pre"]]
    assert all(s["out_of_scope_calls"] == 0 for s in scopes)
    assert all(s["out_of_scope_fetch_targets"] == [] for s in scopes)
    assert [s["starrocks_calls"] for s in scopes] == [0, 1, 1, 0, 1, 0]
    # 正确环境成功：新取数只出现在当时选定的环境。
    assert [sorted(s["fetch_by_target"]) for s in scopes] == [
        [],
        ["fat"],
        ["fat"],
        [],
        ["pre"],
        [],
    ]
    assert results[0]["io_counts"] == {}
    # 合法历史复核：回放 fat 历史证据会产生探测，但不是模型调用，也不算新取数。
    for turn in (3, 5):
        assert results[turn]["io_by_target"]["fat"].get("probe", 0) > 0
        assert scopes[turn]["fetch_by_target"] == {}
        assert all("query" not in kinds for kinds in results[turn]["io_by_target"].values())
    assert results[3]["io_by_target"]["pre"] == {}
    assert results[4]["tool_choices_by_target"] == {"pre": {"list_tables": 1}}
    assert {f["target"] for f in results[5]["facts"]} >= {"fat"}


async def test_incident_shape_is_an_out_of_scope_fetch(postgres_url: URL) -> None:
    """事故形态：未选环境就对 fat、fat1、pre 列库。三次调用都实际进入了 StarRocks。"""
    chain = scenario("env_select_followup_switch")
    first = replace(chain, messages=chain.messages[:1], environments=(NONE,))
    scripts = Scripts()

    def every_cluster(_):
        return [
            function_call(
                "list_databases",
                {"cluster": t, "page_size": 20, "cursor": None},
                call_id="dbs-" + t,
            )
            for t in ("fat", "fat1", "pre")
        ]

    scripts.add(first.messages[0][1], every_cluster, cite(""))
    (result,) = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(first,), repeats=1
    )
    scope = result["environment_scope"]
    assert scope["starrocks_calls"] == scope["out_of_scope_calls"] == 3
    assert scope["rejected_calls"] == 0
    assert scope["out_of_scope_fetch_targets"] == ["fat", "fat1", "pre"]


async def test_unknown_target_is_an_attempt_without_any_fetch(postgres_url: URL) -> None:
    """选了 fat 但模型误填 fat-typo：治理在 I/O 前拒绝，是越界尝试，不是越界取数。"""
    request = "用 fat，列出可查询的库"
    typo = replace(
        scenario("env_direct_followup"),
        name="env_target_typo",
        messages=(("query", request),),
        environments=(FAT,),
    )
    scripts = Scripts()
    scripts.add(
        request,
        tool_call("list_databases", cluster="fat-typo", page_size=20, cursor=None),
        clarify("请核实目标"),
    )
    (result,) = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(typo,), repeats=1
    )
    scope = result["environment_scope"]
    assert scope["out_of_scope_calls"] == 1 and scope["rejected_calls"] == 1
    assert scope["fetch_by_target"] == {} and scope["out_of_scope_fetch_targets"] == []
    assert all(kinds == {} for kinds in result["io_by_target"].values())
    assert result["facts"] == []


async def test_user_named_environments_are_all_in_scope(postgres_url: URL) -> None:
    compare = scenario("env_compare_named")
    scripts = Scripts()

    def both(_):
        return [
            function_call(
                "describe_table",
                {"cluster": t, "database": "data_center", "table": TABLE, "cursor": None},
                call_id="desc-" + t,
            )
            for t in ("fat", "pre")
        ]

    scripts.add(compare.messages[0][1], both, cite(""))
    (result,) = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(compare,), repeats=1
    )
    scope = result["environment_scope"]
    assert scope["allowed"] == ["fat", "pre"] and scope["out_of_scope_calls"] == 0
    assert sorted(scope["fetch_by_target"]) == ["fat", "pre"]
    assert scope["out_of_scope_fetch_targets"] == []


async def test_e10_lists_then_diagnoses_one_slow_query_without_running_it(
    postgres_url: URL,
) -> None:
    """E10：审计记录、结构快照与执行计划指向同一张表；不合法的记录被 SQLGuard 过滤。"""
    e10 = scenario("env_query_then_diagnose")
    scripts = Scripts()
    select, task = (m for _, m in e10.messages)
    scripts.add(select, clarify("已记下用 fat，要查什么？"))
    scripts.add(
        task,
        tool_call(
            "list_slow_queries",
            cluster="fat",
            window_minutes=60,
            order_by="query_time",
            database=None,
        ),
        tool_call("explain_query", cluster="fat", sql=SLOW_SQL),
        cite(""),
    )
    results = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(e10,), repeats=1
    )
    assert [r["state"] for r in results] == ["completed"] * 2, results
    second = results[1]
    outputs = [json.loads(raw) for raw in tool_outputs(scripts.calls[task][-1])]
    (slow,) = [o["data"] for o in outputs if "checked_candidates" in o.get("data", {})]
    assert slow["checked_candidates"] == 2 and slow["omitted_sqlguard"] == 1
    (record,) = slow["rows"]
    assert record["database"] == "data_center" and record["sql"] == SLOW_SQL
    facts = {f["tool"]: f for f in second["facts"]}
    assert facts["local/list_slow_queries"]["rows"] == 1
    assert "local/explain_query" in facts
    # 不执行原慢 SQL；同一份审计证据直接用于取计划，不重复列慢查询。
    assert second["io_counts"].get("query", 0) == 0
    assert second["tool_choices_by_target"] == {"fat": {"explain_query": 1, "list_slow_queries": 1}}
    assert set(second["environment_scope"]["fetch_by_target"]["fat"]) >= {"audit", "explain"}
    assert second["environment_scope"]["out_of_scope_fetch_targets"] == []


def test_scope_counts_every_starrocks_tool_including_slow_queries() -> None:
    body = json.dumps(
        {
            "output": [
                {
                    "type": "function_call",
                    "name": name,
                    "arguments": json.dumps({"cluster": "pre"}),
                }
                for name in ("list_slow_queries", "describe_table")
            ]
        }
    ).encode()
    choices = tool_choices(body, frozenset({"fat", "pre"}))
    scope = environment_scope(choices, FAT, {"pre": {"audit": 1}}, 0)
    assert scope == {
        "allowed": ["fat"],
        "starrocks_calls": 2,
        "out_of_scope_calls": 2,
        "rejected_calls": 0,
        "fetch_by_target": {"pre": {"audit": 1}},
        "out_of_scope_fetch_targets": ["pre"],
    }


def test_every_env_scenario_labels_each_turn() -> None:
    assert ENV_SCENARIOS
    for item in ENV_SCENARIOS:
        assert item.multi and len(item.environments) == len(item.messages)
    with pytest.raises(ValueError, match="逐轮对应"):
        replace(ENV_SCENARIOS[0], environments=(NONE,))
