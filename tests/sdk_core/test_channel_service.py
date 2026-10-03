"""P1-B Task 5：共享 ChannelService。真 Runner + ``open_model`` 装配的脚本模型（HTTP mock，不建立
网络连接）+ 受治理的合成工具 + ChannelStore + 隔离的真实 PostgreSQL。

只验证共享业务路径：授权失败关闭、本轮用途与范围、去重、会话互斥、双存储时序、交付重验与
显式重发。渠道协议（HTTP、飞书 SDK）属于 Task 6/7。HTTP mock 不等于真实模型验证。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.sdk_core.synthetic_tools import (
    QUERY_TOOL,
    RETENTION_SECONDS,
    TARGET,
    TOTAL_TOOL,
    Clock,
    Grants,
    RecordingAdapter,
    catalog,
    ready_engine,
)
from tests.sdk_core.test_app import Scripts, after, answer, cite, tool_call, upstream_error
from tests.sdk_core.test_model_api import OPENAI as PROFILE

from xiaowei.app import AppConfig, Application, DataPolicy, TurnError
from xiaowei.channel import (
    AccessDecision,
    AccessDeniedError,
    ChannelService,
    InboundRequest,
    RequestRef,
    ResultDelivery,
    ResultUnavailableError,
)
from xiaowei.channel_store import (
    ChannelStore,
    ChannelStoreUnavailableError,
    NotReadyError,
    RequestRecord,
    RequestUnavailableError,
    ResultNotSavedError,
    SessionBusyError,
)
from xiaowei.evidence import EvidenceStore
from xiaowei.governance import GovernedTools
from xiaowei.model_api import ModelBinding, open_model
from xiaowei.models import AgentAnswer, Budget, Channel, Delivery, Identity
from xiaowei.session import SessionInputPolicy, SessionLimits
from xiaowei.storage import Readiness, hold_instance_lock

pytestmark = pytest.mark.loopback

FAKE_MODEL_KEY = "sk-test-channel-model"
BUDGET = Budget(max_turns=6, max_tool_calls=5, timeout_seconds=30.0)
TOOLS = frozenset({TOTAL_TOOL, QUERY_TOOL})


@dataclass
class Access:
    """测试用的唯一授权来源：入口解析与 Evidence 授权读同一份可撤销授权。"""

    grants: Grants
    broken: bool = False
    resolves: int = 0

    async def resolve(self, channel: Channel, subject_id: str) -> AccessDecision | None:
        self.resolves += 1
        if self.broken:
            raise RuntimeError("authorization backend down")
        tools = frozenset(t for s, g, t in self.grants.allowed if s == subject_id and g == TARGET)
        if not tools:
            return None
        return AccessDecision(
            subject_id=subject_id,
            target_ids=frozenset({TARGET}),
            authorized_tools=tools,
            policy_version="p1",
        )

    async def authorize(self, identity: Identity, target_id: str, tool_id: str) -> bool:
        return await self.grants(identity, target_id, tool_id)


class BarrierStore(ChannelStore):
    """结果保存前停住：第一轮已经从 run_turn 返回，但终态尚未落库。"""

    entered: asyncio.Event
    release: asyncio.Event

    async def complete(self, record: RequestRecord, result: AgentAnswer) -> RequestRecord:
        self.entered.set()
        await self.release.wait()
        return await super().complete(record, result)


def app_config(**overrides: Any) -> AppConfig:
    values: dict[str, Any] = {
        "purposes": {"query": TOOLS, "diagnose": frozenset({TOTAL_TOOL})},
        "data_policies": {
            PROFILE.data_policy_id: DataPolicy(
                input=SessionInputPolicy(max_bytes=200), model_tools=TOOLS
            )
        },
        "session_limits": SessionLimits(
            max_history_turns=5, max_history_bytes=40_000, retention_seconds=7200
        ),
        "max_concurrent_turns": 4,
    }
    values.update(overrides)
    return AppConfig(**values)


@dataclass
class Env:
    engine: AsyncEngine
    clock: Clock
    grants: Grants
    access: Access
    adapter: RecordingAdapter
    scripts: Scripts
    binding: ModelBinding
    evidence: EvidenceStore
    app: Application
    readiness: Readiness = field(default_factory=Readiness)
    store: ChannelStore = field(init=False)
    service: ChannelService = field(init=False)

    def __post_init__(self) -> None:
        self.store = self.channel_store()
        self.service = ChannelService(self.app, self.results(self.store))

    def channel_store(
        self, cls: type[ChannelStore] = ChannelStore, readiness: Readiness | None = None
    ) -> ChannelStore:
        return cls(
            self.engine,
            key=SecretStr("synthetic-channel-key"),
            clock=self.clock,
            readiness=readiness or self.readiness,
            request_retention_seconds=RETENTION_SECONDS,
            session_retention_seconds=7200,
            evidence_retention_seconds=RETENTION_SECONDS,
            max_answer_bytes=4000,
        )

    def results(self, store: ChannelStore | None = None) -> ResultDelivery:
        return ResultDelivery(store or self.store, self.evidence, self.access, budget=BUDGET)

    def inbound(
        self,
        message: str,
        request_id: str = "r1",
        *,
        mode: str = "query",
        subject: str = "alice",
        conversation: str = "cookie-1",
        channel: str = "web",
    ) -> InboundRequest:
        return InboundRequest(
            channel=channel,  # type: ignore[arg-type]
            channel_request_id=request_id,
            subject_id=subject,
            conversation_id=conversation,
            mode=mode,  # type: ignore[arg-type]
            message=message,
            received_at=datetime(2026, 10, 1, tzinfo=UTC),
        )

    async def run(self, message: str, request_id: str = "r1", **kwargs: Any) -> RequestRecord:
        return await self.service.process(
            await self.service.accept(self.inbound(message, request_id, **kwargs))
        )

    def model_calls(self, message: str) -> int:
        return len(self.scripts.calls.get(message, []))

    async def requests(self) -> int:
        async with self.engine.connect() as conn:
            return int(await conn.scalar(text("SELECT count(*) FROM xiaowei_request")) or 0)

    async def session_state(self, session_id: str) -> str | None:
        async with self.engine.connect() as conn:
            return await conn.scalar(
                text("SELECT state FROM xiaowei_session WHERE session_id = :s"), {"s": session_id}
            )

    async def fail_request_writes(self, when: str = "true") -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE FUNCTION xw_fail() RETURNS trigger LANGUAGE plpgsql AS"
                    " $$ BEGIN RAISE EXCEPTION 'injected'; END $$"
                )
            )
            await conn.execute(
                text(
                    "CREATE TRIGGER xw_fail BEFORE UPDATE ON xiaowei_request"
                    f" FOR EACH ROW WHEN ({when}) EXECUTE FUNCTION xw_fail()"
                )
            )

    async def heal_request_writes(self) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER xw_fail ON xiaowei_request"))
            await conn.execute(text("DROP FUNCTION xw_fail()"))


def ref(request_id: str = "r1", subject: str = "alice", channel: Channel = "web") -> RequestRef:
    return RequestRef(
        channel=channel, subject_id=subject, conversation_id="cookie-1", request_id=request_id
    )


@dataclass
class Outbox:
    """渠道的单次发送：记录发出的内容，返回预设结果。"""

    outcome: str = "sent"
    sent: list[Delivery] = field(default_factory=list)

    async def __call__(self, delivery: Delivery) -> Any:
        self.sent.append(delivery)
        return self.outcome


@pytest.fixture
async def env(postgres_url: URL, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), FAKE_MODEL_KEY)
    async with ready_engine(postgres_url) as engine:
        clock, grants = Clock(), Grants()
        grants.grant("alice", TOTAL_TOOL, QUERY_TOOL)
        grants.grant("bob", TOTAL_TOOL, QUERY_TOOL)
        access = Access(grants)
        evidence = EvidenceStore(
            engine,
            catalog(),
            authorize=access.authorize,
            clock=clock,
            retention_seconds=RETENTION_SECONDS,
        )
        adapter = RecordingAdapter()
        scripts = Scripts()
        async with open_model(PROFILE, transport=scripts.transport()) as bound:
            app = Application(
                app_config(),
                model=bound,
                engine=engine,
                governance=GovernedTools(evidence),
                local_tools={
                    (TOTAL_TOOL, TARGET): adapter.execute,
                    (QUERY_TOOL, TARGET): adapter.execute,
                },
                clock=clock,
            )
            yield Env(engine, clock, grants, access, adapter, scripts, bound, evidence, app)


# ---- 授权与本轮范围 ------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["raises", "none", "wrong_type"])
async def test_access_policy_fails_closed(env: Env, failure: str) -> None:
    if failure == "raises":
        env.access.broken = True
    elif failure == "none":
        env.grants.revoke_all()
    else:
        env.access.resolve = lambda *_: _async({"subject_id": "alice"})  # type: ignore[method-assign]
    message = env.scripts.add("授权失败时？", tool_call("order_total", region="east"), cite())
    with pytest.raises(AccessDeniedError):
        await env.service.accept(env.inbound(message))
    # 不创建可运行轮次：请求、模型与工具都为 0。
    assert await env.requests() == 0
    assert env.model_calls(message) == 0 and env.adapter.calls == []


async def _async(value: object) -> object:
    return value


async def test_mode_and_current_grants_decide_each_turn_scope(env: Env) -> None:
    first = env.scripts.add("查询东区", tool_call("order_total", region="east"), cite())
    second = env.scripts.add("诊断东区", tool_call("order_total", region="east"), cite())
    third = env.scripts.add("撤销查询后再查", tool_call("order_total", region="east"), cite())
    await env.run(first, "r1", mode="query")
    await env.run(second, "r2", mode="diagnose")
    env.grants.allowed.discard(("alice", TARGET, QUERY_TOOL))
    await env.run(third, "r3", mode="query")
    # 诊断轮不展示实际查询工具；查询轮只在当前仍获准时展示。上一轮许可不延续到下一轮。
    assert env.scripts.tools_seen(first) == [{"order_total", "run_query"}] * 2
    assert env.scripts.tools_seen(second) == [{"order_total"}] * 2
    assert env.scripts.tools_seen(third) == [{"order_total"}] * 2


def test_inbound_cannot_carry_session_target_or_tool_scope(env: Env) -> None:
    base = env.inbound("问题").model_dump()
    for forged in ({"session_id": "s-x"}, {"target_scope": ["other"]}, {"tool_scope": ["x/y"]}):
        with pytest.raises(ValidationError):
            InboundRequest(**{**base, **forged})
    with pytest.raises(ValidationError):
        InboundRequest(**{**base, "message": ""})


async def test_runs_bind_trusted_identity_and_server_session(env: Env) -> None:
    message = env.scripts.add("东区订单？", tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.inbound(message))
    current = await env.store.current_session("web", "alice", "cookie-1")
    identity = receipt.context.identity
    assert (identity.subject_id, identity.session_id, identity.channel) == (
        "alice",
        current.session_id,
        "web",
    )
    assert identity.turn_id == receipt.record.turn_id
    assert receipt.context.target_scope == frozenset({TARGET})
    record = await env.service.process(receipt)
    assert record.state == "completed" and record.answer is not None
    assert [c.tool_id for c in env.adapter.calls] == [TOTAL_TOOL]


# ---- 去重与会话互斥 ------------------------------------------------------------------------


async def test_duplicate_requests_never_run_again(env: Env) -> None:
    gate = asyncio.Event()
    message = env.scripts.add(
        "重复？", after(gate, tool_call("order_total", region="east")), cite()
    )
    first = asyncio.ensure_future(env.run(message))
    while env.model_calls(message) == 0:
        await asyncio.sleep(0.01)
    # 运行中：同一请求编号返回“处理中”，不再运行。
    duplicate = await env.service.accept(env.inbound(message))
    assert not duplicate.created and duplicate.record.state == "running"
    assert (await env.service.process(duplicate)).state == "running"
    gate.set()
    done = await first
    assert done.state == "completed"
    # 已完成：返回原记录，模型与工具调用次数不变。
    again = await env.service.process(await env.service.accept(env.inbound(message)))
    assert again.state == "completed" and again.answer == done.answer
    assert env.model_calls(message) == 2 and len(env.adapter.calls) == 1


async def test_session_stays_busy_until_the_result_is_saved(env: Env) -> None:
    store = env.channel_store(BarrierStore)
    assert isinstance(store, BarrierStore)
    store.entered, store.release = asyncio.Event(), asyncio.Event()
    service = ChannelService(env.app, env.results(store))
    first = env.scripts.add("第一轮", tool_call("order_total", region="east"), cite())
    second = env.scripts.add("第二轮", tool_call("order_total", region="west"), cite())

    running = asyncio.ensure_future(service.process(await service.accept(env.inbound(first))))
    await store.entered.wait()  # 第一轮已从 run_turn 返回，结果尚未保存
    blocked = await service.process(await service.accept(env.inbound(second, "r2")))
    assert (blocked.state, blocked.failure_code) == ("failed", "busy")
    assert env.model_calls(second) == 0 and len(env.adapter.calls) == 1
    with pytest.raises(SessionBusyError):
        await service.new_session("web", "alice", "cookie-1")

    store.release.set()
    assert (await running).state == "completed"
    third = env.scripts.add("第三轮", tool_call("order_total", region="east"), cite())
    assert (await service.process(await service.accept(env.inbound(third, "r3")))).state == (
        "completed"
    )


async def test_global_concurrency_limit_marks_requests_busy(env: Env) -> None:
    env.app = Application(
        app_config(max_concurrent_turns=1),
        model=env.binding,
        engine=env.engine,
        governance=GovernedTools(env.evidence),
        local_tools={
            (TOTAL_TOOL, TARGET): env.adapter.execute,
            (QUERY_TOOL, TARGET): env.adapter.execute,
        },
        clock=env.clock,
    )
    env.service = ChannelService(env.app, env.results())
    gate = asyncio.Event()
    slow = env.scripts.add("占住并发", after(gate, tool_call("order_total", region="east")), cite())
    other = env.scripts.add("另一个会话", cite())
    first = asyncio.ensure_future(env.run(slow))
    while env.model_calls(slow) == 0:
        await asyncio.sleep(0.01)
    refused = await env.run(other, "r2", conversation="cookie-2")
    assert (refused.state, refused.failure_code) == ("failed", "busy")
    assert env.model_calls(other) == 0
    gate.set()
    assert (await first).state == "completed"


# ---- 失败、双存储与 readiness -----------------------------------------------------------------


async def test_turn_failures_persist_safe_codes_and_fixed_receipts(env: Env) -> None:
    model = env.scripts.add("模型失败", upstream_error)
    forged = env.scripts.add("伪造引用", lambda call: answer(["ev_forged"]))
    failed = await env.run(model, "r1")
    rejected = await env.run(forged, "r2")
    too_long = await env.run("长" * 300, "r3")  # 超出输入准入策略：模型调用前拒绝
    assert (failed.state, failed.failure_code) == ("failed", "model_failed")
    assert (rejected.state, rejected.failure_code) == ("failed", "evidence_failed")
    assert (too_long.state, too_long.failure_code) == ("failed", "session_failed")
    assert env.model_calls("长" * 300) == 0
    view = await env.service.results.view(ref("r1"))
    assert view.delivery is not None and view.delivery.evidence_ids == ()
    assert "invalid request" not in view.delivery.content


async def test_result_save_failure_closes_the_session(env: Env) -> None:
    message = env.scripts.add("结果保存失败", tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.inbound(message))
    await env.fail_request_writes("NEW.state = 'completed'")
    with pytest.raises(ResultNotSavedError):
        await env.service.process(receipt)
    record = await env.store.get("web", "alice", "cookie-1", "r1")
    assert (record.state, record.failure_code) == ("failed", "result_not_saved")
    assert await env.session_state(record.session_id) == "closed"
    assert env.readiness.ok
    await env.heal_request_writes()
    # 同一会话不再回放：下一轮在模型调用前失败，用户需要新建会话。
    nxt = env.scripts.add("同会话下一轮", cite())
    assert (await env.run(nxt, "r2")).failure_code == "session_failed"
    assert env.model_calls(nxt) == 0
    await env.service.new_session("web", "alice", "cookie-1")
    fresh = env.scripts.add("新会话", tool_call("order_total", region="east"), cite())
    assert (await env.run(fresh, "r3")).state == "completed"


async def test_unwritable_terminal_state_locks_readiness(env: Env) -> None:
    message = env.scripts.add("状态写不了", tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.inbound(message))
    await env.fail_request_writes("NEW.state IN ('completed', 'failed')")
    with pytest.raises(ResultNotSavedError):
        await env.service.process(receipt)
    assert not env.readiness.ok
    with pytest.raises(NotReadyError):
        await env.service.accept(env.inbound("下一条", "r2", conversation="cookie-2"))
    with pytest.raises(NotReadyError):
        await env.service.new_session("web", "alice", "cookie-1")


async def test_cancelled_turn_locks_readiness_for_restart_recovery(
    env: Env, postgres_url: URL
) -> None:
    gate = asyncio.Event()
    message = env.scripts.add("被取消", after(gate, cite()))
    task = asyncio.ensure_future(env.run(message))
    while env.model_calls(message) == 0:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not env.readiness.ok
    record = await env.store.get("web", "alice", "cookie-1", "r1")
    assert record.state == "running"
    # 停止并重启：持锁恢复标为 interrupted 并关闭会话，中断回执最多发送一次。
    restarted = env.channel_store(readiness=Readiness())  # 新进程的 readiness
    # 恢复后的工作在持锁期间进行（与 serve 相同）；绑定的锁释放后存储不再开始新工作。
    async with hold_instance_lock(env.engine, restarted.readiness) as lock:
        await restarted.recover(lock)
        after_restart = await restarted.get("web", "alice", "cookie-1", "r1")
        assert (after_restart.state, after_restart.failure_code) == ("interrupted", "interrupted")
        assert await env.session_state(record.session_id) == "closed"
        outbox = Outbox()
        results = env.results(restarted)
        assert await results.send(ref("r1"), outbox) == "sent"
        assert await results.send(ref("r1"), outbox) is None
        assert [d.content for d in outbox.sent] == ["上次处理已中断，请重新发送"]


async def test_new_session_rotates_without_copying_history(env: Env) -> None:
    first = env.scripts.add("旧会话的问题", tool_call("order_total", region="east"), cite())
    old = await env.run(first)
    new = await env.service.new_session("web", "alice", "cookie-1")
    assert new.session_id != old.session_id
    second = env.scripts.add("新会话的问题", tool_call("order_total", region="east"), cite())
    record = await env.run(second, "r2")
    assert record.session_id == new.session_id
    inputs = env.scripts.calls[second][0].input
    assert [i for i in inputs if i.get("role") == "user"] == [{"role": "user", "content": second}]


# ---- 交付：重读、首次发送、显式重发与撤权 ---------------------------------------


async def test_reread_and_resend_use_saved_answer_without_runner(env: Env) -> None:
    message = env.scripts.add("可重发", tool_call("order_total", region="east"), cite())
    record = await env.run(message)
    model_calls, tool_calls = env.model_calls(message), len(env.adapter.calls)

    # 不持有 Application 或模型绑定的交付对象。
    results = env.results()
    view = await results.view(ref())
    assert view.state == "completed" and view.delivery is not None
    assert view.delivery.facts and view.delivery.evidence_ids == record.answer.evidence_ids  # type: ignore[union-attr]

    outbox = Outbox(outcome="failed")
    assert await results.send(ref(), outbox) == "failed"
    assert await results.send(ref(), outbox) is None  # 事件重投遇到 failed：零发送
    outbox.outcome = "sent"
    assert await results.send(ref(), outbox, resend=True) == "sent"
    assert await results.send(ref(), outbox, resend=True) is None
    assert len(outbox.sent) == 2 and outbox.sent[0] == outbox.sent[1]
    assert (env.model_calls(message), len(env.adapter.calls)) == (model_calls, tool_calls)


async def test_revoked_results_are_neither_readable_nor_sent(env: Env) -> None:
    message = env.scripts.add("撤权前完成", tool_call("order_total", region="east"), cite())
    await env.run(message)
    env.grants.allowed.discard(("alice", TARGET, TOTAL_TOOL))
    with pytest.raises(ResultUnavailableError):
        await env.service.results.view(ref())
    outbox = Outbox()
    with pytest.raises(ResultUnavailableError):
        await env.service.results.send(ref(), outbox)
    assert outbox.sent == []
    record = await env.store.get("web", "alice", "cookie-1", "r1")
    assert (record.state, record.delivery) == ("completed", "failed")
    # 后续同会话回放引用了已撤权的证据：在模型调用前失败。
    nxt = env.scripts.add("撤权后追问", cite())
    assert (await env.run(nxt, "r2")).failure_code == "session_failed"
    assert env.model_calls(nxt) == 0


async def test_requests_are_unreadable_across_identities_and_channels(env: Env) -> None:
    message = env.scripts.add("只属于 alice", tool_call("order_total", region="east"), cite())
    await env.run(message)
    for other in (ref(subject="bob"), ref(channel="feishu"), ref("r-missing")):
        with pytest.raises(RequestUnavailableError):
            await env.service.results.view(other)
        with pytest.raises(RequestUnavailableError):
            await env.service.results.send(other, Outbox())


async def test_transmit_errors_are_recorded_as_unknown(env: Env) -> None:
    message = env.scripts.add("发送异常", tool_call("order_total", region="east"), cite())
    await env.run(message)

    async def explode(delivery: Delivery) -> Any:
        raise ConnectionError("channel down")

    with pytest.raises(ConnectionError):
        await env.service.results.send(ref(), explode)
    record = await env.store.get("web", "alice", "cookie-1", "r1")
    assert record.delivery == "unknown"
    assert await env.service.results.send(ref(), Outbox()) is None
    assert await env.service.results.send(ref(), Outbox(), resend=True) == "sent"


async def test_fixed_receipts_are_not_resendable(env: Env) -> None:
    failed = await env.run(env.scripts.add("失败回执", upstream_error))
    assert failed.state == "failed"
    outbox = Outbox(outcome="failed")
    assert await env.service.results.send(ref(), outbox) == "failed"
    assert await env.service.results.send(ref(), outbox, resend=True) is None
    assert len(outbox.sent) == 1


# ---- 装配 --------------------------------------------------------------------------------


class Impersonator:
    """另一个授权来源：复用原策略的 ``authorize`` 绑定方法，却把 mallory 解析成 alice。"""

    def __init__(self, original: Access) -> None:
        self.authorize = original.authorize
        self._original = original

    async def resolve(self, channel: Channel, subject_id: str) -> AccessDecision | None:
        return await self._original.resolve(channel, "alice" if subject_id == "mallory" else "")


async def test_same_access_policy_instance_assembles_and_serves(env: Env) -> None:
    results = env.results()
    service = ChannelService(env.app, results)
    assert results.access is env.access and results.evidence is env.evidence
    message = env.scripts.add("同一来源？", tool_call("order_total", region="east"), cite())
    record = await service.process(await service.accept(env.inbound(message)))
    assert record.state == "completed"
    assert await results.send(ref(), Outbox()) == "sent"


@pytest.mark.parametrize("source", ["impersonator", "equal_twin", "plain_callable", "other_method"])
async def test_assembly_rejects_any_other_authorization_source(env: Env, source: str) -> None:
    if source == "impersonator":
        # 复用同一个绑定方法：callable 相等，但来源对象不同。
        access: Any = Impersonator(env.access)
        evidence = env.evidence
        assert evidence.authorize == access.authorize
    elif source == "equal_twin":
        # 与原策略值相等（dataclass 比较字段）却是另一个对象，同样复用原绑定方法。
        access = Access(env.grants)
        access.authorize = env.access.authorize  # type: ignore[method-assign]
        access.resolve = Impersonator(env.access).resolve  # type: ignore[method-assign]
        evidence = env.evidence
        assert access == env.access and evidence.authorize == access.authorize
    else:
        access = env.access

        # 包装函数无法证明它来自这个 AccessPolicy；同一对象的其他方法也不是它的授权。
        async def wrapped(identity: Identity, target_id: str, tool_id: str) -> bool:
            return await env.access.authorize(identity, target_id, tool_id)

        evidence = EvidenceStore(
            env.engine,
            catalog(),
            authorize=wrapped if source == "plain_callable" else env.access.resolve,  # type: ignore[arg-type]
            clock=env.clock,
            retention_seconds=RETENTION_SECONDS,
        )
    message = env.scripts.add("冒充 alice", tool_call("order_total", region="east"), cite())
    with pytest.raises(ValueError, match="AccessPolicy"):
        ResultDelivery(env.store, evidence, access, budget=BUDGET)
    # 启动装配即失败：没有可接收请求的服务，请求、模型、工具与 Adapter I/O 都为 0。
    assert await env.requests() == 0
    assert env.model_calls(message) == 0 and env.adapter.calls == []


async def test_authorization_source_cannot_be_replaced_after_assembly(env: Env) -> None:
    results = env.results()
    service = ChannelService(env.app, results)
    impostor = Impersonator(env.access)
    for target, name, value in (
        (results, "access", impostor),
        (results, "evidence", env.evidence),
        (results, "store", env.store),
        (results, "budget", BUDGET),
        (service, "results", env.results()),
    ):
        with pytest.raises(AttributeError):
            setattr(target, name, value)
    assert results.access is env.access and service.results is results


async def test_entry_and_evidence_authorization_share_one_access_policy(env: Env) -> None:
    other = Access(env.grants)
    with pytest.raises(ValueError, match="AccessPolicy"):
        ResultDelivery(env.store, env.evidence, other, budget=BUDGET)
    separate = EvidenceStore(
        env.engine,
        catalog(),
        authorize=env.access.authorize,
        clock=env.clock,
        retention_seconds=RETENTION_SECONDS,
    )
    with pytest.raises(ValueError, match="EvidenceStore"):
        ChannelService(env.app, ResultDelivery(env.store, separate, env.access, budget=BUDGET))


# ---- 实例接管：请求启动与 Session 边界 ------------------------------------------------------

# 只终止持有会话级实例锁（排他）的后端。
TERMINATE_INSTANCE_LOCK = text(
    "SELECT count(pg_terminate_backend(pid)) FROM pg_locks"
    " WHERE locktype = 'advisory' AND granted AND mode = 'ExclusiveLock'"
)
WAITING_ON_LOCK = text(
    "SELECT count(*) FROM pg_stat_activity"
    " WHERE wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
)
INTERRUPTED_TEXT = "接管时中断的轮次"


async def terminate_instance_lock(env: Env) -> None:
    async with env.engine.begin() as conn:
        assert await conn.scalar(TERMINATE_INSTANCE_LOCK) == 1


async def waiting_on_locks(env: Env, count: int, timeout: float = 5) -> bool:
    async with env.engine.connect() as conn:
        for _ in range(int(timeout / 0.02)):
            if await conn.scalar(WAITING_ON_LOCK) >= count:
                return True
            await conn.commit()
            await asyncio.sleep(0.02)
    return False


async def history_items(env: Env, session_id: str) -> int:
    async with env.engine.connect() as conn:
        count = await conn.scalar(
            text("SELECT count(*) FROM agent_messages WHERE session_id = :s"), {"s": session_id}
        )
    return int(count or 0)


async def test_start_after_the_lock_moved_is_refused_before_the_model(env: Env) -> None:
    """旧实例已接受请求、错过终止通知；新实例已取得锁但尚未恢复：旧实例不能启动它。"""
    message = env.scripts.add(INTERRUPTED_TEXT, tool_call("order_total", region="east"), cite())
    async with hold_instance_lock(env.engine, Readiness()) as old_lock:
        await env.store.recover(old_lock)
        receipt = await env.service.accept(env.inbound(message))
        await terminate_instance_lock(env)
        async with hold_instance_lock(env.engine, Readiness()):
            assert env.readiness.ok
            with pytest.raises(NotReadyError):
                await env.service.process(receipt)
            assert env.readiness.reason == "instance_lock_lost"
    assert env.model_calls(message) == 0 and env.adapter.calls == []
    assert (await env.store.get("web", "alice", "cookie-1", "r1")).state == "accepted"


async def test_a_bound_instance_still_runs_accepted_requests(env: Env) -> None:
    """成功对照：持锁并已恢复的实例照常 accepted → running → completed。"""
    message = env.scripts.add("持锁实例的轮次", tool_call("order_total", region="east"), cite())
    async with hold_instance_lock(env.engine, env.readiness) as lock:
        await env.store.recover(lock)
        assert (await env.run(message)).state == "completed"
    assert env.readiness.ok


async def recover_elsewhere(env: Env) -> tuple[ChannelStore, int]:
    """另一个进程持锁恢复；返回它的存储（锁已释放）与中断数。"""
    restarted = env.channel_store(readiness=Readiness())
    async with hold_instance_lock(env.engine, restarted.readiness) as lock:
        report = await restarted.recover(lock)
    return restarted, report.interrupted


async def after_takeover_history_is_not_replayed(env: Env, session_id: str) -> None:
    """恢复后：中断轮次所在会话不可回放，同一会话语境的新轮次在模型前失败；新建会话后
    新轮次完成，模型输入不含中断轮次的内容。"""
    assert await env.session_state(session_id) == "closed"
    async with hold_instance_lock(env.engine, Readiness()) as lock:
        restarted = env.channel_store(readiness=Readiness())
        await restarted.recover(lock)
        service = ChannelService(env.app, env.results(restarted))
        same = env.scripts.add("同一会话的下一轮", cite())
        record = await service.process(await service.accept(env.inbound(same, "r2")))
        assert (record.state, record.failure_code) == ("failed", "session_failed")
        assert env.model_calls(same) == 0
        await service.new_session("web", "alice", "cookie-1")
        fresh = env.scripts.add("新会话的下一轮", tool_call("order_total", region="west"), cite())
        assert (await service.process(await service.accept(env.inbound(fresh, "r3")))).state == (
            "completed"
        )
        for call in env.scripts.calls[fresh]:
            assert INTERRUPTED_TEXT not in json.dumps(call.input, ensure_ascii=False)


async def test_recovery_before_the_first_session_registration_closes_it_for_good(
    env: Env,
) -> None:
    """请求已在旧实例 running、Session 元数据尚未登记时被恢复：旧 Runner 之后不能登记出可回放
    的会话，也不调用模型。"""
    message = env.scripts.add(INTERRUPTED_TEXT, tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.inbound(message))
    await env.store.start(receipt.record)
    _, interrupted = await recover_elsewhere(env)
    assert interrupted == 1
    with pytest.raises(TurnError) as raised:
        await env.app.run_turn(receipt.context, receipt.message)
    assert raised.value.reason == "session_unavailable"
    assert env.model_calls(message) == 0
    assert await history_items(env, receipt.record.session_id) == 0
    await after_takeover_history_is_not_replayed(env, receipt.record.session_id)


async def test_recovery_racing_the_session_registration_closes_it_for_good(env: Env) -> None:
    """恢复与旧 Runner 的首次登记并发：无论谁先提交，会话最终关闭、历史不提交。"""
    message = env.scripts.add(INTERRUPTED_TEXT, tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.inbound(message))
    record = await env.store.start(receipt.record)
    # 暂停点：同一 session_id 的未提交插入挡住 Runner 的首次登记。
    blocker = await env.engine.connect()
    await blocker.begin()
    await blocker.execute(
        text(
            "INSERT INTO xiaowei_session VALUES (:s, 'alice', 'web', 'x', now(), now(), 0,"
            " 'active')"
        ),
        {"s": record.session_id},
    )
    run = asyncio.create_task(env.app.run_turn(receipt.context, receipt.message))
    assert await waiting_on_locks(env, 1)
    recovery = asyncio.create_task(recover_elsewhere(env))
    await waiting_on_locks(env, 2, timeout=2)  # 修复后恢复也在这一行上等待
    await blocker.rollback()
    await blocker.close()
    _, interrupted = await asyncio.wait_for(recovery, 20)
    assert interrupted == 1
    with pytest.raises(TurnError):
        await asyncio.wait_for(run, 30)
    assert await history_items(env, record.session_id) == 0
    await after_takeover_history_is_not_replayed(env, record.session_id)


async def test_recovery_while_the_session_is_active_blocks_the_commit(env: Env) -> None:
    gate, entered = asyncio.Event(), asyncio.Event()
    message = env.scripts.add(
        INTERRUPTED_TEXT,
        after(gate, tool_call("order_total", region="east"), entered=entered),
        cite(),
    )
    receipt = await env.service.accept(env.inbound(message))
    record = await env.store.start(receipt.record)
    run = asyncio.create_task(env.app.run_turn(receipt.context, receipt.message))
    await asyncio.wait_for(entered.wait(), 20)
    assert await env.session_state(record.session_id) == "active"
    await recover_elsewhere(env)
    gate.set()
    with pytest.raises(TurnError):
        await asyncio.wait_for(run, 30)
    assert await history_items(env, record.session_id) == 0
    await after_takeover_history_is_not_replayed(env, record.session_id)


async def test_recovery_while_the_session_is_writing_keeps_it_closed(env: Env) -> None:
    message = env.scripts.add(INTERRUPTED_TEXT, tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.inbound(message))
    record = await env.store.start(receipt.record)
    async with env.engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE FUNCTION xw_slow() RETURNS trigger LANGUAGE plpgsql AS"
                " $$ BEGIN PERFORM pg_sleep(1.5); RETURN NEW; END $$"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER xw_slow BEFORE INSERT ON agent_messages"
                " FOR EACH ROW EXECUTE FUNCTION xw_slow()"
            )
        )
    try:
        run = asyncio.create_task(env.app.run_turn(receipt.context, receipt.message))
        async with asyncio.timeout(20):
            while await env.session_state(record.session_id) != "writing":
                await asyncio.sleep(0.02)
        await recover_elsewhere(env)
        with pytest.raises(TurnError):
            await asyncio.wait_for(run, 30)
    finally:
        async with env.engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER xw_slow ON agent_messages"))
            await conn.execute(text("DROP FUNCTION xw_slow()"))
    # 底层历史可能已写入，但会话保持关闭，永不回放。
    await after_takeover_history_is_not_replayed(env, record.session_id)


# ---- 投递尝试归属 ----------------------------------------------------------------------------


@dataclass
class Gated:
    """一次发送：进入后等待放行，再返回预设结果或抛出异常。"""

    outcome: str = "sent"
    sent: list[Delivery] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self, delivery: Delivery) -> Any:
        self.sent.append(delivery)
        self.entered.set()
        await self.release.wait()
        if self.outcome == "raise":
            raise RuntimeError("transport down")
        return self.outcome


async def delivery_of(env: Env) -> str:
    async with env.engine.connect() as conn:
        return str(await conn.scalar(text("SELECT delivery FROM xiaowei_request")))


@pytest.mark.parametrize("old_outcome", ["sent", "failed", "unknown", "raise", "cancel"])
@pytest.mark.parametrize("path", ["first", "resend"])
async def test_a_stale_send_never_settles_a_newer_attempt(
    env: Env, path: str, old_outcome: str
) -> None:
    """旧尝试在途时被启动恢复记为 unknown，显式重发取得新尝试；旧尝试返回（或异常、取消）
    不能改写新尝试，新尝试进行中第三次取得失败且不发送，新尝试的结果照常落定。"""
    message = env.scripts.add("投递尝试", tool_call("order_total", region="east"), cite())
    await env.run(message)
    if path == "resend":
        await env.results().send(ref(), Outbox("failed"))
    old = Gated("unknown" if old_outcome == "cancel" else old_outcome)
    stale = asyncio.create_task(env.results().send(ref(), old, resend=path == "resend"))
    await asyncio.wait_for(old.entered.wait(), 10)

    restarted = env.channel_store(readiness=Readiness())
    async with hold_instance_lock(env.engine, restarted.readiness) as lock:
        assert (await restarted.recover(lock)).unknown == 1
        results = env.results(restarted)
        newer = Gated("sent")
        current = asyncio.create_task(results.send(ref(), newer, resend=True))
        await asyncio.wait_for(newer.entered.wait(), 10)

        if old_outcome == "cancel":
            stale.cancel()
        else:
            old.release.set()
        # 旧尝试不能落定：结果正常返回时报存储错误；发送本身的异常或取消原样传播。
        expected: type[BaseException] = {
            "raise": RuntimeError,
            "cancel": asyncio.CancelledError,
        }.get(old_outcome, ChannelStoreUnavailableError)
        with pytest.raises(expected):
            await stale
        assert env.readiness.reason == "delivery_state_lost"  # 锁低的是旧尝试所在的实例
        assert restarted.readiness.ok
        assert await delivery_of(env) == "sending"  # 新尝试仍在进行

        third = Gated()
        assert await results.send(ref(), third, resend=True) is None
        assert third.sent == []

        newer.release.set()
        assert await current == "sent"
        assert await delivery_of(env) == "sent"
    assert len(old.sent) == len(newer.sent) == 1


async def test_concurrent_resends_send_once(env: Env) -> None:
    message = env.scripts.add("并发重发", tool_call("order_total", region="east"), cite())
    await env.run(message)
    await env.results().send(ref(), Outbox("failed"))
    first, second = Gated(), Gated()
    tasks = [
        asyncio.create_task(env.results().send(ref(), gated, resend=True))
        for gated in (first, second)
    ]
    await asyncio.sleep(0.3)
    first.release.set()
    second.release.set()
    outcomes = await asyncio.gather(*tasks)
    assert sorted(map(str, outcomes)) == ["None", "sent"]
    assert len(first.sent) + len(second.sent) == 1
    assert await delivery_of(env) == "sent"
