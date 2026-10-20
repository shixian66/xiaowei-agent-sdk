"""T5：真实 Runner hooks、进度合并和故障隔离；不调用外部服务。"""

import asyncio
import json
import time
from dataclasses import dataclass, field

import pytest
from agents import Agent, Runner, function_tool
from agents.testing import ScriptedModel, assistant_message, function_call


@dataclass
class Sink:
    cards: list[dict] = field(default_factory=list)
    updates: list[tuple[str, dict]] = field(default_factory=list)
    created: asyncio.Event = field(default_factory=asyncio.Event)
    block: asyncio.Event | None = None
    create_id: str | None = "message-one"
    updated: bool = True
    block_update: asyncio.Event | None = None
    update_started: asyncio.Event = field(default_factory=asyncio.Event)
    tools_shown: asyncio.Event = field(default_factory=asyncio.Event)
    times: list[float] = field(default_factory=list)

    async def create(self, message):
        self.cards.append(message)
        if "工具请求" in json.dumps(message, ensure_ascii=False):
            self.tools_shown.set()
        self.created.set()
        if self.block is not None:
            await self.block.wait()
        return self.create_id

    async def update(self, message_id, message):
        self.updates.append((message_id, message))
        if "工具请求" in json.dumps(message, ensure_ascii=False):
            self.tools_shown.set()
        self.times.append(time.monotonic())
        self.update_started.set()
        if self.block_update is not None:
            await self.block_update.wait()
        return self.updated


def observer(sink, **kwargs):
    from xiaowei.progress import Progress

    return Progress(sink.create, sink.update, grouped=False, delay=0.01, interval=0.04, **kwargs)


async def settled(progress):
    if progress.task is not None:
        await asyncio.wait_for(asyncio.shield(progress.task), 0.3)


async def test_short_task_and_unstarted_observer_never_create_a_card():
    sink = Sink()
    progress = observer(sink)
    progress.mark("model")
    await asyncio.sleep(0.02)
    assert sink.cards == []
    progress.start()
    progress.finish("sent")
    await settled(progress)
    assert sink.cards == sink.updates == []


async def test_finishing_before_the_display_threshold_reclaims_the_task_immediately():
    from xiaowei.progress import Progress

    sink = Sink()
    progress = Progress(sink.create, sink.update, grouped=True)
    progress.start()
    await asyncio.sleep(0)
    progress.finish("sent")
    try:
        await asyncio.wait_for(asyncio.shield(progress.task), 0.1)
        assert sink.cards == []
    finally:
        progress.stop()
        await asyncio.gather(progress.task, return_exceptions=True)


async def test_repeated_events_and_time_do_not_refresh_but_real_changes_are_coalesced():
    sink = Sink()
    progress = observer(sink)
    progress.start()
    await sink.created.wait()
    for _ in range(20):
        progress.mark("preparing")
    await asyncio.sleep(0.06)
    assert sink.updates == []
    progress.mark("model")
    progress.mark("saving")
    await asyncio.sleep(0.06)
    assert len(sink.updates) == 1
    assert "保存结果" in json.dumps(sink.updates[0], ensure_ascii=False)
    progress.finish("sent")
    await settled(progress)
    assert len(sink.cards) == 1
    assert sink.updates[-1][0] == "message-one"
    assert "正式回复已送达" in json.dumps(sink.updates[-1], ensure_ascii=False)


@pytest.mark.parametrize("failed_at", ["create", "update"])
async def test_unknown_creation_or_failed_update_stops_observation(failed_at):
    sink = Sink(create_id=None if failed_at == "create" else "message-one", updated=False)
    progress = observer(sink)
    progress.start()
    await sink.created.wait()
    await asyncio.sleep(0.02)
    progress.mark("model")
    await asyncio.sleep(0.06)
    progress.mark("saving")
    progress.finish("sent")
    await settled(progress)
    assert len(sink.cards) == 1
    assert len(sink.updates) == (0 if failed_at == "create" else 1)


@pytest.mark.parametrize("inflight", ["create", "update"])
@pytest.mark.parametrize(
    ("stage", "label"), [("sent", "正式回复已送达"), ("unsent", "正式回复尚未确认送达")]
)
async def test_finish_returns_immediately_then_sends_one_terminal_after_success(
    inflight, stage, label
):
    release = asyncio.Event()
    sink = Sink(
        block=release if inflight == "create" else None,
        block_update=release if inflight == "update" else None,
    )
    progress = observer(sink)
    progress.mark("model")
    progress.start()
    try:
        await asyncio.wait_for(sink.created.wait(), 0.3)
        if inflight == "update":
            progress.mark("saving")
            await asyncio.wait_for(sink.update_started.wait(), 0.3)
        previous = len(sink.updates)
        progress.finish(stage)
        progress.finish("approval")  # 已结束的观察不能被后续事件改写。
        progress.mark("tools")
        await asyncio.sleep(0)
        assert not progress.task.done()
        assert len(sink.updates) == previous  # 在途请求明确返回前不能并发收尾。
        release.set()
        await settled(progress)
        assert len(sink.cards) == 1
        assert len(sink.updates) == previous + 1
        assert label in json.dumps(sink.updates[-1], ensure_ascii=False)
        assert sum(label in json.dumps(card, ensure_ascii=False) for _, card in sink.updates) == 1
    finally:
        progress.stop()
        await asyncio.gather(progress.task, return_exceptions=True)


@pytest.mark.parametrize("inflight", ["create", "update"])
@pytest.mark.parametrize("outcome", ["failed", "timeout", "error", "cancelled"])
async def test_finish_during_unsuccessful_io_never_sends_a_terminal_or_retries(inflight, outcome):
    release = asyncio.Event()
    sink = Sink(
        block=release if inflight == "create" else None,
        block_update=release if inflight == "update" else None,
        create_id=None if inflight == "create" else "message-one",
        updated=False,
    )

    async def create(card):
        result = await sink.create(card)
        if outcome == "error" and inflight == "create":
            raise RuntimeError("PRIVATE_FAILURE")
        return result

    async def update(message_id, card):
        result = await sink.update(message_id, card)
        if outcome == "error" and inflight == "update":
            raise RuntimeError("PRIVATE_FAILURE")
        return result

    from xiaowei.progress import Progress

    progress = Progress(create, update, grouped=False, delay=0.01, interval=0.04, io_timeout=0.1)
    progress.start()
    try:
        await asyncio.wait_for(sink.created.wait(), 0.3)
        if inflight == "update":
            progress.mark("saving")
            await asyncio.wait_for(sink.update_started.wait(), 0.3)
        progress.finish("sent")
        if outcome == "cancelled":
            progress.stop()
        elif outcome != "timeout":
            release.set()
        await settled(progress)
        progress.mark("model")
        release.set()  # 超时/取消后即使平台稍后完成，也不再对该卡补发。
        await asyncio.sleep(0)
        assert len(sink.cards) == 1
        assert len(sink.updates) == (0 if inflight == "create" else 1)
        assert "正式回复已送达" not in json.dumps([sink.cards, sink.updates], ensure_ascii=False)
    finally:
        progress.stop()
        await asyncio.gather(progress.task, return_exceptions=True)


async def test_slow_progress_stays_within_global_sender_bound_and_cleans_up():
    slots = asyncio.Semaphore(2)
    sink = Sink(block=asyncio.Event())
    observations = [observer(sink, slots=slots, io_timeout=0.1) for _ in range(12)]
    for progress in observations:
        progress.start()
    await asyncio.sleep(0.04)
    assert len(sink.cards) == 2
    for progress in observations:
        progress.finish("sent")
    await asyncio.gather(*(settled(progress) for progress in observations))
    assert sink.updates == []
    assert not slots.locked()


async def test_update_timeout_disables_observation_without_retry():
    sink = Sink(block_update=asyncio.Event())
    progress = observer(sink, io_timeout=0.015)
    progress.start()
    await sink.created.wait()
    progress.mark("model")
    await settled(progress)
    progress.mark("saving")
    progress.finish("sent")
    assert len(sink.updates) == 1


async def test_real_runner_hooks_keep_parallel_tools_and_never_leak_payloads():
    sink = Sink()
    progress = observer(sink)
    entered = []
    barrier = asyncio.Event()

    @function_tool
    async def inspect_synthetic(secret: str) -> str:
        """合成读取，不访问外部服务。"""
        entered.append(secret)
        if len(entered) == 2:
            barrier.set()
        await barrier.wait()
        await sink.tools_shown.wait()
        return "PRIVATE_RESULT"

    progress.mark("model")
    progress.start()
    await sink.created.wait()  # 建卡可以早于工具阶段；不能把它当作工具已展示。
    model = ScriptedModel(
        [
            [
                function_call("inspect_synthetic", {"secret": "PRIVATE_SQL"}, call_id="a"),
                function_call("inspect_synthetic", {"secret": "PRIVATE_URL"}, call_id="b"),
            ],
            [assistant_message("PRIVATE_MODEL")],
        ]
    )
    result = await asyncio.wait_for(
        Runner.run(
            Agent(name="private-agent", model=model, tools=[inspect_synthetic]),
            "PRIVATE_INPUT",
            hooks=progress.hooks,
        ),
        2,
    )
    progress.finish("sent")
    await settled(progress)
    assert result.final_output == "PRIVATE_MODEL"
    assert set(entered) == {"PRIVATE_SQL", "PRIVATE_URL"}
    rendered = json.dumps([sink.cards, sink.updates], ensure_ascii=False)
    assert "工具请求" in rendered
    assert "PRIVATE" not in rendered and "private-agent" not in rendered
    assert "最终结果以正式回复为准" in rendered


async def test_hook_publication_failure_does_not_abort_real_runner(monkeypatch):
    sink = Sink()
    progress = observer(sink)

    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE_FAILURE")

    monkeypatch.setattr(progress, "_record", broken)
    progress.start()
    result = await Runner.run(
        Agent(name="test", model=ScriptedModel([[assistant_message("done")]])),
        "question",
        hooks=progress.hooks,
    )
    progress.finish("sent")
    await settled(progress)
    assert result.final_output == "done"
    assert sink.cards == sink.updates == []


@pytest.mark.loopback
@pytest.mark.parametrize("progress_result", ["off", "progress-card", None, "blocked"])
async def test_raw_feishu_entry_keeps_governance_and_delivery_when_progress_is_unknown(
    env,
    monkeypatch,
    progress_result,
):
    from tests.sdk_core.test_app import after, cite, tool_call
    from tests.sdk_core.test_feishu import Feishu, Outbox, config

    from xiaowei import feishu
    from xiaowei.progress import Progress

    class FastProgress(Progress):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, delay=0.01, interval=0.02)

    monkeypatch.setattr(feishu, "Progress", FastProgress)
    sink = Sink(
        create_id=progress_result, block=asyncio.Event() if progress_result == "blocked" else None
    )
    destinations = []

    async def create(chat_id, card, reply_to):
        destinations.append((chat_id, reply_to))
        return await sink.create(card)

    outbox = Outbox()
    gateway = feishu.FeishuGateway(
        env.service,
        config(),
        outbox,
        clock=env.clock,
        progress_create=None if progress_result == "off" else create,
        progress_update=None if progress_result == "off" else sink.update,
    )
    fs = Feishu(env, gateway, outbox)
    release = asyncio.Event()
    message = env.scripts.add(
        "T5 合成统计", after(release, tool_call("order_total", region="east")), cite()
    )
    consumers = asyncio.create_task(gateway.run())
    try:
        event = fs.event(message)
        await gateway.receive(event)
        if progress_result != "off":
            await asyncio.wait_for(sink.created.wait(), 2)
        release.set()
        await asyncio.wait_for(gateway.idle(), 2)
        await gateway.receive(event)  # 重投没有新执行或进度。
        await gateway.idle()
        assert len(outbox.sent) == 1
        assert "100" in "\n".join(outbox.texts())
        assert env.model_calls(message) == 2
        assert len(env.adapter.calls) == 1
        assert env.readiness.ok
        assert len(sink.cards) == (0 if progress_result == "off" else 1)
        assert destinations == ([] if progress_result == "off" else [("oc_alice", None)])
        assert "T5 合成统计" not in json.dumps(sink.cards, ensure_ascii=False)
    finally:
        consumers.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumers


from tests.sdk_core.test_channel_service import env as env  # noqa: E402
from tests.sdk_core.test_group_actions import actions as actions  # noqa: E402


@pytest.mark.loopback
@pytest.mark.parametrize(
    ("approve", "first_outcome"),
    [(True, "sent"), (False, "sent"), (None, "failed"), (None, "unknown")],
)
async def test_group_pause_and_new_approval_or_rejection_have_separate_observations(
    actions,
    monkeypatch,
    approve,
    first_outcome,
):
    from tests.sdk_core.test_app import after, cite, tool_call
    from tests.sdk_core.test_group_actions import request_execution
    from tests.sdk_core.test_group_gateway import running
    from tests.sdk_core.test_group_identity import CHAT, A, B

    from xiaowei import feishu
    from xiaowei.progress import Progress

    class FastProgress(Progress):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, delay=0.01, interval=0.02)

    monkeypatch.setattr(feishu, "Progress", FastProgress)
    env = actions.env
    sink, destinations = Sink(), []
    resumed_card = asyncio.Event()

    async def create(chat_id, card, reply_to):
        destinations.append((chat_id, reply_to))
        await sink.create(card)
        if len(sink.cards) == 2:
            resumed_card.set()
        return f"progress-{len(sink.cards)}"

    first_release, resume_release = asyncio.Event(), asyncio.Event()
    message = env.scripts.add(
        feishu.attributed(A, "改为 after"),
        after(first_release, tool_call("am-synthetic__propose_change", value="after")),
        request_execution,
        after(resume_release, cite("已根据审批结果收尾")),
    )
    async with running(
        env, max_reply_chars=3500, progress_create=create, progress_update=sink.update
    ) as group:
        group.outbox.outcomes.append(first_outcome)
        asking = asyncio.create_task(group.ask("改为 after", message_id="om_proposal"))
        await asyncio.wait_for(sink.created.wait(), 2)
        first_release.set()
        await asyncio.wait_for(asking, 3)
        assert actions.adapter.calls == []
        await asyncio.wait_for(asyncio.gather(*tuple(group.gateway._progress_tasks)), 1)
        first_cards = [card for message_id, card in sink.updates if message_id == "progress-1"]
        label = "等待审批" if first_outcome == "sent" else "正式回复尚未确认送达"
        assert label in json.dumps(first_cards[-1], ensure_ascii=False)
        assert "正式回复已送达" not in json.dumps(first_cards, ensure_ascii=False)
        assert await env.rows(
            "SELECT state, delivery FROM xiaowei_request WHERE reply_message_id=:id",
            id="om_proposal",
        ) == [("paused", first_outcome)]
        if first_outcome != "sent":
            return
        action_id = await actions.one()
        command = f"/{'批准' if approve else '拒绝'} {action_id}"
        asking = asyncio.create_task(group.ask(command, sender=B, message_id="om_resume"))
        await asyncio.wait_for(resumed_card.wait(), 2)
        resume_release.set()
        await asyncio.wait_for(asking, 3)
        await group.ask(command, sender=B, message_id="om_resume")
        assert len(actions.adapter.calls) == int(approve)
        assert env.model_calls(message) == 3
        assert destinations == [(CHAT, "om_proposal"), (CHAT, "om_resume")]
        assert len(sink.cards) == 2
        assert all(message_id in {"progress-1", "progress-2"} for message_id, _ in sink.updates)
        assert action_id not in json.dumps([sink.cards, sink.updates], ensure_ascii=False)
        assert await env.rows(
            "SELECT state, delivery FROM xiaowei_request WHERE reply_message_id=:id", id="om_resume"
        ) == [("completed", "sent")]


@pytest.mark.parametrize("reply_to", [None, "original-message"])
async def test_transport_uses_real_sdk_http_and_schedule_and_rejects_missing_code(
    monkeypatch, reply_to
):
    import httpx
    from lark_channel import FeishuChannel
    from lark_channel.core.token import TokenManager
    from tests.sdk_core.test_feishu import FakeChannel, config

    from xiaowei.feishu import LarkTransport

    payloads = iter([{"code": 0}, {}, {"code": 230006}])
    creates = iter(
        [
            {"code": 0, "data": {"message_id": "om_created"}},
            {"code": 0, "data": {}},
            {"data": {"message_id": "om_missing_code"}},
            {"code": 230006, "data": {"message_id": "om_rejected"}},
        ]
    )
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=next(payloads if request.method == "PATCH" else creates))

    client_type = httpx.AsyncClient
    monkeypatch.setattr(TokenManager, "get_self_tenant_token", lambda config: "synthetic-token")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            **(kwargs | {"transport": httpx.MockTransport(respond), "trust_env": False})
        ),
    )
    synthetic_value = "synthetic-progress-probe"
    channel = FeishuChannel(app_id=synthetic_value, app_secret=synthetic_value)
    fake = FakeChannel()
    fake.client = channel.client
    fake.send = channel.send
    transport = LarkTransport(fake, config())
    card = {"card": {"schema": "2.0", "body": {"elements": []}}}
    try:
        await transport.start(lambda payload: asyncio.sleep(0))
        assert await transport.create_progress("original-chat", card, reply_to) == "om_created"
        for _ in range(3):
            assert await transport.create_progress("original-chat", card, reply_to) is None
        assert [await transport.update_progress("returned-id", card) for _ in range(3)] == [
            True,
            False,
            False,
        ]
        assert all(r.url.path.endswith("/returned-id") for r in requests if r.method == "PATCH")
        assert all(json.loads(json.loads(r.content)["content"]) == card["card"] for r in requests)
        assert requests[0].url.path.endswith("/original-message/reply" if reply_to else "/messages")
        assert sum(request.method == "POST" for request in requests) == 4
    finally:
        assert await transport.stop()
