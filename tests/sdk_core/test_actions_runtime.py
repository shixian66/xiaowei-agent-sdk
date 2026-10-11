"""正式 serve 的 W5 群闭环：只替换模型/渠道/远端写及可信合成工具登记。"""

import asyncio
from typing import Any

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


@pytest.mark.parametrize("interruption", [None, "read", "write", "feedback"])
async def test_formal_serve_proposes_approves_and_refuses_non_approver(
    runtime_env: Env,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str | None,
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

    def action_service(governance: GovernedTools, *args: Any, **kwargs: Any) -> ActionService:
        service = ActionService(
            governance,
            *args,
            bindings=[
                ActionBinding(
                    proposal, write, adapter.prepare, adapter.execute, target_binding="synthetic-v1"
                )
            ],
            **kwargs,
        )
        if interruption == "feedback":
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
        access={"policy_version": "w5", "grants": {B: [WRITE], OPERATOR: sorted(QUERY_TOOLS)}},
        data_policy={
            "input": {"max_bytes": 2000},
            "model_tools": [PROPOSE, STATUS, *sorted(QUERY_TOOLS)],
        },
        budget={**BUDGET_CONFIG, "timeout_seconds": 1 if interruption else 30},
    )
    channel = GroupChannel()
    asked = env.scripts.add(
        attributed(A, "提出"),
        tool_call("am-synthetic__propose_change", value="after"),
        cite("等待批准"),
    )
    async with env.running(config, feishu_channel=channel) as served:
        channel.emit_raw_from_sdk_thread(raw_group_event(env, "提出", "om_p"))
        await until(lambda: len(channel.sends) == 1)
        await gateways[0].idle()
        action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
        assert action_id is not None and adapter.calls == []
        channel.emit_raw_from_sdk_thread(raw_group_event(env, f"/批准 {action_id}", "om_denied", A))
        await until(lambda: len(channel.sends) == 2)
        await gateways[0].idle()
        assert adapter.calls == []
        if interruption == "read":
            adapter.read_release = asyncio.Event()
        elif interruption == "write":
            adapter.release = asyncio.Event()
        channel.emit_raw_from_sdk_thread(raw_group_event(env, f"/批准 {action_id}", "om_ok", B))
        await until(lambda: len(channel.sends) == 3)
        await gateways[0].idle()
        assert [c.arguments for c in adapter.calls] == (
            [] if interruption == "read" else [{"value": "after"}]
        )
        assert (
            await env.scalar("SELECT state FROM xiaowei_action")
            == {
                None: "succeeded",
                "read": "rejected",
                "write": "unknown",
                "feedback": "succeeded",
            }[interruption]
        )
        assert len(env.scripts.calls[asked]) == 2
        assert len(env.scripts.calls) == 1  # 两次批准均未调用模型。
        assert (await served.ready())["status"] == "ready"
        if interruption:
            receipt = message_text(channel.sends[-1][1])
            assert "工具执行失败" not in receipt
            assert (
                await env.scalar(
                    "SELECT failure_code FROM xiaowei_request WHERE reply_message_id='om_ok'"
                )
                == {
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
                "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM sales"
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
