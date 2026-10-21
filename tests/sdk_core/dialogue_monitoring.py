"""A finite loopback Prometheus MCP fixture; it does not implement PromQL semantics."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from tests.sdk_core.mcp_fixture import LOOPBACK, ASGIApp, Recorder, recording, serve

SOURCE = "prometheus-prod"
DENIED_SOURCE = "prometheus-denied"
SUPPORTED_QUERIES = frozenset(
    {"up", "node_uname_info", 'node_uname_info{cluster="fat"}', 'node_uname_info{job="node"}'}
)
HOSTS = (("192.0.2.11", "fat-be-1"), ("192.0.2.12", "fat-be-2"), ("192.0.2.13", "fat-fe-1"))
SAMPLED_AT = int(datetime(2026, 10, 19, 17, 30, tzinfo=UTC).timestamp())
METRICS = "\n".join(
    f'node_uname_info{{instance="{ip}:9100",nodename="{name}",cluster="fat",job="node"}}'
    f" => 1 @[{SAMPLED_AT}]"
    for ip, name in HOSTS
)


def nodes_server(recorder: Recorder) -> ASGIApp:
    server = MCPServer("P4-synthetic-monitoring")

    @server.tool(structured_output=False)
    def query(query: str) -> CallToolResult:
        recorder.tool_calls.append(("query", {"query": query}))
        if query not in SUPPORTED_QUERIES:
            return CallToolResult(
                is_error=True,
                content=[
                    TextContent(
                        type="text",
                        text=(
                            "P4 synthetic fixture does not support this expression; "
                            "real PromQL validity is unverified"
                        ),
                    )
                ],
            )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "result": (
                                "\n".join(
                                    f'up{{instance="{ip}:9100",cluster="fat"}}'
                                    f" => {value} @[{SAMPLED_AT}]"
                                    for (ip, _), value in zip(HOSTS, (1, 0, 1), strict=True)
                                )
                                if query == "up"
                                else METRICS
                            ),
                            "warnings": None,
                        }
                    ),
                )
            ]
        )

    return recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)


@contextmanager
def synthetic_monitoring() -> Iterator[Recorder]:
    with serve(nodes_server) as running:
        yield running.recorder


def denied_server(recorder: Recorder) -> ASGIApp:
    server = MCPServer("P4-synthetic-denied-monitoring")

    @server.tool(structured_output=False)
    def query(query: str) -> CallToolResult:
        recorder.tool_calls.append(("query", {"query": query}))
        return CallToolResult(
            is_error=True,
            content=[
                TextContent(
                    type="text",
                    text=(
                        "failed making query api call: failed to execute instant query: "
                        "client_error: client error: 403"
                    ),
                )
            ],
        )

    return recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)


@contextmanager
def synthetic_monitoring_sources() -> Iterator[dict[str, Recorder]]:
    with serve(nodes_server) as primary, serve(denied_server) as denied:
        yield {SOURCE: primary.recorder, DENIED_SOURCE: denied.recorder}
