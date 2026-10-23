"""CC0 独立脚本：真 SDK + 临时 loopback MCP，无模型或公司服务。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field
from tests.sdk_core.mcp_fixture import recording, redirecting, serve

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
        assert report["call"]["result_json_bytes"] > 0
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
