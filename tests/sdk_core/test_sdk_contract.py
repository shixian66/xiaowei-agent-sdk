"""Task 1：锁定版 Agents SDK 的公开扩展点契约。

全部使用真实 SDK ``Runner``；模型是 SDK 公开的 ``agents.testing.ScriptedModel``，不访问真实
模型 API。PostgreSQL 与 MCP 只连接 loopback 上的隔离测试实例。
"""

import json
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from agents import Agent, FunctionTool, Runner, function_tool, set_tracing_disabled
from agents.extensions.memory import SQLAlchemySession
from agents.mcp import MCPServerStreamableHttp
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext
from agents.tracing import TracingProcessor, flush_traces, set_trace_processors
from agents.tracing.processors import default_exporter, default_processor
from pydantic import BaseModel, SecretStr
from sqlalchemy.engine import URL

from xiaowei.config import configure_runtime
from xiaowei.storage import (
    StorageNotInitializedError,
    StorageUnavailableError,
    check_storage,
    initialize_storage,
    open_engine,
)


class TotalAnswer(BaseModel):
    total: int
    note: str


def _items_of_type(items: Sequence[Any], kind: str) -> list[dict[str, Any]]:
    """按 Responses 输入项类型筛选；SDK 输入项是 TypedDict 联合，这里按普通字典读取。"""
    return [dict(item) for item in items if item.get("type") == kind]


def _tool_outputs(call: ModelCall) -> list[dict[str, Any]]:
    """模型本次收到的 function_call_output 项。"""
    assert isinstance(call.input, list)
    return _items_of_type(call.input, "function_call_output")


def _final_answer(total: int) -> ModelStep:
    return ModelStep(output=[assistant_message(json.dumps({"total": total, "note": "ok"}))])


def _secret(url: URL) -> SecretStr:
    return SecretStr(url.render_as_string(hide_password=False))


def _synthetic_total_agent(model: ScriptedModel, executed: list[str]) -> Agent[None]:
    @function_tool
    def synthetic_total(region: str) -> str:
        """返回合成地区的订单总数。"""
        executed.append(region)
        return json.dumps({"region": region, "total": 100})

    return Agent(
        name="sdk-contract",
        instructions="使用工具回答。",
        model=model,
        tools=[synthetic_total],
        output_type=TotalAnswer,
    )


async def test_real_runner_calls_tool_and_returns_typed_answer() -> None:
    executed: list[str] = []
    model = ScriptedModel(
        [
            [function_call("synthetic_total", {"region": "east"}, call_id="call-1")],
            _final_answer(100),
        ]
    )
    agent = _synthetic_total_agent(model, executed)

    result = await Runner.run(agent, "东区订单总数？", max_turns=4)

    assert executed == ["east"]
    assert isinstance(result.final_output, TotalAnswer)
    assert result.final_output.total == 100
    # 工具返回后 Runner 确实发起了下一轮模型调用，并把工具结果交给模型。
    assert len(model.calls) == 2
    (output,) = _tool_outputs(model.calls[1])
    assert output["call_id"] == "call-1"
    assert '"total": 100' in output["output"]
    model.assert_complete()


@pytest.mark.loopback
async def test_postgres_session_public_roundtrip(postgres_url: URL) -> None:
    session_id = "web:operator:session-1"
    executed: list[str] = []

    async with open_engine(_secret(postgres_url)) as engine:
        await initialize_storage(engine)
        await check_storage(engine)
        first = ScriptedModel(
            [
                [function_call("synthetic_total", {"region": "east"}, call_id="call-1")],
                _final_answer(100),
            ]
        )
        session = SQLAlchemySession(session_id, engine=engine)
        await Runner.run(_synthetic_total_agent(first, executed), "东区订单总数？", session=session)

    # 重建引擎与 Session 对象后，从 PostgreSQL 回放同一会话。
    async with open_engine(_secret(postgres_url)) as engine:
        await check_storage(engine)
        session = SQLAlchemySession(session_id, engine=engine)
        items = await session.get_items()
        stored_calls = _items_of_type(items, "function_call")
        stored_outputs = _items_of_type(items, "function_call_output")
        assert [c["call_id"] for c in stored_calls] == ["call-1"]
        assert [o["call_id"] for o in stored_outputs] == ["call-1"]

        followup = ScriptedModel([_final_answer(100)])
        result = await Runner.run(
            _synthetic_total_agent(followup, executed), "刚才的总数是多少？", session=session
        )

    assert isinstance(result.final_output, TotalAnswer)
    assert executed == ["east"]  # 追问没有重跑工具
    replayed = followup.calls[0].input
    assert isinstance(replayed, list)
    assert [o["call_id"] for o in _tool_outputs(followup.calls[0])] == ["call-1"]
    assert [c["call_id"] for c in _items_of_type(replayed, "function_call")] == ["call-1"]


@pytest.mark.loopback
async def test_storage_unavailable_or_uninitialized_is_rejected(postgres_url: URL) -> None:
    model = ScriptedModel([_final_answer(1)])

    async def gated_turn(url: SecretStr) -> None:
        async with open_engine(url) as engine:
            await check_storage(engine)
            await Runner.run(_synthetic_total_agent(model, []), "hi")

    # 数据库存在但未执行显式初始化：普通路径不自动建表。
    with pytest.raises(StorageNotInitializedError):
        await gated_turn(_secret(postgres_url))

    # 连接不可用：端口 1 在 loopback 上无服务。错误不携带地址或凭据。
    unreachable = postgres_url.set(port=1, password="pw-must-not-leak")  # noqa: S106
    with pytest.raises(StorageUnavailableError) as excinfo:
        await gated_turn(_secret(unreachable))
    assert "pw-must-not-leak" not in str(excinfo.value)
    assert "127.0.0.1" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None

    assert model.calls == ()


@dataclass(frozen=True)
class _Scope:
    """测试用的最小可信范围；正式 RunContext 在 Task 2 定义。"""

    allowed_tools: frozenset[str]


@pytest.mark.loopback
async def test_public_mcp_interception_contract(mcp_fixture: Any) -> None:
    requested_methods: list[str] = []
    factory_timeouts: list[Any] = []

    async def record_request(request: httpx2.Request) -> None:
        body = request.content
        if body:
            requested_methods.append(json.loads(body).get("method", ""))

    def client_factory(
        headers: dict[str, str] | None = None, timeout: Any = None, auth: Any = None
    ) -> httpx2.AsyncClient:
        # 公开的 transport 扩展点：由应用决定超时、重定向与请求观察。
        factory_timeouts.append(timeout)
        return httpx2.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=False,
            trust_env=False,
            event_hooks={"request": [record_request]},
        )

    server = MCPServerStreamableHttp(
        params={
            "url": mcp_fixture.url,
            "timeout": 5,
            "sse_read_timeout": 5,
            "httpx_client_factory": client_factory,
        },
        name="fixture",
        cache_tools_list=True,
        client_session_timeout_seconds=5,
        max_retry_attempts=0,
    )

    async with server:
        (remote,) = [t for t in await server.list_tools() if t.name == "lookup"]

        async def governed_lookup(ctx: ToolContext[_Scope], raw_arguments: str) -> str:
            # 调用前检查：拒绝时不向 MCP Server 发出任何请求。
            if "fixture/lookup" not in ctx.context.allowed_tools:
                return "拒绝：当前范围不允许调用 fixture/lookup"
            arguments = json.loads(raw_arguments)
            result = await server.call_tool(remote.name, arguments)
            # 返回后过滤：只把获准字段交给模型。
            structured = result.structured_content or {}
            return json.dumps({"key": structured["key"], "value": structured["value"]})

        tool = FunctionTool(
            name="fixture_lookup",
            description=remote.description or "",
            params_json_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
            on_invoke_tool=governed_lookup,
        )

        def run_once(scope: _Scope) -> tuple[ScriptedModel, Any]:
            model = ScriptedModel(
                [
                    [function_call("fixture_lookup", {"key": "k1"}, call_id="mcp-1")],
                    _final_answer(7),
                ]
            )
            agent = Agent[_Scope](
                name="mcp-contract", model=model, tools=[tool], output_type=TotalAnswer
            )
            return model, Runner.run(agent, "查 k1", context=scope)

        requested_methods.clear()
        denied_model, denied_run = run_once(_Scope(allowed_tools=frozenset()))
        await denied_run
        assert mcp_fixture.tool_calls == []
        assert "tools/call" not in requested_methods
        (denied_output,) = _tool_outputs(denied_model.calls[1])
        assert denied_output["output"].startswith("拒绝")

        allowed_model, allowed_run = run_once(_Scope(allowed_tools=frozenset({"fixture/lookup"})))
        await allowed_run
        assert mcp_fixture.tool_calls == [{"key": "k1"}]
        assert requested_methods.count("tools/call") == 1
        (allowed_output,) = _tool_outputs(allowed_model.calls[1])
        assert json.loads(allowed_output["output"]) == {"key": "k1", "value": 7}
        assert "fixture-private-note" not in json.dumps(allowed_model.calls[1].input)

    assert factory_timeouts, "SDK 未使用应用提供的 httpx_client_factory"
    assert all(t is not None for t in factory_timeouts)


class _RecordingProcessor(TracingProcessor):
    """记录 SDK 交给 trace 处理器的全部 trace/span。"""

    def __init__(self) -> None:
        self.events: list[str] = []

    def on_trace_start(self, trace: Any) -> None:
        self.events.append("trace")

    def on_trace_end(self, trace: Any) -> None:
        self.events.append("trace_end")

    def on_span_start(self, span: Any) -> None:
        self.events.append("span")

    def on_span_end(self, span: Any) -> None:
        self.events.append("span_end")

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


@pytest.fixture
def trace_exports(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """观察 SDK 默认导出器实际会发往后端的内容；测试后恢复关闭状态。"""
    exports: list[Any] = []
    monkeypatch.setattr(default_exporter(), "export", lambda items: exports.extend(items))
    yield exports
    configure_runtime()


async def _run_tool_turn() -> None:
    model = ScriptedModel(
        [
            [function_call("synthetic_total", {"region": "east"}, call_id="call-1")],
            _final_answer(100),
        ]
    )
    await Runner.run(_synthetic_total_agent(model, []), "东区订单总数？")


async def test_default_tracing_has_no_export(trace_exports: list[Any]) -> None:
    # 对照组：SDK 默认处理器 + 开启 tracing 时，同一轮会产生可导出的 trace。
    set_tracing_disabled(False)
    set_trace_processors([default_processor()])
    await _run_tool_turn()
    flush_traces()
    assert trace_exports, "对照组没有产生导出，测试无法证明 tracing 被关闭"

    trace_exports.clear()
    configure_runtime()
    await _run_tool_turn()
    flush_traces()
    assert trace_exports == []

    # 两项措施各自有效：处理器被重新挂回时，关闭的 tracing 仍不产生任何 trace……
    recorder = _RecordingProcessor()
    set_trace_processors([recorder, default_processor()])
    await _run_tool_turn()
    flush_traces()
    assert recorder.events == []
    assert trace_exports == []

    # ……tracing 被重新打开时，默认导出处理器已不在 provider 上。
    configure_runtime()
    set_tracing_disabled(False)
    await _run_tool_turn()
    flush_traces()
    assert trace_exports == []


def test_new_package_does_not_load_legacy_runtime() -> None:
    code = (
        "import sys, xiaowei.config, xiaowei.storage; "
        "assert not [m for m in sys.modules if m.startswith('xiaowei_agent')]"
    )
    subprocess.run([sys.executable, "-c", code], check=True)  # noqa: S603
