"""P1-B Task 7：飞书单聊入口。真 Runner + HTTP mock 脚本模型 + 受治理合成工具 + ChannelService +
隔离的真实 PostgreSQL；飞书侧是替身：``Outbox`` 代替单次文本发送，事件字典与 lark-channel-sdk 1.4.0
``on("raw")`` 交给处理器的结构相同（安装探针核对）。

替身不能证明：真实长连接、平台事件字段、平台重投、真实发送结果与断线行为（Task 7 实测项，需授权）。
"""

import asyncio
import dataclasses
import json
import logging
import re
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from lark_channel.channel import FeishuChannel
from lark_channel.channel.errors import FeishuChannelErrorCode, SendError
from lark_channel.channel.types import SendResult
from pydantic import ValidationError
from sqlalchemy import text as text_sql
from tests.sdk_core.synthetic_tools import QUERY_TOOL, TARGET
from tests.sdk_core.test_app import after, cite, tool_call
from tests.sdk_core.test_channel_service import Env, app_config
from tests.sdk_core.test_channel_service import env as env  # pytest fixture

from xiaowei.app import Application
from xiaowei.channel import ChannelService, InboundRequest, RequestReceipt, RequestRef
from xiaowei.channel_store import ChannelSession, ChannelStore, DeliveryClaim, RequestRecord
from xiaowei.config import FeishuConfig
from xiaowei.evidence import EvidenceStoreError
from xiaowei.feishu import (
    EMPTY_COMMAND,
    FACTS_TRUNCATED,
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
from xiaowei.models import Channel, Delivery, DeliveryLayout, FactLines, RunContext
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
        "stop_timeout_seconds": 5,
    }
    values.update(overrides)
    return FeishuConfig(**values)


@dataclass
class Outbox:
    """单次文本发送的替身：记录 (chat_id, text) 与回复的原消息，按顺序返回预设结果或抛出。"""

    outcomes: list[Any] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    replies: list[str | None] = field(default_factory=list)

    async def __call__(self, chat_id: str, text: str, *, reply_to: str | None = None) -> Any:
        self.sent.append((chat_id, text))
        self.replies.append(reply_to)
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


async def test_plain_text_is_a_default_turn_answered_once(env: Env) -> None:
    """P2.5 Task 7：普通文本由单 Agent 判断，查询工具按授权可见（不再默认诊断）。"""
    async with running(env) as fs:
        message = env.scripts.add("飞书普通消息", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(message))
        await fs.drain()
    assert all(QUERY_NAME in seen for seen in env.scripts.tools_seen(message))
    assert env.model_calls(message) == 2 and len(env.adapter.calls) == 1
    ((chat_id, text),) = fs.outbox.sent
    assert chat_id == "oc_alice" and "来源 local/order_total" in text
    async with env.engine.connect() as conn:
        from sqlalchemy import text as sql

        row = (await conn.execute(sql("SELECT state, delivery FROM xiaowei_request"))).one()
    assert tuple(row) == ("completed", "sent")


async def test_diagnose_command_applies_only_to_its_own_message(env: Env) -> None:
    async with running(env) as fs:
        query = env.scripts.add("看看东区", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(f"/查询  {query}"))
        await fs.drain()
        follow = env.scripts.add("接着看", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(follow))
        diagnose = env.scripts.add("显式诊断", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(f"/诊断 {diagnose}"))
        await fs.drain()
    # /查询 与普通文本相同（单 Agent 判断）；只有 /诊断 收窄本条消息，之后的普通文本恢复默认。
    assert all(QUERY_NAME in seen for seen in env.scripts.tools_seen(query))
    assert all(QUERY_NAME in seen for seen in env.scripts.tools_seen(follow))
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


REJECTED: dict[str, tuple[dict[str, Any], str]] = {
    "other event type": ({"header__event_type": "im.message.reaction.created_v1"}, "event_type"),
    "other app": ({"header__app_id": "cli_other"}, "app"),
    "other tenant": ({"header__tenant_key": "tenant-2"}, "tenant"),
    "sender tenant differs": ({"event__sender__tenant_key": "tenant-2"}, "tenant"),
    "bot sender": ({"event__sender__sender_type": "bot"}, "sender_type"),
    "app sender": ({"event__sender__sender_type": "app"}, "sender_type"),
    "unknown sender": ({"sender": "ou_mallory"}, "sender"),
    "group chat": ({"event__message__chat_type": "group"}, "chat_type"),
    "image": ({"event__message__message_type": "image"}, "message_type"),
    "post": ({"event__message__message_type": "post"}, "message_type"),
    "stale": ({"age_seconds": 301}, "stale"),
    "from the future": ({"age_seconds": -120}, "stale"),
    "blank text": ({"content": json.dumps({"text": "  \n "})}, "content"),
    "content not json": ({"content": "not json"}, "content"),
    "text not string": ({"content": json.dumps({"text": 3})}, "content"),
    "lone surrogate": ({"content": '{"text": "\\ud800"}'}, "content"),
    "too long": ({"content": json.dumps({"text": "长" * 201})}, "too_long"),
    "missing message id": ({"event__message__message_id": ""}, "malformed"),
    "message id too long": ({"event__message__message_id": "om_" + "x" * 198}, "malformed"),
    "chat id too long": ({"event__message__chat_id": "oc_" + "x" * 198}, "malformed"),
    "missing chat": ({"event__message__chat_id": None}, "malformed"),
    "not a mapping": ({"event__message": "x"}, "malformed"),
}


def dropped(records: list[logging.LogRecord]) -> list[str]:
    """日志中的丢弃原因码（日志只有原因码，不含正文或标识）。"""
    prefix = "飞书事件已丢弃："
    return [
        r.getMessage().removeprefix(prefix) for r in records if r.getMessage().startswith(prefix)
    ]


@pytest.mark.parametrize("name", REJECTED)
async def test_unacceptable_events_never_reach_storage_or_the_model(env: Env, name: str) -> None:
    changes, code = REJECTED[name]
    async with running(env) as fs:
        message = env.scripts.add("不应运行", tool_call("order_total", region="east"), cite())
        with logs() as records:
            await fs.gateway.receive(fs.event(message, **changes))
            await fs.drain()
    assert await env.requests() == 0 and env.scripts.calls == {} and fs.outbox.sent == []
    assert env.adapter.calls == []
    assert dropped(records) == [code]
    assert all(message not in r.getMessage() for r in records)


# 上限 1000 字符内的各种正文：序列化后远超 200 字符，JSON 转义最多膨胀 12 倍（代理对 emoji）。
LONG_TEXTS = {
    "500 中文": "长" * 500,
    "exactly the limit": "x" * 1000,
    "quotes and backslashes": '"\\' * 500,
    "control characters": "\x01\x02" * 500,
    "emoji": "😀" * 1000,
}


@pytest.mark.parametrize("ascii_only", [True, False], ids=["escaped", "raw"])
@pytest.mark.parametrize("name", LONG_TEXTS)
def test_text_up_to_the_configured_limit_is_accepted(env: Env, name: str, ascii_only: bool) -> None:
    gateway = FeishuGateway(env.service, config(max_message_chars=1000), Outbox(), clock=env.clock)
    text = LONG_TEXTS[name]
    content = json.dumps({"text": text}, ensure_ascii=ascii_only)
    assert len(content) > 200
    inbound = gateway.inbound(Feishu(env, gateway, Outbox()).event(content=content))
    assert inbound is not None and inbound.message == text


async def test_long_text_is_persisted_and_answered(env: Env) -> None:
    outbox = Outbox()
    gateway = FeishuGateway(env.service, config(max_message_chars=1000), outbox, clock=env.clock)
    fs = Feishu(env, gateway, outbox)
    await gateway.receive(fs.event("长" * 500))
    assert await env.requests() == 1


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (json.dumps({"text": "x" * 1001}), "too_long"),
        (json.dumps({"text": "😀" * 1001}), "too_long"),  # 转义后约 12 KB，仍在容器上限内
        (json.dumps({"text": "x" * 20_000}), "content_too_large"),
        ("{" * 20_000, "content_too_large"),  # 超大且不合法：在解析前按大小拒绝
        ("{", "content"),
        (json.dumps({"text": ["x"]}), "content"),
        (json.dumps(["x"]), "content"),
    ],
    ids=["limit+1", "emoji limit+1", "huge json", "huge malformed", "malformed", "list", "array"],
)
async def test_text_beyond_the_limit_is_rejected(env: Env, content: str, code: str) -> None:
    async with running(env, max_message_chars=1000) as fs:
        with logs() as records:
            await fs.gateway.receive(fs.event(content=content))
    assert dropped(records) == [code]
    assert await env.requests() == 0 and env.scripts.calls == {}


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
    # 恢复后的工作在持锁期间进行（与 serve 相同）；绑定的锁释放后存储不再开始新工作。
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
        local_tools={(t, TARGET): env.adapter.execute for t in env.app.available_tools},
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


# ---- 结果保存后到投递落定：取消与异常 -----------------------------------------------------


class ClaimBarrierStore(ChannelStore):
    """取得投递权前停住：结果已保存为 completed，投递仍是 pending。"""

    entered: asyncio.Event
    release: asyncio.Event

    async def claim_send(
        self, record: RequestRecord, *, resend: bool = False
    ) -> DeliveryClaim | None:
        self.entered.set()
        await self.release.wait()
        return await super().claim_send(record, resend=resend)


def feishu_ref(message_id: str, subject: str = "alice", chat: str = "oc_alice") -> RequestRef:
    return RequestRef(
        channel="feishu", subject_id=subject, conversation_id=chat, request_id=message_id
    )


async def request_row(env: Env) -> tuple[str, str]:
    async with env.engine.connect() as conn:
        row = (await conn.execute(text_sql("SELECT state, delivery FROM xiaowei_request"))).one()
    return row[0], row[1]


async def test_cancel_after_completed_before_claim_is_settled_by_recovery(env: Env) -> None:
    store = env.channel_store(ClaimBarrierStore)
    assert isinstance(store, ClaimBarrierStore)
    store.entered, store.release = asyncio.Event(), asyncio.Event()
    service = ChannelService(env.app, env.results(store))
    outbox = Outbox()
    gateway = FeishuGateway(service, config(), outbox, clock=env.clock)
    fs = Feishu(env, gateway, outbox)
    message = env.scripts.add("保存后取消", tool_call("order_total", region="east"), cite())
    event = fs.event(message)
    task = asyncio.create_task(gateway.run())
    await gateway.receive(event)
    await asyncio.wait_for(store.entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 结果已保存但未取得投递权：不能以 readiness 正常的状态继续接活。
    assert await request_row(env) == ("completed", "pending")
    assert not env.readiness.ok and outbox.sent == []
    # 停止并重启：持锁恢复把未发送的飞书结果记为 failed，之后只能显式重发，且不重跑。
    restarted = env.channel_store(readiness=Readiness())
    # 恢复后的工作在持锁期间进行（与 serve 相同）；绑定的锁释放后存储不再开始新工作。
    async with hold_instance_lock(env.engine, restarted.readiness) as lock:
        report = await restarted.recover(lock)
        assert report.unsent == 1
        assert await request_row(env) == ("completed", "failed")
        resend = Outbox()
        message_id = event["event"]["message"]["message_id"]

        async def transmit(delivery: Delivery) -> Any:
            return await resend("oc_alice", delivery.content)

        results = env.results(restarted)
        assert await results.send(feishu_ref(message_id), transmit) is None  # 首次发送路径不再发送
        assert await results.send(feishu_ref(message_id), transmit, resend=True) == "sent"
        assert len(resend.sent) == 1 and env.model_calls(message) == 2


async def test_recovery_leaves_web_results_pending(env: Env) -> None:
    """Web 结果经 GET 读取，从不发送：恢复不改它的投递状态。"""
    await env.run(env.scripts.add("网页结果", tool_call("order_total", region="east"), cite()))
    restarted = env.channel_store(readiness=Readiness())
    async with hold_instance_lock(env.engine, restarted.readiness) as lock:
        report = await restarted.recover(lock)
    assert report.unsent == 0
    assert await request_row(env) == ("completed", "pending")


async def test_cancel_during_send_records_unknown(env: Env) -> None:
    hold = asyncio.Event()

    class Hanging(Outbox):
        async def __call__(self, chat_id: str, text: str, *, reply_to: str | None = None) -> Any:
            self.sent.append((chat_id, text))
            await hold.wait()
            return "sent"

    outbox = Hanging()
    gateway = FeishuGateway(env.service, config(), outbox, clock=env.clock)
    fs = Feishu(env, gateway, outbox)
    task = asyncio.create_task(gateway.run())
    await gateway.receive(
        fs.event(env.scripts.add("发送中取消", tool_call("order_total", region="east"), cite()))
    )
    async with asyncio.timeout(10):
        while not outbox.sent:
            await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await request_row(env) == ("completed", "unknown")
    assert len(outbox.sent) == 1


def failing_delivery_validation(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """交付重验（``ResultDelivery`` 的 context 不授予任何工具）因证据存储故障失败；运行中的
    预校验与 Session 提交照常。"""
    original = env.evidence.validate_answer

    async def validate(answer: Any, context: RunContext, **kwargs: Any) -> Delivery:
        if not context.tool_scope:
            raise EvidenceStoreError("injected evidence read failure")
        return await original(answer, context, **kwargs)

    monkeypatch.setattr(env.evidence, "validate_answer", validate)


async def test_evidence_failure_before_claim_is_not_swallowed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    outbox = Outbox()
    gateway = FeishuGateway(env.service, config(), outbox, clock=env.clock)
    fs = Feishu(env, gateway, outbox)
    message = env.scripts.add("证据读不出", tool_call("order_total", region="east"), cite())
    event = fs.event(message)
    inbound = gateway.inbound(event)
    assert inbound is not None
    await env.service.process(await env.service.accept(inbound))
    failing_delivery_validation(env, monkeypatch)
    with pytest.raises(EvidenceStoreError):
        await gateway.receive(event)  # 重投触发首次发送；校验在取得投递权前失败
    assert await request_row(env) == ("completed", "pending")
    assert not env.readiness.ok and outbox.sent == []


async def test_unexpected_consumer_failure_stops_taking_work(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    outbox = Outbox()
    gateway = FeishuGateway(env.service, config(), outbox, clock=env.clock)
    fs = Feishu(env, gateway, outbox)
    failing_delivery_validation(env, monkeypatch)
    task = asyncio.create_task(gateway.run())
    await gateway.receive(
        fs.event(env.scripts.add("交付失败", tool_call("order_total", region="east"), cite()))
    )
    with pytest.raises(ExceptionGroup) as raised:
        await asyncio.wait_for(task, 10)
    assert raised.group_contains(EvidenceStoreError)
    assert await request_row(env) == ("completed", "pending")
    assert not env.readiness.ok and outbox.sent == []


async def test_drain_stops_intake_and_waits_for_delivery(env: Env) -> None:
    async with running(env) as fs:
        message = env.scripts.add("停止前的消息", tool_call("order_total", region="east"), cite())
        await fs.gateway.receive(fs.event(message))
        assert await fs.gateway.drain(10)
        with logs() as records:
            await fs.gateway.receive(fs.event(env.scripts.add("停止后", cite())))
    assert dropped(records) == ["closing"]
    assert len(fs.outbox.sent) == 1 and await env.requests() == 1
    assert env.readiness.ok


async def test_drain_timeout_with_queued_work_blocks_readiness(env: Env) -> None:
    gateway = FeishuGateway(env.service, config(), Outbox(), clock=env.clock)
    fs = Feishu(env, gateway, Outbox())  # 消费者未启动：请求停在队列中
    await gateway.receive(fs.event(env.scripts.add("排队未运行", cite())))
    assert not await gateway.drain(0.1)
    assert not env.readiness.ok
    assert await request_row(env) == ("accepted", "pending")


class GatedService(ChannelService):
    """在落库或新建会话前停住：``receive`` 已过关闭检查，请求尚未入队。"""

    entered: asyncio.Event
    release: asyncio.Event

    async def _hold(self) -> None:
        self.entered.set()
        await self.release.wait()

    async def accept(self, inbound: InboundRequest) -> RequestReceipt:
        await self._hold()
        return await super().accept(inbound)

    async def new_session(
        self, channel: Channel, subject_id: str, conversation_id: str, **kwargs: Any
    ) -> ChannelSession:
        await self._hold()
        return await super().new_session(channel, subject_id, conversation_id, **kwargs)


def gated(env: Env) -> GatedService:
    service = GatedService(env.app, env.results())
    service.entered, service.release = asyncio.Event(), asyncio.Event()
    return service


async def test_drain_waits_for_receive_before_enqueue(env: Env) -> None:
    service = gated(env)
    async with running(env, service=service) as fs:
        message = env.scripts.add("落库前停止", tool_call("order_total", region="east"), cite())
        receiving = asyncio.create_task(fs.gateway.receive(fs.event(message)))
        await asyncio.wait_for(service.entered.wait(), 5)
        draining = asyncio.create_task(fs.gateway.drain(10))
        await asyncio.sleep(0.2)
        assert not draining.done()  # 队列为空，但仍有 receive 在落库前
        service.release.set()
        assert await asyncio.wait_for(draining, 10)
        await receiving
    assert await request_row(env) == ("completed", "sent")
    assert len(fs.outbox.sent) == 1 and env.readiness.ok


async def test_drain_timeout_with_receive_in_flight_blocks_readiness(env: Env) -> None:
    service = gated(env)
    async with running(env, service=service) as fs:
        receiving = asyncio.create_task(
            fs.gateway.receive(fs.event(env.scripts.add("卡住", cite())))
        )
        await asyncio.wait_for(service.entered.wait(), 5)
        assert not await fs.gateway.drain(0.1)
        assert not env.readiness.ok
        service.release.set()
        await receiving


async def test_drain_waits_for_commands_and_duplicates(env: Env) -> None:
    """不入队的路径（/新建、空命令提示、重复事件的首次发送）同样计入关闭等待。"""
    hold = asyncio.Event()

    class Held(Outbox):
        async def __call__(self, chat_id: str, text: str, *, reply_to: str | None = None) -> Any:
            await hold.wait()
            return await super().__call__(chat_id, text)

    service = gated(env)
    service.release.set()
    outbox = Held()
    async with running(env, outbox, service=service) as fs:
        hold.set()
        duplicate = fs.event(
            env.scripts.add("重复事件", tool_call("order_total", region="east"), cite())
        )
        await fs.gateway.receive(duplicate)
        await fs.drain()
        hold.clear()  # 空命令提示停在发送
        service.release.clear()  # /新建与重复事件停在服务调用
        tasks = [
            asyncio.create_task(fs.gateway.receive(event))
            for event in (fs.event("/新建"), fs.event("/查询"), duplicate)
        ]
        await asyncio.sleep(0.2)
        draining = asyncio.create_task(fs.gateway.drain(10))
        await asyncio.sleep(0.2)
        assert not draining.done()
        hold.set()
        await asyncio.sleep(0.2)
        assert not draining.done()  # 空命令提示已发出，/新建与重复事件仍在途
        service.release.set()
        assert await asyncio.wait_for(draining, 10)
        await asyncio.gather(*tasks)
    assert outbox.texts()[1:] in ([NEW_SESSION, EMPTY_COMMAND], [EMPTY_COMMAND, NEW_SESSION])
    assert env.readiness.ok


# ---- 文本渲染与发送结果映射 -----------------------------------------------------------------


def plain(content: str) -> Delivery:
    """没有分段的交付（澄清、固定回执）。"""
    return Delivery(content=content, evidence_ids=(), channel="feishu")


def test_render_keeps_short_text_and_truncates_long_text_on_a_line() -> None:
    assert render(plain("短文本"), 200) == "短文本"
    long = "\n".join(f"第{i}行" + "x" * 40 for i in range(50))
    shown = render(plain(long), 300)
    assert len(shown) <= 300 and shown.endswith(TRUNCATED)
    body = shown.removesuffix("\n" + TRUNCATED)
    assert long.startswith(body) and long[len(body)] == "\n"
    one_line = render(plain("y" * 1000), 250)
    assert one_line.endswith(TRUNCATED) and len(one_line) <= 250


# ---- 飞书单条上限：先保住分析（审查 F2，用户 2026-10-02 选 a） ------------------------------

DIAGNOSIS = "分析建议（模型推断，未经系统核实）"
FACTS = "工具结果（系统根据证据生成）"
# 一行中的转义：单字符转义、\uXXXX、单元格中的 \|。
ESCAPED = "a\\u2028b\\u202ec\\\\d\\|e\\n"


def fact(index: int, rows: int, cell: str = "x" * 30) -> FactLines:
    return FactLines(
        head=(f"[ev_{index}] 来源 local/explain_query · 目标 sr · 采集于 t", f"说明：说明{index}"),
        # 每行文字各不相同，便于按原顺序核对保留了哪些行。
        body=(
            f"sql: SELECT {index}",
            f"| plan{index} |",
            *(f"| {cell}{index}-{i} |" for i in range(rows)),
        ),
    )


def laid_out(*facts: FactLines, analysis: tuple[str, ...] = ()) -> Delivery:
    layout = DeliveryLayout(facts_header=FACTS, facts=facts, analysis=analysis)
    return Delivery(
        content="\n".join(layout.lines()),
        evidence_ids=("ev_1",),
        channel="feishu",
        layout=layout,
    )


ANALYSIS = (DIAGNOSIS, "- 全表扫描；建议加日期过滤（依据：ev_1）", "- 分桶键倾斜（依据：ev_2）")


def test_render_is_unchanged_up_to_the_limit() -> None:
    delivery = laid_out(fact(1, 3), fact(2, 3), analysis=ANALYSIS)
    exact = len(delivery.content)
    assert render(delivery, exact) == delivery.content  # 刚好等于上限：逐字不变
    shown = render(delivery, exact - 1)
    assert shown != delivery.content and len(shown) <= exact - 1


def test_over_the_limit_keeps_the_analysis_and_every_fact_head() -> None:
    facts = (fact(1, 40), fact(2, 40), fact(3, 40))
    delivery = laid_out(*facts, analysis=ANALYSIS)
    shown = render(delivery, 1200)
    lines = shown.split("\n")

    assert len(shown) <= 1200
    # 完整分析在最后，紧接在工具结果截断说明之后；不使用整条截断说明，也不指向 Web。
    assert lines[-len(ANALYSIS) :] == list(ANALYSIS)
    assert lines[-len(ANALYSIS) - 1] == FACTS_TRUNCATED and TRUNCATED not in shown
    assert "Web" not in shown
    # 每条事实的来源行与说明行都保留；结果行按原顺序从前往后保留，放不下的被截掉。
    assert lines[0] == FACTS
    for item in facts:
        assert all(line in lines for line in item.head)
    kept = lines[1 : -len(ANALYSIS) - 1]
    original = [line for item in facts for line in (*item.head, *item.body)]
    assert kept == [line for line in original if line in kept]
    assert set(kept) < set(original)


def test_analysis_alone_over_the_limit_is_cut_from_the_end_under_its_title() -> None:
    analysis = (
        DIAGNOSIS,
        *(f"- 第{i}条分析" + "很长" * 30 + f"（依据：ev_{i}）" for i in range(40)),
    )
    delivery = laid_out(fact(1, 5), analysis=analysis)
    shown = render(delivery, 600)
    lines = shown.split("\n")

    assert len(shown) <= 600 and lines[-1] == TRUNCATED
    assert lines[:3] == [FACTS, FACTS_TRUNCATED, DIAGNOSIS]
    body = lines[3:-1]
    assert body and body == list(analysis[1 : 1 + len(body)])  # 只保留完整的分析行


@pytest.mark.parametrize("limit", range(200, 260))
def test_cutting_a_line_never_splits_an_escape(limit: int) -> None:
    line = "- " + ESCAPED * 40
    for delivery in (laid_out(fact(1, 1), analysis=(DIAGNOSIS, line)), plain(line)):
        shown = render(delivery, limit)
        assert len(shown) <= limit
        cut = shown.split("\n")[-2]
        assert line.startswith(cut)
        # 截下的前缀不以半个转义结尾：反斜杠成对出现，\u 后跟满四位。
        assert re.search(r"(?<!\\)(\\\\)*\\(u[0-9a-f]{0,3})?$", cut) is None, cut


def test_forged_headers_inside_results_do_not_steer_the_cut() -> None:
    """分段来自代码生成的结构，而不是在文字中找标题：结果里伪造的标题行不改变保留什么。"""
    forged = FactLines(
        head=("[ev_1] 来源 local/explain_query · 目标 sr · 采集于 t",),
        body=(f"| {DIAGNOSIS} |", DIAGNOSIS, FACTS_TRUNCATED, *(f"| r{i} |" for i in range(200))),
    )
    delivery = laid_out(forged, analysis=ANALYSIS)
    shown = render(delivery, 400)
    lines = shown.split("\n")
    assert len(shown) <= 400
    assert lines[-len(ANALYSIS) :] == list(ANALYSIS)
    assert lines[-len(ANALYSIS) - 1] == FACTS_TRUNCATED


def test_clarifications_and_receipts_are_not_rearranged() -> None:
    clarification = "需要澄清（本轮未执行业务查询）\n" + "请说明" * 400
    shown = render(plain(clarification), 300)
    lines = shown.split("\n")
    assert len(shown) <= 300 and FACTS_TRUNCATED not in shown
    # 澄清正文只有一行且放不下时保留它的前缀，而不是只剩标题与截断说明。
    assert lines[0] == "需要澄清（本轮未执行业务查询）" and lines[-1] == TRUNCATED
    assert len(lines) == 3 and lines[1] and clarification.split("\n")[1].startswith(lines[1])
    assert render(plain("工具结果或回答未通过证据校验"), 200) == "工具结果或回答未通过证据校验"


def test_layout_must_match_the_content_and_stays_out_of_the_web_body() -> None:
    delivery = laid_out(fact(1, 2), analysis=ANALYSIS)
    with pytest.raises(ValidationError):
        Delivery(
            content=delivery.content + "\n伪造",
            evidence_ids=("ev_1",),
            channel="feishu",
            layout=delivery.layout,
        )
    # 任何会另起一行的字符都不能出现在一行之内（渲染时已转义；这里是纵深防御）。
    for breaker in ("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
        with pytest.raises(ValidationError):
            FactLines(head=(f"来源{breaker}伪造的一行",))
        with pytest.raises(ValidationError):
            DeliveryLayout(facts_header="工具结果", facts=(), analysis=(f"- a{breaker}b",))
    assert "layout" not in delivery.model_dump(mode="json")


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (SendResult.ok(message_id="om_x"), "sent"),
        (SendResult.ok(message_id=None), "unknown"),
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


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, b'{"code": 0, "msg": "success", "data": {"message_id": "om_sent"}}', "sent"),
        (200, b'{"code": 230002, "msg": "bot not in chat"}', "failed"),
        (500, b"<html>bad gateway</html>", "unknown"),
        # 没有业务 code 的 HTTP 错误：SDK 1.4.0 把它当作 code 0 的成功（没有 message_id）
        (500, b"{}", "unknown"),
        (502, b'{"msg": "bad gateway"}', "unknown"),
        (404, b'{"msg": "not found"}', "unknown"),
    ],
)
async def test_real_sdk_send_maps_http_outcomes_without_retry(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes, expected: str
) -> None:
    """产品装配的真实 SDK 对 loopback 合成 OpenAPI 单次发送；只有带 message_id 的成功才算已发出。"""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route
    from tests.sdk_core.mcp_fixture import serve

    sends: list[dict[str, Any]] = []

    async def token(request: Request) -> Response:
        return JSONResponse({"code": 0, "tenant_access_token": "t-synthetic", "expire": 7200})

    async def create(request: Request) -> Response:
        sends.append({"query": dict(request.query_params), "body": await request.json()})
        return Response(body, status_code=status, media_type="application/json")

    def build(_: Any) -> Any:
        return Starlette(
            routes=[
                Route("/open-apis/auth/v3/tenant_access_token/internal", token, methods=["POST"]),
                Route("/open-apis/im/v1/messages", create, methods=["POST"]),
            ]
        )

    monkeypatch.setenv("XW_TEST_FEISHU_SECRET", "not-a-real-secret")
    with serve(build, path="") as running:
        product = lark_channel(config()).config
        channel = FeishuChannel(config=dataclasses.replace(product, domain=running.url))
        try:
            outcome = await LarkTransport(channel, config()).send("oc_alice", "合成回复")
        finally:
            channel.stop(join_timeout=5)
    assert outcome == expected
    assert len(sends) == 1  # 单次发送：不重试，也不换成其他请求
    assert sends[0]["query"] == {"receive_id_type": "chat_id"}
    assert sends[0]["body"]["receive_id"] == "oc_alice"
    assert json.loads(sends[0]["body"]["content"]) == {"text": "合成回复"}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"code": 0, "msg": "success", "data": {"message_id": "om_reply"}}', "sent"),
        # 官方：原消息已撤回 / 对操作者不可见；SDK 1.4.0 归为 unknown，平台已拒绝，未发出
        (b'{"code": 230011, "msg": "The message is recalled."}', "failed"),
        (b'{"code": 230050, "msg": "The message is invisible to the operator."}', "failed"),
        # 官方：单群限频；SDK 1.4.0 归为 target_revoked，fresh 会改发新消息
        (b'{"code": 230020, "msg": "This operation triggers the frequency limit."}', "failed"),
    ],
)
async def test_real_sdk_reply_maps_target_codes_without_fresh_send(
    monkeypatch: pytest.MonkeyPatch, body: bytes, expected: str
) -> None:
    """产品装配的真实 SDK 以 ``reply_target_gone="fail"`` 回复原消息：只发 1 次回复，不新建消息。"""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route
    from tests.sdk_core.mcp_fixture import serve

    replies: list[tuple[str, dict[str, Any]]] = []
    creates: list[dict[str, Any]] = []

    async def token(request: Request) -> Response:
        return JSONResponse({"code": 0, "tenant_access_token": "t-synthetic", "expire": 7200})

    async def reply(request: Request) -> Response:
        replies.append((request.path_params["message_id"], await request.json()))
        return Response(body, media_type="application/json")

    async def create(request: Request) -> Response:
        creates.append(await request.json())
        return JSONResponse({"code": 0, "data": {"message_id": "om_fresh"}})

    def build(_: Any) -> Any:
        return Starlette(
            routes=[
                Route("/open-apis/auth/v3/tenant_access_token/internal", token, methods=["POST"]),
                Route("/open-apis/im/v1/messages/{message_id}/reply", reply, methods=["POST"]),
                Route("/open-apis/im/v1/messages", create, methods=["POST"]),
            ]
        )

    monkeypatch.setenv("XW_TEST_FEISHU_SECRET", "not-a-real-secret")
    with serve(build, path="") as running:
        product = lark_channel(config()).config
        channel = FeishuChannel(config=dataclasses.replace(product, domain=running.url))
        try:
            future = channel.schedule(
                channel.send(
                    "oc_group",
                    {"text": "合成回复"},
                    {"reply_to": "om_origin", "reply_target_gone": "fail"},
                )
            )
            async with asyncio.timeout(10):
                outcome = send_outcome(await asyncio.wrap_future(future))
        finally:
            channel.stop(join_timeout=5)
    assert outcome == expected
    assert [message_id for message_id, _ in replies] == ["om_origin"]
    assert json.loads(replies[0][1]["content"]) == {"text": "合成回复"}
    assert creates == []


def test_feishu_config_validation() -> None:
    bad_values: tuple[dict[str, Any], ...] = (
        {"app_secret_ref": "plain-secret"},
        {"users": {"ou_a": "same", "ou_b": "same"}},
        {"users": {"user-1": "alice"}},
        {"max_reply_chars": 100},
        {"app_id": "app"},
    )
    for bad in bad_values:
        with pytest.raises(ValidationError):
            config(**bad)
    # 单聊名单可以为空（首次部署取得 open_id 前）；为空时单聊无人获权。
    assert config(users={}).users == {}


@dataclass
class FakeChannel:
    """FeishuChannel 的公开面：raw 处理器在另一个线程的事件循环上被调用；发送经 schedule。"""

    handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    result: Any = field(default_factory=lambda: SendResult.ok(message_id="om_sent"))
    delay: float = 0.0
    sends: list[tuple[Any, ...]] = field(default_factory=list)
    start_error: BaseException | None = None
    stop_hangs: bool = False
    started: bool = False
    stops: int = 0
    unblock_stop: threading.Event = field(default_factory=threading.Event)
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
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    def stop(self, *, join_timeout: float) -> None:
        """与 SDK 一样同步阻塞；``stop_hangs`` 模拟关闭一直不返回。"""
        self.stops += 1
        if self.stop_hangs:
            self.unblock_stop.wait()
        self.close_loop()

    def close_loop(self) -> None:
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)

    def schedule(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def send(self, to: str, message: Any, opts: Any = None) -> Any:
        assert asyncio.get_running_loop() is self.loop  # 发送在 SDK 的循环上执行
        self.sends.append((to, message, opts))
        await asyncio.sleep(self.delay)
        return self.result

    def emit_raw_from_sdk_thread(self, payload: dict[str, Any]) -> None:
        self.loop.call_soon_threadsafe(self.handlers["raw"], payload)

    async def flush(self) -> None:
        """等 SDK 循环处理完此前排入的回调。"""
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(asyncio.sleep(0), self.loop))


async def test_transport_bridges_raw_events_and_sends_across_loops() -> None:
    received: list[tuple[dict[str, Any], asyncio.AbstractEventLoop]] = []
    done = asyncio.Event()

    async def receive(payload: dict[str, Any]) -> None:
        received.append((payload, asyncio.get_running_loop()))
        done.set()

    fake = FakeChannel()
    transport = LarkTransport(fake, config(send_timeout_seconds=1))
    await transport.start(receive)
    assert fake.started
    fake.emit_raw_from_sdk_thread({"k": "v"})
    await asyncio.wait_for(done.wait(), 5)
    assert received == [({"k": "v"}, asyncio.get_running_loop())]
    assert await transport.send("oc_1", "你好") == "sent"
    assert fake.sends == [("oc_1", {"text": "你好"}, {"receive_id_type": "chat_id"})]
    fake.delay = 5
    assert await transport.send("oc_1", "慢") == "unknown"  # 超时结果不明
    assert await transport.stop() is True
    assert fake.stops == 1


async def test_bridge_admits_at_most_queue_size_events_in_flight() -> None:
    started, release = 0, asyncio.Event()

    async def receive(payload: dict[str, Any]) -> None:
        nonlocal started
        started += 1
        await release.wait()

    fake = FakeChannel()
    transport = LarkTransport(fake, config(queue_size=4))
    await transport.start(receive)
    with logs() as records:
        for i in range(200):
            fake.emit_raw_from_sdk_thread({"i": i})
        await fake.flush()
        await asyncio.sleep(0.1)
    assert started == 4 and transport.in_flight == 4
    assert dropped(records) == ["intake_full"] * 196
    assert all("'i'" not in r.getMessage() for r in records)
    release.set()
    async with asyncio.timeout(5):
        while transport.in_flight:
            await asyncio.sleep(0.01)
    fake.emit_raw_from_sdk_thread({"i": "after"})  # 名额释放后重新接收
    await fake.flush()
    async with asyncio.timeout(5):
        while started < 5:
            await asyncio.sleep(0.01)
    assert await transport.stop() is True
    with logs() as records:
        fake.handlers["raw"]({"i": "late"})  # 停止后 SDK 仍可能回调：直接丢弃
    assert dropped(records) == ["closing"] and started == 5


async def test_gateway_behind_the_bridge_runs_no_turn_for_dropped_events(env: Env) -> None:
    """桥接超限发生在持久化前：被丢弃的事件零请求、零 Runner、零模型、零工具。"""
    gate, entered = asyncio.Event(), asyncio.Event()
    outbox = Outbox()
    gateway = FeishuGateway(env.service, config(queue_size=1), outbox, clock=env.clock)
    fs = Feishu(env, gateway, outbox)
    original = gateway.receive

    async def slow_receive(payload: dict[str, Any]) -> None:
        entered.set()
        await gate.wait()
        await original(payload)

    fake = FakeChannel()
    transport = LarkTransport(fake, config(queue_size=1))
    await transport.start(slow_receive)
    first = env.scripts.add("桥内第一条", tool_call("order_total", region="east"), cite())
    second = env.scripts.add("桥外第二条", tool_call("order_total", region="east"), cite())
    fake.emit_raw_from_sdk_thread(fs.event(first))
    await asyncio.wait_for(entered.wait(), 5)
    fake.emit_raw_from_sdk_thread(fs.event(second))
    await fake.flush()
    gate.set()
    async with asyncio.timeout(5):
        while transport.in_flight:
            await asyncio.sleep(0.01)
    await transport.stop()
    assert await env.requests() == 1 and env.model_calls(second) == 0
    assert env.adapter.calls == []  # 第一条只入队，消费者未启动


async def test_stop_is_bounded_when_the_sdk_never_returns() -> None:
    fake = FakeChannel(stop_hangs=True)
    transport = LarkTransport(fake, config(stop_timeout_seconds=0.2))

    async def receive(payload: dict[str, Any]) -> None:
        return None

    await transport.start(receive)
    loop = asyncio.get_running_loop()
    began = loop.time()
    try:
        with logs() as records:
            assert await asyncio.wait_for(transport.stop(), 3) is False
    finally:
        fake.unblock_stop.set()  # 测试失败时也放行替身的关闭线程
    assert loop.time() - began < 2
    assert any("关闭超时" in r.getMessage() for r in records)
    assert await transport.stop() is False and fake.stops == 1  # 重复停止不再关闭


async def test_stop_after_failed_start_and_repeated_stop() -> None:
    fake = FakeChannel(start_error=RuntimeError("connect failed"))
    transport = LarkTransport(fake, config())

    async def receive(payload: dict[str, Any]) -> None:
        return None

    with pytest.raises(RuntimeError):
        await transport.start(receive)
    with logs() as records:
        fake.handlers["raw"]({"i": "after failed start"})
    assert dropped(records) == ["closing"] and transport.in_flight == 0
    assert await transport.stop() is True
    assert await transport.stop() is True and fake.stops == 1


async def test_stop_error_is_not_swallowed() -> None:
    class Broken(FakeChannel):
        def stop(self, *, join_timeout: float) -> None:
            self.close_loop()
            raise RuntimeError("stop failed")

    transport = LarkTransport(Broken(), config())
    with pytest.raises(RuntimeError, match="stop failed"):
        await transport.stop()


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
    transport = LarkTransport(channel, config())
    fs = Feishu(env, gateway, outbox)
    consumers = asyncio.create_task(gateway.run())
    await transport.start(gateway.receive)
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
        assert await transport.stop() is True
        consumers.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumers
    assert env.model_calls(message) == 2 and env.model_calls(ignored) == 0
    ((chat_id, text),) = outbox.sent
    assert chat_id == "oc_alice" and "来源 local/order_total" in text
