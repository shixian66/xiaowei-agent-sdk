"""P1-B Task 7：飞书单聊入口。真 Runner + HTTP mock 脚本模型 + 受治理合成工具 + ChannelService +
隔离的真实 PostgreSQL；飞书侧是替身：``Outbox`` 代替单次文本发送，事件字典与 lark-channel-sdk 1.4.0
``on("raw")`` 交给处理器的结构相同（安装探针核对）。

替身不能证明：真实长连接、平台事件字段、平台重投、真实发送结果与断线行为（Task 7 实测项，需授权）。
"""

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from lark_channel.channel.errors import FeishuChannelErrorCode, SendError
from lark_channel.channel.types import SendResult
from pydantic import ValidationError
from tests.sdk_core.synthetic_tools import QUERY_TOOL
from tests.sdk_core.test_app import after, cite, tool_call
from tests.sdk_core.test_channel_service import Env, app_config
from tests.sdk_core.test_channel_service import env as env  # pytest fixture

from xiaowei.app import Application
from xiaowei.channel import ChannelService
from xiaowei.config import FeishuConfig
from xiaowei.feishu import (
    EMPTY_COMMAND,
    NEW_SESSION,
    NEW_SESSION_BUSY,
    TRUNCATED,
    FeishuGateway,
    LarkTransport,
    lark_channel,
    render,
    send_outcome,
)
from xiaowei.governance import GovernedTools
from xiaowei.storage import Readiness, hold_instance_lock

pytestmark = pytest.mark.loopback

QUERY_NAME = QUERY_TOOL.split("/", 1)[1]
APP_ID, TENANT = "cli_test", "tenant-1"
USERS = {"ou_alice": "alice", "ou_bob": "bob", "ou_carol": "carol"}  # carol 没有任何授权
CHATS = {"ou_alice": "oc_alice", "ou_bob": "oc_bob", "ou_carol": "oc_carol"}


def config(**overrides: Any) -> FeishuConfig:
    values: dict[str, Any] = {
        "app_id": APP_ID,
        "app_secret_ref": "env:XW_TEST_FEISHU_SECRET",
        "tenant_key": TENANT,
        "users": USERS,
        "max_event_age_seconds": 300,
        "max_message_chars": 200,
        "max_reply_chars": 2000,
        "queue_size": 4,
        "consumer_count": 1,
        "send_timeout_seconds": 5,
        "connect_timeout_seconds": 5,
    }
    values.update(overrides)
    return FeishuConfig(**values)


@dataclass
class Outbox:
    """单次文本发送的替身：记录 (chat_id, text)，按顺序返回预设结果或抛出。"""

    outcomes: list[Any] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)

    async def __call__(self, chat_id: str, text: str) -> Any:
        self.sent.append((chat_id, text))
        outcome = self.outcomes.pop(0) if self.outcomes else "sent"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def texts(self) -> list[str]:
        return [text for _, text in self.sent]


@dataclass
class Feishu:
    env: Env
    gateway: FeishuGateway
    outbox: Outbox
    _ids: Iterator[int] = field(default_factory=lambda: iter(range(1, 10_000)))

    def event(
        self,
        text: str | None = None,
        *,
        sender: str = "ou_alice",
        message_id: str | None = None,
        content: str | None = None,
        age_seconds: float = 0,
        **changes: Any,
    ) -> dict[str, Any]:
        """``on("raw")`` 交给处理器的 im.message.receive_v1 事件；``changes`` 用点路径覆盖字段。"""
        created = self.env.clock() - timedelta(seconds=age_seconds)
        millis = str(int(created.timestamp() * 1000))
        event: dict[str, Any] = {
            "schema": "2.0",
            "header": {
                "event_id": f"ev_{next(self._ids)}",
                "token": "",
                "create_time": millis,
                "event_type": "im.message.receive_v1",
                "tenant_key": TENANT,
                "app_id": APP_ID,
            },
            "event": {
                "sender": {
                    "sender_id": {"user_id": "u", "open_id": sender, "union_id": "on_u"},
                    "sender_type": "user",
                    "tenant_key": TENANT,
                },
                "message": {
                    "message_id": message_id or f"om_{next(self._ids)}",
                    "create_time": millis,
                    "chat_id": CHATS.get(sender, "oc_other"),
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": content if content is not None else json.dumps({"text": text}),
                },
            },
        }
        for path, value in changes.items():
            *parents, leaf = path.split("__")
            node = event
            for name in parents:
                node = node[name]
            node[leaf] = value
        return event

    async def drain(self) -> None:
        await self.gateway.idle()


@asynccontextmanager
async def running(
    env: Env, outbox: Outbox | None = None, service: ChannelService | None = None, **overrides: Any
) -> AsyncIterator[Feishu]:
    outbox = outbox or Outbox()
    gateway = FeishuGateway(service or env.service, config(**overrides), outbox, clock=env.clock)
    task = asyncio.create_task(gateway.run())
    try:
        yield Feishu(env, gateway, outbox)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@contextmanager
def logs() -> Iterator[list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger, handler = logging.getLogger("xiaowei.feishu"), Capture(logging.DEBUG)
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


# ---- 正常路径与用途 --------------------------------------------------------------------------


async def test_plain_text_is_a_diagnose_turn_answered_once(env: Env) -> None:
    async with running(env) as fs:
        message = env.scripts.add("飞书诊断", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(message))
        await fs.drain()
    assert all(QUERY_NAME not in seen for seen in env.scripts.tools_seen(message))
    assert env.model_calls(message) == 2 and len(env.adapter.calls) == 1
    ((chat_id, text),) = fs.outbox.sent
    assert chat_id == "oc_alice" and "来源 local/order_total" in text
    async with env.engine.connect() as conn:
        from sqlalchemy import text as sql

        row = (await conn.execute(sql("SELECT state, delivery FROM xiaowei_request"))).one()
    assert tuple(row) == ("completed", "sent")


async def test_query_command_applies_only_to_its_own_message(env: Env) -> None:
    async with running(env) as fs:
        query = env.scripts.add("看看东区", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(f"/查询  {query}"))
        await fs.drain()
        follow = env.scripts.add("接着看", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(follow))
        diagnose = env.scripts.add("显式诊断", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(f"/诊断 {diagnose}"))
        await fs.drain()
    assert all(QUERY_NAME in seen for seen in env.scripts.tools_seen(query))
    assert all(QUERY_NAME not in seen for seen in env.scripts.tools_seen(follow))
    assert all(QUERY_NAME not in seen for seen in env.scripts.tools_seen(diagnose))
    assert len(fs.outbox.sent) == 3


@pytest.mark.parametrize("command", ["/查询", "/诊断   ", "/查询\n\t "])
async def test_command_without_content_is_refused_before_the_model(env: Env, command: str) -> None:
    async with running(env) as fs:
        event = fs.event(command)
        await fs.gateway.receive(event)
        await fs.gateway.receive(event)  # 同一事件重投：提示也只发一次
        await fs.drain()
    assert fs.outbox.texts() == [EMPTY_COMMAND]
    assert await env.requests() == 0 and env.scripts.calls == {}


# ---- 入站拒绝：持久化与模型之前 --------------------------------------------------------------


REJECTED: dict[str, dict[str, Any]] = {
    "other event type": {"header__event_type": "im.message.reaction.created_v1"},
    "other app": {"header__app_id": "cli_other"},
    "other tenant": {"header__tenant_key": "tenant-2"},
    "sender tenant differs": {"event__sender__tenant_key": "tenant-2"},
    "bot sender": {"event__sender__sender_type": "bot"},
    "app sender": {"event__sender__sender_type": "app"},
    "unknown sender": {"sender": "ou_mallory"},
    "group chat": {"event__message__chat_type": "group"},
    "image": {"event__message__message_type": "image"},
    "post": {"event__message__message_type": "post"},
    "stale": {"age_seconds": 301},
    "from the future": {"age_seconds": -120},
    "blank text": {"content": json.dumps({"text": "  \n "})},
    "content not json": {"content": "not json"},
    "text not string": {"content": json.dumps({"text": 3})},
    "too long": {"content": json.dumps({"text": "长" * 201})},
    "missing message id": {"event__message__message_id": ""},
    "message id too long": {"event__message__message_id": "om_" + "x" * 198},
    "chat id too long": {"event__message__chat_id": "oc_" + "x" * 198},
    "missing chat": {"event__message__chat_id": None},
    "not a mapping": {"event__message": "x"},
}


@pytest.mark.parametrize("name", REJECTED)
async def test_unacceptable_events_never_reach_storage_or_the_model(env: Env, name: str) -> None:
    changes = dict(REJECTED[name])
    async with running(env) as fs:
        message = env.scripts.add("不应运行", tool_call("order_total", region="east"), cite())
        with logs() as records:
            await fs.gateway.receive(fs.event(message, **changes))
            await fs.drain()
    assert await env.requests() == 0 and env.scripts.calls == {} and fs.outbox.sent == []
    assert env.adapter.calls == []
    assert [r.getMessage() for r in records if r.levelno >= logging.INFO]  # 有安全原因码
    assert all(message not in r.getMessage() for r in records)


async def test_sender_without_current_authorization_is_dropped(env: Env) -> None:
    async with running(env) as fs:
        message = env.scripts.add("无授权", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(message, sender="ou_carol"))
        await fs.drain()
    assert await env.requests() == 0 and env.scripts.calls == {} and fs.outbox.sent == []


async def test_persistence_failure_drops_the_message_without_reply(env: Env) -> None:
    """契约（用户 2026-10-01 选择）：事件已由 SDK ack，落库失败时不处理、不回复。"""
    env.readiness.lock("injected")
    async with running(env) as fs:
        message = env.scripts.add("落库失败", tool_call("order_total", region="east"), cite())
        with logs() as records:
            await fs.gateway.receive(fs.event(message))
            await fs.drain()
    assert await env.requests() == 0 and env.scripts.calls == {} and fs.outbox.sent == []
    assert any("NotReadyError" in r.getMessage() for r in records)


# ---- 去重、重投与发送结果 --------------------------------------------------------------------


async def test_redelivered_event_runs_and_sends_once(env: Env) -> None:
    async with running(env) as fs:
        message = env.scripts.add("重投", tool_call("order_total", region="east"), cite())
        event = fs.event(message)
        await asyncio.gather(*(fs.gateway.receive(event) for _ in range(3)))
        await fs.drain()
        await fs.gateway.receive(event)
        await fs.drain()
    assert env.model_calls(message) == 2 and len(env.adapter.calls) == 1
    assert len(fs.outbox.sent) == 1 and await env.requests() == 1


async def test_same_message_id_from_two_users_does_not_collide(env: Env) -> None:
    async with running(env) as fs:
        a = env.scripts.add("甲的消息", tool_call("order_total", region="east"), cite())
        b = env.scripts.add("乙的消息", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(a, message_id="om_same"))
        await fs.gateway.receive(fs.event(b, sender="ou_bob", message_id="om_same"))
        await fs.drain()
    assert {chat for chat, _ in fs.outbox.sent} == {"oc_alice", "oc_bob"}
    assert await env.requests() == 2


async def test_completed_result_is_sent_once_when_the_event_is_redelivered(env: Env) -> None:
    """结果已保存但首次发送前进程退出：同一事件重投只发送保存的结果，不重跑。"""
    async with running(env) as fs:
        message = env.scripts.add("先保存后发送", tool_call("order_total", region="east"), cite())
        event = fs.event(message)
        inbound = fs.gateway.inbound(event)
        assert inbound is not None
        await env.service.process(await env.service.accept(inbound))
        assert fs.outbox.sent == []
        await fs.gateway.receive(event)
        await fs.gateway.receive(event)
        await fs.drain()
    assert len(fs.outbox.sent) == 1 and env.model_calls(message) == 2


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("failed", "failed"), ("unknown", "unknown"), (RuntimeError("boom"), "unknown")],
)
async def test_failed_or_unknown_send_is_not_retried(env: Env, outcome: Any, expected: str) -> None:
    async with running(env, Outbox([outcome])) as fs:
        message = env.scripts.add("发送失败", tool_call("order_total", region="east"), cite())
        event = fs.event(message)
        await fs.gateway.receive(event)
        await fs.drain()
        await fs.gateway.receive(event)  # 重投不再发送
        await fs.drain()
    assert len(fs.outbox.sent) == 1 and env.model_calls(message) == 2
    async with env.engine.connect() as conn:
        from sqlalchemy import text as sql

        delivery = await conn.scalar(sql("SELECT delivery FROM xiaowei_request"))
    assert delivery == expected


async def test_interrupted_request_gets_one_notice_on_redelivery(env: Env) -> None:
    async with running(env) as fs:
        event = fs.event("重启前接受")
        inbound = fs.gateway.inbound(event)
        assert inbound is not None
        await env.service.accept(inbound)  # 已接受，未运行即“重启”
    restarted = env.channel_store(readiness=Readiness())
    async with hold_instance_lock(env.engine, restarted.readiness) as lock:
        await restarted.recover(lock)
    service = ChannelService(env.app, env.results(restarted))
    async with running(env, service=service) as fs:
        await fs.gateway.receive(event)
        await fs.gateway.receive(event)
        await fs.drain()
    assert fs.outbox.texts() == ["上次处理已中断，请重新发送"]
    assert env.scripts.calls == {}


# ---- 队列、全局并发与会话 --------------------------------------------------------------------


async def test_full_queue_records_busy_and_sends_one_receipt(env: Env) -> None:
    outbox = Outbox()
    gateway = FeishuGateway(env.service, config(queue_size=1), outbox, clock=env.clock)
    fs = Feishu(env, gateway, outbox)  # 消费者未启动：第一条占满队列
    first = env.scripts.add("排队中", tool_call("order_total", region="east"), cite())
    second = env.scripts.add("队列已满", tool_call("order_total", region="east"), cite())
    await gateway.receive(fs.event(first))
    second_event = fs.event(second)
    await gateway.receive(second_event)
    await gateway.receive(second_event)  # 重投：已是 failed/busy，回执不再发送
    assert outbox.texts() == ["系统繁忙，本轮未执行，请稍后用新消息重试"]
    assert env.scripts.calls == {}
    async with env.engine.connect() as conn:
        from sqlalchemy import text as sql

        rows = (
            await conn.execute(
                sql("SELECT state, failure_code FROM xiaowei_request ORDER BY created_at, state")
            )
        ).all()
    assert [tuple(r) for r in rows] == [("accepted", None), ("failed", "busy")]


async def test_web_and_feishu_share_the_global_turn_limit(env: Env) -> None:
    app = Application(
        app_config(max_concurrent_turns=1),
        model=env.binding,
        engine=env.engine,
        governance=GovernedTools(env.evidence),
        local_tools={t: env.adapter.execute for t in env.app.available_tools},
        clock=env.clock,
    )
    service = ChannelService(app, env.results())
    gate, entered = asyncio.Event(), asyncio.Event()
    web = env.scripts.add(
        "网页占用", after(gate, tool_call("order_total", region="east"), entered=entered), cite()
    )
    web_turn = asyncio.create_task(service.process(await service.accept(env.inbound(web))))
    await asyncio.wait_for(entered.wait(), 10)
    async with running(env, service=service) as fs:
        busy = env.scripts.add("飞书被挡", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(busy))
        await fs.drain()
        gate.set()
        await web_turn
    assert fs.outbox.texts() == ["系统繁忙，本轮未执行，请稍后用新消息重试"]
    assert env.model_calls(busy) == 0


def test_consumers_cannot_exceed_the_global_turn_limit(env: Env) -> None:
    with pytest.raises(ValueError, match="consumer_count"):
        FeishuGateway(env.service, config(consumer_count=5), Outbox(), clock=env.clock)


async def test_new_session_command_switches_session_without_the_model(env: Env) -> None:
    async with running(env) as fs:
        first = env.scripts.add("旧会话", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(first))
        await fs.drain()
        before = await env.store.current_session("feishu", "alice", "oc_alice")
        command = fs.event("/新建")
        await fs.gateway.receive(command)
        await fs.gateway.receive(command)  # 重投不再新建
        after_new = await env.store.current_session("feishu", "alice", "oc_alice")
        second = env.scripts.add("新会话", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(second))
        await fs.drain()
    assert after_new.generation == before.generation + 1
    assert fs.outbox.texts()[1] == NEW_SESSION and len(fs.outbox.sent) == 3
    # 新会话没有旧历史：模型输入里看不到上一轮的证据
    assert env.model_calls(second) == 2 and "旧会话" not in json.dumps(
        [c.input for c in env.scripts.calls[second]], ensure_ascii=False
    )


async def test_new_session_while_running_is_refused(env: Env) -> None:
    gate, entered = asyncio.Event(), asyncio.Event()
    async with running(env) as fs:
        slow = env.scripts.add(
            "慢的一轮",
            after(gate, tool_call("order_total", region="east"), entered=entered),
            cite(),
        )
        await fs.gateway.receive(fs.event(slow))
        await asyncio.wait_for(entered.wait(), 10)
        before = await env.store.current_session("feishu", "alice", "oc_alice")
        await fs.gateway.receive(fs.event("/新建"))
        gate.set()
        await fs.drain()
    after_refused = await env.store.current_session("feishu", "alice", "oc_alice")
    assert after_refused.session_id == before.session_id
    assert fs.outbox.texts()[0] == NEW_SESSION_BUSY


async def test_cancelled_consumer_locks_readiness(env: Env) -> None:
    gate, entered = asyncio.Event(), asyncio.Event()
    gateway = FeishuGateway(env.service, config(), Outbox(), clock=env.clock)
    fs = Feishu(env, gateway, Outbox())
    slow = env.scripts.add(
        "运行中取消", after(gate, tool_call("order_total", region="east"), entered=entered), cite()
    )
    task = asyncio.create_task(gateway.run())
    await gateway.receive(fs.event(slow))
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not env.readiness.ok


# ---- 文本渲染与发送结果映射 -----------------------------------------------------------------


def test_render_keeps_short_text_and_truncates_long_text_on_a_line() -> None:
    assert render("短文本", 200) == "短文本"
    long = "\n".join(f"第{i}行" + "x" * 40 for i in range(50))
    shown = render(long, 300)
    assert len(shown) <= 300 and shown.endswith(TRUNCATED)
    body = shown.removesuffix("\n" + TRUNCATED)
    assert long.startswith(body) and long[len(body)] == "\n"
    assert render("y" * 1000, 250).endswith(TRUNCATED) and len(render("y" * 1000, 250)) <= 250


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (SendResult.ok(message_id="om_x"), "sent"),
        (
            SendResult.fail(SendError(code=FeishuChannelErrorCode.UNKNOWN, retryable=True)),
            "unknown",
        ),
        (
            SendResult.fail(SendError(code=FeishuChannelErrorCode.SEND_TIMEOUT, retryable=True)),
            "unknown",
        ),
        (
            SendResult.fail(
                SendError(code=FeishuChannelErrorCode.PERMISSION_DENIED, retryable=False)
            ),
            "failed",
        ),
        ("not a result", "unknown"),
    ],
)
def test_send_result_mapping(result: Any, expected: str) -> None:
    assert send_outcome(result) == expected


# ---- SDK 装配（不连网） ----------------------------------------------------------------------


def test_sdk_channel_is_configured_for_single_text_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XW_TEST_FEISHU_SECRET", "not-a-real-secret")
    channel = lark_channel(config(max_reply_chars=1500))
    cfg = channel.config
    assert cfg.outbound.retry.max_attempts == 1 and cfg.outbound.text_chunk_limit == 1500
    assert cfg.inbound.emit_raw_events and not cfg.inbound.include_raw
    assert not cfg.inbound.expand_merge_forward and not cfg.inbound.fetch_interactive_card
    assert not any(vars(cfg.inbound.media_capabilities).values())
    assert not cfg.resolve_sender_names
    assert cfg.policy.dm_policy == "disabled" and cfg.policy.group_policy == "disabled"
    assert cfg.transport.kind == "ws" and cfg.app_id == APP_ID


def test_feishu_config_validation() -> None:
    bad_values: tuple[dict[str, Any], ...] = (
        {"app_secret_ref": "plain-secret"},
        {"users": {}},
        {"users": {"ou_a": "same", "ou_b": "same"}},
        {"users": {"user-1": "alice"}},
        {"max_reply_chars": 100},
        {"app_id": "app"},
    )
    for bad in bad_values:
        with pytest.raises(ValidationError):
            config(**bad)


@dataclass
class FakeChannel:
    """FeishuChannel 的公开面：raw 处理器在另一个线程的事件循环上被调用；发送经 schedule。"""

    handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    result: Any = field(default_factory=lambda: SendResult.ok(message_id="om_sent"))
    delay: float = 0.0
    sends: list[tuple[Any, ...]] = field(default_factory=list)
    started: bool = False
    stopped: bool = False
    loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.new_event_loop)
    thread: threading.Thread = field(init=False)

    def __post_init__(self) -> None:
        def serve() -> None:
            try:
                self.loop.run_forever()
            finally:
                self.loop.close()

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()

    def on(self, name: str, handler: Callable[..., Any]) -> Callable[[], None]:
        self.handlers[name] = handler
        return lambda: None

    async def start_background(self, *, timeout: float) -> None:
        self.started = True

    async def stop_background(self) -> None:
        self.stopped = True
        self.loop.call_soon_threadsafe(self.loop.stop)
        await asyncio.to_thread(self.thread.join, 5)

    def schedule(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def send(self, to: str, message: Any, opts: Any = None) -> Any:
        assert asyncio.get_running_loop() is self.loop  # 发送在 SDK 的循环上执行
        self.sends.append((to, message, opts))
        await asyncio.sleep(self.delay)
        return self.result

    def emit_raw_from_sdk_thread(self, payload: dict[str, Any]) -> None:
        self.loop.call_soon_threadsafe(self.handlers["raw"], payload)


async def test_transport_bridges_raw_events_and_sends_across_loops() -> None:
    received: list[tuple[dict[str, Any], asyncio.AbstractEventLoop]] = []
    done = asyncio.Event()

    async def receive(payload: dict[str, Any]) -> None:
        received.append((payload, asyncio.get_running_loop()))
        done.set()

    fake = FakeChannel()
    transport = LarkTransport(fake, send_timeout_seconds=1)
    await transport.start(receive, connect_timeout_seconds=1)
    assert fake.started
    fake.emit_raw_from_sdk_thread({"k": "v"})
    await asyncio.wait_for(done.wait(), 5)
    assert received == [({"k": "v"}, asyncio.get_running_loop())]
    assert await transport.send("oc_1", "你好") == "sent"
    assert fake.sends == [("oc_1", {"text": "你好"}, {"receive_id_type": "chat_id"})]
    fake.delay = 5
    assert await transport.send("oc_1", "慢") == "unknown"  # 超时结果不明
    await transport.stop()
    assert fake.stopped


async def test_real_sdk_dispatcher_feeds_the_gateway_through_the_raw_bridge(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 SDK 的分发器 → ``on("raw")``（SDK 后台线程）→ 应用循环 → 共享服务 → 单次回复。

    只把传输改为 webhook，用公开的 ``handle_webhook_request`` 注入事件，不建立长连接。
    对外连接被 pytest-socket 阻断（域名解析仍会发生）：SDK 启动时的机器人身份查询因此失败并转入
    后台重试。长连接本身、平台事件与真实发送仍需在获准的测试应用中验证。
    """
    monkeypatch.setenv("XW_TEST_FEISHU_SECRET", "not-a-real-secret")
    channel = lark_channel(config())
    channel.config.transport.kind = "webhook"
    outbox = Outbox()
    gateway = FeishuGateway(env.service, config(), outbox, clock=env.clock)
    transport = LarkTransport(channel, send_timeout_seconds=1)
    fs = Feishu(env, gateway, outbox)
    consumers = asyncio.create_task(gateway.run())
    await transport.start(gateway.receive, connect_timeout_seconds=5)
    try:
        message = env.scripts.add("经真实分发器", tool_call("order_total", region="east"), cite())
        ignored = env.scripts.add("群聊消息", tool_call("order_total", region="east"), cite())
        for event in (fs.event(message), fs.event(ignored, event__message__chat_type="group")):
            status, _ = await channel.handle_webhook_request({}, json.dumps(event).encode())
            assert status == 200  # SDK 在处理前就应答成功
        async with asyncio.timeout(10):
            while not outbox.sent:
                await asyncio.sleep(0.02)
        await gateway.idle()
    finally:
        await transport.stop()
        consumers.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumers
    assert env.model_calls(message) == 2 and env.model_calls(ignored) == 0
    ((chat_id, text),) = outbox.sent
    assert chat_id == "oc_alice" and "来源 local/order_total" in text
