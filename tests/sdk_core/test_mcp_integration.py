"""Task 4：最小 MCP Client Integration。

真 Runner + SDK 官方 Streamable HTTP 客户端 + loopback MCP fixture + 隔离的真实 PostgreSQL。
fixture 记录到达的 HTTP 请求与实际执行的工具；拒绝路径断言远端零执行。
"""

import asyncio
import io
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from agents import Agent, Runner, Tool
from agents.exceptions import ModelBehaviorError
from agents.extensions.memory import SQLAlchemySession
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message, function_call
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests.sdk_core.mcp_fixture import (
    FAKE_TOKEN,
    FORGED_EVIDENCE,
    INJECTION,
    PRIVATE,
    gzipped,
    mcp_app,
    redirecting,
    rerouted,
    serve,
)
from tests.sdk_core.synthetic_tools import (
    CONTRACTS,
    POLICIES,
    TARGET,
    TOTAL_TOOL,
    Clock,
    Grants,
    RecordingAdapter,
    context,
    ready_engine,
    sdk_tool,
    store,
)

from xiaowei.config import MCPServerConfig
from xiaowei.evidence import EvidenceStore
from xiaowei.governance import GovernedTools, Projection, ToolCatalog, ToolPolicy
from xiaowei.mcp import MCPIntegration
from xiaowei.models import AgentAnswer, RunContext, ToolContract
from xiaowei.session import PolicySession, SessionInputPolicy, SessionLimits

FIXTURE_TARGET = "fixture-target"
TOKEN_ENV = "XIAOWEI_TEST_MCP_TOKEN"  # noqa: S105 - 环境变量名，不是凭据
REGISTERED = (
    "lookup",
    "text_lookup",
    "drifted",
    "missing",
    "wrong_type",
    "stringly",
    "picture",
    "failing",
    "slow",
    "huge",
)


class KeyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str


class KeyResult(BaseModel):
    # 默认的宽松配置：严格校验必须由 MCP 接入执行，不依赖策略作者的模型配置。
    key: str
    value: int
    note: str


FIXTURE_POLICY = ToolPolicy(
    policy_id="fixture.key",
    arguments=KeyArgs,
    projections={
        "model": Projection(fields=("key", "value", "note"), max_bytes=2000),
        "session": Projection(fields=("key", "value", "note"), max_bytes=2000),
        "web": Projection(fields=("key", "value", "note"), max_bytes=4000),
        "feishu": Projection(fields=("key", "value"), max_bytes=1000),
    },
    result=KeyResult,
)


class MapArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    filters: dict[str, int]


MAP_POLICY = ToolPolicy(
    policy_id="fixture.map",
    arguments=MapArgs,
    projections=FIXTURE_POLICY.projections,
    result=KeyResult,
)


def fixture_contracts(server_id: str = "fixture") -> tuple[ToolContract, ...]:
    return tuple(
        ToolContract(
            tool_id=f"{server_id}/{name}",
            target_id=FIXTURE_TARGET,
            input_schema=KeyArgs.model_json_schema(),
            policy_id="fixture.key",
            description="按 key 查询合成记录",
        )
        for name in REGISTERED
    )


def mcp_catalog(*server_ids: str, extra: Sequence[ToolContract] = ()) -> ToolCatalog:
    contracts = [c for sid in server_ids or ("fixture",) for c in fixture_contracts(sid)]
    return ToolCatalog((*CONTRACTS, *contracts, *extra), (*POLICIES, FIXTURE_POLICY))


def config(
    url: str,
    *,
    server_id: str = "fixture",
    tools: Sequence[str] = REGISTERED,
    policy: str = "fixture.key",
    auth_ref: str | None = None,
    timeout: float = 5.0,
    max_bytes: int = 64_000,
) -> MCPServerConfig:
    return MCPServerConfig(
        server_id=server_id,
        url=url,
        auth_ref=auth_ref,
        timeout_seconds=timeout,
        max_response_bytes=max_bytes,
        allowed_tools=dict.fromkeys(tools, policy),
    )


def mcp_context(
    *server_ids: str, max_tool_calls: int = 5, local: bool = True, channel: Any = "web"
) -> RunContext:
    tools = {f"{sid}/{name}" for sid in server_ids or ("fixture",) for name in REGISTERED}
    if local:
        tools.add(TOTAL_TOOL)
    return context(
        channel=channel,
        tools=frozenset(tools),
        targets=frozenset({TARGET, FIXTURE_TARGET}),
        max_tool_calls=max_tool_calls,
    )


@dataclass
class Core:
    engine: AsyncEngine
    grants: Grants
    clock: Clock
    evidence: EvidenceStore
    governed: GovernedTools
    adapter: RecordingAdapter
    last_output: object = None

    def integration(self, *configs: MCPServerConfig) -> MCPIntegration:
        return MCPIntegration(configs, self.governed, clock=self.clock)

    def agent(self, model: ScriptedModel, tools: Sequence[Tool]) -> Agent[RunContext]:
        return Agent[RunContext](
            name="mcp", model=model, tools=list(tools), output_type=AgentAnswer
        )

    async def run(
        self,
        tools: Sequence[Tool],
        steps: Sequence[Any],
        ctx: RunContext,
        session: PolicySession | None = None,
    ) -> ScriptedModel:
        model = ScriptedModel(list(steps))
        result = await Runner.run(self.agent(model, tools), "查 k1", context=ctx, session=session)
        self.last_output = result.final_output
        return model

    async def dump(self) -> str:
        """应用表与 SDK 表的全部内容，用于断言禁止内容没有落库。"""
        async with self.engine.connect() as conn:
            parts = []
            for table in ("xiaowei_evidence", "agent_messages", "xiaowei_session"):
                rows = await conn.execute(text(f"SELECT * FROM {table}"))  # noqa: S608
                parts.extend(json.dumps(list(r), default=str, ensure_ascii=False) for r in rows)
            return "\n".join(parts)

    async def evidence_count(self) -> int:
        async with self.engine.connect() as conn:
            return (await conn.execute(text("SELECT count(*) FROM xiaowei_evidence"))).scalar_one()


@asynccontextmanager
async def core(url: URL, catalog: ToolCatalog | None = None) -> AsyncIterator[Core]:
    async with ready_engine(url) as engine:
        grants, clock = Grants(), Clock()
        tools = catalog or mcp_catalog()
        evidence = store(engine, grants, clock, tools)
        governed = GovernedTools(tools, evidence, authorize=grants)
        grants.grant("alice", TOTAL_TOOL)
        grants.grant(
            "alice",
            *(c.tool_id for c in tools.contracts if c.target_id == FIXTURE_TARGET),
            target=FIXTURE_TARGET,
        )
        yield Core(engine, grants, clock, evidence, governed, RecordingAdapter())


def call(name: str, key: str = "k1", call_id: str = "mcp-1") -> list[Any]:
    return [function_call(name, {"key": key}, call_id=call_id)]


def outputs(call_: ModelCall) -> list[str]:
    assert isinstance(call_.input, list)
    return [i["output"] for i in call_.input if i.get("type") == "function_call_output"]


def seen(model: ScriptedModel) -> str:
    return json.dumps([c.input for c in model.calls], ensure_ascii=False, default=str)


def cite() -> ModelStep:
    def respond(call_: ModelCall) -> Any:
        evidence_id = json.loads(outputs(call_)[-1])["evidence_id"]
        answer = {
            "evidence_ids": [evidence_id],
            "inferences": [{"text": "记录正常", "evidence_ids": [evidence_id]}],
            "clarification": None,
        }
        return [assistant_message(json.dumps(answer, ensure_ascii=False))]

    return ModelStep.respond(respond)


def clarify() -> ModelStep:
    answer = {"evidence_ids": [], "inferences": [], "clarification": "暂时无法取得数据"}
    return ModelStep(output=[assistant_message(json.dumps(answer, ensure_ascii=False))])


def names(tools: Sequence[Tool]) -> set[str]:
    return {t.name for t in tools}


def unreachable_engine() -> AsyncEngine:
    """不会被使用的引擎：只为构造 EvidenceStore，创建时不建立连接。"""
    return create_async_engine("postgresql+asyncpg://nobody@127.0.0.1:1/none")


# --- 空配置 -------------------------------------------------------------------------------


async def test_empty_config_performs_no_network() -> None:
    # 未标记 loopback：pytest-socket 禁止一切连接，任何网络访问都会让用例失败。
    tools = mcp_catalog()
    evidence = store(unreachable_engine(), Grants(), Clock(), tools)
    governed = GovernedTools(tools, evidence, authorize=Grants())
    async with MCPIntegration((), governed) as integration:
        assert integration.tools_for(mcp_context()) == []


# --- 正常路径 -----------------------------------------------------------------------------


@pytest.mark.loopback
async def test_sdk_runner_calls_governed_mcp(postgres_url: URL) -> None:
    with serve(mcp_app()) as fixture:
        async with core(postgres_url) as c, c.integration(config(fixture.url)) as integration:
            ctx = mcp_context()
            tools = integration.tools_for(ctx)
            assert "fixture__lookup" in names(tools)
            assert "fixture__text_lookup" in names(tools)

            model = await c.run(tools, [call("fixture__lookup"), cite()], ctx)
            assert fixture.recorder.tool_calls == [("lookup", {"key": "k1"})]
            (content,) = outputs(model.calls[1])
            envelope = json.loads(content)
            assert envelope["evidence_id"].startswith("ev_")
            assert envelope["data"] == {"key": "k1", "value": 7, "note": INJECTION}

            answer = c.last_output
            assert isinstance(answer, AgentAnswer)
            delivery = await c.evidence.validate_answer(answer, ctx)
            assert "7" in delivery.content

            # 只有一段 JSON 文本、没有结构化内容的结果同样按登记契约接收。
            text_model = await c.run(
                tools, [call("fixture__text_lookup", call_id="mcp-2"), cite()], ctx
            )
            (text_content,) = outputs(text_model.calls[1])
            assert json.loads(text_content)["data"] == {"key": "k1", "value": 8, "note": "文本"}
            assert fixture.recorder.tool_calls[-1] == ("text_lookup", {"key": "k1"})


# --- 撤权、范围与预算 -----------------------------------------------------------------------


@pytest.mark.loopback
async def test_revoked_mcp_tool_has_zero_calls(postgres_url: URL) -> None:
    with serve(mcp_app()) as fixture:
        async with core(postgres_url) as c, c.integration(config(fixture.url)) as integration:
            recorder = fixture.recorder

            # 本轮范围不含该工具：不展示；模型强行调用时 SDK 找不到工具，远端零请求。
            narrow = context(
                tools=frozenset({TOTAL_TOOL}), targets=frozenset({TARGET, FIXTURE_TARGET})
            )
            assert names(integration.tools_for(narrow)) == set()
            before = len(recorder.requests)
            with pytest.raises(ModelBehaviorError):
                await c.run(
                    [*integration.tools_for(narrow), sdk_tool(c.governed, c.adapter, TOTAL_TOOL)],
                    [call("fixture__lookup"), clarify()],
                    narrow,
                )
            assert recorder.tool_calls == []
            assert len(recorder.requests) == before

            # 展示之后撤权：调用前复核当前权限，远端零请求。
            ctx = mcp_context()
            tools = integration.tools_for(ctx)
            assert "fixture__lookup" in names(tools)
            c.grants.revoke_all()
            model = await c.run(tools, [call("fixture__lookup"), clarify()], ctx)
            assert recorder.tool_calls == []
            assert len(recorder.requests) == before
            (denied,) = outputs(model.calls[1])
            assert "当前无权调用该工具" in denied
            assert await c.evidence_count() == 0

            # 参数不是 JSON 对象：在治理之前拒绝，远端零请求。
            malformed = await c.run(
                tools,
                [[function_call("fixture__lookup", "not json", call_id="bad")], clarify()],
                ctx,
            )
            assert "参数不符合工具契约" in outputs(malformed.calls[1])[0]
            assert recorder.tool_calls == []
            assert len(recorder.requests) == before


@pytest.mark.loopback
async def test_injected_text_cannot_raise_budget(postgres_url: URL) -> None:
    with serve(mcp_app()) as fixture:
        async with core(postgres_url) as c, c.integration(config(fixture.url)) as integration:
            ctx = mcp_context(max_tool_calls=1)
            tools = integration.tools_for(ctx)
            model = await c.run(
                tools,
                [
                    call("fixture__lookup", "k1", "mcp-1"),
                    # 远端结果里的注入文字声称预算不限；模型照做也不能多执行一次。
                    call("fixture__lookup", "k2", "mcp-2"),
                    clarify(),
                ],
                ctx,
            )
            assert INJECTION in outputs(model.calls[1])[0]
            assert fixture.recorder.tool_calls == [("lookup", {"key": "k1"})]
            assert "本轮工具调用次数已用完" in outputs(model.calls[2])[1]


# --- 契约核对与名字 -------------------------------------------------------------------------


@pytest.mark.loopback
async def test_unknown_schema_and_collision_fail_closed(postgres_url: URL) -> None:
    with serve(mcp_app()) as fixture:
        # 登记与工具目录不一致时拒绝装配，不连接远端。
        async with core(postgres_url) as c:
            with pytest.raises(ValueError, match="冲突"):
                clash = ToolContract(
                    tool_id="local/fixture__lookup",
                    target_id=TARGET,
                    input_schema=KeyArgs.model_json_schema(),
                    policy_id="fixture.key",
                )
                tools = mcp_catalog(extra=(clash,))
                MCPIntegration(
                    (config(fixture.url),), GovernedTools(tools, c.evidence, authorize=c.grants)
                )
            with pytest.raises(ValueError, match="不一致"):
                MCPIntegration(
                    (config(fixture.url, tools=("purge",)),), c.governed
                )  # purge 不在工具目录
            wrong_policy = config(fixture.url).model_copy(
                update={"allowed_tools": {"lookup": "synthetic.region"}}
            )
            with pytest.raises(ValueError, match="不一致"):
                MCPIntegration((wrong_policy,), c.governed)
            with pytest.raises(ValueError, match="重复"):
                MCPIntegration((config(fixture.url), config(fixture.url)), c.governed)
            no_result = ToolCatalog(
                fixture_contracts(),
                (FIXTURE_POLICY.__class__(**{**FIXTURE_POLICY.__dict__, "result": None}),),
            )
            with pytest.raises(ValueError, match="结果模型"):
                MCPIntegration(
                    (config(fixture.url),), GovernedTools(no_result, c.evidence, authorize=c.grants)
                )
            # SDK 严格 schema 不支持的参数形状（映射）：连接任何 Server 之前拒绝整份登记，
            # 而不是连上之后在构造工具时中止全部 MCP 装配。
            mapped = ToolContract(
                tool_id="maps/mapped",
                target_id=FIXTURE_TARGET,
                input_schema=MapArgs.model_json_schema(),
                policy_id="fixture.map",
            )
            with_map = ToolCatalog((*fixture_contracts(), mapped), (FIXTURE_POLICY, MAP_POLICY))
            with pytest.raises(ValueError, match="schema"):
                MCPIntegration(
                    (
                        config(fixture.url),
                        config(
                            fixture.url, server_id="maps", tools=("mapped",), policy="fixture.map"
                        ),
                    ),
                    GovernedTools(with_map, c.evidence, authorize=c.grants),
                )
            assert fixture.recorder.requests == []

        # 远端改了参数 schema、缺少登记的工具、未登记的“只读”工具：一律不开放。
        async with core(postgres_url) as c, c.integration(config(fixture.url)) as integration:
            ctx = mcp_context()
            exposed = names(integration.tools_for(ctx))
            assert "fixture__lookup" in exposed
            assert "fixture__drifted" not in exposed
            assert "fixture__missing" not in exposed
            assert not any("purge" in name for name in exposed)
            for hidden in ("fixture__drifted", "fixture__purge"):
                with pytest.raises(ModelBehaviorError):
                    await c.run(integration.tools_for(ctx), [call(hidden), clarify()], ctx)
            assert fixture.recorder.tool_calls == []


@pytest.mark.loopback
async def test_same_remote_name_maps_to_its_own_server(postgres_url: URL) -> None:
    with serve(mcp_app()) as first, serve(mcp_app()) as second:
        configs = (config(first.url, server_id="alpha"), config(second.url, server_id="beta"))
        async with (
            core(postgres_url, mcp_catalog("alpha", "beta")) as c,
            c.integration(*configs) as integration,
        ):
            ctx = mcp_context("alpha", "beta")
            tools = integration.tools_for(ctx)
            assert {"alpha__lookup", "beta__lookup"} <= names(tools)
            await c.run(tools, [call("beta__lookup"), cite()], ctx)
            assert first.recorder.tool_calls == []
            assert second.recorder.tool_calls == [("lookup", {"key": "k1"})]
            async with c.engine.connect() as conn:
                tool_id = (
                    await conn.execute(text("SELECT tool_id FROM xiaowei_evidence"))
                ).scalar_one()
            assert tool_id == "beta/lookup"


# --- 认证、端点、期限与关闭 ------------------------------------------------------------------


@pytest.mark.loopback
async def test_auth_timeout_and_shutdown(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(TOKEN_ENV, FAKE_TOKEN)
    ref = f"env:{TOKEN_ENV}"
    with serve(mcp_app(token=FAKE_TOKEN, slow_seconds=3.0)) as secured:
        async with core(postgres_url) as c:
            integration = c.integration(config(secured.url, auth_ref=ref, timeout=0.5))
            async with integration:
                ctx = mcp_context()
                tools = integration.tools_for(ctx)
                model = await c.run(tools, [call("fixture__lookup"), cite()], ctx)
                assert secured.recorder.tool_calls == [("lookup", {"key": "k1"})]
                assert {a for _, _, a in secured.recorder.requests} == {f"Bearer {FAKE_TOKEN}"}

                # 超时：远端已开始执行，结果未知，不自动重试。
                started = time.monotonic()
                timed_out = await c.run(
                    tools, [call("fixture__slow", call_id="mcp-2"), clarify()], ctx
                )
                assert time.monotonic() - started < 2.5
                assert [n for n, _ in secured.recorder.tool_calls].count("slow") == 1
                assert "工具执行失败" in outputs(timed_out.calls[1])[0]
                # 一次超时不能拖垮整个连接：后续调用照常执行。
                await c.run(tools, [call("fixture__lookup", "k2", "mcp-4"), cite()], ctx)
                assert secured.recorder.tool_calls[-1] == ("lookup", {"key": "k2"})

                # 取消：调用中取消本轮，远端调用不重放。
                task = asyncio.create_task(
                    c.run(tools, [call("fixture__slow", call_id="mcp-3"), clarify()], ctx)
                )
                for _ in range(200):
                    if [n for n, _ in secured.recorder.tool_calls].count("slow") == 2:
                        break
                    await asyncio.sleep(0.01)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.sleep(0.6)
                assert [n for n, _ in secured.recorder.tool_calls].count("slow") == 2

            # 关闭后：不再提供工具，没有遗留任务与连接。
            assert integration.tools_for(ctx) == []
            await _until(lambda: secured.open_connections() == 0)
            assert asyncio.all_tasks() == {asyncio.current_task()}

            # 假凭据不进入模型输入、数据库或日志。
            assert FAKE_TOKEN not in seen(model) + seen(timed_out)
            assert FAKE_TOKEN not in await c.dump()
            assert FAKE_TOKEN not in caplog.text

        # 错误或缺失的凭据：该 Server 不可用，只隐藏它的工具，本地工具继续可用。
        with serve(mcp_app()) as open_server:
            for env_value in ("wrong-token", None):
                if env_value is None:
                    monkeypatch.delenv(TOKEN_ENV)
                else:
                    monkeypatch.setenv(TOKEN_ENV, env_value)
                calls_before = len(secured.recorder.tool_calls)
                configs = (
                    config(secured.url, server_id="secured", auth_ref=ref),
                    config(open_server.url, server_id="open"),
                )
                async with (
                    core(postgres_url, mcp_catalog("secured", "open")) as c,
                    c.integration(*configs) as integration,
                ):
                    ctx = mcp_context("secured", "open")
                    exposed = names(integration.tools_for(ctx))
                    assert not any(name.startswith("secured__") for name in exposed)
                    assert "open__lookup" in exposed
                    local = sdk_tool(c.governed, c.adapter, TOTAL_TOOL)
                    await c.run(
                        [local],
                        [[function_call("order_total", {"region": "east"}, call_id="l1")], cite()],
                        ctx,
                    )
                    assert len(c.adapter.calls) == 1
                assert len(secured.recorder.tool_calls) == calls_before
    assert "wrong-token" not in caplog.text


@pytest.mark.loopback
async def test_redirect_never_carries_credentials(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV, FAKE_TOKEN)
    ref = f"env:{TOKEN_ENV}"
    with serve(mcp_app()) as other:
        # 跨源重定向：不跟随，另一端点收不到任何请求。
        with serve(lambda r: redirecting(other.url, r)) as cross:
            async with core(postgres_url) as c:
                async with c.integration(config(cross.url, auth_ref=ref)) as integration:
                    assert integration.tools_for(mcp_context()) == []
            assert other.recorder.requests == []

    # 同源重定向：MCP 客户端会跟随；登记端点之外的目标（不同路径、附加 query、编码不同的
    # 路径）一律不发出，登记端点本身照常带认证（对照）。
    for registered, location in (
        ("/moved", "/mcp"),
        ("/mcp", "/mcp?other-service=1"),
        ("/mcp/tenant-a", "/mcp%2Ftenant-a"),
    ):
        with serve(rerouted(registered, location), path=registered) as same_origin:
            async with core(postgres_url) as c:
                async with c.integration(config(same_origin.url, auth_ref=ref)) as integration:
                    assert integration.tools_for(mcp_context()) == []
            requests = same_origin.recorder.requests
            assert requests
            assert {(t, a) for _, t, a in requests} == {(registered, f"Bearer {FAKE_TOKEN}")}


@pytest.mark.loopback
async def test_compressed_response_is_rejected(postgres_url: URL) -> None:
    # 压缩内容在读取阶段无法按实际字节限额（少量压缩字节可解压成很大的内容），一律拒绝。
    with serve(lambda r: gzipped(mcp_app()(r))) as compressed:
        async with (
            core(postgres_url) as c,
            c.integration(config(compressed.url, max_bytes=20_000)) as integration,
        ):
            assert integration.tools_for(mcp_context()) == []
        assert compressed.recorder.requests


# --- 日志 ---------------------------------------------------------------------------------


@contextmanager
def root_log() -> Iterator[io.StringIO]:
    """应用在 root logger 上配置的处理器看到的全部日志（DEBUG 起，含异常堆栈）。

    不用 caplog：pytest 会把捕获处理器直接挂到不向上传递的 logger 上，看到的不是应用所见。
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


@pytest.mark.loopback
async def test_wire_logs_carry_no_tool_data(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MCP 库在本地过滤之前记录完整的 JSON-RPC 消息（参数与远端结果）；日志不能绕过数据边界。
    monkeypatch.setenv(TOKEN_ENV, FAKE_TOKEN)
    marker = "arg-marker-5c1e"
    with root_log() as log, serve(mcp_app(token=FAKE_TOKEN)) as fixture:
        async with (
            core(postgres_url) as c,
            c.integration(config(fixture.url, auth_ref=f"env:{TOKEN_ENV}")) as integration,
        ):
            ctx = mcp_context()
            tools = integration.tools_for(ctx)
            model = await c.run(tools, [call("fixture__lookup", marker), cite()], ctx)
            await c.run(tools, [call("fixture__failing", marker, "mcp-2"), clarify()], ctx)
    # 对照：调用真实发生，参数经获准字段回到模型。
    assert [name for name, _ in fixture.recorder.tool_calls] == ["lookup", "failing"]
    assert marker in seen(model)
    for forbidden in (marker, PRIVATE, FORGED_EVIDENCE, INJECTION, FAKE_TOKEN, "remote failure"):
        assert forbidden not in log.getvalue()
    # 对照：MCP 库的日志没有被静默丢弃，而是以固定信息转出。
    relayed = "xiaowei.mcp DEBUG MCP 库日志：mcp.client.streamable_http（内容已省略）"
    assert relayed in log.getvalue()


# --- 结果过滤 -----------------------------------------------------------------------------


@pytest.mark.loopback
async def test_mcp_payload_filtered_before_model(postgres_url: URL) -> None:
    with serve(mcp_app()) as fixture:
        async with (
            core(postgres_url) as c,
            c.integration(config(fixture.url, max_bytes=20_000)) as integration,
        ):
            # 本轮 1 次成功调用 + 5 次被拒绝的调用。
            ctx = mcp_context(max_tool_calls=6)
            tools = integration.tools_for(ctx)
            session = PolicySession(
                SQLAlchemySession("s1", engine=c.engine),
                ctx,
                c.evidence,
                "profile-under-test",
                SessionLimits(
                    max_history_turns=5, max_history_bytes=20_000, retention_seconds=7200
                ),
                input_policy=SessionInputPolicy(max_bytes=2000),
                engine=c.engine,
                clock=c.clock,
            )
            model = await c.run(tools, [call("fixture__lookup"), cite()], ctx, session=session)
            await session.commit_validated()
            stored = await c.dump()
            for forbidden in (PRIVATE, FORGED_EVIDENCE):
                assert forbidden not in seen(model)
                assert forbidden not in stored

            # 下一轮从 Session 回放 MCP 证据，远端不重跑。
            followup = PolicySession(
                SQLAlchemySession("s1", engine=c.engine),
                ctx.model_copy(
                    update={"identity": ctx.identity.model_copy(update={"turn_id": "t2"})}
                ),
                c.evidence,
                "profile-under-test",
                SessionLimits(
                    max_history_turns=5, max_history_bytes=20_000, retention_seconds=7200
                ),
                input_policy=SessionInputPolicy(max_bytes=2000),
                engine=c.engine,
                clock=c.clock,
            )
            replay = await c.run(tools, [cite()], ctx, session=followup)
            assert json.loads(outputs(replay.calls[0])[0])["data"]["value"] == 7
            assert fixture.recorder.tool_calls == [("lookup", {"key": "k1"})]
            evidence_before = await c.evidence_count()

            # 不合约的类型、图片、远端错误、超过接收上限：执行一次，结果不交给模型、不生成证据。
            for name in ("wrong_type", "stringly", "picture", "failing", "huge"):
                rejected = await c.run(
                    tools, [call(f"fixture__{name}", call_id=name), clarify()], ctx
                )
                (content,) = outputs(rejected.calls[1])
                assert "工具执行失败" in content
                assert "seven" not in content and "remote failure" not in content
                assert PRIVATE not in seen(rejected)
                assert fixture.recorder.tool_calls[-1] == (name, {"key": "k1"})
            assert await c.evidence_count() == evidence_before
            # 被拒绝的结果不影响连接：后续调用照常执行。
            next_turn = ctx.model_copy(
                update={"identity": ctx.identity.model_copy(update={"turn_id": "t3"})}
            )
            await c.run(tools, [call("fixture__lookup", "k3", "after"), cite()], next_turn)
            assert fixture.recorder.tool_calls[-1] == ("lookup", {"key": "k3"})


async def _until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.05)


# --- 配置 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "http://example.com/mcp"},
        {"url": "http://localhost/mcp"},
        {"url": "https://user:pw@example.com/mcp"},
        {"url": "https://example.com/mcp?token=x"},
        {"auth_ref": "raw-token-value"},
        {"server_id": "local"},
        {"server_id": "has_underscore"},
        {"allowed_tools": {}},
        {"allowed_tools": {"Bad.Name": "fixture.key"}},
        {"timeout_seconds": 0},
        {"server_id": "s" * 30, "allowed_tools": {"t" * 40: "fixture.key"}},
    ],
)
def test_config_rejects_untrusted_registration(overrides: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "server_id": "fixture",
        "url": "https://mcp.example.com/mcp",
        "auth_ref": None,
        "timeout_seconds": 5.0,
        "max_response_bytes": 64_000,
        "allowed_tools": {"lookup": "fixture.key"},
    }
    MCPServerConfig(**values)
    with pytest.raises(ValidationError):
        MCPServerConfig(**{**values, **overrides})
