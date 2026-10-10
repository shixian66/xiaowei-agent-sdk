"""G3 严格结果/错误反例：合成协议经真 SDK/正式治理，不放宽其他 MCP 的契约。"""

import json
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from tests.sdk_core.mcp_fixture import Recorder, recording, serve
from tests.sdk_core.test_app import tool_call
from tests.sdk_core.test_grafana_g3 import SOURCE, g3_config
from tests.sdk_core.test_grafana_g3 import synthetic_token as synthetic_token
from tests.sdk_core.test_prometheus_p2 import cite
from tests.sdk_core.test_runtime import Env, serve_config
from tests.sdk_core.test_runtime import env as env

from xiaowei.mcp import _accepts_input, _grafana_read_failure
from xiaowei.runtime import ServeConfig, _monitoring_catalog

pytestmark = pytest.mark.loopback


def test_all_selected_schema_shapes_match_locked_official_schema() -> None:
    from pathlib import Path

    locked = json.loads(Path(__file__).with_name("grafana_schemas.json").read_text())
    config = ServeConfig.model_validate(
        serve_config(8501, **g3_config("http://127.0.0.1:9999/mcp"))
    )
    contracts, _ = _monitoring_catalog(config)
    assert len(contracts) == 5
    for contract in contracts:
        assert _accepts_input(contract.input_schema, locked[contract.tool_id.split("/")[1]])


@pytest.mark.parametrize(
    "error",
    [
        "get annotations: [GET /annotations][404] getAnnotationsNotFound {}",
        "get annotations: [POST /annotations][401] getAnnotationsUnauthorized {}",
        "get annotations: [GET /annotations][401] getAnnotationsInternalServerError {}",
        "get annotations: [GET /annotations][401] getAnnotationsUnauthorized not-json",
        "get annotations: [GET /annotations][401] getAnnotationsUnauthorized {} extra",
        "arbitrary failure 401",
    ],
)
def test_similar_unverified_errors_are_not_classified(error: str) -> None:
    result = CallToolResult(is_error=True, content=[TextContent(type="text", text=error)])
    assert _grafana_read_failure(result, "get_annotations", {}) is None


@pytest.mark.parametrize(
    "kind", ["wrong_uid", "missing_title", "wrong_type", "error", "multiple", "structured"]
)
async def test_malformed_result_aborts_formal_turn_with_zero_evidence(env: Env, kind: str) -> None:
    def factory(recorder: Recorder) -> Any:
        server = MCPServer("g3-malformed")

        @server.tool(structured_output=False)
        def get_dashboard_summary(uid: str) -> CallToolResult:
            recorder.tool_calls.append(("get_dashboard_summary", {"uid": uid}))
            data: dict[str, Any] = {
                "uid": uid,
                "title": "Host CPU",
                "panelCount": 0,
                "panels": [],
                "timeRange": {"from": "now-1h", "to": "now"},
            }
            if kind == "wrong_uid":
                data["uid"] = "other"
            elif kind == "missing_title":
                del data["title"]
            elif kind == "wrong_type":
                data["panelCount"] = "0"
            content = [TextContent(type="text", text=json.dumps(data))]
            if kind == "multiple":
                content += [TextContent(type="text", text="{}")]
            return CallToolResult(
                content=content,
                is_error=kind == "error",
                structured_content=data if kind == "structured" else None,
            )

        return recording(
            server.streamable_http_app(),
            recorder,
            token="synthetic-mcp-token",  # noqa: S106 - synthetic
        )

    with serve(factory) as source:
        config = env.config(**g3_config(source.url))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "结果不合约", tool_call(f"{SOURCE}__get_dashboard_summary", uid="host")
            )
            assert (await served.turn(message, "query")).json()["state"] == "failed"
        assert source.recorder.tool_calls == [("get_dashboard_summary", {"uid": "host"})]
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
        assert await env.scalar("SELECT count(*) FROM agent_messages") == 0


async def test_one_grafana_schema_drift_hides_only_that_tool(env: Env) -> None:
    def factory(recorder: Recorder) -> Any:
        server = MCPServer("g3-drift")

        @server.tool(structured_output=False)
        def get_dashboard_summary(uid: str) -> str:
            recorder.tool_calls.append(("get_dashboard_summary", {"uid": uid}))
            return json.dumps(
                {
                    "uid": uid,
                    "title": "Host CPU",
                    "panelCount": 0,
                    "panels": [],
                    "timeRange": {"from": "now", "to": "now"},
                }
            )

        @server.tool(structured_output=False)
        def get_dashboard_panel_queries(uid: str, panelId: str) -> str:  # noqa: N803 - drift
            recorder.tool_calls.append(
                ("get_dashboard_panel_queries", {"uid": uid, "panelId": panelId})
            )
            return "[]"

        return recording(
            server.streamable_http_app(),
            recorder,
            token="synthetic-mcp-token",  # noqa: S106 - synthetic
        )

    with serve(factory) as source:
        values = g3_config(source.url)
        values["mcp_servers"][0]["allowed_tools"] = {
            name: f"grafana.{name}"
            for name in ("get_dashboard_summary", "get_dashboard_panel_queries")
        }
        tools = sorted({f"{SOURCE}/{name}" for name in values["mcp_servers"][0]["allowed_tools"]})
        values["data_policy"]["model_tools"] = tools
        values["access"]["grants"] = {"operator": tools}
        async with env.running(env.config(**values)) as served:
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
            await served.page()
            message = env.scripts.add(
                "兼容工具仍可读", tool_call(f"{SOURCE}__get_dashboard_summary", uid="host"), cite()
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            visible = env.scripts.tools_seen(message)[0]
            assert f"{SOURCE}__get_dashboard_summary" in visible
            assert f"{SOURCE}__get_dashboard_panel_queries" not in visible
        assert source.recorder.tool_calls == [("get_dashboard_summary", {"uid": "host"})]
