"""安全巡检经正式群请求/定时运行和通知账本；外部 I/O 均为替身。"""

import json
from dataclasses import replace

import pytest
from tests.p1b.test_starrocks_audit import AUDIT_TARGET
from tests.p1b.test_starrocks_security_audit import record
from tests.sdk_core.test_app import tool_call
from tests.sdk_core.test_group_identity import A
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_scheduled_agent import TASK, activate, agent_config
from tests.sdk_core.test_security_incomplete_results import cite_results
from tests.sdk_core.test_security_inspection_eval import ARGS, security_driver
from tests.sdk_core.test_webhook_notification import running, script

from xiaowei import runtime, starrocks_tools
from xiaowei.starrocks import StarRocksAdapter, StarRocksError, StarRocksErrorCode
from xiaowei.starrocks_security import SECURITY_TOOL

pytestmark = pytest.mark.loopback


def security_config(env):
    data = agent_config(env).model_dump(mode="json")
    data["targets"][0]["starrocks"] = AUDIT_TARGET.model_dump(mode="json")
    data["scheduled_tasks"]["automatic_tools"] = [SECURITY_TOOL]
    data["feishu"]["group"]["tools"].append(SECURITY_TOOL)
    data["data_policy"]["model_tools"].append(SECURITY_TOOL)
    data["access"]["grants"][A].append(SECURITY_TOOL)
    data["webhook_bots"][0]["receive_tools"].append(SECURITY_TOOL)
    return runtime.ServeConfig.model_validate(data)


@pytest.mark.parametrize("case", ["failed", "unclassified", "limited"])
async def test_scheduled_inspection_only_source_failure_marks_run_incomplete(
    env, monkeypatch, case
):
    env.drv = security_driver(record())
    if case == "failed":

        async def failed(*args):
            raise StarRocksError(StarRocksErrorCode.SERVER_TIMEOUT)

        monkeypatch.setattr(StarRocksAdapter, "security_audit", failed)
    else:
        original = starrocks_tools.analyze_security_rows

        def incomplete(*args, **kwargs):
            found = original(*args, **kwargs)
            return replace(
                found,
                **(
                    {"unclassified_candidate_rows": 3}
                    if case == "unclassified"
                    else {"scan_limited": True}
                ),
            )

        monkeypatch.setattr(starrocks_tools, "analyze_security_rows", incomplete)
    env.scripts.add(TASK, tool_call("inspect_audit_security", **ARGS), cite_results())
    async with running(env, monkeypatch, config=security_config(env)) as (group, requests, rt):
        await activate(env, group, tools=[SECURITY_TOOL])
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        expected = "failed" if case == "failed" else "completed"
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == expected
        sent = json.loads(requests[0].content)["content"]["text"]
        assert ("本次巡检未全部完成" in sent) == (case == "failed")
        if case == "failed":
            assert sent.startswith("⚪ 本次巡检未全部完成")
            assert "预算内" not in sent
        assert "scheduled_agent_failure_notice" not in sent
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 1


@pytest.mark.parametrize("recipient_allowed", [True, False])
async def test_manual_failure_only_notification_keeps_code_prefix_and_receiver_check(
    env,
    monkeypatch,
    recipient_allowed,
):
    env.drv = security_driver(record(), missing=("stmt",))
    data = security_config(env).model_dump(mode="json")
    if not recipient_allowed:
        data["webhook_bots"][0]["receive_tools"].remove(SECURITY_TOOL)
    message = script(
        env,
        "巡检这个窗口，然后通知运维群",
        tool_call("inspect_audit_security", **ARGS),
        tool_call(
            "send_webhook_message", bot_alias="ops_alert_bot", text="本窗说明", evidence_ids=[]
        ),
        cite_results(),
    )
    async with running(env, monkeypatch, config=runtime.ServeConfig.model_validate(data)) as (
        group,
        requests,
        _rt,
    ):
        await group.ask(message)
        assert len(requests) == int(recipient_allowed)
        if recipient_allowed:
            sent = json.loads(requests[0].content)["content"]["text"]
            assert sent.startswith("⚪ 本次巡检未全部完成") and "unsupported" in sent
        assert await env.scalar("SELECT state FROM xiaowei_request") == "completed"
        from tests.sdk_core.test_feishu_render import message_text

        assert "本次巡检未全部完成" in message_text(group.outbox.sent[-1][1])
        saved = json.loads(await env.scalar("SELECT answer FROM xiaowei_request"))
        assert saved["inspection_failures"][0]["reason"] == "unsupported"


@pytest.mark.parametrize("revoked", [False, True])
async def test_paused_failure_rechecks_source_before_runner_and_activation(
    env, monkeypatch, revoked
):
    from datetime import timedelta

    from tests.sdk_core.test_group_actions import request_execution

    from xiaowei.feishu import attributed

    env.drv = security_driver(record(), missing=("stmt",))
    message = "先巡检，然后创建提醒"
    key = attributed(A, message)
    env.scripts.add(
        key,
        tool_call("inspect_audit_security", **ARGS),
        tool_call(
            "scheduled__propose_reminder",
            bot_alias="ops_alert_bot",
            text="提醒",
            evidence_ids=[],
            at=(env.clock() + timedelta(seconds=60)).isoformat(),
        ),
        request_execution,
        cite_results("完成"),
    )
    async with running(env, monkeypatch, config=security_config(env)) as (group, requests, rt):
        await group.ask(message)
        action = await env.scalar("SELECT action_id FROM xiaowei_action")
        assert action is not None and len(env.scripts.calls[key]) == 3
        if revoked:
            access = rt.service._access
            access._group = replace(access._group, tools=access._group.tools - {SECURITY_TOOL})
        await group.ask(f"/批准 {action}")
        assert len(env.scripts.calls[key]) == (3 if revoked else 4)
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == (
            "draft" if revoked else "active"
        )
        if revoked:
            assert (
                await env.scalar("SELECT failure_code FROM xiaowei_action")
                == "action_requester_invalid"
            )
        assert requests == []


@pytest.mark.parametrize("proxy", [False, True])
async def test_61_security_samples_notification_is_short_and_history_retains_rows(
    env, monkeypatch, proxy
):
    from tests.sdk_core.test_group_identity import CHAT, GROUP
    from tests.sdk_core.test_starrocks_tools import tool_outputs
    from tests.sdk_core.test_webhook_proxy import PROXY, proxy_config

    from xiaowei.channel import RequestRef

    env.drv = security_driver(
        *(
            record("INSERT OVERWRITE t SELECT 1", queryId=f"q-plain-{i}", user="etl-reader")
            for i in range(61)
        ),
        totals=(61, 0, 0),
    )
    data = security_config(env).model_dump(mode="json")
    data["targets"][0]["starrocks"]["audit"].update(max_rows=100, candidate_rows=200)
    data["model"]["max_request_bytes"] = 256_000  # 61 条样本的合成 Profile；不修改生产配置。
    cfg = runtime.ServeConfig.model_validate(data)
    if proxy:
        monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
        cfg = proxy_config(env, cfg)
    summary = (
        "🟡 核对变更。本次代表样本中 etl-reader 有61次覆盖写入，归纳数字为模型说明；"
        "建议对照发布记录。"
    )

    def notify(call):
        outputs = [json.loads(v) for v in tool_outputs(call)]
        assert all("evidence_id" in value for value in outputs), [
            (value.get("status"), value.get("code"), value.get("reason")) for value in outputs
        ]
        ids = [value["evidence_id"] for value in outputs]
        return tool_call(
            "send_webhook_message", bot_alias="ops_alert_bot", text=summary, evidence_ids=ids
        )(call)

    message = script(
        env,
        "巡检后发送简短报告",
        tool_call("inspect_audit_security", **ARGS),
        notify,
        cite_results(),
    )
    async with running(env, monkeypatch, config=cfg) as (group, requests, rt):
        await group.ask(message, message_id="om_plain_61")
        assert len(requests) == 1, [len(calls) for calls in env.scripts.calls.values()]
        body = json.loads(requests[0].content)
        sent = body["message"] if proxy else body["content"]["text"]
        assert summary in sent and sent.count("etl-reader") == 1
        assert "代表样本 61 条未在消息中列出" in sent and "覆盖写入 61" in sent
        assert "第 1 行" not in sent and "q-plain-" not in sent
        if proxy:
            assert body["openUrl"] == cfg.webhook_bots[0].open_url
        view = await rt.service.results.view(
            RequestRef(
                channel="feishu",
                subject_id=A,
                conversation_id=CHAT,
                group=GROUP,
                request_id="om_plain_61",
            )
        )
        source = next(f for f in view.delivery.facts if f.tool_id == SECURITY_TOOL)
        assert len(source.rows) == len(json.loads(source.result_json)["rows"]) == 61
        assert "第 1 行" not in view.delivery.content
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 1
