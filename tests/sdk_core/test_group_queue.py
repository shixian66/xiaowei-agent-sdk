"""飞书单群 F3：同群有界 FIFO、排队提示、等待期限、生命周期与恢复。

证据层次与 F2 相同：原始事件字典 → ``FeishuGateway`` → ``ChannelService`` → 真 Runner + 脚本模型
（HTTP mock）→ 受治理合成工具 → 真 PolicySession / SQLAlchemySession / 隔离 PostgreSQL →
``ResultDelivery`` → 发送替身；授权用产品 ``StaticAccess``，成员目录是可控替身。为了控制先后，
``Application.run_turn`` 外包一层只挂起与计数的替身（不改变调用参数与结果）。恢复用例直接在
真实 PostgreSQL 上构造中断前状态并调用产品 ``ChannelStore.recover``。正式入口用例经
``runtime.serve`` 的正式装配与 SDK 公开面替身。

替身不能证明：真实平台的并发投递与重投节奏、真实模型在多人排队时的理解、长连接停机耗时
（P3 在获准群验证）。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from tests.sdk_core.test_app import cite, clarify, tool_call
from tests.sdk_core.test_feishu import Outbox
from tests.sdk_core.test_group_gateway import (
    BOT,
    Group,
    GroupChannel,
    raw_group_event,
    runtime_group,
    states,
)
from tests.sdk_core.test_group_gateway import group_config as base_config
from tests.sdk_core.test_group_gateway import runtime_env as runtime_env  # pytest fixture
from tests.sdk_core.test_group_identity import CHAT, A, B, C, Env
from tests.sdk_core.test_group_identity import env as env  # pytest fixture
from tests.sdk_core.test_runtime import Env as RuntimeEnv
from tests.sdk_core.test_runtime import until

from xiaowei.app import TurnError
from xiaowei.config import FeishuConfig
from xiaowei.feishu import QUEUED, FeishuGateway, attributed
from xiaowei.storage import hold_instance_lock

pytestmark = pytest.mark.loopback

BUSY = "系统繁忙，本轮未执行"
BUSY_REPLY = "系统繁忙，本轮未执行，请稍后用新消息重试"
DENIED = "未能确认你当前的使用权限或群成员身份"


def queue_settings(**overrides: Any) -> dict[str, Any]:
    group: dict[str, Any] = {
        "chat_id": CHAT,
        "tools": sorted(GROUP_TOOLS),
        "member_page_size": 50,
        "member_max_pages": 2,
        "member_timeout_seconds": 2,
        "max_waiting": 2,
        "max_wait_seconds": 60,
        "wait_check_seconds": 0.05,
    }
    group.update(overrides)
    return group


GROUP_TOOLS = frozenset(base_config().group.tools)  # type: ignore[union-attr]


def config(*, group: dict[str, Any] | None = None, **overrides: Any) -> FeishuConfig:
    """F3 群配置；单聊用户 ``ou_alice_dm`` 映射到有个人授权的 ``alice``。"""
    values: dict[str, Any] = {
        "users": {"ou_alice_dm": "alice"},
        "consumer_count": 2,
        "stop_timeout_seconds": 7,
        "group": queue_settings(**(group or {})),
    }
    values.update(overrides)
    return base_config(**values)


@dataclass
class Turns:
    """包住 ``Application.run_turn``：按问题挂起指定轮次，记录开始顺序与同时运行的群轮次数。"""

    holds: dict[str, asyncio.Event] = field(default_factory=dict)
    entered: dict[str, asyncio.Event] = field(default_factory=dict)
    started: list[str] = field(default_factory=list)
    group_active: int = 0
    group_peak: int = 0
    active: int = 0
    peak: int = 0

    def install(self, env: Env) -> None:
        run_turn = env.app.run_turn

        async def wrapped(context: Any, message: str) -> Any:
            question = message.split("\n", 1)[-1]
            grouped = context.identity.owner.kind == "group"
            self.started.append(question)
            self.active += 1
            self.peak = max(self.peak, self.active)
            if grouped:
                self.group_active += 1
                self.group_peak = max(self.group_peak, self.group_active)
            self.entered.setdefault(question, asyncio.Event()).set()
            try:
                hold = self.holds.get(question)
                if hold is not None:
                    await hold.wait()
                return await run_turn(context, message)
            finally:
                self.active -= 1
                if grouped:
                    self.group_active -= 1

        env.app.run_turn = wrapped  # type: ignore[method-assign]

    def hold(self, question: str) -> asyncio.Event:
        return self.holds.setdefault(question, asyncio.Event())

    async def wait_entered(self, question: str) -> None:
        async with asyncio.timeout(5):
            await self.entered.setdefault(question, asyncio.Event()).wait()


@asynccontextmanager
async def queue(
    env: Env, outbox: Outbox | None = None, **overrides: Any
) -> AsyncIterator[tuple[Group, Turns]]:
    outbox = outbox or Outbox()
    env.members.current = {A, B, C}
    turns = Turns()
    turns.install(env)
    gateway = FeishuGateway(
        env.service, config(**overrides), outbox, clock=env.clock, bot_open_id=lambda: BOT
    )
    task = asyncio.create_task(gateway.run())
    try:
        yield Group(env, gateway, outbox), turns
    finally:
        for hold in turns.holds.values():
            hold.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def replies(group: Group) -> list[tuple[str | None, str]]:
    return [
        (reply_to, text)
        for reply_to, (_, text) in zip(group.outbox.replies, group.outbox.sent, strict=True)
    ]


def notices(group: Group) -> list[str | None]:
    return [reply_to for reply_to, text in replies(group) if text == QUEUED]


async def send(group: Group, question: str, message_id: str, sender: str = A) -> dict[str, Any]:
    event = group.event(question, sender=sender, message_id=message_id)
    await group.gateway.receive(event)
    return event


ANY = "*"


@dataclass
class GatedOutbox(Outbox):
    """按（原消息编号，文本前缀）挂起发送，直到对应事件被设置；``ANY`` 匹配任意原消息。

    ``sent`` 按挂起结束的先后记录，即平台可见的顺序。另记录到达过挂起点的发送，以及同时在途的
    繁忙回执数与峰值。
    """

    gates: dict[tuple[str, str], asyncio.Event] = field(default_factory=dict)
    reached: set[tuple[str, str]] = field(default_factory=set)
    busy_active: int = 0
    busy_peak: int = 0

    def gate(self, reply_to: str, prefix: str) -> asyncio.Event:
        return self.gates.setdefault((reply_to, prefix), asyncio.Event())

    async def __call__(self, chat_id: str, text: str, *, reply_to: str | None = None) -> Any:
        key = next(
            (
                k
                for k in self.gates
                if k[0] in (ANY, reply_to) and text.startswith(k[1]) and not self.gates[k].is_set()
            ),
            None,
        )
        busy = text.startswith(BUSY)
        if busy:
            self.busy_active += 1
            self.busy_peak = max(self.busy_peak, self.busy_active)
        try:
            if key is not None:
                self.reached.add((reply_to or "", key[1]))
                await self.gates[key].wait()
            return await super().__call__(chat_id, text, reply_to=reply_to)
        finally:
            if busy:
                self.busy_active -= 1


async def settled(
    env: Env, expected: dict[str | None, str | tuple[str, str]], timeout: float = 1
) -> None:
    """在 ``timeout`` 内等到这些请求符合预期（只看给出的键）：值为 (state, delivery)，或只给
    state（投递状态正在变化时）。"""
    current: dict[str | None, tuple[str, str]] = {}
    try:
        async with asyncio.timeout(timeout):
            while True:
                current = await states(env)
                if all(
                    current.get(key) == value
                    or (isinstance(value, str) and current.get(key, ("",))[0] == value)
                    for key, value in expected.items()
                ):
                    return
                await asyncio.sleep(0.02)
    except TimeoutError:
        raise AssertionError(f"期望 {expected}，实际 {current}") from None


def texts_for(group: Group, reply_to: str) -> list[str]:
    return [t for r, t in replies(group) if r == reply_to]


async def dm(group: Group, question: str, message_id: str) -> None:
    """单聊用户 ``ou_alice_dm`` 的一条私聊（另一会话）。"""
    event = group.event(question, sender="ou_alice_dm", message_id=message_id)
    event["event"]["message"].update(
        chat_type="p2p", chat_id="oc_dm", content=json.dumps({"text": question})
    )
    await group.gateway.receive(event)


async def sdk_items(env: Env) -> int:
    rows = await env.rows("SELECT count(*) FROM agent_messages")
    return int(rows[0][0])


def member_checks(env: Env, who: str) -> int:
    return sum(actor == who for _, actor in env.members.checks)


# ---- 按接受顺序串行 ------------------------------------------------------------------------


async def test_group_turns_run_one_at_a_time_in_acceptance_order(env: Env) -> None:
    """A 运行中 B、C 先后到达：各得一次排队提示，按接受顺序运行，同群同时最多一轮；
    B 能看到 A 的历史；
    同时另一单聊会话可以运行（两个消费者）。成员目录每轮只在开始与发送前各查一次，提示不查目录。"""
    async with queue(env) as (group, turns):
        first = env.scripts.add(
            attributed(A, "东区订单？"), tool_call("order_total", region="east"), cite()
        )
        second = env.scripts.add(attributed(B, "和西区比呢？"), cite("东区较高"))
        third = env.scripts.add(attributed(C, "那北区呢？"), clarify())
        hold = turns.hold("东区订单？")
        await send(group, "东区订单？", "om_a")
        await turns.wait_entered("东区订单？")
        await send(group, "和西区比呢？", "om_b", B)
        await send(group, "那北区呢？", "om_c", C)
        assert notices(group) == ["om_b", "om_c"] and env.members.checks == [(CHAT, A)]
        # 单聊（另一会话）在群轮次挂起时照常运行。
        personal = env.scripts.add("单聊问题", clarify())
        dm = group.event("单聊问题", sender="ou_alice_dm", message_id="om_dm")
        dm["event"]["message"]["chat_type"] = "p2p"
        dm["event"]["message"]["chat_id"] = "oc_dm"
        dm["event"]["message"]["content"] = '{"text": "单聊问题"}'
        await group.gateway.receive(dm)
        await until(lambda: any(r is None for r in group.outbox.replies))
        # 发送返回后投递状态才落定为 sent：等存储结果，不以替身记下发送为准。
        await settled(env, {None: ("completed", "sent")})
        assert turns.started == ["东区订单？", "单聊问题"] and env.model_calls(personal) == 1
        assert await states(env) == {
            "om_a": ("running", "pending"),
            "om_b": ("accepted", "pending"),
            "om_c": ("accepted", "pending"),
            None: ("completed", "sent"),
        }
        hold.set()
        await until(lambda: len(group.outbox.sent) == 6)
        await group.gateway.idle()
    assert turns.started == ["东区订单？", "单聊问题", "和西区比呢？", "那北区呢？"]
    assert turns.group_peak == 1 and turns.peak == 2
    assert [r for r, t in replies(group) if t != QUEUED and r] == ["om_a", "om_b", "om_c"]
    # B 的模型输入回放了 A 的提问与本轮 A 的证据。
    (call,) = env.scripts.calls[second]
    users = [item for item in call.input if item.get("role") == "user"]
    assert len(users) == 2 and env.model_calls(first) == 2 and env.model_calls(third) == 1
    assert env.members.checks == [(CHAT, A), (CHAT, A), (CHAT, B), (CHAT, B), (CHAT, C), (CHAT, C)]


# ---- 排队提示与推进 ------------------------------------------------------------------------


async def held_notice(
    group: Group, turns: Turns, outbox: GatedOutbox, env: Env
) -> tuple[asyncio.Event, asyncio.Event, asyncio.Task[Any]]:
    """A 运行中（挂起），B 进入等待且其排队提示挂在发送中。返回 A 的放行、提示的放行与 B 的
    ``receive`` 任务。"""
    env.scripts.add(attributed(A, "一"), clarify())
    hold = turns.hold("一")
    release = outbox.gate("om_2", QUEUED)
    await send(group, "一", "om_1")
    await turns.wait_entered("一")
    receiving = asyncio.create_task(send(group, "二", "om_2", B))
    await until(lambda: ("om_2", QUEUED) in outbox.reached, timeout=5)
    return hold, release, receiving


async def test_a_waiter_does_not_start_before_its_queue_notice_attempt_ends(env: Env) -> None:
    """B 的排队提示仍在发送时 A 结束：B 不进入 Runner、保持 accepted；提示结束后才运行，
    本地发送顺序中 B 的提示先于最终回答。"""
    outbox = GatedOutbox()
    async with queue(env, outbox) as (group, turns):
        hold, release, receiving = await held_notice(group, turns, outbox, env)
        env.scripts.add(attributed(B, "二"), clarify())
        hold.set()
        await until(lambda: "om_1" in group.outbox.replies)
        await asyncio.sleep(0.3)
        assert turns.started == ["一"]
        assert (await states(env))["om_2"] == ("accepted", "pending")
        release.set()
        await receiving
        await until(lambda: len(turns.started) == 2)
        await group.gateway.idle()
    assert texts_for(group, "om_2")[0] == QUEUED and len(texts_for(group, "om_2")) == 2
    assert await states(env) == {"om_1": ("completed", "sent"), "om_2": ("completed", "sent")}


@pytest.mark.parametrize(
    "outcome",
    ["failed", "unknown", RuntimeError("notice lost")],
    ids=["failed", "unknown", "raise"],
)
async def test_a_waiter_runs_after_its_queue_notice_fails(env: Env, outcome: Any) -> None:
    """提示挂起后明确失败、结果不明或抛出：不重发提示，B 在提示尝试结束后照常运行并得到回答。"""
    outbox = GatedOutbox(outcomes=["sent", outcome])  # A 的最终回答先于挂起的提示完成
    async with queue(env, outbox) as (group, turns):
        hold, release, receiving = await held_notice(group, turns, outbox, env)
        waiting = env.scripts.add(attributed(B, "二"), clarify())
        hold.set()
        await until(lambda: "om_1" in group.outbox.replies)
        release.set()
        await receiving
        await until(lambda: len(turns.started) == 2)
        await group.gateway.idle()
    assert env.model_calls(waiting) == 1 and texts_for(group, "om_2")[0] == QUEUED
    assert await states(env) == {"om_1": ("completed", "sent"), "om_2": ("completed", "sent")}


async def test_a_wait_that_expires_during_the_queue_notice_replies_after_it(env: Env) -> None:
    """提示仍在发送时等待到期：检查间隔内结算为 failed/busy，但繁忙回执等提示尝试结束后才发，
    本地发送顺序中提示不会晚于回执；模型 0 次。"""
    outbox = GatedOutbox()
    async with queue(env, outbox) as (group, turns):
        hold, release, receiving = await held_notice(group, turns, outbox, env)
        expired = env.scripts.add(attributed(B, "二"), clarify())
        env.clock.advance(61)
        await settled(env, {"om_2": ("failed", "pending")})
        await asyncio.sleep(0.3)
        assert texts_for(group, "om_2") == []
        release.set()
        await receiving
        await until(lambda: len(texts_for(group, "om_2")) == 2)
        hold.set()
        await group.gateway.idle()
    assert texts_for(group, "om_2")[0] == QUEUED and texts_for(group, "om_2")[1].startswith(BUSY)
    assert env.model_calls(expired) == 0 and turns.started == ["一"]
    assert (await states(env))["om_2"] == ("failed", "sent")


async def test_drain_waits_for_a_queue_notice_attempt(env: Env) -> None:
    outbox = GatedOutbox()
    async with queue(env, outbox) as (group, turns):
        hold, release, receiving = await held_notice(group, turns, outbox, env)
        env.scripts.add(attributed(B, "二"), clarify())
        hold.set()
        await until(lambda: "om_1" in group.outbox.replies)
        draining = asyncio.create_task(group.gateway.drain(10))
        await asyncio.sleep(0.2)
        assert not draining.done()
        release.set()
        assert await asyncio.wait_for(draining, 10)
        await receiving
    assert await states(env) == {"om_1": ("completed", "sent"), "om_2": ("completed", "sent")}
    assert env.store.readiness.ok


# ---- 容量与期限 ----------------------------------------------------------------------------


async def test_a_full_group_queue_rejects_without_running(env: Env) -> None:
    """等待上限 1：B 等待（恰好到上限），C 超出 → failed/busy 并回复原消息，模型 0 次；
    A、B 照常完成。"""
    async with queue(env, group={"max_waiting": 1}) as (group, turns):
        env.scripts.add(attributed(A, "问一"), clarify())
        env.scripts.add(attributed(B, "问二"), clarify())
        refused = env.scripts.add(attributed(C, "问三"), clarify())
        hold = turns.hold("问一")
        await send(group, "问一", "om_1")
        await turns.wait_entered("问一")
        await send(group, "问二", "om_2", B)
        await send(group, "问三", "om_3", C)
        await until(lambda: any(t.startswith(BUSY) for _, t in replies(group)))
        await settled(env, {"om_3": ("failed", "sent")})  # 发送返回后才落定为 sent
        hold.set()
        await until(lambda: len(turns.started) == 2)
        await group.gateway.idle()
    assert env.model_calls(refused) == 0 and turns.started == ["问一", "问二"]
    assert notices(group) == ["om_2"]
    assert [r for r, t in replies(group) if t.startswith(BUSY)] == ["om_3"]
    assert await states(env) == {
        "om_1": ("completed", "sent"),
        "om_2": ("completed", "sent"),
        "om_3": ("failed", "sent"),
    }


async def test_a_waiting_turn_expires_within_the_check_interval(env: Env) -> None:
    """等待恰好到期限不结束，超过后在检查间隔内记为 failed/busy，随后回复，不等前一轮结束；
    模型 0 次。"""
    async with queue(env) as (group, turns):
        env.scripts.add(attributed(A, "长任务"), clarify())
        expired = env.scripts.add(attributed(B, "等待中"), clarify())
        hold = turns.hold("长任务")
        await send(group, "长任务", "om_long")
        await turns.wait_entered("长任务")
        await send(group, "等待中", "om_wait", B)
        env.clock.advance(60)
        await asyncio.sleep(0.3)
        assert (await states(env))["om_wait"] == ("accepted", "pending")
        env.clock.advance(1)
        await settled(env, {"om_wait": "failed"})  # 检查间隔（0.05 s）内结算
        await until(lambda: any(t.startswith(BUSY) for _, t in replies(group)), timeout=2)
        await settled(env, {"om_long": ("running", "pending"), "om_wait": ("failed", "sent")})
        hold.set()
        await group.gateway.idle()
    assert env.model_calls(expired) == 0 and turns.started == ["长任务"]


async def test_an_expired_turn_is_skipped_when_dequeued(env: Env) -> None:
    """检查间隔很长时，过期项在消费者开始前结算：下一条照常运行。重投不刷新期限。"""
    async with queue(env, group={"wait_check_seconds": 60}) as (group, turns):
        env.scripts.add(attributed(A, "长任务"), clarify())
        env.scripts.add(attributed(B, "过期"), clarify())
        env.scripts.add(attributed(C, "未过期"), clarify())
        hold = turns.hold("长任务")
        await send(group, "长任务", "om_long")
        await turns.wait_entered("长任务")
        late = await send(group, "过期", "om_late", B)
        env.clock.advance(50)
        await send(group, "未过期", "om_fresh", C)
        await group.gateway.receive(late)  # 重投：不重新排队、不再提示
        env.clock.advance(11)
        hold.set()
        await until(lambda: len(turns.started) == 2)
        await group.gateway.idle()
    assert turns.started == ["长任务", "未过期"] and notices(group) == ["om_late", "om_fresh"]
    assert await states(env) == {
        "om_long": ("completed", "sent"),
        "om_late": ("failed", "sent"),
        "om_fresh": ("completed", "sent"),
    }


@pytest.mark.parametrize("blocker", ["send_hangs", "member_hangs", "member_fails"])
async def test_expired_waiters_settle_while_one_receipt_is_blocked(env: Env, blocker: str) -> None:
    """B、C、D 同时到期，B 的回执发送挂起、成员查询挂起或抛出：三条都在检查间隔内结算为
    failed（busy），C、D 照常收到回执；B 解除阻塞后按当前成员资格交付（抛出时不发送）。
    三条的模型、Runner 与业务查询调用均为 0。"""
    outbox = GatedOutbox()
    unblock = asyncio.Event()
    if blocker == "send_hangs":
        unblock = outbox.gate("om_b", BUSY)
    elif blocker == "member_hangs":
        env.members.holds[B] = unblock
    async with queue(env, outbox, group={"max_waiting": 3}) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        expired = [env.scripts.add(attributed(who, "等待"), clarify()) for who in (B, C)]
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        for who, message_id in ((B, "om_b"), (C, "om_c"), (C, "om_d")):
            await send(group, "等待", message_id, who)
        if blocker == "member_fails":
            env.members.failing.add(B)
        env.clock.advance(61)
        # 发送挂起时 B 已取得投递权（sending）；成员查询挂起或抛出时尚未取得（pending）。
        blocked = ("failed", "sending" if blocker == "send_hangs" else "pending")
        await settled(
            env, {"om_b": blocked, "om_c": ("failed", "sent"), "om_d": ("failed", "sent")}
        )
        unblock.set()
        if blocker != "member_fails":
            await settled(env, {"om_b": ("failed", "sent")})
        hold.set()
        await group.gateway.idle()
        assert await group.gateway.drain(5)
    assert turns.started == ["一"] and env.adapter.calls == []
    assert all(env.model_calls(script) == 0 for script in expired)
    # 各条回执并发发送、各自回复原消息，不同请求之间的先后不作保证。
    assert sorted(r for r, t in replies(group) if t.startswith(BUSY)) == (
        ["om_c", "om_d"] if blocker == "member_fails" else ["om_b", "om_c", "om_d"]
    )


async def test_expiry_receipts_have_bounded_tasks_and_concurrency(env: Env) -> None:
    """6 条等待同时到期、回执全部挂起：都在检查间隔内结算，同时在途的回执最多 4 条；回执未落定前
    它们仍占群等待容量，新到的请求记为繁忙、不进入等待也不提示。"""
    outbox = GatedOutbox()
    release = outbox.gate(ANY, BUSY)
    waiting = [f"om_w{i}" for i in range(6)]
    async with queue(env, outbox, queue_size=8, group={"max_waiting": 6}) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        for message_id in waiting:
            await send(group, "等待", message_id, B)
        env.clock.advance(61)
        await settled(env, dict.fromkeys(waiting, "failed"))
        await until(lambda: outbox.busy_active == 4, timeout=5)
        await asyncio.sleep(0.3)
        assert outbox.busy_peak == 4
        late = asyncio.create_task(send(group, "新来", "om_late", C))
        await settled(env, {"om_late": "failed"})
        release.set()
        await late
        await settled(env, dict.fromkeys([*waiting, "om_late"], ("failed", "sent")))
        hold.set()
        await group.gateway.idle()
        assert await group.gateway.drain(5)
    assert turns.started == ["一"] and "om_late" not in notices(group)
    assert notices(group) == waiting


# ---- 到期项：按请求去重、统一提示门与回执上限 ------------------------------------------------


async def test_a_redelivered_expired_request_still_waits_for_its_notice(env: Env) -> None:
    """B 的排队提示仍在发送时到期并已结算，此时平台重投 B：重投不发送回执、不查成员目录；提示
    尝试结束后原到期任务只回复一次，本地发送顺序为提示在前。"""
    outbox = GatedOutbox()
    async with queue(env, outbox, group={"max_wait_seconds": 1}) as (group, turns):
        hold, release, receiving = await held_notice(group, turns, outbox, env)
        env.clock.advance(2)
        await settled(env, {"om_2": ("failed", "pending")})
        await group.gateway.receive(group.event("二", sender=B, message_id="om_2"))
        await asyncio.sleep(0.3)
        assert texts_for(group, "om_2") == [] and member_checks(env, B) == 0
        release.set()
        await receiving
        await settled(env, {"om_2": ("failed", "sent")})
        hold.set()
        await group.gateway.idle()
    assert texts_for(group, "om_2")[0] == QUEUED and len(texts_for(group, "om_2")) == 2
    assert texts_for(group, "om_2")[1].startswith(BUSY) and member_checks(env, B) == 1
    assert turns.started == ["一"] and env.adapter.calls == []


async def test_redeliveries_during_an_expiry_do_not_check_membership_or_send_again(
    env: Env,
) -> None:
    """原到期任务的成员查询挂起时 B 被重投 3 次：成员目录仍只查 1 次，回执只发 1 次。"""
    release = asyncio.Event()
    async with queue(env, group={"max_wait_seconds": 1}) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        expired = env.scripts.add(attributed(B, "二"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        event = await send(group, "二", "om_2", B)
        env.members.holds[B] = release
        env.clock.advance(2)
        await until(lambda: member_checks(env, B) == 1)
        redeliveries = [asyncio.create_task(group.gateway.receive(event)) for _ in range(3)]
        await asyncio.sleep(0.3)
        observed = member_checks(env, B)
        release.set()
        await asyncio.gather(*redeliveries)
        assert observed == 1 and await sdk_items(env) == 0
        await settled(env, {"om_2": ("failed", "sent")})
        hold.set()
        await group.gateway.idle()
    assert [t for t in texts_for(group, "om_2") if t.startswith(BUSY)] == [BUSY_REPLY]
    assert member_checks(env, B) == 1 and env.model_calls(expired) == 0
    assert env.adapter.calls == []


async def test_redeliveries_keep_the_expiry_receipt_bound(env: Env) -> None:
    """6 条到期回执中 4 条在发送、2 条等名额时，6 条全部被重投：同时在途的回执仍最多 4 条，
    重投不查成员目录；放行后每条只回复一次。"""
    outbox = GatedOutbox()
    release = outbox.gate(ANY, BUSY)
    waiting = [f"om_w{i}" for i in range(6)]
    async with queue(
        env, outbox, queue_size=8, group={"max_waiting": 6, "max_wait_seconds": 1}
    ) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        events = [await send(group, "等待", message_id, B) for message_id in waiting]
        env.clock.advance(2)
        await until(lambda: outbox.busy_active == 4)
        checks = member_checks(env, B)
        redeliveries = [asyncio.create_task(group.gateway.receive(event)) for event in events]
        await asyncio.sleep(0.3)
        observed = (outbox.busy_peak, member_checks(env, B) - checks)
        release.set()
        await asyncio.gather(*redeliveries)
        assert observed == (4, 0)
        await settled(env, dict.fromkeys(waiting, ("failed", "sent")))
        hold.set()
        await group.gateway.idle()
    assert sorted(r for r, t in replies(group) if t.startswith(BUSY)) == waiting
    assert outbox.busy_peak == 4 and member_checks(env, B) == 6


async def test_a_head_expiring_before_dequeue_shares_the_receipt_bound(env: Env) -> None:
    """唯一的消费者被单聊占用：群队头与 5 条等待（共 6 条尚未开始，恰好到等待上限）都仍在群 FIFO
    中按期限结算；6 条回执同时在途最多 4 条，单聊结束后消费者不运行任何过期项。"""
    outbox = GatedOutbox()
    release = outbox.gate(ANY, BUSY)
    waiting = ["om_head", *(f"om_w{i}" for i in range(5))]
    async with queue(
        env,
        outbox,
        consumer_count=1,
        queue_size=8,
        group={"max_wait_seconds": 1, "max_waiting": 6},
    ) as (group, turns):
        env.scripts.add("私聊长任务", clarify())
        hold = turns.hold("私聊长任务")
        await dm(group, "私聊长任务", "om_dm")
        await turns.wait_entered("私聊长任务")
        for message_id in waiting:
            await send(group, "群请求", message_id, B)
        env.clock.advance(2)
        await settled(env, dict.fromkeys(waiting, "failed"))
        await until(lambda: outbox.busy_active == 4)
        hold.set()
        await settled(env, {None: ("completed", "sent")})
        await asyncio.sleep(0.3)
        assert outbox.busy_peak == 4
        release.set()
        await settled(env, dict.fromkeys(waiting, ("failed", "sent")))
        await group.gateway.idle()
    assert turns.started == ["私聊长任务"] and env.adapter.calls == []
    assert sorted(r for r, t in replies(group) if t.startswith(BUSY)) == sorted(waiting)


async def test_an_expiry_withheld_for_membership_can_be_redelivered_once_after_recovery(
    env: Env,
) -> None:
    """到期回执发送前无法确认 B 的成员资格：记为 failed/pending、不发送。原到期任务结束后成员
    恢复，平台重投 B：按 F2 的首次发送竞争复核成员并回复一次；之后的重投不查目录、不发送。"""
    async with queue(env, group={"max_wait_seconds": 1}) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        event = await send(group, "二", "om_2", B)
        env.members.failing.add(B)
        env.clock.advance(2)
        await until(lambda: member_checks(env, B) == 1)
        assert (await states(env))["om_2"] == ("failed", "pending")
        env.members.failing.discard(B)
        # 原到期任务结束前的重投直接返回（不查目录）；结束后的一次重投复核成员并发送。
        async with asyncio.timeout(5):
            while (await states(env))["om_2"] != ("failed", "sent"):
                await group.gateway.receive(event)
                await asyncio.sleep(0.02)
        await group.gateway.receive(event)
        hold.set()
        await group.gateway.idle()
    assert texts_for(group, "om_2")[0] == QUEUED
    assert [t for t in texts_for(group, "om_2") if t.startswith(BUSY)] == [BUSY_REPLY]
    assert member_checks(env, B) == 2 and turns.started == ["一"]


@pytest.mark.parametrize("check", [60, 0.05], ids=["at_start", "by_sweep"])
async def test_a_notice_ending_as_the_wait_expires_runs_or_settles_only_once(
    env: Env, check: float
) -> None:
    """A 已结束，B 的提示挂起期间等待到期，然后提示尝试结束：提示的放行、到期检查与消费者开始
    运行之间只有一个结果。检查间隔很长时由消费者开始前的核对结算，很短时与定时检查竞争；
    两种情况 B 都不运行、只结算并回复一次。"""
    outbox = GatedOutbox()
    async with queue(env, outbox, group={"wait_check_seconds": check}) as (group, turns):
        hold, release, receiving = await held_notice(group, turns, outbox, env)
        expired = env.scripts.add(attributed(B, "二"), clarify())
        hold.set()
        await until(lambda: "om_1" in outbox.replies)
        env.clock.advance(61)
        release.set()
        await receiving
        await settled(env, {"om_2": ("failed", "sent")})
        await asyncio.sleep(0.2)
        await group.gateway.idle()
    assert turns.started == ["一"] and env.model_calls(expired) == 0
    assert texts_for(group, "om_2") == [QUEUED, BUSY_REPLY] and member_checks(env, B) == 1


async def test_a_started_waiter_is_not_settled_when_its_wait_runs_out(env: Env) -> None:
    """B 已开始运行后超过等待期限：定时检查不再管理它，不结算为繁忙、不发回执，B 正常完成。"""
    async with queue(env) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        env.scripts.add(attributed(B, "二"), clarify())
        await send(group, "一", "om_1")
        hold = turns.hold("二")
        await send(group, "二", "om_2", B)
        await turns.wait_entered("二")
        env.clock.advance(61)
        await asyncio.sleep(0.3)
        assert (await states(env))["om_2"] == ("running", "pending")
        hold.set()
        await settled(env, {"om_2": ("completed", "sent")})
        await group.gateway.idle()
    assert not any(t.startswith(BUSY) for t in texts_for(group, "om_2"))


async def test_an_expiry_found_at_start_is_registered_like_any_other(env: Env) -> None:
    """检查间隔很长，B 的到期由消费者开始前的核对发现：它同样按请求登记、经统一路径回执，成员
    查询挂起期间的重投不查目录、不发送。"""
    release = asyncio.Event()
    async with queue(env, group={"wait_check_seconds": 60}) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        event = await send(group, "二", "om_2", B)
        env.members.holds[B] = release
        env.clock.advance(61)
        hold.set()
        await until(lambda: member_checks(env, B) == 1)
        redeliveries = [asyncio.create_task(group.gateway.receive(event)) for _ in range(3)]
        await asyncio.sleep(0.3)
        observed = member_checks(env, B)
        release.set()
        await asyncio.gather(*redeliveries)
        await settled(env, {"om_2": ("failed", "sent")})
        await group.gateway.idle()
    assert observed == 1 and member_checks(env, B) == 1
    assert texts_for(group, "om_2") == [QUEUED, BUSY_REPLY] and turns.started == ["一"]


async def test_a_waiter_starts_when_its_notice_ends_without_waiting_for_a_check(env: Env) -> None:
    """检查间隔很长：A 结束时 B 的提示仍在发送，提示尝试一结束 B 就开始运行，不等定时检查。"""
    outbox = GatedOutbox()
    async with queue(env, outbox, group={"wait_check_seconds": 60}) as (group, turns):
        hold, release, receiving = await held_notice(group, turns, outbox, env)
        env.scripts.add(attributed(B, "二"), clarify())
        hold.set()
        await until(lambda: "om_1" in outbox.replies)
        release.set()
        await receiving
        await settled(env, {"om_2": ("completed", "sent")})
        await group.gateway.idle()
    assert turns.started == ["一", "二"]


async def test_the_next_waiter_runs_once_an_unready_head_expires(env: Env) -> None:
    """A 结束时队头 B 的提示仍在发送（不可运行），C 的提示已发完、期限较晚：定时检查把 B 结算为
    到期后，C 随即运行；B 的回执等它的提示尝试结束。"""
    outbox = GatedOutbox()
    async with queue(env, outbox) as (group, turns):
        hold, release, receiving = await held_notice(group, turns, outbox, env)
        expired = env.scripts.add(attributed(B, "二"), clarify())
        env.scripts.add(attributed(C, "三"), clarify())
        env.clock.advance(30)
        await send(group, "三", "om_3", C)
        hold.set()
        await until(lambda: "om_1" in outbox.replies)
        env.clock.advance(31)  # B 等了 61 s，C 等了 31 s
        await settled(env, {"om_2": ("failed", "pending"), "om_3": ("completed", "sent")})
        release.set()
        await receiving
        await settled(env, {"om_2": ("failed", "sent")})
        await group.gateway.idle()
    assert turns.started == ["一", "三"] and env.model_calls(expired) == 0
    assert texts_for(group, "om_2") == [QUEUED, BUSY_REPLY]


async def test_a_head_waiting_for_a_consumer_counts_toward_max_waiting(env: Env) -> None:
    """唯一的消费者被单聊占用：尚未开始的群队头计入等待上限，只有正在运行的一条不计。等待上限 1
    时 B 记为繁忙、不提示、模型 0 次；单聊结束后只有队头运行。"""
    async with queue(env, consumer_count=1, group={"max_waiting": 1}) as (group, turns):
        env.scripts.add("私聊长任务", clarify())
        env.scripts.add(attributed(A, "一"), clarify())
        refused = env.scripts.add(attributed(B, "二"), clarify())
        hold = turns.hold("私聊长任务")
        await dm(group, "私聊长任务", "om_dm")
        await turns.wait_entered("私聊长任务")
        await send(group, "一", "om_1")
        await send(group, "二", "om_2", B)
        assert await states(env) == {
            None: ("running", "pending"),  # 单聊不回复原消息
            "om_1": ("accepted", "pending"),
            "om_2": ("failed", "sent"),
        }
        hold.set()
        await settled(env, {"om_1": ("completed", "sent")})
        await group.gateway.idle()
    assert turns.started == ["私聊长任务", "一"] and notices(group) == []
    assert texts_for(group, "om_2") == [BUSY_REPLY] and env.model_calls(refused) == 0


async def test_unsettled_expiry_receipts_fill_the_queue_until_they_settle(env: Env) -> None:
    """等待上限 1：B 到期、回执挂起时，即使 A 已结束、群已空闲，B 仍占等待名额，C 记为繁忙且
    不运行；B 的回执落定后恢复接收，D 成为队头并运行。"""
    outbox = GatedOutbox()
    release = outbox.gate("om_2", BUSY)
    limits = {"max_waiting": 1, "max_wait_seconds": 1}
    async with queue(env, outbox, group=limits) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        expired = env.scripts.add(attributed(B, "二"), clarify())
        refused = env.scripts.add(attributed(C, "三"), clarify())
        env.scripts.add(attributed(A, "四"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        await send(group, "二", "om_2", B)
        env.clock.advance(2)
        await until(lambda: outbox.busy_active == 1)
        hold.set()
        await settled(env, {"om_1": ("completed", "sent"), "om_2": ("failed", "sending")})
        await send(group, "三", "om_3", C)
        assert (await states(env))["om_3"] == ("failed", "sent")
        release.set()
        await settled(env, {"om_2": ("failed", "sent")})
        await send(group, "四", "om_4")
        await settled(env, {"om_4": ("completed", "sent")})
        await group.gateway.idle()
    assert turns.started == ["一", "四"] and notices(group) == ["om_2"]
    assert env.model_calls(expired) == 0 and env.model_calls(refused) == 0
    assert texts_for(group, "om_3") == [BUSY_REPLY]


# ---- 尚未开始的群请求：期限管理与消费者占用 ------------------------------------------------


async def test_a_waiter_whose_notice_is_sending_still_expires_after_the_head_ends(
    env: Env,
) -> None:
    """A 结束时 B 的提示仍在发送：B 仍受期限管理，到期后在检查间隔内结算为 failed/busy，
    回执等提示尝试结束；B 不运行，模型、SDK Session 与业务 Adapter 均无调用。"""
    outbox = GatedOutbox()
    async with queue(env, outbox, group={"max_wait_seconds": 1}) as (group, turns):
        hold, release, receiving = await held_notice(group, turns, outbox, env)
        expired = env.scripts.add(attributed(B, "二"), clarify())
        hold.set()
        await until(lambda: "om_1" in outbox.replies)
        items = await sdk_items(env)  # A 的一轮
        env.clock.advance(2)
        await settled(env, {"om_2": ("failed", "pending")})
        assert texts_for(group, "om_2") == []
        release.set()
        await receiving
        await settled(env, {"om_2": ("failed", "sent")})
        await group.gateway.idle()
    assert texts_for(group, "om_2") == [QUEUED, BUSY_REPLY]
    assert turns.started == ["一"] and env.model_calls(expired) == 0
    assert env.adapter.calls == [] and await sdk_items(env) == items


async def test_a_notice_wait_does_not_occupy_the_only_consumer(env: Env) -> None:
    """``consumer_count=1``：A 结束后 B 的提示仍在发送，此时到达的单聊照常运行；提示尝试结束后
    B 才运行。"""
    outbox = GatedOutbox()
    async with queue(env, outbox, consumer_count=1) as (group, turns):
        hold, release, receiving = await held_notice(group, turns, outbox, env)
        env.scripts.add(attributed(B, "二"), clarify())
        env.scripts.add("私聊", clarify())
        hold.set()
        await until(lambda: "om_1" in outbox.replies)
        await dm(group, "私聊", "om_dm")
        await settled(env, {None: ("completed", "sent")})
        assert turns.started == ["一", "私聊"]
        release.set()
        await receiving
        await settled(env, {"om_2": ("completed", "sent")})
        await group.gateway.idle()
    assert turns.started == ["一", "私聊", "二"] and texts_for(group, "om_2")[0] == QUEUED


async def test_a_group_head_waiting_for_a_consumer_expires_and_never_runs(env: Env) -> None:
    """唯一的消费者被单聊占用：群队头等不到消费者，到期后在检查间隔内结算并回复一次；之后新到的
    群请求成为队头，单聊结束后只运行它，过期的队头既不运行也不再回复。"""
    async with queue(env, consumer_count=1, group={"max_wait_seconds": 1}) as (group, turns):
        env.scripts.add("私聊长任务", clarify())
        expired = env.scripts.add(attributed(A, "群请求"), clarify())
        env.scripts.add(attributed(B, "下一条"), clarify())
        hold = turns.hold("私聊长任务")
        await dm(group, "私聊长任务", "om_dm")
        await turns.wait_entered("私聊长任务")
        await send(group, "群请求", "om_group")
        env.clock.advance(2)
        await settled(env, {"om_group": ("failed", "sent")})
        await send(group, "下一条", "om_next", B)
        hold.set()
        await settled(env, {"om_next": ("completed", "sent")})
        await asyncio.sleep(0.2)
        await group.gateway.idle()
    assert turns.started == ["私聊长任务", "下一条"] and env.model_calls(expired) == 0
    assert texts_for(group, "om_group") == [BUSY_REPLY] and notices(group) == []


# ---- 队头结束与失败 ------------------------------------------------------------------------


async def test_a_turn_error_head_releases_the_group(env: Env) -> None:
    async with queue(env) as (group, _):
        env.scripts.add(attributed(B, "后来"), clarify())
        run_turn = env.app.run_turn

        async def failing(context: Any, message: str) -> Any:
            if message.endswith("先来"):
                await asyncio.sleep(0.1)
                raise TurnError("model_failed")
            return await run_turn(context, message)

        env.app.run_turn = failing  # type: ignore[method-assign]
        await send(group, "先来", "om_1")
        await send(group, "后来", "om_2", B)
        await until(lambda: len(group.outbox.sent) == 3)
        await group.gateway.idle()
    assert notices(group) == ["om_2"]
    assert await states(env) == {"om_1": ("failed", "sent"), "om_2": ("completed", "sent")}


async def test_clarification_releases_the_head_for_the_next_member(env: Env) -> None:
    async with queue(env) as (group, turns):
        env.scripts.add(attributed(A, "看看订单"), clarify())
        env.scripts.add(
            attributed(B, "东区订单？"), tool_call("order_total", region="east"), cite()
        )
        hold = turns.hold("看看订单")
        await send(group, "看看订单", "om_1")
        await turns.wait_entered("看看订单")
        await send(group, "东区订单？", "om_2", B)
        hold.set()
        await until(lambda: len(turns.started) == 2)
        await group.gateway.idle()
    assert await states(env) == {"om_1": ("completed", "sent"), "om_2": ("completed", "sent")}
    assert "请说明要看的地区" in replies(group)[1][1] or "请说明要看的地区" in replies(group)[0][1]


async def test_a_member_who_left_while_waiting_is_not_run(env: Env) -> None:
    """排队中离群：出队重验失败，模型 0 次，固定回执发送前同样无法确认 → 不发送；下一条继续。"""
    async with queue(env) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        left = env.scripts.add(attributed(B, "二"), clarify())
        env.scripts.add(attributed(C, "三"), clarify())
        env.members.current = {A, B, C}
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        await send(group, "二", "om_2", B)
        await send(group, "三", "om_3", C)
        env.members.current.discard(B)
        hold.set()
        await until(lambda: len(turns.started) == 2)
        await group.gateway.idle()
    assert env.model_calls(left) == 0 and turns.started == ["一", "三"]
    assert (await states(env))["om_2"] == ("failed", "pending")
    assert all(r != "om_2" or t == QUEUED for r, t in replies(group))


async def test_a_failed_queue_notice_does_not_cancel_the_turn(env: Env) -> None:
    outbox = Outbox(outcomes=[RuntimeError("notice lost")])
    async with queue(env, outbox) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        env.scripts.add(attributed(B, "二"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        await send(group, "二", "om_2", B)
        hold.set()
        await until(lambda: len(turns.started) == 2)
        await group.gateway.idle()
    # 提示（第 1 次发送）抛出异常：不重试，也不影响 B 的运行与结果交付。
    assert replies(group)[0] == ("om_2", QUEUED) and len(group.outbox.sent) == 3
    assert await states(env) == {"om_1": ("completed", "sent"), "om_2": ("completed", "sent")}


async def test_a_redelivered_waiting_event_is_not_queued_or_noticed_twice(env: Env) -> None:
    async with queue(env) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        waiting = env.scripts.add(attributed(B, "二"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        event = await send(group, "二", "om_2", B)
        await asyncio.gather(*(group.gateway.receive(event) for _ in range(3)))
        hold.set()
        await until(lambda: len(turns.started) == 2)
        await group.gateway.idle()
    assert notices(group) == ["om_2"] and env.model_calls(waiting) == 1
    assert len(group.outbox.sent) == 3 and len(env.members.checks) == 4


async def test_a_storage_failure_stops_the_queue_without_running_waiters(env: Env) -> None:
    """排队期间 readiness 锁低（存储或实例所有权故障）：队头的会话保持封闭，等待项出队后记为
    failed/busy、不调用模型；发送因 readiness 锁低不取得投递权。不能带着不明状态继续接活。"""
    async with queue(env) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        waiting = env.scripts.add(attributed(B, "二"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        await send(group, "二", "om_2", B)
        env.store.readiness.lock("synthetic_storage_failure")
        hold.set()
        await group.gateway.idle()
    assert env.model_calls(waiting) == 0 and turns.started == ["一"]
    assert (await states(env))["om_2"] == ("failed", "pending")
    assert notices(group) == ["om_2"] and not env.store.readiness.ok


# ---- /新建 -------------------------------------------------------------------------------


async def test_new_session_is_refused_while_running_or_waiting_and_rotates_when_idle(
    env: Env,
) -> None:
    async with queue(env) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        env.scripts.add(attributed(B, "二"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        await send(group, "二", "om_2", B)
        await group.gateway.receive(group.event("/新建", sender=C, message_id="om_new1"))
        hold.set()
        await until(lambda: len(turns.started) == 2)
        await group.gateway.idle()
        await group.gateway.receive(group.event("/新建", sender=C, message_id="om_new2"))
    texts = [t for r, t in replies(group) if r in ("om_new1", "om_new2")]
    assert len(texts) == 2 and texts[0] != texts[1] and "本群" in texts[1]
    sessions = await env.rows("SELECT DISTINCT session_id FROM xiaowei_request")
    assert len(sessions) == 1  # 排队的 B 仍在原会话
    current = await env.rows(
        "SELECT generation FROM xiaowei_channel_session WHERE state = 'current'"
        " AND owner_kind = 'group'"
    )
    assert current == [(2,)]


# ---- 配置 --------------------------------------------------------------------------------


def test_group_queue_settings_are_bounded_and_require_a_safe_stop_timeout() -> None:
    config()  # 7 s 及以上通过
    for bad in (
        {"max_waiting": 0},
        {"max_waiting": 5},  # 超过 queue_size（4）
        {"max_wait_seconds": 0},
        {"wait_check_seconds": 0},
        {"wait_check_seconds": 61},
        {"max_wait_seconds": 1, "wait_check_seconds": 2},
    ):
        with pytest.raises(ValidationError):
            config(group=bad)
    with pytest.raises(ValidationError):
        config(stop_timeout_seconds=6.9)
    # 单聊配置的现有取值不变。
    base_config(group=None, stop_timeout_seconds=5)


def test_group_wait_must_end_within_the_request_retention(runtime_env: RuntimeEnv) -> None:
    runtime_env.config(feishu=runtime_group(max_wait_seconds=3599))
    with pytest.raises(ValidationError):
        runtime_env.config(feishu=runtime_group(max_wait_seconds=3600))


# ---- 停机与恢复 --------------------------------------------------------------------------


async def test_drain_waits_for_the_group_queue(env: Env) -> None:
    async with queue(env) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        env.scripts.add(attributed(B, "二"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        await send(group, "二", "om_2", B)
        draining = asyncio.create_task(group.gateway.drain(10))
        await asyncio.sleep(0.2)
        assert not draining.done()
        hold.set()
        assert await asyncio.wait_for(draining, 10)
    assert await states(env) == {"om_1": ("completed", "sent"), "om_2": ("completed", "sent")}
    assert env.store.readiness.ok


async def test_drain_waits_for_every_expiry_receipt_in_flight(env: Env) -> None:
    """队列与群 FIFO 都已空，但两条到期回执仍在发送：drain 等它们全部落定。"""
    outbox = GatedOutbox()
    release = outbox.gate(ANY, BUSY)
    async with queue(env, outbox) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        await send(group, "二", "om_2", B)
        await send(group, "三", "om_3", C)
        env.clock.advance(61)
        await until(lambda: outbox.busy_active == 2, timeout=5)
        hold.set()
        await group.gateway.idle()  # 队头已完成，队列与群 FIFO 为空
        draining = asyncio.create_task(group.gateway.drain(10))
        await asyncio.sleep(0.2)
        assert not draining.done()
        release.set()
        assert await asyncio.wait_for(draining, 10)
    assert await states(env) == {
        "om_1": ("completed", "sent"),
        "om_2": ("failed", "sent"),
        "om_3": ("failed", "sent"),
    }


async def test_drain_timeout_with_a_waiting_group_turn_blocks_readiness(env: Env) -> None:
    async with queue(env) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        env.scripts.add(attributed(B, "二"), clarify())
        turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        await send(group, "二", "om_2", B)
        assert not await group.gateway.drain(0.2)
        assert not env.store.readiness.ok


async def test_a_consumer_stops_when_a_lower_layer_converts_its_cancellation(env: Env) -> None:
    """停机取消到达时队头正在运行，而下层把取消转换成了普通失败（本条按失败处理并回执）：消费者
    不能吞掉取消、继续等下一项；``run`` 在期限内结束并锁低 readiness。"""
    entered = asyncio.Event()

    async def converting(context: Any, message: str) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise TurnError("model_failed") from None

    env.members.current = {A, B, C}
    env.app.run_turn = converting  # type: ignore[method-assign]
    outbox = Outbox()
    gateway = FeishuGateway(env.service, config(), outbox, clock=env.clock, bot_open_id=lambda: BOT)
    task = asyncio.create_task(gateway.run())
    await send(Group(env, gateway, outbox), "一", "om_1")
    async with asyncio.timeout(5):
        await entered.wait()
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=5)
    if not done:  # 吞掉了取消：再取消一次（此时停在取下一项上）以便清理，然后判失败
        task.cancel()
        await asyncio.wait({task}, timeout=5)
    assert done and task.cancelled(), "run 在取消后没有结束"
    assert (await states(env))["om_1"] == ("failed", "sent")
    assert not env.store.readiness.ok


async def test_a_converted_cancellation_hands_over_nothing_during_shutdown(env: Env) -> None:
    """停机取消被下层转换成普通失败时，下一等待项已经到期：消费者不交接、不创建到期任务（任务组
    正在关闭），``run`` 以取消结束并锁低 readiness；B 留给重启恢复。"""
    entered = asyncio.Event()

    async def converting(context: Any, message: str) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise TurnError("model_failed") from None

    env.members.current = {A, B, C}
    env.app.run_turn = converting  # type: ignore[method-assign]
    outbox = Outbox()
    gateway = FeishuGateway(
        env.service,
        config(group={"wait_check_seconds": 60}),
        outbox,
        clock=env.clock,
        bot_open_id=lambda: BOT,
    )
    task = asyncio.create_task(gateway.run())
    group = Group(env, gateway, outbox)
    await send(group, "运行中", "om_1")
    async with asyncio.timeout(5):
        await entered.wait()
    await send(group, "排队中", "om_2", B)
    env.clock.advance(61)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=5)
    assert done and task.cancelled(), repr(task.exception() if done else "run 没有结束")
    assert (await states(env))["om_2"] == ("accepted", "pending")
    assert not env.store.readiness.ok


async def session_state(env: Env, session_id: str) -> str | None:
    rows = await env.rows("SELECT state FROM xiaowei_session WHERE session_id = :s", s=session_id)
    return rows[0][0] if rows else None


async def recover(env: Env) -> Any:
    async with hold_instance_lock(env.engine, env.store.readiness) as lock:
        return await env.store.recover(lock)


async def test_recovery_keeps_a_group_session_open_when_only_waiting_turns_were_interrupted(
    env: Env,
) -> None:
    """真实 PostgreSQL：A 已完成，B 只在排队（accepted）→ 恢复后 B interrupted，
    原会话仍可回放续问。"""
    done = await env.run(env.group(env.scripts.add("A：一", clarify()), "om_a"))
    waiting = await env.service.accept(env.group("B：二", "om_b", B))
    session = done.session_id
    assert waiting.record.session_id == session and await session_state(env, session) == "active"
    async with hold_instance_lock(env.engine, env.store.readiness) as lock:
        report = await env.store.recover(lock)
        assert report.interrupted == 1
        assert await session_state(env, session) == "active"
        follow = env.scripts.add("A：三", clarify())
        later = await env.run(env.group(follow, "om_c"))
    assert later.session_id == session and later.state == "completed"
    assert len(env.scripts.calls[follow][0].input) > 1  # 回放了 A 的一轮


@pytest.mark.parametrize("prior", ["running", "writing", "personal"])
async def test_recovery_still_closes_sessions_that_may_have_been_written(
    env: Env, prior: str
) -> None:
    """同会话有 running（另有 accepted）、会话处于 writing，或个人会话只有 accepted：
    照原规则关闭。"""
    if prior == "personal":
        done = await env.run(
            env.personal(env.scripts.add("个人一", clarify()), "om_p1", "alice", "feishu", "oc_dm")
        )
        await env.service.accept(env.personal("个人二", "om_p2", "alice", "feishu", "oc_dm"))
    else:
        done = await env.run(env.group(env.scripts.add("A：一", clarify()), "om_a"))
        await env.service.accept(env.group("B：二", "om_b", B))
        if prior == "running":
            started = await env.service.accept(env.group("C：三", "om_c", C))
            async with env.engine.begin() as conn:
                await conn.execute(
                    text("UPDATE xiaowei_request SET state = 'running' WHERE turn_id = :t"),
                    {"t": started.record.turn_id},
                )
        else:
            async with env.engine.begin() as conn:
                await conn.execute(
                    text("UPDATE xiaowei_session SET state = 'writing' WHERE session_id = :s"),
                    {"s": done.session_id},
                )
    await recover(env)
    assert await session_state(env, done.session_id) == "closed"


# ---- 正式入口 ----------------------------------------------------------------------------


@dataclass
class SlowFirstCheck(GroupChannel):
    """第一次成员查询延迟：A 的开始前复核尚未返回时，B、C 已被接受。"""

    first_delay: float = 0.5

    async def get_chat_members(self, chat_id: str, **kwargs: Any) -> Any:
        if not self.member_calls:
            self.member_delay = self.first_delay
        else:
            self.member_delay = 0.0
        return await super().get_chat_members(chat_id, **kwargs)


async def test_formal_runtime_queues_a_second_member_and_refuses_beyond_the_limit(
    runtime_env: RuntimeEnv,
) -> None:
    """``runtime.serve``：A 先被接受，B 排队（一次提示，按序得到回答）；
    等待上限 1 时 C 记为繁忙。"""
    env = runtime_env
    config = env.config(feishu=runtime_group(max_waiting=1))
    channel = SlowFirstCheck(members=[A, B, C])
    env.scripts.add(attributed(A, "一"), clarify())
    env.scripts.add(attributed(B, "二"), clarify())
    refused = env.scripts.add(attributed(C, "三"), clarify())
    async with env.running(config, feishu_channel=channel) as served:
        assert (await served.ready())["feishu"] == "connected"
        channel.emit_raw_from_sdk_thread(raw_group_event(env, "一", "om_1"))
        await until(lambda: len(channel.member_calls) == 1)
        channel.emit_raw_from_sdk_thread(raw_group_event(env, "二", "om_2", sender=B))
        channel.emit_raw_from_sdk_thread(raw_group_event(env, "三", "om_3", sender=C))
        await until(lambda: len(channel.sends) == 4)
        assert await served.finish() == 0
    sent = [(opts["reply_to"], message["text"]) for _, message, opts in channel.sends]
    assert sent[0] == ("om_2", QUEUED)
    assert [r for r, t in sent if t != QUEUED] == ["om_3", "om_1", "om_2"]
    assert BUSY in dict(sent[1:])["om_3"]
    assert env.scripts.calls.get(refused) is None
    # A、B 各两次（开始与发送前）；C 的繁忙回执发送前一次；提示不查目录。
    assert len(channel.member_calls) == 5
