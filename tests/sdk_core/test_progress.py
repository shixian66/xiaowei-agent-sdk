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
    times: list[float] = field(default_factory=list)

    async def create(self, message):
        self.cards.append(message)
        self.created.set()
        if self.block is not None:
            await self.block.wait()
        return self.create_id

    async def update(self, message_id, message):
        self.updates.append((message_id, message))
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


async def test_finish_cancels_inflight_creation_without_waiting_or_late_terminal():
    sink = Sink(block=asyncio.Event())
    progress = observer(sink)
    progress.start()
    await sink.created.wait()
    progress.finish("sent")
    await asyncio.sleep(0)
    await settled(progress)
    assert sink.updates == []
    assert progress.task.done()


async def test_finish_during_update_never_sends_a_competing_terminal_or_later_snapshot():
    sink = Sink(block_update=asyncio.Event())
    progress = observer(sink)
    progress.start()
    await sink.created.wait()
    progress.mark("model")
    await sink.update_started.wait()
    progress.finish("sent")
    await settled(progress)
    progress.mark("saving")
    sink.block_update.set()
    await asyncio.sleep(0)
    assert len(sink.updates) == 1
    assert "正式回复已送达" not in json.dumps(sink.updates, ensure_ascii=False)


async def test_slow_progress_stays_within_global_sender_bound_and_cleans_up():
    slots = asyncio.Semaphore(2)
    sink = Sink(block=asyncio.Event())
    observations = [observer(sink, slots=slots) for _ in range(12)]
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
        await sink.created.wait()
        return "PRIVATE_RESULT"

    progress.start()
    model = ScriptedModel(
        [
            [
                function_call("inspect_synthetic", {"secret": "PRIVATE_SQL"}, call_id="a"),
                function_call("inspect_synthetic", {"secret": "PRIVATE_URL"}, call_id="b"),
            ],
            [assistant_message("PRIVATE_MODEL")],
        ]
    )
    result = await Runner.run(
        Agent(name="private-agent", model=model, tools=[inspect_synthetic]),
        "PRIVATE_INPUT",
        hooks=progress.hooks,
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


async def test_transport_uses_real_sdk_http_and_schedule_and_rejects_missing_code(monkeypatch):
    import httpx
    from lark_channel import FeishuChannel
    from lark_channel.channel.types import SendResult
    from lark_channel.core.token import TokenManager
    from tests.sdk_core.test_feishu import FakeChannel, config

    from xiaowei.feishu import LarkTransport

    payloads = iter([{"code": 0}, {}, {"code": 230006}])
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=next(payloads))

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
    transport = LarkTransport(fake, config())
    card = {"card": {"schema": "2.0", "body": {"elements": []}}}
    try:
        await transport.start(lambda payload: asyncio.sleep(0))
        assert (
            await transport.create_progress("original-chat", card, "original-message") == "om_sent"
        )
        fake.result = SendResult.ok(message_id="")
        assert await transport.create_progress("original-chat", card, None) is None
        assert [await transport.update_progress("returned-id", card) for _ in range(3)] == [
            True,
            False,
            False,
        ]
        assert all(r.method == "PATCH" and r.url.path.endswith("/returned-id") for r in requests)
        assert all(json.loads(json.loads(r.content)["content"]) == card["card"] for r in requests)
        assert fake.sends[0][2]["reply_to"] == "original-message"
    finally:
        assert await transport.stop()
