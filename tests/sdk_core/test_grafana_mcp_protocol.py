"""G3 官方 v2.0.2 的公开协议，全部上游为合成 loopback API。"""

import hashlib
import json
import os
from pathlib import Path

import httpx2
import pytest
from agents.mcp import MCPServerStreamableHttp
from mcp.shared.exceptions import MCPError
from mcp.types import TextContent
from tests.sdk_core.grafana_fixture import (
    BINARY_SHA256,
    SELECTED,
    grafana_backend,
    official_grafana,
)

pytestmark = pytest.mark.loopback
CALLS = {
    "search_dashboards": {
        "query": "host",
        "folderUid": "",
        "tag": [],
        "starred": False,
        "limit": 2,
        "page": 1,
    },
    "get_dashboard_summary": {"uid": "host"},
    "get_dashboard_panel_queries": {"uid": "host", "panelId": 1},
    "list_datasources": {"type": "", "name": "", "limit": 2, "offset": 0},
    "get_annotations": {
        "from": 1720000000000,
        "to": 1720000060000,
        "limit": 2,
        "dashboardUid": "host",
        "tags": [],
        "matchAny": False,
    },
}


@pytest.fixture
def grafana_binary() -> Path:
    location = os.getenv("XW_TEST_GRAFANA_MCP_BIN")
    if not location:
        pytest.skip("未配置已校验的官方 Grafana MCP v2.0.2 二进制")
    binary = Path(location)
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == BINARY_SHA256
    return binary


async def test_official_contracts_results_pagination_and_separate_auth(
    grafana_binary: Path,
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        async with MCPServerStreamableHttp(
            {"url": url, "headers": {"Authorization": "Bearer synthetic-mcp-token"}},
            max_retry_attempts=0,
        ) as client:
            schemas = {tool.name: tool.input_schema for tool in await client.list_tools()}
            expected = json.loads(Path(__file__).with_name("grafana_schemas.json").read_text())
            assert {name: schemas[name] for name in SELECTED} == expected
            assert (
                not {
                    "query_prometheus",
                    "run_panel_query",
                    "update_dashboard",
                    "create_datasource",
                    "create_annotation",
                    "delete_annotation",
                    "grafana_api",
                }
                & schemas.keys()
            )
            for name, args in CALLS.items():
                before = len(state.requests)
                result = await client.call_tool(name, args)
                assert not result.is_error and result.result_type == "complete"
                assert result.structured_content is None and len(result.content) == 1
                item = result.content[0]
                assert isinstance(item, TextContent)
                data = json.loads(item.text)
                if name == "get_dashboard_panel_queries":
                    assert data[0]["query"] == 'cpu_ratio{instance="$host"}'
                elif name == "get_annotations":
                    assert data["Payload"][0]["text"] == "Synthetic deploy"
                elif name == "get_dashboard_summary":
                    assert data["meta"]["version"] == 7
                else:
                    assert data["total"] == 1 and data["hasMore"] is False
                # 第一份 Dashboard 读取有一次 capability discovery；之后均只有一次业务 GET。
                assert len(state.requests) - before == (2 if name == "get_dashboard_summary" else 1)
            assert all(
                header == "Bearer synthetic-upstream-token" for _, _, header in state.requests
            )
            state.count = 3000
            for name in ("search_dashboards", "list_datasources"):
                result = await client.call_tool(name, CALLS[name])
                assert isinstance(result.content[0], TextContent)
                data = json.loads(result.content[0].text)
                key = "dashboards" if name == "search_dashboards" else "datasources"
                assert len(data[key]) == 2 and data["hasMore"] is True
                assert data["total"] == (2 if name == "search_dashboards" else 3000)


@pytest.mark.parametrize("token", [None, "wrong-token"])
async def test_official_wrong_mcp_token_never_reaches_grafana(
    grafana_binary: Path, token: str | None
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        params = {
            "url": url,
            "headers": {} if token is None else {"Authorization": f"Bearer {token}"},
        }
        async with httpx2.AsyncClient() as http:
            response = await http.get(url, headers=params["headers"])
            assert response.status_code == 401
        with pytest.raises(ExceptionGroup) as caught:
            async with MCPServerStreamableHttp(params, max_retry_attempts=0):
                pytest.fail("错误凭据不应建立会话")
        assert caught.value.subgroup(MCPError) is not None
        assert state.requests == []
