"""环境选择固定样例的离线统计口径。脚本端点只证明调用链与越界计数，不代表模型会遵守。"""

from dataclasses import replace

import pytest
from agents.testing import function_call
from sqlalchemy.engine import URL
from tests.sdk_core.dialogue_gate import ENV_SCENARIOS, NONE, TABLE, run_dialogue
from tests.sdk_core.test_app import Scripts, cite, clarify, tool_call
from tests.sdk_core.test_dialogue_gate import PROFILE

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


async def test_followed_selection_has_no_out_of_scope_calls(postgres_url: URL) -> None:
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
    assert [s["starrocks_calls"] for s in scopes] == [0, 1, 1, 0, 1, 0]
    assert results[0]["io_counts"] == {}
    # 提及 pre 时只澄清：pre 零 I/O；回放中复核 fat 历史证据的探测不是模型调用，也不是取数。
    assert results[3]["io_by_target"]["pre"] == {}
    assert all("query" not in kinds for kinds in results[3]["io_by_target"].values())
    assert results[4]["tool_choices_by_target"] == {"pre": {"list_tables": 1}}
    # 切到 pre 后再看 fat 的历史结果：引用历史证据，没有模型调用，也没有业务查询。
    assert results[5]["tool_choices_by_target"] == {}
    assert all("query" not in kinds for kinds in results[5]["io_by_target"].values())
    assert {f["target"] for f in results[5]["facts"]} >= {"fat"}


async def test_incident_shape_is_counted_as_out_of_scope(postgres_url: URL) -> None:
    """事故形态：未选环境就对 fat、fat1、pre 列库。统计必须把三次调用都记为越界。"""
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
    assert result["environment_scope"] == {
        "allowed": [],
        "starrocks_calls": 3,
        "out_of_scope_calls": 3,
    }


async def test_group_member_follows_the_shared_environment(postgres_url: URL) -> None:
    group = scenario("env_group_shared")
    scripts = Scripts()
    scripts.add(group.messages[0][1], clarify("已记下用 fat，要查什么？"))
    scripts.add(
        group.messages[1][1],
        tool_call(
            "describe_table", cluster="fat", database="data_center", table=TABLE, cursor=None
        ),
        cite(""),
    )
    results = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(group,), repeats=1
    )
    assert results[1]["environment_scope"]["out_of_scope_calls"] == 0
    assert results[1]["tool_choices_by_target"] == {"fat": {"describe_table": 1}}
    # 沿用群内选定的环境，但执行仍按 B 本人复核群成员资格。
    assert results[1]["membership_checks_for_actor"] > 0


def test_every_env_scenario_labels_each_turn() -> None:
    assert ENV_SCENARIOS
    for item in ENV_SCENARIOS:
        assert item.multi and len(item.environments) == len(item.messages)
    with pytest.raises(ValueError, match="逐轮对应"):
        replace(ENV_SCENARIOS[0], environments=(NONE,))
