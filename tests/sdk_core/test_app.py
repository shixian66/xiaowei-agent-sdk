"""Task 5：组合的运行核心。``Application.run_turn`` + 真 Runner + ScriptedModel + loopback MCP
fixture + SQLAlchemySession + 隔离的真实 PostgreSQL。

应用只持有一个 SDK Model 对象；并发的多轮共用它，脚本按本轮用户消息分派。
"""

import asyncio
import inspect
import io
import itertools
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from agents import set_tracing_disabled
from agents.extensions.memory import SQLAlchemySession
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message, function_call
from agents.tracing import flush_traces, set_trace_processors
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.sdk_core.mcp_fixture import FAKE_TOKEN, INJECTION, PRIVATE, Running, mcp_app, serve
from tests.sdk_core.synthetic_tools import (
    PRIVATE_NOTE,
    QUERY_TOOL,
    TARGET,
    TOTAL_TOOL,
    Clock,
    Grants,
    RecordingAdapter,
    ready_engine,
    store,
)
from tests.sdk_core.test_mcp_integration import FIXTURE_TARGET, TOKEN_ENV, mcp_catalog
from tests.sdk_core.test_mcp_integration import config as mcp_config
from tests.sdk_core.test_model_api import OPENAI as PROFILE
from tests.sdk_core.test_sdk_contract import _RecordingProcessor

from xiaowei.app import AppConfig, Application, DataPolicy, TurnError
from xiaowei.config import configure_runtime
from xiaowei.evidence import EvidenceStore
from xiaowei.governance import GovernedTools
from xiaowei.mcp import MCPIntegration
from xiaowei.models import Budget, Channel, Identity, RunContext
from xiaowei.session import SessionInputPolicy, SessionLimits

pytestmark = pytest.mark.loopback

LOOKUP = "fixture/lookup"
ALL_TOOLS = frozenset({TOTAL_TOOL, QUERY_TOOL, LOOKUP})
SDK_NAMES = {TOTAL_TOOL: "order_total", QUERY_TOOL: "run_query", LOOKUP: "fixture__lookup"}
SYNTHETIC_SQL = "SELECT secret_margin FROM synthetic_orders WHERE region = 'east'"
MODEL_SECRET = "模型私下写的结论-5c1e"  # noqa: S105 - 模型输出标记，不是凭据


def app_config(**overrides: Any) -> AppConfig:
    values: dict[str, Any] = {
        # 查询用途含实际查询工具 run_query；诊断用途不含。
        "purposes": {
            "query": ALL_TOOLS,
            "diagnose": frozenset({TOTAL_TOOL, LOOKUP}),
        },
        "data_policies": {
            PROFILE.data_policy_id: DataPolicy(
                input=SessionInputPolicy(max_bytes=2000), model_tools=ALL_TOOLS
            )
        },
        "session_limits": SessionLimits(
            max_history_turns=5, max_history_bytes=40_000, retention_seconds=7200
        ),
        "max_concurrent_turns": 4,
    }
    values.update(overrides)
    return AppConfig(**values)


# ---- 按消息分派的脚本模型 ------------------------------------------------------------

Step = Callable[[ModelCall], Any]
_call_ids = itertools.count(1)


def _user_text(call: ModelCall) -> str:
    items = call.input
    assert isinstance(items, list)
    content = [i for i in items if i.get("role") == "user"][-1]["content"]
    return content if isinstance(content, str) else "".join(p["text"] for p in content)


def evidence_in(call: ModelCall) -> list[str]:
    """模型输入中所有工具结果（本轮与回放的历史）的证据标识，按出现顺序。"""
    items = call.input
    assert isinstance(items, list)
    found = []
    for item in items:
        if item.get("type") != "function_call_output":
            continue
        try:
            envelope = json.loads(item["output"])
        except ValueError:
            continue
        found.append(envelope["evidence_id"])
    return found


def tool_call(name: str, **arguments: object) -> Step:
    return lambda call: [function_call(name, arguments, call_id=f"call-{next(_call_ids)}")]


def answer(evidence_ids: Sequence[str], analysis: str = "数据平稳", **extra: object) -> list[Any]:
    body = {
        "evidence_ids": list(evidence_ids),
        "inferences": [{"text": analysis, "evidence_ids": list(evidence_ids)}] if analysis else [],
        "clarification": None,
        **extra,
    }
    return [assistant_message(json.dumps(body, ensure_ascii=False))]


def cite(analysis: str = "数据平稳") -> Step:
    """引用模型在输入中看到的全部证据。"""
    return lambda call: answer(evidence_in(call), analysis)


def clarify(question: str = "请说明要看的地区") -> Step:
    body = {"evidence_ids": [], "inferences": [], "clarification": question}
    return lambda call: [assistant_message(json.dumps(body, ensure_ascii=False))]


def upstream_error(call: ModelCall) -> Any:
    raise RuntimeError(f"upstream 400: {MODEL_SECRET}")


def after(event: asyncio.Event, step: Step, *, entered: asyncio.Event | None = None) -> Step:
    """等待事件后再返回：让一轮停在模型调用中。"""

    async def respond(call: ModelCall) -> Any:
        if entered is not None:
            entered.set()
        await event.wait()
        return step(call)

    return respond


def sleeping(seconds: float, step: Step, *, entered: asyncio.Event | None = None) -> Step:
    async def respond(call: ModelCall) -> Any:
        if entered is not None:
            entered.set()
        await asyncio.sleep(seconds)
        return step(call)

    return respond


@dataclass
class Scripts:
    by_message: dict[str, list[Step]] = field(default_factory=dict)
    calls: dict[str, list[ModelCall]] = field(default_factory=dict)

    def model(self) -> ScriptedModel:
        return ScriptedModel([ModelStep.respond(self._route) for _ in range(200)])

    def add(self, message: str, *steps: Step) -> str:
        self.by_message[message] = list(steps)
        return message

    def tools_seen(self, message: str) -> list[set[str]]:
        return [{tool.name for tool in call.tools} for call in self.calls.get(message, [])]

    async def _route(self, call: ModelCall) -> Any:
        message = _user_text(call)
        self.calls.setdefault(message, []).append(call)
        result = self.by_message[message].pop(0)(call)
        return await result if inspect.isawaitable(result) else result


# ---- 装配 ----------------------------------------------------------------------------


@dataclass
class Env:
    engine: AsyncEngine
    grants: Grants
    clock: Clock
    adapter: RecordingAdapter
    scripts: Scripts
    evidence: EvidenceStore
    governed: GovernedTools
    mcp: MCPIntegration
    fixture: Running
    app: Application = field(init=False)

    def __post_init__(self) -> None:
        self.app = self.application()

    def application(self, config: AppConfig | None = None) -> Application:
        return Application(
            config or app_config(),
            profile=PROFILE,
            model=self.scripts.model(),
            engine=self.engine,
            governance=self.governed,
            evidence=self.evidence,
            local_tools={TOTAL_TOOL: self.adapter.execute, QUERY_TOOL: self.adapter.execute},
            mcp=self.mcp,
            clock=self.clock,
        )

    def scope(self, mode: Any, authorized: frozenset[str] = ALL_TOOLS) -> frozenset[str]:
        return self.app.scope_for_turn(mode, authorized, self.app.available_tools)

    def ctx(
        self,
        subject: str = "alice",
        session: str = "s1",
        turn: str = "t1",
        channel: Channel = "web",
        *,
        mode: Any = "query",
        max_tool_calls: int = 5,
        timeout: float = 30.0,
    ) -> RunContext:
        return RunContext(
            identity=Identity(
                subject_id=subject, session_id=session, turn_id=turn, channel=channel
            ),
            target_scope=frozenset({TARGET, FIXTURE_TARGET}),
            tool_scope=self.scope(mode),
            budget=Budget(max_turns=6, max_tool_calls=max_tool_calls, timeout_seconds=timeout),
        )

    async def stored(self, session_id: str = "s1") -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT message_data FROM agent_messages WHERE session_id = :sid ORDER BY id"),
                {"sid": session_id},
            )
            return [json.loads(r[0]) for r in rows]

    async def state(self, session_id: str = "s1") -> tuple[str, int] | None:
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT state, turns FROM xiaowei_session WHERE session_id = :sid"),
                    {"sid": session_id},
                )
            ).one_or_none()
        return None if row is None else (row[0], row[1])

    def tool_ids(self) -> list[str]:
        return [request.tool_id for request in self.adapter.calls]


@pytest.fixture
async def env(postgres_url: URL, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    monkeypatch.setenv(TOKEN_ENV, FAKE_TOKEN)
    with serve(mcp_app(token=FAKE_TOKEN)) as running:
        async with ready_engine(postgres_url) as engine:
            grants, clock = Grants(), Clock()
            for subject in ("alice", "bob"):
                grants.grant(subject, TOTAL_TOOL, QUERY_TOOL)
                grants.grant(subject, LOOKUP, target=FIXTURE_TARGET)
            tools = mcp_catalog("fixture")
            evidence = store(engine, grants, clock, tools)
            governed = GovernedTools(tools, evidence, authorize=grants)
            configs = [mcp_config(running.url, tools=("lookup",), auth_ref=f"env:{TOKEN_ENV}")]
            async with MCPIntegration(configs, governed, clock=clock) as integration:
                yield Env(
                    engine=engine,
                    grants=grants,
                    clock=clock,
                    adapter=RecordingAdapter(),
                    scripts=Scripts(),
                    evidence=evidence,
                    governed=governed,
                    mcp=integration,
                    fixture=running,
                )


# ---- 用例 ----------------------------------------------------------------------------


async def test_local_and_mcp_followup_through_real_runner(env: Env) -> None:
    first = env.scripts.add(
        "东区订单与 k1？",
        tool_call("order_total", region="east"),
        tool_call("fixture__lookup", key="k1"),
        cite("两者都来自本轮工具"),
    )
    delivered = await env.app.run_turn(env.ctx(turn="t1"), first)

    assert env.scripts.tools_seen(first) == [set(SDK_NAMES.values())] * 3
    assert len(delivered.evidence_ids) == 2 and delivered.channel == "web"
    assert "local/order_total" in delivered.content and "fixture/lookup" in delivered.content
    assert '"rows"' in delivered.content and INJECTION in delivered.content
    assert "两者都来自本轮工具" in delivered.content
    assert env.tool_ids() == [TOTAL_TOOL]
    assert [name for name, _ in env.fixture.recorder.tool_calls] == ["lookup"]

    # 追问：历史从 Session 回放，模型引用上一轮证据，工具不重跑。
    followup = env.scripts.add("刚才两项再总结一下", cite("沿用上一轮证据"))
    again = await env.app.run_turn(env.ctx(turn="t2"), followup)
    assert set(again.evidence_ids) == set(delivered.evidence_ids)
    assert evidence_in(env.scripts.calls[followup][0]) == list(delivered.evidence_ids)
    assert env.tool_ids() == [TOTAL_TOOL]
    assert len(env.fixture.recorder.tool_calls) == 1
    assert await env.state("s1") == ("active", 2)

    # 另一渠道的会话：同样的问题得到飞书的获准投影。
    feishu = env.scripts.add(
        "飞书：东区订单与 k1？",
        tool_call("order_total", region="east"),
        tool_call("fixture__lookup", key="k1"),
        cite(),
    )
    other = await env.app.run_turn(env.ctx(session="s2", turn="t3", channel="feishu"), feishu)
    assert other.channel == "feishu" and len(other.evidence_ids) == 2
    assert '"total": 100' in other.content and '"value": 7' in other.content
    assert '"rows"' not in other.content and INJECTION not in other.content
    for content in (delivered.content, again.content, other.content):
        assert PRIVATE_NOTE not in content and PRIVATE not in content


async def test_concurrent_sessions_are_isolated(env: Env) -> None:
    b_done, a_entered = asyncio.Event(), asyncio.Event()
    a_message = env.scripts.add(
        "A：东区",
        after(b_done, tool_call("order_total", region="east"), entered=a_entered),
        cite("A 的结论"),
    )
    b_message = env.scripts.add(
        "B：西区", tool_call("order_total", region="west"), cite("B 的结论")
    )
    alice = env.ctx("alice", "s-a", "t-a", max_tool_calls=1)
    bob = env.ctx("bob", "s-b", "t-b", mode="diagnose", max_tool_calls=1)

    a_task = asyncio.create_task(env.app.run_turn(alice, a_message))
    await a_entered.wait()
    b = await env.app.run_turn(bob, b_message)
    b_done.set()
    a = await a_task

    # 每轮一个 Agent：B 的诊断范围不影响 A 在 B 之后的模型调用。
    assert env.scripts.tools_seen(a_message) == [set(SDK_NAMES.values())] * 2
    assert env.scripts.tools_seen(b_message) == [{"order_total", "fixture__lookup"}] * 2
    # 预算按轮隔离：两轮的上限都是 1，各自执行一次。
    assert sorted(str(r.arguments["region"]) for r in env.adapter.calls) == ["east", "west"]
    assert '"region": "east"' in a.content and "west" not in a.content
    assert '"region": "west"' in b.content and "east" not in b.content
    assert not set(a.evidence_ids) & set(b.evidence_ids)

    # 证据不跨会话：B 引用 A 的证据被拒绝。
    stolen = env.scripts.add("B：引用 A", lambda call: answer(a.evidence_ids))
    with pytest.raises(TurnError) as refused:
        await env.app.run_turn(env.ctx("bob", "s-b", "t-b2", mode="diagnose"), stolen)
    assert refused.value.reason == "answer_rejected"
    assert await env.state("s-b") == ("active", 1)


async def test_same_session_reentry_is_rejected(env: Env) -> None:
    app = env.application(app_config(max_concurrent_turns=1))
    release, entered = asyncio.Event(), asyncio.Event()
    slow = env.scripts.add("慢", after(release, clarify(), entered=entered))
    reentry = env.scripts.add("重入", clarify())
    other = env.scripts.add("另一个会话", clarify())

    running = asyncio.create_task(app.run_turn(env.ctx(turn="t1"), slow))
    await entered.wait()
    with pytest.raises(TurnError) as same:
        await app.run_turn(env.ctx(turn="t2"), reentry)
    assert same.value.reason == "session_busy"
    with pytest.raises(TurnError) as full:
        await app.run_turn(env.ctx("bob", "s2", "t1"), other)
    assert full.value.reason == "busy"
    assert reentry not in env.scripts.calls and other not in env.scripts.calls

    release.set()
    assert (await running).evidence_ids == ()
    # 结束后释放：同一会话的下一轮照常。
    assert (await app.run_turn(env.ctx(turn="t3"), reentry)).evidence_ids == ()
    assert await env.state("s1") == ("active", 2)

    # 同一会话同时到达的两条消息：只有一条进入。
    first, second = env.scripts.add("同时一", clarify()), env.scripts.add("同时二", clarify())
    outcomes = await asyncio.gather(
        app.run_turn(env.ctx(session="s3", turn="x1"), first),
        app.run_turn(env.ctx(session="s3", turn="x2"), second),
        return_exceptions=True,
    )
    refused = [o for o in outcomes if isinstance(o, TurnError)]
    assert len(refused) == 1 and refused[0].reason == "session_busy"
    assert await env.state("s3") == ("active", 1)


async def test_invalid_answer_is_never_delivered_or_committed(env: Env) -> None:
    cases = {
        "伪造证据": (
            [
                tool_call("order_total", region="east"),
                lambda c: answer(["ev_forged"], MODEL_SECRET),
            ],
            "answer_rejected",
        ),
        "夹带事实字段": (
            [lambda c: answer([], "", verified_total=MODEL_SECRET)],
            "model_failed",
        ),
        "自由文本": ([lambda c: [assistant_message(MODEL_SECRET)]], "model_failed"),
        # 模型客户端把上游错误体写进异常消息（Task 1B 实测）；出口不得转述。
        "上游错误": ([upstream_error], "model_failed"),
    }
    for turn, (message, (steps, reason)) in enumerate(cases.items()):
        env.scripts.add(message, *steps)
        with pytest.raises(TurnError) as failed:
            await env.app.run_turn(env.ctx(turn=f"t{turn}"), message)
        assert failed.value.reason == reason
        assert MODEL_SECRET not in str(failed.value) and failed.value.__cause__ is None
        assert await env.stored("s1") == []
        assert await env.state("s1") == ("active", 0)
    assert env.tool_ids() == [TOTAL_TOOL]

    # 下一轮不带出失败轮次的任何内容。
    fresh = env.scripts.add("重新开始", clarify())
    await env.app.run_turn(env.ctx(turn="t9"), fresh)
    assert len(env.scripts.calls[fresh][0].input) == 1
    assert [item["role"] for item in await env.stored("s1")] == ["user", "assistant"]


async def test_timeout_does_not_replay_tools(env: Env) -> None:
    slow = env.scripts.add("超时", tool_call("order_total", region="east"), sleeping(10, cite()))
    started = time.monotonic()
    with pytest.raises(TurnError) as timed_out:
        await env.app.run_turn(env.ctx(turn="t1", timeout=0.5), slow)
    assert timed_out.value.reason == "timeout"
    assert time.monotonic() - started < 3
    assert env.tool_ids() == [TOTAL_TOOL]
    assert await env.stored("s1") == []

    # 取消：本轮在模型调用中被取消，取消照常传播，没有交付，也不提交。
    entered = asyncio.Event()
    cancelled = env.scripts.add(
        "取消", tool_call("order_total", region="west"), sleeping(10, cite(), entered=entered)
    )
    task = asyncio.create_task(env.app.run_turn(env.ctx(turn="t2"), cancelled))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.tool_ids() == [TOTAL_TOOL, TOTAL_TOOL]
    assert await env.stored("s1") == []

    # 下一轮不重放任何工具，也看不到未提交轮次的调用。
    nxt = env.scripts.add("继续", clarify())
    await env.app.run_turn(env.ctx(turn="t3"), nxt)
    assert len(env.scripts.calls[nxt][0].input) == 1
    assert env.tool_ids() == [TOTAL_TOOL, TOTAL_TOOL]
    assert await env.state("s1") == ("active", 1)


async def test_delivery_rechecks_permission_after_commit(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提交之后、交付之前撤权：本轮已保存，但不交付。"""
    committed = SQLAlchemySession.add_items

    async def add_then_revoke(self: SQLAlchemySession, items: Any) -> None:
        await committed(self, items)
        env.grants.revoke_all()

    monkeypatch.setattr(SQLAlchemySession, "add_items", add_then_revoke)
    message = env.scripts.add("东区", tool_call("order_total", region="east"), cite())
    with pytest.raises(TurnError) as refused:
        await env.app.run_turn(env.ctx(turn="t1"), message)
    assert refused.value.reason == "answer_rejected"
    assert await env.state("s1") == ("active", 1)


async def test_turn_budget_is_released_after_each_turn(env: Env) -> None:
    """轮次结束（含失败）时清理工具计数；这里复用同一 turn_id 观察计数是否残留。"""
    ctx = env.ctx(turn="t1", max_tool_calls=1)
    failing = env.scripts.add(
        "失败的一轮", tool_call("order_total", region="east"), lambda c: answer(["ev_forged"])
    )
    with pytest.raises(TurnError):
        await env.app.run_turn(ctx, failing)
    again = env.scripts.add("同一编号", tool_call("order_total", region="west"), cite())
    await env.app.run_turn(ctx, again)
    assert [str(r.arguments["region"]) for r in env.adapter.calls] == ["east", "west"]


async def test_turn_is_not_traced_even_if_tracing_is_reenabled(env: Env) -> None:
    recorder = _RecordingProcessor()
    set_trace_processors([recorder])
    set_tracing_disabled(False)
    try:
        message = env.scripts.add("东区", tool_call("order_total", region="east"), cite())
        await env.app.run_turn(env.ctx(), message)
        flush_traces()
    finally:
        configure_runtime()
    assert recorder.events == []


async def test_query_permission_is_not_inherited(env: Env) -> None:
    assert QUERY_TOOL in env.scope("query")
    assert QUERY_TOOL not in env.scope("diagnose")
    assert QUERY_TOOL not in env.scope("query", frozenset({TOTAL_TOOL, LOOKUP}))
    with pytest.raises(ValueError):
        env.scope("write")

    query = env.scripts.add("查询东区", tool_call("run_query", region="east"), cite())
    await env.app.run_turn(env.ctx(turn="t1", mode="query"), query)
    assert env.tool_ids() == [QUERY_TOOL]

    # 下一轮是诊断：查询工具不展示，模型强行调用时零执行，本轮失败且不提交。
    forced = env.scripts.add("诊断东区", tool_call("run_query", region="east"), cite())
    with pytest.raises(TurnError) as refused:
        await env.app.run_turn(env.ctx(turn="t2", mode="diagnose"), forced)
    assert refused.value.reason == "model_failed"
    assert env.scripts.tools_seen(forced) == [{"order_total", "fixture__lookup"}]
    assert env.tool_ids() == [QUERY_TOOL]
    assert await env.state("s1") == ("active", 1)


_FAIL_WRITES = (
    """
    CREATE FUNCTION xw_test_fail_write() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'injected failure'; END $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER xw_test_fail_write BEFORE INSERT ON agent_messages
    FOR EACH ROW EXECUTE FUNCTION xw_test_fail_write()
    """,
)


async def test_commit_failure_does_not_replay_tools(env: Env) -> None:
    async with env.engine.begin() as conn:
        for statement in _FAIL_WRITES:
            await conn.execute(text(statement))
    message = env.scripts.add("东区", tool_call("order_total", region="east"), cite())
    with pytest.raises(TurnError) as failed:
        await env.app.run_turn(env.ctx(turn="t1"), message)
    assert failed.value.reason == "storage_failed"
    assert "injected" not in str(failed.value)
    assert env.tool_ids() == [TOTAL_TOOL]
    assert await env.state("s1") == ("writing", 0)

    # 存储恢复后，写入中断的会话仍被隔离：模型零调用，工具不补跑。
    async with env.engine.begin() as conn:
        await conn.execute(text("DROP TRIGGER xw_test_fail_write ON agent_messages"))
    retry = env.scripts.add("再试一次", cite())
    with pytest.raises(TurnError) as isolated:
        await env.app.run_turn(env.ctx(turn="t2"), retry)
    assert isolated.value.reason == "session_unavailable"
    assert retry not in env.scripts.calls
    assert env.tool_ids() == [TOTAL_TOOL]
    assert await env.stored("s1") == []


@contextmanager
def all_logs() -> Iterator[list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    handler, level = Keep(logging.DEBUG), root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


_STAGE = re.compile(r"turn=(\S+) stage=(\w+) reason=([\w-]+) elapsed_ms=\d+")


async def test_stage_logs_contain_only_safe_metadata(env: Env) -> None:
    ok = env.scripts.add(
        f"东区与 k1，{SYNTHETIC_SQL}",
        tool_call("order_total", region="east"),
        tool_call("fixture__lookup", key="k1"),
        cite(MODEL_SECRET),
    )
    bad = env.scripts.add(
        f"再查，{SYNTHETIC_SQL}",
        tool_call("order_total", region="east"),
        lambda c: answer(["ev_forged"], MODEL_SECRET),
    )
    with all_logs() as records:
        await env.app.run_turn(env.ctx(turn="req-ok"), ok)
        with pytest.raises(TurnError):
            await env.app.run_turn(env.ctx(turn="req-bad"), bad)

    stages: dict[str, list[tuple[str, str]]] = {}
    for record in records:
        if record.name == "xiaowei.app":
            match = _STAGE.fullmatch(record.getMessage())
            assert match is not None and record.exc_info is None
            stages.setdefault(match[1], []).append((match[2], match[3]))
    assert stages == {
        "req-ok": [
            ("received", "-"),
            ("storage_ready", "-"),
            ("answered", "-"),
            ("committed", "-"),
            ("delivered", "-"),
        ],
        "req-bad": [
            ("received", "-"),
            ("storage_ready", "-"),
            ("answered", "-"),
            ("failed", "answer_rejected"),
        ],
    }
    stream = io.StringIO()
    formatter = logging.Formatter()
    for record in records:
        stream.write(formatter.format(record) + "\n")
    output = stream.getvalue()
    for forbidden in (SYNTHETIC_SQL, MODEL_SECRET, PRIVATE_NOTE, PRIVATE, INJECTION, FAKE_TOKEN):
        assert forbidden not in output


async def test_turn_is_refused_before_the_model(env: Env) -> None:
    # 本轮范围超出模型数据策略：可信入口的错误也不放行。
    narrow = app_config(
        data_policies={
            PROFILE.data_policy_id: DataPolicy(
                input=SessionInputPolicy(max_bytes=2000, forbidden_patterns=(r"身份证号",)),
                model_tools=frozenset({TOTAL_TOOL, LOOKUP}),
            )
        }
    )
    app = env.application(narrow)
    assert QUERY_TOOL not in app.scope_for_turn("query", ALL_TOOLS, app.available_tools)
    message = env.scripts.add("越界", tool_call("run_query", region="east"), cite())
    with pytest.raises(TurnError) as refused:
        await app.run_turn(env.ctx(turn="t1"), message)
    assert refused.value.reason == "scope_rejected"
    assert message not in env.scripts.calls and env.adapter.calls == []

    # 用户输入不符合 Profile 数据策略：首个模型调用前拒绝。
    sensitive = env.scripts.add("我的身份证号是 000", clarify())
    with pytest.raises(TurnError) as rejected:
        await app.run_turn(env.ctx(turn="t0", mode="diagnose"), sensitive)
    assert rejected.value.reason == "content_rejected"
    assert sensitive not in env.scripts.calls

    # 存储未初始化：就绪检查在模型调用前拒绝。
    async with env.engine.begin() as conn:
        await conn.execute(text("DROP TABLE xiaowei_session"))
    unready = env.scripts.add("未就绪", clarify())
    with pytest.raises(TurnError) as missing:
        await env.app.run_turn(env.ctx(turn="t2"), unready)
    assert missing.value.reason == "storage_unavailable"
    assert unready not in env.scripts.calls


async def test_configuration_is_checked_at_startup(env: Env) -> None:
    with pytest.raises(ValidationError):
        app_config(purposes={"query": ALL_TOOLS})
    other_profile = PROFILE.model_copy(update={"data_policy_id": "unknown"})
    bad: dict[str, Callable[[], Awaitable[object] | object]] = {
        "数据策略": lambda: Application(
            app_config(),
            profile=other_profile,
            model=env.scripts.model(),
            engine=env.engine,
            governance=env.governed,
            evidence=env.evidence,
            local_tools={},
            clock=env.clock,
        ),
        "未登记": lambda: env.application(
            app_config(purposes={"query": frozenset({"local/unknown"}), "diagnose": frozenset()})
        ),
        "local/": lambda: Application(
            app_config(),
            profile=PROFILE,
            model=env.scripts.model(),
            engine=env.engine,
            governance=env.governed,
            evidence=env.evidence,
            local_tools={LOOKUP: env.adapter.execute},
            clock=env.clock,
        ),
    }
    for expected, build in bad.items():
        with pytest.raises(ValueError, match=expected):
            build()
