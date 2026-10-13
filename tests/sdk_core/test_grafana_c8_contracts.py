"""C8 请求凭据边界和远端数据源回读投影。"""

import json
from datetime import UTC

import pytest
from mcp.types import CallToolResult, TextContent

from xiaowei.governance import ToolRejectedError
from xiaowei.grafana_c8 import prepare_datasource, wire_payload_c8
from xiaowei.models import ToolRequest


def datasource_request(settings: dict[str, object]) -> ToolRequest:
    return ToolRequest(
        tool_id="grafana-c8/propose_create_datasource",
        target_id="grafana-c8",
        call_id="call-1",
        tool_name="grafana-c8__propose_create_datasource",
        arguments={
            "name": "example",
            "plugin_type": "any-installed-plugin",
            "settings_json": json.dumps(settings),
        },
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"fields": {"password": "synthetic-private-value"}},
        {"fields": {"apiKey": "synthetic-private-value"}},
        {"fields": {"api_key": "synthetic-private-value"}},
        {"fields": {"accessToken": "synthetic-private-value"}},
        {"fields": {"nested": {"clientSecret": "synthetic-private-value"}}},
        {"jsonData": {"password": "synthetic-private-value"}},
        {"fields": {"url": "https://operator:synthetic-private-value@grafana.local"}},
        {"fields": {"url": "https://grafana.local/api?apiKey=synthetic-private-value"}},
        {"fields": {"secureJsonData": {"password": "synthetic-private-value"}}},
    ],
)
def test_datasource_credentials_rejected_before_action(settings: dict[str, object]) -> None:
    with pytest.raises(ToolRejectedError):
        prepare_datasource(datasource_request(settings))


def test_nonsecret_dynamic_plugin_fields_are_allowed() -> None:
    request = datasource_request(
        {"fields": {"url": "http://metrics.internal", "bucket": "all-hosts"}}
    )
    assert prepare_datasource(request) is request


def test_remote_datasource_readback_never_projects_credentials() -> None:
    remote = {
        "id": 7,
        "uid": "example",
        "name": "example",
        "type": "any-installed-plugin",
        "url": "https://operator:synthetic-private-value@grafana.local",
        "access": "proxy",
        "version": 1,
        "orgId": 1,
        "jsonData": {
            "bucket": "all-hosts",
            "password": "synthetic-private-value",
            "accessToken": "synthetic-private-value",
            "nested": {"clientSecret": "synthetic-private-value"},
            "endpoint": "https://grafana.local/api?apiKey=synthetic-private-value",
        },
        "secureJsonData": {"password": "synthetic-private-value"},
    }
    result = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(remote))],
        is_error=False,
    )
    projected = wire_payload_c8(result, "get_datasource")
    text = str(projected["result_json"])
    assert "synthetic-private-value" not in text
    assert "all-hosts" in text
    assert json.loads(text)["digest"]


async def test_version_change_after_approval_claim_is_definitely_unwritten() -> None:
    from datetime import datetime
    from typing import Any

    from xiaowei.actions import ActionWriteRejectedError
    from xiaowei.config import MCPServerConfig
    from xiaowei.governance import ToolCatalog
    from xiaowei.grafana_c8 import datasource_actions, datasource_catalog
    from xiaowei.models import AUDIENCES, ToolObservation

    config = MCPServerConfig(
        server_id="grafana-c8",
        url="http://127.0.0.1:8000/mcp",
        auth_ref="env:XW_TEST_C8_MCP_TOKEN",
        timeout_seconds=3,
        max_response_bytes=128_000,
        allowed_tools={
            "get_datasource": "grafana.get_datasource",
            "update_datasource": "grafana.update_datasource",
        },
    )
    contracts, policies = datasource_catalog([config], dict.fromkeys(AUDIENCES, 60_000))
    catalog = ToolCatalog(contracts, policies)

    class FakeMCP:
        writes = 0

        def execute_for(self, tool_id: str, target_id: str) -> Any:
            assert target_id == "grafana-c8"
            if tool_id == "grafana-c8/get_datasource":

                async def read(_request: ToolRequest) -> ToolObservation:
                    return ToolObservation(
                        payload={"result_json": json.dumps({"uid": "example", "digest": "new"})},
                        captured_at=datetime.now(UTC),
                        truncated=False,
                    )

                return read

            async def write(_request: ToolRequest) -> ToolObservation:
                self.writes += 1
                raise AssertionError("stale action must not reach write")

            return write

    mcp = FakeMCP()
    binding = datasource_actions(mcp, [config], catalog, clock=lambda: datetime.now(UTC))[0]
    request = ToolRequest(
        tool_id="grafana-c8/update_datasource",
        target_id="grafana-c8",
        call_id="write-1",
        tool_name="grafana-c8__update_datasource",
        arguments={
            "uid": "example",
            "settings_json": json.dumps({"fields": {"url": "http://new.internal"}}),
            "expected_digest": "old",
        },
    )
    assert binding.execute is not None
    with pytest.raises(ActionWriteRejectedError):
        await binding.execute.run(binding.execute.check(request))
    assert mcp.writes == 0


def test_datasource_action_availability_stays_bound_to_its_source() -> None:
    from xiaowei.config import MCPServerConfig
    from xiaowei.governance import ToolCatalog
    from xiaowei.grafana_c8 import datasource_actions, datasource_catalog
    from xiaowei.models import AUDIENCES

    configs = [
        MCPServerConfig(
            server_id=sid,
            url=f"http://127.0.0.1:{port}/mcp",
            auth_ref="env:XW_TEST_C8_MCP_TOKEN",
            timeout_seconds=3,
            max_response_bytes=128_000,
            allowed_tools={
                "get_datasource": "grafana.get_datasource",
                "update_datasource": "grafana.update_datasource",
            },
        )
        for sid, port in (("grafana-a", 8000), ("grafana-b", 8001))
    ]
    contracts, policies = datasource_catalog(configs, dict.fromkeys(AUDIENCES, 60_000))

    class FakeMCP:
        def execute_for(self, _tool_id: str, target_id: str) -> None:
            if target_id == "grafana-a":
                raise ToolRejectedError("source a unavailable")

    from datetime import datetime

    bindings = datasource_actions(
        FakeMCP(), configs, ToolCatalog(contracts, policies), clock=lambda: datetime.now(UTC)
    )
    assert len(bindings) == 2
    assert bindings[0].available is not None and bindings[0].available() is False
    assert bindings[1].available is not None and bindings[1].available() is True
