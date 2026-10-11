"""D7 protocol gate only: locked official MCP, isolated Grafana; no product writes enabled."""

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
import pytest
from agents.mcp import MCPServerStreamableHttp
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent
from tests.sdk_core.grafana_fixture import grafana_backend, official_grafana
from tests.sdk_core.test_grafana_mcp_protocol import grafana_binary as grafana_binary

from xiaowei.config import MCPServerConfig
from xiaowei.mcp import _client_factory

pytestmark = pytest.mark.loopback
REMOTE = ("get_dashboard_by_uid", "list_dashboard_versions", "update_dashboard")
UNSELECTED = (
    "create_datasource",
    "create_annotation",
    "delete_annotation",
    "create_folder",
    "grafana_api",
)


def client(url: str, token: str | None = "synthetic-mcp-token") -> MCPServerStreamableHttp:  # noqa: S107
    config = MCPServerConfig(
        server_id="grafana-d7",
        url=url,
        auth_ref=None,
        timeout_seconds=5,
        max_response_bytes=128_000,
        allowed_tools={name: f"grafana.{name}" for name in REMOTE},
    )
    return MCPServerStreamableHttp(
        {
            "url": url,
            "httpx_client_factory": _client_factory(
                config, None if token is None else f"Bearer {token}"
            ),
        },
        max_retry_attempts=0,
    )


def payload(result: CallToolResult) -> object:
    assert not result.is_error
    assert result.result_type == "complete" and result.structured_content is None
    assert len(result.content) == 1
    item = result.content[0]
    assert isinstance(item, TextContent)
    return json.loads(item.text)


async def test_disable_write_override_does_not_register_dashboard_write(
    grafana_binary: Path,
) -> None:
    with (
        grafana_backend() as (port, state),
        official_grafana(
            grafana_binary,
            port,
            dashboard_override=True,
        ) as url,
    ):
        async with client(url) as mcp:
            names = {tool.name for tool in await mcp.list_tools()}
            assert "get_dashboard_by_uid" in names
            assert "update_dashboard" not in names
            assert state.requests == [
                ("/api/frontend/settings", {}, "Bearer synthetic-upstream-token")
            ]


async def test_dashboard_category_isolated_write_schema_and_unknown_tools(
    grafana_binary: Path,
) -> None:
    with (
        grafana_backend() as (port, state),
        official_grafana(
            grafana_binary,
            port,
            dashboard_write=True,
        ) as url,
    ):
        async with client(url) as mcp:
            tools = {tool.name: tool for tool in await mcp.list_tools()}
            assert set(tools) == {
                *REMOTE,
                "search_dashboards",
                "search_folders",
                "get_dashboard_summary",
                "get_dashboard_panel_queries",
                "get_dashboard_property",
            }
            assert {
                n
                for n, t in tools.items()
                if t.annotations and t.annotations.read_only_hint is False
            } == {"update_dashboard"}
            assert not set(UNSELECTED) & tools.keys()
            expected = json.loads(Path(__file__).with_name("grafana_d7_schemas.json").read_text())
            assert {name: tools[name].input_schema for name in REMOTE} == expected
            # Initialization fetches same-source settings once after caller authentication.
            assert state.requests == [
                ("/api/frontend/settings", {}, "Bearer synthetic-upstream-token")
            ]
            state.requests.clear()
            for name in UNSELECTED:
                with pytest.raises(MCPError) as error:
                    await mcp.call_tool(name, {})
                assert error.value.error.code == -32602
            assert state.requests == []  # valid caller credentials, zero upstream I/O


@pytest.mark.parametrize("token", [None, "wrong-token"])
async def test_write_endpoint_requires_correct_caller_auth_zero_upstream_io(
    grafana_binary: Path,
    token: str | None,
) -> None:
    with (
        grafana_backend() as (port, state),
        official_grafana(
            grafana_binary,
            port,
            dashboard_write=True,
        ) as url,
    ):
        async with httpx2.AsyncClient(trust_env=False) as http:
            headers = {} if token is None else {"Authorization": f"Bearer {token}"}
            response = await http.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "update_dashboard",
                        "arguments": {
                            "dashboard": {"title": "must-not-write", "panels": []},
                            "overwrite": False,
                        },
                    },
                },
                headers=headers,
            )
            assert response.status_code == 401
        assert state.requests == []


@pytest.fixture
async def isolated_api() -> AsyncIterator[httpx2.AsyncClient]:
    url = os.environ.get("XW_TEST_GRAFANA_D7_URL")
    if not url:
        pytest.skip("opt-in isolated official Grafana D7 container not configured")
    endpoint = urlsplit(url)
    assert endpoint.hostname == "127.0.0.1" and endpoint.port == 3307 and endpoint.scheme == "http"
    async with httpx2.AsyncClient(
        base_url=url,
        auth=("admin", "synthetic-d7-local"),
        trust_env=False,
    ) as api:
        health = await api.get("/api/health")
        assert health.status_code == 200 and health.json()["version"] == "13.2.3"
        yield api


async def test_real_grafana_classic_create_history_version_conflict_and_readback(
    grafana_binary: Path,
    isolated_api: httpx2.AsyncClient,
) -> None:
    api = isolated_api
    uid = f"d7-{uuid4().hex}"
    account = await api.post("/api/serviceaccounts", json={"name": uid, "role": "Admin"})
    assert account.status_code == 201
    aid = account.json()["id"]
    try:
        token = await api.post(f"/api/serviceaccounts/{aid}/tokens", json={"name": uid})
        assert token.status_code == 200
        with official_grafana(
            grafana_binary,
            3307,
            dashboard_write=True,
            upstream_token=token.json()["key"],
        ) as url:
            async with client(url) as mcp:
                args = {
                    "dashboard": {
                        "uid": uid,
                        "title": uid,
                        "version": 0,
                        "schemaVersion": 39,
                        "panels": [],
                    },
                    "folderUid": "",
                    "message": "local-create",
                    "overwrite": False,
                }
                saved = payload(await mcp.call_tool("update_dashboard", args))
                assert isinstance(saved, dict)
                assert (
                    saved["uid"] == uid and saved["version"] == 1 and saved["status"] == "success"
                )
                # New current API omits version; it must not be guessed from a client clock.
                current = payload(await mcp.call_tool("get_dashboard_by_uid", {"uid": uid}))
                assert isinstance(current, dict) and current["isV2"] is False
                assert "version" not in current["dashboard"] and "version" not in current["meta"]
                versions = payload(
                    await mcp.call_tool("list_dashboard_versions", {"uid": uid, "limit": 1})
                )
                assert (
                    isinstance(versions, list)
                    and len(versions) == 1
                    and versions[0]["version"] == 1
                )
                old = payload(
                    await mcp.call_tool("get_dashboard_by_uid", {"uid": uid, "version": 1})
                )
                assert (
                    isinstance(old, dict)
                    and old["dashboard"]["version"] == old["meta"]["version"] == 1
                )
                external = {**old["dashboard"], "title": "external-newer"}
                response = await api.post(
                    "/api/dashboards/db", json={**args, "dashboard": external}
                )
                assert response.status_code == 200 and response.json()["version"] == 2
                stale = await mcp.call_tool(
                    "update_dashboard",
                    {**args, "dashboard": {**old["dashboard"], "title": "must-not-overwrite"}},
                )
                assert stale.is_error and len(stale.content) == 1
                assert isinstance(stale.content[0], TextContent)
                assert (
                    stale.content[0].text == "unable to save dashboard: [POST /dashboards/db] "
                    "postDashboard (status 409): {}"
                )
                actual = (await api.get(f"/api/dashboards/uid/{uid}")).json()["dashboard"]
                assert actual["version"] == 2 and actual["title"] == "external-newer"
                fresh = payload(
                    await mcp.call_tool("get_dashboard_by_uid", {"uid": uid, "version": 2})
                )
                assert isinstance(fresh, dict)
                changed = {**fresh["dashboard"], "title": "approved-new"}
                updated = payload(
                    await mcp.call_tool("update_dashboard", {**args, "dashboard": changed})
                )
                assert isinstance(updated, dict) and updated["version"] == 3
                readback = payload(
                    await mcp.call_tool("get_dashboard_by_uid", {"uid": uid, "version": 3})
                )
                assert isinstance(readback, dict)
                assert readback["dashboard"] == {**changed, "version": 3}
                # An explicit false does not permit cloning/overwriting an existing UID.
                duplicate = await mcp.call_tool("update_dashboard", args)
                assert duplicate.is_error
                assert (await api.get(f"/api/dashboards/uid/{uid}")).json()["dashboard"][
                    "version"
                ] == 3
    finally:
        # Remove only this fixture's objects on the synthetic isolated container.
        await api.delete(f"/api/dashboards/uid/{uid}")
        await api.delete(f"/api/serviceaccounts/{aid}")


async def test_real_native_v2_full_json_reloads_latest_version_before_overwrite(
    grafana_binary: Path,
    isolated_api: httpx2.AsyncClient,
) -> None:
    """A stale v2 body is accepted after another edit; unsuitable for version-bound approval."""
    api = isolated_api
    uid = f"d7-{uuid4().hex}"
    account = await api.post("/api/serviceaccounts", json={"name": uid, "role": "Admin"})
    assert account.status_code == 201
    aid = account.json()["id"]
    try:
        token = await api.post(f"/api/serviceaccounts/{aid}/tokens", json={"name": uid})
        assert token.status_code == 200
        with official_grafana(
            grafana_binary,
            3307,
            dashboard_write=True,
            upstream_token=token.json()["key"],
        ) as url:
            async with client(url) as mcp:
                body = {
                    "uid": uid,
                    "title": "old-approved-body",
                    "timeSettings": {"from": "now-6h", "to": "now"},
                    "variables": [],
                    "elements": {},
                    "layout": {"kind": "GridLayout", "spec": {"items": []}},
                }
                args = {
                    "dashboard": body,
                    "folderUid": "",
                    "message": "v2-create",
                    "overwrite": False,
                }
                created = payload(await mcp.call_tool("update_dashboard", args))
                assert (
                    isinstance(created, dict) and created["uid"] == uid and created["version"] == 1
                )
                old = payload(await mcp.call_tool("get_dashboard_by_uid", {"uid": uid}))
                assert isinstance(old, dict) and old["isV2"] is True
                newer = {**old["dashboard"], "uid": uid, "title": "external-v2-edit"}
                saved = payload(
                    await mcp.call_tool(
                        "update_dashboard", {**args, "dashboard": newer, "overwrite": True}
                    )
                )
                assert isinstance(saved, dict) and saved["version"] == 2
                stale = {**old["dashboard"], "uid": uid, "title": "stale-overwrites-newer"}
                # The public tool takes no expected resourceVersion. It fetches v2's current
                # object internally, so an older approved definition replaces a newer edit.
                saved = payload(
                    await mcp.call_tool(
                        "update_dashboard", {**args, "dashboard": stale, "overwrite": True}
                    )
                )
                assert isinstance(saved, dict) and saved["version"] == 3
                actual = payload(await mcp.call_tool("get_dashboard_by_uid", {"uid": uid}))
                assert (
                    isinstance(actual, dict)
                    and actual["dashboard"]["title"] == "stale-overwrites-newer"
                )
    finally:
        await api.delete(f"/api/dashboards/uid/{uid}")
        await api.delete(f"/api/serviceaccounts/{aid}")
