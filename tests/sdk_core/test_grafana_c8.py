"""C8 formal group path: selected MCP writes through native approval and Evidence."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from tests.sdk_core.grafana_fixture import official_grafana
from tests.sdk_core.test_alertmanager_s6 import group_channel as group_channel
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_grafana_c8_protocol import (
    grafana_binary as grafana_binary,
)
from tests.sdk_core.test_grafana_c8_protocol import (
    isolated_api as isolated_api,
)
from tests.sdk_core.test_grafana_d7 import group_send
from tests.sdk_core.test_group_actions import request_execution
from tests.sdk_core.test_group_gateway import GroupChannel, runtime_group
from tests.sdk_core.test_group_identity import A, B
from tests.sdk_core.test_runtime import BUDGET_CONFIG, Env
from tests.sdk_core.test_runtime import env as env

from xiaowei.feishu import attributed
from xiaowei.models import AUDIENCES
from xiaowei.starrocks_tools import QUERY_TOOLS

pytestmark = pytest.mark.loopback
SOURCE = "grafana-c8"
CREATE = f"{SOURCE}/propose_create_annotation"
WRITE = f"{SOURCE}/create_annotation"
DS_CREATE = f"{SOURCE}/propose_create_datasource"
DS_UPDATE = f"{SOURCE}/propose_update_datasource"
DS_CREATE_WRITE = f"{SOURCE}/create_datasource"
DS_UPDATE_WRITE = f"{SOURCE}/update_datasource"
STATUS = f"{SOURCE}/get_action_status"


def c8_config(url: str, *, datasources: bool = False, **changes: Any) -> dict[str, Any]:
    shared = sorted(
        QUERY_TOOLS | {CREATE, STATUS} | ({DS_CREATE, DS_UPDATE} if datasources else set())
    )
    allowed = {
        "get_annotations": "grafana.get_annotations",
        "create_annotation": "grafana.create_annotation",
    }
    if datasources:
        allowed.update(
            {
                "get_datasource": "grafana.get_datasource",
                "create_datasource": "grafana.create_datasource",
                "update_datasource": "grafana.update_datasource",
            }
        )
    values = {
        "mcp_servers": [
            {
                "server_id": SOURCE,
                "url": url,
                "auth_ref": "env:XW_TEST_C8_MCP_TOKEN",
                "timeout_seconds": 3,
                "max_response_bytes": 128_000,
                "allowed_tools": allowed,
            }
        ],
        "feishu": runtime_group(tools=shared),
        "access": {
            "policy_version": "c8-1",
            "grants": {
                "operator": sorted(QUERY_TOOLS),
                B: [WRITE, *([DS_CREATE_WRITE, DS_UPDATE_WRITE] if datasources else [])],
            },
        },
        "data_policy": {"input": {"max_bytes": 2000}, "model_tools": shared},
        "projection_bytes": dict.fromkeys(AUDIENCES, 60_000),
    }
    values["feishu"]["max_reply_chars"] = 3500
    values.update(changes)
    return values


@pytest.fixture
async def write_endpoint(
    grafana_binary: Path,
    isolated_api: httpx2.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[str]:
    name = f"c8-{uuid4().hex}"
    account = await isolated_api.post("/api/serviceaccounts", json={"name": name, "role": "Admin"})
    assert account.status_code == 201
    account_id = account.json()["id"]
    try:
        token = await isolated_api.post(
            f"/api/serviceaccounts/{account_id}/tokens", json={"name": name}
        )
        assert token.status_code == 200
        monkeypatch.setenv("XW_TEST_C8_MCP_TOKEN", "synthetic-mcp-token")
        with official_grafana(
            grafana_binary, 3308, c8_write=True, upstream_token=token.json()["key"]
        ) as url:
            yield url
    finally:
        await isolated_api.delete(f"/api/serviceaccounts/{account_id}")


async def test_group_annotation_approval_writes_once_and_hides_other_remote_tools(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
) -> None:
    name = f"c8-{uuid4().hex}"
    when = 1720000000000
    args = {
        "text": name,
        "time": when,
        "time_end": when,
        "tags": [name],
        "dashboard_uid": "",
        "panel_id": 0,
    }
    asked = env.scripts.add(
        attributed(A, "给监控图加部署标记"),
        tool_call(CREATE.replace("/", "__"), **args),
        request_execution,
        cite(),
    )
    created_id: int | None = None
    try:
        async with env.running(
            env.config(**c8_config(write_endpoint)), feishu_channel=group_channel
        ) as served:
            await group_send(env, group_channel, "给监控图加部署标记", "om_c8_create")
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            assert action_id and await env.scalar("SELECT state FROM xiaowei_action") == "pending"
            seen = env.scripts.tools_seen(asked)[0]
            assert SOURCE + "__create_annotation" not in seen
            assert SOURCE + "__delete_annotation" not in seen
            assert SOURCE + "__update_annotation" not in seen
            assert SOURCE + "__grafana_api_request" not in seen
            assert name in message_text(group_channel.sends[-1][1])
            await group_send(env, group_channel, f"/批准 {action_id}", "om_c8_approve", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "succeeded"
            assert "succeeded" in message_text(group_channel.sends[-1][1])
            rows = await isolated_api.get(
                "/api/annotations", params={"from": when, "to": when, "tags": name}
            )
            assert rows.status_code == 200
            matching = [row for row in rows.json() if row["text"] == name]
            assert len(matching) == 1
            created_id = matching[0]["id"]
            env.scripts.add(
                attributed(A, "删除刚才的标记"),
                tool_call(f"{SOURCE}__delete_annotation", id=created_id),
                cite(),
            )
            await group_send(env, group_channel, "删除刚才的标记", "om_c8_forbidden")
            still_there = await isolated_api.get(
                "/api/annotations", params={"from": when, "to": when, "tags": name}
            )
            assert any(row["id"] == created_id for row in still_there.json())
            await group_send(env, group_channel, f"/批准 {action_id}", "om_c8_repeat", sender=B)
            rows = await isolated_api.get(
                "/api/annotations", params={"from": when, "to": when, "tags": name}
            )
            assert len([row for row in rows.json() if row["text"] == name]) == 1
            assert (await served.ready())["status"] == "ready"
    finally:
        if created_id is not None:
            await isolated_api.delete(f"/api/annotations/{created_id}")


async def test_reversed_annotation_time_rejected_before_remote_write(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
) -> None:
    name = f"c8-{uuid4().hex}"
    when = 1720000000000
    env.scripts.add(
        attributed(A, "加标记"),
        tool_call(
            CREATE.replace("/", "__"),
            text=name,
            time=when,
            time_end=when - 1,
            tags=[name],
            dashboard_uid="",
            panel_id=0,
        ),
        cite(),
    )
    async with env.running(env.config(**c8_config(write_endpoint)), feishu_channel=group_channel):
        await group_send(env, group_channel, "加标记", "om_c8_badtime")
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
        rows = await isolated_api.get(
            "/api/annotations", params={"from": when - 1, "to": when, "tags": name}
        )
        assert rows.status_code == 200 and rows.json() == []
        assert "时间" in message_text(group_channel.sends[-1][1])


async def test_group_datasource_create_update_and_conflict(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
) -> None:
    name = f"c8-{uuid4().hex}"
    create_args = {
        "name": name,
        "plugin_type": "prometheus",
        "settings_json": json.dumps({"fields": {"url": "http://127.0.0.1:9090"}}),
    }
    asked = env.scripts.add(
        attributed(A, "创建监控数据源"),
        tool_call(DS_CREATE.replace("/", "__"), **create_args),
        request_execution,
        cite(),
    )
    uid: str | None = None
    try:
        async with env.running(
            env.config(**c8_config(write_endpoint, datasources=True)),
            feishu_channel=group_channel,
        ):
            await group_send(env, group_channel, "创建监控数据源", "om_c8_ds_create")
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            assert action_id and await env.scalar("SELECT state FROM xiaowei_action") == "pending"
            seen = env.scripts.tools_seen(asked)[0]
            assert SOURCE + "__create_datasource" not in seen
            assert SOURCE + "__update_datasource" not in seen
            assert SOURCE + "__delete_annotation" not in seen
            material = message_text(group_channel.sends[-1][1])
            assert name in material and "127.0.0.1:9090" in material
            assert all(
                item["name"] != name for item in (await isolated_api.get("/api/datasources")).json()
            )
            await group_send(env, group_channel, f"/批准 {action_id}", "om_c8_ds_approve", sender=B)
            assert (
                await env.scalar(
                    "SELECT state FROM xiaowei_action WHERE action_id=:id", {"id": action_id}
                )
                == "succeeded"
            )
            sources = (await isolated_api.get("/api/datasources")).json()
            selected = [item for item in sources if item["name"] == name]
            assert len(selected) == 1
            uid = selected[0]["uid"]
            assert selected[0]["url"] == "http://127.0.0.1:9090"
            assert "待配置凭据" in message_text(group_channel.sends[-1][1])

            env.scripts.add(
                attributed(A, "修改连接地址"),
                tool_call(
                    DS_UPDATE.replace("/", "__"),
                    uid=uid,
                    settings_json=json.dumps({"fields": {"url": "http://127.0.0.1:9091"}}),
                ),
                request_execution,
                cite(),
            )
            await group_send(env, group_channel, "修改连接地址", "om_c8_ds_update")
            update_id = await env.scalar(
                "SELECT action_id FROM xiaowei_action WHERE proposal_tool_id=:tool",
                {"tool": DS_UPDATE},
            )
            assert update_id
            before = await isolated_api.get(f"/api/datasources/uid/{uid}")
            assert before.json()["url"] == "http://127.0.0.1:9090"
            await group_send(
                env, group_channel, f"/批准 {update_id}", "om_c8_ds_update_ok", sender=B
            )
            after = await isolated_api.get(f"/api/datasources/uid/{uid}")
            assert after.json()["url"] == "http://127.0.0.1:9091"
            assert (
                await env.scalar(
                    "SELECT state FROM xiaowei_action WHERE action_id=:id", {"id": update_id}
                )
                == "succeeded"
            )

            env.scripts.add(
                attributed(A, "再修改连接地址"),
                tool_call(
                    DS_UPDATE.replace("/", "__"),
                    uid=uid,
                    settings_json=json.dumps({"fields": {"url": "http://127.0.0.1:9092"}}),
                ),
                request_execution,
                cite(),
            )
            await group_send(env, group_channel, "再修改连接地址", "om_c8_ds_stale")
            stale_id = await env.scalar(
                "SELECT action_id FROM xiaowei_action "
                "WHERE proposal_tool_id=:tool AND action_id<>:previous",
                {"tool": DS_UPDATE, "previous": update_id},
            )
            assert stale_id and stale_id != update_id
            external = await isolated_api.put(
                f"/api/datasources/uid/{uid}",
                json={**after.json(), "url": "http://127.0.0.1:9191"},
            )
            assert external.status_code == 200
            await group_send(
                env, group_channel, f"/批准 {stale_id}", "om_c8_ds_stale_approve", sender=B
            )
            assert (
                await env.scalar(
                    "SELECT state FROM xiaowei_action WHERE action_id=:id", {"id": stale_id}
                )
                == "rejected"
            )
            assert (await isolated_api.get(f"/api/datasources/uid/{uid}")).json()[
                "url"
            ] == "http://127.0.0.1:9191"
    finally:
        if uid is not None:
            await isolated_api.delete(f"/api/datasources/uid/{uid}")


async def test_group_datasource_secret_field_rejected_without_action_or_write(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
) -> None:
    name = f"c8-{uuid4().hex}"
    hidden = "synthetic-private-value"
    env.scripts.add(
        attributed(A, "创建数据源"),
        tool_call(
            DS_CREATE.replace("/", "__"),
            name=name,
            plugin_type="any-installed-plugin",
            settings_json=json.dumps({"fields": {"apiKey": hidden}}),
        ),
        cite(),
    )
    async with env.running(
        env.config(**c8_config(write_endpoint, datasources=True)),
        feishu_channel=group_channel,
    ):
        await group_send(env, group_channel, "创建数据源", "om_c8_secret")
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
        assert all(
            item["name"] != name for item in (await isolated_api.get("/api/datasources")).json()
        )
        assert hidden not in message_text(group_channel.sends[-1][1])


async def test_web_cannot_request_c8_write_or_remote_delete(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
) -> None:
    name = f"c8-{uuid4().hex}"
    message = env.scripts.add(
        "创建数据源",
        tool_call(
            DS_CREATE.replace("/", "__"),
            name=name,
            plugin_type="prometheus",
            settings_json=json.dumps({"fields": {"url": "http://127.0.0.1:9090"}}),
        ),
        cite(),
    )
    async with env.running(
        env.config(**c8_config(write_endpoint, datasources=True)),
        feishu_channel=group_channel,
    ) as served:
        await served.page()
        response = await served.turn(message, mode="query", request_id="web-c8-write")
        assert response.status_code == 200
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
        assert all(
            item["name"] != name for item in (await isolated_api.get("/api/datasources")).json()
        )
        assert DS_CREATE.replace("/", "__") not in env.scripts.tools_seen(message)[0]


async def test_group_datasource_partial_jsondata_update_preserves_approved_existing_fields(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
) -> None:
    name = f"c8-{uuid4().hex}"
    created = await isolated_api.post(
        "/api/datasources",
        json={
            "name": name,
            "type": "prometheus",
            "access": "proxy",
            "url": "http://127.0.0.1:9090",
            "jsonData": {
                "httpMethod": "GET",
                "timeInterval": "30s",
                "apiKey": "",
                "custom": {"one": 1, "two": 2},
            },
        },
    )
    assert created.status_code in (200, 201)
    uid = created.json()["datasource"]["uid"]
    try:
        env.scripts.add(
            attributed(A, "只调整数据源的采样间隔"),
            tool_call(
                DS_UPDATE.replace("/", "__"),
                uid=uid,
                settings_json=json.dumps(
                    {"jsonData": {"timeInterval": "60s", "custom": {"two": 3}}}
                ),
            ),
            request_execution,
            cite(),
        )
        async with env.running(
            env.config(**c8_config(write_endpoint, datasources=True)),
            feishu_channel=group_channel,
        ):
            await group_send(env, group_channel, "只调整数据源的采样间隔", "om_c8_partial")
            saved = await env.scalar("SELECT plan FROM xiaowei_action")
            assert saved is not None
            parameters = json.loads(saved)["plan"]["parameters"]
            assert json.loads(parameters["settings_json"])["jsonData"] == {
                "httpMethod": "GET",
                "timeInterval": "60s",
                "apiKey": "",
                "custom": {"one": 1, "two": 3},
            }
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            await group_send(env, group_channel, f"/批准 {action_id}", "om_c8_partial_ok", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "succeeded"
            actual = await isolated_api.get(f"/api/datasources/uid/{uid}")
            assert actual.status_code == 200
            assert actual.json()["jsonData"] == {
                "httpMethod": "GET",
                "timeInterval": "60s",
                "apiKey": "",
                "custom": {"one": 1, "two": 3},
            }
    finally:
        await isolated_api.delete(f"/api/datasources/uid/{uid}")


async def test_group_datasource_jsondata_with_hidden_existing_value_rejected_before_write(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
) -> None:
    name = f"c8-{uuid4().hex}"
    hidden = "synthetic-private-value"
    created = await isolated_api.post(
        "/api/datasources",
        json={
            "name": name,
            "type": "prometheus",
            "access": "proxy",
            "url": "http://127.0.0.1:9090",
            "jsonData": {"apiKey": hidden, "httpMethod": "GET"},
        },
    )
    assert created.status_code in (200, 201)
    uid = created.json()["datasource"]["uid"]
    try:
        env.scripts.add(
            attributed(A, "调整数据源设置"),
            tool_call(
                DS_UPDATE.replace("/", "__"),
                uid=uid,
                settings_json=json.dumps({"jsonData": {"timeInterval": "60s"}}),
            ),
            cite(),
        )
        async with env.running(
            env.config(**c8_config(write_endpoint, datasources=True)),
            feishu_channel=group_channel,
        ):
            await group_send(env, group_channel, "调整数据源设置", "om_c8_hidden_existing")
            assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
            actual = await isolated_api.get(f"/api/datasources/uid/{uid}")
            assert actual.status_code == 200
            assert actual.json()["jsonData"] == {"apiKey": hidden, "httpMethod": "GET"}
            assert hidden not in message_text(group_channel.sends[-1][1])
    finally:
        await isolated_api.delete(f"/api/datasources/uid/{uid}")


async def test_datasource_schema_drift_hides_only_changed_action_without_tool_io(
    env: Env,
    write_endpoint: str,
    group_channel: GroupChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.mcp import MCPServerStreamableHttp
    from tests.sdk_core.test_app import clarify

    listed = MCPServerStreamableHttp.list_tools
    called = MCPServerStreamableHttp.call_tool
    remote_calls: list[str] = []

    async def drift(server, *args, **kwargs):
        tools = await listed(server, *args, **kwargs)
        result = []
        for tool in tools:
            if tool.name == "create_datasource":
                schema = json.loads(json.dumps(tool.input_schema))
                schema["properties"]["unexpected"] = {"type": "boolean"}
                tool = tool.model_copy(update={"input_schema": schema})
            result.append(tool)
        return result

    async def tracked(server, name, arguments):
        remote_calls.append(name)
        return await called(server, name, arguments)

    monkeypatch.setattr(MCPServerStreamableHttp, "list_tools", drift)
    monkeypatch.setattr(MCPServerStreamableHttp, "call_tool", tracked)
    asked = env.scripts.add(attributed(A, "现在能创建数据源吗"), clarify("写契约已变化"))
    async with env.running(
        env.config(**c8_config(write_endpoint, datasources=True)),
        feishu_channel=group_channel,
    ):
        await group_send(env, group_channel, "现在能创建数据源吗", "om_c8_drift")
        seen = env.scripts.tools_seen(asked)[0]
        assert SOURCE + "__propose_create_datasource" not in seen
        assert SOURCE + "__create_datasource" not in seen
        assert SOURCE + "__propose_update_datasource" in seen
        assert remote_calls == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0


async def test_group_datasource_unknown_field_is_rejected_before_action_or_write(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
) -> None:
    from tests.sdk_core.test_starrocks_tools import tool_outputs

    name = f"c8-{uuid4().hex}"
    original_url = "http://127.0.0.1:9090"
    created = await isolated_api.post(
        "/api/datasources",
        json={"name": name, "type": "prometheus", "access": "proxy", "url": original_url},
    )
    assert created.status_code in (200, 201)
    uid = created.json()["datasource"]["uid"]
    try:
        asked = env.scripts.add(
            attributed(A, "把数据源地址改为新地址"),
            tool_call(
                DS_UPDATE.replace("/", "__"),
                uid=uid,
                settings_json=json.dumps(
                    {"fields": {"url": "http://127.0.0.1:9091", "bogusKey": "ignored"}}
                ),
            ),
            cite(),
        )
        async with env.running(
            env.config(**c8_config(write_endpoint, datasources=True)),
            feishu_channel=group_channel,
        ):
            await group_send(env, group_channel, "把数据源地址改为新地址", "om_c8_unknown_key")
            assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
            current = await isolated_api.get(f"/api/datasources/uid/{uid}")
            assert current.status_code == 200 and current.json()["url"] == original_url
            feedback = json.loads(tool_outputs(env.scripts.calls[asked][1])[0])
            assert feedback["status"] == "rejected" and feedback["retry_this_turn"] is True
            assert "url" in feedback["allowed_fields"]
            assert "bogusKey" not in feedback["allowed_fields"]
    finally:
        await isolated_api.delete(f"/api/datasources/uid/{uid}")


async def test_group_datasource_without_schema_requires_top_level_url(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
) -> None:
    from tests.sdk_core.test_starrocks_tools import tool_outputs

    name = f"c8-{uuid4().hex}"
    url = "http://127.0.0.1:9090"
    asked = env.scripts.add(
        attributed(A, "创建测试数据源"),
        tool_call(
            DS_CREATE.replace("/", "__"),
            name=name,
            plugin_type="grafana-testdata-datasource",
            settings_json=json.dumps({"fields": {"url": url}}),
        ),
        tool_call(
            DS_CREATE.replace("/", "__"),
            name=name,
            plugin_type="grafana-testdata-datasource",
            settings_json=json.dumps({"url": url}),
        ),
        request_execution,
        cite(),
    )
    uid: str | None = None
    try:
        async with env.running(
            env.config(
                **c8_config(
                    write_endpoint,
                    datasources=True,
                    budget={**BUDGET_CONFIG, "max_tool_calls": 4},
                )
            ),
            feishu_channel=group_channel,
        ):
            await group_send(env, group_channel, "创建测试数据源", "om_c8_no_schema")
            feedback = json.loads(tool_outputs(env.scripts.calls[asked][1])[0])
            assert feedback["status"] == "rejected" and feedback["retry_this_turn"] is True
            assert "顶层" in feedback["reason"]
            assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 1
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            assert action_id and await env.scalar("SELECT state FROM xiaowei_action") == "pending"
            assert all(
                item["name"] != name for item in (await isolated_api.get("/api/datasources")).json()
            )
            await group_send(
                env, group_channel, f"/批准 {action_id}", "om_c8_no_schema_ok", sender=B
            )
            assert await env.scalar("SELECT state FROM xiaowei_action") == "succeeded"
            selected = [
                item
                for item in (await isolated_api.get("/api/datasources")).json()
                if item["name"] == name
            ]
            assert len(selected) == 1 and selected[0]["url"] == url
            uid = selected[0]["uid"]
    finally:
        if uid is not None:
            await isolated_api.delete(f"/api/datasources/uid/{uid}")


@pytest.mark.parametrize("fields", [{"url": 42}, {"isDefault": "yes"}])
async def test_group_datasource_field_with_wrong_root_type_is_rejected_before_action(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
    fields: dict[str, object],
) -> None:
    from tests.sdk_core.test_starrocks_tools import tool_outputs

    name = f"c8-{uuid4().hex}"
    asked = env.scripts.add(
        attributed(A, "创建监控数据源"),
        tool_call(
            DS_CREATE.replace("/", "__"),
            name=name,
            plugin_type="prometheus",
            settings_json=json.dumps({"fields": fields}),
        ),
        cite(),
    )
    async with env.running(
        env.config(**c8_config(write_endpoint, datasources=True)),
        feishu_channel=group_channel,
    ):
        await group_send(env, group_channel, "创建监控数据源", "om_c8_wrong_type")
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
        assert all(
            item["name"] != name for item in (await isolated_api.get("/api/datasources")).json()
        )
        feedback = json.loads(tool_outputs(env.scripts.calls[asked][1])[0])
        assert feedback["status"] == "rejected" and feedback["retry_this_turn"] is True
        assert "字段值类型" in feedback["reason"] and "url" in feedback["allowed_fields"]
