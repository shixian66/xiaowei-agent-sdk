"""P2 选中工具的锁版协议证据：官方二进制，只连接 loopback 合成 API。"""

import hashlib
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from agents.mcp import MCPServerStreamableHttp
from mcp.types import CallToolResult, TextContent
from tests.sdk_core.test_prometheus_mcp_protocol import official_server

pytestmark = pytest.mark.loopback

BINARY_SHA256 = "6272e0d8fbc8af341d1e93fdc6aa130f7086b7a993faedb015966c2ace5e3a82"
WINDOW = {"start_time": "1720000000", "end_time": "1720000060"}
CALLS: dict[str, dict[str, Any]] = {
    "query": {"query": "up"},
    "range_query": {"query": "up", **WINDOW, "step": "30s"},
    "label_names": {"matches": [], **WINDOW},
    "label_values": {"label": "__name__", "matches": [], **WINDOW},
    "series": {"matches": ["node_uname_info"], **WINDOW},
    "metric_metadata": {"metric": "up", "limit": "0"},
    "list_rules": {},
}
PREFIXES = {
    "query": "failed making query api call: failed to execute instant query: ",
    "range_query": "failed making range query api call: failed to execute range query: ",
    "label_names": "failed making label names api call: failed to get label names: ",
    "label_values": "failed making label values api call: failed to get label values: ",
    "series": "failed making series api call: failed to get series: ",
    "metric_metadata": (
        "failed making metric metadata api call: failed to get metric metadata from Prometheus: "
    ),
    "list_rules": "failed making rules api call: failed to get rules from Prometheus: ",
}
RULES = {
    "groups": [
        {
            "name": "host",
            "file": "/synthetic/host.rules",
            "interval": 60,
            "rules": [
                {
                    "name": "HostCPUHigh",
                    "query": "cpu_ratio > 0.8",
                    "duration": 60,
                    "labels": {"severity": "warning"},
                    "annotations": {"summary": "CPU high"},
                    "alerts": [],
                    "health": "ok",
                    "state": "firing",
                    "type": "alerting",
                    "evaluationTime": 0.001,
                    "lastEvaluation": "2024-07-03T09:46:40Z",
                },
                {
                    "name": "host:cpu:ratio",
                    "query": "cpu_ratio",
                    "labels": {},
                    "health": "ok",
                    "type": "recording",
                    "evaluationTime": 0.001,
                    "lastEvaluation": "2024-07-03T09:46:40Z",
                },
            ],
            "evaluationTime": 0.001,
            "lastEvaluation": "2024-07-03T09:46:40Z",
        }
    ]
}
SAMPLES: dict[str, Any] = {
    "/api/v1/query": {
        "resultType": "vector",
        "result": [
            {"metric": {"__name__": "up", "instance": "host1"}, "value": [1720000000, "1"]},
            {"metric": {"__name__": "up", "instance": "host2"}, "value": [1720000000, "0"]},
        ],
    },
    "/api/v1/query_range": {
        "resultType": "matrix",
        "result": [
            {
                "metric": {"__name__": "up", "instance": "host1"},
                "values": [[1720000000, "1"], [1720000030, "0"], [1720000060, "1"]],
            }
        ],
    },
    "/api/v1/labels": ["__name__", "instance", "job"],
    "/api/v1/label/__name__/values": ["up", "node_cpu_seconds_total"],
    "/api/v1/series": [
        {"__name__": "node_uname_info", "instance": "host1:9100", "nodename": "host1"}
    ],
    "/api/v1/metadata": {"up": [{"type": "gauge", "help": "Target scrape status", "unit": ""}]},
    "/api/v1/rules": RULES,
}


@dataclass
class BackendState:
    requests: list[tuple[str, dict[str, list[str]], str | None]] = field(default_factory=list)
    status: int = 200
    error_type: str = "bad_data"
    detail: str = 'invalid parameter "query": 1:4: parse error: unexpected end of input'
    query_errors: dict[str, tuple[int, str, str]] = field(default_factory=dict)


@contextmanager
def prometheus_backend(
    *, samples: dict[str, Any] | None = None
) -> Iterator[tuple[ThreadingHTTPServer, BackendState]]:
    """替换远端 Prometheus HTTP API；保留官方 MCP 与客户端的真实序列化路径。"""
    state = BackendState()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            arguments = parse_qs(parsed.query)
            state.requests.append((parsed.path, arguments, self.headers.get("Authorization")))
            status, error_type, detail = state.query_errors.get(
                arguments.get("query", [""])[0], (state.status, state.error_type, state.detail)
            )
            if status == 200:
                data = (SAMPLES if samples is None else samples)[parsed.path]
                if parsed.path == "/api/v1/metadata":
                    metric = arguments.get("metric", [""])[0]
                    if metric:
                        data = {metric: data[metric]} if metric in data else {}
                    limit = int(arguments.get("limit", ["0"])[0])
                    if limit > 0:
                        data = dict(list(data.items())[:limit])
                body = {
                    "status": "success",
                    "data": data,
                    "warnings": ["synthetic partial data"],
                }
            else:
                body = {"status": "error", "errorType": error_type, "error": detail}
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args: object) -> None:
            return

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=backend.serve_forever, daemon=True)
    worker.start()
    try:
        yield backend, state
    finally:
        backend.shutdown()
        backend.server_close()
        worker.join(timeout=5)


@pytest.fixture
def official_binary() -> Path:
    location = os.getenv("XW_TEST_PROMETHEUS_MCP_BIN")
    if not location:
        pytest.skip("未配置已校验的官方 v0.18.0 二进制")
    binary = Path(location)
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == BINARY_SHA256
    return binary


def text_result(result: CallToolResult) -> str:
    assert result.result_type == "complete"
    assert result.structured_content is None
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def test_official_p2_input_output_and_single_upstream_request(official_binary: Path) -> None:
    with prometheus_backend() as (backend, state):
        with official_server(official_binary, backend.server_port, tools="list_rules") as url:
            async with MCPServerStreamableHttp(
                {"url": url}, max_retry_attempts=0, client_session_timeout_seconds=5
            ) as client:
                listed = {tool.name: tool.input_schema for tool in await client.list_tools()}
                assert set(CALLS) <= set(listed)
                assert not {"reload", "quit", "delete_series", "clean_tombstones"} & set(listed)
                assert listed["query"]["required"] == ["query"]
                assert listed["range_query"]["required"] == ["query"]
                assert listed["series"]["required"] == ["matches"]
                assert listed["label_values"]["required"] == ["label"]
                assert listed["list_rules"] == {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                }
                for name in ("series", "label_values", "label_names"):
                    assert listed[name]["properties"]["matches"]["type"] == ["null", "array"]
                    assert listed[name]["properties"]["matches"]["items"] == {"type": "string"}

                expected_texts = {
                    "query": (
                        'up{instance="host1"} => 1 @[1720000000]\n'
                        'up{instance="host2"} => 0 @[1720000000]'
                    ),
                    "range_query": (
                        'up{instance="host1"} =>\n1 @[1720000000]\n0 @[1720000030]\n1 @[1720000060]'
                    ),
                    "label_names": "__name__\ninstance\njob",
                    "label_values": "up\nnode_cpu_seconds_total",
                    "series": (
                        '{__name__="node_uname_info", instance="host1:9100", nodename="host1"}'
                    ),
                }
                for name, arguments in CALLS.items():
                    before = len(state.requests)
                    result = await client.call_tool(name, arguments)
                    assert not result.is_error
                    assert len(state.requests) == before + 1
                    payload = json.loads(text_result(result))
                    if name in expected_texts:
                        assert payload == {
                            "result": expected_texts[name],
                            "warnings": ["synthetic partial data"],
                        }
                    elif name == "metric_metadata":
                        assert payload == SAMPLES["/api/v1/metadata"]
                        assert "warnings" not in payload  # 官方 client.Metadata 丢弃上游 warnings。
                    else:
                        rules = payload["groups"][0]["rules"]
                        assert rules[0]["name"] == "HostCPUHigh"
                        assert rules[0]["state"] == "firing"
                        assert rules[1]["name"] == "host:cpu:ratio"
                        assert "type" not in rules[0]
                        assert "warnings" not in payload
                assert state.requests[1][0:2] == (
                    "/api/v1/query_range",
                    {
                        "query": ["up"],
                        "start": ["1720000000"],
                        "end": ["1720000060"],
                        "step": ["30"],
                    },
                )
                assert state.requests[0][1]["time"] != ["1720000000"]
                before = len(state.requests)
                await client.call_tool("query", {"query": "up", "timestamp": "1720000000"})
                assert len(state.requests) == before + 1
                assert state.requests[-1][1]["time"] == ["1720000000"]


@pytest.mark.parametrize(
    ("status", "kind", "detail", "suffix"),
    [
        (
            400,
            "bad_data",
            'invalid parameter "query": 1:4: parse error: unexpected end of input',
            'bad_data: invalid parameter "query": 1:4: parse error: unexpected end of input',
        ),
        (
            400,
            "bad_data",
            'invalid parameter "step": must be positive',
            'bad_data: invalid parameter "step": must be positive',
        ),
        (
            422,
            "execution",
            "query processing would load too many samples",
            "execution: query processing would load too many samples",
        ),
        (401, "unauthorized", "not exposed", "client_error: client error: 401"),
        (403, "forbidden", "not exposed", "client_error: client error: 403"),
        (500, "internal", "not exposed", "server_error: server error: 500"),
        (503, "timeout", "not exposed", "server_error: server error: 503"),
    ],
)
async def test_official_p2_error_shapes_are_not_structured_and_not_retried(
    official_binary: Path, status: int, kind: str, detail: str, suffix: str
) -> None:
    with prometheus_backend() as (backend, state):
        state.status, state.error_type, state.detail = status, kind, detail
        with official_server(official_binary, backend.server_port, tools="list_rules") as url:
            async with MCPServerStreamableHttp(
                {"url": url}, max_retry_attempts=0, client_session_timeout_seconds=5
            ) as client:
                for name, arguments in CALLS.items():
                    before = len(state.requests)
                    result = await client.call_tool(name, arguments)
                    assert len(state.requests) == before + 1
                    assert result.is_error
                    assert text_result(result) == PREFIXES[name] + suffix


async def test_official_truncation_marker_and_metadata_non_json_suffix(
    official_binary: Path,
) -> None:
    with prometheus_backend() as (backend, state):
        with official_server(
            official_binary, backend.server_port, tools="list_rules", truncation_limit=1
        ) as url:
            async with MCPServerStreamableHttp(
                {"url": url}, max_retry_attempts=0, client_session_timeout_seconds=5
            ) as client:
                before = len(state.requests)
                result = await client.call_tool("range_query", CALLS["range_query"])
                assert len(state.requests) == before + 1
                payload = json.loads(text_result(result))
                assert "--prometheus.truncation-limit=1" in payload["result"]
                assert "0 @[1720000030]" not in payload["result"]
                assert payload["warnings"] == ["synthetic partial data"]
                before = len(state.requests)
                metadata = await client.call_tool("metric_metadata", {"metric": "up", "limit": "1"})
                assert len(state.requests) == before + 1
                text = text_result(metadata)
                parsed, end = json.JSONDecoder().raw_decode(text)
                assert parsed == SAMPLES["/api/v1/metadata"]
                assert text[end:].startswith("\n\nWarning: The result was truncated")
                with pytest.raises(json.JSONDecodeError):
                    json.loads(text)
