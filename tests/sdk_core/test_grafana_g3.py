"""G3 正式 serve/Web、真 SDK/治理/存储；官方 MCP，Grafana/模型为合成数据。"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.sdk_core.browser import launch
from tests.sdk_core.grafana_fixture import SELECTED, grafana_backend, official_grafana
from tests.sdk_core.mcp_fixture import serve
from tests.sdk_core.test_app import answer, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_grafana_mcp_protocol import CALLS
from tests.sdk_core.test_grafana_mcp_protocol import grafana_binary as grafana_binary
from tests.sdk_core.test_group_gateway import GroupChannel, raw_group_event, runtime_group
from tests.sdk_core.test_group_identity import CHAT, A
from tests.sdk_core.test_monitoring_r1a import query_server
from tests.sdk_core.test_prometheus_p2 import cite
from tests.sdk_core.test_runtime import Env, serve_config, until
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_web_browser import send, settled

from xiaowei.feishu import attributed
from xiaowei.models import AUDIENCES
from xiaowei.runtime import ServeConfig
from xiaowei.starrocks_tools import QUERY_TOOLS

pytestmark = pytest.mark.loopback
SOURCE = "grafana-prod"


def g3_config(url: str, *, granted: bool = True) -> dict[str, Any]:
    tools = sorted(QUERY_TOOLS | {f"{SOURCE}/{name}" for name in SELECTED})
    return {
        "mcp_servers": [
            {
                "server_id": SOURCE,
                "url": url,
                "auth_ref": "env:XW_TEST_GRAFANA_TOKEN",
                "timeout_seconds": 2,
                "max_response_bytes": 128_000,
                "allowed_tools": {name: f"grafana.{name}" for name in SELECTED},
            }
        ],
        "data_policy": {"input": {"max_bytes": 2000}, "model_tools": tools},
        "access": {
            "policy_version": "g3-1",
            "grants": {"operator": tools if granted else sorted(QUERY_TOOLS), "alice": tools},
        },
        "projection_bytes": dict.fromkeys(AUDIENCES, 60_000),
        "budget": {
            "max_turns": 8,
            "max_tool_calls": 6,
            "timeout_seconds": 30,
            "max_scope_checks": 1000,
        },
    }


@pytest.fixture(autouse=True)
def synthetic_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XW_TEST_GRAFANA_TOKEN", "synthetic-mcp-token")


def test_selected_grafana_read_mapping_rejects_write_proxy_and_wrong_policy() -> None:
    values = g3_config("http://10.0.0.12:8000/mcp")
    config = ServeConfig.model_validate(serve_config(8501, **values))
    assert set(config.mcp_servers[0].allowed_tools) == set(SELECTED)
    for name, policy in [
        ("update_dashboard", "grafana.update_dashboard"),
        ("query_prometheus", "grafana.query_prometheus"),
        ("get_annotations", "prometheus.query"),
    ]:
        values = g3_config("http://10.0.0.12:8000/mcp")
        values["mcp_servers"][0]["allowed_tools"][name] = policy
        with pytest.raises(ValueError):
            ServeConfig.model_validate(serve_config(8501, **values))


async def test_formal_web_definitions_evidence_and_secret_projection(
    env: Env, grafana_binary: Path
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        async with env.running(env.config(**g3_config(url))) as served:
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
            await served.page()
            state.requests.clear()  # 不计官方 Server 初始化时的设置读取
            message = env.scripts.add(
                "Host CPU 面板怎么算，近期有什么部署标记？",
                *[tool_call(f"{SOURCE}__{name}", **args) for name, args in CALLS.items()],
                cite("定义与标记不是实时健康；变量 host 尚未选择"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            assert len(body["delivery"]["facts"]) == 5
            assert all(
                fact["target_id"] == SOURCE and fact["captured_at"]
                for fact in body["delivery"]["facts"]
            )
            assert (
                "cpu_ratio" in body["delivery"]["content"]
                and "Synthetic deploy" in body["delivery"]["content"]
            )
            for secret in (
                "private-author",
                "private-email",
                "private-login",
                "secret-canary",
                "private-upstream",
            ):
                assert secret not in json.dumps(body)
            assert set(SELECTED) == {
                t.removeprefix(f"{SOURCE}__")
                for t in env.scripts.tools_seen(message)[0]
                if t.startswith(f"{SOURCE}__")
            }
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 5
        stored = await env.scalar(
            "SELECT string_agg(model_content || session_content || "
            "web_content || feishu_content, '') "
            "FROM xiaowei_evidence"
        )
        assert "private-" not in stored and "secret-canary" not in stored
        assert all(
            path.startswith(
                (
                    "/api/search",
                    "/api/dashboards",
                    "/api/datasources",
                    "/api/annotations",
                    "/apis",
                    "/api/frontend/settings",
                )
            )
            for path, _, _ in state.requests
        )


async def test_formal_web_hidden_unauthorized_tool_forced_call_has_zero_io(
    env: Env, grafana_binary: Path
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        async with env.running(env.config(**g3_config(url, granted=False))) as served:
            await served.page()
            state.requests.clear()  # 不计官方 Server 初始化时的设置读取
            message = env.scripts.add(
                "试图强行读未授权 Grafana",
                tool_call(f"{SOURCE}__get_dashboard_summary", uid="host"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "failed"
            assert not any(
                tool.startswith(f"{SOURCE}__") for tool in env.scripts.tools_seen(message)[0]
            )
            assert state.requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


@pytest.mark.parametrize("status", [401, 403, 500, 502])
@pytest.mark.parametrize("name", SELECTED)
async def test_formal_grafana_known_read_error_is_source_local_and_no_evidence(
    env: Env, grafana_binary: Path, status: int, name: str
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        state.status = status
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()  # 不计官方 Server 初始化时的设置读取
            message = env.scripts.add(
                "读取标记失败怎么办",
                tool_call(f"{SOURCE}__{name}", **CALLS[name]),
                lambda call: answer(
                    [], analysis="", advice="未经数据验证，请检查监控源连接与权限。"
                ),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            assert SOURCE in body["delivery"]["monitoring_notice"]
            assert "未经数据验证" in body["delivery"]["content"]
            assert "private detail" not in json.dumps(body)
            assert len(state.requests) == (
                2
                if name
                in {"list_datasources", "get_dashboard_summary", "get_dashboard_panel_queries"}
                else 1
            )
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


@pytest.mark.parametrize(
    "name,invalid",
    [
        ("search_dashboards", {"limit": 0}),
        ("search_dashboards", {"page": 0}),
        ("list_datasources", {"limit": 101}),
        ("list_datasources", {"offset": -1}),
        ("get_dashboard_summary", {"uid": ""}),
        ("get_dashboard_panel_queries", {"panelId": -1}),
        ("get_annotations", {"from": 1720000060001}),
        ("get_annotations", {"limit": 101}),
    ],
)
async def test_grafana_invalid_arguments_reject_before_io(
    env: Env, grafana_binary: Path, name: str, invalid: dict[str, Any]
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()  # 不计官方 Server 初始化时的设置读取
            message = env.scripts.add(
                "检查参数错误",
                tool_call(f"{SOURCE}__{name}", **{**CALLS[name], **invalid}),
                lambda call: answer([], analysis="", advice="参数不合法，请修改范围"),
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            assert state.requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_grafana_history_is_local_when_source_down_and_revocation_blocks_it(
    env: Env, grafana_binary: Path
) -> None:
    with grafana_backend() as (port, _state), official_grafana(grafana_binary, port) as url:
        config = env.config(**g3_config(url))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "保存面板定义", tool_call(f"{SOURCE}__get_dashboard_summary", uid="host"), cite()
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            cookies = httpx.Cookies(served.client.cookies)
            assert await served.finish() == 0
    async with env.running(config) as served:
        served.client.cookies.update(cookies)
        response = await served.client.get("/api/turns/r1")
        assert response.status_code == 200 and "Host CPU" in response.text
        assert (await served.ready())["status"] == "ready"
        assert await served.finish() == 0
    async with env.running(env.config(**g3_config(url, granted=False))) as served:
        served.client.cookies.update(cookies)
        assert (await served.client.get("/api/turns/r1")).status_code == 403


async def test_grafana_failure_does_not_block_prometheus_or_retry_failed_source(
    env: Env, grafana_binary: Path
) -> None:
    with (
        grafana_backend() as (port, state),
        official_grafana(grafana_binary, port) as url,
        serve(query_server) as prom,
    ):
        state.status = 403
        values = g3_config(url)
        values["mcp_servers"].append(
            {
                "server_id": "prometheus-prod",
                "url": prom.url,
                "auth_ref": None,
                "timeout_seconds": 2,
                "max_response_bytes": 64000,
                "allowed_tools": {"query": "prometheus.query"},
            }
        )
        values["data_policy"]["model_tools"].append("prometheus-prod/query")
        values["access"]["grants"]["operator"].append("prometheus-prod/query")
        async with env.running(env.config(**values)) as served:
            await served.page()
            state.requests.clear()  # 不计官方 Server 初始化时的设置读取
            message = env.scripts.add(
                "Grafana 不可读，查获准 Prometheus 的状态",
                tool_call(f"{SOURCE}__get_annotations", **CALLS["get_annotations"]),
                tool_call(f"{SOURCE}__list_datasources", **CALLS["list_datasources"]),
                tool_call("prometheus-prod__query", query="up"),
                cite("Grafana 未核实，Prometheus 仅证明本次查询范围"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            assert len(body["delivery"]["facts"]) == 1
            assert body["delivery"]["facts"][0]["target_id"] == "prometheus-prod"
            assert SOURCE in body["delivery"]["monitoring_notice"]
            assert len(state.requests) == 1
            assert prom.recorder.tool_calls == [("query", {"query": "up"})]
            cookies = httpx.Cookies(served.client.cookies)
            assert await served.finish() == 0
        values["access"]["grants"]["operator"] = sorted(QUERY_TOOLS | {"prometheus-prod/query"})
        async with env.running(env.config(**values)) as served:
            served.client.cookies.update(cookies)
            # 失败源的详情也必须通过当前源授权，不能通过其他源的成功证据绕过。
            assert (await served.client.get("/api/turns/r1")).status_code == 403


@pytest.mark.parametrize("name", ["search_dashboards", "list_datasources", "get_annotations"])
async def test_formal_grafana_pagination_is_partial_evidence(
    env: Env, grafana_binary: Path, name: str
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        state.count = 3000
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()  # 不计官方 Server 初始化时的设置读取
            message = env.scripts.add(
                "读取第一批并说明限制",
                tool_call(f"{SOURCE}__{name}", **CALLS[name]),
                cite("结果只覆盖本次读取，不能称全量或健康"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            fact = body["delivery"]["facts"][0]
            assert fact["truncated"] is True
            data = json.loads(fact["result_json"])
            assert (
                len(
                    data[
                        {
                            "search_dashboards": "dashboards",
                            "list_datasources": "datasources",
                            "get_annotations": "annotations",
                        }[name]
                    ]
                )
                == 2
            )
            assert "结果已截断" in body["delivery"]["content"]


async def test_unknown_grafana_error_and_wire_limit_abort_without_evidence(
    env: Env, grafana_binary: Path
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        state.status = 404
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()  # 不计官方 Server 初始化时的设置读取
            message = env.scripts.add(
                "未分类的读取错误", tool_call(f"{SOURCE}__get_dashboard_summary", uid="host")
            )
            assert (await served.turn(message, "query")).json()["state"] == "failed"
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
            assert await served.finish() == 0
        state.status = 200
        state.dashboard["dashboard"]["description"] = "oversized-definition" * 10000
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()  # 不计官方 Server 初始化时的设置读取
            message = env.scripts.add(
                "超限定义", tool_call(f"{SOURCE}__get_dashboard_summary", uid="host")
            )
            assert (await served.turn(message, "query", request_id="r2")).json()[
                "state"
            ] == "failed"
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_all_panels_and_source_annotations_are_optional_filters(
    env: Env, grafana_binary: Path
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()  # 不计官方 Server 初始化时的设置读取
            message = env.scripts.add(
                "查全部面板与源内标记",
                tool_call(f"{SOURCE}__get_dashboard_panel_queries", uid="host", panelId=0),
                tool_call(
                    f"{SOURCE}__get_annotations", **{**CALLS["get_annotations"], "dashboardUid": ""}
                ),
                cite(),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            panel = json.loads(body["delivery"]["facts"][0]["result_json"])
            assert panel["uid"] == "host" and panel["panelId"] == 0
            assert "id" not in panel["panels"][0] and "panelId" not in panel["panels"][0]
            annotations = [args for path, args, _ in state.requests if path == "/api/annotations"]
            assert len(annotations) == 1 and "dashboardUID" not in annotations[0]
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 2


async def test_search_error_with_reported_grafana_url_and_version(
    env: Env, grafana_binary: Path
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        state.public_url, state.version = "https://grafana.example", "12.2.0"
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()
            state.status = 403
            message = env.scripts.add(
                "搜索权限失效",
                tool_call(f"{SOURCE}__search_dashboards", **CALLS["search_dashboards"]),
                lambda call: answer([], analysis="", advice="未经数据验证，检查权限"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            assert SOURCE in body["delivery"]["monitoring_notice"]
            assert "grafana.example" not in json.dumps(body)
            assert len(state.requests) == 1


@pytest.mark.parametrize("name", ["search_dashboards", "list_datasources"])
async def test_grafana_second_last_and_empty_pages(
    env: Env, grafana_binary: Path, name: str
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        state.count = 3
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()
            for index, count in enumerate([2, 1, 0]):
                args = {
                    **CALLS[name],
                    **(
                        {"page": index + 1}
                        if name == "search_dashboards"
                        else {"offset": index * 2}
                    ),
                }
                message = env.scripts.add(
                    f"读取第{index + 1}页", tool_call(f"{SOURCE}__{name}", **args), cite()
                )
                body = (await served.turn(message, "query", request_id=f"r{index}")).json()
                assert body["state"] == "completed"
                fact = body["delivery"]["facts"][-1]
                data = json.loads(fact["result_json"])
                items = data["dashboards" if name == "search_dashboards" else "datasources"]
                assert len(items) == count
                assert data["hasMore"] is (index == 0)
                assert fact["truncated"] is (index == 0)
                assert data["total"] == (count if name == "search_dashboards" else 3)
                if count:
                    assert items[0]["uid"] == (
                        f"host{index * 2}" if name == "search_dashboards" else f"prom{index * 2}"
                    )
            assert len(state.requests) == 3


async def test_grafana_optional_filter_replay_and_no_unnecessary_queries(
    env: Env, grafana_binary: Path
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()
            message = env.scripts.add(
                "读取全部面板",
                tool_call(f"{SOURCE}__get_dashboard_panel_queries", uid="host", panelId=0),
                cite(),
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            before = len(state.requests)

            def followup(call: Any) -> list[Any]:
                past = [item for item in call.input if item.get("type") == "function_call"]
                assert len(past) == 1 and json.loads(past[0]["arguments"]) == {
                    "uid": "host",
                    "panelId": 0,
                }
                assert "不能证明它对应哪个 Prometheus" in call.instructions
                assert "StarRocks非分页结果被截断时" in call.instructions
                return cite("只解释上次定义；实时状态未核实")(call)

            next_message = env.scripts.add("解释刚才的定义，不再查询", followup)
            body = (await served.turn(next_message, "query", request_id="r2")).json()
            assert body["state"] == "completed"
            assert len(state.requests) == before
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1


async def test_grafana_projection_omits_whole_collection_with_partial_notice(
    env: Env, grafana_binary: Path
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        state.dashboard["dashboard"]["panels"] *= 700
        values = g3_config(url)
        values["mcp_servers"][0]["max_response_bytes"] = 512000
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "读取较大定义", tool_call(f"{SOURCE}__get_dashboard_summary", uid="host"), cite()
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            fact = body["delivery"]["facts"][0]
            data = json.loads(fact["result_json"])
            assert data["uid"] == "host" and "panels" not in data
            assert fact["truncated"] and "结果已截断" in body["delivery"]["content"]
            assert len(fact["result_json"].encode()) <= 60000


async def test_grafana_authorized_source_does_not_grant_all_tools(
    env: Env, grafana_binary: Path
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        values = g3_config(url)
        values["access"]["grants"]["operator"] = [f"{SOURCE}/get_dashboard_summary"]
        async with env.running(env.config(**values)) as served:
            await served.page()
            state.requests.clear()
            message = env.scripts.add(
                "强行读未授权标记",
                tool_call(f"{SOURCE}__get_annotations", **CALLS["get_annotations"]),
            )
            assert (await served.turn(message, "query")).json()["state"] == "failed"
            visible = env.scripts.tools_seen(message)[0]
            assert f"{SOURCE}__get_dashboard_summary" in visible
            assert f"{SOURCE}__get_annotations" not in visible
            assert not state.requests


@pytest.mark.parametrize("granted", [True, False])
async def test_group_grafana_definitions_and_scope(
    env: Env, grafana_binary: Path, granted: bool
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        tools = [f"{SOURCE}/{name}" for name in SELECTED] if granted else sorted(QUERY_TOOLS)
        config = env.config(**g3_config(url), feishu=runtime_group(tools=tools))
        channel = GroupChannel()
        message = env.scripts.add(
            attributed(A, "面板怎么计算"),
            tool_call(f"{SOURCE}__get_dashboard_panel_queries", uid="host", panelId=0),
            cite("只报告定义，实时状态未核实"),
        )
        async with env.running(config, feishu_channel=channel):
            state.requests.clear()
            channel.emit_raw_from_sdk_thread(raw_group_event(env, "面板怎么计算", "om_g3"))
            await until(lambda: len(channel.sends) == 1)
            destination, payload, _ = channel.sends[0]
            shown = message_text(payload)
            assert destination == CHAT
            if granted:
                assert SOURCE in shown and "cpu_ratio" in shown and "定义" in shown
                assert "private-" not in shown
            else:
                assert (
                    f"{SOURCE}__get_dashboard_panel_queries"
                    not in env.scripts.tools_seen(message)[0]
                )
                assert not state.requests
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == int(granted)


@pytest.mark.browser
async def test_browser_grafana_success_and_refusal(
    env: Env, grafana_binary: Path, chrome_binary: str
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        async with env.running(env.config(**g3_config(url))), launch(chrome_binary) as chrome:
            page = await chrome.page()
            await page.navigate(f"{env.origin}/")
            state.requests.clear()
            success = env.scripts.add(
                "浏览器看面板定义",
                tool_call(f"{SOURCE}__get_dashboard_panel_queries", uid="host", panelId=0),
                cite("实时状态未核实"),
            )
            await send(page, success, "query")
            shown = await settled(page, 0, "completed")
            assert SOURCE in shown and "cpu_ratio" in shown and "实时状态未核实" in shown
            before = len(state.requests)
            bad = env.scripts.add(
                "浏览器拒绝超限标记",
                tool_call(
                    f"{SOURCE}__get_annotations", **{**CALLS["get_annotations"], "limit": 101}
                ),
                lambda call: answer([], analysis="", advice="上限100，未执行；请缩小范围"),
            )
            await send(page, bad, "query")
            assert "未执行" in await settled(page, 1, "completed")
            assert len(state.requests) == before


async def test_grafana_group_resend_is_local_and_revocation_refuses(
    env: Env, grafana_binary: Path
) -> None:
    from lark_channel.channel.errors import FeishuChannelErrorCode, SendError
    from lark_channel.channel.types import SendResult

    from xiaowei import runtime
    from xiaowei.channel import ResultUnavailableError

    tools = [f"{SOURCE}/get_dashboard_summary"]
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        values = g3_config(url)
        config = env.config(**values, feishu=runtime_group(tools=tools))
        channel = GroupChannel(
            result=SendResult.fail(
                SendError(code=FeishuChannelErrorCode.PERMISSION_DENIED, retryable=False)
            )
        )
        env.scripts.add(
            attributed(A, "待重发面板"),
            tool_call(f"{SOURCE}__get_dashboard_summary", uid="host"),
            cite(),
        )
        async with env.running(config, feishu_channel=channel) as served:
            channel.emit_raw_from_sdk_thread(raw_group_event(env, "待重发面板", "om_g3_retry"))
            await until(lambda: len(channel.sends) == 1)
            assert await served.finish() == 0
        assert await env.scalar("SELECT delivery FROM xiaowei_request") == "failed"
        count = len(state.requests)
        revoked = env.config(**values, feishu=runtime_group(tools=sorted(QUERY_TOOLS)))
        receiver = GroupChannel()
        with pytest.raises(ResultUnavailableError):
            await runtime.resend(
                revoked,
                subject_id=A,
                message_id="om_g3_retry",
                group=True,
                clock=env.clock,
                feishu_channel=receiver,
                starrocks_connect=env.connect(revoked),
            )
        assert receiver.sends == [] and len(state.requests) == count
        assert await env.scalar("SELECT delivery FROM xiaowei_request") == "failed"
    # MCP 离线仍可按源授权复核并重发；不重跑工具，也不装配模型。
    receiver = GroupChannel()
    assert (
        await runtime.resend(
            config,
            subject_id=A,
            message_id="om_g3_retry",
            group=True,
            clock=env.clock,
            feishu_channel=receiver,
            starrocks_connect=env.connect(config),
        )
        == "sent"
    )
    assert len(receiver.sends) == 1 and SOURCE in message_text(receiver.sends[0][1])


async def test_grafana_failure_authorization_rechecked_at_commit(
    env: Env, grafana_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from xiaowei.evidence import EvidenceStore
    from xiaowei.runtime import StaticAccess

    authorize = StaticAccess.authorize
    validate = EvidenceStore.validate_answer
    revoked = False

    async def current(self: Any, identity: Any, target_id: str, tool_id: str) -> bool:
        return (
            False
            if revoked and target_id == SOURCE
            else await authorize(self, identity, target_id, tool_id)
        )

    async def final(self: Any, turn: Any, ctx: Any, **kw: Any) -> Any:
        nonlocal revoked
        result = await validate(self, turn, ctx, **kw)
        revoked = bool(turn.monitoring_failures)
        return result

    monkeypatch.setattr(StaticAccess, "authorize", current)
    monkeypatch.setattr(EvidenceStore, "validate_answer", final)
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        state.status = 403
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()
            message = env.scripts.add(
                "提交前撤权",
                tool_call(f"{SOURCE}__get_annotations", **CALLS["get_annotations"]),
                lambda call: answer([], analysis="", advice="未经数据验证，检查权限"),
            )
            assert (await served.turn(message, "query")).json()["state"] == "failed"
            assert await env.scalar("SELECT count(*) FROM agent_messages") == 0
            assert len(state.requests) == 1


async def test_visible_grafana_tool_rechecks_current_permission_before_io(
    env: Env, grafana_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from xiaowei.runtime import StaticAccess

    original = StaticAccess.authorize
    revoked = False

    async def current(self: Any, identity: Any, target_id: str, tool_id: str) -> bool:
        return (
            False
            if revoked and target_id == SOURCE
            else await original(self, identity, target_id, tool_id)
        )

    monkeypatch.setattr(StaticAccess, "authorize", current)
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()

            def revoke(call: Any) -> list[Any]:
                nonlocal revoked
                assert f"{SOURCE}__get_dashboard_summary" in call.tools
                revoked = True
                return tool_call(f"{SOURCE}__get_dashboard_summary", uid="host")(call)

            message = env.scripts.add(
                "选工具后撤权",
                revoke,
                lambda call: answer([], analysis="", advice="当前无权，未读取"),
            )
            body = (await served.turn(message, "query")).json()
            assert not state.requests
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
            assert body["state"] == "completed" and "未读取" in body["delivery"]["content"]


@pytest.mark.parametrize("name", ["get_dashboard_summary", "get_dashboard_panel_queries"])
@pytest.mark.parametrize("branch", ["discovery", "namespace", "v1beta1", "v2beta1"])
@pytest.mark.parametrize("status", [401, 403, 500, 502])
async def test_native_dashboard_failure_is_source_local_and_not_replayed(
    env: Env, grafana_binary: Path, name: str, branch: str, status: int
) -> None:
    with (
        grafana_backend() as (port, state),
        official_grafana(grafana_binary, port) as url,
        serve(query_server) as prom,
    ):
        if branch == "discovery":
            state.discovery_status = status
        else:
            state.discovery_status = 200
            state.namespace = "org-2"
            if branch == "namespace":
                state.settings_status = status
            elif branch == "v1beta1":
                state.dashboard_status = status
            else:
                state.stored_version = "v2beta1"
                state.native_status = status
        values = g3_config(url)
        values["mcp_servers"].append(
            {
                "server_id": "prometheus-prod",
                "url": prom.url,
                "auth_ref": None,
                "timeout_seconds": 2,
                "max_response_bytes": 64000,
                "allowed_tools": {"query": "prometheus.query"},
            }
        )
        values["data_policy"]["model_tools"].append("prometheus-prod/query")
        values["access"]["grants"]["operator"].append("prometheus-prod/query")
        async with env.running(env.config(**values)) as served:
            await served.page()
            state.requests.clear()
            message = env.scripts.add(
                "Grafana 的读取失败，继续调查其他获准源",
                tool_call(f"{SOURCE}__{name}", **CALLS[name]),
                tool_call(f"{SOURCE}__{name}", **CALLS[name]),
                tool_call("prometheus-prod__query", query="up"),
                cite("Grafana 未核实，Prometheus 仅证明本次查询范围"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            assert len(body["delivery"]["facts"]) == 1
            assert body["delivery"]["facts"][0]["target_id"] == "prometheus-prod"
            assert SOURCE in body["delivery"]["monitoring_notice"]
            assert (
                len(state.requests)
                == {"discovery": 1, "namespace": 2, "v1beta1": 2, "v2beta1": 3}[branch]
            )
            assert all(
                path.startswith("/apis/dashboard.grafana.app") or path == "/api/frontend/settings"
                for path, _, _ in state.requests
            )
            assert all(
                token == "Bearer synthetic-upstream-token"  # noqa: S105 - synthetic
                for _, _, token in state.requests
            )
            assert prom.recorder.tool_calls == [("query", {"query": "up"})]
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1
            assert "synthetic dashboard failure" not in json.dumps(body)
            assert "synthetic discovery failure" not in json.dumps(body)


@pytest.mark.parametrize("name", ["get_dashboard_summary", "get_dashboard_panel_queries"])
@pytest.mark.parametrize("native", ["", "v2alpha1", "v2beta1"])
async def test_native_dashboard_success_keeps_locked_result_contract(
    env: Env, grafana_binary: Path, name: str, native: str
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        state.discovery_status = 200
        state.namespace = "org-2"
        state.stored_version = native
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()
            message = env.scripts.add(
                "读取原生面板定义", tool_call(f"{SOURCE}__{name}", **CALLS[name]), cite()
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            assert (
                "Host CPU" in body["delivery"]["content"] or name == "get_dashboard_panel_queries"
            )
            if name == "get_dashboard_panel_queries":
                assert "cpu_ratio" in body["delivery"]["content"]
            assert len(state.requests) == (3 if native else 2)
            assert all(
                path.startswith("/apis/dashboard.grafana.app") for path, _, _ in state.requests
            )
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1


@pytest.mark.parametrize("name", ["get_dashboard_summary", "get_dashboard_panel_queries"])
@pytest.mark.parametrize("branch", ["namespace", "v1beta1", "v2beta1"])
async def test_native_dashboard_unclassified_404_aborts_without_evidence(
    env: Env, grafana_binary: Path, name: str, branch: str
) -> None:
    with grafana_backend() as (port, state), official_grafana(grafana_binary, port) as url:
        state.discovery_status = 200
        state.namespace = "org-2"
        if branch == "namespace":
            state.settings_status = 404
        elif branch == "v1beta1":
            state.dashboard_status = 404
        else:
            state.stored_version = "v2beta1"
            state.native_status = 404
        async with env.running(env.config(**g3_config(url))) as served:
            await served.page()
            state.requests.clear()
            message = env.scripts.add(
                "原生定义找不到", tool_call(f"{SOURCE}__{name}", **CALLS[name])
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "failed"
            assert len(state.requests) == (3 if branch == "v2beta1" else 2)
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
            assert await env.scalar("SELECT count(*) FROM agent_messages") == 0
