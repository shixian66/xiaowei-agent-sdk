"""S6 正式 serve → 群事件 → Runner/治理 → 官方 API → Action/Evidence。"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.sdk_core.alertmanager_fixture import (
    AUTHORIZATION,
    PASSWORD,
    SILENCE_ID,
    alert,
    backend,
    official,
)
from tests.sdk_core.alertmanager_fixture import alertmanager_binary as alertmanager_binary
from tests.sdk_core.test_alertmanager_a4 import SOURCE, a4_config
from tests.sdk_core.test_alertmanager_a4 import synthetic_basic as synthetic_basic
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_gateway import GroupChannel, raw_group_event, runtime_group
from tests.sdk_core.test_group_identity import A, B
from tests.sdk_core.test_runtime import Env, until
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_starrocks_tools import tool_outputs

from xiaowei.feishu import attributed
from xiaowei.models import AUDIENCES
from xiaowei.starrocks_tools import QUERY_TOOLS

pytestmark = pytest.mark.loopback
CREATE = f"{SOURCE}/create_silence"
CANCEL = f"{SOURCE}/cancel_silence"
PROPOSE_CREATE = f"{SOURCE}/propose_create_silence"
PROPOSE_CANCEL = f"{SOURCE}/propose_cancel_silence"
STATUS = f"{SOURCE}/get_action_status"


@pytest.fixture
async def group_channel() -> AsyncIterator[GroupChannel]:
    channel = GroupChannel()
    try:
        yield channel
    finally:
        channel.close_loop()


def create_args(env: Env) -> dict[str, Any]:
    return {
        "matchers": [{"name": "instance", "value": "host1", "isRegex": False, "isEqual": True}],
        "starts_at": (env.clock() - timedelta(minutes=1)).isoformat(),
        "ends_at": (env.clock() + timedelta(days=365 * 3)).isoformat(),
        "comment": "host1 三年维护，明确到期",
    }


def s6_config(url: str, **changes: Any) -> dict[str, Any]:
    values = a4_config(url)
    values["alertmanager_sources"][0]["write_actions"] = ["create_silence", "cancel_silence"]
    shared = sorted(QUERY_TOOLS | {PROPOSE_CREATE, PROPOSE_CANCEL, STATUS, f"{SOURCE}/get_silence"})
    values.update(
        feishu=runtime_group(tools=shared),
        access={
            "policy_version": "s6-1",
            "grants": {"operator": sorted(QUERY_TOOLS), B: [CREATE, CANCEL]},
        },
        data_policy={"input": {"max_bytes": 2000}, "model_tools": shared},
        projection_bytes=dict.fromkeys(AUDIENCES, 60_000),
    )
    values["feishu"]["max_reply_chars"] = 3500
    values["alertmanager_sources"][0]["timeout_seconds"] = 2
    values.update(changes)
    return values


async def group_send(
    env: Env, channel: GroupChannel, message: str, mid: str, *, sender: str = A
) -> None:
    count = len(channel.sends)
    channel.emit_raw_from_sdk_thread(raw_group_event(env, message, mid, sender))
    await until(lambda: len(channel.sends) > count)


async def test_formal_group_creates_then_cancels_official_silence_once(
    env: Env, alertmanager_binary: Path, group_channel: GroupChannel
) -> None:
    with official(alertmanager_binary) as url:
        channel = group_channel
        args = create_args(env)
        with httpx.Client(base_url=url, auth=("reader", PASSWORD), trust_env=False) as client:
            value = alert()
            for key in ("status", "fingerprint", "receivers", "updatedAt"):
                value.pop(key)
            current = datetime.now(UTC)
            value.update(
                startsAt=(current - timedelta(minutes=1)).isoformat(),
                endsAt=(current + timedelta(hours=1)).isoformat(),
            )
            assert client.post("/api/v2/alerts", json=[value]).status_code == 200
        asked = env.scripts.add(
            attributed(A, "静默 host1 三年"),
            tool_call(f"{SOURCE}__propose_create_silence", **args),
            cite("等待群批准"),
        )
        async with env.running(env.config(**s6_config(url)), feishu_channel=channel) as served:
            await group_send(env, channel, "静默 host1 三年", "om_create")
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            assert action_id and await env.scalar("SELECT state FROM xiaowei_action") == "pending"
            with httpx.Client(base_url=url, auth=("reader", PASSWORD), trust_env=False) as client:
                assert client.get("/api/v2/silences").json() == []
            material = message_text(channel.sends[-1][1])
            assert "host1" in material and "当前命中 1 条告警" in material
            assert "HostCPU" in material
            assert args["ends_at"].split("T")[0] in material
            assert CREATE not in env.scripts.tools_seen(asked)[0]
            assert f"{SOURCE}__create_silence" not in env.scripts.tools_seen(asked)[0]
            # 非名单成员拒绝，随后名单成员批准；两条批准都不调用模型。
            await group_send(env, channel, f"/批准 {action_id}", "om_denied")
            assert await env.scalar("SELECT state FROM xiaowei_action") == "pending"
            await group_send(env, channel, f"/批准 {action_id}", "om_approve", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "succeeded"
            result = json.loads(await env.scalar("SELECT result FROM xiaowei_action"))
            sid = result["id"]
            with httpx.Client(base_url=url, auth=("reader", PASSWORD), trust_env=False) as client:
                actual = client.get(f"/api/v2/silence/{sid}").json()
                assert (
                    actual["comment"] == args["comment"] and actual["status"]["state"] == "active"
                )
                assert actual["createdBy"] == "xiaowei"
                assert client.get("/api/v2/alerts").json()[0]["status"]["silencedBy"] == [sid]
                assert len(client.get("/api/v2/silences").json()) == 1
            assert sid in message_text(channel.sends[-1][1])
            await group_send(env, channel, f"/批准 {action_id}", "om_repeat", sender=B)
            assert len(env.scripts.calls) == 1
            env.scripts.add(
                attributed(A, "取消静默"),
                tool_call(f"{SOURCE}__propose_cancel_silence", silence_id=sid),
                cite("提前结束须另次批准"),
            )
            await group_send(env, channel, "取消静默", "om_cancel")
            cancel_id = await env.scalar(
                "SELECT action_id FROM xiaowei_action WHERE write_tool_id=:tool", {"tool": CANCEL}
            )
            assert cancel_id and sid in message_text(channel.sends[-1][1])
            await group_send(env, channel, f"/批准 {cancel_id}", "om_cancel_approve", sender=B)
            assert (
                await env.scalar(
                    "SELECT state FROM xiaowei_action WHERE action_id=:id", {"id": cancel_id}
                )
                == "succeeded"
            )
            assert "expired" in message_text(channel.sends[-1][1])
            with httpx.Client(base_url=url, auth=("reader", PASSWORD), trust_env=False) as client:
                assert client.get(f"/api/v2/silence/{sid}").json()["status"]["state"] == "expired"
                assert len(client.get("/api/v2/silences").json()) == 1
                assert client.get("/api/v2/alerts").json()[0]["status"]["silencedBy"] == []
            assert (await served.ready())["status"] == "ready"
            env.scripts.add(
                attributed(A, "动作结果"),
                tool_call(f"{SOURCE}__get_action_status", action_id=cancel_id),
                cite(),
            )
            await group_send(env, channel, "动作结果", "om_status")
            assert "succeeded" in message_text(channel.sends[-1][1])
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 2
        stored = await env.scalar(
            "SELECT string_agg(model_content || session_content || web_content || "
            "feishu_content, '') FROM xiaowei_evidence"
        )
        assert PASSWORD not in stored and "Basic " not in stored


@pytest.mark.parametrize("tool", [PROPOSE_CREATE, CREATE])
async def test_formal_web_cannot_propose_or_execute_silences(
    env: Env, alertmanager_binary: Path, group_channel: GroupChannel, tool: str
) -> None:
    with official(alertmanager_binary) as url:
        config = s6_config(url)
        config["access"]["grants"]["operator"] += [CREATE, PROPOSE_CREATE]
        config["data_policy"]["model_tools"] += [CREATE]
        async with env.running(env.config(**config), feishu_channel=group_channel) as served:
            await served.page()
            asked = env.scripts.add(
                "Web 要求静默", tool_call(tool.replace("/", "__"), **create_args(env))
            )
            assert (await served.turn(asked, "query")).json()["state"] == "failed"
            assert not any("silence" in name for name in env.scripts.tools_seen(asked)[0])
            assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
            with httpx.Client(base_url=url, auth=("reader", PASSWORD), trust_env=False) as client:
                assert client.get("/api/v2/silences").json() == []


async def test_write_tools_never_enter_any_runner_purpose(env: Env) -> None:
    from xiaowei.runtime import _app_config

    config = env.config(**s6_config("http://127.0.0.1:1"))
    app = _app_config(config)
    assert not ({CREATE, CANCEL} & frozenset().union(*app.purposes.values()))


@pytest.mark.parametrize("field", ["matchers", "comment", "endsAt", "id", "startsAt", "status"])
async def test_create_readback_must_match_approved_write(
    env: Env, group_channel: GroupChannel, field: str
) -> None:
    with backend() as (url, state):
        args = create_args(env)
        if field == "startsAt":
            args["starts_at"] = (env.clock() + timedelta(days=365)).isoformat()
        replacements = {
            "matchers": [{"name": "instance", "value": "host2", "isRegex": False, "isEqual": True}],
            "comment": "outside change",
            "endsAt": (env.clock() + timedelta(days=365 * 4)).isoformat(),
            "id": "22222222-2222-4222-8222-222222222222",
            "startsAt": (env.clock() + timedelta(days=365 * 2)).isoformat(),
            "status": {"state": "expired"},
        }
        state.write_result_updates[field] = replacements[field]
        env.scripts.add(
            attributed(A, "提出"),
            tool_call(f"{SOURCE}__propose_create_silence", **args),
            cite(),
        )
        async with env.running(env.config(**s6_config(url)), feishu_channel=group_channel):
            await group_send(env, group_channel, "提出", "om_p")
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            await group_send(env, group_channel, f"/批准 {action_id}", "om_a", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "unknown"
            assert await env.scalar("SELECT result FROM xiaowei_action") is None
            assert len(state.writes) == 1
            assert sum(method == "POST" for method, _, _ in state.requests) == 1
            assert (
                await env.scalar(
                    "SELECT count(*) FROM xiaowei_evidence WHERE tool_id=:tool", {"tool": CREATE}
                )
                == 0
            )
            receipt = message_text(group_channel.sends[-1][1])
            assert "未知" in receipt and "核对" in receipt
            assert "工具执行失败" not in receipt


@pytest.mark.parametrize(
    ("state_name", "start_offset"),
    [
        ("active", timedelta(days=365)),
        ("pending", timedelta(days=365)),
        ("pending", timedelta(minutes=1)),
    ],
)
async def test_past_start_adjustment_must_be_active_and_at_most_updated_at(
    env: Env, group_channel: GroupChannel, state_name: str, start_offset: timedelta
) -> None:
    with backend() as (url, state):
        state.write_result_updates.update(
            startsAt=(env.clock() + start_offset).isoformat(),
            status={"state": state_name},
        )
        env.scripts.add(
            attributed(A, "提出"),
            tool_call(f"{SOURCE}__propose_create_silence", **create_args(env)),
            cite(),
        )
        async with env.running(env.config(**s6_config(url)), feishu_channel=group_channel):
            await group_send(env, group_channel, "提出", "om_p")
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            await group_send(env, group_channel, f"/批准 {action_id}", "om_a", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "unknown"
            assert await env.scalar("SELECT result FROM xiaowei_action") is None
            assert (
                await env.scalar(
                    "SELECT count(*) FROM xiaowei_evidence WHERE tool_id=:tool", {"tool": CREATE}
                )
                == 0
            )
            receipt = message_text(group_channel.sends[-1][1])
            assert "未知" in receipt and "核对" in receipt
            await group_send(env, group_channel, f"/批准 {action_id}", "om_again", sender=B)
            assert len(state.writes) == 1
            assert sum(method == "POST" for method, _, _ in state.requests) == 1


@pytest.mark.parametrize("problem", ["timeout", "readback", "auth", "5xx", "redirect"])
async def test_formal_unknown_write_not_replayed_and_readonly_continues(
    env: Env, group_channel: GroupChannel, problem: str
) -> None:
    channel = group_channel
    with backend() as (url, state):
        config = s6_config(url)
        config["alertmanager_sources"][0]["timeout_seconds"] = 0.15
        env.scripts.add(
            attributed(A, "提出静默"),
            tool_call(f"{SOURCE}__propose_create_silence", **create_args(env)),
            cite(),
        )
        async with env.running(env.config(**config), feishu_channel=channel) as served:
            await group_send(env, channel, "提出静默", "om_p")
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            assert action_id and state.writes == []
            if problem == "timeout":
                state.write_delay = 0.3
            elif problem == "readback":
                state.read_status_after_write = 503
            else:
                state.write_status = {"auth": 401, "5xx": 503, "redirect": 307}[problem]
            await group_send(env, channel, f"/批准 {action_id}", "om_a", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "unknown"
            assert await env.scalar("SELECT result FROM xiaowei_action") is None
            receipt = message_text(channel.sends[-1][1])
            assert "未知" in receipt and "核对" in receipt and "工具执行失败" not in receipt
            count = len(state.requests)
            await group_send(env, channel, f"/批准 {action_id}", "om_again", sender=B)
            assert len(state.requests) == count
            assert sum(method == "POST" for method, _, _ in state.requests) == 1
            assert len(state.writes) == (1 if problem in {"timeout", "readback"} else 0)
            if state.writes:
                method, path, payload = state.writes[0]
                assert method == "POST" and path == "/api/v2/silences"
                assert set(payload) == {"matchers", "startsAt", "endsAt", "comment", "createdBy"}
                assert payload["createdBy"] == "xiaowei"
            assert (await served.ready())["status"] == "ready"
            assert (
                await env.scalar(
                    "SELECT count(*) FROM xiaowei_evidence WHERE tool_id=:tool", {"tool": CREATE}
                )
                == 0
            )
            env.scripts.add(
                "继续读 StarRocks",
                tool_call(
                    "run_readonly_query",
                    cluster="sr-test",
                    sql="SELECT region, total FROM sales",
                ),
                cite(),
            )
            await served.page()
            response = await served.turn("继续读 StarRocks", "query")
            assert response.status_code == 200
            assert response.json()["state"] == "completed"
            assert all(auth == AUTHORIZATION for _, _, auth in state.requests)


@pytest.mark.parametrize("action", ["create", "cancel"])
async def test_changed_approval_material_refuses_before_write(
    env: Env, group_channel: GroupChannel, action: str
) -> None:
    channel = group_channel
    with backend() as (url, state):
        args = create_args(env) if action == "create" else {"silence_id": SILENCE_ID}
        name = f"propose_{action}_silence"
        env.scripts.add(attributed(A, "提出"), tool_call(f"{SOURCE}__{name}", **args), cite())
        async with env.running(env.config(**s6_config(url)), feishu_channel=channel) as served:
            await group_send(env, channel, "提出", "om_p")
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            assert action_id and state.writes == []
            if action == "cancel":
                state.payloads[f"/api/v2/silence/{SILENCE_ID}"]["comment"] = "另一位管理员已修改"
            else:
                state.payloads["/api/v2/alerts"] = []
            await group_send(env, channel, f"/批准 {action_id}", "om_a", sender=B)
            assert state.writes == []
            assert not any(method != "GET" for method, _, _ in state.requests)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "rejected"
            receipt = message_text(channel.sends[-1][1])
            assert "未执行" in receipt and "重新提出" in receipt
            assert (await served.ready())["status"] == "ready"


@pytest.mark.parametrize("permission", ["source", "proposal", "forced_write"])
async def test_group_hidden_silence_tools_cannot_send_any_io(
    env: Env, group_channel: GroupChannel, permission: str
) -> None:
    with backend() as (url, state):
        config = s6_config(url)
        shared = config["feishu"]["group"]["tools"]
        if permission == "source":
            config["feishu"]["group"]["tools"] = [
                t for t in shared if not t.startswith(SOURCE + "/")
            ]
        elif permission == "proposal":
            config["feishu"]["group"]["tools"] = [t for t in shared if t != PROPOSE_CREATE]
        else:
            config["data_policy"]["model_tools"] += [CREATE]
        tool = CREATE if permission == "forced_write" else PROPOSE_CREATE
        asked = env.scripts.add(
            attributed(B, "提出"), tool_call(tool.replace("/", "__"), **create_args(env))
        )
        async with env.running(env.config(**config), feishu_channel=group_channel):
            await group_send(env, group_channel, "提出", "om_p", sender=B)
            assert tool.replace("/", "__") not in env.scripts.tools_seen(asked)[0]
            assert state.requests == [] and state.writes == []
            assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_silence_expired_during_approval_wait_is_rejected_before_io(
    env: Env, group_channel: GroupChannel
) -> None:
    with backend() as (url, state):
        args = create_args(env)
        args["ends_at"] = (env.clock() + timedelta(minutes=1)).isoformat()
        env.scripts.add(
            attributed(A, "提出"),
            tool_call(f"{SOURCE}__propose_create_silence", **args),
            cite(),
        )
        async with env.running(env.config(**s6_config(url)), feishu_channel=group_channel):
            await group_send(env, group_channel, "提出", "om_p")
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            count = len(state.requests)
            env.clock.advance(120)
            await group_send(env, group_channel, f"/批准 {action_id}", "om_a", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "rejected"
            assert len(state.requests) == count and state.writes == []
            receipt = message_text(group_channel.sends[-1][1])
            assert "未执行" in receipt and "重新提出" in receipt


async def test_future_silence_keeps_exact_normalized_times_and_self_approval(
    env: Env, group_channel: GroupChannel
) -> None:
    with backend() as (url, state):
        args = create_args(env)
        args.update(
            starts_at="2028-10-12T08:00:00.123456+08:00",
            ends_at="2029-10-12T08:00:00.456789+08:00",
        )
        state.write_result_updates["status"] = {"state": "pending"}
        env.scripts.add(
            attributed(B, "提出"),
            tool_call(f"{SOURCE}__propose_create_silence", **args),
            cite(),
        )
        async with env.running(env.config(**s6_config(url)), feishu_channel=group_channel):
            await group_send(env, group_channel, "提出", "om_p", sender=B)
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            assert state.writes == []
            await group_send(env, group_channel, f"/批准 {action_id}", "om_a", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "succeeded"
            result = json.loads(await env.scalar("SELECT result FROM xiaowei_action"))
            assert result["startsAt"] == "2028-10-12T00:00:00.123000+00:00"
            assert result["endsAt"] == "2029-10-12T00:00:00.456000+00:00"
            assert result["status"]["state"] == "pending"
            assert len(state.writes) == 1
            payload = state.writes[0][2]
            assert payload["startsAt"] == result["startsAt"]
            assert payload["endsAt"] == result["endsAt"]


async def test_cancel_readback_not_expired_is_unknown_and_never_replayed(
    env: Env, group_channel: GroupChannel
) -> None:
    with backend() as (url, state):
        state.write_result_updates["status"] = {"state": "active"}
        env.scripts.add(
            attributed(A, "取消"),
            tool_call(f"{SOURCE}__propose_cancel_silence", silence_id=SILENCE_ID),
            cite(),
        )
        async with env.running(env.config(**s6_config(url)), feishu_channel=group_channel):
            await group_send(env, group_channel, "取消", "om_p")
            action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
            await group_send(env, group_channel, f"/批准 {action_id}", "om_a", sender=B)
            assert await env.scalar("SELECT state FROM xiaowei_action") == "unknown"
            assert await env.scalar("SELECT result FROM xiaowei_action") is None
            assert state.writes == [("DELETE", f"/api/v2/silence/{SILENCE_ID}", None)]
            receipt = message_text(group_channel.sends[-1][1])
            assert "未知" in receipt and "核对" in receipt
            count = len(state.requests)
            await group_send(env, group_channel, f"/批准 {action_id}", "om_again", sender=B)
            assert len(state.requests) == count
            assert (
                await env.scalar(
                    "SELECT count(*) FROM xiaowei_evidence WHERE tool_id=:tool", {"tool": CANCEL}
                )
                == 0
            )


@pytest.mark.parametrize(
    "bad", ["past", "order", "naive", "empty_matchers", "default_matcher", "id", "url"]
)
async def test_invalid_create_can_be_corrected_with_zero_bad_io(
    env: Env, group_channel: GroupChannel, bad: str
) -> None:
    channel = group_channel
    with backend() as (url, state):
        args = create_args(env)
        broken = json.loads(json.dumps(args))
        if bad == "past":
            broken["ends_at"] = (env.clock() - timedelta(seconds=1)).isoformat()
        elif bad == "order":
            broken["ends_at"] = broken["starts_at"]
        elif bad == "naive":
            broken["ends_at"] = "2029-10-11T00:00:00"
        elif bad == "empty_matchers":
            broken["matchers"] = []
        elif bad == "default_matcher":
            broken["matchers"][0].pop("isEqual")
        else:
            broken[bad] = SILENCE_ID if bad == "id" else "http://another-source"
        asked = env.scripts.add(
            attributed(A, "提出"),
            tool_call(f"{SOURCE}__propose_create_silence", **broken),
            tool_call(f"{SOURCE}__propose_create_silence", **args),
            cite(),
        )
        async with env.running(env.config(**s6_config(url)), feishu_channel=channel):
            await group_send(env, channel, "提出", "om_p")
            assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 1
            assert len(state.requests) == 1 and state.requests[0][0] == "GET"
            assert state.writes == []
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1
            if bad == "past":
                feedback = json.loads(tool_outputs(env.scripts.calls[asked][1])[0])
                assert feedback["retry_this_turn"] is True
                assert "ends_at" in feedback["reason"] and "当前时间" in feedback["reason"]
                assert "多年" in feedback["reason"] and "31天" not in feedback["reason"]


async def test_expired_cancel_never_proposed_or_written(
    env: Env, group_channel: GroupChannel
) -> None:
    with backend() as (url, state):
        state.payloads[f"/api/v2/silence/{SILENCE_ID}"]["status"]["state"] = "expired"
        env.scripts.add(
            attributed(A, "取消已到期"),
            tool_call(f"{SOURCE}__propose_cancel_silence", silence_id=SILENCE_ID),
            cite(),
        )
        async with env.running(env.config(**s6_config(url)), feishu_channel=group_channel):
            await group_send(env, group_channel, "取消已到期", "om_expired")
            assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
            assert state.writes == [] and len(state.requests) == 1


async def test_large_matching_response_discloses_unknown_count(
    env: Env, group_channel: GroupChannel
) -> None:
    with backend() as (url, state):
        state.payloads["/api/v2/alerts"] *= 100
        config = s6_config(url)
        config["alertmanager_sources"][0]["max_response_bytes"] = 4000
        env.scripts.add(
            attributed(A, "提出"),
            tool_call(f"{SOURCE}__propose_create_silence", **create_args(env)),
            cite(),
        )
        async with env.running(env.config(**config), feishu_channel=group_channel):
            await group_send(env, group_channel, "提出", "om_p")
            assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 1
            material = message_text(group_channel.sends[-1][1])
            assert "数量未知" in material and "无法完整计数" in material
            assert "命中 0" not in material and "命中 100" not in material
            assert len(state.requests) == 1 and state.writes == []


@pytest.mark.parametrize("mutation", ["no_auth", "duplicate", "unselected"])
async def test_only_selected_authenticated_write_configuration(env: Env, mutation: str) -> None:
    config = s6_config("http://127.0.0.1:1")
    source = config["alertmanager_sources"][0]
    if mutation == "no_auth":
        source.update(username_ref=None, password_ref=None)
    elif mutation == "duplicate":
        source["write_actions"] = ["create_silence", "create_silence"]
    else:
        source["write_actions"] = ["update_silence"]
    with pytest.raises(ValueError):
        env.config(**config)
