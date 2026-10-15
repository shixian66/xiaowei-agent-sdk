"""D4 备选协议特征：真锁版 Grafana MCP，只连合成 loopback 数据源。"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from agents.mcp import MCPServerStreamableHttp
from mcp.types import CallToolResult, TextContent
from tests.sdk_core.grafana_fixture import grafana_backend, official_grafana
from tests.sdk_core.test_grafana_mcp_protocol import grafana_binary as grafana_binary

pytestmark = pytest.mark.loopback
ARGS = {
    "datasourceUid": "prom0",
    "startTime": "2024-07-03T09:46:40Z",
    "endTime": "2024-07-03T09:47:40Z",
    "stepSeconds": 30,
}
AUTH = {"Authorization": "Bearer synthetic-mcp-token"}


def payload(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error and result.result_type == "complete"
    assert result.structured_content is None and len(result.content) == 1
    item = result.content[0]
    assert isinstance(item, TextContent)
    return json.loads(item.text, parse_float=Decimal)


def arguments(query_type: str) -> dict[str, Any]:
    return {
        **ARGS,
        "queryType": query_type,
        "expr": '{__name__!=""}' + ("[1m]" if query_type == "instant" else ""),
    }


@pytest.mark.parametrize("query_type", ["range", "instant"])
@pytest.mark.parametrize(
    ("left", "right"),
    [
        (
            [
                {"metric": {"__name__": "a"}, "values": [[1720000000, "1"]]},
                {"metric": {"__name__": "b"}, "values": [[1720000000, "2"]]},
            ],
            [
                {
                    "metric": {"__name__": "a =>\n1 @[1720000000]\nb"},
                    "values": [[1720000000, "2"]],
                }
            ],
        ),
        (
            [
                {
                    "metric": {"__name__": "up", "a": "x", "b": "y"},
                    "values": [[1720000000, "1"]],
                }
            ],
            [
                {
                    "metric": {"__name__": "up", 'a="x", b': "y"},
                    "values": [[1720000000, "1"]],
                }
            ],
        ),
    ],
    ids=["different-series-count", "different-label-identities"],
)
async def test_official_grafana_distinguishes_original_name_collision_pairs(
    grafana_binary: Path, query_type: str, left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> None:
    args = arguments(query_type)
    endpoint = "query_range" if query_type == "range" else "query"
    path = f"/api/datasources/uid/prom0/resources/api/v1/{endpoint}"
    with grafana_backend() as (port, state):
        with official_grafana(grafana_binary, port, prometheus_query=True) as url:
            async with MCPServerStreamableHttp(
                {"url": url, "headers": AUTH}, max_retry_attempts=0
            ) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                schema = tools["query_prometheus"].input_schema
                assert schema["required"] == ["datasourceUid", "expr", "endTime"]
                assert schema["properties"]["stepSeconds"]["type"] == "integer"
                assert schema["additionalProperties"] is False
                assert "orgId" not in schema["properties"]
                assert tools["query_prometheus"].output_schema is None
                assert "update_dashboard" not in tools and "grafana_api_request" not in tools
                outputs = []
                for original in (left, right):
                    state.prometheus_response = json.dumps(
                        {
                            "status": "success",
                            "data": {"resultType": "matrix", "result": original},
                            "warnings": ["synthetic partial data"],
                        }
                    )
                    data = payload(await client.call_tool("query_prometheus", args))
                    assert data == {"data": original, "warnings": ["synthetic partial data"]}
                    outputs.append(data)
                assert outputs[0] != outputs[1]
                queries = [request for request in state.requests if request[0] == path]
                assert len(queries) == 2
                for _, params, _ in queries:
                    assert params["query"] == [args["expr"]]
                    if query_type == "range":
                        assert params == {
                            "query": [args["expr"]],
                            "start": ["1720000000"],
                            "end": ["1720000060"],
                            "step": ["30"],
                        }
                    else:
                        assert params == {"query": [args["expr"]], "time": ["1720000060"]}
                assert all(
                    header == "Bearer synthetic-upstream-token" for _, _, header in state.requests
                )
                assert {request[0] for request in state.requests} == {
                    "/api/frontend/settings",
                    "/api/datasources/uid/prom0",
                    path,
                }


@pytest.mark.parametrize("query_type", ["range", "instant"])
async def test_official_grafana_normalizes_api_numbers_and_omits_result_type(
    grafana_binary: Path, query_type: str
) -> None:
    # 原始数字词法直接进 HTTP，不先经过 Python float；验证接口保真，非真实源验收。
    raw = """{"status":"success","data":{"resultType":"matrix","result":[{
        "metric":{"__name__":"up","instance":"host1"},"values":[
        [1720000000.123456789,"9007199254740993"],
        [1720000001.999,"1.0000000000000001"],
        [1720000005,"NaN"],[1720000003,"+Inf"],
        [1720000007,"-Inf"],[1720000007,"-0"],
        [1720000009,"1e3"],[1720000010.123,"1.2345678901234567"]]}]},
        "warnings":["synthetic partial data"]}"""
    original = json.loads(raw, parse_float=Decimal)
    with grafana_backend() as (port, state):
        state.prometheus_response = raw
        with official_grafana(grafana_binary, port, prometheus_query=True) as url:
            async with MCPServerStreamableHttp(
                {"url": url, "headers": AUTH}, max_retry_attempts=0
            ) as client:
                data = payload(await client.call_tool("query_prometheus", arguments(query_type)))
                assert data == {
                    "data": [
                        {
                            "metric": {"__name__": "up", "instance": "host1"},
                            "values": [
                                [Decimal("1720000000.123"), "9007199254740992"],
                                [Decimal("1720000001.999"), "1"],
                                [1720000005, "NaN"],
                                [1720000003, "+Inf"],
                                [1720000007, "-Inf"],
                                [1720000007, "-0"],
                                [1720000009, "1000"],
                                [Decimal("1720000010.123"), "1.2345678901234567"],
                            ],
                        }
                    ],
                    "warnings": original["warnings"],
                }
                assert data["data"] != original["data"]["result"]
                assert "resultType" not in data


async def test_official_grafana_denied_datasource_never_queries_proxy(grafana_binary: Path) -> None:
    with grafana_backend() as (port, state):
        state.prometheus_response = (
            '{"status":"success","data":{"resultType":"matrix","result":[]}}'
        )
        state.status = 403
        with official_grafana(grafana_binary, port, prometheus_query=True) as url:
            async with MCPServerStreamableHttp(
                {"url": url, "headers": AUTH}, max_retry_attempts=0
            ) as client:
                result = await client.call_tool("query_prometheus", arguments("range"))
                assert result.is_error and result.structured_content is None
                assert state.requests
                assert not any(
                    "/resources/" in path or "/proxy/" in path for path, _, _ in state.requests
                )


async def test_official_grafana_query_error_does_not_become_empty_success(
    grafana_binary: Path,
) -> None:
    with grafana_backend() as (port, state):
        state.prometheus_response = (
            '{"status":"error","errorType":"timeout","error":"synthetic timeout"}'
        )
        with official_grafana(grafana_binary, port, prometheus_query=True) as url:
            async with MCPServerStreamableHttp(
                {"url": url, "headers": AUTH}, max_retry_attempts=0
            ) as client:
                result = await client.call_tool("query_prometheus", arguments("range"))
                assert result.is_error and result.structured_content is None
                assert isinstance(result.content[0], TextContent)
                assert "synthetic timeout" in result.content[0].text
                assert sum("/resources/" in path for path, _, _ in state.requests) == 1
