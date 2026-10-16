"""R7 fixed scenarios via the existing runtime eval harness. No real-model calls."""

import json

import pytest
from agents.testing import function_call
from sqlalchemy.engine import URL
from tests.sdk_core.dialogue_gate import (
    ENV_SCENARIOS,
    R7_SCENARIOS,
    TABLE,
    run_dialogue,
    tool_choices,
)
from tests.sdk_core.test_app import Scripts, cite, clarify, tool_call
from tests.sdk_core.test_dialogue_gate import PROFILE

pytestmark = pytest.mark.loopback


@pytest.fixture(autouse=True)
def model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "r7-synthetic-model-key")


@pytest.mark.parametrize(
    "case",
    [
        "explicit_target",
        "unselected_target",
        "target_unavailable",
        "group_reference",
        "group_unclear",
        "new_session",
    ],
)
async def test_r7_fixed_target_scenarios(postgres_url: URL, case: str) -> None:
    scenario = next(s for s in R7_SCENARIOS if s.name == "r7_" + case)
    scripts = Scripts()
    first = scenario.messages[0][1]

    def describe(target):
        return tool_call(
            "describe_table", cluster=target, database="data_center", table=TABLE, cursor=None
        )

    layout = tool_call("describe_table_layout", cluster="fat", database="data_center", table=TABLE)
    if case == "unselected_target":
        scripts.add(first, clarify("请先选择环境：fat、fat1 或 pre"))
    elif case == "target_unavailable":
        scripts.add(first, describe("fat"), clarify("fat暂不可用，本轮停止，不改查其他集群"))
    else:
        scripts.add(first, describe("fat"), cite(""))
        if case == "group_reference":
            scripts.add(scenario.messages[1][1], layout, cite(""))
        elif case == "group_unclear":
            scripts.add(scenario.messages[1][1], describe("pre"), cite(""))
            scripts.add(scenario.messages[2][1], clarify("请选择fat或pre"))
        elif case == "new_session":
            scripts.add(scenario.messages[1][1], clarify("请选择集群"))
    results = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(scenario,), repeats=1
    )
    assert all(r["state"] == "completed" for r in results), results
    if case == "unselected_target":
        # 原多匹配/发现不完整样例的保护：不读取猜选候选的结构、布局、DDL 或业务数据。
        (result,) = results
        assert result["tool_choices_by_target"] == {} and result["facts"] == []
        assert result["io_counts"] == {}
        assert "不做跨环境发现" in scripts.calls[first][-1].instructions
    elif case == "target_unavailable":
        assert results[0]["tool_choices_by_target"] == {"fat": {"describe_table": 1}}
        assert results[0]["facts"] == [] and results[0]["io_counts"] == {}
        output = next(
            i["output"]
            for i in scripts.calls[first][1].input
            if i.get("type") == "function_call_output"
        )
        assert json.loads(output)["code"] == "schema_unavailable"
    else:
        assert results[0]["facts_by_tool_target"] == {"fat": {"local/describe_table": 1}}
        if case == "group_reference":
            assert results[1]["membership_checks_for_actor"] > 0
            assert results[1]["tool_choices_by_target"] == {"fat": {"describe_table_layout": 1}}
            assert "执行许可不继承" in scripts.calls[scenario.messages[1][1]][0].instructions
        elif case in {"new_session", "group_unclear"}:
            if case == "group_unclear":
                # B 明确点名 pre：只在 pre 查询，并按 B 本人复核群成员资格。
                assert results[1]["tool_choices_by_target"] == {"pre": {"describe_table": 1}}
                assert results[1]["membership_checks_for_actor"] > 0
            assert results[-1]["tool_choices_by_target"] == {}
            assert results[-1]["io_counts"].get("query", 0) == 0
            if case == "new_session":
                assert results[-1]["io_counts"] == {}
                assert results[-1]["requests"][0]["history_tool_calls"] == 0


@pytest.mark.parametrize("protocol", ["responses", "chat", "vertex"])
def test_choice_report_retains_only_closed_labels(protocol: str) -> None:
    args = {"cluster": "PRIVATE_SENTINEL", "sql": "PRIVATE_SQL"}
    if protocol == "responses":
        body = {
            "output": [
                {"type": "function_call", "name": "PRIVATE_TOOL", "arguments": json.dumps(args)}
            ]
        }
    elif protocol == "chat":
        body = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "PRIVATE_TOOL", "arguments": json.dumps(args)}}
                        ]
                    }
                }
            ]
        }
    else:
        body = {
            "candidates": [
                {"content": {"parts": [{"functionCall": {"name": "PRIVATE_TOOL", "args": args}}]}}
            ]
        }
    choices = tool_choices(json.dumps(body).encode(), frozenset({"pre"}))
    assert choices == [("other-tool", "other-target")]
    assert "PRIVATE" not in json.dumps(choices)


async def test_fixed_busy_sample_stops_at_governance(postgres_url: URL) -> None:
    scenario = next(s for s in R7_SCENARIOS if s.name == "r7_busy")
    scripts = Scripts()
    message = scenario.messages[0][1]
    call = tool_call("list_databases", cluster="pre", page_size=20, cursor=None)
    scripts.add(message, call, call, call, clarify("本轮该集群再试已用完，请后续再问"))
    (result,) = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(scenario,), repeats=1
    )
    assert result["state"] == "completed" and result["facts"] == []
    assert result["tool_choices_by_target"] == {"pre": {"list_databases": 3}}
    assert result["synthetic_slot_timeouts_by_target"] == {"pre": 2}
    assert result["connection_attempts_by_target"] == {"pre": 0}
    assert result["io_counts"] == {}


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"\xff",
        b"[]",
        b'{"output":[null,4,{"type":"function_call","name":[]}]}',
        b'{"candidates":[null,{"content":4},{"content":{"parts":[null,{"functionCall":[]} ]}}]}',
        b'{"choices":[null,{"message":null},{"message":{"tool_calls":[null,{"function":4}]}}]}',
    ],
)
def test_choice_observer_cannot_replace_model_rejection(body: bytes) -> None:
    assert all(
        tool == "other-tool" and target == "other-target"
        for tool, target in tool_choices(body, frozenset({"pre"}))
    )


@pytest.mark.parametrize(
    "case",
    [
        "after_discovery",
        "explicit_followup",
        "compare_targets",
        "compare_one_unavailable",
        "switch_unavailable",
    ],
)
async def test_r7_context_and_cross_target_tasks(postgres_url: URL, case: str) -> None:
    scenario = next(s for s in R7_SCENARIOS if s.name == "r7_" + case)
    scripts = Scripts()

    def descriptions(targets):
        return [
            function_call(
                "describe_table",
                {"cluster": t, "database": "data_center", "table": TABLE, "cursor": None},
                call_id="desc-" + t,
            )
            for t in targets
        ]

    if case == "after_discovery":

        def databases(_):
            return [
                function_call(
                    "list_databases",
                    {"cluster": t, "page_size": 20, "cursor": None},
                    call_id="dbs-" + t,
                )
                for t in ("fat", "fat1", "pre")
            ]

        scripts.add(scenario.messages[0][1], databases, cite(""))
        scripts.add(scenario.messages[1][1], clarify("存在多个同名对象，请选集群"))
    elif case == "explicit_followup":
        scripts.add(scenario.messages[0][1], lambda _: descriptions(["fat"]), cite(""))
        scripts.add(
            scenario.messages[1][1],
            tool_call("describe_table_layout", cluster="fat", database="data_center", table=TABLE),
            cite(""),
        )
        scripts.add(scenario.messages[2][1], lambda _: descriptions(["pre"]), cite(""))
    elif case == "compare_one_unavailable":
        scripts.add(
            scenario.messages[0][1],
            lambda _: descriptions(["fat", "fat1", "pre"]),
            cite("fat1 暂不可用，只比较 fat 与 pre"),
        )
    elif case == "compare_targets":
        scripts.add(
            scenario.messages[0][1], lambda _: descriptions(["fat", "fat1", "pre"]), cite("")
        )
    else:
        scripts.add(scenario.messages[0][1], lambda _: descriptions(["fat"]), cite(""))
        scripts.add(
            scenario.messages[1][1],
            lambda _: descriptions(["fat1"]),
            clarify("fat1暂不可用，本轮停止，不改查其他环境"),
        )

    results = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(scenario,), repeats=1
    )
    assert all(r["state"] == "completed" for r in results), results
    if case == "after_discovery":
        assert set(results[0]["tool_choices_by_target"]) == {"fat", "fat1", "pre"}
        assert (
            results[1]["tool_choices_by_target"] == {}
            and results[1]["io_counts"].get("query", 0) == 0
        )
    elif case == "explicit_followup":
        assert [r["tool_choices_by_target"] for r in results] == [
            {"fat": {"describe_table": 1}},
            {"fat": {"describe_table_layout": 1}},
            {"pre": {"describe_table": 1}},
        ]
    elif case == "compare_targets":
        assert results[0]["facts_by_tool_target"] == {
            t: {"local/describe_table": 1} for t in ("fat", "fat1", "pre")
        }
        # Driver metadata differs by source; facts must not get silently substituted.
        for t in ("fat", "fat1", "pre"):
            assert t in results[0]["io_by_target"]
    elif case == "compare_one_unavailable":
        # 原 discovery_error 的同轮保护：用户点名的多个环境中一个失败，失败源无事实、零 I/O，
        # 其余照常交付且不串源。
        assert results[0]["tool_choices_by_target"] == {
            t: {"describe_table": 1} for t in ("fat", "fat1", "pre")
        }
        assert {f["target"] for f in results[0]["facts"]} == {"fat", "pre"}
        assert results[0]["io_by_target"]["fat1"] == {}
    else:
        # 原 discovery_error 的保护：失败源没有事实、结果不串源、不改查其他环境代替。
        assert results[0]["facts_by_tool_target"] == {"fat": {"local/describe_table": 1}}
        assert results[1]["tool_choices_by_target"] == {"fat1": {"describe_table": 1}}
        assert results[1]["facts"] == []
        assert results[1]["io_by_target"]["fat1"] == {}
        assert results[1]["io_counts"].get("query", 0) == 0


async def test_collector_selects_r7_without_changing_u1_default(
    postgres_url: URL, monkeypatch, tmp_path
) -> None:
    from contextlib import asynccontextmanager

    from scripts import dialogue_real_model as command
    from tests.sdk_core.dialogue_gate import SCENARIOS

    monkeypatch.setattr(command, "source_sha", lambda: "a" * 40)
    monkeypatch.setattr(command, "load_profile", lambda _: PROFILE)
    monkeypatch.setattr(command, "check_postgres", lambda: None)
    observed = []

    @asynccontextmanager
    async def isolated():
        yield postgres_url

    async def capture(profile, url, network, **kwargs):
        assert profile == PROFILE and url == postgres_url
        observed.append(kwargs.get("scenarios", SCENARIOS))
        return []  # Verifies selection only, without instantiating the real network transport.

    monkeypatch.setattr(command, "isolated_database", isolated)
    monkeypatch.setattr(command, "run_dialogue", capture)
    report = await command.collect(tmp_path / "unused.json", 3, "r7")
    assert report["suite"] == "r7" and observed[-1] is R7_SCENARIOS
    report = await command.collect(tmp_path / "unused.json", 3, "env")
    assert report["suite"] == "env" and observed[-1] is ENV_SCENARIOS
    report = await command.collect(tmp_path / "unused.json", 3)
    assert report["suite"] == "u1" and observed[-1] is SCENARIOS
