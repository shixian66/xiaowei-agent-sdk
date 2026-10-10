"""Prometheus MCP v0.18.0 的代表工具契约；真实 Server 探测记录见 handoff。"""

import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from agents import Agent, Runner, UserError
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message, function_call
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.engine import URL
from tests.sdk_core.synthetic_tools import Clock, Grants, cited, context, ready_engine, store

from xiaowei.config import MCPServerConfig
from xiaowei.governance import (
    GovernedTools,
    Projection,
    ToolCatalog,
    ToolExecutionError,
    ToolPolicy,
)
from xiaowei.mcp import MCPIntegration, _Binding, _payload, _verified
from xiaowei.models import AgentAnswer, ToolContract


class QueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str


class QueryResult(BaseModel):
    result: str
    warnings: list[str] | None


QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "timestamp": {"type": "string"},
        "truncation_limit": {"type": "integer"},
    },
    "required": ["query"],
    "additionalProperties": False,
}


def binding() -> tuple[MCPServerConfig, dict[str, _Binding]]:
    config = MCPServerConfig(
        server_id="prometheus",
        url="http://127.0.0.1:8080/mcp",
        auth_ref=None,
        timeout_seconds=5,
        max_response_bytes=64_000,
        allowed_tools={"query": "prometheus.query"},
    )
    contract = ToolContract(
        tool_id="prometheus/query",
        target_id="prometheus-test",
        input_schema=QueryArgs.model_json_schema(),
        policy_id="prometheus.query",
        description="执行一次 PromQL 即时查询",
    )
    return config, {"prometheus__query": _Binding(config, "query", contract, QueryResult)}


def test_selected_query_can_omit_upstream_optional_arguments() -> None:
    """v0.18.0 允许 timestamp/truncation_limit 省略；本地只开放 query。"""
    config, planned = binding()
    listed = [MCPTool(name="query", input_schema=QUERY_SCHEMA)]

    assert _verified(listed, planned, config) == ["prometheus__query"]


def test_unexposed_optional_schema_is_not_parsed() -> None:
    config, planned = binding()
    schema = {
        **QUERY_SCHEMA,
        "properties": {
            **QUERY_SCHEMA["properties"],
            "truncation_limit": {"$ref": "#/$defs/missing"},
        },
    }

    assert _verified([MCPTool(name="query", input_schema=schema)], planned, config) == [
        "prometheus__query"
    ]


@pytest.mark.parametrize(
    "drift",
    [
        {"properties": {**QUERY_SCHEMA["properties"], "query": {"type": "integer"}}},
        {"required": ["query", "timestamp"]},
        {"required": [["query"]]},
        {"oneOf": [{"required": ["timestamp"]}]},
        {"$schema": "https://json-schema.org/draft/2020-12/schema"},
    ],
)
def test_query_schema_drift_hides_tool(drift: dict[str, object]) -> None:
    config, planned = binding()
    listed = [MCPTool(name="query", input_schema={**QUERY_SCHEMA, **drift})]

    assert _verified(listed, planned, config) == []


def test_query_json_text_keeps_result_and_warnings() -> None:
    upstream = {"result": 'up{instance="host1"} => 1 @[1720000000]', "warnings": None}
    result = CallToolResult(content=[TextContent(type="text", text=json.dumps(upstream))])

    assert _payload(result, QueryResult) == upstream


@contextmanager
def official_server(
    binary: Path,
    backend_port: int,
    *,
    port: int | None = None,
    tools: str = "core",
    truncation_limit: int = 0,
) -> Iterator[str]:
    if port is None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
    process = subprocess.Popen(  # noqa: S603 - opt-in local protocol test binary
        [
            str(binary),
            "--mcp.transport=http",
            f"--prometheus.url=http://127.0.0.1:{backend_port}",
            f"--web.listen-address=127.0.0.1:{port}",
            f"--mcp.tools={tools}",
            f"--prometheus.truncation-limit={truncation_limit}",
            "--log.level=error",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Prometheus MCP 提前退出")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Prometheus MCP 未开始监听")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def answer_from_tool(call: ModelCall) -> list[object]:
    assert isinstance(call.input, list)
    output = [item["output"] for item in call.input if item.get("type") == "function_call_output"]
    evidence_id = json.loads(output[-1])["evidence_id"]
    return [
        assistant_message(
            json.dumps(
                {
                    "evidence_ids": [evidence_id],
                    "inferences": [{"text": "合成指标已返回", "evidence_ids": [evidence_id]}],
                    "clarification": None,
                }
            )
        )
    ]


@pytest.mark.loopback
@pytest.mark.skipif(
    not os.getenv("XW_TEST_PROMETHEUS_MCP_BIN"), reason="未配置官方 v0.18.0 测试二进制"
)
async def test_official_query_through_runner_and_governance(postgres_url: URL) -> None:
    """真 Server + 真 SDK + 现有治理/Evidence；上游仅是本机合成 API。"""
    binary = Path(os.environ["XW_TEST_PROMETHEUS_MCP_BIN"])
    version = subprocess.run(  # noqa: S603 - opt-in local protocol test binary
        [str(binary), "--version"], capture_output=True, text=True, check=True
    )
    assert "version: 0.18.0" in version.stdout
    requests: list[tuple[str, str | None]] = []

    class Backend(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path != "/api/v1/query":
                self.send_error(404)
                return
            expression = parse_qs(parsed.query)["query"][0]
            requests.append((expression, self.headers.get("Authorization")))
            if expression == "bad(":
                status = 400
                body = {"status": "error", "errorType": "bad_data", "error": "parse error"}
            else:
                status = 200
                body = {
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
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args: object) -> None:
            return

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    try:
        with official_server(binary, backend.server_port) as url:
            policy = ToolPolicy(
                policy_id="prometheus.query",
                arguments=QueryArgs,
                result=QueryResult,
                projections={
                    audience: Projection(fields=("result", "warnings"), max_bytes=2000)
                    for audience in ("model", "session", "web", "feishu")
                },
            )
            contract = binding()[1]["prometheus__query"].contract
            catalog = ToolCatalog((contract,), (policy,))
            async with ready_engine(postgres_url) as engine:
                grants, clock = Grants(), Clock()
                grants.grant("alice", "prometheus/query", target="prometheus-test")
                governed = GovernedTools(store(engine, grants, clock, catalog))
                for token, expected_auth in ((False, None), (True, "Bearer synthetic-token")):
                    cfg = MCPServerConfig(
                        server_id="prometheus",
                        url=url,
                        auth_ref="env:XW_TEST_TOKEN" if token else None,
                        timeout_seconds=5,
                        max_response_bytes=64_000,
                        allowed_tools={"query": "prometheus.query"},
                    )
                    async with MCPIntegration(
                        (cfg,), governed, resolve_secret=lambda _: SecretStr("synthetic-token")
                    ) as integration:
                        ctx = context(
                            turn=f"t-{token}",
                            tools=frozenset({"prometheus/query"}),
                            targets=frozenset({"prometheus-test"}),
                        )
                        tools = integration.tools_for(ctx)
                        assert [tool.name for tool in tools] == ["prometheus__query"]
                        model = ScriptedModel(
                            [
                                [
                                    function_call(
                                        "prometheus__query", {"query": "up"}, call_id=f"q-{token}"
                                    )
                                ],
                                ModelStep.respond(answer_from_tool),
                            ]
                        )
                        before = len(requests)
                        result = await Runner.run(
                            Agent(
                                name="prometheus", model=model, tools=tools, output_type=AgentAnswer
                            ),
                            "查合成指标",
                            context=ctx,
                        )
                        assert len(requests) == before + 1
                        assert requests[-1] == ("up", expected_auth)
                        assert isinstance(result.final_output, AgentAnswer)
                        delivery = await governed.evidence.validate_answer(
                            cited(result.final_output), ctx
                        )
                        assert "host1" in delivery.content

                        if not token:
                            failed = context(
                                turn="t-bad",
                                tools=frozenset({"prometheus/query"}),
                                targets=frozenset({"prometheus-test"}),
                            )
                            bad_model = ScriptedModel(
                                [
                                    [
                                        function_call(
                                            "prometheus__query", {"query": "bad("}, call_id="q-bad"
                                        )
                                    ]
                                ]
                            )
                            before = len(requests)
                            with pytest.raises(UserError) as error:
                                await Runner.run(
                                    Agent(
                                        name="prometheus",
                                        model=bad_model,
                                        tools=tools,
                                        output_type=AgentAnswer,
                                    ),
                                    "查错误表达式",
                                    context=failed,
                                )
                            assert len(requests) == before + 1
                            assert requests[-1] == ("bad(", None)
                            assert isinstance(error.value.__cause__, ToolExecutionError)
                            assert governed.turn_runs(failed.identity).produced == ()
    finally:
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=5)
