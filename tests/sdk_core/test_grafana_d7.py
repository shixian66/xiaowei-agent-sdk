"""D7 formal serve/group path, real SDK/PG + locked official MCP/isolated Grafana."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from tests.sdk_core.grafana_d7_fixture import DashboardTraffic, dashboard_relay
from tests.sdk_core.grafana_fixture import official_grafana
from tests.sdk_core.test_alertmanager_s6 import group_channel as group_channel
from tests.sdk_core.test_alertmanager_s6 import group_send
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_grafana_d7_protocol import isolated_api as isolated_api
from tests.sdk_core.test_grafana_mcp_protocol import grafana_binary as grafana_binary
from tests.sdk_core.test_group_gateway import GroupChannel, runtime_group
from tests.sdk_core.test_group_identity import A, B
from tests.sdk_core.test_runtime import Env
from tests.sdk_core.test_runtime import env as env

from xiaowei.feishu import attributed
from xiaowei.models import AUDIENCES
from xiaowei.starrocks_tools import QUERY_TOOLS

pytestmark = pytest.mark.loopback
SOURCE = "grafana-write"
WRITE = f"{SOURCE}/update_dashboard"
CREATE = f"{SOURCE}/propose_create_dashboard"
UPDATE = f"{SOURCE}/propose_update_dashboard"
STATUS = f"{SOURCE}/get_action_status"
READ = ("get_dashboard_by_uid", "list_dashboard_versions", "update_dashboard")


def dashboard(uid: str, title: str = "Host CPU") -> dict[str, Any]:
    return {"uid": uid, "title": title, "schemaVersion": 39, "panels": []}


def d7_config(url: str, **changes: Any) -> dict[str, Any]:
    shared = sorted(QUERY_TOOLS | {CREATE, UPDATE, STATUS})
    values = {
        "mcp_servers": [
            {
                "server_id": SOURCE,
                "url": url,
                "auth_ref": "env:XW_TEST_D7_MCP_TOKEN",
                "timeout_seconds": 3,
                "max_response_bytes": 128_000,
                "allowed_tools": {n: f"grafana.{n}" for n in READ},
            }
        ],
        "feishu": runtime_group(tools=shared),
        "access": {
            "policy_version": "d7-1",
            "grants": {"operator": sorted(QUERY_TOOLS), B: [WRITE]},
        },
        "data_policy": {"input": {"max_bytes": 2000}, "model_tools": shared},
        "projection_bytes": dict.fromkeys(AUDIENCES, 60_000),
    }
    values["feishu"]["max_reply_chars"] = 3500
    values.update(changes)
    return values


@pytest.fixture
def traffic() -> Any:
    with dashboard_relay() as (port, state):
        yield port, state


@pytest.fixture
async def write_endpoint(
    grafana_binary: Path,
    isolated_api: httpx2.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    traffic: tuple[int, DashboardTraffic],
) -> AsyncIterator[str]:
    name = f"d7-{uuid4().hex}"
    response = await isolated_api.post("/api/serviceaccounts", json={"name": name, "role": "Admin"})
    assert response.status_code == 201
    account_id = response.json()["id"]
    try:
        token = await isolated_api.post(
            f"/api/serviceaccounts/{account_id}/tokens", json={"name": name}
        )
        assert token.status_code == 200
        monkeypatch.setenv("XW_TEST_D7_MCP_TOKEN", "synthetic-mcp-token")
        with official_grafana(
            grafana_binary,
            traffic[0],
            dashboard_write=True,
            upstream_token=token.json()["key"],
        ) as url:
            yield url
    finally:
        await isolated_api.delete(f"/api/serviceaccounts/{account_id}")


async def test_formal_group_create_and_conflicting_update_not_written(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
    traffic: tuple[int, DashboardTraffic],
) -> None:
    uid = f"d7-{uuid4().hex}"
    body = dashboard(uid)
    body["panels"] = [
        {
            "id": 1,
            "title": "CPU",
            "type": "timeseries",
            "fieldConfig": {
                "defaults": {"thresholds": {"steps": [{"color": "red", "value": 1.0}]}}
            },
            "targets": [{"refId": "A", "expr": 'rate(node_cpu_seconds_total{mode="idle"}[5m])'}],
        }
    ]
    args = {"dashboard_json": json.dumps(body), "folder_uid": "", "message": "host investigation"}
    asked = env.scripts.add(
        attributed(A, "创建主机看板"),
        tool_call(CREATE.replace("/", "__"), **args),
        cite(),
    )
    try:
        async with env.running(
            env.config(**d7_config(write_endpoint)), feishu_channel=group_channel
        ) as served:
            await group_send(env, group_channel, "创建主机看板", "om_create")
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            assert action_id and await env.scalar("SELECT state FROM xiaowei_action") == "pending"
            assert (await isolated_api.get(f"/api/dashboards/uid/{uid}")).status_code == 404
            material = message_text(group_channel.sends[-1][1])
            assert uid in material and "Host CPU" in material and "panels" in material
            assert SOURCE + "__update_dashboard" not in env.scripts.tools_seen(asked)[0]
            before = len(traffic[1].requests)
            await group_send(env, group_channel, f"/批准 {action_id}", "om_denied", sender=A)
            assert len(traffic[1].requests) == before and len(traffic[1].writes) == 0
            assert await env.scalar("SELECT state FROM xiaowei_action") == "pending"
            await group_send(env, group_channel, f"/批准 {action_id}", "om_approve", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "succeeded"
            actual = (await isolated_api.get(f"/api/dashboards/uid/{uid}")).json()["dashboard"]
            assert actual["version"] == 1 and actual["title"] == body["title"]
            assert "succeeded" in message_text(group_channel.sends[-1][1])
            await group_send(env, group_channel, f"/批准 {action_id}", "om_repeat", sender=B)
            assert (await isolated_api.get(f"/api/dashboards/uid/{uid}")).json()["dashboard"][
                "version"
            ] == 1
            env.scripts.add(
                attributed(A, "动作执行了吗"),
                tool_call(STATUS.replace("/", "__"), action_id=action_id),
                cite(),
            )
            await group_send(env, group_channel, "动作执行了吗", "om_status")
            reply = message_text(group_channel.sends[-1][1])
            assert action_id in reply and "succeeded" in reply

            changed = {**body, "title": "Approved edit"}
            env.scripts.add(
                attributed(A, "修改主机看板"),
                tool_call(
                    UPDATE.replace("/", "__"), **{**args, "dashboard_json": json.dumps(changed)}
                ),
                cite(),
            )
            await group_send(env, group_channel, "修改主机看板", "om_update")
            update_id = await env.scalar(
                "SELECT action_id FROM xiaowei_action WHERE proposal_tool_id=:tool",
                {"tool": UPDATE},
            )
            request_states = await env.scalar(
                "SELECT json_agg(json_build_array(reply_message_id, state, failure_code)) "
                "FROM xiaowei_request"
            )
            assert update_id, (
                message_text(group_channel.sends[-1][1]),
                request_states,
                traffic[1].requests[-4:],
            )
            external = await isolated_api.post(
                "/api/dashboards/db",
                json={
                    "dashboard": {**actual, "title": "External edit"},
                    "overwrite": False,
                    "folderUid": "",
                },
            )
            assert external.status_code == 200 and external.json()["version"] == 2
            await group_send(env, group_channel, f"/批准 {update_id}", "om_conflict", sender=B)
            assert (
                await env.scalar(
                    "SELECT state FROM xiaowei_action WHERE action_id=:id",
                    {"id": update_id},
                )
                == "rejected"
            )
            actual = (await isolated_api.get(f"/api/dashboards/uid/{uid}")).json()["dashboard"]
            assert actual["version"] == 2 and actual["title"] == "External edit"
            assert "未执行" in message_text(group_channel.sends[-1][1])
            assert (await served.ready())["status"] == "ready"
            assert len(env.scripts.calls) == 3
    finally:
        await isolated_api.delete(f"/api/dashboards/uid/{uid}")


async def test_dashboard_write_tools_never_enter_runner_scopes(env: Env) -> None:
    from xiaowei.runtime import _app_config

    app = _app_config(env.config(**d7_config("http://127.0.0.1:1")))
    assert WRITE not in frozenset().union(*app.purposes.values())
    assert {CREATE, UPDATE} <= app.purposes["query"]


async def test_unavailable_dashboard_source_hides_proposals(
    env: Env,
    group_channel: GroupChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.sdk_core.test_app import clarify

    monkeypatch.setenv("XW_TEST_D7_MCP_TOKEN", "synthetic-mcp-token")
    asked = env.scripts.add(attributed(A, "现在能创建吗"), clarify("监控源暂不可用"))
    async with env.running(
        env.config(**d7_config("http://127.0.0.1:1")), feishu_channel=group_channel
    ):
        await group_send(env, group_channel, "现在能创建吗", "om_unavailable")
        seen = env.scripts.tools_seen(asked)[0]
        assert SOURCE + "__propose_create_dashboard" not in seen
        assert SOURCE + "__propose_update_dashboard" not in seen
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0


@pytest.mark.parametrize(
    "failure,written",
    [
        ("500", False),
        ("timeout", True),
        ("save_mismatch", True),
        ("read_mismatch", True),
        ("read_type_mismatch", True),
        ("read_missing_null", True),
    ],
)
async def test_formal_write_unknown_is_not_replayed(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
    traffic: tuple[int, DashboardTraffic],
    failure: str,
    written: bool,
) -> None:
    uid = f"d7-{uuid4().hex}"
    state = traffic[1]
    body = dashboard(uid)
    if failure == "read_missing_null":
        body["description"] = None
    if failure == "read_type_mismatch":
        body["panels"] = [{"id": 1, "title": "CPU", "type": "timeseries", "targets": []}]
    args = {"dashboard_json": json.dumps(body), "folder_uid": "", "message": "create"}
    env.scripts.add(attributed(A, "提出"), tool_call(CREATE.replace("/", "__"), **args), cite())
    try:
        async with env.running(
            env.config(**d7_config(write_endpoint)), feishu_channel=group_channel
        ) as served:
            await group_send(env, group_channel, "提出", "om_p")
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            assert action_id and len(state.writes) == 0
            state.behavior = failure
            await group_send(env, group_channel, f"/批准 {action_id}", "om_a", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "unknown"
            assert await env.scalar("SELECT result FROM xiaowei_action") is None
            assert len(state.writes) == 1
            response = await isolated_api.get(f"/api/dashboards/uid/{uid}")
            assert response.status_code == (200 if written else 404)
            if written:
                assert response.json()["dashboard"]["version"] == 1
            receipt = message_text(group_channel.sends[-1][1])
            assert "未知" in receipt and "核对" in receipt and "工具执行失败" not in receipt
            assert (
                await env.scalar(
                    "SELECT count(*) FROM xiaowei_evidence WHERE tool_id=:tool",
                    {"tool": WRITE},
                )
                == 0
            )
            await group_send(env, group_channel, f"/批准 {action_id}", "om_again", sender=B)
            assert len(state.writes) == 1 and len(env.scripts.calls) == 1
            assert (await served.ready())["status"] == "ready"
    finally:
        await isolated_api.delete(f"/api/dashboards/uid/{uid}")


@pytest.mark.parametrize("tool", [CREATE, WRITE])
async def test_formal_web_cannot_propose_or_write_dashboards(
    env: Env,
    write_endpoint: str,
    traffic: tuple[int, DashboardTraffic],
    tool: str,
    group_channel: GroupChannel,
) -> None:
    config = d7_config(write_endpoint)
    config["access"]["grants"]["operator"] += [CREATE, WRITE]
    config["data_policy"]["model_tools"] += [WRITE]
    async with env.running(env.config(**config), feishu_channel=group_channel) as served:
        await served.page()
        before = len(traffic[1].requests)
        asked = env.scripts.add(
            "Web创建看板",
            tool_call(
                tool.replace("/", "__"),
                dashboard_json=json.dumps(dashboard("web-forbidden")),
                folder_uid="",
                message="create",
            ),
        )
        assert (await served.turn(asked, "query")).json()["state"] == "failed"
        assert not any(
            "propose_" in name or name.endswith("__update_dashboard")
            for name in env.scripts.tools_seen(asked)[0]
        )
        assert len(traffic[1].requests) == before and len(traffic[1].writes) == 0
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0


async def test_formal_update_success_then_atomic_race_is_rejected(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
    traffic: tuple[int, DashboardTraffic],
) -> None:
    uid = f"d7-{uuid4().hex}"
    body = dashboard(uid)
    assert (
        await isolated_api.post("/api/dashboards/db", json={"dashboard": body, "overwrite": False})
    ).status_code == 200
    args = {
        "dashboard_json": json.dumps({**body, "title": "Approved title"}),
        "folder_uid": "",
        "message": "edit",
    }
    env.scripts.add(attributed(A, "修改"), tool_call(UPDATE.replace("/", "__"), **args), cite())
    try:
        async with env.running(
            env.config(**d7_config(write_endpoint)), feishu_channel=group_channel
        ):
            await group_send(env, group_channel, "修改", "om_update")
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            assert action_id and len(traffic[1].writes) == 0
            await group_send(env, group_channel, f"/批准 {action_id}", "om_approve", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "succeeded"
            actual = (await isolated_api.get(f"/api/dashboards/uid/{uid}")).json()["dashboard"]
            assert actual["title"] == "Approved title" and actual["version"] == 2
            assert len(traffic[1].writes) == 1
            wire = traffic[1].writes[0]
            assert wire["dashboard"]["version"] == 1 and wire.get("overwrite", False) is False
            assert not {"operations", "uid", "userId"} & wire.keys()
            saved_plan = json.loads(await env.scalar("SELECT plan FROM xiaowei_action"))["plan"]
            before = json.loads(saved_plan["change"])["before"]
            assert before["dashboard"]["title"] == "Host CPU" and before["folderUid"] == ""
            assert (
                "before.dashboard" in saved_plan["rollback"]
                and "另次群审批" in saved_plan["rollback"]
            )
            env.scripts.add(
                attributed(A, "竞争修改"),
                tool_call(
                    UPDATE.replace("/", "__"),
                    **{
                        **args,
                        "dashboard_json": json.dumps({**body, "title": "Must not overwrite"}),
                    },
                ),
                cite(),
            )
            await group_send(env, group_channel, "竞争修改", "om_race_p")
            racing_id = await env.scalar(
                "SELECT action_id FROM xiaowei_action WHERE state='pending'"
            )
            assert racing_id
            traffic[1].behavior = "race"
            await group_send(env, group_channel, f"/批准 {racing_id}", "om_race_a", sender=B)
            assert (
                await env.scalar(
                    "SELECT state FROM xiaowei_action WHERE action_id=:id", {"id": racing_id}
                )
                == "rejected"
            )
            actual = (await isolated_api.get(f"/api/dashboards/uid/{uid}")).json()["dashboard"]
            assert actual["version"] == 3 and actual["title"] == "External racing edit"
            assert len(traffic[1].writes) == 2
            assert "未执行" in message_text(group_channel.sends[-1][1])
            assert len(env.scripts.calls) == 2
    finally:
        await isolated_api.delete(f"/api/dashboards/uid/{uid}")


@pytest.mark.parametrize("change", ["no_auth", "no_versions", "mixed_categories"])
async def test_write_endpoint_prerequisites_are_configuration_gates(env: Env, change: str) -> None:
    from pydantic import ValidationError

    config = d7_config("http://127.0.0.1:1")
    server = config["mcp_servers"][0]
    if change == "no_auth":
        server["auth_ref"] = None
    elif change == "no_versions":
        del server["allowed_tools"]["list_dashboard_versions"]
    else:
        server["allowed_tools"]["list_datasources"] = "grafana.list_datasources"
    with pytest.raises(ValidationError):
        env.config(**config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("dashboard_json", '{"uid":"v2","title":"native","elements":{},"layout":{}}'),
        ("folder_uid", "../outside"),
        ("message", ""),
        (
            "dashboard_json",
            '{"uid":"large","title":"large","panels":[],"description":"' + "x" * 16000 + '"}',
        ),
    ],
)
async def test_invalid_proposals_have_zero_tool_io(
    env: Env,
    write_endpoint: str,
    traffic: tuple[int, DashboardTraffic],
    group_channel: GroupChannel,
    field: str,
    value: str,
) -> None:
    from tests.sdk_core.test_app import clarify

    args = {
        "dashboard_json": json.dumps(dashboard("invalid")),
        "folder_uid": "",
        "message": "create",
    }
    args[field] = value
    env.scripts.add(
        attributed(A, "提出"), tool_call(CREATE.replace("/", "__"), **args), clarify("参数需修改")
    )
    async with env.running(env.config(**d7_config(write_endpoint)), feishu_channel=group_channel):
        before = len(traffic[1].requests)
        await group_send(env, group_channel, "提出", "om_invalid")
        assert len(traffic[1].requests) == before and len(traffic[1].writes) == 0
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0


async def test_restore_requires_new_action_and_uses_latest_version(
    env: Env,
    write_endpoint: str,
    isolated_api: httpx2.AsyncClient,
    group_channel: GroupChannel,
    traffic: tuple[int, DashboardTraffic],
) -> None:
    uid = f"d7-{uuid4().hex}"
    folder = f"d7-{uuid4().hex}"
    created_folder = await isolated_api.post("/api/folders", json={"uid": folder, "title": folder})
    assert created_folder.status_code == 200
    assert (
        await isolated_api.post(
            "/api/dashboards/db",
            json={"dashboard": dashboard(uid), "folderUid": folder, "overwrite": False},
        )
    ).status_code == 200
    try:
        async with env.running(
            env.config(**d7_config(write_endpoint)), feishu_channel=group_channel
        ):
            env.scripts.add(
                attributed(A, "修改"),
                tool_call(
                    UPDATE.replace("/", "__"),
                    dashboard_json=json.dumps(dashboard(uid, "New title")),
                    folder_uid=folder,
                    message="edit",
                ),
                cite(),
            )
            await group_send(env, group_channel, "修改", "om_edit")
            edit_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            await group_send(env, group_channel, f"/批准 {edit_id}", "om_edit_a", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "succeeded"
            stored = json.loads(await env.scalar("SELECT plan FROM xiaowei_action"))["plan"]
            before = json.loads(stored["change"])["before"]
            assert before["folderUid"] == folder
            env.scripts.add(
                attributed(A, "恢复原版"),
                tool_call(
                    UPDATE.replace("/", "__"),
                    dashboard_json=json.dumps(before["dashboard"]),
                    folder_uid=before["folderUid"],
                    message="restore prior approved definition",
                ),
                cite(),
            )
            await group_send(env, group_channel, "恢复原版", "om_restore")
            restore_id = await env.scalar(
                "SELECT action_id FROM xiaowei_action WHERE state='pending'"
            )
            assert restore_id and restore_id != edit_id and len(traffic[1].writes) == 1
            assert (await isolated_api.get(f"/api/dashboards/uid/{uid}")).json()["dashboard"][
                "title"
            ] == "New title"
            await group_send(env, group_channel, f"/批准 {restore_id}", "om_restore_a", sender=B)
            assert (
                await env.scalar(
                    "SELECT state FROM xiaowei_action WHERE action_id=:id", {"id": restore_id}
                )
                == "succeeded"
            )
            actual = (await isolated_api.get(f"/api/dashboards/uid/{uid}")).json()["dashboard"]
            assert actual["title"] == "Host CPU" and actual["version"] == 3
            assert (await isolated_api.get(f"/api/dashboards/uid/{uid}")).json()["meta"][
                "folderUid"
            ] == folder
            assert len(traffic[1].writes) == 2
            assert traffic[1].writes[1]["dashboard"]["version"] == 2
    finally:
        await isolated_api.delete(f"/api/dashboards/uid/{uid}")
        await isolated_api.delete(f"/api/folders/{folder}")


async def test_full_approval_material_over_capacity_creates_no_action(
    env: Env,
    write_endpoint: str,
    group_channel: GroupChannel,
    traffic: tuple[int, DashboardTraffic],
    isolated_api: httpx2.AsyncClient,
) -> None:
    uid = f"d7-{uuid4().hex}"
    body = {**dashboard(uid), "description": "\n" * 7000}
    created = await isolated_api.post(
        "/api/dashboards/db", json={"dashboard": body, "overwrite": False}
    )
    assert created.status_code == 200
    try:
        env.scripts.add(
            attributed(A, "提出"),
            tool_call(
                UPDATE.replace("/", "__"),
                dashboard_json=json.dumps({**body, "title": "Changed"}),
                folder_uid="",
                message="edit",
            ),
        )
        async with env.running(
            env.config(**d7_config(write_endpoint)), feishu_channel=group_channel
        ):
            before = len(traffic[1].requests)
            await group_send(env, group_channel, "提出", "om_material")
            assert len(traffic[1].requests) > before
            assert len(traffic[1].writes) == 0
            assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
            assert (
                await env.scalar(
                    "SELECT state FROM xiaowei_request WHERE reply_message_id='om_material'"
                )
                == "failed"
            )
    finally:
        await isolated_api.delete(f"/api/dashboards/uid/{uid}")


async def test_wire_schema_drift_hides_dashboard_actions_zero_tool_io(
    env: Env,
    write_endpoint: str,
    group_channel: GroupChannel,
    traffic: tuple[int, DashboardTraffic],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.mcp import MCPServerStreamableHttp
    from tests.sdk_core.test_app import clarify

    listed = MCPServerStreamableHttp.list_tools
    observed = []

    async def drift(server, *args, **kwargs):
        tools = await listed(server, *args, **kwargs)
        result = []
        for tool in tools:
            if tool.name == "update_dashboard":
                observed.append(tool.name)
                schema = json.loads(json.dumps(tool.input_schema))
                schema["properties"]["bypass"] = {"type": "boolean"}
                tool = tool.model_copy(update={"input_schema": schema})
            result.append(tool)
        return result

    monkeypatch.setattr(MCPServerStreamableHttp, "list_tools", drift)
    asked = env.scripts.add(attributed(A, "现在能创建吗"), clarify("写契约变化，暂不可用"))
    async with env.running(env.config(**d7_config(write_endpoint)), feishu_channel=group_channel):
        before = len(traffic[1].requests)
        await group_send(env, group_channel, "现在能创建吗", "om_drift")
        seen = env.scripts.tools_seen(asked)[0]
        assert observed == ["update_dashboard"]
        assert SOURCE + "__propose_create_dashboard" not in seen
        assert SOURCE + "__propose_update_dashboard" not in seen
        assert SOURCE + "__update_dashboard" not in seen
        assert len(traffic[1].requests) == before and len(traffic[1].writes) == 0
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
