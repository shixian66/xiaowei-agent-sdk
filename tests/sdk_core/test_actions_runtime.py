"""正式 serve 的 W5 群闭环：只替换模型/渠道/远端写及可信合成工具登记。"""

import asyncio
from typing import Any

import httpx2
import pytest
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_actions import (
    PROPOSE,
    SOURCE,
    STATUS,
    WRITE,
    ActionAdapter,
    ChangeArgs,
    request_execution,
)
from tests.sdk_core.test_group_gateway import (
    GroupChannel,
    raw_group_event,
    runtime_group,
)
from tests.sdk_core.test_group_gateway import (
    runtime_env as runtime_env,
)
from tests.sdk_core.test_group_identity import A, B
from tests.sdk_core.test_runtime import BUDGET_CONFIG, OPERATOR, Env, statements, until

from xiaowei import runtime
from xiaowei.actions import ActionBinding, ActionService, proposal_policy
from xiaowei.feishu import FeishuGateway, attributed
from xiaowei.governance import GovernedTools, Projection, ToolPolicy
from xiaowei.models import AUDIENCES, ToolContract
from xiaowei.starrocks_tools import QUERY_TOOLS

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize(
    ("interruption", "notify"),
    [
        (None, False),
        ("preclaim", False),
        ("read", False),
        ("write", False),
        ("feedback", False),
        pytest.param(None, True, id="notify_after_approval"),
    ],
)
async def test_formal_serve_proposes_approves_and_refuses_non_approver(
    runtime_env: Env,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str | None,
    notify: bool,
) -> None:
    env = runtime_env
    adapter = ActionAdapter(env.clock)
    proposal = ToolContract(
        tool_id=PROPOSE,
        target_id=SOURCE,
        policy_id="fixture.proposal",
        input_schema=ChangeArgs.model_json_schema(),
        description="提出合成动作；等待群批准",
    )
    write = ToolContract(
        tool_id=WRITE,
        target_id=SOURCE,
        policy_id="fixture.write",
        input_schema=ChangeArgs.model_json_schema(),
    )
    registered, monitoring = runtime._registered_tools, runtime._monitoring_catalog

    def synthetic_registered(config: runtime.ServeConfig) -> frozenset[str]:
        return registered(config) | {PROPOSE, WRITE}

    def synthetic_catalog(config: runtime.ServeConfig) -> Any:
        contracts, policies = monitoring(config)
        return (*contracts, proposal, write), (
            *policies,
            proposal_policy(proposal.policy_id, ChangeArgs, 10_000),
            ToolPolicy(
                write.policy_id,
                ChangeArgs,
                {a: Projection(("value",), 10_000) for a in AUDIENCES},
                effect="write",
            ),
        )

    def action_service(
        governance: GovernedTools,
        action_store: Any,
        *args: Any,
        bindings: Any = (),
        **kwargs: Any,
    ) -> ActionService:
        assert not bindings  # 本夹具配置没有实际监控写动作。
        service = ActionService(
            governance,
            action_store,
            *args,
            bindings=[
                ActionBinding(
                    proposal, write, adapter.prepare, adapter.execute, target_binding="synthetic-v1"
                )
            ],
            **kwargs,
        )
        if interruption == "preclaim":
            get_action, paused = action_store.get_action, False

            async def pause_once(action_id, identity):
                nonlocal paused
                if identity.subject_id == B and not paused:
                    paused = True
                    await asyncio.Event().wait()
                return await get_action(action_id, identity)

            monkeypatch.setattr(action_store, "get_action", pause_once)
        elif interruption == "feedback":
            status = service.status

            async def slow_status(ctx, request):
                await asyncio.sleep(2)
                return await status(ctx, request)

            monkeypatch.setattr(service, "status", slow_status)
        return service

    gateways: list[FeishuGateway] = []

    def observe_gateway(*args: Any, **kwargs: Any) -> FeishuGateway:
        gateway = FeishuGateway(*args, **kwargs)
        gateways.append(gateway)
        return gateway

    monkeypatch.setattr(runtime, "_registered_tools", synthetic_registered)
    monkeypatch.setattr(runtime, "_monitoring_catalog", synthetic_catalog)
    monkeypatch.setattr(runtime, "ActionService", action_service)
    monkeypatch.setattr(runtime, "FeishuGateway", observe_gateway)
    feishu = runtime_group(tools=[PROPOSE, STATUS])
    feishu["max_reply_chars"] = 3500
    notification = "notifications/send_webhook_message"
    requests = []
    options = {}
    if notify:
        monkeypatch.setenv(
            "XW_TEST_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/synthetic"
        )
        feishu["group"]["tools"].append(notification)
        options["webhook_bots"] = [
            {
                "bot_id": "ops",
                "alias": "ops_alert_bot",
                "description": "运维告警机器人",
                "target_version": "v1",
                "url_ref": "env:XW_TEST_WEBHOOK_URL",
                "signing_secret_ref": None,
                "receive_targets": [SOURCE],
                "receive_tools": [PROPOSE, STATUS, f"{SOURCE}/request_action_execution"],
            }
        ]
        opened = runtime.open_runtime

        async def post(request):
            assert len(adapter.calls) == 1  # 必须先经真实审批执行，随后才通知
            requests.append(request)
            return httpx2.Response(200, json={"code": 0})

        def with_webhook(*args, **kwargs):
            return opened(*args, **kwargs, webhook_transport=httpx2.MockTransport(post))

        monkeypatch.setattr(runtime, "open_runtime", with_webhook)
    config = env.config(
        alertmanager_sources=[
            {
                "server_id": SOURCE,
                "url": "http://127.0.0.1:1",
                "username_ref": None,
                "password_ref": None,
                "timeout_seconds": 2,
                "max_response_bytes": 64000,
                "tools": ["get_status"],
            }
        ],
        feishu=feishu,
        access={
            "policy_version": "w5",
            "grants": {
                B: [WRITE],
                **({A: [notification]} if notify else {}),
                OPERATOR: sorted(QUERY_TOOLS),
            },
        },
        data_policy={
            "input": {"max_bytes": 2000},
            "model_tools": [
                PROPOSE,
                STATUS,
                *sorted(QUERY_TOOLS),
                *([notification] if notify else []),
            ],
        },
        budget={
            **BUDGET_CONFIG,
            "timeout_seconds": 1 if interruption else 30,
        },
        **options,
    )
    channel = GroupChannel()

    def resumed(call):
        assert "send_webhook_message" not in call.tools
        assert "批准后不会自动通知，完成后请另发通知要求" in call.instructions
        return cite("动作已执行；未发送通知，完成后请另发通知要求。")(call)

    asked = env.scripts.add(
        attributed(A, "提出"),
        tool_call("am-synthetic__propose_change", value="after"),
        request_execution,
        resumed if notify else cite("等待批准"),
    )
    async with env.running(config, feishu_channel=channel) as served:
        channel.emit_raw_from_sdk_thread(raw_group_event(env, "提出", "om_p"))
        await until(lambda: len(channel.sends) >= 1)
        await gateways[0].idle()
        assert len(channel.sends) == 1
        if notify:
            assert "批准后不会自动通知，完成后请另发通知要求" in message_text(channel.sends[0][1])
        action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
        assert action_id is not None and adapter.calls == []
        offset = 0
        if notify:
            # 普通 completed 轮次也可展示仍可批准的材料，不能只在 paused 轮加提示。
            status_question = "查看待批准动作"
            env.scripts.add(
                attributed(A, status_question),
                tool_call("am-synthetic__get_action_status", action_id=action_id),
                cite("仍待批准"),
            )
            channel.emit_raw_from_sdk_thread(raw_group_event(env, status_question, "om_status"))
            await until(lambda: len(channel.sends) > 1)
            await gateways[0].idle()
            assert (
                await env.scalar(
                    "SELECT state FROM xiaowei_request WHERE reply_message_id='om_status'"
                )
                == "completed"
            )
            assert "批准后不会自动通知，完成后请另发通知要求" in "\n".join(
                message_text(message) for _, message, _ in channel.sends[1:]
            )
            assert requests == []
            offset = len(channel.sends) - 1
        channel.emit_raw_from_sdk_thread(raw_group_event(env, f"/批准 {action_id}", "om_denied", A))
        await until(lambda: len(channel.sends) >= 2 + offset)
        await gateways[0].idle()
        assert len(channel.sends) == 2 + offset
        assert adapter.calls == []
        if interruption == "read":
            adapter.read_release = asyncio.Event()
        elif interruption == "write":
            adapter.release = asyncio.Event()
        channel.emit_raw_from_sdk_thread(raw_group_event(env, f"/批准 {action_id}", "om_ok", B))
        expected_sends = (4 if interruption is None else 3) + offset
        await until(lambda: len(channel.sends) >= expected_sends)
        await gateways[0].idle()
        assert len(channel.sends) == expected_sends
        if interruption is None:
            assert [opts["reply_to"] for _, _, opts in channel.sends[-2:]] == ["om_ok", "om_ok"]
        assert [c.arguments for c in adapter.calls] == (
            [] if interruption in {"preclaim", "read"} else [{"value": "after"}]
        )
        assert (
            await env.scalar("SELECT state FROM xiaowei_action")
            == {
                None: "succeeded",
                "preclaim": "pending",
                "read": "rejected",
                "write": "unknown",
                "feedback": "succeeded",
            }[interruption]
        )
        assert len(env.scripts.calls[asked]) == (3 if interruption is None else 2)
        if notify:
            assert requests == []
            assert "未发送通知" in message_text(channel.sends[-1][1])
        assert len(env.scripts.calls) == (2 if notify else 1)  # 批准恢复原任务的模型脚本。
        assert (await served.ready())["status"] == "ready"
        if notify:
            env.scripts.add(
                attributed(A, "通知 ops_alert_bot"),
                tool_call(
                    "send_webhook_message",
                    bot_alias="ops_alert_bot",
                    text="动作已执行",
                    evidence_ids=[],
                ),
                cite(),
            )
            channel.emit_raw_from_sdk_thread(
                raw_group_event(env, "通知 ops_alert_bot", "om_notify")
            )
            await until(lambda: len(channel.sends) >= expected_sends + 1)
            await gateways[0].idle()
            assert len(requests) == 1
            assert await env.scalar("SELECT state FROM xiaowei_notification_attempt") == "accepted"
        if interruption:
            receipt = message_text(channel.sends[-1][1])
            assert "工具执行失败" not in receipt
            assert (
                await env.scalar(
                    "SELECT failure_code FROM xiaowei_request WHERE reply_message_id='om_ok'"
                )
                == {
                    "preclaim": "action_not_approvable",
                    "read": "action_rejected",
                    "write": "action_unknown",
                    "feedback": "action_feedback_failed",
                }[interruption]
            )
        # 正式 HTTP 入口仍可执行只读查询，证据来自真实治理/SQLGuard/存储链。
        await served.page()
        query = env.scripts.add(
            "各地区销售额",
            tool_call(
                "run_readonly_query",
                cluster=SR.target_id,
                sql="SELECT region, total FROM shop.sales",
            ),
            cite(),
        )
        response = await served.turn(query, "query", "after-approval")
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "completed"
        (fact,) = body["delivery"]["facts"]
        assert fact["tool_id"] == "local/run_readonly_query"
        assert fact["rows"] == [{"region": "east", "total": 100}, {"region": "west", "total": 50}]
        assert len(statements(env.drv)) == 1
        assert await served.finish() == 0
