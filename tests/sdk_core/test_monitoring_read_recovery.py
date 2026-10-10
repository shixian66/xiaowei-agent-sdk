"""监控只读源部分故障：正式 Web 入口、Runner、治理、Session 与 Evidence。"""

import asyncio
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent
from tests.sdk_core.browser import launch
from tests.sdk_core.mcp_fixture import LOOPBACK, ASGIApp, Recorder, recording, serve
from tests.sdk_core.test_app import ModelCall, answer, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_gateway import GroupChannel, raw_group_event, runtime_group
from tests.sdk_core.test_group_identity import CHAT, A
from tests.sdk_core.test_monitoring_r1a import SOURCE, monitoring_config, query_server
from tests.sdk_core.test_prometheus_mcp_protocol import official_server
from tests.sdk_core.test_runtime import Env, until
from tests.sdk_core.test_runtime import env as env  # pytest fixture
from tests.sdk_core.test_web_browser import send, settled

from xiaowei.feishu import attributed
from xiaowei.mcp import MCPTransportError, _prometheus_query_failure, _read_transport_failure

pytestmark = pytest.mark.loopback

OTHER = "prometheus-other"
OTHER_TOOL = f"{OTHER}/query"


def error_server(recorder: Recorder, detail: str) -> ASGIApp:
    server = MCPServer("prometheus-read-error")

    @server.tool(structured_output=False)
    def query(query: str) -> CallToolResult:
        recorder.tool_calls.append(("query", {"query": query}))
        return CallToolResult(is_error=True, content=[TextContent(type="text", text=detail)])

    return recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)


@contextmanager
def prometheus_error_api() -> Iterator[tuple[int, list[str]]]:
    requests: list[str] = []

    class Backend(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            assert parsed.path == "/api/v1/query"
            expression = parse_qs(parsed.query)["query"][0]
            requests.append(expression)
            status = {"auth401": 401, "auth403": 403, "server500": 500}[expression]
            body = json.dumps(
                {"status": "error", "errorType": "internal", "error": "synthetic failure"}
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    worker = threading.Thread(target=backend.serve_forever, daemon=True)
    worker.start()
    try:
        yield backend.server_port, requests
    finally:
        backend.shutdown()
        worker.join(timeout=5)
        backend.server_close()


def two_sources(first: str, second: str) -> dict[str, Any]:
    values = monitoring_config(first)
    values["mcp_servers"].append({**values["mcp_servers"][0], "server_id": OTHER, "url": second})
    values["data_policy"]["model_tools"].append(OTHER_TOOL)
    values["access"]["grants"]["operator"].append(OTHER_TOOL)
    return values


def cite_last_success(call: ModelCall) -> list[Any]:
    outputs = [
        json.loads(item["output"])
        for item in call.input
        if item.get("type") == "function_call_output"
    ]
    assert outputs[0]["status"] == "unverified"
    assert outputs[0]["retry_this_turn"] is False
    return answer([outputs[-1]["evidence_id"]], analysis="第二个源有指标；第一个源未核实")


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("client_error: client error: 401", "auth"),
        ("client_error: client error: 403", "auth"),
        ("server_error: server error: 500", "upstream_5xx"),
        ("server_error: server error: 599", "upstream_5xx"),
        ("client_error: client error: 404", None),
        ("server_error: server error: 400", None),
        ("bad_data: parse error", None),
        ("server_error: server error: 500 extra", None),
    ],
)
def test_only_locked_prometheus_query_errors_are_source_local(
    detail: str, expected: str | None
) -> None:
    prefix = "failed making query api call: failed to execute instant query: "
    result = CallToolResult(is_error=True, content=[TextContent(type="text", text=prefix + detail)])
    assert _prometheus_query_failure(result) == expected
    assert (
        _prometheus_query_failure(
            result.model_copy(update={"structured_content": {"error": detail}})
        )
        is None
    )


def test_only_typed_transport_failures_are_source_local() -> None:
    assert _read_transport_failure(MCPError(-32000, "Connection closed")) == "unavailable"
    assert _read_transport_failure(MCPError(-32000, "secret")) is None
    assert _read_transport_failure(MCPError(-32000, "Connection closed", data={})) is None
    assert _read_transport_failure(MCPError(-32600, "Session terminated")) == "unavailable"
    assert _read_transport_failure(MCPError(-32600, "secret")) is None
    assert _read_transport_failure(MCPError(-32600, "Session terminated", data={})) is None
    assert _read_transport_failure(MCPError(-32001, "secret")) == "timeout"
    assert _read_transport_failure(MCPError(-32603, "secret")) is None
    assert _read_transport_failure(MCPTransportError("secret")) is None
    assert (
        _read_transport_failure(
            ExceptionGroup("mixed", [MCPError(-32000, "Connection closed"), ValueError("secret")])
        )
        is None
    )
    assert _read_transport_failure(ValueError("secret")) is None


async def test_upstream_401_does_not_block_other_authorized_source_and_is_saved(
    env: Env,
) -> None:
    error = (
        "failed making query api call: failed to execute instant query: "
        "client_error: client error: 401"
    )
    with serve(lambda r: error_server(r, error)) as first, serve(query_server) as second:
        async with env.running(env.config(**two_sources(first.url, second.url))) as served:
            await served.page()
            message = env.scripts.add(
                "查主机为何告警",
                tool_call(f"{SOURCE}__query", query="up"),
                tool_call(f"{OTHER}__query", query="up"),
                cite_last_success,
            )
            response = (await served.turn(message, "query")).json()
            assert response["state"] == "completed"
            delivery = response["delivery"]
            assert [fact["target_id"] for fact in delivery["facts"]] == [OTHER]
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1
            assert SOURCE in delivery["content"] and "未核实" in delivery["content"]
            assert error not in delivery["content"]
            assert first.recorder.tool_calls == [("query", {"query": "up"})]
            assert second.recorder.tool_calls == [("query", {"query": "up"})]
            historical = (await served.client.get("/api/turns/r1")).json()
            assert SOURCE in historical["delivery"]["content"]
            assert "未核实" in historical["delivery"]["content"]
            instructions = env.scripts.calls[message][0].instructions
            assert "只约束 StarRocks 工具" in instructions
            assert "不必先搜 StarRocks 表" in instructions
            assert "即时查询不能冒充历史时间段" in instructions
            assert "不要改查其他 StarRocks 集群代替" in instructions


async def test_group_can_show_partial_failure_and_successful_evidence(env: Env) -> None:
    detail = (
        "failed making query api call: failed to execute instant query: "
        "client_error: client error: 403"
    )
    with serve(lambda r: error_server(r, detail)) as first, serve(query_server) as second:
        config = env.config(
            **two_sources(first.url, second.url),
            feishu=runtime_group(tools=[f"{SOURCE}/query", OTHER_TOOL]),
        )
        channel = GroupChannel()
        env.scripts.add(
            attributed(A, "群调查主机"),
            tool_call(f"{SOURCE}__query", query="up"),
            tool_call(f"{OTHER}__query", query="up"),
            cite_last_success,
        )
        async with env.running(config, feishu_channel=channel):
            channel.emit_raw_from_sdk_thread(raw_group_event(env, "群调查主机", "om_partial"))
            await until(lambda: len(channel.sends) == 1)
            to, payload, _ = channel.sends[0]
            rendered = message_text(payload)
            assert to == CHAT and SOURCE in rendered and "未核实" in rendered
            assert OTHER in rendered and "host1" in rendered
        assert first.recorder.tool_calls == [("query", {"query": "up"})]
        assert second.recorder.tool_calls == [("query", {"query": "up"})]


@pytest.mark.browser
async def test_browser_shows_failed_source_alongside_successful_facts(
    env: Env, chrome_binary: str
) -> None:
    detail = (
        "failed making query api call: failed to execute instant query: "
        "client_error: client error: 401"
    )
    with serve(lambda r: error_server(r, detail)) as first, serve(query_server) as second:
        async with (
            env.running(env.config(**two_sources(first.url, second.url))),
            launch(chrome_binary) as chrome,
        ):
            page = await chrome.page()
            await page.navigate(f"{env.origin}/")
            message = env.scripts.add(
                "浏览器调查告警",
                tool_call(f"{SOURCE}__query", query="up"),
                tool_call(f"{OTHER}__query", query="up"),
                cite_last_success,
            )
            await send(page, message, "query")
            displayed = await settled(page, 0, "completed")
            assert SOURCE in displayed and "未核实" in displayed
            assert "host1" in displayed and OTHER in displayed


async def test_all_monitoring_reads_fail_allows_unverified_advice_with_fixed_sources(
    env: Env,
) -> None:
    error = (
        "failed making query api call: failed to execute instant query: "
        "client_error: client error: 403"
    )
    with (
        serve(lambda r: error_server(r, error)) as first,
        serve(lambda r: error_server(r, error)) as second,
    ):
        async with env.running(env.config(**two_sources(first.url, second.url))) as served:
            await served.page()
            message = env.scripts.add(
                "主机没有指标时怎么排查",
                tool_call(f"{SOURCE}__query", query="up"),
                tool_call(f"{OTHER}__query", query="up"),
                lambda call: answer([], analysis="", advice="检查采集端与网络连通性。"),
            )
            response = (await served.turn(message, "query")).json()
            assert response["state"] == "completed"
            delivery = response["delivery"]
            assert not delivery["facts"] and not delivery["evidence_ids"]
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
            assert "未经数据验证" in delivery["content"]
            assert SOURCE in delivery["content"] and OTHER in delivery["content"]
            assert "本轮未执行业务查询" not in delivery["content"]
            assert first.recorder.tool_calls == [("query", {"query": "up"})]
            assert second.recorder.tool_calls == [("query", {"query": "up"})]


async def test_unknown_remote_error_still_aborts_without_evidence(env: Env) -> None:
    with serve(lambda r: error_server(r, "unexpected remote text")) as upstream:
        async with env.running(env.config(**monitoring_config(upstream.url))) as served:
            await served.page()
            message = env.scripts.add("未知远端错误", tool_call(f"{SOURCE}__query", query="up"))
            response = (await served.turn(message, "query")).json()
            assert response["state"] == "failed"
            assert upstream.recorder.tool_calls == [("query", {"query": "up"})]
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_same_failed_source_cannot_be_called_again_in_the_turn(env: Env) -> None:
    detail = (
        "failed making query api call: failed to execute instant query: "
        "server_error: server error: 500"
    )
    with serve(lambda r: error_server(r, detail)) as first, serve(query_server) as second:
        async with env.running(env.config(**two_sources(first.url, second.url))) as served:
            await served.page()
            message = env.scripts.add(
                "失败后换源",
                tool_call(f"{SOURCE}__query", query="up"),
                tool_call(f"{SOURCE}__query", query="up"),
                tool_call(f"{OTHER}__query", query="up"),
                lambda call: answer(
                    [
                        json.loads(item["output"])["evidence_id"]
                        for item in call.input
                        if item.get("type") == "function_call_output"
                        and "evidence_id" in item["output"]
                    ],
                    analysis="第二源提供了指标",
                ),
            )
            response = (await served.turn(message, "query")).json()
            assert response["state"] == "completed"
            assert first.recorder.tool_calls == [("query", {"query": "up"})]
            assert second.recorder.tool_calls == [("query", {"query": "up"})]
            assert SOURCE in response["delivery"]["monitoring_notice"]


async def test_disconnected_source_can_leave_other_source_available(env: Env) -> None:
    with serve(query_server) as first, serve(query_server) as second:
        async with env.running(env.config(**two_sources(first.url, second.url))) as served:
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
            first.server.should_exit = True
            await until(lambda: first.open_connections() == 0)
            await served.page()
            message = env.scripts.add(
                "第一个源断线后换源",
                tool_call(f"{SOURCE}__query", query="up"),
                tool_call(f"{OTHER}__query", query="up"),
                cite_last_success,
            )
            response = (await served.turn(message, "query")).json()
            assert response["state"] == "completed"
            assert (await served.ready())[f"mcp.{SOURCE}"] == "disconnected"
            assert SOURCE in response["delivery"]["monitoring_notice"]
            assert first.recorder.tool_calls == []
            assert second.recorder.tool_calls == [("query", {"query": "up"})]


async def test_timed_out_read_has_unknown_result_and_is_not_replayed(env: Env) -> None:
    def slow_server(recorder: Recorder) -> ASGIApp:
        server = MCPServer("prometheus-timeout")

        @server.tool(structured_output=False)
        async def query(query: str) -> str:
            recorder.tool_calls.append(("query", {"query": query}))
            await asyncio.sleep(3)
            return json.dumps({"result": "up => 1", "warnings": []})

        return recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)

    with serve(slow_server) as upstream:
        async with env.running(env.config(**monitoring_config(upstream.url))) as served:
            await served.page()
            message = env.scripts.add(
                "超时后的排查建议",
                tool_call(f"{SOURCE}__query", query="up"),
                lambda call: answer([], analysis="", advice="检查采集端。"),
            )
            response = (await served.turn(message, "query")).json()
            assert response["state"] == "completed"
            assert "结果未知" in response["delivery"]["monitoring_notice"]
            assert response["delivery"]["evidence_ids"] == []
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
            assert upstream.recorder.tool_calls == [("query", {"query": "up"})]


async def test_revoked_failed_source_blocks_historical_read(env: Env) -> None:
    detail = (
        "failed making query api call: failed to execute instant query: "
        "client_error: client error: 401"
    )
    with serve(lambda r: error_server(r, detail)) as first, serve(query_server) as second:
        values = two_sources(first.url, second.url)
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "待撤权的失败说明",
                tool_call(f"{SOURCE}__query", query="up"),
                tool_call(f"{OTHER}__query", query="up"),
                cite_last_success,
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            cookies = httpx.Cookies(served.client.cookies)
            assert await served.finish() == 0
        values["access"]["grants"]["operator"].remove(f"{SOURCE}/query")
        async with env.running(env.config(**values)) as served:
            served.client.cookies.update(cookies)
            assert (await served.client.get("/api/turns/r1")).status_code == 403
        assert first.recorder.tool_calls == [("query", {"query": "up"})]
        assert second.recorder.tool_calls == [("query", {"query": "up"})]


@pytest.mark.skipif(
    not os.getenv("XW_TEST_PROMETHEUS_MCP_BIN"), reason="官方 v0.18.0 二进制需显式设置"
)
async def test_official_prometheus_401_403_500_each_call_once_through_web(env: Env) -> None:
    binary = Path(os.environ["XW_TEST_PROMETHEUS_MCP_BIN"])
    with prometheus_error_api() as (port, requests):
        with official_server(binary, port) as url:
            async with env.running(env.config(**monitoring_config(url))) as served:
                await served.page()
                for index, (expression, reason) in enumerate(
                    (("auth401", "认证"), ("auth403", "认证"), ("server500", "服务错误")),
                    start=1,
                ):
                    message = env.scripts.add(
                        f"官方源错误 {expression}",
                        tool_call(f"{SOURCE}__query", query=expression),
                        lambda call: answer([], analysis="", advice="未经数据验证：检查采集链路。"),
                    )
                    before = len(requests)
                    result = (await served.turn(message, "query", f"r{index}")).json()
                    assert result["state"] == "completed"
                    assert len(requests) == before + 1
                    assert requests[-1] == expression
                    assert reason in result["delivery"]["monitoring_notice"]
                    assert result["delivery"]["evidence_ids"] == []
                    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
