"""S1: actual runtime limits, not an autonomous-model quality evaluation."""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import URL
from tests.sdk_core.dialogue_gate import ROOT
from tests.sdk_core.mcp_fixture import serve
from tests.sdk_core.session_capacity import (
    PROFILE,
    CapacityRecorder,
    capacity_config,
    drivers,
    sequence,
)
from tests.sdk_core.synthetic_tools import context
from tests.sdk_core.test_monitoring_r1a import query_server
from tests.sdk_core.test_runtime import Env
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_session_policy import _call, _cite, harness

from xiaowei.session import SessionLimitError, SessionLimits

pytestmark = pytest.mark.loopback


@pytest.fixture(autouse=True)
def model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "synthetic-capacity-key")


def save_report(tmp_path: Path, report: dict[str, Any]) -> None:
    (tmp_path / "capacity.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


async def test_default_template_supports_thirty_full_troubleshooting_turns(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = CapacityRecorder()
    recorder.install(monkeypatch)
    with serve(query_server) as upstream:
        config = capacity_config(env, monitoring_url=upstream.url)
        connectors = drivers(config, rows=1)
        async with env.running(
            config,
            model_transport=recorder.network(sequence(30)),
            starrocks_connect=connectors,
        ) as served:
            await served.page()
            for i in range(1, 31):
                result = await recorder.turn(served, i)
                assert result["state"] == "completed", (i, result)
            assert await served.finish() == 0

    assert await env.scalar("SELECT turns FROM xiaowei_session") == 30
    assert (
        await env.scalar(
            "SELECT count(*) FROM xiaowei_request"
            " WHERE state='completed' AND session_state IS NOT NULL"
        )
        == 30
    )
    assert len(upstream.recorder.tool_calls) == 5
    assert config.session_limits.max_history_turns == 100
    report = recorder.report(config, connectors)
    save_report(tmp_path, report)
    assert report["limits"] == {
        "max_history_turns": 100,
        "max_history_bytes": 400000,
        "retention_seconds": 604800,
        "max_request_bytes": 1000000,
        "max_scope_checks": 2048,
        "max_value_bytes": 4000,
        "max_result_bytes": 200000,
        "model_projection_bytes": 400000,
        "session_projection_bytes": 400000,
    }
    assert all(t.replay_bytes <= t.commit_replay_bytes < 400000 for t in recorder.turns)
    for turn in recorder.turns:
        for request in turn.requests:
            assert request["tool_calls"] == request["tool_outputs"] == request["signatures"]
            assert 0 < request["overhead_bytes"] < request["bytes"] <= 1000000
            assert request["sent"]
    encoded = json.dumps(report, ensure_ascii=False)
    for sentinel in ("第 1 轮", "SELECT", "ev_", "原文", "vertex-signature", "synthetic.invalid"):
        assert sentinel not in encoded


@pytest.mark.parametrize("limit", [20, 100])
async def test_last_legal_turn_and_next_turn_have_exact_model_and_query_counts(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limit: int
) -> None:
    recorder = CapacityRecorder(scenario="small")
    recorder.install(monkeypatch)
    config = capacity_config(env, session_limits={"max_history_turns": limit})
    connectors = drivers(config)
    async with env.running(
        config,
        model_transport=recorder.network(sequence(limit + 1, scenario="small")),
        starrocks_connect=connectors,
    ) as served:
        await served.page()
        for i in range(1, limit + 1):
            result = await recorder.turn(served, i)
            assert result["state"] == "completed", (i, result)
        assert await env.scalar("SELECT turns FROM xiaowei_session") == limit
        before = recorder.report(config, connectors)["io"]
        failed = await recorder.turn(served, limit + 1)
        assert failed["state"] == "failed"
        assert recorder.current.requests == []
        assert recorder.current.scope_checks == 0
        assert recorder.report(config, connectors)["io"] == before
        assert before["query"] == 1
        assert await env.scalar("SELECT turns FROM xiaowei_session") == limit
        for t in recorder.turns[1:-1]:
            (wire,) = t.requests
            assert wire["tool_calls"] == wire["tool_outputs"] == wire["signatures"] == 1
    save_report(tmp_path, recorder.report(config, connectors))


async def test_raising_turn_limit_continues_active_history_but_not_sealed_or_closed(
    postgres_url: URL,
) -> None:
    from tests.sdk_core.test_session_policy import INPUT_POLICY

    limits = SessionLimits.model_validate(
        json.loads((ROOT / "examples/xiaowei.example.json").read_text())["session_limits"]
    )
    baseline = limits.model_copy(update={"max_history_turns": 20})
    async with harness(postgres_url) as h:
        for i in range(1, 21):
            await h.turn(
                context(turn=f"t{i}"), [_call(), _cite()] if i == 1 else [_cite()], limits=baseline
            )
        await h.refused(context(turn="t21"), SessionLimitError, limits=baseline)
        continued, _ = await h.turn(context(turn="t22"), [_cite()], limits=limits)
        assert await h.state() == ("active", 21)
        calls = [x for x in continued.calls[0].input if x.get("type") == "function_call"]
        assert len(calls) == 1 and calls[0]["call_id"] == "c1"
        assert len(h.adapter.calls) == 1  # Increasing the limit did not replay business I/O.
        h.grants.revoke_all()
        await h.refused(context(turn="revoked"), limits=limits)
        assert len(h.adapter.calls) == 1

    for state in ("sealed", "closed"):
        async with harness(postgres_url) as h:
            ctx = context(session=state)
            if state == "sealed":
                small = baseline.model_copy(update={"max_history_bytes": 300})
                with pytest.raises(SessionLimitError):
                    await h.turn(ctx, [_call(), _cite()], limits=small)
            else:
                await h.session(ctx, limits=baseline, input_policy=INPUT_POLICY).clear_session()
            await h.refused(ctx, limits=limits)
            assert (await h.state(state))[0] == state


@pytest.mark.parametrize("when", ["first", "after_tool"])
async def test_oversized_serialized_request_is_rejected_before_that_http_io(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, when: str
) -> None:
    recorder = CapacityRecorder(scenario="multi_query")
    recorder.install(monkeypatch)
    # 40KB 容纳当前首请求的提示词/工具声明，三份真实结果使下一请求超限。
    # 这是测试门槛，不改变产品默认 1MB；下方仍断言超限发生前后的准确 I/O。
    cap = 500 if when == "first" else 40000
    config = capacity_config(env, model={"max_request_bytes": cap})
    connectors = drivers(config, rows=45)
    async with env.running(
        config,
        model_transport=recorder.network(sequence(1, scenario="multi_query")),
        starrocks_connect=connectors,
    ) as served:
        await served.page()
        result = await recorder.turn(served, 1)
        assert result["state"] == "failed"
        rejected = recorder.current.requests[-1]
        assert rejected["bytes"] > cap and rejected["sent"] is False
        assert len(recorder.current.requests) == (1 if when == "first" else 2)
        if when == "after_tool":
            first = recorder.current.requests[0]
            assert first["bytes"] <= cap and first["sent"] is True
        assert recorder.report(config, connectors)["io"]["query"] == (0 if when == "first" else 3)
        assert await env.scalar("SELECT count(*) FROM agent_messages") == 0
    save_report(tmp_path, recorder.report(config, connectors))


@pytest.mark.parametrize(
    ("scenario", "changes", "driver_changes", "monitor_lines"),
    [
        ("small", {"session_limits": {"max_history_bytes": 2500}}, {}, 0),
        ("ddl", {}, {"ddl_bytes": 60000}, 0),
        ("multi_query", {}, {"rows": 45}, 0),
        ("monitoring", {}, {}, 1000),
    ],
    # MCP 接收上限不再是失败边界：超限读取记为本源未核实、本轮照常交付（见 P2 接收上限用例）。
    ids=["history-2500", "ddl-60000", "three-large-queries", "wide-monitoring"],
)
async def test_first_non_turn_limit_is_reported_without_trimming_or_increasing_limits(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scenario: str,
    changes: dict[str, Any],
    driver_changes: dict[str, Any],
    monitor_lines: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from mcp.server.mcpserver import MCPServer
    from tests.sdk_core.mcp_fixture import LOOPBACK, recording

    response_sizes: list[int] = []

    def wide_monitoring(recorder: Any) -> Any:
        server = MCPServer("capacity-wide")

        @server.tool(structured_output=False)
        def query(query: str) -> str:
            recorder.tool_calls.append(("query", {"query": query}))
            return json.dumps(
                {"result": "合成监控值\n" * monitor_lines, "warnings": None}, ensure_ascii=False
            )

        inner = recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)

        async def measured(scope: Any, receive: Any, send: Any) -> None:
            size = 0

            async def observe(message: Any) -> None:
                nonlocal size
                if message["type"] == "http.response.body":
                    size += len(message.get("body", b""))
                await send(message)

            try:
                await inner(scope, receive, observe)
            finally:
                if scope["type"] == "http":
                    response_sizes.append(size)

        return measured

    recorder = CapacityRecorder(scenario=scenario)
    recorder.install(monkeypatch)
    caplog.set_level("INFO", logger="xiaowei.app")
    with serve(wide_monitoring if scenario == "monitoring" else query_server) as upstream:
        config = capacity_config(env, monitoring_url=upstream.url, **changes)
        connectors = drivers(config, **driver_changes)
        async with env.running(
            config,
            model_transport=recorder.network(sequence(101, scenario=scenario)),
            starrocks_connect=connectors,
        ) as served:
            await served.page()
            for i in range(1, 102):
                result = await recorder.turn(served, i)
                if result["state"] == "failed":
                    break
            else:
                pytest.fail("Expected a non-turn capacity boundary")
            assert i < 101  # 100 is not a promise that other limits cannot trigger sooner.
            assert await env.scalar("SELECT turns FROM xiaowei_session") < 100
            assert "reason=session_unavailable" in caplog.text
            assert await env.scalar("SELECT state FROM xiaowei_session") == "sealed"
            assert recorder.current.commit_replay_bytes > config.session_limits.max_history_bytes
            first_limit = "history_bytes"
            if scenario != "multi_query":
                assert i > 1
        report = recorder.report(config, connectors)
        report["mcp_max_response_bytes"] = config.mcp_servers[0].max_response_bytes
        report["first_failed_turn"] = i
        report["first_limit"] = first_limit
        report["mcp_calls"] = len(upstream.recorder.tool_calls)
        report["mcp_max_response_bytes_observed"] = max(response_sizes, default=0)
        save_report(tmp_path, report)
        assert report["limits"]["max_request_bytes"] == 1000000
        assert report["limits"]["max_scope_checks"] == 2048


@pytest.mark.parametrize("targets", [1, 3])
@pytest.mark.parametrize("monitoring", [False, True])
async def test_target_and_tool_growth_records_actual_profile_headroom(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, targets: int, monitoring: bool
) -> None:
    from xiaowei.runtime import ServeConfig

    recorder = CapacityRecorder(scenario="small")
    recorder.install(monkeypatch)
    with serve(query_server) as upstream:
        values = capacity_config(
            env, monitoring_url=upstream.url if monitoring else None
        ).model_dump(mode="json")
        for i in range(1, targets):
            other = json.loads(json.dumps(values["targets"][0]))
            other["starrocks"]["target_id"] = f"pre{i}"
            values["targets"].append(other)
        config = ServeConfig.model_validate(values)
        connectors = drivers(config)
        async with env.running(
            config,
            model_transport=recorder.network(sequence(1, scenario="small")),
            starrocks_connect=connectors,
        ) as served:
            await served.page()
            assert (await recorder.turn(served, 1))["state"] == "completed"
        first, following = recorder.current.requests
        assert first["tool_count"] == 8 + monitoring
        assert following["bytes"] > first["bytes"]
        report = recorder.report(config, connectors)
        report["first_request_headroom"] = config.model.max_request_bytes - first["bytes"]
        report["post_tool_headroom"] = config.model.max_request_bytes - following["bytes"]
        assert report["first_request_headroom"] > 0 and report["post_tool_headroom"] > 0
        save_report(tmp_path, report)


async def test_scope_checks_can_stop_large_catalog_before_history_or_request_limits(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.sdk_core.session_capacity import finish, parallel, question
    from tests.sdk_core.test_gate0 import VertexLikeEndpoint, vertex_call

    recorder = CapacityRecorder(scenario="small")
    recorder.install(monkeypatch)
    caplog.set_level("INFO", logger="xiaowei.app")
    config = capacity_config(env)
    connectors = drivers(config)
    connectors["pre"].tables.update(
        {("data_center", f"table{i:03}"): ("region",) for i in range(199)}
    )
    call = vertex_call(
        "list_tables",
        cluster="pre",
        database="data_center",
        keyword=None,
        page_size=200,
        cursor=None,
    )
    endpoint = VertexLikeEndpoint(
        {question(1, "small"): [parallel(call, call, call, call), finish]}
    )
    async with env.running(
        config, model_transport=recorder.network(endpoint), starrocks_connect=connectors
    ) as served:
        await served.page()
        assert (await recorder.turn(served, 1))["state"] == "failed"
        assert "reason=scope_unverifiable" in caplog.text
        assert 0 < recorder.current.scope_checks <= config.budget.max_scope_checks
        assert all(r["bytes"] < 1000000 for r in recorder.current.requests)
        assert recorder.current.commit_replay_bytes < 400000
        report = recorder.report(config, connectors)
        assert report["io"]["query"] == 0 and report["io"]["probe"] > 0
        assert await env.scalar("SELECT count(*) FROM agent_messages") == 0
        report["first_limit"] = "scope_checks"
        save_report(tmp_path, report)
