"""飞书单群 F1：群会话归属（owner）与本轮发起人（actor）分离。

真 Runner + 脚本模型（HTTP mock，不建立网络连接）+ 受治理的合成工具 + 真 PolicySession /
SQLAlchemySession + 隔离的真实 PostgreSQL；授权用产品的 ``StaticAccess`` 与群共享策略，成员目录
是可控替身（F2 才接入 SDK 的成员接口）。不涉及群入口协议、排队与原消息发送（F2/F3）。
"""

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import pytest
from agents.extensions.memory import SQLAlchemySession
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
    RecordingAdapter,
    catalog,
    ready_engine,
    secret,
)
from tests.sdk_core.test_app import Scripts, answer, cite, clarify, tool_call
from tests.sdk_core.test_channel_service import BUDGET, FAKE_MODEL_KEY, Outbox, app_config
from tests.sdk_core.test_model_api import OPENAI as PROFILE

from xiaowei.app import Application
from xiaowei.channel import (
    AccessDecision,
    AccessDeniedError,
    ChannelService,
    GroupScope,
    InboundRequest,
    RequestRef,
    ResultDelivery,
)
from xiaowei.channel_store import (
    ChannelStore,
    RequestConflictError,
    RequestRecord,
    RequestUnavailableError,
)
from xiaowei.evidence import EvidenceStore, EvidenceUnavailableError
from xiaowei.governance import GovernedTools
from xiaowei.model_api import open_model
from xiaowei.models import Identity, Owner, RunContext
from xiaowei.runtime import AccessConfig, GroupAccess, StaticAccess
from xiaowei.session import PolicySession, SessionInputPolicy, SessionUnavailableError
from xiaowei.storage import hold_instance_lock, open_engine, upgrade_storage

pytestmark = pytest.mark.loopback

TOOLS = frozenset({TOTAL_TOOL, QUERY_TOOL})
APP = "cli_synthetic"
TENANT = "tenant-1"
CHAT = "oc_group"
GROUP = GroupScope(app_id=APP, tenant_key=TENANT, chat_id=CHAT)
KEY = "synthetic-channel-key"
A, B, C = "ou_alice", "ou_bob", "ou_carol"
WHEN = datetime(2026, 10, 5, tzinfo=UTC)


@dataclass
class Members:
    """成员目录替身：``mode`` 为 ok（按 ``current``）、error（抛出）或 unknown（不返回 True）。
    ``failing`` 中的成员查询抛出，``holds`` 中的成员查询挂起到对应事件被设置。"""

    current: set[str] = field(default_factory=lambda: {A, B})
    mode: str = "ok"
    checks: list[tuple[str, str]] = field(default_factory=list)
    failing: set[str] = field(default_factory=set)
    holds: dict[str, asyncio.Event] = field(default_factory=dict)

    async def __call__(self, chat_id: str, open_id: str) -> bool:
        self.checks.append((chat_id, open_id))
        hold = self.holds.get(open_id)
        if hold is not None:
            await hold.wait()
        if self.mode == "error" or open_id in self.failing:
            raise RuntimeError("directory unavailable")
        if self.mode == "unknown":
            return None  # type: ignore[return-value]
        return chat_id == CHAT and open_id in self.current


@dataclass
class Env:
    engine: AsyncEngine
    clock: Clock
    members: Members
    access: StaticAccess
    adapter: RecordingAdapter
    scripts: Scripts
    evidence: EvidenceStore
    app: Application
    store: ChannelStore = field(init=False)
    service: ChannelService = field(init=False)

    def __post_init__(self) -> None:
        self.store = ChannelStore(
            self.engine,
            key=SecretStr(KEY),
            clock=self.clock,
            readiness=self.app_readiness(),
            request_retention_seconds=RETENTION_SECONDS,
            session_retention_seconds=7200,
            evidence_retention_seconds=RETENTION_SECONDS,
            max_answer_bytes=4000,
        )
        self.results = ResultDelivery(self.store, self.evidence, self.access, budget=BUDGET)
        self.service = ChannelService(self.app, self.results)

    @staticmethod
    def app_readiness() -> Any:
        from xiaowei.storage import Readiness

        return Readiness()

    def group(
        self,
        message: str,
        request_id: str,
        actor: str = A,
        *,
        group: GroupScope = GROUP,
        mode: str = "query",
    ) -> InboundRequest:
        return InboundRequest(
            channel="feishu",
            channel_request_id=request_id,
            subject_id=actor,
            conversation_id=group.chat_id,
            mode=mode,  # type: ignore[arg-type]
            message=message,
            received_at=WHEN,
            group=group,
        )

    def personal(
        self,
        message: str,
        request_id: str,
        subject: str,
        channel: str,
        conversation: str,
        *,
        mode: str = "query",
    ) -> InboundRequest:
        return InboundRequest(
            channel=channel,  # type: ignore[arg-type]
            channel_request_id=request_id,
            subject_id=subject,
            conversation_id=conversation,
            mode=mode,  # type: ignore[arg-type]
            message=message,
            received_at=WHEN,
        )

    async def run(self, inbound: InboundRequest) -> RequestRecord:
        return await self.service.process(await self.service.accept(inbound))

    def model_calls(self, message: str) -> int:
        return len(self.scripts.calls.get(message, []))

    async def rows(self, sql: str, **params: Any) -> list[tuple[Any, ...]]:
        async with self.engine.connect() as conn:
            return [tuple(r) for r in await conn.execute(text(sql), params)]

    async def evidence_of(self, session_id: str) -> list[tuple[Any, ...]]:
        return await self.rows(
            "SELECT evidence_id, owner_kind, owner_id, subject_id FROM xiaowei_evidence"
            " WHERE session_id = :s ORDER BY recorded_at, evidence_id",
            s=session_id,
        )


def group_ref(request_id: str, actor: str = A) -> RequestRef:
    return RequestRef(
        channel="feishu", subject_id=actor, conversation_id=CHAT, request_id=request_id, group=GROUP
    )


@pytest.fixture
async def env(postgres_url: URL, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), FAKE_MODEL_KEY)
    async with ready_engine(postgres_url) as engine:
        clock, members = Clock(), Members()
        access = StaticAccess(
            AccessConfig(policy_version="p1", grants={"alice": TOOLS, A: TOOLS}),
            frozenset({TARGET}),
            GroupAccess(scope=GROUP, tools=TOOLS, members=members),
        )
        evidence = EvidenceStore(
            engine, catalog(), authorize=access.authorize, clock=clock, retention_seconds=3600
        )
        adapter, scripts = RecordingAdapter(), Scripts()
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
            yield Env(engine, clock, members, access, adapter, scripts, evidence, app)


async def a_then_b(env: Env) -> tuple[RequestRecord, RequestRecord, str]:
    """A 在群里查询并留下证据；B 随后在同一群会话中追问并新查一次。返回两条记录与 A 的证据。"""
    first = env.scripts.add("A：东区订单？", tool_call("order_total", region="east"), cite())
    done_a = await env.run(env.group(first, "om_a"))
    assert done_a.state == "completed" and done_a.answer is not None
    (evidence_a,) = done_a.answer.evidence_ids
    second = env.scripts.add(
        "B：和西区比呢？", tool_call("order_total", region="west"), cite("西区较低")
    )
    done_b = await env.run(env.group(second, "om_b", B))
    assert done_b.state == "completed" and done_b.answer is not None
    return done_a, done_b, evidence_a


# ---- 共享归属与采集者 ----------------------------------------------------------------------


async def test_group_members_share_one_session_and_facts_with_their_own_actor(env: Env) -> None:
    done_a, done_b, evidence_a = await a_then_b(env)
    group = GROUP.owner
    assert done_a.owner == done_b.owner == group
    assert (done_a.subject_id, done_b.subject_id) == (A, B)
    assert done_a.session_id == done_b.session_id
    # B 的回答引用了 A 采集的事实（经真 PolicySession 回放）与 B 本轮的新事实。
    assert done_b.answer is not None and evidence_a in done_b.answer.evidence_ids
    rows = await env.evidence_of(done_a.session_id)
    collectors = {evidence: (kind, owner, actor) for evidence, kind, owner, actor in rows}
    assert collectors[evidence_a] == ("group", group.id, A)
    assert sorted(collectors.values()) == [("group", group.id, A), ("group", group.id, B)]
    # 会话元数据与映射只记群 owner，不记任何个人。
    assert await env.rows(
        "SELECT owner_kind, owner_id FROM xiaowei_session WHERE session_id = :s",
        s=done_a.session_id,
    ) == [("group", group.id)]
    assert await env.rows("SELECT owner_kind, owner_id FROM xiaowei_channel_session") == [
        ("group", group.id)
    ]


async def test_reading_a_shared_fact_rechecks_the_current_reader(env: Env) -> None:
    _, done_b, evidence_a = await a_then_b(env)
    reader = RunContext(
        identity=Identity(
            subject_id=B,
            session_id=done_b.session_id,
            turn_id=done_b.turn_id,
            channel="feishu",
            owner=GROUP.owner,
        ),
        target_scope=frozenset({TARGET}),
        tool_scope=frozenset(),
        budget=BUDGET,
    )
    assert await env.evidence.project(evidence_a, reader, "session")
    # 群共享策略撤下该工具后，B 的读取按当前授权拒绝，与采集者当时是否获准无关。
    env.access._group = GroupAccess(scope=GROUP, tools=frozenset({QUERY_TOOL}), members=env.members)
    with pytest.raises(EvidenceUnavailableError):
        await env.evidence.project(evidence_a, reader, "session")


async def test_follow_ups_are_authorized_for_the_current_actor_not_the_collector(
    env: Env,
) -> None:
    _, _, evidence_a = await a_then_b(env)
    env.members.current = {B}  # 采集者 A 离群，B 仍在
    follow = env.scripts.add("B：再看一遍东区", lambda call: answer([evidence_a]))
    record = await env.run(env.group(follow, "om_b2", B))
    assert record.state == "completed" and record.subject_id == B
    env.members.current = {A}  # 反过来：B 离群，A 仍在
    denied = env.scripts.add("B：还能看吗", lambda call: answer([evidence_a]))
    record = await env.run(env.group(denied, "om_b3", B))
    assert (record.state, record.failure_code) == ("failed", "access_denied")
    assert env.model_calls(denied) == 0


async def test_group_facts_stay_in_the_group_session(env: Env) -> None:
    done_a, _, evidence_a = await a_then_b(env)
    # 同一人（个人 subject 与群 actor 同名）经私聊、Web，以及另一个群或租户，都读不到群证据。
    owners = [
        ("feishu", Owner(kind="personal", id=A), A),
        ("web", Owner(kind="personal", id=A), A),
        ("feishu", GroupScope(app_id=APP, tenant_key=TENANT, chat_id="oc_other").owner, A),
        ("feishu", GroupScope(app_id=APP, tenant_key="tenant-2", chat_id=CHAT).owner, A),
    ]
    for channel, owner, actor in owners:
        ctx = RunContext(
            identity=Identity(
                subject_id=actor,
                session_id=done_a.session_id,
                turn_id=done_a.turn_id,
                channel=channel,  # type: ignore[arg-type]
                owner=owner,
            ),
            target_scope=frozenset({TARGET}),
            tool_scope=frozenset(),
            budget=BUDGET,
        )
        with pytest.raises(EvidenceUnavailableError):
            await env.evidence.project(evidence_a, ctx, "model")
    # 个人渠道引用群证据的回答不交付，模型之外不触发任何业务 I/O。
    calls = len(env.adapter.calls)
    for channel, conversation in (("feishu", "oc_dm"), ("web", "cookie-1")):
        message = env.scripts.add(
            f"{channel}：引用群里的结果", lambda call, e=evidence_a: answer([e])
        )
        record = await env.run(env.personal(message, f"p-{channel}", A, channel, conversation))
        assert (record.state, record.failure_code) == ("failed", "evidence_failed")
    assert len(env.adapter.calls) == calls


async def test_a_new_generation_does_not_read_the_previous_group_facts(env: Env) -> None:
    done_a, _, evidence_a = await a_then_b(env)
    fresh = await env.service.new_session("feishu", B, CHAT, group=GROUP)
    assert fresh.owner == GROUP.owner and fresh.session_id != done_a.session_id
    message = env.scripts.add("新会话里引用旧结果", lambda call: answer([evidence_a]))
    record = await env.run(env.group(message, "om_new"))
    assert record.session_id == fresh.session_id
    assert (record.state, record.failure_code) == ("failed", "evidence_failed")


async def test_group_session_is_not_claimed_by_other_owners_or_bindings(env: Env) -> None:
    """真 PolicySession：群会话只按群 owner 与原运行绑定打开；个人或其他群身份、换绑定都拒绝。

    历史只有澄清（没有证据）：证据层的归属检查帮不上忙，会话归属是唯一的边界。
    """
    message = env.scripts.add("A：看一下那个表", clarify("请说明是哪个集群的哪张表"))
    done_a = await env.run(env.group(message, "om_a"))
    assert done_a.state == "completed"
    ((binding,),) = await env.rows(
        "SELECT profile_fingerprint FROM xiaowei_session WHERE session_id = :s",
        s=done_a.session_id,
    )

    def session(owner: Owner, fingerprint: str = binding) -> PolicySession:
        ctx = RunContext(
            identity=Identity(
                subject_id=A,
                session_id=done_a.session_id,
                turn_id="t-probe",
                channel="feishu",
                owner=owner,
            ),
            target_scope=frozenset({TARGET}),
            tool_scope=frozenset(),
            budget=BUDGET,
        )
        return PolicySession(
            SQLAlchemySession(done_a.session_id, engine=env.engine),
            ctx,
            env.evidence,
            fingerprint,
            app_config().session_limits,
            input_policy=SessionInputPolicy(max_bytes=200),
            engine=env.engine,
            clock=env.clock,
        )

    assert await session(GROUP.owner).get_items()  # 成功对照：同群、原绑定可回放
    other = GroupScope(app_id=APP, tenant_key=TENANT, chat_id="oc_other").owner
    for owner in (Owner(kind="personal", id=A), other):
        with pytest.raises(SessionUnavailableError):
            await session(owner).get_items()
    with pytest.raises(SessionUnavailableError):
        await session(GROUP.owner, "changed-binding").get_items()


def test_group_policy_authorizes_only_its_own_group_and_never_personal_use() -> None:
    members = Members()
    access = StaticAccess(
        AccessConfig(policy_version="p1", grants={"alice": TOOLS}),
        frozenset({TARGET}),
        GroupAccess(scope=GROUP, tools=frozenset({TOTAL_TOOL}), members=members),
    )

    def who(owner: Owner, actor: str = A) -> Identity:
        return Identity(
            subject_id=actor, session_id="s", turn_id="t", channel="feishu", owner=owner
        )

    async def check() -> list[bool]:
        other = GroupScope(app_id=APP, tenant_key=TENANT, chat_id="oc_other").owner
        return [
            await access.authorize(who(GROUP.owner), TARGET, TOTAL_TOOL),
            await access.authorize(who(GROUP.owner), TARGET, QUERY_TOOL),
            await access.authorize(who(GROUP.owner), "other-target", TOTAL_TOOL),
            await access.authorize(who(other), TARGET, TOTAL_TOOL),
            await access.authorize(who(Owner(kind="personal", id=A)), TARGET, TOTAL_TOOL),
            await access.authorize(who(Owner(kind="personal", id="alice"), A), TARGET, TOTAL_TOOL),
        ]

    import asyncio

    assert asyncio.run(check()) == [True, False, False, False, False, False]
    assert members.checks == []


async def test_personal_records_do_not_mix_into_the_group(env: Env) -> None:
    # 私聊里同一聊天编号、同一消息编号的个人请求，与群请求各自独立。
    personal = env.scripts.add("私聊：东区？", tool_call("order_total", region="east"), cite())
    mine = await env.run(env.personal(personal, "om_same", A, "feishu", CHAT))
    assert mine.owner == Owner(kind="personal", id=A) and mine.answer is not None
    (personal_evidence,) = mine.answer.evidence_ids
    message = env.scripts.add("群里：引用私聊结果", lambda call: answer([personal_evidence]))
    record = await env.run(env.group(message, "om_same"))
    assert record.owner == GROUP.owner and record.session_id != mine.session_id
    assert (record.state, record.failure_code) == ("failed", "evidence_failed")


# ---- 去重、伪造与回复目的地 ----------------------------------------------------------------


async def test_group_request_binds_its_reply_target_and_rejects_changed_events(env: Env) -> None:
    message = env.scripts.add("东区？", tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.group(message, "om_1"))
    record = receipt.record
    assert (record.reply_chat_id, record.reply_message_id) == (CHAT, "om_1")
    stored = await env.rows("SELECT reply_chat_id, reply_message_id FROM xiaowei_request")
    assert stored == [(CHAT, "om_1")]
    again = await env.service.accept(env.group(message, "om_1"))
    assert not again.created and again.record.turn_id == record.turn_id
    # 同一消息换发起人、换正文都是冲突：不能借另一个人的键空间把同一消息运行两次。
    for changed in (env.group(message, "om_1", B), env.group("东区？改一下", "om_1")):
        with pytest.raises(RequestConflictError):
            await env.service.accept(changed)
    assert len(await env.rows("SELECT 1 FROM xiaowei_request")) == 1
    assert env.model_calls(message) == 0


def test_group_envelopes_cannot_point_elsewhere() -> None:
    base = {
        "channel": "feishu",
        "channel_request_id": "om_1",
        "subject_id": A,
        "conversation_id": CHAT,
        "mode": "query",
        "message": "问题",
        "received_at": WHEN,
        "group": GROUP,
    }
    for forged in ({"conversation_id": "oc_elsewhere"}, {"channel": "web"}):
        with pytest.raises(ValidationError):
            InboundRequest(**{**base, **forged})
        with pytest.raises(ValidationError):
            RequestRef(
                **{
                    "channel": "feishu",
                    "subject_id": A,
                    "conversation_id": CHAT,
                    "request_id": "om_1",
                    "group": GROUP,
                    **forged,
                }
            )


async def test_receipts_belong_to_the_original_actor(env: Env) -> None:
    await a_then_b(env)
    outbox = Outbox()
    # 同群另一成员、个人身份或省略群都不能借用 A 的请求读取或发送。
    for ref in (
        group_ref("om_a", B),
        RequestRef(channel="feishu", subject_id=A, conversation_id=CHAT, request_id="om_a"),
    ):
        with pytest.raises((RequestUnavailableError, AccessDeniedError)):
            await env.results.send(ref, outbox)
        with pytest.raises((RequestUnavailableError, AccessDeniedError)):
            await env.results.view(ref)
    assert outbox.sent == []
    assert await env.results.send(group_ref("om_a"), outbox) == "sent"
    assert len(outbox.sent) == 1


async def test_unconfigured_groups_and_tenants_are_rejected_before_any_state(env: Env) -> None:
    others = (
        GroupScope(app_id=APP, tenant_key=TENANT, chat_id="oc_other"),
        GroupScope(app_id=APP, tenant_key="tenant-2", chat_id=CHAT),
        GroupScope(app_id="cli_other", tenant_key=TENANT, chat_id=CHAT),
    )
    message = env.scripts.add("别的群", tool_call("order_total", region="east"), cite())
    for other in others:
        with pytest.raises(AccessDeniedError):
            await env.service.accept(env.group(message, "om_x", group=other))
    assert await env.rows("SELECT 1 FROM xiaowei_request") == []
    assert env.model_calls(message) == 0 and env.adapter.calls == []


# ---- 当前成员资格 --------------------------------------------------------------------------


async def test_accepting_does_not_query_the_member_directory(env: Env) -> None:
    message = env.scripts.add("东区？", tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.group(message, "om_1"))
    assert env.members.checks == []
    assert not hasattr(receipt, "context")  # 接受不产生执行 context，也就不赋予查询权
    record = await env.service.process(receipt)
    assert record.state == "completed"
    assert env.members.checks == [(CHAT, A)]  # 开始运行时查一次；同轮工具复核不再查


@pytest.mark.parametrize("change", ["left", "error", "unknown"])
async def test_membership_is_rechecked_when_the_turn_starts(env: Env, change: str) -> None:
    message = env.scripts.add("东区？", tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.group(message, "om_1"))
    if change == "left":
        env.members.current.discard(A)  # 排队后离群
    else:
        env.members.mode = change
    record = await env.service.process(receipt)
    assert (record.state, record.failure_code) == ("failed", "access_denied")
    assert env.model_calls(message) == 0 and env.adapter.calls == []
    outbox = Outbox()
    env.members.mode, env.members.current = "ok", {A, B}
    assert await env.results.send(group_ref("om_1"), outbox) == "sent"
    assert outbox.sent[0].evidence_ids == () and "群成员" in outbox.sent[0].content


async def test_membership_is_rechecked_before_the_final_send(env: Env) -> None:
    message = env.scripts.add("东区？", tool_call("order_total", region="east"), cite())
    record = await env.run(env.group(message, "om_1"))
    assert record.state == "completed"
    env.members.current.discard(A)
    outbox = Outbox()
    with pytest.raises(AccessDeniedError):
        await env.results.send(group_ref("om_1"), outbox)
    assert outbox.sent == []
    assert await env.rows("SELECT delivery FROM xiaowei_request") == [("pending",)]
    assert env.members.checks[-1] == (CHAT, A)


async def test_persistently_unconfirmed_members_receive_nothing(env: Env) -> None:
    """失败回执同样先查成员：离群或目录不可确认时不发送、不取得投递权，请求保持待投递。"""
    message = env.scripts.add("东区？", tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.group(message, "om_1"))
    env.members.current.discard(A)
    record = await env.service.process(receipt)
    assert (record.state, record.failure_code) == ("failed", "access_denied")
    outbox = Outbox()
    for mode in ("ok", "error", "unknown"):
        env.members.mode = mode
        with pytest.raises(AccessDeniedError):
            await env.results.send(group_ref("om_1"), outbox)
        with pytest.raises(AccessDeniedError):
            await env.results.view(group_ref("om_1"))
    assert outbox.sent == []
    assert await env.rows("SELECT state, delivery, delivery_attempt FROM xiaowei_request") == [
        ("failed", "pending", None)
    ]


async def _turns(env: Env, session_id: str) -> int:
    rows = await env.rows("SELECT count(*) FROM agent_messages WHERE session_id = :s", s=session_id)
    return int(rows[0][0])


async def _request_state(env: Env, record: RequestRecord) -> tuple[Any, ...]:
    (row,) = await env.rows(
        "SELECT state, failure_code, session_id, mode FROM xiaowei_request WHERE request_key = :k",
        k=record.request_key,
    )
    return row


_GROUP_TAMPERING = {
    "message": lambda r, other: replace(r, message=other),
    "mode": lambda r, other: replace(r, record=replace(r.record, mode="query")),
    "session": lambda r, other: replace(r, record=replace(r.record, session_id="s-elsewhere")),
    "reply_message": lambda r, other: replace(
        r, record=replace(r.record, reply_message_id="om_elsewhere")
    ),
    "reply_chat": lambda r, other: replace(r, record=replace(r.record, reply_chat_id="oc_other")),
    "no_group": lambda r, other: replace(r, group=None),
    "other_group": lambda r, other: replace(
        r, group=GroupScope(app_id=APP, tenant_key=TENANT, chat_id="oc_other")
    ),
    "combined": lambda r, other: replace(
        r,
        message=other,
        record=replace(r.record, mode="query", session_id="s-elsewhere"),
    ),
}


@pytest.mark.parametrize("change", sorted(_GROUP_TAMPERING))
async def test_a_group_run_is_bound_to_the_stored_request(env: Env, change: str) -> None:
    """执行输入以数据库中的请求为准：正文、用途、会话、回复目的地或群范围不符都在 Runner 前
    记为 access_denied，模型、Adapter 与 SDK Session 均不增加；请求行保持原会话与用途。"""
    await a_then_b(env)
    diagnose = env.scripts.add("B：诊断东区", tool_call("run_query", region="east"), cite())
    other = env.scripts.add("B：换个问题", tool_call("run_query", region="west"), cite())
    receipt = await env.service.accept(env.group(diagnose, "om_b2", B, mode="diagnose"))
    session = receipt.record.session_id
    before = await _turns(env, session)
    record = await env.service.process(_GROUP_TAMPERING[change](receipt, other))
    assert (record.state, record.failure_code) == ("failed", "access_denied")
    assert record.session_id == session and record.mode == "diagnose"  # 返回数据库中的记录
    assert env.model_calls(diagnose) == env.model_calls(other) == 0
    assert len(env.adapter.calls) == 2  # 只有 a_then_b 的两次
    assert await _turns(env, session) == before and await _turns(env, "s-elsewhere") == 0
    assert await _request_state(env, receipt.record) == (
        "failed",
        "access_denied",
        session,
        "diagnose",
    )


async def test_a_group_request_is_never_run_under_a_personal_grant(env: Env) -> None:
    """A 同时有个人授权：去掉群范围的群请求也不能改按个人授权运行（群请求以记录的 owner 判断）。"""
    message = env.scripts.add("A：东区？", tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.group(message, "om_a"))
    env.members.current = set()  # A 已离群，个人授权仍在
    record = await env.service.process(replace(receipt, group=None))
    assert (record.state, record.failure_code) == ("failed", "access_denied")
    assert env.model_calls(message) == 0 and env.adapter.calls == []
    assert await _turns(env, receipt.record.session_id) == 0


@pytest.mark.parametrize("change", ["actor", "turn"])
async def test_a_receipt_for_another_identity_changes_no_request(env: Env, change: str) -> None:
    """记录的身份（发起人、轮次）与数据库中的请求不符：不运行，也不改动任何请求。"""
    message = env.scripts.add("B：东区？", tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.group(message, "om_b", B))
    update = {"subject_id": A} if change == "actor" else {"turn_id": "turn-x"}
    with pytest.raises(RequestUnavailableError):
        await env.service.process(replace(receipt, record=replace(receipt.record, **update)))
    assert env.model_calls(message) == 0 and env.adapter.calls == []
    assert (await _request_state(env, receipt.record))[:2] == ("accepted", None)
    # 原样的 receipt 照常运行一次；重复事件不再运行。
    assert (await env.service.process(receipt)).state == "completed"
    again = await env.service.accept(env.group(message, "om_b", B))
    assert not again.created and (await env.service.process(again)).state == "completed"
    assert len(env.adapter.calls) == 1


@pytest.mark.parametrize("change", ["mode", "message", "session", "group", "policy"])
async def test_a_personal_run_is_bound_to_the_stored_request(
    env: Env, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    diagnose = env.scripts.add("个人：诊断东区", tool_call("run_query", region="east"), cite())
    other = env.scripts.add("个人：换个问题", tool_call("run_query", region="west"), cite())
    inbound = env.personal(diagnose, "p1", "alice", "web", "cookie-1", mode="diagnose")
    receipt = await env.service.accept(inbound)
    session = receipt.record.session_id
    if change == "mode":
        receipt = replace(receipt, record=replace(receipt.record, mode="query"))
    elif change == "message":
        receipt = replace(receipt, message=other)
    elif change == "session":
        receipt = replace(receipt, record=replace(receipt.record, session_id="s-elsewhere"))
    elif change == "group":
        receipt = replace(receipt, group=GROUP)
    else:  # 接受后数据策略已变化：按新策略不能证明这是同一请求
        original = env.access.resolve

        async def changed(channel: Any, subject_id: str) -> AccessDecision | None:
            decision = await original(channel, subject_id)
            assert decision is not None
            return decision.model_copy(update={"policy_version": "p2"})

        monkeypatch.setattr(env.access, "resolve", changed)
    record = await env.service.process(receipt)
    assert (record.state, record.failure_code) == ("failed", "access_denied")
    assert env.model_calls(diagnose) == env.model_calls(other) == 0 and env.adapter.calls == []
    assert await _turns(env, session) == 0 and await _turns(env, "s-elsewhere") == 0
    assert (record.session_id, record.mode) == (session, "diagnose")


async def test_a_personal_decision_must_be_for_the_requesting_subject(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """授权来源把 mallory 解析成一份自洽的 alice 决策时，接受与结果读取都拒绝，不落库、不调用。"""
    original = env.access.resolve

    async def impersonate(channel: Any, subject_id: str) -> AccessDecision | None:
        return await original(channel, "alice" if subject_id == "mallory" else subject_id)

    monkeypatch.setattr(env.access, "resolve", impersonate)
    message = env.scripts.add("冒充 alice", tool_call("order_total", region="east"), cite())
    with pytest.raises(AccessDeniedError):
        await env.service.accept(env.personal(message, "m1", "mallory", "web", "cookie-1"))
    assert await env.rows("SELECT 1 FROM xiaowei_request") == []
    assert env.model_calls(message) == 0 and env.adapter.calls == []
    # alice 本人的请求照常；mallory 不能借解析结果读取或发送 alice 的结果。
    own = env.scripts.add("alice：东区？", tool_call("order_total", region="east"), cite())
    assert (await env.run(env.personal(own, "a1", "alice", "web", "cookie-1"))).state == "completed"
    outbox = Outbox()
    ref = RequestRef(
        channel="web", subject_id="mallory", conversation_id="cookie-1", request_id="a1"
    )
    with pytest.raises(AccessDeniedError):
        await env.results.send(ref, outbox)
    with pytest.raises(AccessDeniedError):
        await env.results.view(ref)
    assert outbox.sent == []


async def test_group_grants_do_not_open_private_chat_or_web(env: Env) -> None:
    message = env.scripts.add("私聊试试", tool_call("order_total", region="east"), cite())
    for channel in ("feishu", "web"):
        with pytest.raises(AccessDeniedError):
            await env.service.accept(env.personal(message, f"r-{channel}", B, channel, "c"))
    assert env.model_calls(message) == 0 and env.members.checks == []
    # 个人授权照常：同名的个人 subject 的历史与重发不受群配置影响。
    personal = env.scripts.add("个人：东区？", tool_call("order_total", region="east"), cite())
    record = await env.run(env.personal(personal, "p1", "alice", "web", "cookie-1"))
    assert record.state == "completed" and record.owner == Owner(kind="personal", id="alice")


# ---- 恢复与升级 ----------------------------------------------------------------------------


async def test_recovery_interrupts_group_requests_and_closes_the_group_session(env: Env) -> None:
    message = env.scripts.add("东区？", tool_call("order_total", region="east"), cite())
    receipt = await env.service.accept(env.group(message, "om_1"))
    await env.store.start(receipt.record, message=receipt.message, policy_version="p1")
    async with hold_instance_lock(env.engine, env.store.readiness) as lock:
        report = await env.store.recover(lock)
    assert report.interrupted == 1
    assert await env.rows(
        "SELECT owner_kind, owner_id, state FROM xiaowei_session WHERE session_id = :s",
        s=receipt.record.session_id,
    ) == [("group", GROUP.owner.id, "closed")]


def _digest(*parts: str) -> str:
    body = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    return hmac.new(KEY.encode(), body.encode(), hashlib.sha256).hexdigest()


async def test_v4_personal_records_keep_their_keys_after_the_upgrade(env: Env) -> None:
    """v4 的个人请求键与摘要按原格式保存：升级到 v5 后同一请求仍是重复请求，不重新运行。"""
    from tests.sdk_core.test_storage_v2 import _install_v1

    async with open_engine(secret(env.engine.url)) as legacy:
        async with legacy.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await _install_v1(legacy, through="004_evidence_dependencies.sql")
        conversation = _digest("conversation", "web", "alice", "cookie-1")
        request_key = _digest("request", "web", "alice", conversation, "r1")
        digest = _digest("message", "web", "alice", conversation, "query", "p1", "旧问题")
        async with legacy.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO xiaowei_channel_session VALUES ('web', 'alice', :c, 1, 's1',"
                    " 'current', now(), now() + interval '1 day')"
                ),
                {"c": conversation},
            )
            await conn.execute(
                text(
                    "INSERT INTO xiaowei_request (channel, request_key, subject_id,"
                    " conversation_key, session_id, turn_id, mode, message_digest, state, answer,"
                    " failure_code, delivery, created_at, updated_at, expires_at) VALUES ('web',"
                    " :k, 'alice', :c, 's1', 't1', 'query', :d, 'failed', NULL, 'busy', 'sent',"
                    " now(), now(), now() + interval '1 day')"
                ),
                {"k": request_key, "c": conversation, "d": digest},
            )
        assert await upgrade_storage(legacy) == 5
    env.clock.now = datetime.now(UTC)
    again = await env.service.accept(env.personal("旧问题", "r1", "alice", "web", "cookie-1"))
    assert not again.created
    assert (again.record.turn_id, again.record.owner) == ("t1", Owner(kind="personal", id="alice"))
    current = await env.store.current_session("web", "alice", "cookie-1")
    assert current.session_id == "s1"
