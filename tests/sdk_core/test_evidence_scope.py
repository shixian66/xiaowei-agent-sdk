"""P2.5 Task 2 审查修订：已保存的 StarRocks 事实在每个出口按当前数据库权限三态复核（离线）。

正式装配 ``runtime.open_runtime``（实例锁、启动恢复、结构快照、受治理工具、唯一授权来源、Evidence、
Application、``ChannelService``）+ 显式重发 ``runtime.resend`` + 隔离的真实 PostgreSQL。只替换最底层
I/O：模型是按消息分派的 HTTP 脚本，StarRocks 是 recording 驱动替身（每条新连接按当前的权限与
可达性应答），飞书发送是传入的 ``transmit`` 或 SDK 公开面替身。

覆盖的出口：同会话追问（PolicySession 回放）、首次发送、历史读取、显式重发；工具记录（执行后
复核）、最终回答与 Session 提交在同一轮内经过同一复核。替身能证明代码在这些出口发出的只有零行
探测与元数据读取、拒绝时模型与业务 SQL 为 0、三种结论的分支与 Session 状态；不能证明 StarRocks
的权限与元数据实际行为（见 ``tests/p1b/test_starrocks_real.py`` 的依赖复核用例）。
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from agents.testing import function_call
from asyncmy.errors import OperationalError, ProgrammingError
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import URL
from tests.p1b.test_starrocks_adapter import (
    SCHEMA_TABLES,
    SESSION_READ,
    SESSION_SET,
    SESSION_VALUES,
    Driver,
    FakeConnection,
    Result,
    schema_results,
)
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.synthetic_tools import Clock
from tests.sdk_core.test_app import Scripts, cite, clarify, tool_call
from tests.sdk_core.test_feishu import FakeChannel
from tests.sdk_core.test_runtime import (
    AUDIT_CONFIG,
    AUDIT_SOURCE,
    BUDGET_CONFIG,
    DB_ENV,
    FEISHU_ENV,
    KEY_ENV,
    MODEL_ENV,
    PLAN,
    audit_record,
    cluster_config,
    feishu_config,
    free_port,
    serve_config,
)
from tests.sdk_core.test_runtime import Env as RuntimeEnv
from tests.sdk_core.test_starrocks_tools import LAYOUT_COLUMNS

from xiaowei import runtime
from xiaowei.channel import (
    InboundRequest,
    RequestRef,
    ResultUnavailableError,
    ResultUnverifiableError,
)
from xiaowei.channel_store import RequestRecord, SendOutcome
from xiaowei.models import Delivery
from xiaowei.starrocks import probe_sql
from xiaowei.starrocks_tools import AUDIT_TOOLS, QUERY_TOOLS
from xiaowei.storage import initialize_storage, open_engine

pytestmark = pytest.mark.loopback

SALES = Result(("region", "total"), [("east", 100), ("west", 50)])
VIEW = ("shop", "v_sales")
TABLES = {**SCHEMA_TABLES, VIEW: (("region", "varchar", "YES", None),)}
CHAT = "oc_alice"
UNREACHABLE = OperationalError(2003, "Can't connect to StarRocks canary-5e1b")


@dataclass
class Database:
    """可切换权限与可达性的 StarRocks 替身：之后新建的每条连接按当前状态应答。

    ``revoked`` 中的对象零行探测为 5203（``information_schema`` 仍可见，符合 Task 0 实测）；
    ``down`` 时建立连接失败。启动时的结构快照按启动那一刻的状态采集，之后不刷新（默认刷新间隔
    60 秒，用例在此之内完成）。
    """

    query: Result = field(default_factory=lambda: SALES)
    tables: dict[tuple[str, str], Any] = field(default_factory=lambda: TABLES)
    revoked: set[tuple[str, str]] = field(default_factory=set)
    overrides: dict[str, Result | BaseException] = field(default_factory=dict)
    drv: Driver = field(init=False)

    def __post_init__(self) -> None:
        def make() -> FakeConnection:
            results: dict[str, Any] = {
                SESSION_SET: Result(()),
                SESSION_READ: Result(("q", "m", "t", "s"), [SESSION_VALUES]),
                **schema_results(self.tables, frozenset(self.revoked), views=frozenset({VIEW})),
                **self.overrides,
                "*": self.query,
            }
            for key in self.revoked:  # 撤权最后生效，覆盖审计源等额外对象的探测结果
                results[probe_sql(*key)] = ProgrammingError(5203, "Access denied canary-5e1b")
            return FakeConnection(results=results)

        self.drv = Driver(make)

    @property
    def down(self) -> bool:
        return self.drv.connect_error is not None

    @down.setter
    def down(self, value: bool) -> None:
        self.drv.connect_error = UNREACHABLE if value else None

    def forget(self) -> None:
        self.drv.attempts = 0
        self.drv.connections.clear()

    def business(self) -> list[str]:
        """到达驱动的业务语句：不含会话设置与回读、结构快照读取、零行探测与依赖元数据读取。"""
        internal = frozenset(schema_results(self.tables, views=frozenset({VIEW})))
        internal |= set(self.overrides)
        return [
            sql
            for conn in self.drv.connections
            for sql, _ in conn.executed
            if not sql.startswith(("SET ", "SELECT @@", "SELECT 1 FROM `")) and sql not in internal
        ]

    def probes(self) -> list[str]:
        return [
            sql
            for conn in self.drv.connections
            for sql, _ in conn.executed
            if sql.startswith("SELECT 1 FROM `")
        ]


@dataclass
class Env:
    url: URL
    port: int = 18_501
    scripts: Scripts = field(default_factory=Scripts)
    clock: Clock = field(default_factory=Clock)
    db: Database = field(default_factory=Database)
    drivers: dict[str, Driver] = field(default_factory=dict)
    """按目标指定驱动；未指定的目标都连到 ``db``。"""

    def config(self, **overrides: Any) -> runtime.ServeConfig:
        return runtime.ServeConfig.model_validate(
            serve_config(self.port, feishu=feishu_config(), **overrides)
        )

    def connect(self, config: runtime.ServeConfig) -> dict[str, Driver]:
        return {t.target_id: self.drivers.get(t.target_id, self.db.drv) for t in config.targets}

    @asynccontextmanager
    async def opened(self, config: runtime.ServeConfig) -> AsyncIterator[runtime.Runtime]:
        async with runtime.open_runtime(
            config,
            clock=self.clock,
            model_transport=self.scripts.transport(),
            starrocks_connect=self.connect(config),
        ) as rt:
            self.db.forget()  # 只统计启动之后的 I/O
            yield rt

    async def turn(self, rt: runtime.Runtime, message: str, request_id: str) -> RequestRecord:
        receipt = await rt.service.accept(
            InboundRequest(
                channel="feishu",
                channel_request_id=request_id,
                subject_id="alice",
                conversation_id=CHAT,
                mode="query",
                message=message,
                received_at=datetime.now(UTC),
            )
        )
        return await rt.service.process(receipt)

    async def resend(self, config: runtime.ServeConfig, request_id: str) -> tuple[Any, list[Any]]:
        channel = FakeChannel()
        result = await runtime.resend(
            config,
            subject_id="alice",
            chat_id=CHAT,
            message_id=request_id,
            clock=self.clock,
            feishu_channel=channel,
            starrocks_connect=self.connect(config),
        )
        return result, channel.sends

    async def scalar(self, sql: str, **params: Any) -> Any:
        async with open_engine(SecretStr(self.url.render_as_string(hide_password=False))) as e:
            async with e.connect() as conn:
                return await conn.scalar(text(sql), params)


@pytest.fixture
async def env(postgres_url: URL, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    monkeypatch.setenv(DB_ENV, postgres_url.render_as_string(hide_password=False))
    monkeypatch.setenv(KEY_ENV, "evidence-scope-digest-key")
    monkeypatch.setenv(MODEL_ENV, "sk-test-evidence-scope")
    monkeypatch.setenv(FEISHU_ENV, "feishu-test-secret")
    async with open_engine(SecretStr(postgres_url.render_as_string(hide_password=False))) as e:
        await initialize_storage(e)
    yield Env(postgres_url)


def ref(request_id: str) -> RequestRef:
    return RequestRef(
        channel="feishu", subject_id="alice", conversation_id=CHAT, request_id=request_id
    )


@dataclass
class Outbox:
    """首次发送的 ``transmit``：记录交给渠道的内容，按 ``outcome`` 报告发送结果。"""

    outcome: SendOutcome = "failed"  # 默认明确失败：之后可显式重发
    sent: list[Delivery] = field(default_factory=list)

    async def __call__(self, delivery: Delivery) -> SendOutcome:
        self.sent.append(delivery)
        return self.outcome


def query(message: str, sql: str = "SELECT region, total FROM sales") -> tuple[str, Any]:
    return message, tool_call("run_readonly_query", cluster=SR.target_id, sql=sql)


async def saved(
    env: Env, rt: runtime.Runtime, request_id: str, sql: str = "SELECT region, total FROM sales"
) -> RequestRecord:
    """经正式路径完成一轮查询并保存（未发送）；返回已保存的请求。"""
    message = env.scripts.add(f"查询 {request_id}", query(request_id, sql)[1], cite())
    record = await env.turn(rt, message, request_id)
    assert record.state == "completed", record.failure_code
    return record


async def followup(env: Env, rt: runtime.Runtime, request_id: str) -> tuple[RequestRecord, str]:
    """同会话追问：只引用回放的历史证据，不调用工具。"""
    message = env.scripts.add(f"追问 {request_id}", cite("延续上一轮"))
    return await env.turn(rt, message, request_id), message


async def session_state(env: Env) -> str:
    return str(await env.scalar("SELECT state FROM xiaowei_session"))


# ---- 确定撤权：所有再次交付都拒绝，且不调用模型、不重跑业务 SQL --------------------------------


async def test_revoked_table_closes_every_later_delivery_of_saved_query_facts(env: Env) -> None:
    config = env.config()
    async with env.opened(config) as rt:
        await saved(env, rt, "om_1")
        assert len(env.db.business()) == 1
        env.db.revoked = {("shop", "sales")}
        env.db.forget()

        outbox = Outbox()
        with pytest.raises(ResultUnavailableError) as first:
            await rt.service.results.send(ref("om_1"), outbox)  # 首次发送
        assert not isinstance(first.value, ResultUnverifiableError)
        assert outbox.sent == []
        assert await env.scalar("SELECT delivery FROM xiaowei_request") == "failed"

        with pytest.raises(ResultUnavailableError):
            await rt.service.results.view(ref("om_1"))  # 历史读取

        record, message = await followup(env, rt, "om_2")  # 追问：回放前拒绝
        assert (record.state, record.failure_code) == ("failed", "session_failed")
        assert message not in env.scripts.calls
        assert env.db.business() == []
        assert env.db.probes() and set(env.db.probes()) == {probe_sql("shop", "sales")}

    env.db.forget()
    with pytest.raises(ResultUnavailableError):
        await env.resend(config, "om_1")
    assert env.db.business() == []
    assert len(env.scripts.calls["查询 om_1"]) == 2  # 只有原轮次的两次模型调用


async def test_unchanged_access_keeps_followup_history_and_resend(env: Env) -> None:
    """成功对照：权限与对象版本都未变，旧事实照常回放、读取与重发，不重跑业务 SQL。"""
    config = env.config()
    async with env.opened(config) as rt:
        await saved(env, rt, "om_1")
        outbox = Outbox()
        assert await rt.service.results.send(ref("om_1"), outbox) == "failed"
        (delivery,) = outbox.sent
        assert "| east | 100 |" in delivery.content  # 飞书交付是纯文本
        env.db.forget()

        view = await rt.service.results.view(ref("om_1"))
        assert view.delivery is not None and view.delivery.content == delivery.content
        record, message = await followup(env, rt, "om_2")
        assert record.state == "completed"
        assert len(env.scripts.calls[message]) == 1
        assert env.db.business() == []
        # 同一批次（一条连接）内同一对象只探测一次。
        for conn in env.db.drv.connections:
            probed = [sql for sql, _ in conn.executed if sql.startswith("SELECT 1 FROM `")]
            assert len(probed) == len(set(probed))

    env.db.forget()
    result, sends = await env.resend(config, "om_1")
    assert result == "sent" and len(sends) == 1
    assert env.db.business() == []
    assert probe_sql("shop", "sales") in env.db.probes()


# ---- 暂不可验证：只阻断本次交付，Evidence 与 Session 保留，恢复后可继续 --------------------------


async def test_unverifiable_access_blocks_only_this_delivery(env: Env) -> None:
    config = env.config()
    async with env.opened(config) as rt:
        await saved(env, rt, "om_1")
        env.db.down = True

        outbox = Outbox()
        with pytest.raises(ResultUnverifiableError):
            await rt.service.results.send(ref("om_1"), outbox)
        assert outbox.sent == []
        assert await env.scalar("SELECT delivery FROM xiaowei_request") == "failed"
        with pytest.raises(ResultUnverifiableError):
            await rt.service.results.view(ref("om_1"))

        record, message = await followup(env, rt, "om_2")
        assert (record.state, record.failure_code) == ("failed", "scope_unverifiable")
        assert message not in env.scripts.calls
        assert await session_state(env) == "active"
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1

    with pytest.raises(ResultUnverifiableError):
        await env.resend(config, "om_1")

    env.db.down = False
    async with env.opened(config) as rt:
        view = await rt.service.results.view(ref("om_1"))
        assert view.delivery is not None and "| east | 100 |" in view.delivery.content
        record, message = await followup(env, rt, "om_3")
        assert record.state == "completed" and len(env.scripts.calls[message]) == 1
        assert env.db.business() == []
    result, sends = await env.resend(config, "om_1")
    assert result == "sent" and len(sends) == 1


async def test_unverifiable_access_at_the_end_of_a_turn_keeps_the_session(env: Env) -> None:
    """工具已执行、最终回答复核时目标断开：本轮不提交、不交付，会话仍可继续。"""
    config = env.config()
    async with env.opened(config) as rt:

        def disconnect_then_cite(call: Any) -> Any:
            env.db.down = True
            return cite()(call)

        message = env.scripts.add(
            "查询后断开",
            tool_call("run_readonly_query", cluster=SR.target_id, sql="SELECT region FROM sales"),
            disconnect_then_cite,
        )
        record = await env.turn(rt, message, "om_1")
        assert (record.state, record.failure_code) == ("failed", "scope_unverifiable")
        assert await session_state(env) == "active"
        env.db.down = False
        # 上一轮没有提交：同一会话的下一条消息照常运行。
        later = env.scripts.add("断开恢复后", clarify())
        assert (await env.turn(rt, later, "om_2")).state == "completed"
        assert await session_state(env) == "active"


# ---- 视图：本轮首次交付可用；跨轮回放、历史读取与重发一律拒绝 -----------------------------------


async def test_view_facts_are_delivered_once_and_never_replayed(env: Env) -> None:
    config = env.config()
    async with env.opened(config) as rt:
        await saved(env, rt, "om_1", sql="SELECT region FROM v_sales")
        outbox = Outbox()
        assert await rt.service.results.send(ref("om_1"), outbox) == "failed"
        (delivery,) = outbox.sent
        assert delivery.evidence_ids and "| east |" in delivery.content

        env.db.forget()
        with pytest.raises(ResultUnavailableError) as history:
            await rt.service.results.view(ref("om_1"))
        assert not isinstance(history.value, ResultUnverifiableError)
        record, message = await followup(env, rt, "om_2")
        assert (record.state, record.failure_code) == ("failed", "session_failed")
        assert message not in env.scripts.calls
        assert env.db.drv.attempts == 0  # 拒绝不依赖数据库：没有任何连接

    with pytest.raises(ResultUnavailableError):
        await env.resend(config, "om_1")
    assert env.db.drv.attempts == 0


async def test_the_same_turn_may_cite_view_facts(env: Env) -> None:
    """视图证据在产生它的这一轮内可交给模型、通过最终校验并提交（首次交付）。"""
    config = env.config()
    async with env.opened(config) as rt:
        message = env.scripts.add(
            "看视图",
            tool_call("run_readonly_query", cluster=SR.target_id, sql="SELECT region FROM v_sales"),
            cite(),
        )
        record = await env.turn(rt, message, "om_1")
        assert record.state == "completed"
        assert record.answer is not None and len(record.answer.evidence_ids) == 1


# ---- 执行计划与审计原文：同一复核 ------------------------------------------------------------


async def test_explain_facts_follow_current_access(env: Env) -> None:
    env.db.query = PLAN
    config = env.config()
    async with env.opened(config) as rt:
        message = env.scripts.add(
            "计划",
            tool_call("explain_query", cluster=SR.target_id, sql="SELECT region FROM sales"),
            cite(),
        )
        assert (await env.turn(rt, message, "om_1")).state == "completed"
        assert (await rt.service.results.view(ref("om_1"))).delivery is not None

        env.db.revoked = {("shop", "sales")}
        with pytest.raises(ResultUnavailableError):
            await rt.service.results.view(ref("om_1"))
        record, later = await followup(env, rt, "om_2")
        assert record.failure_code == "session_failed" and later not in env.scripts.calls


async def test_explain_of_an_object_revoked_since_the_refresh_is_never_delivered(
    env: Env,
) -> None:
    """快照仍认为可读、数据库已撤权：计划记录前的复核确定失效，本轮不交付。"""
    env.db.query = PLAN
    config = env.config()
    async with env.opened(config) as rt:
        env.db.revoked = {("shop", "sales")}
        message = env.scripts.add(
            "撤权后的计划",
            tool_call("explain_query", cluster=SR.target_id, sql="SELECT region FROM sales"),
            cite(),
        )
        record = await env.turn(rt, message, "om_1")
        assert record.state == "failed"
        assert len(env.scripts.calls[message]) == 1  # 工具结果没有交回模型


def audit_config(port: int) -> dict[str, Any]:
    tools = sorted(QUERY_TOOLS | AUDIT_TOOLS)
    return serve_config(
        port,
        feishu=feishu_config(),
        starrocks={**SR.model_dump(mode="json"), "audit": AUDIT_CONFIG},
        data_policy={"input": {"max_bytes": 2000}, "model_tools": tools},
        access={"policy_version": "p1", "grants": {"alice": tools}},
    )


AUDIT_TABLE = (AUDIT_CONFIG["database"], AUDIT_CONFIG["table"])


async def test_audit_rows_follow_current_access_of_their_objects(env: Env) -> None:
    env.db.query = Result(
        AUDIT_SOURCE,
        [
            audit_record("SELECT name FROM regions", "q-regions"),
            audit_record("SELECT region, total FROM sales", "q-sales"),
        ],
    )
    env.db.overrides = {probe_sql(*AUDIT_TABLE): Result(("1",))}
    config = runtime.ServeConfig.model_validate(audit_config(env.port))
    async with env.opened(config) as rt:
        env.db.revoked = {("shop", "regions")}  # 快照之后撤权：该行在交付前移除
        message = env.scripts.add(
            "慢查询",
            tool_call(
                "list_slow_queries", cluster=SR.target_id, window_minutes=60, order_by="query_time"
            ),
            cite(),
        )
        record = await env.turn(rt, message, "om_1")
        assert record.state == "completed", record.failure_code
        view = await rt.service.results.view(ref("om_1"))
        assert view.delivery is not None
        assert "q-sales" in view.delivery.content and "q-regions" not in view.delivery.content
        assert "regions" not in json.dumps(view.delivery.model_dump(mode="json"))

        env.db.revoked = {("shop", "regions"), ("shop", "sales")}
        with pytest.raises(ResultUnavailableError):
            await rt.service.results.view(ref("om_1"))

        env.db.revoked = {AUDIT_TABLE}  # 审计源本身撤权，同样失效
        with pytest.raises(ResultUnavailableError):
            await rt.service.results.view(ref("om_1"))


# ---- 保存的依赖：缺失或格式不符按确定失效处理 ---------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        None,  # 旧格式（v3 升级而来）或被删除
        '{"format":2,"objects":[]}',  # 未知格式版本
        "not json",
    ],
    ids=["缺失", "未知格式", "不是 JSON"],
)
async def test_missing_or_tampered_dependencies_are_definitely_unreadable(
    env: Env, stored: str | None
) -> None:
    config = env.config()
    async with env.opened(config) as rt:
        await saved(env, rt, "om_1")
        async with open_engine(SecretStr(env.url.render_as_string(hide_password=False))) as e:
            async with e.begin() as conn:
                await conn.execute(
                    text("UPDATE xiaowei_evidence SET dependencies = :d"), {"d": stored}
                )
        with pytest.raises(ResultUnavailableError) as refused:
            await rt.service.results.view(ref("om_1"))
        assert not isinstance(refused.value, ResultUnverifiableError)


# ---- Web：提交后的首次交付与之后的历史读取 --------------------------------------------------


async def test_web_first_delivery_and_history_follow_the_same_rules(env: Env) -> None:
    """正式 ``serve``（真实 Uvicorn）：视图事实只在提交响应中交付一次，之后读取 403，含它的会话
    不能续轮；表事实在目标暂时不可达时读取 503（不是 403），恢复后再读成功。"""
    web = RuntimeEnv(env.url, free_port(), scripts=env.scripts, clock=env.clock, drv=env.db.drv)
    async with web.running(web.config()) as served:
        await served.page()
        view_message = env.scripts.add(
            "网页看视图",
            tool_call("run_readonly_query", cluster=SR.target_id, sql="SELECT region FROM v_sales"),
            cite(),
        )
        body = (await served.turn(view_message, "query", "r1")).json()
        assert body["state"] == "completed" and body["delivery"]["facts"]
        assert (await served.client.get("/api/turns/r1")).status_code == 403

        table_message = env.scripts.add(
            "网页看表",
            tool_call("run_readonly_query", cluster=SR.target_id, sql="SELECT region FROM sales"),
            cite(),
        )
        # 同一会话的下一轮会回放这条视图事实：整段历史拒绝，要求新建会话（模型不被调用）。
        refused = (await served.turn(table_message, "query", "r2")).json()
        assert refused["state"] == "failed" and table_message not in env.scripts.calls
        origin = {"origin": str(served.client.base_url).rstrip("/")}
        assert (
            await served.client.post("/api/sessions", json={}, headers=origin)
        ).status_code == 200
        assert (await served.turn(table_message, "query", "r3")).json()["state"] == "completed"
        env.db.down = True
        unverifiable = await served.client.get("/api/turns/r3")
        assert unverifiable.status_code == 503
        assert "canary" not in unverifiable.text
        env.db.down = False
        assert (await served.client.get("/api/turns/r3")).status_code == 200
        assert await served.finish() == 0


# ---- 多目标：同一批依赖按目标各自复核 -------------------------------------------------------


async def test_every_target_in_a_batch_is_verified(env: Env) -> None:
    """一个回答引用两个集群的事实；只在第二个集群撤权，历史读取同样拒绝（不能只核对第一个）。"""
    second = Database()
    config = env.config(targets=[cluster_config("sr-a"), cluster_config("sr-b")])
    env.drivers = {"sr-b": second.drv}
    async with env.opened(config) as rt:
        second.forget()
        message = env.scripts.add(
            "两个集群",
            lambda call: [
                function_call(
                    "run_readonly_query",
                    {"cluster": t, "sql": "SELECT region FROM sales"},
                    call_id=f"q-{t}",
                )
                for t in ("sr-a", "sr-b")
            ],
            cite(),
        )
        record = await env.turn(rt, message, "om_1")
        assert record.state == "completed" and record.answer is not None
        assert len(record.answer.evidence_ids) == 2
        assert (await rt.service.results.view(ref("om_1"))).delivery is not None

        second.revoked = {("shop", "sales")}
        with pytest.raises(ResultUnavailableError):
            await rt.service.results.view(ref("om_1"))
        assert probe_sql("shop", "sales") in env.db.probes()  # 第一个集群也照常复核


async def test_one_unreachable_target_does_not_void_a_mixed_history(env: Env) -> None:
    """A/B 两个集群的事实在同一段历史中：B 暂时不可达时只阻断本次交付，Session 与两条证据都保留；
    恢复后同一会话继续，业务 SQL 不重跑。"""
    second = Database()
    config = env.config(targets=[cluster_config("sr-a"), cluster_config("sr-b")])
    env.drivers = {"sr-b": second.drv}
    async with env.opened(config) as rt:
        second.forget()
        await env.turn(rt, both_clusters(env, "两个集群 om_1"), "om_1")
        second.down = True
        record, message = await followup(env, rt, "om_2")
        assert (record.state, record.failure_code) == ("failed", "scope_unverifiable")
        assert message not in env.scripts.calls
        assert await session_state(env) == "active"
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 2
        with pytest.raises(ResultUnverifiableError):
            await rt.service.results.view(ref("om_1"))

        second.down = False
        env.db.forget()
        second.forget()
        record, message = await followup(env, rt, "om_3")
        assert record.state == "completed" and len(env.scripts.calls[message]) == 1
        assert env.db.business() == [] and second.business() == []


async def test_an_unrelated_target_does_not_affect_history(env: Env) -> None:
    """历史只有 A 集群的事实：B 撤权或不可达都不影响追问与读取，B 不被复核。"""
    second = Database(revoked={("shop", "sales")})
    second.down = True
    config = env.config(targets=[cluster_config("sr-a"), cluster_config("sr-b")])
    env.drivers = {"sr-b": second.drv}
    async with env.opened(config) as rt:
        message = env.scripts.add(
            "只看 sr-a",
            tool_call("run_readonly_query", cluster="sr-a", sql="SELECT region, total FROM sales"),
            cite(),
        )
        assert (await env.turn(rt, message, "om_1")).state == "completed"
        second.forget()
        record, _ = await followup(env, rt, "om_2")
        assert record.state == "completed"
        assert (await rt.service.results.view(ref("om_1"))).delivery is not None
        assert second.drv.attempts == 0


LAYOUT_ROW = Result(
    LAYOUT_COLUMNS, [("DUP_KEYS", "`region`", "HASH", "`region`", 8, "`region`", "")]
)
METADATA_FACTS = {
    "list_tables": ({}, SALES),
    "describe_table": ({"database": "shop", "table": "sales"}, SALES),
    "describe_table_layout": ({"database": "shop", "table": "sales"}, LAYOUT_ROW),
}


@pytest.mark.parametrize("tool", METADATA_FACTS)
async def test_revoked_metadata_facts_are_never_delivered_again(env: Env, tool: str) -> None:
    """列表、表结构与布局事实同查询事实一样：撤权后追问、历史读取与重发都拒绝，只做零行探测。"""
    arguments, result = METADATA_FACTS[tool]
    env.db.query = result
    config = env.config()
    async with env.opened(config) as rt:
        message = env.scripts.add(
            f"元数据 {tool}", tool_call(tool, cluster=SR.target_id, **arguments), cite()
        )
        assert (await env.turn(rt, message, "om_1")).state == "completed"
        outbox = Outbox()  # 首次发送明确失败：之后可显式重发
        assert await rt.service.results.send(ref("om_1"), outbox) == "failed"
        assert outbox.sent and (await rt.service.results.view(ref("om_1"))).delivery is not None
        env.db.revoked = {("shop", "sales")}
        env.db.forget()
        with pytest.raises(ResultUnavailableError) as refused:
            await rt.service.results.view(ref("om_1"))
        assert not isinstance(refused.value, ResultUnverifiableError)
        record, followed = await followup(env, rt, "om_2")
        assert (record.state, record.failure_code) == ("failed", "session_failed")
        assert followed not in env.scripts.calls
        assert env.db.business() == []
    with pytest.raises(ResultUnavailableError):
        await env.resend(config, "om_1")
    assert env.db.business() == []


# ---- 对象检查次数上限（计划 §2.2）：每次复核按批去重后计数，超限在 I/O 前暂不可用 -------------

WIDE = {("shop", f"t{i}"): (("region", "varchar", "YES", None),) for i in range(6)}
WIDE_SQL = (
    "SELECT t0.region FROM t0 JOIN t1 ON t1.region = t0.region "
    "JOIN t2 ON t2.region = t0.region JOIN t3 ON t3.region = t0.region "
    "JOIN t4 ON t4.region = t0.region JOIN t5 ON t5.region = t0.region"
)


def capped(env: Env, checks: int, **overrides: Any) -> runtime.ServeConfig:
    """每页列表 1 行（上限的下界因此为 5），对象检查上限为 ``checks``。"""
    starrocks = SR.model_dump(mode="json")
    starrocks["policy"]["max_rows"] = 1
    budget = {**BUDGET_CONFIG, "max_scope_checks": checks}
    if "targets" in overrides:
        for target in overrides["targets"]:
            target["starrocks"]["policy"]["max_rows"] = 1
        return env.config(budget=budget, **overrides)
    return env.config(starrocks=starrocks, budget=budget, **overrides)


def wide(env: Env, message: str, *clusters: str) -> str:
    """一轮在给定集群上各查一次 6 张表的 JOIN。"""
    return env.scripts.add(
        message,
        lambda call: [
            function_call("run_readonly_query", {"cluster": c, "sql": WIDE_SQL}, call_id=f"w-{c}")
            for c in clusters
        ],
        cite(),
    )


def both_clusters(env: Env, message: str) -> str:
    return env.scripts.add(
        message,
        lambda call: [
            function_call(
                "run_readonly_query",
                {"cluster": t, "sql": "SELECT region FROM sales"},
                call_id=f"q-{t}",
            )
            for t in ("sr-a", "sr-b")
        ],
        cite(),
    )


async def fresh_session(rt: runtime.Runtime) -> None:
    await rt.service.new_session("feishu", "alice", CHAT)


async def test_a_turn_succeeds_at_the_check_limit_and_stops_before_io_beyond_it(env: Env) -> None:
    env.db.tables = {**TABLES, **WIDE}
    env.db.query = Result(("region",), [("east",)])
    async with env.opened(capped(env, 1000)) as rt:
        assert (await env.turn(rt, wide(env, "量一轮", SR.target_id), "om_0")).state == "completed"
        needed = len(env.db.probes())
    # 每次复核这 6 个对象各探测一次：工具记录、写入 Session、最终校验、提交时的校验与提交前的
    # 整段回放。
    assert needed == 6 * 5

    async with env.opened(capped(env, needed)) as rt:
        await fresh_session(rt)
        assert (
            await env.turn(rt, wide(env, "恰好用完", SR.target_id), "om_1")
        ).state == "completed"
        assert len(env.db.probes()) == needed
        # 计数只属于一轮：同一进程的下一轮重新计数。
        await fresh_session(rt)
        env.db.forget()
        assert (await env.turn(rt, wide(env, "下一轮", SR.target_id), "om_2")).state == "completed"
        assert len(env.db.probes()) == needed

    async with env.opened(capped(env, needed - 1)) as rt:
        await fresh_session(rt)
        record = await env.turn(rt, wide(env, "超出一次", SR.target_id), "om_3")
        assert (record.state, record.failure_code) == ("failed", "scope_unverifiable")
        # 最后一次复核需要 6 次而只剩 5 次：在任何探测之前拒绝，已用的不退还。
        assert len(env.db.probes()) == needed - 6
        state = "SELECT state FROM xiaowei_session WHERE session_id = :s"
        assert await env.scalar(state, s=record.session_id) == "active"


async def test_history_reads_and_resends_have_their_own_limit(env: Env) -> None:
    """单独的历史读取与重发每次各自计数；同一批中同一对象只探测、计数一次。"""
    env.db.tables = {**TABLES, **WIDE}
    env.db.query = Result(("region",), [("east",)])
    async with env.opened(capped(env, 1000)) as rt:
        message = env.scripts.add(
            "两次同表",
            lambda call: [
                function_call(
                    "run_readonly_query", {"cluster": SR.target_id, "sql": WIDE_SQL}, call_id=c
                )
                for c in ("w-1", "w-2")
            ],
            cite(),
        )
        record = await env.turn(rt, message, "om_1")
        assert record.state == "completed" and record.answer is not None
        assert len(record.answer.evidence_ids) == 2
        assert await rt.service.results.send(ref("om_1"), Outbox()) == "failed"  # 之后可重发

    config = capped(env, 5)
    async with env.opened(config) as rt:
        with pytest.raises(ResultUnverifiableError):
            await rt.service.results.view(ref("om_1"))
        assert env.db.probes() == []
    with pytest.raises(ResultUnverifiableError):
        await env.resend(config, "om_1")
    assert env.db.probes() == []

    config = capped(env, 6)
    async with env.opened(config) as rt:
        assert (await rt.service.results.view(ref("om_1"))).delivery is not None
        assert sorted(env.db.probes()) == sorted(probe_sql("shop", f"t{i}") for i in range(6))
        assert (await rt.service.results.view(ref("om_1"))).delivery is not None
    env.db.forget()
    result, sends = await env.resend(config, "om_1")
    assert result == "sent" and len(sends) == 1
    assert len(env.db.probes()) == 6


async def test_the_same_object_on_two_clusters_is_checked_and_counted_on_each(env: Env) -> None:
    second = Database(tables={**TABLES, **WIDE}, query=Result(("region",), [("east",)]))
    env.db.tables = {**TABLES, **WIDE}
    env.db.query = Result(("region",), [("east",)])
    targets = [cluster_config("sr-a"), cluster_config("sr-b")]
    env.drivers = {"sr-b": second.drv}
    async with env.opened(capped(env, 1000, targets=targets)) as rt:
        second.forget()
        record = await env.turn(rt, wide(env, "两个集群同名表", "sr-a", "sr-b"), "om_1")
        assert record.state == "completed"

    targets = [cluster_config("sr-a"), cluster_config("sr-b")]
    async with env.opened(capped(env, 11, targets=targets)) as rt:
        second.forget()
        with pytest.raises(ResultUnverifiableError):
            await rt.service.results.view(ref("om_1"))
        assert env.db.probes() == [] and second.probes() == []
    targets = [cluster_config("sr-a"), cluster_config("sr-b")]
    async with env.opened(capped(env, 12, targets=targets)) as rt:
        second.forget()
        assert (await rt.service.results.view(ref("om_1"))).delivery is not None
        assert len(env.db.probes()) == 6 and len(second.probes()) == 6


async def test_a_full_listing_page_fits_the_smallest_allowed_limit(env: Env) -> None:
    """启动校验的下界（5 × 一页）足够一轮新会话列出满页：列表自身挑选可读对象的探测另受
    ``2 × max_rows`` 封顶、不计入，证据依赖的 1 个对象在 5 次复核中各计一次。"""
    async with env.opened(capped(env, 5)) as rt:
        message = env.scripts.add("列一页", tool_call("list_tables", cluster=SR.target_id), cite())
        record = await env.turn(rt, message, "om_1")
        assert record.state == "completed", record.failure_code
        listed = [sql for sql in env.db.probes() if sql == probe_sql("shop", "regions")]
        assert len(listed) == 1 + 5
