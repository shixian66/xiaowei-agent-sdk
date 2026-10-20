"""进度设计的锁版 SDK 实验；不实现产品观察器，不访问模型、数据库或飞书。"""

import asyncio
import json

import pytest
from agents import Agent, RunHooks, Runner, function_tool
from agents.exceptions import UserError
from agents.testing import ScriptedModel, assistant_message, function_call
from lark_channel import FeishuChannel
from lark_channel.api.im.v1.model.patch_message_request import PatchMessageRequest
from lark_channel.api.im.v1.model.patch_message_request_body import PatchMessageRequestBody
from lark_channel.api.im.v1.model.patch_message_response import PatchMessageResponse


def scripted_agent(tool, calls):
    return Agent(
        name="progress-contract-probe",
        tools=[tool],
        model=ScriptedModel([calls, [assistant_message("finished")]]),
    )


async def test_tool_end_also_fires_for_a_rejection_returned_as_data():
    events = []

    class Hooks(RunHooks):
        async def on_llm_start(self, context, agent, system_prompt, input_items):
            events.append("model_start")

        async def on_llm_end(self, context, agent, response):
            events.append("model_end")

        async def on_tool_start(self, context, agent, tool):
            events.append(("tool_start", context.tool_call_id))

        async def on_tool_end(self, context, agent, tool, result):
            events.append(("tool_end", context.tool_call_id, result))

    @function_tool
    async def refused() -> str:
        """返回一个合成的治理拒绝。"""
        return '{"status":"rejected"}'

    agent = scripted_agent(refused, [function_call("refused", {}, call_id="c1")])
    result = await Runner.run(agent, "probe", hooks=Hooks())
    assert result.final_output == "finished"
    assert events == [
        "model_start",
        "model_end",
        ("tool_start", "c1"),
        ("tool_end", "c1", '{"status":"rejected"}'),
        "model_start",
        "model_end",
    ]


async def test_runner_waits_for_tool_start_hook_before_invoking_tool():
    entered = asyncio.Event()
    release = asyncio.Event()
    executed = []

    class BlockingHook(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            entered.set()
            await release.wait()

    @function_tool
    async def read() -> str:
        """记录合成调用。"""
        executed.append("read")
        return "ok"

    agent = scripted_agent(read, [function_call("read", {}, call_id="c1")])
    running = asyncio.create_task(Runner.run(agent, "probe", hooks=BlockingHook()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert executed == []
        assert not running.done()
    finally:
        release.set()
        await asyncio.wait_for(running, timeout=2)
    assert executed == ["read"]


async def test_hook_failure_propagates_and_prevents_tool_invocation():
    executed = []

    class BrokenHook(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            raise RuntimeError("synthetic-observer-failure")

    @function_tool
    async def read() -> str:
        """记录合成调用。"""
        executed.append("read")
        return "ok"

    agent = scripted_agent(read, [function_call("read", {}, call_id="c1")])
    with pytest.raises(UserError, match="synthetic-observer-failure") as caught:
        await Runner.run(agent, "probe", hooks=BrokenHook())
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert executed == []


async def test_native_concurrent_tools_have_distinct_hook_call_ids():
    both_started = asyncio.Event()
    started = set()
    ended = set()

    class Hooks(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            started.add(context.tool_call_id)
            if len(started) == 2:
                both_started.set()

        async def on_tool_end(self, context, agent, tool, result):
            ended.add(context.tool_call_id)

    @function_tool
    async def read() -> str:
        """等待第二个合成工具进入，验证 SDK 原有并发。"""
        await asyncio.wait_for(both_started.wait(), timeout=2)
        return "ok"

    agent = scripted_agent(
        read, [function_call("read", {}, call_id=call_id) for call_id in ("c1", "c2")]
    )
    result = await Runner.run(agent, "probe", hooks=Hooks())
    assert result.final_output == "finished"
    assert started == ended == {"c1", "c2"}


async def test_approval_pause_does_not_mean_the_tool_has_started():
    started = []
    executed = []

    class Hooks(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            started.append(context.tool_call_id)

    @function_tool(needs_approval=True)
    async def apply_change() -> str:
        """只记录合成动作，不执行任何业务写。"""
        executed.append("applied")
        return "ok"

    agent = scripted_agent(
        apply_change, [function_call("apply_change", {}, call_id="approval-c1")]
    )
    paused = await Runner.run(agent, "probe", hooks=Hooks())
    assert len(paused.interruptions) == 1
    assert started == executed == []
    state = paused.to_state()
    state.approve(paused.interruptions[0])
    resumed = await Runner.run(agent, state, hooks=Hooks())
    assert resumed.final_output == "finished"
    assert started == ["approval-c1"]
    assert executed == ["applied"]


@pytest.mark.parametrize("details", [{}, {"msg": "success"}])
async def test_card_update_loses_missing_code_but_public_response_preserves_it(
    monkeypatch, details
):
    synthetic_value = "synthetic-progress-probe"
    channel = FeishuChannel(app_id=synthetic_value, app_secret=synthetic_value)
    card = {"elements": []}
    request = (
        PatchMessageRequest.builder()
        .message_id("synthetic-message")
        .request_body(PatchMessageRequestBody.builder().content(json.dumps(card)).build())
        .build()
    )
    payload = {}

    async def apatch(sent_request):
        assert sent_request.message_id == "synthetic-message"
        assert json.loads(sent_request.request_body.content) == card
        return PatchMessageResponse(payload)

    # 只替换公开 HTTP 调用边界；真实 Channel 归一化与响应类型仍运行。
    monkeypatch.setattr(channel.client.im.v1.message, "apatch", apatch)
    normalized = []
    typed = []
    for payload in ({"code": 0, **details}, details):
        typed.append(await channel.client.im.v1.message.apatch(request))
        assert typed[-1].code == payload.get("code")
        normalized.append(await channel.update_card("synthetic-message", card))

    assert normalized[0].raw == normalized[1].raw
    assert all(result.success for result in normalized)
    assert all(result.message_id == "synthetic-message" for result in normalized)
    assert typed[0].code == 0 and typed[0].success()
    assert typed[1].code is None and not typed[1].success()
