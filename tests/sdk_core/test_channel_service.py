"""P1-B Task 5：共享 ChannelService。真 Runner + ``open_model`` 装配的脚本模型（HTTP mock，不建立
网络连接）+ 受治理的合成工具 + ChannelStore + 隔离的真实 PostgreSQL。

只验证共享业务路径：授权失败关闭、本轮用途与范围、去重、会话互斥、双存储时序、交付重验与
显式重发。渠道协议（HTTP、飞书 SDK）属于 Task 6/7。HTTP mock 不等于真实模型验证。
"""

import asyncio
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

from xiaowei.app import AppConfig, Application, DataPolicy
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
            subject_id=subject_id, target_id=TARGET, authorized_tools=tools, policy_version="p1"
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
                local_tools={TOTAL_TOOL: adapter.execute, QUERY_TOOL: adapter.execute},
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
        local_tools={TOTAL_TOOL: env.adapter.execute, QUERY_TOOL: env.adapter.execute},
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
