"""C8 selected tools against the locked official MCP; Grafana is isolated/optional."""

import hashlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
import pytest
from agents.mcp import MCPServerStreamableHttp
from mcp.types import CallToolResult, ResourceLink, TextContent
from tests.sdk_core.grafana_fixture import (
    BINARY_SHA256,
    grafana_backend,
    official_grafana,
)

from xiaowei.config import MCPServerConfig
from xiaowei.governance import schema_shape
from xiaowei.mcp import _client_factory

pytestmark = pytest.mark.loopback
SCHEMAS = {
    "create_annotation": "20f67554334bf68669dc5584592c3c455184a8d558ab93e24d06d8bb43c33493",
    "create_datasource": "0598f5d75f84c4ce5b89ec7d36683f1df33c27e82f5a93966e76664dd21e2f26",
    "get_annotations": "8f3f4519f6e8e4a40ffa3923dfbd912f0d8e5f2a8d2ef2b2dcae84068a50dd17",
    "get_datasource": "25e98dfb8425e0f07867ad457c44657732e00daa7b3b09c8d9af98902d1d1927",
    "update_datasource": "b9a4ec959223882f7b29236fdcc9a1a575031e3665a24ad388e7826390d65e1c",
}


@pytest.fixture
def grafana_binary() -> Path:
    path = os.getenv("XW_TEST_GRAFANA_MCP_BIN")
    if not path:
        pytest.skip("未配置已校验的官方 Grafana MCP v2.0.2 二进制")
    binary = Path(path)
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == BINARY_SHA256
    return binary


def client(url: str, token: str | None = "synthetic-mcp-token") -> MCPServerStreamableHttp:  # noqa: S107
    config = MCPServerConfig(
        server_id="grafana-c8",
        url=url,
        auth_ref=None,
        timeout_seconds=5,
        max_response_bytes=128_000,
        allowed_tools={"create_annotation": "grafana.create_annotation"},
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


def payload(result: CallToolResult) -> dict[str, object]:
    assert not result.is_error and result.result_type == "complete"
    assert result.structured_content is None
    assert isinstance(result.content[0], TextContent)
    return json.loads(result.content[0].text)


async def test_c8_category_registers_unselected_delete_and_selected_schemas(
    grafana_binary: Path,
) -> None:
    with (
        grafana_backend() as (port, state),
        official_grafana(grafana_binary, port, c8_write=True) as url,
    ):
        async with client(url) as mcp:
            tools = {tool.name: tool for tool in await mcp.list_tools()}
            assert {"delete_annotation", "update_annotation"} <= tools.keys()
            assert "grafana_api_request" not in tools
            for name, expected in SCHEMAS.items():
                shape = schema_shape(tools[name].input_schema, ignore_extra_flags=True)
                digest = hashlib.sha256(
                    json.dumps(
                        shape,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
                assert digest == expected
            before = len(state.requests)
            result = await mcp.call_tool(
                "create_annotation", {"text": "probe", "time": 1720000000000}
            )
            assert result.is_error
            assert len(state.requests) == before + 1
            assert state.requests[-1][0] == "/api/annotations"


@pytest.mark.parametrize("token", [None, "wrong-token"])
async def test_c8_write_endpoint_rejects_missing_or_wrong_token_without_io(
    grafana_binary: Path, token: str | None
) -> None:
    with (
        grafana_backend() as (port, state),
        official_grafana(grafana_binary, port, c8_write=True) as url,
    ):
        before = len(state.requests)
        async with httpx2.AsyncClient(trust_env=False) as http:
            response = await http.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "create_annotation",
                        "arguments": {"text": "must not write", "time": 1720000000000},
                    },
                },
                headers={} if token is None else {"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 401
        assert len(state.requests) == before


@pytest.fixture
async def isolated_api() -> AsyncIterator[httpx2.AsyncClient]:
    url = os.environ.get("XW_TEST_GRAFANA_C8_URL")
    if not url:
        pytest.skip("未配置本机隔离 Grafana C8 容器")
    endpoint = urlsplit(url)
    assert endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1"
    assert endpoint.port == 3308
    async with httpx2.AsyncClient(
        base_url=url, auth=("admin", "synthetic-c8-local"), trust_env=False
    ) as api:
        yield api


async def test_official_annotation_create_returns_id_and_can_be_read_back(
    grafana_binary: Path, isolated_api: httpx2.AsyncClient
) -> None:
    name = f"c8-{uuid4().hex}"
    account = await isolated_api.post("/api/serviceaccounts", json={"name": name, "role": "Admin"})
    assert account.status_code == 201
    account_id = account.json()["id"]
    created_id: int | None = None
    try:
        token = await isolated_api.post(
            f"/api/serviceaccounts/{account_id}/tokens", json={"name": name}
        )
        assert token.status_code == 200
        with official_grafana(
            grafana_binary,
            3308,
            c8_write=True,
            upstream_token=token.json()["key"],
        ) as url:
            async with client(url) as mcp:
                when = 1720000000000
                created = payload(
                    await mcp.call_tool(
                        "create_annotation",
                        {"text": name, "time": when, "timeEnd": when, "tags": [name]},
                    )
                )
                assert created["Payload"]["message"] == "Annotation added"
                created_id = created["Payload"]["id"]
                assert isinstance(created_id, int) and created_id > 0
                rows = payload(
                    await mcp.call_tool(
                        "get_annotations",
                        {"from": when, "to": when, "tags": [name], "limit": 100},
                    )
                )["Payload"]
                assert any(row["id"] == created_id and row["text"] == name for row in rows)
    finally:
        if created_id is not None:
            await isolated_api.delete(f"/api/annotations/{created_id}")
        await isolated_api.delete(f"/api/serviceaccounts/{account_id}")


async def test_official_datasource_create_result_has_text_and_resource_link(
    grafana_binary: Path, isolated_api: httpx2.AsyncClient
) -> None:
    name = f"c8-{uuid4().hex}"
    account = await isolated_api.post("/api/serviceaccounts", json={"name": name, "role": "Admin"})
    assert account.status_code == 201
    account_id = account.json()["id"]
    uid: str | None = None
    try:
        token = await isolated_api.post(
            f"/api/serviceaccounts/{account_id}/tokens", json={"name": name}
        )
        assert token.status_code == 200
        with official_grafana(
            grafana_binary, 3308, c8_write=True, upstream_token=token.json()["key"]
        ) as url:
            async with client(url) as mcp:
                original = (await isolated_api.get("/api/datasources")).json()
                schema = payload(
                    await mcp.call_tool("create_datasource", {"name": "", "type": "prometheus"})
                )
                assert any(
                    f["key"] == "url" and f["required"] is True and f["type"] == "string"
                    for f in schema["fields"]
                )
                no_schema = payload(
                    await mcp.call_tool("create_datasource", {"name": "", "type": "testdata"})
                )
                assert no_schema["type"] == "testdata" and "fields" not in no_schema
                assert (await isolated_api.get("/api/datasources")).json() == original
                result = await mcp.call_tool(
                    "create_datasource",
                    {
                        "name": name,
                        "type": "prometheus",
                        "schemaReviewed": True,
                        "fields": {"url": "http://127.0.0.1:9090"},
                    },
                )
                assert not result.is_error and len(result.content) == 2
                assert isinstance(result.content[0], TextContent)
                assert isinstance(result.content[1], ResourceLink)
                body = json.loads(result.content[0].text)
                uid = body["uid"]
                assert body["name"] == name and isinstance(uid, str)
                current = payload(await mcp.call_tool("get_datasource", {"uid": uid}))
                assert current["uid"] == uid and current["url"] == "http://127.0.0.1:9090"
                update_schema = payload(await mcp.call_tool("update_datasource", {"uid": uid}))
                assert update_schema["type"] == "prometheus"
                assert any(f["key"] == "url" for f in update_schema["fields"])
                unchanged = payload(await mcp.call_tool("get_datasource", {"uid": uid}))
                assert unchanged == current
    finally:
        if uid is not None:
            await isolated_api.delete(f"/api/datasources/uid/{uid}")
        await isolated_api.delete(f"/api/serviceaccounts/{account_id}")


async def test_unknown_plugin_schema_probe_can_send_a_write_request(
    grafana_binary: Path,
) -> None:
    """创建工具的无 schemaReviewed 分支不是通用只读预检。"""
    with (
        grafana_backend() as (port, state),
        official_grafana(grafana_binary, port, c8_write=True) as url,
    ):
        async with client(url) as mcp:
            before = sum(path == "/api/datasources" for path, _, _ in state.requests)
            result = await mcp.call_tool(
                "create_datasource",
                {"name": "synthetic-unknown", "type": "synthetic-unknown"},
            )
            assert result.is_error
            assert sum(path == "/api/datasources" for path, _, _ in state.requests) == before + 1
