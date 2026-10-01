"""P1-B Task 8：正式运行装配 ``xiaowei.runtime``。

真实 PostgreSQL（隔离数据库）+ 正式装配顺序（实例锁、启动恢复、StarRocks 工具、唯一授权来源、
Evidence、Application、ChannelService）+ 真实 Uvicorn 监听 loopback。只替换最底层 I/O：模型是
HTTP mock 脚本（``model_transport``），StarRocks 是驱动替身（``starrocks_connect``），飞书是
SDK 公开面替身（``feishu_channel``）。

替身不能证明：真实模型、真实 StarRocks、真实飞书长连接与发送（Task 9，需授权）；CLI 参数、环境
变量强制与信号处理由 ``test_cli.py`` 的子进程用例覆盖。
"""

import asyncio
import json
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from lark_channel.channel.errors import FeishuChannelErrorCode, SendError
from lark_channel.channel.types import SendResult
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import URL
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.p1b.test_starrocks_adapter import Result, driver
from tests.sdk_core.synthetic_tools import Clock
from tests.sdk_core.test_app import Scripts, after, cite, tool_call, upstream_error
from tests.sdk_core.test_feishu import FakeChannel
from tests.sdk_core.test_model_api import OPENAI as PROFILE

from xiaowei import runtime
from xiaowei.channel import AccessDeniedError
from xiaowei.channel_store import ChannelStore, RequestUnavailableError
from xiaowei.config import SecretRefError
from xiaowei.models import AUDIENCES, Identity
from xiaowei.starrocks import StarRocksAdapter
from xiaowei.starrocks_tools import QUERY_TOOLS, starrocks_tools
from xiaowei.storage import (
    Readiness,
    StorageBusyError,
    StorageNotInitializedError,
    hold_instance_lock,
    initialize_storage,
    open_engine,
)

pytestmark = pytest.mark.loopback

DB_ENV, KEY_ENV, FEISHU_ENV = "XW_TEST_DATABASE_URL", "XW_TEST_DIGEST_KEY", "XW_TEST_FEISHU_SECRET"
MODEL_ENV = PROFILE.api_key_ref.removeprefix("env:")
SR_ENV = SR.password_ref.removeprefix("env:")
OPERATOR = "operator"
SALES = Result(("region", "total"), [("east", 100), ("west", 50)])
APP_ID, TENANT = "cli_test", "tenant-1"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def serve_config(port: int, **overrides: Any) -> dict[str, Any]:
    """与正式 JSON 配置同构的字典；凭据只以 env: 引用出现。"""
    values: dict[str, Any] = {
        "storage": {
            "database_url_ref": f"env:{DB_ENV}",
            "digest_key_ref": f"env:{KEY_ENV}",
            "evidence_retention_seconds": 3600,
            "request_retention_seconds": 3600,
            "max_answer_bytes": 20_000,
        },
        "model": PROFILE.model_dump(mode="json"),
        "data_policy": {"input": {"max_bytes": 2000}, "model_tools": sorted(QUERY_TOOLS)},
        "session_limits": {
            "max_history_turns": 5,
            "max_history_bytes": 200_000,
            "retention_seconds": 7200,
        },
        "budget": {"max_turns": 6, "max_tool_calls": 3, "timeout_seconds": 30},
        "max_concurrent_turns": 4,
        "starrocks": SR.model_dump(mode="json"),
        "projection_bytes": dict.fromkeys(AUDIENCES, 60_000),
        "access": {
            "policy_version": "p1",
            "grants": {OPERATOR: sorted(QUERY_TOOLS), "alice": sorted(QUERY_TOOLS)},
        },
        "web": {
            "operator_id": OPERATOR,
            "allowed_origins": [f"http://127.0.0.1:{port}"],
            "secure_cookie": False,
            "max_body_bytes": 65_536,
        },
        "listen_port": port,
        "shutdown_timeout_seconds": 5,
        "lock_check_seconds": 0.2,
    }
    values.update(overrides)
    return values


def feishu_config(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "app_id": APP_ID,
        "app_secret_ref": f"env:{FEISHU_ENV}",
        "tenant_key": TENANT,
        "users": {"ou_alice": "alice"},
        "max_event_age_seconds": 300,
        "max_message_chars": 1000,
        "max_reply_chars": 2000,
        "queue_size": 4,
        "consumer_count": 1,
        "send_timeout_seconds": 5,
        "connect_timeout_seconds": 5,
        "stop_timeout_seconds": 5,
    }
    values.update(overrides)
    return values


@dataclass
class Env:
    url: URL
    port: int
    scripts: Scripts = field(default_factory=Scripts)
    clock: Clock = field(default_factory=Clock)
    drv: Any = field(default_factory=lambda: driver(SALES))

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def config(self, **overrides: Any) -> runtime.ServeConfig:
        return runtime.ServeConfig.model_validate(serve_config(self.port, **overrides))

    async def serve(self, config: runtime.ServeConfig, stop: asyncio.Event, **kw: Any) -> int:
        return await runtime.serve(
            config,
            stop=stop,
            clock=self.clock,
            model_transport=self.scripts.transport(),
            starrocks_connect=self.drv,
            **kw,
        )

    @asynccontextmanager
    async def running(self, config: runtime.ServeConfig, **kw: Any) -> AsyncIterator["Served"]:
        stop = asyncio.Event()
        task = asyncio.create_task(self.serve(config, stop, **kw))
        async with httpx.AsyncClient(base_url=self.origin, timeout=30) as client:
            served = Served(client, task, stop)
            await served.ready()
            try:
                yield served
            finally:
                stop.set()
                await asyncio.wait({task}, timeout=30)

    async def scalar(self, sql: str) -> Any:
        async with open_engine(SecretStr(self.url.render_as_string(hide_password=False))) as e:
            async with e.connect() as conn:
                return await conn.scalar(text(sql))


@dataclass
class Served:
    client: httpx.AsyncClient
    task: asyncio.Task[int]
    stop: asyncio.Event

    async def ready(self) -> dict[str, Any]:
        async with asyncio.timeout(20):
            while True:
                if self.task.done():
                    pytest.fail(f"serve 提前结束：{self.task.result()}")
                try:
                    response = await self.client.get("/readyz")
                except httpx.TransportError:
                    await asyncio.sleep(0.05)
                    continue
                body: dict[str, Any] = response.json()
                return body

    async def page(self) -> None:
        assert (await self.client.get("/")).status_code == 200

    async def turn(self, message: str, mode: str = "diagnose", request_id: str = "r1") -> Any:
        response = await self.client.post(
            "/api/turns",
            json={"request_id": request_id, "mode": mode, "message": message},
            headers={"origin": str(self.client.base_url).rstrip("/")},
        )
        return response

    async def finish(self) -> int:
        self.stop.set()
        return await asyncio.wait_for(self.task, 30)


@pytest.fixture
async def env(postgres_url: URL, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    monkeypatch.setenv(DB_ENV, postgres_url.render_as_string(hide_password=False))
    monkeypatch.setenv(KEY_ENV, "runtime-test-digest-key")
    monkeypatch.setenv(MODEL_ENV, "sk-test-runtime-model")
    monkeypatch.setenv(FEISHU_ENV, "feishu-test-secret")
    async with open_engine(SecretStr(postgres_url.render_as_string(hide_password=False))) as e:
        await initialize_storage(e)
    yield Env(postgres_url, free_port())


async def lock_is_free(env: Env) -> bool:
    secret = SecretStr(env.url.render_as_string(hide_password=False))
    async with open_engine(secret) as engine:
        try:
            async with hold_instance_lock(engine, Readiness()):
                return True
        except StorageBusyError:
            return False


@contextmanager
def port_is_released(port: int) -> Iterator[None]:
    """结束后没有进程在监听该端口（连接被拒绝）；TIME_WAIT 不影响这一判断。"""
    yield
    with socket.socket() as probe, pytest.raises(ConnectionRefusedError):
        probe.connect(("127.0.0.1", port))


# ---- 成功路径 ------------------------------------------------------------------------


async def test_formal_assembly_answers_web_turns_and_stops_cleanly(env: Env) -> None:
    config = env.config()
    with port_is_released(env.port):
        async with env.running(config) as served:
            assert await served.ready() == {"status": "ready", "feishu": "disabled"}
            await served.page()
            diagnose = env.scripts.add("诊断表结构", tool_call("list_tables"), cite())
            response = await served.turn(diagnose)
            assert response.status_code == 200 and response.json()["state"] == "completed"
            assert all(
                "run_readonly_query" not in seen for seen in env.scripts.tools_seen(diagnose)
            )

            query = env.scripts.add(
                "各地区销售额",
                tool_call("run_readonly_query", sql="SELECT region, total FROM sales"),
                cite(),
            )
            body = (await served.turn(query, "query", "r2")).json()
            assert body["state"] == "completed"
            facts = body["delivery"]["facts"]
            (fact,) = [f for f in facts if f["tool_id"] == "local/run_readonly_query"]
            assert fact["rows"] == [
                {"region": "east", "total": 100},
                {"region": "west", "total": 50},
            ]
            assert not await lock_is_free(env)  # 运行中持有实例锁
            assert await served.finish() == 0
    assert await lock_is_free(env)
    assert await env.scalar("SELECT count(*) FROM xiaowei_request WHERE state = 'completed'") == 2


async def test_model_failure_is_a_fixed_receipt_through_the_formal_assembly(env: Env) -> None:
    async with env.running(env.config()) as served:
        await served.page()
        response = await served.turn(env.scripts.add("上游报错", upstream_error))
        body = response.json()
        assert body["state"] == "failed" and "模型未能完成本轮" in body["delivery"]["content"]
        assert await served.finish() == 0


async def test_startup_recovers_interrupted_requests_before_serving(env: Env) -> None:
    secret = SecretStr(env.url.render_as_string(hide_password=False))
    async with open_engine(secret) as engine:
        store = ChannelStore(
            engine,
            key=SecretStr("runtime-test-digest-key"),
            clock=env.clock,
            readiness=Readiness(),
            request_retention_seconds=3600,
            session_retention_seconds=7200,
            evidence_retention_seconds=3600,
            max_answer_bytes=20_000,
        )
        acceptance = await store.accept(
            channel="web",
            subject_id=OPERATOR,
            conversation="c" * 43,
            request_id="crashed",
            mode="diagnose",
            message="上次进程崩溃时的请求",
            policy_version="p1",
        )
        await store.start(acceptance.record)
    async with env.running(env.config()) as served:
        await served.ready()
        state = await env.scalar("SELECT state FROM xiaowei_request")
        assert state == "interrupted"
        assert await served.finish() == 0
    assert env.scripts.calls == {}  # 恢复不重跑


# ---- 启动失败：不对外服务，逆序释放 ----------------------------------------------------


async def test_second_instance_is_refused_and_releases_its_port(env: Env) -> None:
    secret = SecretStr(env.url.render_as_string(hide_password=False))
    async with open_engine(secret) as engine, hold_instance_lock(engine, Readiness()):
        with port_is_released(env.port), pytest.raises(StorageBusyError):
            await env.serve(env.config(), asyncio.Event())


async def test_uninitialized_database_is_refused(
    env: Env, postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = SecretStr(env.url.render_as_string(hide_password=False))
    async with open_engine(secret) as engine, engine.begin() as conn:
        await conn.execute(text("DROP TABLE xiaowei_request"))
    with pytest.raises(StorageNotInitializedError):
        await env.serve(env.config(), asyncio.Event())
    assert await lock_is_free(env)


async def test_partial_startup_failure_releases_the_lock(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(MODEL_ENV)  # 模型凭据在锁与恢复之后才解析
    with port_is_released(env.port), pytest.raises(SecretRefError):
        await env.serve(env.config(), asyncio.Event())
    assert await lock_is_free(env)


async def test_occupied_port_fails_before_touching_the_database(env: Env) -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", env.port))
        holder.listen()
        with pytest.raises(runtime.ListenError):
            await env.serve(env.config(), asyncio.Event())
    assert await lock_is_free(env)


# ---- 运行中失败 ----------------------------------------------------------------------


# 只终止持有会话级实例锁（排他）的后端；接收事务的共享屏障锁不受影响。
TERMINATE_LOCK = (
    "SELECT count(pg_terminate_backend(pid)) FROM pg_locks"
    " WHERE locktype = 'advisory' AND granted AND mode = 'ExclusiveLock'"
)
WAITING_ON_LOCK = "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"


async def waiting_on_locks(env: Env, count: int, timeout: float = 10) -> bool:
    """在期限内至少有 ``count`` 个后端在数据库锁上等待。"""
    for _ in range(int(timeout / 0.02)):
        if await env.scalar(WAITING_ON_LOCK) >= count:
            return True
        await asyncio.sleep(0.02)
    return False


async def not_serving(served: Served, timeout: float = 2) -> None:
    """``/readyz`` 在期限内不再返回 200（503 或监听已关闭都算）。"""
    async with asyncio.timeout(timeout):
        while True:
            try:
                if (await served.client.get("/readyz")).status_code != 200:
                    return
            except httpx.TransportError:
                return
            await asyncio.sleep(0.02)


async def test_losing_the_instance_lock_stops_the_process(env: Env) -> None:
    async with env.running(env.config()) as served:
        assert await env.scalar(TERMINATE_LOCK) == 1
        assert await asyncio.wait_for(served.task, 30) == 1


async def test_normal_stop_during_a_lock_check_is_not_a_lock_loss(env: Env) -> None:
    """核对查询频繁在途时正常停止：不能因中途取消核对而让持锁连接失效、被判为丢锁。"""
    config = env.config(lock_check_seconds=0.001)
    for _ in range(5):
        async with env.running(config) as served:
            await asyncio.sleep(0.05)
            assert await served.finish() == 0


async def test_lock_loss_is_noticed_at_once_not_at_the_next_check(env: Env) -> None:
    """周期核对远在测试期限之外：只能靠连接终止通知立即锁低 readiness。"""
    config = env.config(lock_check_seconds=60)
    async with env.running(config) as served:
        await served.page()
        assert await env.scalar(TERMINATE_LOCK) == 1
        await not_serving(served)
        late = env.scripts.add("丢锁后的新请求", tool_call("list_tables"), cite())
        try:
            response = await served.turn(late, request_id="late")
        except httpx.TransportError:
            pass  # 监听已关闭
        else:
            assert response.status_code == 503
        assert await asyncio.wait_for(served.task, 30) == 1
    assert late not in env.scripts.calls  # 模型与工具都没有被调用
    assert env.drv.attempts == 0


async def test_second_instance_after_lock_loss_never_runs_the_old_request(env: Env) -> None:
    """旧实例丢锁 → 第二实例取得锁并恢复 → 旧实例在途轮次的结果不能覆盖恢复结论。"""
    gate, entered = asyncio.Event(), asyncio.Event()
    slow = env.scripts.add(
        "丢锁时在途", after(gate, tool_call("list_tables"), entered=entered), cite()
    )
    old = env.config(lock_check_seconds=60, shutdown_timeout_seconds=20)
    async with env.running(old) as served:
        await served.page()
        pending = asyncio.create_task(served.turn(slow, request_id="inflight"))
        await asyncio.wait_for(entered.wait(), 20)
        assert await env.scalar(TERMINATE_LOCK) == 1
        await not_serving(served)

        second = Env(env.url, free_port(), env.scripts, env.clock, env.drv)
        async with second.running(second.config()) as fresh:
            assert await fresh.ready() == {"status": "ready", "feishu": "disabled"}
            # 第二实例的启动恢复已把旧实例的在途请求标为 interrupted，且没有重跑它。
            assert await env.scalar("SELECT state FROM xiaowei_request") == "interrupted"
            gate.set()
            response = await asyncio.wait_for(pending, 30)
            assert response.status_code != 200 or response.json()["state"] != "completed"
            assert await asyncio.wait_for(served.task, 30) == 1
            assert await env.scalar("SELECT state FROM xiaowei_request") == "interrupted"
            assert len(env.scripts.calls[slow]) <= 2  # 只来自旧实例的那一次运行
            assert await fresh.finish() == 0


async def test_accept_paused_across_a_takeover_ends_interrupted_not_stranded(env: Env) -> None:
    """旧实例的接收事务未提交时失去锁、新实例接管：恢复等它提交后再读取，请求不停在 accepted；
    重复请求在新实例得到已有终态，不运行模型或查询。"""
    first = env.scripts.add("先建立会话", tool_call("list_tables"), cite())
    late = env.scripts.add("接管时尚未提交", tool_call("list_tables"), cite())
    old = env.config(lock_check_seconds=60)
    async with env.running(old) as served:
        await served.page()
        assert (await served.turn(first, request_id="first")).json()["state"] == "completed"
        attempts = env.drv.attempts
        secret = SecretStr(env.url.render_as_string(hide_password=False))
        async with open_engine(secret) as admin:
            # 暂停点：行锁挡住接收事务读取 current 映射，此时它已开始、未提交。
            blocker = await admin.connect()
            await blocker.begin()
            await blocker.execute(text("SELECT 1 FROM xiaowei_channel_session FOR UPDATE"))
            pending = asyncio.create_task(served.turn(late, request_id="late"))
            assert await waiting_on_locks(env, 1)
            assert await env.scalar(TERMINATE_LOCK) == 1

            second = Env(env.url, free_port(), env.scripts, env.clock, env.drv)
            stop = asyncio.Event()
            task = asyncio.create_task(second.serve(second.config(), stop))
            # 新实例的恢复应在接收屏障上等待；没有屏障时它直接完成，这里只是等到期限。
            await waiting_on_locks(env, 2, timeout=2)
            await blocker.rollback()
            await blocker.close()
        response = await asyncio.wait_for(pending, 30)
        assert response.status_code != 200 or response.json()["state"] != "completed"
        assert await asyncio.wait_for(served.task, 30) == 1

        async with httpx.AsyncClient(
            base_url=second.origin, timeout=30, cookies=served.client.cookies
        ) as client:
            fresh = Served(client, task, stop)
            await fresh.ready()
            states = await env.scalar("SELECT array_agg(state ORDER BY state) FROM xiaowei_request")
            assert states == ["completed", "interrupted"]
            again = await fresh.turn(late, request_id="late")
            assert again.status_code == 200 and again.json()["state"] == "interrupted"
            assert await fresh.finish() == 0
    assert late not in env.scripts.calls  # 模型与查询都没有为它运行
    assert env.drv.attempts == attempts


# ---- 飞书 ----------------------------------------------------------------------------


def feishu_event(env: Env, message: str, message_id: str) -> dict[str, Any]:
    millis = str(int(env.clock().timestamp() * 1000))
    return {
        "schema": "2.0",
        "header": {
            "event_id": f"ev_{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": millis,
            "tenant_key": TENANT,
            "app_id": APP_ID,
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_alice"},
                "sender_type": "user",
                "tenant_key": TENANT,
            },
            "message": {
                "message_id": message_id,
                "create_time": millis,
                "chat_id": "oc_alice",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": message}, ensure_ascii=False),
            },
        },
    }


async def until(predicate: Any, timeout: float = 20) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@dataclass
class OrderedChannel(FakeChannel):
    """记录发送与关闭的先后：停止时 drain 必须在关闭长连接之前完成投递。"""

    log: list[str] = field(default_factory=list)

    async def send(self, to: str, message: Any, opts: Any = None) -> Any:
        result = await super().send(to, message, opts)
        self.log.append("send")
        return result

    def stop(self, *, join_timeout: float) -> None:
        self.log.append("stop")
        super().stop(join_timeout=join_timeout)


async def test_feishu_turn_is_drained_before_the_long_connection_closes(env: Env) -> None:
    config = env.config(feishu=feishu_config())
    channel = OrderedChannel()
    gate, entered = asyncio.Event(), asyncio.Event()
    message = env.scripts.add(
        "飞书慢轮", after(gate, tool_call("list_tables"), entered=entered), cite()
    )
    async with env.running(config, feishu_channel=channel) as served:
        assert (await served.ready())["feishu"] == "connected"
        channel.emit_raw_from_sdk_thread(feishu_event(env, message, "om_1"))
        await asyncio.wait_for(entered.wait(), 20)
        served.stop.set()
        await asyncio.sleep(0.3)
        assert not served.task.done()  # 在途飞书轮次未结束前不关闭
        gate.set()
        assert await asyncio.wait_for(served.task, 30) == 0
    assert channel.log == ["send", "stop"]
    (sent,) = channel.sends
    assert sent[0] == "oc_alice" and sent[2] == {"receive_id_type": "chat_id"}


async def test_feishu_unavailable_at_startup_leaves_web_serving(env: Env) -> None:
    channel = FakeChannel(start_error=TimeoutError())
    async with env.running(env.config(feishu=feishu_config()), feishu_channel=channel) as served:
        assert await served.ready() == {"status": "ready", "feishu": "unavailable"}
        await served.page()
        assert (await served.turn(env.scripts.add("飞书断开时的网页", cite()))).status_code == 200
        assert await served.finish() == 0


# ---- 显式重发 ------------------------------------------------------------------------


async def failed_feishu_result(env: Env, config: runtime.ServeConfig) -> str:
    """经正式装配跑一条飞书轮次，发送明确失败（failed）；返回消息正文。"""
    refused = SendResult.fail(
        SendError(code=FeishuChannelErrorCode.PERMISSION_DENIED, retryable=False)
    )
    channel = FakeChannel(result=refused)
    message = env.scripts.add("飞书待重发", tool_call("list_tables"), cite())
    async with env.running(config, feishu_channel=channel) as served:
        channel.emit_raw_from_sdk_thread(feishu_event(env, message, "om_resend"))
        await until(lambda: len(channel.sends) == 1)
        assert await served.finish() == 0
    assert await env.scalar("SELECT delivery FROM xiaowei_request") == "failed"
    return message


async def test_resend_sends_a_failed_result_once_without_rerunning(env: Env) -> None:
    config = env.config(feishu=feishu_config())
    message = await failed_feishu_result(env, config)
    sends: list[Any] = []

    async def resend(**changes: str) -> Any:
        # 每条重发命令都是独立进程：新建发送通道，结束时关闭。
        channel = FakeChannel()
        target = {"subject_id": "alice", "chat_id": "oc_alice", "message_id": "om_resend"}
        try:
            return await runtime.resend(
                config, clock=env.clock, feishu_channel=channel, **{**target, **changes}
            )
        finally:
            sends.extend(channel.sends)
            assert channel.stops == 1

    with pytest.raises(RequestUnavailableError):
        await resend(chat_id="oc_other")  # 目的地参与会话摘要：不能改投别处
    with pytest.raises(RequestUnavailableError):
        await resend(message_id="om_unknown")
    with pytest.raises(AccessDeniedError):
        await resend(subject_id="mallory")
    assert sends == []
    assert await resend() == "sent"
    assert await resend() is None  # 已 sent：不再发送
    assert len(sends) == 1 and sends[0][0] == "oc_alice"
    assert len(env.scripts.calls[message]) == 2  # 只有原轮次的两次模型调用
    assert len(env.drv.connections) == 1  # 原轮次的一次元数据查询；重发不连接 StarRocks


@dataclass
class GatedChannel(FakeChannel):
    """发送进入后停住，直到测试放行；在 SDK 循环上等待，放行用线程安全事件。"""

    entered: threading.Event = field(default_factory=threading.Event)
    gate: threading.Event = field(default_factory=threading.Event)

    async def send(self, to: str, message: Any, opts: Any = None) -> Any:
        self.sends.append((to, message, opts))
        self.entered.set()
        while not self.gate.is_set():
            await asyncio.sleep(0.01)
        return self.result


async def test_serve_recovery_leaves_a_running_resend_to_finish(env: Env) -> None:
    """独立的 ``requests resend`` 正在发送时启动 serve：启动恢复不把它当作遗留 sending，
    重发照常落定为 sent；它进行中时其他重发取不到投递权。"""
    config = env.config(feishu=feishu_config())
    await failed_feishu_result(env, config)
    target = {"subject_id": "alice", "chat_id": "oc_alice", "message_id": "om_resend"}
    channel = GatedChannel()
    running = asyncio.create_task(
        runtime.resend(config, clock=env.clock, feishu_channel=channel, **target)
    )
    await until(channel.entered.is_set)
    async with env.running(config, feishu_channel=FakeChannel()) as served:
        await served.ready()
        assert await env.scalar("SELECT delivery FROM xiaowei_request") == "sending"
        other = FakeChannel()
        assert await runtime.resend(config, clock=env.clock, feishu_channel=other, **target) is None
        assert other.sends == []
        channel.gate.set()
        assert await asyncio.wait_for(running, 30) == "sent"
        assert await env.scalar("SELECT delivery FROM xiaowei_request") == "sent"
        assert await served.finish() == 0
    assert len(channel.sends) == 1


async def test_resend_refuses_after_the_evidence_expired(env: Env) -> None:
    config = env.config(feishu=feishu_config())
    await failed_feishu_result(env, config)
    env.clock.advance(3601)
    channel = FakeChannel()
    with pytest.raises(RequestUnavailableError):
        await runtime.resend(
            config,
            subject_id="alice",
            chat_id="oc_alice",
            message_id="om_resend",
            clock=env.clock,
            feishu_channel=channel,
        )
    assert channel.sends == []


async def test_resend_requires_feishu(env: Env) -> None:
    with pytest.raises(runtime.ResendNotConfiguredError):
        await runtime.resend(env.config(), subject_id="alice", chat_id="oc", message_id="om")


# ---- 维护命令 ------------------------------------------------------------------------


async def test_storage_commands_use_the_configured_database(env: Env) -> None:
    config = env.config()
    await runtime.initialize(config)  # 已初始化时幂等
    assert await runtime.upgrade(config) == 3
    report = await runtime.cleanup(config, batch_size=10)
    assert (report.sessions, report.unregistered) == (0, 0)


# ---- 配置 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "path"),
    [
        ({"listen_host": "0.0.0.0"}, "listen_host"),  # noqa: S104 - 断言被拒绝
        ({"projection_bytes": {"model": 1}}, "projection_bytes"),
        ({"access": {"policy_version": "p1", "grants": {"a": ["local/drop_table"]}}}, "<root>"),
        ({"web": {**serve_config(1)["web"], "allowed_origins": ["http://127.0.0.1:9"]}}, "<root>"),
        # 正式监听只有 HTTP：同一地址的 HTTPS Origin 不能代替它（浏览器实际发送 http://）。
        (
            {"web": {**serve_config(1)["web"], "allowed_origins": ["https://127.0.0.1:8501"]}},
            "<root>",
        ),
        ({"storage": {**serve_config(1)["storage"], "request_retention_seconds": 7200}}, "<root>"),
        ({"feishu": feishu_config(users={"ou_op": OPERATOR})}, "<root>"),
        ({"storage": {**serve_config(1)["storage"], "digest_key_ref": "plain-key"}}, "storage"),
        ({"unexpected": 1}, "unexpected"),
    ],
    ids=[
        "public listen",
        "missing audiences",
        "unknown tool",
        "listen not allowed",
        "https listen origin",
        "retention",
        "shared subject",
        "secret not ref",
        "extra key",
    ],
)
def test_invalid_configuration_is_reported_without_values(
    tmp_path: Path, changes: dict[str, Any], path: str
) -> None:
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(serve_config(8501, **changes)), encoding="utf-8")
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.load_config(file)
    assert path in str(raised.value)
    assert "plain-key" not in str(raised.value)


def test_valid_configuration_round_trips_through_json(tmp_path: Path) -> None:
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(serve_config(8501, feishu=feishu_config())), encoding="utf-8")
    config = runtime.load_config(file)
    assert config.listen_port == 8501 and config.feishu is not None
    assert config.starrocks == SR


@pytest.mark.parametrize(
    ("host", "origins"),
    [
        ("127.0.0.1", ["http://127.0.0.1:8501"]),
        ("127.0.0.1", ["http://127.0.0.1:8501", "https://127.0.0.1:8443"]),
        ("::1", ["http://[::1]:8501"]),
    ],
)
def test_listen_origin_must_be_the_actual_http_address(
    tmp_path: Path, host: str, origins: list[str]
) -> None:
    web = {**serve_config(1)["web"], "allowed_origins": origins}
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(serve_config(8501, listen_host=host, web=web)), encoding="utf-8")
    assert runtime.load_config(file).web.allowed_origins == frozenset(origins)
    only_https = {**web, "allowed_origins": [o.replace("http://", "https://") for o in origins]}
    file.write_text(json.dumps(serve_config(8501, listen_host=host, web=only_https)), "utf-8")
    with pytest.raises(runtime.ConfigError, match="http://"):
        runtime.load_config(file)


def test_unreadable_configuration(tmp_path: Path) -> None:
    with pytest.raises(runtime.ConfigError, match="不可读"):
        runtime.load_config(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text('{"storage": "secret-looking-value', encoding="utf-8")
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.load_config(broken)
    assert "secret-looking-value" not in str(raised.value)


async def test_resend_refusal_with_the_real_sdk_makes_no_network_call(env: Env) -> None:
    """真实 ``FeishuChannel``（未启动长连接）：记录不存在时在任何发送前拒绝；pytest-socket 只放行
    127.0.0.1，构造与关闭 SDK 通道若访问飞书会直接报错。"""
    config = env.config(feishu=feishu_config())
    with pytest.raises(RequestUnavailableError):
        await runtime.resend(
            config, subject_id="alice", chat_id="oc_alice", message_id="om_none", clock=env.clock
        )


def test_example_configuration_is_valid_and_fits_the_projections() -> None:
    """README 引用的示例配置：通过校验，且声明的结果上限装得进四种投影（启动时同样检查）。"""
    config = runtime.load_config(
        Path(__file__).resolve().parents[2] / "examples/xiaowei.example.json"
    )
    adapter = StarRocksAdapter(config.starrocks, connect=driver(SALES), clock=Clock())
    starrocks_tools(adapter, config.projection_bytes)
    assert config.listen_port == 8501 and config.feishu is None


async def test_static_access_is_the_single_source_for_entry_and_evidence() -> None:
    access = runtime.StaticAccess(
        runtime.AccessConfig(
            policy_version="p1", grants={"alice": frozenset({"local/list_tables"})}
        ),
        "sr-test",
    )
    decision = await access.resolve("feishu", "alice")
    assert decision is not None
    assert (decision.target_id, decision.authorized_tools) == ("sr-test", {"local/list_tables"})
    assert await access.resolve("web", "mallory") is None

    def who(subject: str) -> Identity:
        return Identity(subject_id=subject, session_id="s", turn_id="t", channel="web")

    assert await access.authorize(who("alice"), "sr-test", "local/list_tables")
    assert not await access.authorize(who("alice"), "other-target", "local/list_tables")
    assert not await access.authorize(who("alice"), "sr-test", "local/run_readonly_query")
    assert not await access.authorize(who("mallory"), "sr-test", "local/list_tables")
