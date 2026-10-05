"""飞书单群 F4：正式入口上的多人任务链（离线退出证据）。

``runtime.serve`` + 隔离 PostgreSQL + SDK 公开面替身（原始群事件、成员查询、原消息回复）+ 两个集群
的驱动替身 + 脚本模型。逐步记录模型调用、到达驱动的业务 SQL / EXPLAIN、成员查询与发送次数。

替身只证明调用链、门控与计数；不证明真实飞书的事件与发送、真实模型的指代理解，也不证明真实
StarRocks 的权限与资源行为（这些在 P3 获准环境验收）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.p1b.test_starrocks_adapter import Result, driver
from tests.sdk_core.test_app import cite, clarify, tool_call
from tests.sdk_core.test_group_gateway import GroupChannel, raw_group_event, runtime_group
from tests.sdk_core.test_group_gateway import runtime_env as runtime_env  # pytest fixture
from tests.sdk_core.test_group_identity import APP, CHAT, TENANT, A, B, C
from tests.sdk_core.test_runtime import (
    PLAN,
    cluster_config,
    feishu_config,
    feishu_event,
    statements,
    until,
)
from tests.sdk_core.test_runtime import Env as RuntimeEnv

from xiaowei import runtime
from xiaowei.feishu import QUEUED, attributed
from xiaowei.starrocks import EXPLAIN_PREFIX
from xiaowei.starrocks_tools import RUN_QUERY

pytestmark = pytest.mark.loopback

SALES_SQL = "SELECT region, total FROM sales"
QUERY_NAME = RUN_QUERY.split("/", 1)[1]
BUSY = "系统繁忙，本轮未执行"
# 两个集群同名库表、不同数据：回答中的数值能区分来源集群。
CLUSTERS = {"sr-a": 1, "sr-b": 2}
SEND_RETURNS_LATER = 0.3
LONG_HISTORY = {"max_history_turns": 10, "max_history_bytes": 200_000, "retention_seconds": 7200}


def clusters() -> dict[str, Any]:
    return {t: driver(Result(("region", "total"), [("east", n)])) for t, n in CLUSTERS.items()}


def with_plan(drv: Any, sql: str) -> None:
    """此后每条连接都能回答 ``EXPLAIN <sql>``（``driver`` 的 ``results`` 只给第一条连接）。"""
    drv.make = lambda: driver(
        Result(("region", "total"), [("east", 0)]), results={EXPLAIN_PREFIX + sql: PLAN}
    ).make()


def replies(channel: GroupChannel) -> list[tuple[str, str]]:
    """（回复的原消息, 文本）；每条都发往指定群。"""
    assert all(to == CHAT for to, _, _ in channel.sends)
    return [(opts["reply_to"], message["text"]) for _, message, opts in channel.sends]


def user_turns(env: RuntimeEnv, message: str, call: int = 0) -> list[str]:
    """模型第 ``call`` 次调用看到的各轮用户输入（回放的历史 + 本轮）。"""
    items = env.scripts.calls[message][call].input
    return [_text(item["content"]) for item in items if item.get("role") == "user"]


def _text(content: Any) -> str:
    return content if isinstance(content, str) else "".join(p["text"] for p in content)


async def rows(env: RuntimeEnv) -> dict[str, tuple[str, str, str | None]]:
    """原消息 → (state, delivery, failure_code)。"""
    async with asyncio.timeout(5):
        body = await env.scalar(
            "SELECT string_agg(reply_message_id || '=' || state || '/' || delivery || '/'"
            " || coalesce(failure_code, '-'), ',' ORDER BY created_at) FROM xiaowei_request"
            " WHERE reply_message_id IS NOT NULL"
        )
    result: dict[str, tuple[str, str, str | None]] = {}
    for item in (body or "").split(","):
        if item:
            key, value = item.split("=")
            state, delivery, code = value.split("/")
            result[key] = (state, delivery, None if code == "-" else code)
    return result


DONE = ("completed", "sent", None)


async def settled(
    env: RuntimeEnv, message_id: str, expected: tuple[str, str, str | None] = DONE
) -> None:
    """等到该原消息对应的请求在存储中落定为 ``expected``（替身记下发送不等于请求处理完成）。"""
    current = None
    try:
        async with asyncio.timeout(20):
            while (current := (await rows(env)).get(message_id)) != expected:
                await asyncio.sleep(0.02)
    except TimeoutError:
        raise AssertionError(f"{message_id}: 期望 {expected}，实际 {current}") from None


def reply_to(channel: GroupChannel, message_id: str) -> str:
    """该原消息收到的唯一回复。"""
    (text,) = [text for to, text in replies(channel) if to == message_id]
    return text


async def test_members_share_one_task_across_clusters_diagnosis_clarification_and_restart(
    runtime_env: RuntimeEnv,
) -> None:
    """A 查 sr-a → B 追问并查 sr-b（看到 A 的一轮）→ A 用 ``/诊断`` 只取 B 那条 SQL 的计划 →
    A 的模糊提问得到澄清、B 回答后查 sr-a → 重启后 C 续问，回放全部历史、不再执行任何业务 SQL。
    每一步只有本步的集群收到语句；每轮成员目录恰好查两次（开始与发送前）。"""
    env = runtime_env
    drivers = clusters()
    config = env.config(
        targets=[cluster_config(t) for t in drivers],
        feishu=runtime_group(),
        session_limits=LONG_HISTORY,
    )
    # 发送在替身记下之后再过一段时间才返回：请求此时仍是“发送中”，下一轮不能据此开始。
    channel = GroupChannel(members=[A, B, C], delay=SEND_RETURNS_LATER)

    async def ask(text: str, message_id: str, sender: str, sends: int) -> None:
        """等到这一轮落定为 ``completed/sent`` 且原消息收到回复，下一轮才发出。"""
        channel.emit_raw_from_sdk_thread(raw_group_event(env, text, message_id, sender=sender))
        await settled(env, message_id)
        assert len(channel.sends) == sends and reply_to(channel, message_id) != QUEUED

    first = env.scripts.add(
        attributed(A, "sr-a 东区销售额"),
        tool_call(QUERY_NAME, cluster="sr-a", sql=SALES_SQL),
        cite(),
    )
    follow = env.scripts.add(
        attributed(B, "那 sr-b 呢"),
        tool_call(QUERY_NAME, cluster="sr-b", sql=SALES_SQL),
        cite(),
    )
    why = env.scripts.add(
        attributed(A, "刚才 sr-b 那条为什么慢"),
        tool_call("explain_query", cluster="sr-b", sql="__RAN_B__"),
        cite(),
    )
    vague = env.scripts.add(attributed(A, "查一下订单"), clarify("要看哪个集群、哪个口径？"))
    answer = env.scripts.add(
        attributed(B, "sr-a，按地区汇总"),
        tool_call(QUERY_NAME, cluster="sr-a", sql=SALES_SQL),
        cite(),
    )
    async with env.running(config, starrocks_connect=drivers, feishu_channel=channel) as served:
        assert (await served.ready())["feishu"] == "connected"

        # 1. A 查 sr-a：模型 2 次（工具 + 回答），sr-a 一条业务 SQL，sr-b 无 I/O。
        await ask("sr-a 东区销售额", "om_1", A, sends=1)
        (ran_a,) = statements(drivers["sr-a"])
        assert len(env.scripts.calls[first]) == 2 and drivers["sr-b"].attempts == 0
        assert replies(channel)[-1][0] == "om_1" and "目标 sr-a" in replies(channel)[-1][1]
        assert len(channel.member_calls) == 2

        # 2. B 追问并查 sr-b：模型输入回放 A 的一轮；sr-a 只有回放证据时的零行权限探测。
        await ask("那 sr-b 呢", "om_2", B, sends=2)
        assert user_turns(env, follow) == [first, follow]
        (ran_b,) = statements(drivers["sr-b"])
        assert statements(drivers["sr-a"]) == [ran_a]
        assert replies(channel)[-1][0] == "om_2" and "目标 sr-b" in replies(channel)[-1][1]
        assert len(channel.member_calls) == 4

        # 3. A /诊断：查询工具不可见，只取 B 那条实际 SQL 的计划，不再执行业务 SQL。
        env.scripts.by_message[why][0] = tool_call("explain_query", cluster="sr-b", sql=ran_b)
        with_plan(drivers["sr-b"], ran_b)
        await ask("/诊断 刚才 sr-b 那条为什么慢", "om_3", A, sends=3)
        assert all(QUERY_NAME not in seen for seen in env.scripts.tools_seen(why))
        assert statements(drivers["sr-b"]) == [ran_b, EXPLAIN_PREFIX + ran_b]
        assert statements(drivers["sr-a"]) == [ran_a]
        assert replies(channel)[-1][0] == "om_3"

        # 4. A 的模糊提问得到澄清（无工具、无 I/O）；B 的回答是新请求，看到 A 的澄清后查 sr-a。
        await ask("查一下订单", "om_4", A, sends=4)
        assert len(env.scripts.calls[vague]) == 1
        assert all(QUERY_NAME in seen for seen in env.scripts.tools_seen(vague))  # 查询工具照常可见
        assert "要看哪个集群" in replies(channel)[-1][1]
        assert statements(drivers["sr-a"]) == [ran_a]
        await ask("sr-a，按地区汇总", "om_5", B, sends=5)
        assert user_turns(env, answer)[-2:] == [vague, answer]
        assert statements(drivers["sr-a"]) == [ran_a, ran_a]
        assert len(channel.member_calls) == 10
        assert all(text != QUEUED for _, text in replies(channel))  # 每轮都在上一轮落定后开始
        assert await served.finish() == 0

    assert await rows(env) == {f"om_{i}": ("completed", "sent", None) for i in range(1, 6)}
    assert [to for to, _ in replies(channel)] == [f"om_{i}" for i in range(1, 6)]

    # 5. 重启：同一测试进程内新建 runtime 实例与长连接替身。C 续问回放全部五轮（各带作者标识），
    # 只引用历史，不执行任何业务 SQL，也不重跑之前的轮次。
    # 启动时的结构快照刷新不计入 ``statements``。
    resumed = env.scripts.add(attributed(C, "汇总刚才两个集群的结果"), cite("sr-a 较低"))
    restarted = GroupChannel(members=[A, B, C])
    calls_before = {m: len(c) for m, c in env.scripts.calls.items()}
    async with env.running(config, starrocks_connect=drivers, feishu_channel=restarted) as served:
        restarted.emit_raw_from_sdk_thread(
            raw_group_event(env, "汇总刚才两个集群的结果", "om_6", sender=C)
        )
        await settled(env, "om_6")
        assert await served.finish() == 0
    assert user_turns(env, resumed) == [first, follow, why, vague, answer, resumed]
    assert len(env.scripts.calls[resumed]) == 1
    assert {m: len(env.scripts.calls[m]) for m in calls_before} == calls_before
    assert {t: statements(d) for t, d in drivers.items()} == {t: [] for t in drivers}
    # 唯一回复：脚本模型的分析，以及由代码从回放证据生成的两个集群的事实。
    ((to, text),) = replies(restarted)
    assert to == "om_6"
    assert "sr-a 较低" in text and "目标 sr-a" in text and "目标 sr-b" in text
    assert len(restarted.member_calls) == 2
    assert (await rows(env))["om_6"] == DONE


class SlowFirstCheck(GroupChannel):
    """第一次成员查询（A 的开始前复核）延迟返回：期间 B、C、D 已被接受。"""

    async def get_chat_members(self, chat_id: str, **kwargs: Any) -> Any:
        self.member_delay = 0.5 if not self.member_calls else 0.0
        return await super().get_chat_members(chat_id, **kwargs)


async def test_simultaneous_questions_overload_redelivery_and_revocation(
    runtime_env: RuntimeEnv,
) -> None:
    """等待上限 1：A 运行中 B 排队（一次提示），C 记为繁忙；B 的重投不再提示、不再排队；B 在排队
    期间离群 → 开始前拒绝，模型与 SQL 为 0，拒绝回执因无法确认成员而不发送。同时 Web 会话照常
    完成；A 完成后重投 A 的事件不重跑、不重发。"""
    env = runtime_env
    drivers = clusters()
    config = env.config(
        targets=[cluster_config(t) for t in drivers], feishu=runtime_group(max_waiting=1)
    )
    channel = SlowFirstCheck(members=[A, B, C])
    asked = env.scripts.add(
        attributed(A, "sr-a 东区销售额"),
        tool_call(QUERY_NAME, cluster="sr-a", sql=SALES_SQL),
        cite(),
    )
    queued = env.scripts.add(
        attributed(B, "sr-b 东区销售额"),
        tool_call(QUERY_NAME, cluster="sr-b", sql=SALES_SQL),
        cite(),
    )
    refused = env.scripts.add(attributed(C, "我也问一个"), clarify())
    web = env.scripts.add(
        "Web 上看 sr-b", tool_call(QUERY_NAME, cluster="sr-b", sql=SALES_SQL), cite()
    )
    async with env.running(config, starrocks_connect=drivers, feishu_channel=channel) as served:
        assert (await served.ready())["feishu"] == "connected"
        await served.page()  # Web 会话 cookie
        first = raw_group_event(env, "sr-a 东区销售额", "om_a", sender=A)
        second = raw_group_event(env, "sr-b 东区销售额", "om_b", sender=B)
        channel.emit_raw_from_sdk_thread(first)
        await until(lambda: len(channel.member_calls) == 1)  # A 的开始前复核进行中
        channel.emit_raw_from_sdk_thread(second)
        channel.emit_raw_from_sdk_thread(raw_group_event(env, "我也问一个", "om_c", sender=C))
        channel.emit_raw_from_sdk_thread(second)  # 重投
        await until(lambda: any(BUSY in text for _, text in replies(channel)))
        channel.members = [A, C]  # B 在排队期间离群

        # 不同会话（Web）不受群队列影响，照常查询并完成。
        body = (await served.turn(web, "query", "w1")).json()
        assert body["state"] == "completed"

        await until(lambda: any(to == "om_a" and text != QUEUED for to, text in replies(channel)))
        await until(lambda: len(channel.member_calls) == 5)
        await asyncio.sleep(0.3)
        channel.emit_raw_from_sdk_thread(first)  # A 完成后的重投
        await asyncio.sleep(0.3)
        assert await served.finish() == 0

    sent = replies(channel)
    assert sent[0] == ("om_b", QUEUED)
    assert [to for to, _ in sent] == ["om_b", "om_c", "om_a"]
    assert BUSY in sent[1][1] and "目标 sr-a" in sent[2][1]
    assert await rows(env) == {
        "om_a": ("completed", "sent", None),
        "om_b": ("failed", "pending", "access_denied"),
        "om_c": ("failed", "sent", "busy"),
    }
    assert len(env.scripts.calls[asked]) == 2
    assert queued not in env.scripts.calls and refused not in env.scripts.calls
    assert len(env.scripts.calls[web]) == 2
    # sr-a：A 的一条；sr-b：只有 Web 的一条，B 的查询从未发出。
    assert len(statements(drivers["sr-a"])) == 1 and len(statements(drivers["sr-b"])) == 1
    # 成员目录：A 开始与发送前、C 的繁忙回执发送前、B 开始前与拒绝回执发送前各一次；提示与重投不查。
    assert len(channel.member_calls) == 5


async def requests(env: RuntimeEnv) -> list[tuple[str, str, str | None, str, str]]:
    """全部请求行（含单聊，按接受顺序）：(owner_kind, owner_id, 原消息, state, delivery)。"""
    async with asyncio.timeout(5):
        body = await env.scalar(
            "SELECT string_agg(owner_kind || '|' || owner_id || '|' || coalesce(reply_message_id,"
            " '-') || '|' || state || '|' || delivery, ';' ORDER BY created_at)"
            " FROM xiaowei_request"
        )
    result = []
    for item in (body or "").split(";"):
        if item:
            kind, owner, message, state, delivery = item.split("|")
            result.append((kind, owner, None if message == "-" else message, state, delivery))
    return result


async def test_turning_the_group_off_and_on_keeps_its_history_and_personal_chat(
    runtime_env: RuntimeEnv, caplog: pytest.LogCaptureFixture
) -> None:
    """群配置移除后重启：群消息一律不处理（无请求、无成员查询、无模型、无回复），单聊照常；群会话与
    记录原样保留，群结果不能经个人路径重发。重新配置后同群续问回放原来的一轮。"""
    env = runtime_env
    group_on = env.config(feishu=runtime_group())
    group_off = env.config(feishu=feishu_config(app_id=APP, tenant_key=TENANT))
    first = env.scripts.add(
        attributed(A, "东区销售额"),
        tool_call(QUERY_NAME, cluster=SR.target_id, sql=SALES_SQL),
        cite(),
    )
    channel = GroupChannel(members=[A, B])
    async with env.running(group_on, feishu_channel=channel) as served:
        channel.emit_raw_from_sdk_thread(raw_group_event(env, "东区销售额", "om_1"))
        await settled(env, "om_1")
        assert await served.finish() == 0
    group_row = ("group", (await requests(env))[0][1], "om_1", "completed", "sent")
    assert await requests(env) == [group_row]

    # 关闭群配置后由 A 发群消息：A 有单聊准入，若群事件误走单聊入口会被接受，这里必须被丢弃。
    personal = env.scripts.add("单聊问题", clarify())
    off = GroupChannel(members=[A, B])
    caplog.set_level(logging.INFO, logger="xiaowei.feishu")
    async with env.running(group_off, feishu_channel=off) as served:
        off.emit_raw_from_sdk_thread(raw_group_event(env, "和西区比呢", "om_2", sender=A))
        await until(lambda: "飞书事件已丢弃：chat_type" in caplog.text)  # 群事件处理完毕
        assert await requests(env) == [group_row]
        assert off.sends == [] and off.member_calls == []
        assert set(env.scripts.calls) == {first}

        # 同一进程的单聊照常回答。
        dm = feishu_event(env, "单聊问题", "om_dm")
        dm["header"]["app_id"] = APP
        dm["header"]["tenant_key"] = dm["event"]["sender"]["tenant_key"] = TENANT
        off.emit_raw_from_sdk_thread(dm)
        async with asyncio.timeout(20):
            while (await requests(env))[-1][3:] != ("completed", "sent"):
                await asyncio.sleep(0.02)
        assert await served.finish() == 0
    assert [to for to, _, _ in off.sends] == ["oc_alice"] and off.member_calls == []
    assert set(env.scripts.calls) == {first, personal} and len(env.scripts.calls[personal]) == 1
    # A 的单聊准入映射到个人用户 alice（``feishu_config`` 的默认用户表）。
    assert await requests(env) == [group_row, ("personal", "alice", None, "completed", "sent")]
    sessions = "SELECT string_agg(owner_kind || '/' || state, ',') FROM xiaowei_session"
    assert await env.scalar(sessions + " WHERE owner_kind = 'group'") == "group/active"
    with pytest.raises(runtime.ResendTargetError):
        await runtime.resend(group_off, subject_id=A, message_id="om_1", group=True)

    resumed = env.scripts.add(attributed(B, "和西区比呢？"), cite("东区较高"))
    again = GroupChannel(members=[A, B])
    async with env.running(group_on, feishu_channel=again) as served:
        again.emit_raw_from_sdk_thread(raw_group_event(env, "和西区比呢？", "om_3", sender=B))
        await settled(env, "om_3")
        assert await served.finish() == 0
    assert user_turns(env, resumed) == [first, resumed]
    assert len(env.scripts.calls[first]) == 2 and statements(env.drv) == []
    assert "东区较高" in reply_to(again, "om_3") and len(again.sends) == 1
