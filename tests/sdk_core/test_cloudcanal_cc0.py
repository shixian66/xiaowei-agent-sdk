"""CC0 独立脚本：真 SDK + 临时 loopback MCP，无模型或公司服务。"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from tests.sdk_core.mcp_fixture import gzipped, recording, redirecting, serve

pytestmark = pytest.mark.loopback
ROOT = Path(__file__).resolve().parents[2]
TOKEN = "cc0-fake-token-9218"  # noqa: S105 - 合成哨兵
BUSINESS = "private-order-4833"


class JobResult(BaseModel):
    job_id: str
    delay: int
    log: str
    extra: dict[str, str]


def fixture_app(recorder, *, path="/mcp", json_response=True):
    server = MCPServer("cc0-fixture")

    @server.tool(description=f"业务描述 {BUSINESS}，token={TOKEN}")
    def queryJob(  # noqa: N802
        job_id: str = Field(description=BUSINESS, json_schema_extra={"x-business": BUSINESS}),
    ) -> JobResult:
        recorder.tool_calls.append(("queryJob", {"job_id": job_id}))
        return JobResult(job_id=job_id, delay=219837, log=TOKEN + BUSINESS, extra={BUSINESS: TOKEN})

    @server.tool()
    def restartJob() -> str:  # noqa: N802
        recorder.tool_calls.append(("restartJob", {}))
        return "should-not-run"

    @server.tool()
    def largeRead() -> str:  # noqa: N802
        recorder.tool_calls.append(("largeRead", {}))
        return BUSINESS * 20000

    @server.tool()
    def failedRead() -> str:  # noqa: N802
        recorder.tool_calls.append(("failedRead", {}))
        raise ValueError(TOKEN + BUSINESS)

    @server.tool()
    def mcp_x(v1_flag: str) -> str:
        recorder.tool_calls.append(("mcp_x", {"v1_flag": v1_flag}))
        return json.dumps({BUSINESS: TOKEN})

    return recording(
        server.streamable_http_app(streamable_http_path=path, json_response=json_response),
        recorder,
        token=None,
    )


def cli(url, *args):
    env = {**os.environ, "CLOUDCANAL_MCP_URL": url}
    return subprocess.run(  # noqa: S603 - 只启动本仓库脚本，URL 仅来自 loopback fixture
        [sys.executable, "-m", "scripts.cloudcanal_cc0", "--timeout", "3", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_catalog_only_never_calls_tools_or_exports_values():
    with serve(fixture_app) as running:
        result = cli(running.recorder.url + "?token=" + TOKEN)
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        tools = {tool["name"]: tool for tool in report["catalog"]["tools"]}
        assert tools["queryJob"]["input_schema"]["properties"]["job_id"]["type"] == "string"
        assert "restartJob" in tools  # 列出不等于获准执行。
        assert running.recorder.tool_calls == []
        assert report["catalog"]["elapsed_ms"] >= 0
        assert report["catalog"]["wire_bytes_observed"] > 0
        assert TOKEN not in result.stdout + result.stderr
        assert BUSINESS not in result.stdout + result.stderr


@pytest.mark.parametrize("link_kind", ["query_json", "path_sse"])
def test_explicit_read_once_exports_shape_not_business_values(tmp_path, link_kind):
    arguments = tmp_path / "args.json"
    arguments.write_text(json.dumps({"job_id": BUSINESS}))
    path = f"/{TOKEN}/mcp" if link_kind == "path_sse" else "/mcp"
    with serve(
        lambda recorder: fixture_app(recorder, path=path, json_response=link_kind != "path_sse"),
        path=path,
    ) as running:
        result = cli(
            running.recorder.url + ("?token=" + TOKEN if link_kind == "query_json" else ""),
            "--read-tool",
            "queryJob",
            "--arguments",
            str(arguments),
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert running.recorder.tool_calls == [("queryJob", {"job_id": BUSINESS})]
        assert report["call"]["status"] == "received"
        assert report["call"]["wire_bytes_observed"] > 0
        assert report["call"]["shape"]["fields"]["structuredContent"]["fields"]["delay"] == {
            "type": "integer"
        }
        assert all(
            value not in result.stdout + result.stderr for value in (TOKEN, BUSINESS, "219837")
        )


def test_mcp_error_is_reported_without_error_text_or_retry(tmp_path):
    arguments = tmp_path / "args.json"
    arguments.write_text("{}")
    with serve(fixture_app) as running:
        result = cli(
            running.recorder.url, "--read-tool", "failedRead", "--arguments", str(arguments)
        )
        assert result.returncode == 1
        assert json.loads(result.stdout)["call"]["status"] == "mcp_error"
        assert running.recorder.tool_calls == [("failedRead", {})]
        assert TOKEN not in result.stdout + result.stderr
        assert BUSINESS not in result.stdout + result.stderr


def test_unknown_tool_does_not_call_any_tool(tmp_path):
    arguments = tmp_path / "args.json"
    arguments.write_text("{}")
    with serve(fixture_app) as running:
        result = cli(running.recorder.url, "--read-tool", "absent", "--arguments", str(arguments))
        assert result.returncode == 1
        assert json.loads(result.stdout)["failure"]["reason"] == "tool_not_listed"
        assert running.recorder.tool_calls == []


def test_overflow_reports_incomplete_once_without_partial_payload(tmp_path):
    arguments = tmp_path / "args.json"
    arguments.write_text("{}")
    with serve(fixture_app) as running:
        result = cli(
            running.recorder.url + "?token=" + TOKEN,
            "--read-tool",
            "largeRead",
            "--arguments",
            str(arguments),
            "--max-response-bytes",
            "12000",
        )
        assert result.returncode == 1
        report = json.loads(result.stdout)
        assert report["failure"]["reason"] == "response_limit"
        assert report["failure"]["phase"] == "call"
        assert "call" not in report
        assert running.recorder.tool_calls == [("largeRead", {})]
        assert TOKEN not in result.stdout + result.stderr
        assert BUSINESS not in result.stdout + result.stderr


def test_redirect_never_contacts_alternate_endpoint():
    with serve(fixture_app) as target:
        redirect_url = target.recorder.url + "?token=" + TOKEN
        with serve(lambda recorder: redirecting(redirect_url, recorder)) as origin:
            result = cli(origin.recorder.url + "?token=" + TOKEN)
        assert result.returncode == 1
        assert target.recorder.requests == []
        assert TOKEN not in result.stdout + result.stderr


def test_bad_arguments_fail_before_connection(tmp_path):
    arguments = tmp_path / "args.json"
    arguments.write_text("[]")
    with serve(fixture_app) as running:
        result = cli(running.recorder.url, "--read-tool", "queryJob", "--arguments", str(arguments))
        assert result.returncode == 2
        assert json.loads(result.stdout)["failure"]["reason"] == "invalid_configuration"
        assert running.recorder.requests == []


def test_malformed_secret_link_does_not_escape_as_traceback():
    result = cli(f"http://127.0.0.1:{TOKEN}/mcp")
    assert result.returncode == 2
    assert json.loads(result.stdout)["failure"]["reason"] == "invalid_configuration"
    assert TOKEN not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_common_link_fragments_preserve_names_and_shape(tmp_path):
    arguments = tmp_path / "args.json"
    arguments.write_text(json.dumps({"v1_flag": BUSINESS}))
    path = "/api/v1/mcp"
    with serve(lambda recorder: fixture_app(recorder, path=path), path=path) as running:
        result = cli(
            running.recorder.url + f"?token={TOKEN}&format=json&flag=true&page=1",
            "--read-tool",
            "mcp_x",
            "--arguments",
            str(arguments),
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        tools = {tool["name"]: tool for tool in report["catalog"]["tools"]}
        assert tools["mcp_x"]["input_schema"]["properties"]["v1_flag"]["type"] == "string"
        assert tools["mcp_x"]["input_schema"]["required"] == ["v1_flag"]
        assert report["call"]["tool"] == "mcp_x"
        content = report["call"]["shape"]["fields"]["content"]["items"][0]
        assert content["fields"]["text"]["json_shape"]["fields"]["<field-1>"]["type"] == "string"
        assert running.recorder.tool_calls == [("mcp_x", {"v1_flag": BUSINESS})]
        assert TOKEN not in result.stdout + result.stderr
        assert BUSINESS not in result.stdout + result.stderr


@pytest.mark.parametrize("secret", [TOKEN, 'escaped-"token-0123'])
def test_secret_in_metadata_fails_without_exporting_report(secret):
    def build(recorder):
        server = MCPServer("cc0-secret-fixture")

        @server.tool(name=f"reflected_{secret}")
        def reflected() -> str:
            recorder.tool_calls.append(("should-not-run", {}))
            return "unused"

        return recording(server.streamable_http_app(), recorder, token=None)

    with serve(build) as running:
        result = cli(running.recorder.url + "?token=" + quote(secret, safe=""))
        assert result.returncode == 1
        assert json.loads(result.stdout) == {
            "status": "failed",
            "failure": {"reason": "secret_in_output"},
        }
        assert secret not in result.stdout + result.stderr
        assert "reflected_" not in result.stdout + result.stderr
        assert running.recorder.tool_calls == []


def test_compressed_response_is_rejected_before_tool_calls():
    with serve(lambda recorder: gzipped(fixture_app(recorder))) as running:
        result = cli(running.recorder.url + "?token=" + TOKEN)
        assert result.returncode == 1
        assert json.loads(result.stdout)["failure"]["reason"] == "redirect_or_encoding_rejected"
        assert running.recorder.tool_calls == []
        assert TOKEN not in result.stdout + result.stderr


def fault_app(recorder, mode):
    """最小 MCP 协议反例：可续接的 SSE 超限，或期限后的慢 DELETE。"""
    call_id = None

    async def stream():
        if mode == "overflow":
            yield (
                b'id: checkpoint\nretry: 1\nevent: message\ndata: {"jsonrpc":"2.0",'
                b'"method":"notifications/tools/list_changed"}\n\n'
            )
            await asyncio.sleep(0.02)
            yield b"data: " + b"x" * 20000 + b"\n\n"
        else:
            for _ in range(20):
                await asyncio.sleep(0.05)
                yield b" "

    async def app(scope, receive, send):
        nonlocal call_id
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        if request.method == "GET":
            if request.headers.get("last-event-id"):
                recorder.tool_calls.append(("unexpected_resume", {}))
                payload = {"jsonrpc": "2.0", "id": call_id, "result": {"content": []}}
                response = Response(
                    f"event: message\ndata: {json.dumps(payload)}\n\n",
                    media_type="text/event-stream",
                )
            else:
                response = Response(status_code=405)
        elif request.method == "DELETE":
            response = StreamingResponse(stream()) if mode == "timeout" else Response()
        else:
            body = await request.json()
            method = body["method"]
            if method == "server/discover":
                await JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )(scope, receive, send)
                return
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cc0-fixture", "version": "1"},
                }
            elif method == "tools/list":
                result = {"tools": [{"name": "read", "inputSchema": {"type": "object"}}]}
            elif method == "tools/call":
                call_id = body["id"]
                recorder.tool_calls.append(("read", {}))
                if mode == "timeout":
                    await asyncio.sleep(1)
                await StreamingResponse(stream(), media_type="text/event-stream")(
                    scope, receive, send
                )
                return
            else:
                await Response(status_code=202)(scope, receive, send)
                return
            response = JSONResponse(
                {"jsonrpc": "2.0", "id": body["id"], "result": result},
                headers={"mcp-session-id": "fixture-session"},
            )
        await response(scope, receive, send)

    return recording(app, recorder, token=None)


@pytest.mark.parametrize("mode", ["overflow", "timeout"])
def test_sdk_stream_resume_and_cleanup_cannot_bypass_failed_boundary(tmp_path, mode):
    arguments = tmp_path / "args.json"
    arguments.write_text("{}")
    with serve(lambda recorder: fault_app(recorder, mode)) as running:
        result = cli(
            running.recorder.url,
            "--read-tool",
            "read",
            "--arguments",
            str(arguments),
            "--max-response-bytes",
            "12000",
            "--timeout",
            "0.3" if mode == "timeout" else "3",
        )
        assert result.returncode == 1
        report = json.loads(result.stdout)
        assert report["failure"]["reason"] == ("timeout" if mode == "timeout" else "response_limit")
        assert "call" not in report
        assert running.recorder.tool_calls == [("read", {})]
        if mode == "timeout":
            assert "DELETE" not in running.recorder.methods()
