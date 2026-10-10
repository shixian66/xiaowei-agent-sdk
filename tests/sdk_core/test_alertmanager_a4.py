"""A4 正式 serve、Web/群、SDK/治理/Evidence/Session；API 与模型合成。"""

import copy
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from tests.sdk_core.alertmanager_fixture import (
    AUTHORIZATION,
    SELECTED,
    SILENCE_ID,
    backend,
    official,
    seed,
)
from tests.sdk_core.alertmanager_fixture import alertmanager_binary as alertmanager_binary
from tests.sdk_core.browser import launch
from tests.sdk_core.mcp_fixture import serve
from tests.sdk_core.test_app import answer, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_gateway import GroupChannel, raw_group_event, runtime_group
from tests.sdk_core.test_group_identity import CHAT, A
from tests.sdk_core.test_monitoring_r1a import query_server
from tests.sdk_core.test_prometheus_p2 import cite
from tests.sdk_core.test_runtime import Env, free_port, until
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_web_browser import send, settled

from xiaowei.feishu import attributed
from xiaowei.models import AUDIENCES
from xiaowei.starrocks_tools import QUERY_TOOLS

pytestmark = pytest.mark.loopback
SOURCE = "alertmanager-prod"
CALLS: dict[str, dict[str, Any]] = {
    "get_alerts": {
        "filters": ['instance="host1"'],
        "active": True,
        "silenced": True,
        "inhibited": True,
        "unprocessed": True,
        "receiver": "",
        "limit": 10,
        "offset": 0,
    },
    "get_alert_groups": {
        "filters": ['instance="host1"'],
        "active": True,
        "silenced": True,
        "inhibited": True,
        "muted": True,
        "receiver": "",
        "limit": 10,
        "offset": 0,
    },
    "get_silences": {"filters": ['instance="host1"'], "limit": 10, "offset": 0},
    "get_silence": {"silence_id": SILENCE_ID},
    "get_receivers": {"limit": 10, "offset": 0},
    "get_status": {},
}


def a4_config(
    url: str, *, granted: bool = True, selected: tuple[str, ...] = SELECTED
) -> dict[str, Any]:
    tools = sorted(QUERY_TOOLS | {f"{SOURCE}/{name}" for name in selected})
    return {
        "alertmanager_sources": [
            {
                "server_id": SOURCE,
                "url": url,
                "username_ref": "env:XW_TEST_AM_USER",
                "password_ref": "env:XW_TEST_AM_PASSWORD",
                "timeout_seconds": 0.5,
                "max_response_bytes": 128_000,
                "tools": list(selected),
            }
        ],
        "data_policy": {"input": {"max_bytes": 2000}, "model_tools": tools},
        "access": {
            "policy_version": "a4-1",
            "grants": {"operator": tools if granted else sorted(QUERY_TOOLS), "alice": tools},
        },
        "projection_bytes": dict.fromkeys(AUDIENCES, 60_000),
        "budget": {
            "max_turns": 10,
            "max_tool_calls": 8,
            "timeout_seconds": 30,
            "max_scope_checks": 1000,
        },
    }


@pytest.fixture(autouse=True)
def synthetic_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XW_TEST_AM_USER", "reader")
    monkeypatch.setenv("XW_TEST_AM_PASSWORD", "synthetic-password")


async def test_formal_web_reads_suppression_sources_and_filters_private_fields(env: Env) -> None:
    with backend() as (url, state):
        async with env.running(env.config(**a4_config(url))) as served:
            assert (await served.ready())[f"alertmanager.{SOURCE}"] == "configured"
            assert state.requests == []  # 不做预读或后台探测。
            await served.page()
            message = env.scripts.add(
                "host1 CPU 被什么静默，Alertmanager 状态如何？",
                *[tool_call(f"{SOURCE}__{name}", **arguments) for name, arguments in CALLS.items()],
                cite("按 silencedBy 对应静默 ID；这些是采集快照，不证明已发送通知"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            assert len(body["delivery"]["facts"]) == 6
            assert all(
                fact["target_id"] == SOURCE and fact["captured_at"]
                for fact in body["delivery"]["facts"]
            )
            assert SILENCE_ID in body["delivery"]["content"]
            assert "suppressed" in body["delivery"]["content"]
            for marker in ("private-", "secret-canary", "synthetic-password", "Basic "):
                assert marker not in json.dumps(body)
            assert len(state.requests) == 6
            assert all(
                method == "GET" and auth == AUTHORIZATION for method, _, auth in state.requests
            )
            assert (await served.ready())[f"alertmanager.{SOURCE}"] == "available"
            assert set(SELECTED) == {
                name.removeprefix(f"{SOURCE}__")
                for name in env.scripts.tools_seen(message)[0]
                if name.startswith(f"{SOURCE}__")
            }
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 6
        stored = await env.scalar(
            "SELECT string_agg(model_content || session_content || "
            "web_content || feishu_content, '') FROM xiaowei_evidence"
        )
        assert "private-" not in stored and "secret-canary" not in stored


async def test_formal_web_hidden_unauthorized_api_forced_call_zero_io(env: Env) -> None:
    with backend() as (url, state):
        async with env.running(env.config(**a4_config(url, granted=False))) as served:
            await served.page()
            message = env.scripts.add(
                "强行读取未授权告警", tool_call(f"{SOURCE}__get_alerts", **CALLS["get_alerts"])
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "failed"
            assert not any(
                name.startswith(f"{SOURCE}__") for name in env.scripts.tools_seen(message)[0]
            )
            assert state.requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_silence_read_cannot_return_another_valid_id(env: Env) -> None:
    with backend() as (url, state):
        state.payloads[f"/api/v2/silence/{SILENCE_ID}"]["id"] = (
            "22222222-2222-4222-8222-222222222222"
        )
        async with env.running(env.config(**a4_config(url))) as served:
            await served.page()
            message = env.scripts.add(
                "核对已知静默 ID",
                tool_call(f"{SOURCE}__get_silence", **CALLS["get_silence"]),
                cite(),
            )
            assert (await served.turn(message, "query")).json()["state"] == "failed"
            assert len(state.requests) == 1
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
            assert await env.scalar("SELECT count(*) FROM agent_messages") == 0


async def test_last_page_retains_partial_marker_and_original_count(env: Env) -> None:
    with backend() as (url, state):
        state.payloads["/api/v2/receivers"] = [{"name": f"ops-{n}"} for n in range(3)]
        async with env.running(env.config(**a4_config(url))) as served:
            await served.page()
            message = env.scripts.add(
                "读取最后一页",
                tool_call(f"{SOURCE}__get_receivers", limit=2, offset=2),
                cite(),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            fact = body["delivery"]["facts"][0]
            assert json.loads(fact["result_json"]) == {
                "data": [{"name": "ops-2"}],
                "offset": 2,
                "limit": 2,
                "total": 3,
                "has_more": False,
            }
            assert fact["truncated"] and "结果已截断" in body["delivery"]["content"]


async def test_official_api_runs_through_formal_runner_and_evidence(
    env: Env, alertmanager_binary: Path
) -> None:
    with official(alertmanager_binary) as url:
        with httpx.Client(
            base_url=url, auth=("reader", "synthetic-password"), trust_env=False
        ) as client:
            silence_id = seed(client)
        calls = copy.deepcopy(CALLS)
        calls["get_silence"]["silence_id"] = silence_id
        for name in ("get_alerts", "get_alert_groups"):
            calls[name].update(
                filters=['instance="host1"', 'alertname="HostCPU"'], receiver="^op.*$"
            )
        async with env.running(env.config(**a4_config(url))) as served:
            await served.page()
            message = env.scripts.add(
                "核对 host1 当前静默和版本",
                *[tool_call(f"{SOURCE}__{name}", **arguments) for name, arguments in calls.items()],
                cite("silencedBy 关联静默，起止时间来自各次采集快照"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed" and len(body["delivery"]["facts"]) == 6
            facts = {
                f["tool_id"].split("/")[1]: json.loads(f["result_json"])
                for f in body["delivery"]["facts"]
            }
            assert facts["get_alerts"]["data"][0]["status"]["silencedBy"] == [silence_id]
            assert facts["get_silence"]["id"] == silence_id
            assert facts["get_silence"]["status"]["state"] == "active"
            assert facts["get_status"]["version"] == "0.34.1"
            assert facts["get_receivers"]["data"] == [{"name": "ops"}]
            assert "config" not in facts["get_status"]
            # true 包含其他状态；false 排除静默，不是“只选未静默”的标签替换。
            message = env.scripts.add(
                "只查未被静默的告警",
                tool_call(f"{SOURCE}__get_alerts", **{**CALLS["get_alerts"], "silenced": False}),
                cite(),
            )
            result = (await served.turn(message, "query", request_id="r2")).json()
            assert result["state"] == "completed"
            assert json.loads(result["delivery"]["facts"][-1]["result_json"])["data"] == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 7


@pytest.mark.parametrize("status", [401, 403, 500, 502])
@pytest.mark.parametrize("name", SELECTED)
async def test_classified_failure_pauses_only_this_source_with_one_request(
    env: Env, status: int, name: str
) -> None:
    with backend() as (url, state):
        state.status = status
        state.raw = b"private-detail secret-canary"
        values = a4_config(url)
        values["budget"]["max_tool_calls"] = 1
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "监控读取失败怎么办",
                tool_call(f"{SOURCE}__{name}", **CALLS[name]),
                tool_call(f"{SOURCE}__get_status"),  # 同源本轮暂停，不再 I/O，也不再占预算。
                lambda call: answer([], analysis="", advice="未经数据验证，请检查监控源权限与连接"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed" and not body["delivery"]["facts"]
            assert SOURCE in body["delivery"]["monitoring_notice"]
            assert "未经数据验证" in body["delivery"]["content"]
            assert "private-detail" not in json.dumps(body) and "secret-canary" not in json.dumps(
                body
            )
            assert len(state.requests) == 1
            assert (await served.ready())[f"alertmanager.{SOURCE}"] == "unavailable"
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_failure_can_continue_authorized_prometheus_and_rechecks_failure_history(
    env: Env,
) -> None:
    with backend() as (url, state), serve(query_server) as prom:
        state.status = 403
        values = a4_config(url)
        values["mcp_servers"] = [
            {
                "server_id": "prometheus-prod",
                "url": prom.url,
                "auth_ref": None,
                "timeout_seconds": 2,
                "max_response_bytes": 64000,
                "allowed_tools": {"query": "prometheus.query"},
            }
        ]
        values["data_policy"]["model_tools"].append("prometheus-prod/query")
        values["access"]["grants"]["operator"].append("prometheus-prod/query")
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "告警源失败时调查可用指标",
                tool_call(f"{SOURCE}__get_alerts", **CALLS["get_alerts"]),
                tool_call("prometheus-prod__query", query="up"),
                cite("Alertmanager 未核实，Prometheus 仅证明查询范围"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed" and len(body["delivery"]["facts"]) == 1
            assert body["delivery"]["facts"][0]["target_id"] == "prometheus-prod"
            assert SOURCE in body["delivery"]["monitoring_notice"]
            assert len(state.requests) == 1 and prom.recorder.tool_calls == [
                ("query", {"query": "up"})
            ]
            cookies = httpx.Cookies(served.client.cookies)
            assert await served.finish() == 0
        values["access"]["grants"]["operator"] = sorted(QUERY_TOOLS | {"prometheus-prod/query"})
        async with env.running(env.config(**values)) as served:
            served.client.cookies.update(cookies)
            assert (await served.client.get("/api/turns/r1")).status_code == 403
            assert len(state.requests) == 1


@pytest.mark.parametrize(
    "name,invalid",
    [
        ("get_alerts", {"limit": 0}),
        ("get_alert_groups", {"limit": 101}),
        ("get_receivers", {"offset": -1}),
        ("get_silences", {"filters": [""]}),
        ("get_alerts", {"filters": [" "]}),
        ("get_silences", {"filters": ["x" * 8193]}),
        ("get_alert_groups", {"filters": ["x"] * 33}),
        ("get_alerts", {"receiver": "x" * 257}),
        ("get_alerts", {"active": "true"}),
        ("get_silence", {"silence_id": "../../status"}),
        ("get_status", {"url": "http://another-host"}),
        ("get_status", {"method": "POST"}),
    ],
)
async def test_invalid_parameters_are_correctable_before_io(
    env: Env, name: str, invalid: dict[str, Any]
) -> None:
    with backend() as (url, state):
        async with env.running(env.config(**a4_config(url))) as served:
            await served.page()
            message = env.scripts.add(
                "参数不合法时未读取",
                tool_call(f"{SOURCE}__{name}", **{**CALLS[name], **invalid}),
                lambda call: answer([], analysis="", advice="参数不合法，未执行，请调整范围"),
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            assert not state.requests
            assert (await served.ready())[f"alertmanager.{SOURCE}"] == "configured"
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_rejected_input_can_be_corrected_with_remaining_budget(env: Env) -> None:
    with backend() as (url, state):
        values = a4_config(url)
        values["budget"]["max_tool_calls"] = 1
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "修正静默 ID 再读取",
                tool_call(f"{SOURCE}__get_silence", silence_id="not-a-uuid"),
                tool_call(f"{SOURCE}__get_silence", silence_id="{" + SILENCE_ID.upper() + "}"),
                cite(),
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            assert state.requests == [("GET", f"/api/v2/silence/{SILENCE_ID}", AUTHORIZATION)]
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1


@pytest.mark.parametrize(
    "kind", ["400", "404", "422", "302", "json", "type", "state", "timezone", "encoding", "limit"]
)
async def test_unknown_protocol_or_oversize_aborts_without_evidence(env: Env, kind: str) -> None:
    with backend() as (url, state):
        values = a4_config(url)
        if kind.isdigit():
            state.status = int(kind)
        elif kind == "json":
            state.raw = b"not JSON secret-canary"
        elif kind == "type":
            state.payloads["/api/v2/alerts"][0]["labels"]["instance"] = 3
        elif kind == "state":
            state.payloads["/api/v2/alerts"][0]["status"]["state"] = "unknown"
        elif kind == "timezone":
            state.payloads["/api/v2/alerts"][0]["startsAt"] = "2026-10-09T00:00:00"
        elif kind == "encoding":
            state.headers["Content-Encoding"] = "gzip"
        else:
            values["alertmanager_sources"][0]["max_response_bytes"] = 100
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "未知读取失败", tool_call(f"{SOURCE}__get_alerts", **CALLS["get_alerts"])
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "failed" and "secret-canary" not in json.dumps(body)
            assert len(state.requests) == 1
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
            assert await env.scalar("SELECT count(*) FROM agent_messages") == 0
            assert (await served.ready())["status"] == "ready"


async def test_total_timeout_no_replay_and_next_turn_is_a_new_read(env: Env) -> None:
    with backend() as (url, state):
        state.delay = 0.8
        values = a4_config(url)
        values["alertmanager_sources"][0]["timeout_seconds"] = 0.1
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "超时后如何排查",
                tool_call(f"{SOURCE}__get_status"),
                lambda call: answer([], analysis="", advice="未经数据验证，请检查连接"),
            )
            started = time.monotonic()
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed" and not body["delivery"]["facts"]
            assert time.monotonic() - started < 1
            assert len(state.requests) == 1
            state.delay = 0
            message = env.scripts.add("再次读取源状态", tool_call(f"{SOURCE}__get_status"), cite())
            body = (await served.turn(message, "query", request_id="r2")).json()
            assert body["state"] == "completed" and len(body["delivery"]["facts"]) == 1
            assert len(state.requests) == 2
            assert (await served.ready())[f"alertmanager.{SOURCE}"] == "available"


async def test_unreachable_source_does_not_delay_a_starrocks_only_turn(env: Env) -> None:
    url = f"http://127.0.0.1:{free_port()}"
    async with env.running(env.config(**a4_config(url))) as served:
        await served.page()
        assert (await served.ready())[f"alertmanager.{SOURCE}"] == "configured"
        message = env.scripts.add(
            "告警源不可达",
            tool_call(f"{SOURCE}__get_status"),
            lambda call: answer([], analysis="", advice="未经数据验证，检查源连接"),
        )
        body = (await served.turn(message, "query")).json()
        assert body["state"] == "completed" and not body["delivery"]["facts"]
        assert (await served.ready())[f"alertmanager.{SOURCE}"] == "unavailable"
        message = env.scripts.add(
            "仅查 StarRocks 销售",
            tool_call(
                "run_readonly_query", cluster="sr-test", sql="SELECT * FROM shop.sales LIMIT 5"
            ),
            cite(),
        )
        started = time.monotonic()
        body = (await served.turn(message, "query", request_id="r2")).json()
        assert body["state"] == "completed" and time.monotonic() - started < 1
        assert body["delivery"]["facts"][-1]["target_id"] == "sr-test"


@pytest.mark.parametrize("auth", ["wrong", "none"])
async def test_basic_header_is_not_bearer_and_optional_read_auth_is_explicit(
    env: Env, monkeypatch: pytest.MonkeyPatch, auth: str
) -> None:
    with backend() as (url, state):
        values = a4_config(url)
        if auth == "none":
            state.require_auth = False
            values["alertmanager_sources"][0].update(username_ref=None, password_ref=None)
        else:
            monkeypatch.setenv("XW_TEST_AM_PASSWORD", "wrong-synthetic-password")

        def finish(call: Any) -> Any:
            return (
                cite()(call)
                if auth == "none"
                else answer([], analysis="", advice="未经数据验证，请检查权限")
            )

        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add("核对认证方式", tool_call(f"{SOURCE}__get_status"), finish)
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed" and len(state.requests) == 1
            header = state.requests[0][2]
            if auth == "none":
                assert header is None and len(body["delivery"]["facts"]) == 1
            else:
                assert header.startswith("Basic ") and header != AUTHORIZATION
                assert not body["delivery"]["facts"]
                assert "wrong-synthetic-password" not in json.dumps(body)


async def test_deployment_base_path_and_repeated_filters_reach_only_fixed_endpoint(
    env: Env,
) -> None:
    with backend() as (url, state):
        state.payloads["/monitoring/api/v2/alerts"] = state.payloads["/api/v2/alerts"]
        async with env.running(env.config(**a4_config(url + "/monitoring/"))) as served:
            await served.page()
            args = {
                **CALLS["get_alerts"],
                "filters": ['instance="host1"', 'alertname=~"CPU.*"'],
                "active": False,
                "receiver": "^op.*$",
            }
            message = env.scripts.add(
                "带子路径和正则读取", tool_call(f"{SOURCE}__get_alerts", **args), cite()
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            assert len(state.requests) == 1
            _, path, auth = state.requests[0]
            assert urlsplit(path).path == "/monitoring/api/v2/alerts" and auth == AUTHORIZATION
            assert parse_qs(urlsplit(path).query) == {
                "filter": args["filters"],
                "active": ["false"],
                "silenced": ["true"],
                "inhibited": ["true"],
                "unprocessed": ["true"],
                "receiver": ["^op.*$"],
            }


async def test_redirect_is_not_followed_and_no_auth_forwarding(env: Env) -> None:
    with backend() as (url, state), backend() as (other, redirected):
        state.status = 302
        state.headers["Location"] = other + "/api/v2/status"
        async with env.running(env.config(**a4_config(url))) as served:
            await served.page()
            message = env.scripts.add("入口跳转", tool_call(f"{SOURCE}__get_status"))
            assert (await served.turn(message, "query")).json()["state"] == "failed"
        assert len(state.requests) == 1 and redirected.requests == []


@pytest.mark.parametrize(
    "offset,expected,more", [(0, ["ops-0", "ops-1"], True), (2, ["ops-2"], False), (9, [], False)]
)
async def test_paging_keeps_scope_and_empty_page(
    env: Env, offset: int, expected: list[str], more: bool
) -> None:
    with backend() as (url, state):
        state.payloads["/api/v2/receivers"] = [{"name": f"ops-{n}"} for n in range(3)]
        async with env.running(env.config(**a4_config(url))) as served:
            await served.page()
            message = env.scripts.add(
                "读取目录一页",
                tool_call(f"{SOURCE}__get_receivers", limit=2, offset=offset),
                cite(),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            fact = body["delivery"]["facts"][0]
            data = json.loads(fact["result_json"])
            assert data == {
                "data": [{"name": n} for n in expected],
                "offset": offset,
                "limit": 2,
                "total": 3,
                "has_more": more,
            }
            assert fact["truncated"]


async def test_group_cap_and_secondary_projection_truncation_are_distinct(env: Env) -> None:
    with backend() as (url, state):
        state.payloads["/api/v2/alerts/groups"][0]["alerts"] *= 101
        values = a4_config(url)
        # 保持模型64KB、交付20KB上限。此场景不查 StarRocks，只收紧其可信 SQL 上限，
        # 让其启动容量检查也符合本次20KB投影；不放宽任何产品检查。
        from tests.p1b.test_starrocks_adapter import TARGET

        values["starrocks"] = TARGET.model_dump(mode="json")
        values["starrocks"]["policy"]["max_sql_bytes"] = 500
        values["projection_bytes"] = dict.fromkeys(AUDIENCES, 20_000)
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "较大告警分组",
                tool_call(f"{SOURCE}__get_alert_groups", **CALLS["get_alert_groups"]),
                cite(),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            fact = body["delivery"]["facts"][0]
            group = json.loads(fact["result_json"])["data"][0]
            assert group["alert_count"] == 101 and group["alerts_truncated"]
            assert group["receiver"]["name"] == "ops" and fact["truncated"]
            assert "alerts" not in json.loads(fact["result_json"])  # 大集合省略，分组摘要仍可用。
            assert await served.finish() == 0
        state.payloads["/api/v2/alerts"] *= 100
        values = a4_config(url)
        values["starrocks"] = TARGET.model_dump(mode="json")
        values["starrocks"]["policy"]["max_sql_bytes"] = 500
        values["projection_bytes"] = dict.fromkeys(AUDIENCES, 20_000)
        async with env.running(env.config(**values)) as served:
            await served.page()

            def check_partial(call: Any) -> Any:
                output = json.loads(
                    next(
                        i["output"]
                        for i in reversed(call.input)
                        if i.get("type") == "function_call_output"
                    )
                )
                assert output["truncated"] is True
                assert output["data"]["has_more"] is False and "data" not in output["data"]
                return cite("本页投影已省略，不能声称完整")(call)

            message = env.scripts.add(
                "投影放不下整页",
                tool_call(f"{SOURCE}__get_alerts", **{**CALLS["get_alerts"], "limit": 100}),
                check_partial,
            )
            body = (await served.turn(message, "query", request_id="r2")).json()
            assert body["state"] == "completed"
            fact = body["delivery"]["facts"][0]
            assert fact["truncated"] and "data" not in json.loads(fact["result_json"])
            assert "has_more=false 也不能证明完整" in fact["note"]


async def test_expired_silence_keeps_expired_state_without_current_suppression_claim(
    env: Env,
) -> None:
    with backend() as (url, state):
        value = state.payloads[f"/api/v2/silence/{SILENCE_ID}"]
        value.update(endsAt="2026-10-09T01:00:00Z", status={"state": "expired"})
        value["matchers"][0].pop("isEqual")  # 官方默认 true，只在结果中补齐。
        async with env.running(env.config(**a4_config(url))) as served:
            await served.page()
            message = env.scripts.add(
                "读取已结束静默",
                tool_call(f"{SOURCE}__get_silence", **CALLS["get_silence"]),
                cite("过期记录不证明当前抑制"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            data = json.loads(body["delivery"]["facts"][0]["result_json"])
            assert data["status"]["state"] == "expired" and data["matchers"][0]["isEqual"]


async def test_successful_read_must_be_cited_not_replaced_by_advice(env: Env) -> None:
    with backend() as (url, state):
        async with env.running(env.config(**a4_config(url))) as served:
            await served.page()
            message = env.scripts.add(
                "查告警后漏证据",
                tool_call(f"{SOURCE}__get_status"),
                lambda call: answer([], analysis="", advice="只解释不引用"),
            )
            assert (await served.turn(message, "query")).json()["state"] == "failed"
            assert len(state.requests) == 1
            assert await env.scalar("SELECT count(*) FROM agent_messages") == 0


async def test_source_authorization_does_not_grant_other_tools(env: Env) -> None:
    with backend() as (url, state):
        values = a4_config(url)
        values["access"]["grants"]["operator"] = [f"{SOURCE}/get_status"]
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "不能扩大工具权限", tool_call(f"{SOURCE}__get_silences", **CALLS["get_silences"])
            )
            assert (await served.turn(message, "query")).json()["state"] == "failed"
            seen = env.scripts.tools_seen(message)[0]
            assert f"{SOURCE}__get_status" in seen and f"{SOURCE}__get_silences" not in seen
            assert state.requests == []


async def test_current_authorization_rechecked_after_tool_selection(
    env: Env, monkeypatch: pytest.MonkeyPatch
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
    with backend() as (url, state):
        async with env.running(env.config(**a4_config(url))) as served:
            await served.page()

            def revoke(call: Any) -> Any:
                nonlocal revoked
                assert f"{SOURCE}__get_status" in call.tools
                revoked = True
                return tool_call(f"{SOURCE}__get_status")(call)

            message = env.scripts.add(
                "调用前撤权",
                revoke,
                lambda call: answer([], analysis="", advice="当前无权，未执行"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed" and "未执行" in body["delivery"]["content"]
            assert state.requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_offline_history_and_session_replay_do_not_probe_source(env: Env) -> None:
    with backend() as (url, state):
        config = env.config(**a4_config(url))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "保存静默记录", tool_call(f"{SOURCE}__get_silence", **CALLS["get_silence"]), cite()
            )
            assert (await served.turn(message, "query")).json()["state"] == "completed"
            cookies = httpx.Cookies(served.client.cookies)
            assert await served.finish() == 0
        assert len(state.requests) == 1
    # 无 API 可用仍可按当前源授权复核历史；不用远端 ACL，也不重跑工具。
    async with env.running(config) as served:
        served.client.cookies.update(cookies)
        response = await served.client.get("/api/turns/r1")
        assert response.status_code == 200 and SILENCE_ID in response.text
        message = env.scripts.add("解释刚才静默快照", cite("仅解释历史快照，不保证当前仍有效"))
        body = (await served.turn(message, "query", request_id="r2")).json()
        assert body["state"] == "completed" and SILENCE_ID in body["delivery"]["content"]
        assert (await served.ready())[f"alertmanager.{SOURCE}"] == "configured"
        assert await served.finish() == 0
    async with env.running(env.config(**a4_config(url, granted=False))) as served:
        served.client.cookies.update(cookies)
        assert (await served.client.get("/api/turns/r1")).status_code == 403
        message = env.scripts.add("撤权后重放", cite())
        body = (await served.turn(message, "query", request_id="r3")).json()
        assert body["state"] == "failed"
        assert env.scripts.tools_seen(message) == []  # 回放前即拒绝，模型未收到历史。
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1


async def test_foreign_session_cannot_cite_prior_source_evidence(env: Env) -> None:
    with backend() as (url, state):
        async with env.running(env.config(**a4_config(url))) as served:
            await served.page()
            message = env.scripts.add("第一会话读状态", tool_call(f"{SOURCE}__get_status"), cite())
            body = (await served.turn(message, "query")).json()
            prior = body["delivery"]["facts"][0]["evidence_id"]
            served.client.cookies.clear()
            await served.page()
            message = env.scripts.add("另一会话伪造引用", lambda call: answer([prior]))
            assert (await served.turn(message, "query")).json()["state"] == "failed"
            assert len(state.requests) == 1


@pytest.mark.parametrize("granted", [True, False])
async def test_group_alert_reading_uses_shared_scope_and_session(env: Env, granted: bool) -> None:
    with backend() as (url, state):
        tools = [f"{SOURCE}/{name}" for name in SELECTED] if granted else sorted(QUERY_TOOLS)
        config = env.config(**a4_config(url), feishu=runtime_group(tools=tools))
        channel = GroupChannel()
        message = env.scripts.add(
            attributed(A, "host1 告警被谁静默"),
            tool_call(f"{SOURCE}__get_alerts", **CALLS["get_alerts"]),
            cite("采集快照不证明当前通知"),
        )
        async with env.running(config, feishu_channel=channel):
            channel.emit_raw_from_sdk_thread(raw_group_event(env, "host1 告警被谁静默", "om_a4"))
            await until(lambda: len(channel.sends) == 1)
            destination, payload, _ = channel.sends[0]
            shown = message_text(payload)
            assert destination == CHAT
            if granted:
                assert SOURCE in shown and SILENCE_ID in shown and "suppressed" in shown
                assert "private-" not in shown and "secret-canary" not in shown
            else:
                assert f"{SOURCE}__get_alerts" not in env.scripts.tools_seen(message)[0]
                assert not state.requests
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == int(granted)


@pytest.mark.browser
async def test_browser_alert_reading_and_invalid_scope_refusal(
    env: Env, chrome_binary: str
) -> None:
    with backend() as (url, state):
        async with env.running(env.config(**a4_config(url))), launch(chrome_binary) as chrome:
            page = await chrome.page()
            await page.navigate(f"{env.origin}/")
            message = env.scripts.add(
                "浏览器查告警",
                tool_call(f"{SOURCE}__get_alerts", **CALLS["get_alerts"]),
                cite("仅采集快照"),
            )
            await send(page, message, "query")
            shown = await settled(page, 0, "completed")
            assert SOURCE in shown and SILENCE_ID in shown and "suppressed" in shown
            before = len(state.requests)
            message = env.scripts.add(
                "浏览器拒绝过宽页",
                tool_call(f"{SOURCE}__get_silences", **{**CALLS["get_silences"], "limit": 101}),
                lambda call: answer([], analysis="", advice="上限100，未执行；请缩小范围"),
            )
            await send(page, message, "query")
            assert "未执行" in await settled(page, 1, "completed")
            assert len(state.requests) == before


async def test_group_resend_offline_rechecks_current_source_permission(env: Env) -> None:
    from lark_channel.channel.errors import FeishuChannelErrorCode, SendError
    from lark_channel.channel.types import SendResult

    from xiaowei import runtime
    from xiaowei.channel import ResultUnavailableError

    with backend() as (url, state):
        values = a4_config(url)
        config = env.config(**values, feishu=runtime_group(tools=[f"{SOURCE}/get_alerts"]))
        channel = GroupChannel(
            result=SendResult.fail(
                SendError(code=FeishuChannelErrorCode.PERMISSION_DENIED, retryable=False)
            )
        )
        env.scripts.add(
            attributed(A, "待重发告警"),
            tool_call(f"{SOURCE}__get_alerts", **CALLS["get_alerts"]),
            cite(),
        )
        async with env.running(config, feishu_channel=channel) as served:
            channel.emit_raw_from_sdk_thread(raw_group_event(env, "待重发告警", "om_a4_retry"))
            await until(lambda: len(channel.sends) == 1)
            assert await served.finish() == 0
        assert await env.scalar("SELECT delivery FROM xiaowei_request") == "failed"
        revoked = env.config(**values, feishu=runtime_group(tools=sorted(QUERY_TOOLS)))
        receiver = GroupChannel()
        with pytest.raises(ResultUnavailableError):
            await runtime.resend(
                revoked,
                subject_id=A,
                message_id="om_a4_retry",
                group=True,
                clock=env.clock,
                feishu_channel=receiver,
                starrocks_connect=env.connect(revoked),
            )
        assert not receiver.sends and len(state.requests) == 1
    receiver = GroupChannel()
    assert (
        await runtime.resend(
            config,
            subject_id=A,
            message_id="om_a4_retry",
            group=True,
            clock=env.clock,
            feishu_channel=receiver,
            starrocks_connect=env.connect(config),
        )
        == "sent"
    )
    assert len(receiver.sends) == 1 and SOURCE in message_text(receiver.sends[0][1])
