"""Task 5：组合的运行核心。``Application.run_turn`` + 真 Runner + ``open_model`` 装配的 SDK
Responses 模型 + loopback MCP fixture + SQLAlchemySession + 隔离的真实 PostgreSQL。

模型端点由 ``httpx2.MockTransport`` 按脚本应答（不建立网络连接）；应用只持有一个模型绑定，
并发的多轮共用它，脚本按请求中的本轮用户消息分派。HTTP mock 不等于真实模型验证。
"""

import asyncio
import inspect
import io
import itertools
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import httpx2
import pytest
from agents import RunConfig, Runner, set_tracing_disabled
from agents.testing import assistant_message, function_call
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
from tests.sdk_core.test_model_api import _responses_body
from tests.sdk_core.test_sdk_contract import _RecordingProcessor
from tests.sdk_core.test_vertex_model import VERTEX, _body, _call_part

from xiaowei import app as app_module
from xiaowei.app import AppConfig, Application, DataPolicy, TurnError, safe_run_config
from xiaowei.config import configure_runtime
from xiaowei.evidence import AnswerRejectedError
from xiaowei.governance import GovernedTools
from xiaowei.mcp import MCPIntegration
from xiaowei.model_api import ModelBinding, open_model
from xiaowei.models import Budget, Channel, Delivery, Identity, RunContext
from xiaowei.session import SessionInputPolicy, SessionLimits
from xiaowei.starrocks import StarRocksError, StarRocksErrorCode

pytestmark = pytest.mark.loopback

LOOKUP = "fixture/lookup"
ALL_TOOLS = frozenset({TOTAL_TOOL, QUERY_TOOL, LOOKUP})
SDK_NAMES = {TOTAL_TOOL: "order_total", QUERY_TOOL: "run_query", LOOKUP: "fixture__lookup"}
SYNTHETIC_SQL = "SELECT secret_margin FROM synthetic_orders WHERE region = 'east'"
MODEL_SECRET = "模型私下写的结论-5c1e"  # noqa: S105 - 模型输出标记，不是凭据
FAKE_MODEL_KEY = "sk-test-app-model"


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


@dataclass(frozen=True)
class ModelCall:
    """模型端点收到的一次 Responses 请求：输入项与工具名。"""

    input: list[dict[str, Any]]
    tools: list[str]
    instructions: str = ""


Step = Callable[[ModelCall], Any]
_call_ids = itertools.count(1)


def _user_text(call: ModelCall) -> str:
    content = [i for i in call.input if i.get("role") == "user"][-1]["content"]
    return content if isinstance(content, str) else "".join(p["text"] for p in content)


def evidence_in(call: ModelCall) -> list[str]:
    """模型输入中所有工具结果（本轮与回放的历史）的证据标识，按出现顺序。"""
    found = []
    for item in call.input:
        if item.get("type") != "function_call_output":
            continue
        try:
            envelope = json.loads(item["output"])
        except ValueError:
            continue
        found.append(envelope["evidence_id"])
    return found


# 搜表（P2.5 Task 6）的第一页：“.” 出现在每个“库名.表名”中，即目录的前若干个对象。
SEARCH_ALL: dict[str, object] = {"keyword": ".", "database": None, "page_size": 5, "cursor": None}


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
    """供应商返回错误；模型客户端会把错误体写进异常消息。"""
    error = {"message": f"invalid request: {MODEL_SECRET}", "type": "invalid_request_error"}
    return httpx2.Response(400, json={"error": error})


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

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def add(self, message: str, *steps: Step) -> str:
        self.by_message[message] = list(steps)
        return message

    def tools_seen(self, message: str) -> list[set[str]]:
        return [set(call.tools) for call in self.calls.get(message, [])]

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        call = ModelCall(
            body["input"],
            [tool["name"] for tool in body.get("tools", [])],
            body.get("instructions", ""),
        )
        message = _user_text(call)
        self.calls.setdefault(message, []).append(call)
        result = self.by_message[message].pop(0)(call)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, httpx2.Response):
            return result
        output = [item.model_dump(mode="json", exclude_none=True) for item in result]
        return httpx2.Response(200, json=_responses_body(output, usage=None))


# ---- 装配 ----------------------------------------------------------------------------


@dataclass
class Env:
    engine: AsyncEngine
    grants: Grants
    clock: Clock
    adapter: RecordingAdapter
    scripts: Scripts
    binding: ModelBinding
    governed: GovernedTools
    mcp: MCPIntegration
    fixture: Running
    app: Application = field(init=False)

    def __post_init__(self) -> None:
        self.app = self.application()

    def application(
        self, config: AppConfig | None = None, *, local_tools: Any = None
    ) -> Application:
        return Application(
            config or app_config(),
            model=self.binding,
            engine=self.engine,
            governance=self.governed,
            local_tools=local_tools
            or {
                (TOTAL_TOOL, TARGET): self.adapter.execute,
                (QUERY_TOOL, TARGET): self.adapter.execute,
            },
            mcp=self.mcp,
            clock=self.clock,
        )

    async def deliver(self, ctx: RunContext, message: str) -> Delivery:
        """渠道的做法：完成一轮后，按接收渠道与当前权限生成交付内容。"""
        answer = await self.app.run_turn(ctx, message)
        return await self.governed.evidence.validate_answer(answer, ctx)

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
            budget=Budget(
                max_turns=6,
                max_tool_calls=max_tool_calls,
                timeout_seconds=timeout,
                max_scope_checks=1000,
            ),
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
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), FAKE_MODEL_KEY)
    with serve(mcp_app(token=FAKE_TOKEN)) as running:
        async with ready_engine(postgres_url) as engine:
            grants, clock = Grants(), Clock()
            for subject in ("alice", "bob"):
                grants.grant(subject, TOTAL_TOOL, QUERY_TOOL)
                grants.grant(subject, LOOKUP, target=FIXTURE_TARGET)
            tools = mcp_catalog("fixture")
            evidence = store(engine, grants, clock, tools)
            governed = GovernedTools(evidence)
            configs = [mcp_config(running.url, tools=("lookup",), auth_ref=f"env:{TOKEN_ENV}")]
            scripts = Scripts()
            async with (
                open_model(PROFILE, transport=scripts.transport()) as bound,
                MCPIntegration(configs, governed, clock=clock) as integration,
            ):
                yield Env(
                    engine=engine,
                    grants=grants,
                    clock=clock,
                    adapter=RecordingAdapter(),
                    scripts=scripts,
                    binding=bound,
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
    delivered = await env.deliver(env.ctx(turn="t1"), first)

    assert env.scripts.tools_seen(first) == [set(SDK_NAMES.values())] * 3
    assert len(delivered.evidence_ids) == 2 and delivered.channel == "web"
    assert "local/order_total" in delivered.content and "fixture/lookup" in delivered.content
    assert '"rows"' in delivered.content and INJECTION in delivered.content
    assert "两者都来自本轮工具" in delivered.content
    assert env.tool_ids() == [TOTAL_TOOL]
    assert [name for name, _ in env.fixture.recorder.tool_calls] == ["lookup"]

    # 追问：历史从 Session 回放，模型引用上一轮证据，工具不重跑。
    followup = env.scripts.add("刚才两项再总结一下", cite("沿用上一轮证据"))
    again = await env.deliver(env.ctx(turn="t2"), followup)
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
    other = await env.deliver(env.ctx(session="s2", turn="t3", channel="feishu"), feishu)
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

    a_task = asyncio.create_task(env.deliver(alice, a_message))
    await a_entered.wait()
    b = await env.deliver(bob, b_message)
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
    assert (await running).answer.evidence_ids == ()
    # 结束后释放：同一会话的下一轮照常。
    assert (await app.run_turn(env.ctx(turn="t3"), reentry)).answer.evidence_ids == ()
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


async def test_delivery_rechecks_permission_after_commit(env: Env) -> None:
    """提交之后、交付之前撤权：本轮已保存，但交付时按当前权限拒绝。"""
    message = env.scripts.add("东区", tool_call("order_total", region="east"), cite())
    ctx = env.ctx(turn="t1")
    answer_ = await env.app.run_turn(ctx, message)
    assert await env.state("s1") == ("active", 1)
    assert (await env.governed.evidence.validate_answer(answer_, ctx)).evidence_ids

    env.grants.revoke_all()
    with pytest.raises(AnswerRejectedError):
        await env.governed.evidence.validate_answer(answer_, ctx)
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


def test_safe_run_config_disables_tracing_and_sensitive_trace_data() -> None:
    first, second = safe_run_config(), safe_run_config()
    for config in (first, second):
        assert config.tracing_disabled is True
        assert config.trace_include_sensitive_data is False
        assert config.tool_execution.max_function_tool_concurrency == 4
    assert first is not second  # 每个调用方一份，互不共享可变对象


async def test_application_runs_with_the_shared_safe_run_config(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[RunConfig] = []

    def recording() -> RunConfig:
        built.append(safe_run_config())
        return built[-1]

    monkeypatch.setattr(app_module, "safe_run_config", recording)
    seen: list[RunConfig] = []
    original = Runner.run

    async def run(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["run_config"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(Runner, "run", run)
    app = env.application()
    message = env.scripts.add("东区", tool_call("order_total", region="east"), cite())
    await app.run_turn(env.ctx(), message)
    assert len(built) == 1 and seen == built


async def test_unknown_tool_result_stops_the_turn(env: Env) -> None:
    """工具已执行但结果未知：本轮中止，模型不能在同一轮重试，也不提交。"""

    async def executed_then_failed(request: Any) -> Any:
        env.adapter.calls.append(request)
        raise RuntimeError("连接在返回结果前断开")

    app = env.application(local_tools={(TOTAL_TOOL, TARGET): executed_then_failed})
    message = env.scripts.add(
        "东区",
        tool_call("order_total", region="east"),
        tool_call("order_total", region="east"),
        clarify(),
    )
    with pytest.raises(TurnError) as failed:
        await app.run_turn(env.ctx(turn="t1", max_tool_calls=2), message)
    assert failed.value.reason == "tool_failed"
    assert "断开" not in str(failed.value)
    assert len(env.adapter.calls) == 1 and len(env.scripts.calls[message]) == 1
    assert await env.stored("s1") == []

    # 对照：I/O 之前的参数拒绝交给模型修正，本轮照常完成。
    fixed = env.scripts.add(
        "东区（修正）",
        lambda call: [function_call("order_total", {"region": 1}, call_id="bad-arg")],
        tool_call("order_total", region="east"),
        cite(),
    )
    await env.app.run_turn(env.ctx(turn="t2", max_tool_calls=2), fixed)
    assert "参数不符合工具契约" in json.dumps(env.scripts.calls[fixed][1].input, ensure_ascii=False)
    assert len(env.adapter.calls) == 2


async def test_session_is_bound_to_the_data_policy(env: Env) -> None:
    first = env.scripts.add("东区", tool_call("order_total", region="east"), cite())
    await env.app.run_turn(env.ctx(turn="t1"), first)

    # 同一 data_policy_id、内容收窄：重启后的应用在首个模型调用前拒绝旧会话。
    policy = app_config().data_policies[PROFILE.data_policy_id]
    narrowed = {
        "工具": policy.model_copy(update={"model_tools": frozenset({QUERY_TOOL, LOOKUP})}),
        "输入容量": policy.model_copy(update={"input": SessionInputPolicy(max_bytes=1000)}),
    }
    for name, changed in narrowed.items():
        app = env.application(app_config(data_policies={PROFILE.data_policy_id: changed}))
        ctx = env.ctx(turn=f"t-{name}").model_copy(
            update={"tool_scope": env.scope("query") & changed.model_tools}
        )
        followup = env.scripts.add(f"追问（{name}）", cite())
        with pytest.raises(TurnError) as refused:
            await app.run_turn(ctx, followup)
        assert refused.value.reason == "session_unavailable", name
        assert followup not in env.scripts.calls

    # 对照：策略内容未变的新应用照常回放。
    same = env.scripts.add("追问（未变）", cite())
    await env.application().run_turn(env.ctx(turn="t2"), same)
    assert len(evidence_in(env.scripts.calls[same][0])) == 1


async def test_changed_instructions_keep_the_session(env: Env) -> None:
    """instructions 不进入会话绑定：更新默认说明（如 P2 诊断说明）后旧会话照常回放。"""
    first = env.scripts.add("东区（旧说明）", tool_call("order_total", region="east"), cite())
    old = env.application(app_config(instructions="旧版说明：只读数据助手。"))
    await old.run_turn(env.ctx(turn="t1"), first)

    followup = env.scripts.add("追问（新说明）", cite())
    await env.application().run_turn(env.ctx(turn="t2"), followup)
    assert len(evidence_in(env.scripts.calls[followup][0])) == 1


async def test_each_turn_tells_the_model_the_current_time(env: Env) -> None:
    first = env.scripts.add("昨天是何时", clarify())
    await env.app.run_turn(env.ctx(turn="clock-1"), first)
    initial = env.clock().isoformat()
    assert initial in env.scripts.calls[first][0].instructions
    env.clock.advance(60)
    followup = env.scripts.add("现在呢", clarify())
    await env.app.run_turn(env.ctx(turn="clock-2"), followup)
    instructions = env.scripts.calls[followup][0].instructions
    assert env.clock().isoformat() in instructions and initial not in instructions


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


_STAGE = re.compile(
    r"turn=(\S+) stage=(\w+) reason=([\w-]+) elapsed_ms=\d+"
    r"(?: error_kind=[a-z_]+ http_status=[\d-]+(?: reject_reason=[a-z_]+)?)?"
    r"(?: tool_error=[a-z_]+)?(?: answer_reject=[a-z_]+)?"
    r"(?: tool_calls=\d+ executed=\d+ truncated=\d+ rejected=\d+)?"
    r"(?: limit_rescue=failed)?"
)


@pytest.mark.parametrize(
    ("case", "kind", "status"),
    [
        ("http_400", "provider_http", "400"),
        ("http_429", "provider_http", "429"),
        ("http_503", "provider_http", "503"),
        ("invalid_output", "invalid_output", "-"),
        ("non_json", "response_rejected", "-"),
        ("connection", "connection", "-"),
        ("timeout", "model_timeout", "-"),
    ],
)
async def test_model_failure_logs_safe_category_and_http_status(
    env: Env, case: str, kind: str, status: str
) -> None:
    def respond(call: ModelCall) -> Any:
        if case.startswith("http_"):
            return httpx2.Response(
                int(status), json={"error": {"message": MODEL_SECRET, "type": "upstream"}}
            )
        if case == "invalid_output":
            return [assistant_message(MODEL_SECRET)]
        if case == "non_json":
            return httpx2.Response(200, text=MODEL_SECRET)
        if case == "connection":
            raise httpx2.ConnectError(MODEL_SECRET)
        raise httpx2.ReadTimeout(MODEL_SECRET)

    message = env.scripts.add("失败分类 " + SYNTHETIC_SQL, respond)
    with all_logs() as records, pytest.raises(TurnError) as failed:
        await env.app.run_turn(env.ctx(turn="failure-kind"), message)
    assert failed.value.reason == "model_failed"
    output = "\n".join(record.getMessage() for record in records)
    assert f"error_kind={kind} http_status={status}" in output
    assert MODEL_SECRET not in output and SYNTHETIC_SQL not in output
    assert all(record.exc_info is None for record in records if record.name == "xiaowei.app")
    assert len(env.scripts.calls[message]) == 1  # 分类不引入请求重试。


def test_unclassified_failure_does_not_log_exception_details() -> None:
    error = RuntimeError(f"{MODEL_SECRET}\n{SYNTHETIC_SQL}\nforged-stage=completed")
    with all_logs() as records:
        app_module._stage("unclassified", "failed", time.monotonic(), "model_failed", cause=error)
    assert len(records) == 1
    record = records[0]
    assert record.getMessage().endswith("error_kind=unexpected_error http_status=-")
    assert _STAGE.fullmatch(record.getMessage()) is not None and record.exc_info is None
    assert MODEL_SECRET not in record.getMessage() and "forged-stage" not in record.getMessage()


@pytest.mark.parametrize(
    ("case", "kind", "status"),
    [
        ("429", "provider_http", "429"),
        ("503", "provider_http", "503"),
        ("connection", "connection", "-"),
        ("timeout", "model_timeout", "-"),
    ],
)
async def test_vertex_failure_uses_the_same_safe_log_categories(
    env: Env, monkeypatch: pytest.MonkeyPatch, case: str, kind: str, status: str
) -> None:
    calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if case == "connection":
            raise httpx2.ConnectError(MODEL_SECRET, request=request)
        if case == "timeout":
            raise httpx2.ReadTimeout(MODEL_SECRET, request=request)
        return httpx2.Response(int(status), json={"error": {"message": MODEL_SECRET}})

    monkeypatch.setenv(VERTEX.api_key_ref.removeprefix("env:"), FAKE_MODEL_KEY)
    profile = VERTEX.model_copy(update={"data_policy_id": PROFILE.data_policy_id})
    async with open_model(profile, transport=httpx2.MockTransport(respond)) as binding:
        vertex_env = replace(env, binding=binding)
        with all_logs() as records, pytest.raises(TurnError) as failed:
            await vertex_env.app.run_turn(vertex_env.ctx(turn="vertex-failure"), SYNTHETIC_SQL)
    output = "\n".join(record.getMessage() for record in records)
    assert failed.value.reason == "model_failed" and calls == 1
    assert f"error_kind={kind} http_status={status}" in output
    assert all(value not in output for value in (MODEL_SECRET, SYNTHETIC_SQL, FAKE_MODEL_KEY))


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("max_tokens", "finish_reason_max_tokens"),
        ("safety", "finish_reason_safety"),
        ("malformed", "finish_reason_malformed_function_call"),
        ("recitation", "finish_reason_recitation"),
        ("unknown_finish", "finish_reason_other"),
        ("missing_finish", "finish_reason_other"),
        ("no_candidate", "not_single_candidate"),
        ("two_candidates", "not_single_candidate"),
        ("unknown_tool", "unknown_tool"),
        ("invalid_args", "invalid_args"),
        ("missing_signature", "invalid_signature"),
        ("duplicate_call_id", "invalid_call_id"),
        ("missing_content", "invalid_content"),
        ("unsupported_part", "unsupported_part"),
        ("too_large", "too_large"),
        ("invalid_json", "invalid_json"),
        ("deep_json", "invalid_json"),
    ],
)
async def test_vertex_rejection_logs_only_fixed_reason(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    reason: str,
) -> None:
    if case == "too_large":
        reply: httpx2.Response = httpx2.Response(200, content=b"x" * 70_000)
    elif case == "invalid_json":
        reply = httpx2.Response(200, content=("{" + MODEL_SECRET).encode())
    elif case == "deep_json":
        reply = httpx2.Response(200, content=b"[" * 1500 + b"0" + b"]" * 1500)
    else:
        parts = [_call_part()]
        if case == "unknown_tool":
            parts = [_call_part(name="unoffered_" + MODEL_SECRET)]
        elif case == "invalid_args":
            parts = [_call_part(name="order_total", args=[MODEL_SECRET])]
        elif case == "missing_signature":
            parts = [_call_part(name="order_total", signature=None)]
        elif case == "duplicate_call_id":
            parts = [
                _call_part(name="order_total", call_id="same-id"),
                _call_part(name="order_total", call_id="same-id", signature=None),
            ]
        elif case == "unsupported_part":
            parts = [{"inlineData": {"data": MODEL_SECRET}}]
        body = _body(parts)
        candidate = body["candidates"][0]
        if case == "max_tokens":
            candidate["finishReason"] = "MAX_TOKENS"
        elif case == "safety":
            candidate["finishReason"] = "SAFETY"
        elif case == "malformed":
            candidate["finishReason"] = "MALFORMED_FUNCTION_CALL"
        elif case == "recitation":
            candidate["finishReason"] = "RECITATION"
        elif case == "unknown_finish":
            candidate["finishReason"] = "UNPUBLISHED_" + MODEL_SECRET
        elif case == "missing_finish":
            candidate.pop("finishReason")
        elif case == "no_candidate":
            body["candidates"] = []
        elif case == "two_candidates":
            body["candidates"].append(candidate.copy())
        elif case == "missing_content":
            candidate.pop("content")
        reply = httpx2.Response(200, json=body)

    calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return reply

    monkeypatch.setenv(VERTEX.api_key_ref.removeprefix("env:"), FAKE_MODEL_KEY)
    profile = VERTEX.model_copy(update={"data_policy_id": PROFILE.data_policy_id})
    async with open_model(profile, transport=httpx2.MockTransport(respond)) as binding:
        vertex_env = replace(env, binding=binding)
        with all_logs() as records, pytest.raises(TurnError) as failed:
            await vertex_env.app.run_turn(vertex_env.ctx(turn="vertex-reject"), SYNTHETIC_SQL)

    output = "\n".join(record.getMessage() for record in records)
    assert failed.value.reason == "model_failed" and calls == 1
    assert f"error_kind=response_rejected http_status=- reject_reason={reason}" in output
    assert MODEL_SECRET not in output and SYNTHETIC_SQL not in output
    assert env.adapter.calls == []
    assert all(
        _STAGE.fullmatch(record.getMessage()) is not None
        for record in records
        if record.name == "xiaowei.app"
    )


@pytest.mark.parametrize("case", ["all_allowed", "budget", "revoked", "diagnose"])
async def test_vertex_parallel_calls_keep_each_tool_governed(
    env: Env, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    first = _call_part(name="order_total", args={"region": "east"})
    second_name = "order_total" if case in ("all_allowed", "budget") else "run_query"
    second = _call_part(name=second_name, args={"region": "west"}, signature=None)
    requests: list[dict[str, Any]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx2.Response(200, json=_body([first, second]))
        evidence_ids = []
        for content in body["contents"]:
            for part in content["parts"]:
                if "functionResponse" not in part:
                    continue
                output = part["functionResponse"]["response"]["output"]
                try:
                    parsed = json.loads(output)
                except ValueError:
                    continue  # SDK 对 I/O 前拒绝的调用返回固定纯文本
                if isinstance(parsed, dict) and "evidence_id" in parsed:
                    evidence_ids.append(parsed["evidence_id"])
        answer = {
            "evidence_ids": evidence_ids,
            "inferences": [],
            "clarification": None if evidence_ids else "本轮没有可用结果",
            "advice": None,
        }
        return httpx2.Response(200, json=_body([{"text": json.dumps(answer)}]))

    if case == "revoked":
        env.grants.allowed.discard(("alice", TARGET, QUERY_TOOL))
    monkeypatch.setenv(VERTEX.api_key_ref.removeprefix("env:"), FAKE_MODEL_KEY)
    profile = VERTEX.model_copy(update={"data_policy_id": PROFILE.data_policy_id})
    async with open_model(profile, transport=httpx2.MockTransport(respond)) as binding:
        vertex_env = replace(env, binding=binding)
        ctx = vertex_env.ctx(
            turn="parallel-" + case,
            mode="diagnose" if case == "diagnose" else "query",
            max_tool_calls=1 if case == "budget" else 5,
        )
        if case == "diagnose":
            with pytest.raises(TurnError) as failed:
                await vertex_env.app.run_turn(ctx, "仅诊断")
            assert failed.value.reason == "model_failed"
        else:
            result = await vertex_env.app.run_turn(ctx, "查两个合成地区")
            assert len(result.answer.evidence_ids) == (2 if case == "all_allowed" else 1)
            if case == "all_allowed":
                followup = await vertex_env.app.run_turn(
                    vertex_env.ctx(turn="parallel-followup"), "继续引用这两项"
                )
                assert set(followup.answer.evidence_ids) == set(result.answer.evidence_ids)

    assert len(env.adapter.calls) == (
        0 if case == "diagnose" else 2 if case == "all_allowed" else 1
    )
    if case == "revoked":
        assert env.tool_ids() == [TOTAL_TOOL]
    if case == "diagnose":
        assert "run_query" not in [
            t["name"] for t in requests[0]["tools"][0]["functionDeclarations"]
        ]
        assert len(requests) == 1
    else:
        assert len(requests) == (3 if case == "all_allowed" else 2)
    if case == "all_allowed":
        for body in requests[1:]:
            calls = [
                part
                for content in body["contents"]
                for part in content["parts"]
                if "functionCall" in part
            ]
            assert calls == [first, second]


async def test_vertex_text_before_tool_never_enters_session_or_delivery(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "ignored-vertex-preface-7f2c"
    requests: list[dict[str, Any]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx2.Response(
                200, json=_body([{"text": marker}, _call_part(name="order_total")])
            )
        output = body["contents"][-1]["parts"][0]["functionResponse"]["response"]["output"]
        evidence_id = json.loads(output)["evidence_id"]
        answer = {
            "evidence_ids": [evidence_id],
            "inferences": [],
            "clarification": None,
            "advice": None,
        }
        return httpx2.Response(200, json=_body([{"text": json.dumps(answer)}]))

    monkeypatch.setenv(VERTEX.api_key_ref.removeprefix("env:"), FAKE_MODEL_KEY)
    profile = VERTEX.model_copy(update={"data_policy_id": PROFILE.data_policy_id})
    async with open_model(profile, transport=httpx2.MockTransport(respond)) as binding:
        vertex_env = replace(env, binding=binding)
        delivered = await vertex_env.deliver(vertex_env.ctx(turn="mixed-text"), "查合成总数")

    assert len(requests) == 2 and env.tool_ids() == [TOTAL_TOOL]
    assert marker not in json.dumps(requests[1], ensure_ascii=False)
    assert marker not in json.dumps(await env.stored(), ensure_ascii=False)
    assert marker not in delivered.content


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
    upstream = env.scripts.add(f"第三次，{SYNTHETIC_SQL}", upstream_error)
    with all_logs() as records:
        await env.app.run_turn(env.ctx(turn="req-ok"), ok)
        with pytest.raises(TurnError):
            await env.app.run_turn(env.ctx(turn="req-bad"), bad)
        # SDK 自身也记录模型错误；上游错误体（含模型/上游内容）不得出现在任何日志中。
        with pytest.raises(TurnError):
            await env.app.run_turn(env.ctx(turn="req-upstream"), upstream)

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
            ("completed", "-"),
        ],
        "req-bad": [
            ("received", "-"),
            ("storage_ready", "-"),
            ("answered", "-"),
            ("failed", "answer_rejected"),
        ],
        "req-upstream": [("received", "-"), ("storage_ready", "-"), ("failed", "model_failed")],
    }
    assert any(r.name.startswith("openai.agents") and r.levelno >= logging.ERROR for r in records)
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

    # 模型：只接受 open_model 按 Profile 装配的运行绑定，不能另配一个 Model 对象。
    with pytest.raises(TypeError):
        ModelBinding(PROFILE, env.binding.model, opened_by=object())
    with pytest.raises(TypeError):
        Application(
            app_config(),
            model=env.binding.model,  # type: ignore[arg-type]
            engine=env.engine,
            governance=env.governed,
            local_tools={},
            clock=env.clock,
        )

    # MCP 接入使用另一套治理（另一授权来源）：拒绝装配，远端零请求。
    other = GovernedTools(store(env.engine, Grants(), env.clock, env.governed.catalog))
    requests = len(env.fixture.recorder.requests)
    with pytest.raises(ValueError, match="治理"):
        Application(
            app_config(),
            model=env.binding,
            engine=env.engine,
            governance=env.governed,
            local_tools={},
            mcp=MCPIntegration([mcp_config(env.fixture.url, tools=("lookup",))], other),
            clock=env.clock,
        )
    assert len(env.fixture.recorder.requests) == requests

    unknown = PROFILE.model_copy(update={"data_policy_id": "unknown"})
    async with open_model(unknown, transport=env.scripts.transport()) as b:
        with pytest.raises(ValueError, match="数据策略"):
            Application(
                app_config(),
                model=b,
                engine=env.engine,
                governance=env.governed,
                local_tools={},
                clock=env.clock,
            )
    with pytest.raises(ValueError, match="未登记"):
        env.application(
            app_config(purposes={"query": frozenset({"local/unknown"}), "diagnose": frozenset()})
        )
    with pytest.raises(ValueError, match="local/"):
        env.application(local_tools={(LOOKUP, FIXTURE_TARGET): env.adapter.execute})
    assert env.scripts.calls == {}


def _with_turns(ctx: RunContext, limit: int) -> RunContext:
    return ctx.model_copy(update={"budget": ctx.budget.model_copy(update={"max_turns": limit})})


async def test_turn_limit_logs_governed_counts_and_no_content(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.adapter.truncated = True
    message = env.scripts.add(
        "计数 " + SYNTHETIC_SQL,
        lambda call: [function_call("order_total", {"region": 1}, call_id="bad")],
        tool_call("order_total", region="east"),
    )
    original = Runner.run

    async def run_without_recovery(*args: Any, **kwargs: Any) -> Any:
        kwargs.pop("error_handlers", None)
        return await original(*args, **kwargs)

    monkeypatch.setattr(Runner, "run", run_without_recovery)
    ctx = _with_turns(env.ctx(turn="counted", max_tool_calls=1), 2)
    with all_logs() as records, pytest.raises(TurnError) as failed:
        await env.app.run_turn(ctx, message)
    assert failed.value.reason == "turn_limit"
    output = "\n".join(r.getMessage() for r in records)
    assert "tool_calls=2 executed=1 truncated=1 rejected=1" in output
    assert SYNTHETIC_SQL not in output and MODEL_SECRET not in output
    assert env.tool_ids() == [TOTAL_TOOL]
    assert await env.stored() == []


@pytest.mark.parametrize("code", list(StarRocksErrorCode))
async def test_starrocks_tool_failure_logs_only_fixed_error_code(
    env: Env, code: StarRocksErrorCode
) -> None:
    calls: list[object] = []

    async def fail(request: Any) -> Any:
        calls.append(request)
        raise StarRocksError(code)

    app = env.application(local_tools={(TOTAL_TOOL, TARGET): fail})
    message = env.scripts.add("工具失败 " + SYNTHETIC_SQL, tool_call("order_total", region="east"))
    with all_logs() as records, pytest.raises(TurnError) as failed:
        await app.run_turn(env.ctx(turn="tool-code"), message)
    output = "\n".join(r.getMessage() for r in records)
    assert failed.value.reason == "tool_failed"
    assert f"tool_error={code.value}" in output
    assert SYNTHETIC_SQL not in output and MODEL_SECRET not in output
    assert len(calls) == 1 and await env.stored() == []


async def test_unknown_tool_failure_logs_other_without_exception_text(env: Env) -> None:
    async def fail(request: Any) -> Any:
        raise RuntimeError(MODEL_SECRET)

    app = env.application(local_tools={(TOTAL_TOOL, TARGET): fail})
    message = env.scripts.add(
        "未知工具失败 " + SYNTHETIC_SQL, tool_call("order_total", region="east")
    )
    with all_logs() as records, pytest.raises(TurnError) as failed:
        await app.run_turn(env.ctx(turn="tool-other"), message)
    output = "\n".join(r.getMessage() for r in records)
    assert failed.value.reason == "tool_failed"
    assert "tool_error=other" in output
    assert MODEL_SECRET not in output and SYNTHETIC_SQL not in output


async def test_mcp_failure_logs_fixed_category_without_remote_text(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = next(iter(env.mcp._sources.values()))
    assert source.snapshot is not None
    server = source.snapshot.server
    calls = 0

    async def fail(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise RuntimeError(MODEL_SECRET)

    monkeypatch.setattr(server, "call_tool", fail)
    message = env.scripts.add("远端失败 " + SYNTHETIC_SQL, tool_call("fixture__lookup", key="k1"))
    with all_logs() as records, pytest.raises(TurnError) as failed:
        await env.app.run_turn(env.ctx(turn="mcp-code"), message)
    output = "\n".join(r.getMessage() for r in records)
    assert failed.value.reason == "tool_failed"
    assert "tool_error=mcp_error" in output
    assert MODEL_SECRET not in output and SYNTHETIC_SQL not in output
    assert calls == 1 and await env.stored() == []


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("mixed", "mixed_kinds"),
        ("missing", "missing_evidence"),
        ("duplicate", "duplicate_evidence"),
        ("unknown", "unknown_evidence"),
        ("inference", "inference_unlinked"),
        ("unavailable", "evidence_unavailable"),
    ],
)
async def test_answer_rejection_logs_exact_fixed_code(env: Env, case: str, code: str) -> None:
    seed = env.scripts.add("取得依据", tool_call("order_total", region="east"), cite())
    evidence_id = (await env.app.run_turn(env.ctx(turn="seed"), seed)).answer.evidence_ids[0]

    def invalid(call: ModelCall) -> list[Any]:
        if case == "unavailable":
            env.grants.revoke_all()
        if case == "mixed":
            return answer([evidence_id], "", clarification=MODEL_SECRET)
        if case == "missing":
            return answer([], "")
        if case == "duplicate":
            return answer([evidence_id, evidence_id], "")
        if case == "unknown":
            return answer(["ev_" + "0" * 32], "")
        if case == "inference":
            return answer(
                [evidence_id], "", inferences=[{"text": MODEL_SECRET, "evidence_ids": ["ev_bad"]}]
            )
        return answer([evidence_id], "")

    message = env.scripts.add("追问 " + SYNTHETIC_SQL, invalid)
    with all_logs() as records, pytest.raises(TurnError) as failed:
        await env.app.run_turn(env.ctx(turn="rejected"), message)
    output = "\n".join(r.getMessage() for r in records)
    assert failed.value.reason == "answer_rejected"
    assert f"answer_reject={code}" in output
    assert MODEL_SECRET not in output and SYNTHETIC_SQL not in output
    assert await env.state() == ("active", 1)


async def test_uncited_query_logs_own_rejection_code(env: Env) -> None:
    message = env.scripts.add(
        "未引用查询 " + SYNTHETIC_SQL,
        tool_call("run_query", region="east"),
        clarify(MODEL_SECRET),
    )
    with all_logs() as records, pytest.raises(TurnError) as failed:
        await env.app.run_turn(env.ctx(turn="uncited"), message)
    output = "\n".join(r.getMessage() for r in records)
    assert failed.value.reason == "answer_rejected"
    assert "answer_reject=uncited_query" in output
    assert MODEL_SECRET not in output and SYNTHETIC_SQL not in output
    assert env.tool_ids() == [QUERY_TOOL] and await env.stored() == []


async def test_max_turns_gets_one_tool_free_final_answer_from_visible_evidence(env: Env) -> None:
    def finish(call: ModelCall) -> list[Any]:
        assert call.tools == []
        assert len(evidence_in(call)) == 2
        return answer(evidence_in(call), "步骤已用完，以下基于已有结果")

    message = env.scripts.add(
        "查询后收尾",
        tool_call("order_total", region="east"),
        tool_call("order_total", region="west"),
        finish,
    )
    ctx = _with_turns(env.ctx(turn="rescued", max_tool_calls=2), 2)
    result = await env.app.run_turn(ctx, message)
    assert len(result.answer.evidence_ids) == 2
    assert "步骤已用完" in (await env.governed.evidence.validate_answer(result, ctx)).content
    assert env.tool_ids() == [TOTAL_TOOL, TOTAL_TOOL]
    assert len(env.scripts.calls[message]) == 3
    assert await env.state() == ("active", 1)


async def test_max_turns_final_prompt_lists_all_current_query_evidence(env: Env) -> None:
    final_instructions: list[str] = []

    def finish(call: ModelCall) -> list[Any]:
        final_instructions.append(call.instructions)
        return answer(evidence_in(call), "只根据已有查询结果回答")

    message = env.scripts.add(
        "先汇总再做两次查询后收尾",
        tool_call("order_total", region="east"),
        tool_call("run_query", region="east"),
        tool_call("run_query", region="west"),
        finish,
    )
    ctx = _with_turns(env.ctx(turn="query-prompt", max_tool_calls=3), 3)
    result = await env.app.run_turn(ctx, message)
    ids = result.answer.evidence_ids
    assert len(ids) == 3 and len(set(ids)) == 3
    assert len(final_instructions) == 1
    assert "本轮查询证据编号" in final_instructions[0]
    assert "、".join(ids[1:]) in final_instructions[0]
    assert ids[0] not in final_instructions[0]
    assert env.tool_ids() == [TOTAL_TOOL, QUERY_TOOL, QUERY_TOOL]


async def test_max_turns_partial_query_citation_keeps_rejection_and_rescue_counts(
    env: Env,
) -> None:
    env.adapter.truncated = True

    def finish(call: ModelCall) -> list[Any]:
        ids = evidence_in(call)
        assert len(ids) == 2
        return answer(ids[:1], MODEL_SECRET)

    message = env.scripts.add(
        "部分引用 " + SYNTHETIC_SQL,
        tool_call("run_query", region="east"),
        tool_call("run_query", region="west"),
        finish,
    )
    ctx = _with_turns(env.ctx(turn="rescue-partial", max_tool_calls=2), 2)
    with all_logs() as records, pytest.raises(TurnError) as failed:
        await env.app.run_turn(ctx, message)

    output = "\n".join(r.getMessage() for r in records if r.name == "xiaowei.app")
    assert failed.value.reason == "answer_rejected"
    assert "reason=answer_rejected" in output and "answer_reject=uncited_query" in output
    assert "limit_rescue=failed" in output
    assert "tool_calls=2 executed=2 truncated=2 rejected=0" in output
    assert all(_STAGE.fullmatch(r.getMessage()) for r in records if r.name == "xiaowei.app")
    assert MODEL_SECRET not in "\n".join(r.getMessage() for r in records)
    assert SYNTHETIC_SQL not in "\n".join(r.getMessage() for r in records)
    assert env.tool_ids() == [QUERY_TOOL, QUERY_TOOL] and await env.stored() == []


async def test_max_turns_tool_call_during_final_keeps_model_error_and_rescue_counts(
    env: Env,
) -> None:
    env.adapter.truncated = True
    message = env.scripts.add(
        "收尾时仍调工具 " + SYNTHETIC_SQL,
        tool_call("run_query", region="east"),
        lambda call: [
            function_call("run_query", {"region": MODEL_SECRET}, call_id="final-unavailable")
        ],
    )
    ctx = _with_turns(env.ctx(turn="rescue-extra-tool", max_tool_calls=1), 1)
    with all_logs() as records, pytest.raises(TurnError) as failed:
        await env.app.run_turn(ctx, message)

    output = "\n".join(r.getMessage() for r in records if r.name == "xiaowei.app")
    assert failed.value.reason == "model_failed"
    assert "reason=model_failed" in output and "error_kind=invalid_output" in output
    assert "limit_rescue=failed" in output
    assert "tool_calls=1 executed=1 truncated=1 rejected=0" in output
    assert all(_STAGE.fullmatch(r.getMessage()) for r in records if r.name == "xiaowei.app")
    assert MODEL_SECRET not in "\n".join(r.getMessage() for r in records)
    assert SYNTHETIC_SQL not in "\n".join(r.getMessage() for r in records)
    assert env.tool_ids() == [QUERY_TOOL] and await env.stored() == []


async def test_max_turns_final_answer_still_rejects_forged_evidence(env: Env) -> None:
    message = env.scripts.add(
        "收尾伪造",
        tool_call("order_total", region="east"),
        lambda call: answer(["ev_forged"], MODEL_SECRET),
    )
    ctx = _with_turns(env.ctx(turn="rescue-rejected", max_tool_calls=1), 1)
    with all_logs() as records, pytest.raises(TurnError) as failed:
        await env.app.run_turn(ctx, message)
    assert failed.value.reason == "answer_rejected"
    assert env.tool_ids() == [TOTAL_TOOL]
    assert len(env.scripts.calls[message]) == 2
    assert await env.stored() == []
    assert MODEL_SECRET not in "\n".join(r.getMessage() for r in records)


async def test_max_turns_without_evidence_stays_a_failure(env: Env) -> None:
    message = env.scripts.add(
        "没有证据 " + SYNTHETIC_SQL,
        lambda call: [function_call("order_total", {"region": 1}, call_id="bad")],
    )
    ctx = _with_turns(env.ctx(turn="no-evidence"), 1)
    with all_logs() as records, pytest.raises(TurnError) as failed:
        await env.app.run_turn(ctx, message)
    output = "\n".join(r.getMessage() for r in records)
    assert failed.value.reason == "turn_limit"
    assert "tool_calls=1 executed=0 truncated=0 rejected=1" in output
    assert len(env.scripts.calls[message]) == 1
    assert env.adapter.calls == [] and await env.stored() == []
    assert SYNTHETIC_SQL not in output


async def test_vertex_limit_rescue_replays_signed_call_without_offering_tools(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, Any]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx2.Response(200, json=_body([_call_part(name="order_total")]))
        assert all(not tool.get("functionDeclarations") for tool in body.get("tools", []))
        evidence_ids = [
            json.loads(part["functionResponse"]["response"]["output"])["evidence_id"]
            for content in body["contents"]
            for part in content["parts"]
            if "functionResponse" in part
        ]
        assert len(evidence_ids) == 1
        final = {
            "evidence_ids": evidence_ids,
            "inferences": [],
            "clarification": None,
            "advice": None,
        }
        return httpx2.Response(200, json=_body([{"text": json.dumps(final)}]))

    monkeypatch.setenv(VERTEX.api_key_ref.removeprefix("env:"), FAKE_MODEL_KEY)
    profile = VERTEX.model_copy(update={"data_policy_id": PROFILE.data_policy_id})
    async with open_model(profile, transport=httpx2.MockTransport(respond)) as binding:
        vertex_env = replace(env, binding=binding)
        ctx = _with_turns(vertex_env.ctx(turn="vertex-rescue", max_tool_calls=1), 1)
        result = await vertex_env.app.run_turn(ctx, "查合成总数")
        delivery = await vertex_env.app.evidence.validate_answer(result, ctx)

    assert len(requests) == 2 and env.tool_ids() == [TOTAL_TOOL]
    assert len(result.answer.evidence_ids) == 1
    assert "步骤已用完" in delivery.content
    assert await env.state() == ("active", 1)
