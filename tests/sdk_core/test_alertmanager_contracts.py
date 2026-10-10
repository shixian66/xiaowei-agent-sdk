"""A4 配置、SDK 函数绑定和 HTTP 生命周期的边界。"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from tests.sdk_core.alertmanager_fixture import SELECTED, backend
from tests.sdk_core.test_alertmanager_a4 import CALLS, SOURCE, a4_config
from tests.sdk_core.test_alertmanager_a4 import synthetic_basic as synthetic_basic
from tests.sdk_core.test_runtime import serve_config

from xiaowei.alertmanager import AlertmanagerAdapter, AlertmanagerConfig, alertmanager_catalog
from xiaowei.governance import Prechecked, ToolCatalog
from xiaowei.models import ALERTMANAGER_READ_POLICIES, ToolRequest
from xiaowei.runtime import ServeConfig, _app_config

pytestmark = pytest.mark.loopback


def source(url: str = "http://10.0.0.12:9093", **changes: Any) -> AlertmanagerConfig:
    return AlertmanagerConfig.model_validate(
        {**a4_config(url)["alertmanager_sources"][0], **changes}
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "file:///tmp/am"},
        {"url": "http://user:password@host:9093"},
        {"url": "http://host:9093?token=x"},
        {"url": "http://host:9093#x"},
        {"url": "http:///api/v2"},
        {"server_id": "local"},
        {"server_id": "with_under"},
        {"tools": []},
        {"tools": ["get_status", "get_status"]},
        {"tools": ["post_silence"]},
        {"tools": ["delete_silence"]},
        {"username_ref": None},
        {"password_ref": "plain-secret"},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"max_response_bytes": 0},
    ],
)
def test_invalid_configuration_is_rejected_without_io(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        source(**changes)


def test_one_authority_for_selected_tools_sources_and_policies() -> None:
    values = a4_config("http://10.0.0.12:9093")
    config = ServeConfig.model_validate(serve_config(8501, **values))
    contracts, policies = alertmanager_catalog(config.alertmanager_sources, config.projection_bytes)
    catalog = ToolCatalog(contracts, policies)
    assert {p.policy_id for p in policies} == ALERTMANAGER_READ_POLICIES
    assert {c.tool_id for c in catalog.contracts} == {f"{SOURCE}/{n}" for n in SELECTED}
    app = _app_config(config)
    assert {c.tool_id for c in contracts} <= app.purposes["query"]
    assert not {c.tool_id for c in contracts} & app.purposes["diagnose"]
    for changed in (SOURCE, config.targets[0].target_id):
        candidate = a4_config("http://10.0.0.12:9093")
        candidate["alertmanager_sources"].append(
            {**candidate["alertmanager_sources"][0], "server_id": changed}
        )
        with pytest.raises(ValueError):
            ServeConfig.model_validate(serve_config(8501, **candidate))
    # API 策略不能偷偷映射成 MCP 工具；MCP 的策略集合仍只含已核约的 Prometheus/Grafana。
    values["mcp_servers"] = [
        {
            "server_id": "other",
            "url": "http://10.0.0.13/mcp",
            "auth_ref": None,
            "timeout_seconds": 2,
            "max_response_bytes": 64000,
            "allowed_tools": {"get_status": "alertmanager.get_status"},
        }
    ]
    with pytest.raises(ValueError):
        ServeConfig.model_validate(serve_config(8501, **values))


def test_model_tool_inputs_are_explicit_and_no_write_contract_exists() -> None:
    config = source()
    contracts, policies = alertmanager_catalog(
        (config,), dict.fromkeys(("model", "session", "web", "feishu"), 2000)
    )
    assert len(contracts) == len(policies) == 6
    for policy in policies:
        assert all(field.is_required() for field in policy.arguments.model_fields.values())
        assert "url" not in policy.arguments.model_fields
        assert "method" not in policy.arguments.model_fields
    for contract in contracts:
        if contract.tool_id.endswith(("get_alerts", "get_alert_groups")):
            assert "正则" in contract.description and "包含" in contract.description
        if contract.tool_id.endswith("get_status"):
            assert "启动时间" in contract.description


def request() -> ToolRequest:
    return ToolRequest(
        tool_id=f"{SOURCE}/get_status",
        target_id=SOURCE,
        call_id="c1",
        tool_name=f"{SOURCE}__get_status",
        arguments={},
    )


@pytest.mark.parametrize("kind", ["normal", "cancel", "timeout"])
async def test_response_and_client_closed_on_normal_cancel_and_timeout(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    clients: list[httpx2.AsyncClient] = []
    responses: list[httpx2.Response] = []
    received_headers = asyncio.Event()
    original = httpx2.AsyncClient

    async def headers(response: httpx2.Response) -> None:
        responses.append(response)
        received_headers.set()

    def capture(**kwargs: Any) -> httpx2.AsyncClient:
        client = original(**kwargs, event_hooks={"response": [headers]})
        clients.append(client)
        return client

    monkeypatch.setattr(httpx2, "AsyncClient", capture)
    with backend() as (url, state):
        if kind != "normal":
            state.body_delay = 0.3
        adapter = AlertmanagerAdapter(
            source(url, timeout_seconds=0.1), clock=lambda: datetime.now(UTC)
        )
        async with adapter:
            execute = adapter.executes[(f"{SOURCE}/get_status", SOURCE)]
            assert isinstance(execute, Prechecked)
            task = asyncio.create_task(execute.run(execute.check(request())))
            await asyncio.wait_for(received_headers.wait(), 1)
            if kind == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif kind == "timeout":
                from xiaowei.governance import MonitoringReadError

                with pytest.raises(MonitoringReadError) as error:
                    await task
                assert error.value.reason == "timeout"
            else:
                assert (await task).payload["version"] == "0.34.1"
            assert len(responses) == 1 and responses[0].is_closed
            assert len(state.requests) == 1
        assert len(clients) == 1 and clients[0].is_closed


async def test_runtime_source_credentials_are_resolved_without_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with backend() as (url, state):
        monkeypatch.delenv("XW_TEST_AM_PASSWORD")
        adapter = AlertmanagerAdapter(source(url), clock=lambda: datetime.now(UTC))
        from xiaowei.config import SecretRefError

        with pytest.raises(SecretRefError):
            async with adapter:
                pytest.fail("缺少引用时不能装配")
        assert state.requests == []


async def test_group_read_limits_details_to_100_and_retains_source_count() -> None:
    with backend() as (url, state):
        state.payloads["/api/v2/alerts/groups"][0]["alerts"] *= 101
        async with AlertmanagerAdapter(source(url), clock=lambda: datetime.now(UTC)) as adapter:
            execute = adapter.executes[(f"{SOURCE}/get_alert_groups", SOURCE)]
            assert isinstance(execute, Prechecked)
            req = request().model_copy(
                update={
                    "tool_id": f"{SOURCE}/get_alert_groups",
                    "tool_name": f"{SOURCE}__get_alert_groups",
                    "arguments": CALLS["get_alert_groups"],
                }
            )
            result = await execute.run(execute.check(req))
            assert result.payload["data"][0]["alert_count"] == 101
            assert result.payload["data"][0]["group_index"] == 0
            assert result.payload["alerts"][0]["group_index"] == 0
            assert len(result.payload["alerts"][0]["alerts"]) == 100
            assert result.truncated and len(state.requests) == 1
