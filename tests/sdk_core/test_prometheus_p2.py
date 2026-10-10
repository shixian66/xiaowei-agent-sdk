"""P2 用户路径：正式 serve/Web、真 SDK/存储，底层模型和上游只用合成数据。"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from agents.testing import function_call
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from tests.sdk_core.browser import launch
from tests.sdk_core.mcp_fixture import LOOPBACK, ASGIApp, Recorder, recording, serve
from tests.sdk_core.test_app import ModelCall, answer, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_gateway import GroupChannel, raw_group_event, runtime_group
from tests.sdk_core.test_group_identity import CHAT, A
from tests.sdk_core.test_monitoring_r1a import SOURCE, monitoring_config
from tests.sdk_core.test_prometheus_mcp_protocol import official_server
from tests.sdk_core.test_prometheus_p2_protocol import PREFIXES, RULES, WINDOW, prometheus_backend
from tests.sdk_core.test_prometheus_p2_protocol import official_binary as official_binary
from tests.sdk_core.test_runtime import Env, serve_config, until
from tests.sdk_core.test_runtime import env as env  # pytest fixture
from tests.sdk_core.test_web_browser import send, settled

from xiaowei.evidence import _policy_fingerprint
from xiaowei.feishu import attributed
from xiaowei.mcp import _accepts_input, _promql_error_position
from xiaowei.runtime import ServeConfig, _monitoring_catalog

pytestmark = pytest.mark.loopback

SELECTED = {
    name: f"prometheus.{name}"
    for name in (
        "query",
        "range_query",
        "label_names",
        "label_values",
        "series",
        "metric_metadata",
        "list_rules",
    )
}
RANGE = {"query": "up", **WINDOW, "step": "30s"}


def cite(analysis: str = "仅解释获准证据的覆盖范围") -> Any:
    def finish(call: ModelCall) -> list[Any]:
        ids = []
        for item in call.input:
            if item.get("type") != "function_call_output":
                continue
            try:
                output = json.loads(item["output"])
            except ValueError:
                continue
            if isinstance(output, dict) and "evidence_id" in output:
                ids.append(output["evidence_id"])
        return answer(ids, analysis=analysis)

    return finish


def p2_config(url: str, *, granted: bool = True) -> dict[str, Any]:
    values = monitoring_config(url, granted=granted)
    values["mcp_servers"][0]["allowed_tools"] = SELECTED.copy()
    tools = [f"{SOURCE}/{name}" for name in SELECTED if name != "query"]
    values["data_policy"]["model_tools"].extend(tools)
    values["access"]["grants"]["alice"].extend(tools)
    if granted:
        values["access"]["grants"]["operator"].extend(tools)
    return values


def p2_server(
    recorder: Recorder, *, errors: dict[str, str] | None = None, delay_first_query: bool = False
) -> ASGIApp:
    server = MCPServer("p2-test")

    def output(name: str, arguments: dict[str, Any], value: object) -> str | CallToolResult:
        recorder.tool_calls.append((name, arguments))
        detail = (errors or {}).get(name)
        if str(arguments.get("query", "")).startswith("bad"):
            detail = (
                'bad_data: invalid parameter "query": 1:4: parse error: unexpected end of input'
            )
        if arguments.get("query") == "auth":
            detail = "client_error: client error: 401"
        if detail is not None:
            return CallToolResult(
                is_error=True, content=[TextContent(type="text", text=PREFIXES[name] + detail)]
            )
        return json.dumps(value)

    @server.tool(structured_output=False)
    async def query(query: str) -> str | CallToolResult:
        result = output(
            "query", {"query": query}, {"result": "up => 1 @[1720000000]", "warnings": None}
        )
        if delay_first_query and query == "bad0(":
            # 锁版 SDK 的同连接网络请求串行；让四个 FunctionTool 在首个响应前完成治理预留。
            await asyncio.sleep(0.25)
        return result

    @server.tool(structured_output=False)
    def range_query(query: str, start_time: str, end_time: str, step: str) -> str | CallToolResult:
        return output(
            "range_query",
            dict(query=query, start_time=start_time, end_time=end_time, step=step),
            {"result": "up =>\n1 @[1720000000]\n0 @[1720000060]", "warnings": ["partial data"]},
        )

    @server.tool(structured_output=False)
    def label_names(matches: list[str], start_time: str, end_time: str) -> str | CallToolResult:
        return output(
            "label_names",
            dict(matches=matches, start_time=start_time, end_time=end_time),
            {"result": "__name__\ninstance\njob", "warnings": None},
        )

    @server.tool(structured_output=False)
    def label_values(
        label: str, matches: list[str], start_time: str, end_time: str
    ) -> str | CallToolResult:
        return output(
            "label_values",
            dict(label=label, matches=matches, start_time=start_time, end_time=end_time),
            {"result": "node_cpu_seconds_total\nnode_uname_info", "warnings": None},
        )

    @server.tool(structured_output=False)
    def series(matches: list[str], start_time: str, end_time: str) -> str | CallToolResult:
        return output(
            "series",
            dict(matches=matches, start_time=start_time, end_time=end_time),
            {"result": '{instance="host1", nodename="host1"}', "warnings": None},
        )

    @server.tool(structured_output=False)
    def metric_metadata(metric: str) -> str | CallToolResult:
        return output(
            "metric_metadata",
            {"metric": metric},
            {"up": [{"type": "gauge", "help": "Scrape status", "unit": ""}]},
        )

    @server.tool(structured_output=False)
    def list_rules() -> str | CallToolResult:
        return output("list_rules", {}, RULES)

    return recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)


async def test_formal_range_records_actual_expression_window_and_warnings(env: Env) -> None:
    with serve(p2_server) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()
            message = env.scripts.add(
                "host1 最近的 up 趋势",
                tool_call(f"{SOURCE}__range_query", **RANGE),
                cite("仅覆盖已查询窗口，存在 partial data"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            (fact,) = body["delivery"]["facts"]
            assert fact["target_id"] == SOURCE
            assert fact["metadata"]["query"] == "up"
            assert fact["metadata"]["start_time"] == "1720000000"
            assert fact["metadata"]["end_time"] == "1720000060"
            assert fact["metadata"]["step"] == "30"
            assert "partial data" in body["delivery"]["content"]
            assert source.recorder.tool_calls == [("range_query", {**RANGE, "step": "30000ms"})]
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("19700101", "19700101"),
        ("20240101", "20240101"),
        ("20240101.1236", "20240101.124"),
        ("-86400.1234", "-86400.123"),
        ("2024-07-03T09:46:40Z", "1720000000"),
    ],
)
async def test_unix_seconds_are_not_compact_dates_in_formal_range(
    env: Env, timestamp: str, expected: str
) -> None:
    arguments = {"query": "up", "start_time": timestamp, "end_time": timestamp, "step": "1"}
    with serve(p2_server) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()
            message = env.scripts.add(
                "指定时刻查询", tool_call(f"{SOURCE}__range_query", **arguments), cite()
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            (fact,) = body["delivery"]["facts"]
            assert fact["metadata"]["start_time"] == expected
            assert fact["metadata"]["end_time"] == expected
        assert source.recorder.tool_calls == [
            (
                "range_query",
                {**arguments, "start_time": expected, "end_time": expected, "step": "1000ms"},
            )
        ]


@pytest.mark.parametrize(
    "changes",
    [
        {"step": "0s"},
        {"step": "0.5"},
        {"step": "NaN"},
        {"end_time": "1719999999"},
        {"end_time": "1722678401"},
        {"end_time": "1720011000", "step": "1"},
        {"start_time": "2024-07-03T00:00:00"},
        {"query": "x" * 8193},
    ],
)
async def test_range_limits_reject_before_upstream_io(env: Env, changes: dict[str, str]) -> None:
    def rejected(call: ModelCall) -> list[Any]:
        output = [i["output"] for i in call.input if i.get("type") == "function_call_output"][-1]
        assert "An error occurred" in output
        return answer([], analysis="", advice="参数越界，尚未查询；请调整范围或步长")

    with serve(p2_server) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()
            message = env.scripts.add(
                "查超限范围", tool_call(f"{SOURCE}__range_query", **{**RANGE, **changes}), rejected
            )
            body = (await served.turn(message, "query")).json()
            assert source.recorder.tool_calls == []
            assert body["state"] == "completed"
            assert body["delivery"]["facts"] == []
            assert source.recorder.tool_calls == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


def test_remote_nullable_array_accepts_only_nonnullable_local_subset() -> None:
    def schema(value: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "properties": {"matches": value}, "required": ["matches"]}

    local = schema({"type": "array", "items": {"type": "string"}})
    remote = schema({"type": ["null", "array"], "items": {"type": "string"}})
    assert _accepts_input(local, remote)
    assert not _accepts_input(remote, local)
    for value in (
        {"type": ["null", "array"], "items": {"type": "integer"}},
        {"type": ["null", "array"], "items": {"type": "string"}, "minItems": 2},
        {"type": ["null", "array", "string"], "items": {"type": "string"}},
    ):
        assert not _accepts_input(local, schema(value))


def test_query_policy_preserves_merged_r1_evidence_fingerprint() -> None:
    config = ServeConfig.model_validate(
        serve_config(8501, **p2_config("http://127.0.0.1:9000/mcp"))
    )
    contracts, policies = _monitoring_catalog(config)
    contract = next(c for c in contracts if c.policy_id == "prometheus.query")
    policy = next(p for p in policies if p.policy_id == contract.policy_id)
    # 从 4c5b894 的独立源码副本取得；不是用本轮策略生成期望值。
    assert _policy_fingerprint(contract, policy) == (
        "sha256:5fa5b30c049a57788d39aefac21491b85c74f2907904e66284dc8b77efe73a7a"
    )


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("query", {"query": "up"}, "host1"),
        ("range_query", RANGE, "1720000030"),
        ("label_names", {"matches": [], **WINDOW}, "instance"),
        ("label_values", {"label": "__name__", "matches": [], **WINDOW}, "node_cpu"),
        ("series", {"matches": ["node_uname_info"], **WINDOW}, "nodename"),
        ("metric_metadata", {"metric": ""}, "Target scrape status"),
        ("list_rules", {}, "HostCPUHigh"),
    ],
)
async def test_official_seven_tools_reach_formal_web_and_evidence(
    env: Env, official_binary: Path, name: str, arguments: dict[str, Any], expected: str
) -> None:
    with prometheus_backend() as (backend, state):
        with official_server(official_binary, backend.server_port, tools="list_rules") as url:
            async with env.running(env.config(**p2_config(url))) as served:
                await served.page()
                message = env.scripts.add(
                    "官方只读工具 " + name,
                    tool_call(f"{SOURCE}__{name}", **arguments),
                    cite("只解释当前工具提供的事实，无法判断整体健康"),
                )
                body = (await served.turn(message, "query")).json()
                assert body["state"] == "completed"
                assert len(state.requests) == 1
                assert expected in body["delivery"]["content"]
                assert body["delivery"]["facts"][0]["tool_id"] == f"{SOURCE}/{name}"
                if name in ("metric_metadata", "list_rules"):
                    assert "不返回" in body["delivery"]["facts"][0]["note"] or (
                        "不传回" in body["delivery"]["facts"][0]["note"]
                    )
                    assert "warnings" in body["delivery"]["content"]
                if name == "metric_metadata":
                    assert state.requests[0][1] == {}  # metric 空串，limit 省略，默认不限制。
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1


async def test_official_rules_duration_survives_all_projections_and_replay(
    env: Env, official_binary: Path
) -> None:
    def check_duration(data: dict[str, Any]) -> None:
        alerting, recording_rule = data["groups"][0]["rules"]
        assert alerting["duration"] == 60
        assert recording_rule["duration"] is None

    with prometheus_backend() as (backend, state):
        with official_server(official_binary, backend.server_port, tools="list_rules") as url:
            async with env.running(env.config(**p2_config(url))) as served:
                await served.page()
                message = env.scripts.add(
                    "CPU 告警须持续多久", tool_call(f"{SOURCE}__list_rules"), cite()
                )
                body = (await served.turn(message, "query")).json()
                assert body["state"] == "completed"
                check_duration(last_output(env.scripts.calls[message][-1])["data"])
                check_duration(json.loads(body["delivery"]["facts"][0]["result_json"]))
                for sql in (
                    "SELECT model_content FROM xiaowei_evidence",
                    "SELECT session_content FROM xiaowei_evidence",
                    "SELECT web_content FROM xiaowei_evidence",
                    "SELECT feishu_content FROM xiaowei_evidence",
                ):
                    envelope = json.loads(await env.scalar(sql))
                    check_duration(envelope["data"])
                followup = env.scripts.add("解释刚才的持续条件，不再查询", cite())
                assert (await served.turn(followup, "query", request_id="r2")).json()["state"] == (
                    "completed"
                )
                check_duration(last_output(env.scripts.calls[followup][0])["data"])
            assert len(state.requests) == 1


@pytest.mark.browser
async def test_browser_range_success_and_resource_refusal(env: Env, chrome_binary: str) -> None:
    with serve(p2_server) as source:
        async with (
            env.running(env.config(**p2_config(source.url))),
            launch(chrome_binary) as chrome,
        ):
            page = await chrome.page()
            await page.navigate(f"{env.origin}/")
            success = env.scripts.add(
                "浏览器看 host1 趋势", tool_call(f"{SOURCE}__range_query", **RANGE), cite()
            )
            await send(page, success, "query")
            shown = await settled(page, 0, "completed")
            assert SOURCE in shown and "1720000000" in shown and "partial data" in shown
            rejected = env.scripts.add(
                "浏览器拒绝超限查询",
                tool_call(f"{SOURCE}__range_query", **{**RANGE, "step": "0s"}),
                lambda call: answer([], analysis="", advice="步长无效，未执行；请调大步长"),
            )
            await send(page, rejected, "query")
            shown = await settled(page, 1, "completed")
            assert "步长无效" in shown
        assert source.recorder.tool_calls == [("range_query", {**RANGE, "step": "30000ms"})]


def last_output(call: ModelCall) -> dict[str, Any]:
    return json.loads(
        [i["output"] for i in call.input if i.get("type") == "function_call_output"][-1]
    )


@pytest.mark.parametrize("name", ["query", "range_query"])
async def test_bad_promql_can_be_corrected_then_only_success_produces_evidence(
    env: Env, name: str
) -> None:
    arguments = {"query": "bad("} if name == "query" else {**RANGE, "query": "bad("}

    def correct(call: ModelCall) -> list[Any]:
        failure = last_output(call)
        assert failure["status"] == "invalid_expression"
        assert failure["correction_allowed"] is True
        assert (failure["line"], failure["column"]) == (1, 4)
        assert "evidence_id" not in failure
        return tool_call(f"{SOURCE}__{name}", **{**arguments, "query": "up"})(call)

    with serve(p2_server) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()
            message = env.scripts.add(
                "修正 PromQL", tool_call(f"{SOURCE}__{name}", **arguments), correct, cite()
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            assert len(body["delivery"]["facts"]) == 1
            assert body["delivery"]["facts"][0]["metadata"]["query"] == "up"
        assert [a["query"] for _, a in source.recorder.tool_calls] == ["bad(", "up"]
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1


@pytest.mark.parametrize(
    "error",
    [
        'bad_data: invalid parameter "step": must be positive',
        'bad_data: invalid parameter "query": non syntax error',
        "execution: query processing would load too many samples",
        "bad_data: parse error",
    ],
)
async def test_uncertain_expression_or_resource_errors_abort(env: Env, error: str) -> None:
    with serve(lambda r: p2_server(r, errors={"range_query": error})) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()
            message = env.scripts.add(
                "未知表达式失败", tool_call(f"{SOURCE}__range_query", **RANGE)
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "failed"
        assert len(source.recorder.tool_calls) == 1
        assert len(env.scripts.calls[message]) == 1
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_failed_expression_is_not_replayed_across_query_tools(env: Env) -> None:
    with serve(p2_server) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()
            message = env.scripts.add(
                "同个失败表达式不要再发",
                tool_call(f"{SOURCE}__query", query="bad("),
                tool_call(f"{SOURCE}__range_query", **{**RANGE, "query": "bad("}),
                tool_call(f"{SOURCE}__query", query="up"),
                cite(),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
        assert [a["query"] for _, a in source.recorder.tool_calls] == ["bad(", "up"]


async def test_parallel_initial_errors_and_two_repair_slots_have_exact_io_counts(env: Env) -> None:
    def parallel(expressions: list[str]) -> Any:
        return lambda call: [
            function_call(f"{SOURCE}__query", {"query": q}, call_id=f"p2-{q}") for q in expressions
        ]

    initial = [f"bad{i}(" for i in range(4)]
    repairs = [f"bad{i}(" for i in range(4, 8)]
    with serve(lambda r: p2_server(r, delay_first_query=True)) as source:
        values = p2_config(source.url)
        values["budget"] = {
            "max_turns": 6,
            "max_tool_calls": 20,
            "timeout_seconds": 30,
            "max_scope_checks": 1000,
        }
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "并行修正不要超额",
                parallel(initial),
                parallel(repairs),
                lambda call: answer([], analysis="", advice="未经数据验证，表达式仍需检查"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
        queries = [a["query"] for _, a in source.recorder.tool_calls]
        assert len(queries) == 6
        assert set(initial) <= set(queries)
        assert len(set(repairs) & set(queries)) == 2
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


@pytest.mark.parametrize("name", ["range_query", "label_names", "metric_metadata", "list_rules"])
async def test_new_read_tools_have_source_local_auth_failure(env: Env, name: str) -> None:
    arguments = {
        "range_query": RANGE,
        "label_names": {"matches": [], **WINDOW},
        "metric_metadata": {"metric": "up"},
        "list_rules": {},
    }[name]
    with serve(lambda r: p2_server(r, errors={name: "client_error: client error: 403"})) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()
            message = env.scripts.add(
                "这个源读失败 " + name,
                tool_call(f"{SOURCE}__{name}", **arguments),
                lambda call: answer([], analysis="", advice="未经数据验证，检查源认证配置"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            assert SOURCE in body["delivery"]["monitoring_notice"]
        assert len(source.recorder.tool_calls) == 1
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_all_reads_failed_at_turn_limit_get_tool_free_unverified_advice(env: Env) -> None:
    def finish(call: ModelCall) -> list[Any]:
        assert call.tools == []
        return answer([], analysis="", advice="未经数据验证，检查采集与源认证")

    with serve(p2_server) as source:
        values = p2_config(source.url)
        values["budget"] = {
            "max_turns": 1,
            "max_tool_calls": 3,
            "timeout_seconds": 30,
            "max_scope_checks": 1000,
        }
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "步数用完也要收尾", tool_call(f"{SOURCE}__query", query="auth"), finish
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            assert "不完整" in body["delivery"]["content"]
            assert SOURCE in body["delivery"]["monitoring_notice"]
            assert not body["delivery"]["facts"]
        assert source.recorder.tool_calls == [("query", {"query": "auth"})]


async def test_same_source_parallel_success_and_failure_are_not_contradictory(env: Env) -> None:
    with serve(p2_server) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()
            message = env.scripts.add(
                "部分成功部分失败",
                lambda call: [
                    function_call(f"{SOURCE}__query", {"query": q}, call_id=q)
                    for q in ("up", "auth")
                ],
                cite(),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            assert len(body["delivery"]["facts"]) == 1
            assert "部分读取" in body["delivery"]["monitoring_notice"]
            assert "这些源" not in body["delivery"]["monitoring_notice"]
            instructions = env.scripts.calls[message][-1].instructions
            assert "该源本轮未取得事实" not in instructions
            assert "该次调用未取得事实" in instructions
            assert "已有成功查询仅证明各自覆盖范围" in instructions


async def test_commit_rechecks_failure_source_authorization(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from xiaowei.evidence import EvidenceStore
    from xiaowei.runtime import StaticAccess

    original = StaticAccess.authorize
    validate = EvidenceStore.validate_answer
    revoked = False

    async def revoke_after_final(self: Any, identity: Any, target_id: str, tool_id: str) -> bool:
        if target_id == SOURCE and revoked:
            return False
        return await original(self, identity, target_id, tool_id)

    async def validated_then_revoked(self: Any, turn: Any, ctx: Any, **kw: Any) -> Any:
        nonlocal revoked
        result = await validate(self, turn, ctx, **kw)
        if turn.monitoring_failures:
            revoked = True
        return result

    monkeypatch.setattr(StaticAccess, "authorize", revoke_after_final)
    monkeypatch.setattr(EvidenceStore, "validate_answer", validated_then_revoked)
    with serve(p2_server) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()
            message = env.scripts.add(
                "提交前撤权",
                tool_call(f"{SOURCE}__query", query="auth"),
                lambda call: answer([], analysis="", advice="未经数据验证，源认证待核实"),
            )
            body = (await served.turn(message, "query")).json()
            assert await env.scalar("SELECT count(*) FROM agent_messages") == 0
            assert body["state"] == "failed"
        assert source.recorder.tool_calls == [("query", {"query": "auth"})]
        assert await env.scalar("SELECT count(*) FROM agent_messages") == 0


async def test_relative_window_is_frozen_once_and_history_uses_original_arguments(env: Env) -> None:
    env.clock.now = datetime(2026, 10, 10, 5, 0, 0, 123600, tzinfo=UTC)
    arguments = {"query": "up", "start_time": "-15m", "end_time": "now", "step": "1.25"}
    expected = {
        "query": "up",
        "start_time": "1791607500.124",
        "end_time": "1791608400.124",
        "step": "1250ms",
    }
    with serve(p2_server) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()
            first = env.scripts.add(
                "相对时间查趋势", tool_call(f"{SOURCE}__range_query", **arguments), cite()
            )
            body = (await served.turn(first, "query")).json()
            assert body["state"] == "completed"
            assert body["delivery"]["facts"][0]["metadata"]["step"] == "1.25"
            assert source.recorder.tool_calls == [("range_query", expected)]
            env.clock.advance(600)

            def explain(call: ModelCall) -> list[Any]:
                past = [i for i in call.input if i.get("type") == "function_call"]
                assert json.loads(past[0]["arguments"]) == arguments
                return cite("历史范围是旧快照，并非当前数据")(call)

            followup = env.scripts.add("解释刚才的趋势，不再查询", explain)
            assert (await served.turn(followup, "query", request_id="r2")).json()["state"] == (
                "completed"
            )
            history = (await served.client.get("/api/turns/r1")).json()
            assert history["delivery"]["facts"][0]["metadata"]["end_time"] == expected["end_time"]
        assert source.recorder.tool_calls == [("range_query", expected)]


async def test_exact_point_limit_succeeds_and_submillisecond_step_has_zero_io(env: Env) -> None:
    with serve(p2_server) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()
            arguments = {**RANGE, "end_time": "1720010999", "step": "1"}
            message = env.scripts.add(
                "正好11000点", tool_call(f"{SOURCE}__range_query", **arguments), cite()
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            refused = env.scripts.add(
                "不支持更细步长",
                tool_call(f"{SOURCE}__range_query", **{**RANGE, "step": "1.0001"}),
                lambda call: answer([], analysis="", advice="步长须为整毫秒，尚未执行本次查询"),
            )
            assert (await served.turn(refused, "query", request_id="r2")).json()["state"] == (
                "completed"
            )
        assert source.recorder.tool_calls == [("range_query", {**arguments, "step": "1000ms"})]


@pytest.mark.parametrize("name", ["query", "range_query"])
async def test_official_parse_failure_then_repair_reaches_real_governance(
    env: Env, official_binary: Path, name: str
) -> None:
    arguments = {"query": "bad("} if name == "query" else {**RANGE, "query": "bad("}
    with prometheus_backend() as (backend, state):
        state.query_errors["bad("] = (
            400,
            "bad_data",
            'invalid parameter "query": 1:4: parse error: unexpected end of input',
        )
        with official_server(official_binary, backend.server_port, tools="list_rules") as url:
            async with env.running(env.config(**p2_config(url))) as served:
                await served.page()

                def repair(call: ModelCall) -> list[Any]:
                    result = last_output(call)
                    assert result["status"] == "invalid_expression"
                    assert result["correction_allowed"] is True
                    assert len(state.requests) == 1
                    return tool_call(f"{SOURCE}__{name}", **{**arguments, "query": "up"})(call)

                message = env.scripts.add(
                    "官方解析错误修正", tool_call(f"{SOURCE}__{name}", **arguments), repair, cite()
                )
                body = (await served.turn(message, "query")).json()
                assert body["state"] == "completed"
                assert [r[1]["query"][0] for r in state.requests] == ["bad(", "up"]
                assert len(body["delivery"]["facts"]) == 1
                assert "表达式解析失败" in body["delivery"]["monitoring_notice"]
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1


@pytest.mark.parametrize("name", ["query", "range_query", "label_names", "metric_metadata"])
async def test_official_truncated_results_are_marked_through_all_projections(
    env: Env, official_binary: Path, name: str
) -> None:
    arguments = {
        "query": {"query": "up"},
        "range_query": RANGE,
        "label_names": {"matches": [], **WINDOW},
        "metric_metadata": {"metric": ""},
    }[name]
    with prometheus_backend() as (backend, state):
        with official_server(
            official_binary, backend.server_port, tools="list_rules", truncation_limit=1
        ) as url:
            async with env.running(env.config(**p2_config(url))) as served:
                await served.page()

                def limited(call: ModelCall) -> list[Any]:
                    assert last_output(call)["truncated"] is True
                    return cite("结果被截断，不足以判断整体健康")(call)

                message = env.scripts.add(
                    "截断保持可见", tool_call(f"{SOURCE}__{name}", **arguments), limited
                )
                body = (await served.turn(message, "query")).json()
                assert body["state"] == "completed"
                assert body["delivery"]["facts"][0]["truncated"] is True
                history = (await served.client.get("/api/turns/r1")).json()
                assert history["delivery"]["facts"][0]["truncated"] is True
                assert len(state.requests) == 1


def test_expression_error_requires_complete_single_text_locked_shape() -> None:
    text = PREFIXES["query"] + (
        'bad_data: invalid parameter "query": 1:4: parse error: unexpected end of input'
    )
    result = CallToolResult(is_error=True, content=[TextContent(type="text", text=text)])
    assert _promql_error_position(result, "query") == (1, 4)
    for changed in (
        {"structured_content": {"message": text}},
        {"content": [*result.content, TextContent(type="text", text="another")]},
        {"is_error": False},
        {"content": [TextContent(type="text", text=text + "\nadditional text")]},
    ):
        assert _promql_error_position(result.model_copy(update=changed), "query") is None
    assert _promql_error_position(result, "range_query") is None


async def test_successful_repair_does_not_reset_quota_and_new_turn_resets_it(env: Env) -> None:
    def last_failed(call: ModelCall) -> list[Any]:
        assert last_output(call)["correction_allowed"] is False
        return tool_call(f"{SOURCE}__query", query="up2")(call)

    with serve(p2_server) as source:
        values = p2_config(source.url)
        values["budget"] = {
            "max_turns": 12,
            "max_tool_calls": 10,
            "timeout_seconds": 30,
            "max_scope_checks": 1000,
        }
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "两次修正成功也不重置额度",
                tool_call(f"{SOURCE}__query", query="bad0("),
                tool_call(f"{SOURCE}__query", query="up0"),
                tool_call(f"{SOURCE}__query", query="bad1("),
                tool_call(f"{SOURCE}__query", query="up1"),
                tool_call(f"{SOURCE}__query", query="bad2("),
                last_failed,
                cite(),
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            assert [a["query"] for _, a in source.recorder.tool_calls] == [
                "bad0(",
                "up0",
                "bad1(",
                "up1",
                "bad2(",
            ]
            next_turn = env.scripts.add(
                "新轮次有新额度",
                tool_call(f"{SOURCE}__query", query="bad0("),
                tool_call(f"{SOURCE}__query", query="up2"),
                cite(),
            )
            assert (await served.turn(next_turn, "query", request_id="r2")).json()["state"] == (
                "completed"
            )
        assert [a["query"] for _, a in source.recorder.tool_calls][-2:] == ["bad0(", "up2"]


async def test_old_query_history_remains_readable_after_p2_configuration(env: Env) -> None:
    with serve(p2_server) as source:
        async with env.running(env.config(**monitoring_config(source.url))) as served:
            await served.page()
            message = env.scripts.add(
                "旧query证据", tool_call(f"{SOURCE}__query", query="up"), cite()
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            cookies = httpx.Cookies(served.client.cookies)
            assert await served.finish() == 0
        async with env.running(env.config(**p2_config(source.url))) as served:
            served.client.cookies.update(cookies)
            response = await served.client.get("/api/turns/r1")
            assert response.status_code == 200
            assert response.json()["delivery"]["facts"][0]["metadata"]["query"] == "up"
        assert source.recorder.tool_calls == [("query", {"query": "up"})]


@pytest.mark.parametrize("granted", [True, False])
async def test_group_range_evidence_and_ungranted_tool_visibility(env: Env, granted: bool) -> None:
    tool = f"{SOURCE}/range_query"
    with serve(p2_server) as source:
        config = env.config(
            **p2_config(source.url), feishu=runtime_group(**({"tools": [tool]} if granted else {}))
        )
        channel = GroupChannel()
        steps = (
            [tool_call(f"{SOURCE}__range_query", **RANGE), cite()]
            if granted
            else [lambda call: answer([], analysis="", advice="本群没有监控源权限")]
        )
        message = env.scripts.add(attributed(A, "群查主机趋势"), *steps)
        async with env.running(config, feishu_channel=channel):
            channel.emit_raw_from_sdk_thread(raw_group_event(env, "群查主机趋势", "om_p2_range"))
            await until(lambda: len(channel.sends) == 1)
            to, payload, _ = channel.sends[0]
            text = message_text(payload)
            assert to == CHAT
            if granted:
                assert SOURCE in text and "partial data" in text and "1720000000" in text
            else:
                assert f"{SOURCE}__range_query" not in env.scripts.tools_seen(message)[0]
        assert len(source.recorder.tool_calls) == int(granted)


@pytest.mark.parametrize("granted", [True, False])
async def test_web_scope_hides_ungranted_discovery_and_rejects_forged_call(
    env: Env, granted: bool
) -> None:
    with serve(p2_server) as source:
        values = p2_config(source.url, granted=granted)
        if granted:
            # 同一源只给 range 权限，其他工具不能因源可读而自动获得权限。
            values["access"]["grants"]["operator"] = [f"{SOURCE}/range_query"]
        async with env.running(env.config(**values)) as served:
            await served.page()

            message = env.scripts.add("无权读取规则", tool_call(f"{SOURCE}__list_rules"))
            assert (await served.turn(message, "query")).json()["state"] == "failed"
            visible = env.scripts.tools_seen(message)[0]
            assert f"{SOURCE}__list_rules" not in visible
            assert (f"{SOURCE}__range_query" in visible) is granted
            assert len(env.scripts.calls[message]) == 1
        assert source.recorder.tool_calls == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_one_new_tool_schema_drift_does_not_hide_verified_query(env: Env) -> None:
    def drifted(recorder: Recorder) -> ASGIApp:
        server = MCPServer("p2-partial-drift")

        @server.tool(structured_output=False)
        def query(query: str) -> str:
            recorder.tool_calls.append(("query", {"query": query}))
            return json.dumps({"result": "up => 1 @[1720000000]", "warnings": None})

        @server.tool(structured_output=False)
        def range_query(query: str, start_time: str, end_time: str, step: int) -> str:
            recorder.tool_calls.append(("range_query", {"query": query}))
            return json.dumps({"result": "should never execute", "warnings": None})

        return recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)

    with serve(drifted) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()

            def selected(call: ModelCall) -> list[Any]:
                visible = set(call.tools)
                assert f"{SOURCE}__query" in visible
                assert f"{SOURCE}__range_query" not in visible
                return tool_call(f"{SOURCE}__query", query="up")(call)

            message = env.scripts.add("单工具漂移", selected, cite())
            assert (await served.turn(message, "query")).json()["state"] == "completed"
        assert source.recorder.tool_calls == [("query", {"query": "up"})]


@pytest.mark.parametrize("tail", ["\n\nWarning: The result was truncated", "\nextra"])
async def test_unrecognized_metadata_suffix_aborts_without_evidence(env: Env, tail: str) -> None:
    def malformed(recorder: Recorder) -> ASGIApp:
        server = MCPServer("p2-malformed-metadata")

        @server.tool(structured_output=False)
        def metric_metadata(metric: str) -> str:
            recorder.tool_calls.append(("metric_metadata", {"metric": metric}))
            return json.dumps({"up": [{"type": "gauge", "help": "status", "unit": ""}]}) + tail

        return recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)

    with serve(malformed) as source:
        async with env.running(env.config(**p2_config(source.url))) as served:
            await served.page()
            message = env.scripts.add(
                "拒绝不完整截断标记", tool_call(f"{SOURCE}__metric_metadata", metric="up")
            )
            assert (await served.turn(message, "query")).json()["state"] == "failed"
        assert len(source.recorder.tool_calls) == 1
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_official_500_stays_source_local_and_does_not_enable_repair(
    env: Env, official_binary: Path
) -> None:
    with prometheus_backend() as (backend, state):
        state.status, state.error_type = 500, "internal"
        with official_server(official_binary, backend.server_port, tools="list_rules") as url:
            async with env.running(env.config(**p2_config(url))) as served:
                await served.page()

                def finish(call: ModelCall) -> list[Any]:
                    output = last_output(call)
                    assert output["status"] == "unverified"
                    assert output["retry_this_turn"] is False
                    assert "correction_allowed" not in output
                    return answer([], analysis="", advice="未经数据验证，请检查服务状态")

                message = env.scripts.add(
                    "服务故障不能当语法错误", tool_call(f"{SOURCE}__range_query", **RANGE), finish
                )
                assert (await served.turn(message, "query")).json()["state"] == "completed"
                assert len(state.requests) == 1
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
