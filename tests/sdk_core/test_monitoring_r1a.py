"""R1a：正式运行入口中的 Prometheus 只读源。"""

import asyncio
import json
import logging
import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from lark_channel.channel.errors import FeishuChannelErrorCode, SendError
from lark_channel.channel.types import SendResult
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError
from tests.sdk_core.browser import launch
from tests.sdk_core.mcp_fixture import LOOPBACK, ASGIApp, Recorder, recording, serve
from tests.sdk_core.test_app import answer, cite, tool_call
from tests.sdk_core.test_feishu import FakeChannel
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_gateway import GroupChannel, raw_group_event, runtime_group
from tests.sdk_core.test_group_identity import CHAT, A
from tests.sdk_core.test_prometheus_mcp_protocol import official_server
from tests.sdk_core.test_runtime import (
    SR_ENV,
    Env,
    feishu_config,
    feishu_event,
    free_port,
    serve_config,
    until,
)
from tests.sdk_core.test_runtime import env as env  # pytest fixture
from tests.sdk_core.test_web_browser import send, settled

from xiaowei import runtime
from xiaowei.channel import ResultUnavailableError
from xiaowei.feishu import attributed
from xiaowei.models import AUDIENCES
from xiaowei.runtime import ServeConfig
from xiaowei.starrocks_tools import QUERY_TOOLS

pytestmark = pytest.mark.loopback

SOURCE = "prometheus-prod"
TOOL = f"{SOURCE}/query"


def monitoring_config(url: str, *, granted: bool = True) -> dict[str, Any]:
    tools = sorted(QUERY_TOOLS | {TOOL})
    return {
        "mcp_servers": [
            {
                "server_id": SOURCE,
                "url": url,
                "auth_ref": None,
                "timeout_seconds": 2,
                "max_response_bytes": 64_000,
                "allowed_tools": {"query": "prometheus.query"},
            }
        ],
        "data_policy": {"input": {"max_bytes": 2000}, "model_tools": tools},
        "access": {
            "policy_version": "monitoring-1",
            "grants": {"operator": tools if granted else sorted(QUERY_TOOLS), "alice": tools},
        },
        "projection_bytes": dict.fromkeys(AUDIENCES, 60_000),
    }


def query_server(recorder: Recorder) -> ASGIApp:
    server = MCPServer("prometheus-r1a-fixture")

    @server.tool(structured_output=False)
    def query(query: str) -> str:
        recorder.tool_calls.append(("query", {"query": query}))
        return json.dumps(
            {
                "result": 'up{instance="host1"} => 1 @[1720000000]',
                "warnings": ["partial"],
                "query": "forged remote expression",
            }
        )

    return recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)


def drifted_server(recorder: Recorder) -> ASGIApp:
    server = MCPServer("prometheus-r1a-drifted")

    @server.tool(structured_output=False)
    def query(query: int) -> str:
        recorder.tool_calls.append(("query", {"query": query}))
        return json.dumps({"result": "wrong", "warnings": None})

    return recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)


def failing_server(recorder: Recorder) -> ASGIApp:
    server = MCPServer("prometheus-r1a-error")

    @server.tool(structured_output=False)
    def query(query: str) -> CallToolResult:
        recorder.tool_calls.append(("query", {"query": query}))
        return CallToolResult(
            is_error=True,
            content=[TextContent(type="text", text='{"result":"false success","warnings":[]}')],
        )

    return recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)


@contextmanager
def prometheus_api() -> Iterator[tuple[int, list[tuple[str, str | None]]]]:
    requests: list[tuple[str, str | None]] = []

    class Backend(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            from urllib.parse import parse_qs, urlsplit

            requests.append(
                (parse_qs(urlsplit(self.path).query)["query"][0], self.headers.get("Authorization"))
            )
            body = json.dumps(
                {
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [
                            {
                                "metric": {"__name__": "up", "instance": "host1"},
                                "value": [1720000000, "1"],
                            }
                        ],
                    },
                }
            ).encode()
            self.send_response(200)
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


def test_trusted_http_monitoring_source_is_opt_in_and_old_config_still_loads() -> None:
    old = ServeConfig.model_validate(serve_config(8501))
    assert old.mcp_servers == ()
    configured = ServeConfig.model_validate(
        serve_config(8501, **monitoring_config("http://10.10.0.12:8080/mcp"))
    )
    assert configured.mcp_servers[0].server_id == SOURCE
    for url in (
        "http://user:pass@10.10.0.12/mcp",
        "http://10.10.0.12/mcp?target=other",
        "http://10.10.0.12/mcp#other",
    ):
        with pytest.raises(ValidationError):
            ServeConfig.model_validate(serve_config(8501, **monitoring_config(url)))


def test_only_selected_read_tools_and_distinct_source_ids_can_be_configured() -> None:
    values = monitoring_config("http://10.10.0.12:8080/mcp")
    values["mcp_servers"][0]["allowed_tools"]["range_query"] = "prometheus.range_query"
    config = ServeConfig.model_validate(serve_config(8501, **values))
    assert config.mcp_servers[0].allowed_tools == {
        "query": "prometheus.query",
        "range_query": "prometheus.range_query",
    }
    for tool, policy in (
        ("query", "prometheus.range_query"),
        ("delete_series", "prometheus.delete_series"),
    ):
        values = monitoring_config("http://10.10.0.12:8080/mcp")
        values["mcp_servers"][0]["allowed_tools"][tool] = policy
        with pytest.raises(ValidationError, match="只允许已核约的 Prometheus 只读工具及其准确映射"):
            ServeConfig.model_validate(serve_config(8501, **values))

    values = monitoring_config("http://10.10.0.12:8080/mcp")
    values["mcp_servers"].append(values["mcp_servers"][0].copy())
    with pytest.raises(ValidationError, match="server_id 不能重复"):
        ServeConfig.model_validate(serve_config(8501, **values))

    values = monitoring_config("http://10.10.0.12:8080/mcp")
    values["mcp_servers"][0]["server_id"] = "sr-test"
    with pytest.raises(ValidationError, match="不能与 StarRocks 目标 ID 重复"):
        ServeConfig.model_validate(serve_config(8501, **values))


def test_config_check_requires_a_selected_monitoring_auth_reference(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = monitoring_config("http://127.0.0.1:9191/mcp")
    values["mcp_servers"][0]["auth_ref"] = "env:XW_R1A_MISSING_AUTH"
    monkeypatch.setenv(SR_ENV, "synthetic-readonly-password")
    monkeypatch.delenv("XW_R1A_MISSING_AUTH", raising=False)
    with pytest.raises(runtime.ConfigError, match=r"mcp_servers\.0\.auth_ref"):
        runtime.validate_config(env.config(**values))
    monkeypatch.setenv("XW_R1A_MISSING_AUTH", "synthetic-readonly-credential")
    runtime.validate_config(env.config(**values))


async def test_formal_web_query_projects_expression_source_time_and_warnings(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve(query_server) as upstream:
        config = env.config(**monitoring_config(upstream.url))
        monkeypatch.setenv(SR_ENV, "synthetic-readonly-password")
        runtime.validate_config(config)
        async with env.running(config) as served:
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
            await served.page()
            message = env.scripts.add(
                "查询 up 指标", tool_call(f"{SOURCE}__query", query="up"), cite("指标已返回")
            )
            response = await served.turn(message, "query")
            assert response.status_code == 200
            body = response.json()
            assert body["state"] == "completed"
            (fact,) = [f for f in body["delivery"]["facts"] if f["tool_id"] == TOOL]
            assert fact["target_id"] == SOURCE
            assert fact["captured_at"]
            assert fact["metadata"]["query"] == "up"
            assert fact["metadata"]["warnings"] == '["partial"]'
            assert "host1" in body["delivery"]["content"]
            assert "采集于" in body["delivery"]["content"]
            assert SOURCE in body["delivery"]["content"]
            assert "forged remote expression" not in body["delivery"]["content"]
            assert upstream.recorder.tool_calls == [("query", {"query": "up"})]


@pytest.mark.browser
async def test_formal_web_ui_shows_monitoring_evidence(env: Env, chrome_binary: str) -> None:
    with serve(query_server) as upstream:
        config = env.config(**monitoring_config(upstream.url))
        async with env.running(config), launch(chrome_binary) as chrome:
            page = await chrome.page()
            await page.navigate(f"{env.origin}/")
            message = env.scripts.add(
                "浏览器查 up", tool_call(f"{SOURCE}__query", query="up"), cite()
            )
            await send(page, message, "query")
            displayed = await settled(page, 0, "completed")
            assert SOURCE in displayed and "采集于" in displayed
            assert "up" in displayed and "host1" in displayed and "partial" in displayed
        assert upstream.recorder.tool_calls == [("query", {"query": "up"})]


async def test_ungranted_query_is_hidden_and_has_zero_query_io(env: Env) -> None:
    with serve(query_server) as upstream:
        config = env.config(**monitoring_config(upstream.url, granted=False))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "只看可用工具",
                lambda call: answer([], analysis="", advice="本轮没有可用监控源"),
            )
            response = await served.turn(message, "query")
            assert response.json()["state"] == "completed"
            assert all(f"{SOURCE}__query" not in seen for seen in env.scripts.tools_seen(message))
            assert upstream.recorder.tool_calls == []


async def test_feishu_query_projects_source_and_expression(env: Env) -> None:
    with serve(query_server) as upstream:
        config = env.config(**monitoring_config(upstream.url), feishu=feishu_config())
        channel = FakeChannel()
        message = env.scripts.add("飞书查询指标", tool_call(f"{SOURCE}__query", query="up"), cite())
        async with env.running(config, feishu_channel=channel) as served:
            assert (await served.ready())["feishu"] == "connected"
            channel.emit_raw_from_sdk_thread(feishu_event(env, message, "om_monitor_query"))
            await until(lambda: len(channel.sends) == 1)
            rendered = message_text(channel.sends[0][1])
            assert SOURCE in rendered and "up" in rendered and "host1" in rendered
            assert "采集于" in rendered and "partial" in rendered
        assert upstream.recorder.tool_calls == [("query", {"query": "up"})]


async def test_feishu_ungranted_query_is_hidden_without_query_io(env: Env) -> None:
    with serve(query_server) as upstream:
        values = monitoring_config(upstream.url)
        values["access"]["grants"]["alice"] = sorted(QUERY_TOOLS)
        config = env.config(**values, feishu=feishu_config())
        channel = FakeChannel()
        message = env.scripts.add(
            "飞书无监控权限", lambda call: answer([], analysis="", advice="无权查询该监控源")
        )
        async with env.running(config, feishu_channel=channel):
            channel.emit_raw_from_sdk_thread(feishu_event(env, message, "om_monitor_rejected"))
            await until(lambda: len(channel.sends) == 1)
            assert f"{SOURCE}__query" not in env.scripts.tools_seen(message)[0]
        assert upstream.recorder.tool_calls == []


async def test_group_member_can_query_only_a_group_granted_source(env: Env) -> None:
    with serve(query_server) as upstream:
        config = env.config(**monitoring_config(upstream.url), feishu=runtime_group(tools=[TOOL]))
        channel = GroupChannel()
        message = env.scripts.add(
            attributed(A, "群查指标"), tool_call(f"{SOURCE}__query", query="up"), cite()
        )
        async with env.running(config, feishu_channel=channel):
            channel.emit_raw_from_sdk_thread(raw_group_event(env, "群查指标", "om_monitor_group"))
            await until(lambda: len(channel.sends) == 1)
            to, payload, _ = channel.sends[0]
            assert (
                to == CHAT and SOURCE in message_text(payload) and "host1" in message_text(payload)
            )
        assert len(env.scripts.calls[message]) == 2
        assert upstream.recorder.tool_calls == [("query", {"query": "up"})]


async def test_group_without_source_grant_cannot_call_query(env: Env) -> None:
    with serve(query_server) as upstream:
        config = env.config(**monitoring_config(upstream.url), feishu=runtime_group())
        channel = GroupChannel()
        message = env.scripts.add(
            attributed(A, "群无监控权限"),
            lambda call: answer([], analysis="", advice="本群没有该监控源权限"),
        )
        async with env.running(config, feishu_channel=channel):
            channel.emit_raw_from_sdk_thread(
                raw_group_event(env, "群无监控权限", "om_monitor_denied")
            )
            await until(lambda: len(channel.sends) == 1)
            assert f"{SOURCE}__query" not in env.scripts.tools_seen(message)[0]
        assert upstream.recorder.tool_calls == []


async def test_unreachable_source_keeps_web_and_starrocks_ready(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    url = f"http://127.0.0.1:{free_port()}/mcp"
    config = env.config(**monitoring_config(url))
    with caplog.at_level(logging.WARNING, logger="xiaowei.mcp"):
        async with env.running(config) as served:
            ready = await served.ready()
            assert ready["status"] == "ready" and ready[f"mcp.{SOURCE}"] == "unavailable"
            await served.page()
            message = env.scripts.add(
                "本地查表",
                tool_call(
                    "list_tables",
                    cluster="sr-test",
                    keyword=".",
                    database=None,
                    page_size=5,
                    cursor=None,
                ),
                cite(),
            )
            response = await served.turn(message, "query")
            assert response.json()["state"] == "completed"
    records = [r.getMessage() for r in caplog.records if r.name == "xiaowei.mcp"]
    assert records == [f"MCP Server {SOURCE} 不可用（UserError），已隐藏其工具"]
    assert url not in caplog.text


async def test_schema_drift_hides_only_the_monitoring_tool(env: Env) -> None:
    with serve(drifted_server) as upstream:
        config = env.config(**monitoring_config(upstream.url))
        async with env.running(config) as served:
            assert (await served.ready())[f"mcp.{SOURCE}"] == "contract_mismatch"
            await served.page()
            message = env.scripts.add(
                "工具契约变化", lambda call: answer([], analysis="", advice="该源暂不可查")
            )
            response = await served.turn(message, "query")
            assert response.json()["state"] == "completed"
            assert all(f"{SOURCE}__query" not in seen for seen in env.scripts.tools_seen(message))
            assert upstream.recorder.tool_calls == []


async def test_revoked_source_evidence_cannot_be_read_from_web_history(env: Env) -> None:
    with serve(query_server) as upstream:
        granted = env.config(**monitoring_config(upstream.url))
        revoked = env.config(**monitoring_config(upstream.url, granted=False))
        async with env.running(granted) as served:
            await served.page()
            message = env.scripts.add(
                "生成监控证据", tool_call(f"{SOURCE}__query", query="up"), cite()
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            assert (await served.client.get("/api/turns/r1")).status_code == 200
            cookies = httpx.Cookies(served.client.cookies)
            assert await served.finish() == 0
        async with env.running(revoked) as served:
            served.client.cookies.update(cookies)
            assert (await served.client.get("/api/turns/r1")).status_code == 403
            assert upstream.recorder.tool_calls == [("query", {"query": "up"})]


async def test_authorized_monitoring_history_stays_readable_while_source_is_offline(
    env: Env,
) -> None:
    with serve(query_server) as upstream:
        config = env.config(**monitoring_config(upstream.url))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "保存监控事实", tool_call(f"{SOURCE}__query", query="up"), cite()
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            cookies = httpx.Cookies(served.client.cookies)
            assert await served.finish() == 0
        assert upstream.recorder.tool_calls == [("query", {"query": "up"})]

    async with env.running(config) as served:
        ready = await served.ready()
        assert ready["status"] == "ready" and ready[f"mcp.{SOURCE}"] == "unavailable"
        served.client.cookies.update(cookies)
        history = await served.client.get("/api/turns/r1")
        assert history.status_code == 200
        assert SOURCE in history.text and "up" in history.text


async def test_revoked_source_evidence_cannot_be_resent_to_feishu(env: Env) -> None:
    with serve(query_server) as upstream:
        granted = env.config(**monitoring_config(upstream.url), feishu=feishu_config())
        values = monitoring_config(upstream.url)
        values["access"]["grants"]["alice"] = sorted(QUERY_TOOLS)
        revoked = env.config(**values, feishu=feishu_config())
        refused = SendResult.fail(
            SendError(code=FeishuChannelErrorCode.PERMISSION_DENIED, retryable=False)
        )
        channel = FakeChannel(result=refused)
        message = env.scripts.add(
            "待重发监控事实", tool_call(f"{SOURCE}__query", query="up"), cite()
        )
        async with env.running(granted, feishu_channel=channel) as served:
            channel.emit_raw_from_sdk_thread(feishu_event(env, message, "om_monitor_resend"))
            await until(lambda: len(channel.sends) == 1)
            assert await served.finish() == 0
        assert await env.scalar("SELECT delivery FROM xiaowei_request") == "failed"
        resend_channel = FakeChannel()
        with pytest.raises(ResultUnavailableError):
            await runtime.resend(
                revoked,
                subject_id="alice",
                chat_id="oc_alice",
                message_id="om_monitor_resend",
                clock=env.clock,
                feishu_channel=resend_channel,
                starrocks_connect=env.connect(revoked),
            )
        assert resend_channel.sends == []
        assert upstream.recorder.tool_calls == [("query", {"query": "up"})]


async def test_revoking_one_source_does_not_authorize_it_through_another(env: Env) -> None:
    second_source = "prometheus-other"
    second_tool = f"{second_source}/query"
    with serve(query_server) as first, serve(query_server) as second:
        values = monitoring_config(first.url)
        values["mcp_servers"].append(
            {**values["mcp_servers"][0], "server_id": second_source, "url": second.url}
        )
        values["data_policy"]["model_tools"].append(second_tool)
        values["access"]["grants"]["operator"].append(second_tool)
        granted = env.config(**values)
        async with env.running(granted) as served:
            await served.page()
            first_message = env.scripts.add(
                "查第一源", tool_call(f"{SOURCE}__query", query="up"), cite()
            )
            assert (await served.turn(first_message, "query")).json()["state"] == "completed"
            cookies = httpx.Cookies(served.client.cookies)
            assert await served.finish() == 0

        values["access"]["grants"]["operator"].remove(TOOL)
        revoked = env.config(**values)
        async with env.running(revoked) as served:
            served.client.cookies.update(cookies)
            assert (await served.client.get("/api/turns/r1")).status_code == 403
            # 旧 Session 含已撤权证据，须按既有边界新建会话后再查另一源。
            assert (
                await served.client.post(
                    "/api/sessions",
                    json={},
                    headers={"origin": str(served.client.base_url).rstrip("/")},
                )
            ).status_code == 200
            other_message = env.scripts.add(
                "查第二源", tool_call(f"{second_source}__query", query="up"), cite()
            )
            assert (await served.turn(other_message, "query", "r2")).json()["state"] == "completed"
            assert f"{SOURCE}__query" not in env.scripts.tools_seen(other_message)[0]
        assert first.recorder.tool_calls == [("query", {"query": "up"})]
        assert second.recorder.tool_calls == [("query", {"query": "up"})]


async def test_disconnect_fails_current_turn_without_replaying_or_blocking_starrocks(
    env: Env,
) -> None:
    with serve(query_server) as upstream:
        config = env.config(**monitoring_config(upstream.url))
        async with env.running(config) as served:
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
            upstream.server.should_exit = True
            for _ in range(100):
                if upstream.open_connections() == 0:
                    break
                await asyncio.sleep(0.02)
            await served.page()
            message = env.scripts.add("断线查询", tool_call(f"{SOURCE}__query", query="up"))
            assert (await served.turn(message, "query")).json()["state"] == "failed"
            ready = await served.ready()
            assert ready["status"] == "ready" and ready[f"mcp.{SOURCE}"] == "disconnected"
            assert upstream.recorder.tool_calls == []


async def test_timeout_result_is_unknown_and_does_not_mark_source_disconnected(env: Env) -> None:
    def slow_once_server(recorder: Recorder) -> ASGIApp:
        server = MCPServer("prometheus-r1a-transient-timeout")

        @server.tool(structured_output=False)
        async def query(query: str) -> str:
            recorder.tool_calls.append(("query", {"query": query}))
            if len(recorder.tool_calls) == 1:
                await asyncio.sleep(3)
            return json.dumps({"result": "up => 1", "warnings": []})

        return recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)

    with serve(slow_once_server) as upstream:
        config = env.config(**monitoring_config(upstream.url))
        async with env.running(config) as served:
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
            await served.page()
            first = env.scripts.add("首次超时", tool_call(f"{SOURCE}__query", query="up"))
            assert (await served.turn(first, "query", "r1")).json()["state"] == "failed"
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"

            second = env.scripts.add(
                "再次查询成功", tool_call(f"{SOURCE}__query", query="up"), cite()
            )
            assert (await served.turn(second, "query", "r2")).json()["state"] == "completed"
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
        assert upstream.recorder.tool_calls == [
            ("query", {"query": "up"}),
            ("query", {"query": "up"}),
        ]


async def test_is_error_does_not_create_success_evidence(env: Env) -> None:
    with serve(failing_server) as upstream:
        config = env.config(**monitoring_config(upstream.url))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add("远端错误", tool_call(f"{SOURCE}__query", query="bad("))
            assert (await served.turn(message, "query")).json()["state"] == "failed"
            assert upstream.recorder.tool_calls == [("query", {"query": "bad("})]
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"


async def test_old_starrocks_evidence_remains_readable_with_new_monitoring_source(env: Env) -> None:
    old = env.config()
    with serve(query_server) as upstream:
        monitoring = env.config(**monitoring_config(upstream.url))
        async with env.running(old) as served:
            await served.page()
            message = env.scripts.add(
                "旧查询证据",
                tool_call(
                    "list_tables",
                    cluster="sr-test",
                    keyword=".",
                    database=None,
                    page_size=5,
                    cursor=None,
                ),
                cite(),
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            cookies = httpx.Cookies(served.client.cookies)
            assert await served.finish() == 0
        async with env.running(monitoring) as served:
            served.client.cookies.update(cookies)
            history = await served.client.get("/api/turns/r1")
            assert history.status_code == 200
            assert history.json()["state"] == "completed"


@pytest.mark.skipif(not os.getenv("XW_TEST_PROMETHEUS_MCP_BIN"), reason="官方二进制需显式设置")
async def test_official_prometheus_query_via_formal_web_forwards_authorization(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = Path(os.environ["XW_TEST_PROMETHEUS_MCP_BIN"])
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True)  # noqa: S603
    assert "version: 0.18.0" in version.stdout
    token = "synthetic-readonly-token"  # noqa: S105 - 合成测试身份
    monkeypatch.setenv("XW_TEST_MONITORING_TOKEN", token)
    with prometheus_api() as (port, requests), official_server(binary, port) as url:
        values = monitoring_config(url)
        values["mcp_servers"][0]["auth_ref"] = "env:XW_TEST_MONITORING_TOKEN"
        config = env.config(**values)
        async with env.running(config) as served:
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
            await served.page()
            message = env.scripts.add("官方指标", tool_call(f"{SOURCE}__query", query="up"), cite())
            result = (await served.turn(message, "query")).json()
            assert result["state"] == "completed"
            assert "host1" in result["delivery"]["content"]
            assert requests == [("up", f"Bearer {token}")]
            ready_text = (await served.client.get("/readyz")).text
            assert token not in ready_text and url not in ready_text
