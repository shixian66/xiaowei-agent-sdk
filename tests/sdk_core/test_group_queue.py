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
        assert (await states(env))["om_3"] == ("failed", "sent")
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
    """等待恰好到期限不结束，超过后在检查间隔内记为 failed/busy 并回复，不等前一轮结束；
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
        await until(lambda: any(t.startswith(BUSY) for _, t in replies(group)), timeout=2)
        assert (await states(env)) == {
            "om_long": ("running", "pending"),
            "om_wait": ("failed", "sent"),
        }
        hold.set()
        await group.gateway.idle()
    assert env.model_calls(expired) == 0 and turns.started == ["长任务"]


async def test_an_expired_turn_is_skipped_when_dequeued(env: Env) -> None:
    """检查间隔很长时，过期项在出队时结束：下一条照常运行。重投不刷新期限。"""
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


@dataclass
class HeldBusyOutbox(Outbox):
    """繁忙回执在 ``release`` 之前挂起；其他发送照常。"""

    holding: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self, chat_id: str, text: str, *, reply_to: str | None = None) -> Any:
        if text.startswith(BUSY):
            self.holding.set()
            await self.release.wait()
        return await super().__call__(chat_id, text, reply_to=reply_to)


async def test_drain_waits_for_an_expiry_receipt_in_flight(env: Env) -> None:
    """队列与群 FIFO 都已空，但到期检查发出的繁忙回执仍在发送：drain 等它落定。"""
    outbox = HeldBusyOutbox()
    async with queue(env, outbox) as (group, turns):
        env.scripts.add(attributed(A, "一"), clarify())
        env.scripts.add(attributed(B, "二"), clarify())
        hold = turns.hold("一")
        await send(group, "一", "om_1")
        await turns.wait_entered("一")
        await send(group, "二", "om_2", B)
        env.clock.advance(61)
        async with asyncio.timeout(5):
            await outbox.holding.wait()
        hold.set()
        await group.gateway.idle()  # 队头已完成，队列为空
        draining = asyncio.create_task(group.gateway.drain(10))
        await asyncio.sleep(0.2)
        assert not draining.done()
        outbox.release.set()
        assert await asyncio.wait_for(draining, 10)
    assert await states(env) == {"om_1": ("completed", "sent"), "om_2": ("failed", "sent")}


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
