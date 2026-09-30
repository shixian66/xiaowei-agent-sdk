"""Task 2：本地受治理工具。真 SDK Runner + ScriptedModel，治理拒绝时 recording adapter 零 I/O。"""

import asyncio
from typing import Any

import pytest
from agents import Agent, Runner, UserError
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import event
from sqlalchemy.engine import URL
from tests.sdk_core.synthetic_tools import (
    CONTRACTS,
    OTHER_TARGET,
    PRIVATE_NOTE,
    PROJECTIONS,
    QUERY_TOOL,
    START,
    TARGET,
    TOTAL_TOOL,
    Clock,
    Grants,
    RecordingAdapter,
    RegionArgs,
    context,
    ready_engine,
    request,
    sdk_tool,
    store,
)

from xiaowei.evidence import EvidenceUnavailableError
from xiaowei.governance import (
    GovernedTools,
    Projection,
    ToolCatalog,
    ToolExecutionError,
    ToolPolicy,
    ToolRejectedError,
)
from xiaowei.models import (
    Budget,
    Identity,
    RunContext,
    ToolContract,
    ToolObservation,
    ToolRequest,
)

pytestmark = pytest.mark.loopback

_DONE = ModelStep(output=[assistant_message("done")])


def _outputs(model: ScriptedModel, index: int) -> list[str]:
    items = model.calls[index].input
    assert isinstance(items, list)
    return [str(i["output"]) for i in items if i.get("type") == "function_call_output"]


async def test_revoked_tool_never_reaches_io(postgres_url: URL) -> None:
    grants, adapter = Grants(), RecordingAdapter()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock()))
        ctx = context()
        assert TOTAL_TOOL in {c.tool_id for c in governed.allowed_contracts(ctx)}

        # 展示之后撤权：SDK 仍按模型请求调用工具，治理层在 I/O 前拒绝。
        grants.revoke_all()
        model = ScriptedModel(
            [[function_call("order_total", {"region": "east"}, call_id="call-1")], _DONE]
        )
        agent = Agent[RunContext](
            name="governed", model=model, tools=[sdk_tool(governed, adapter, TOTAL_TOOL)]
        )
        await Runner.run(agent, "东区订单？", context=ctx, max_turns=3)

    assert adapter.calls == []
    (output,) = _outputs(model, 1)
    assert "当前无权调用该工具" in output
    assert grants.checks == [("alice", TARGET, TOTAL_TOOL)]


@pytest.mark.parametrize("phase", ["execute", "record"])
async def test_revocation_after_io_withholds_result(postgres_url: URL, phase: str) -> None:
    """授权在执行前通过，执行期间或证据写入期间撤权：结果不得交给模型。"""
    grants, adapter = Grants(), RecordingAdapter()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        if phase == "execute":
            original = adapter.execute

            async def revoke_while_executing(req: ToolRequest) -> ToolObservation:
                observation = await original(req)
                grants.revoke_all()
                return observation

            adapter.execute = revoke_while_executing  # type: ignore[method-assign]
        else:

            def revoke_on_insert(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
                if "INSERT INTO xiaowei_evidence" in statement:
                    grants.revoke_all()

            event.listen(engine.sync_engine, "before_cursor_execute", revoke_on_insert)

        governed = GovernedTools(store(engine, grants, Clock()))
        model = ScriptedModel(
            [[function_call("order_total", {"region": "east"}, call_id="call-1")], _DONE]
        )
        agent = Agent[RunContext](
            name="governed", model=model, tools=[sdk_tool(governed, adapter, TOTAL_TOOL)]
        )
        # I/O 已发生、结果不能交给模型：本轮中止，模型没有续轮，也就不能在同一轮重试。
        with pytest.raises(UserError) as aborted:
            await Runner.run(agent, "东区订单？", context=context(max_tool_calls=1), max_turns=3)
        assert isinstance(aborted.value.__cause__, EvidenceUnavailableError)

        # 结果未知不重试：同一轮再次调用因预算已用而拒绝，执行次数保持 1。
        with pytest.raises(ToolRejectedError):
            await governed.invoke(context(max_tool_calls=1), request(), adapter.execute)

    assert len(adapter.calls) == 1
    assert len(model.calls) == 1
    for business in ('"total"', "100", '"rows"', "2026-09-01"):
        assert business not in str(aborted.value)


def _reject_cases() -> list[Any]:
    base = request()
    return [
        pytest.param(context(tools=frozenset({QUERY_TOOL})), base, id="tool-out-of-scope"),
        pytest.param(context(targets=frozenset({OTHER_TARGET})), base, id="target-out-of-scope"),
        pytest.param(
            context(), base.model_copy(update={"target_id": OTHER_TARGET}), id="target-mismatch"
        ),
        pytest.param(context(), base.model_copy(update={"tool_id": "local/unknown"}), id="unknown"),
        pytest.param(
            context(), base.model_copy(update={"arguments": {"region": 1}}), id="bad-type"
        ),
        pytest.param(
            context(),
            base.model_copy(update={"arguments": {"region": "e", "x": 1}}),
            id="extra-arg",
        ),
        pytest.param(context(), base.model_copy(update={"arguments": {}}), id="missing-arg"),
    ]


@pytest.mark.parametrize(("ctx", "req"), _reject_cases())
async def test_invalid_request_is_rejected_before_io(
    postgres_url: URL, ctx: RunContext, req: ToolRequest
) -> None:
    grants, adapter = Grants(), RecordingAdapter()
    grants.grant("alice", TOTAL_TOOL, QUERY_TOOL)
    grants.grant("alice", TOTAL_TOOL, target=OTHER_TARGET)
    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock()))
        with pytest.raises(ToolRejectedError):
            await governed.invoke(ctx, req, adapter.execute)
    assert adapter.calls == []
    # 结构性拒绝发生在授权回调之前，也不消耗预算。
    assert grants.checks == []


async def test_parallel_calls_share_one_budget(postgres_url: URL) -> None:
    grants, adapter = Grants(), RecordingAdapter()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock()))

        # 同一轮的两个并发调用：授权回调都通过后才预占预算，只有一个能执行。
        ctx = context(turn="t-gather", max_tool_calls=1)
        results = await asyncio.gather(
            *(
                governed.invoke(ctx, r, adapter.execute)
                for r in (request(call_id="a"), request(call_id="b"))
            ),
            return_exceptions=True,
        )
        assert len(adapter.calls) == 1
        assert sum(isinstance(r, ToolRejectedError) for r in results) == 1

        # 真 Runner：模型一次返回两个工具调用，SDK 并行执行，总执行数仍为 1。
        model = ScriptedModel(
            [
                [
                    function_call("order_total", {"region": "east"}, call_id="c1"),
                    function_call("order_total", {"region": "west"}, call_id="c2"),
                ],
                _DONE,
            ]
        )
        agent = Agent[RunContext](
            name="governed", model=model, tools=[sdk_tool(governed, adapter, TOTAL_TOOL)]
        )
        await Runner.run(agent, "两个地区？", context=context(turn="t-runner", max_tool_calls=1))
        assert len(adapter.calls) == 2
        outputs = _outputs(model, 1)
        assert sum("本轮工具调用次数已用完" in o for o in outputs) == 1

        # 计数按可信 subject/session/turn 隔离：其他轮次、会话与用户各自独立。
        grants.grant("bob", TOTAL_TOOL)
        for other in (
            context(turn="t-next", max_tool_calls=1),
            context(session="s2", turn="t-gather", max_tool_calls=1),
            context(subject="bob", turn="t-gather", max_tool_calls=1),
        ):
            await governed.invoke(other, request(), adapter.execute)
        assert len(adapter.calls) == 5

        # 轮次结束清理计数；同一轮次标识不会被旧计数永久锁死，也不无限累积。
        governed.end_turn(context(turn="t-gather").identity)
        await governed.invoke(
            context(turn="t-gather", max_tool_calls=1),
            request(),
            adapter.execute,
        )
        assert len(adapter.calls) == 6


async def test_execution_failure_is_bounded(postgres_url: URL) -> None:
    grants = Grants()
    grants.grant("alice", TOTAL_TOOL)

    async def failing(_: ToolRequest) -> Any:
        raise RuntimeError(f"db=postgres://u:pw@host/x {PRIVATE_NOTE}")

    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock()))
        with pytest.raises(ToolExecutionError) as excinfo:
            await governed.invoke(context(max_tool_calls=1), request(), failing)
        # 执行结果未知，不退还预算，也不自动重试。
        with pytest.raises(ToolRejectedError):
            await governed.invoke(context(max_tool_calls=1), request(), failing)

    message = str(excinfo.value)
    assert "pw" not in message and PRIVATE_NOTE not in message
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


class _Window(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int


class _CountArgs(BaseModel):
    # 策略作者的默认（宽松）配置：严格校验必须由治理层执行。
    model_config = ConfigDict(extra="forbid")
    count: int
    window: _Window
    ratio: float


_COUNT_TOOL = "local/count_orders"


def _count_catalog() -> ToolCatalog:
    contract = ToolContract(
        tool_id=_COUNT_TOOL,
        target_id=TARGET,
        input_schema=_CountArgs.model_json_schema(),
        policy_id="synthetic.count",
    )
    policy = ToolPolicy(policy_id="synthetic.count", arguments=_CountArgs, projections=PROJECTIONS)
    return ToolCatalog((contract,), (policy,))


_VALID_COUNT = {"count": 7, "window": {"days": 3}, "ratio": 1}


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({**_VALID_COUNT, "count": "7"}, id="string-number"),
        pytest.param({**_VALID_COUNT, "window": {"days": "3"}}, id="nested-string-number"),
        pytest.param({**_VALID_COUNT, "count": 7.0}, id="float-for-int"),
        pytest.param({**_VALID_COUNT, "ratio": float("nan")}, id="nan"),
        pytest.param({**_VALID_COUNT, "ratio": float("inf")}, id="infinity"),
    ],
)
async def test_arguments_are_checked_strictly_before_io(
    postgres_url: URL, arguments: dict[str, object]
) -> None:
    grants = Grants()
    grants.grant("alice", _COUNT_TOOL)
    executed: list[ToolRequest] = []
    req = ToolRequest(
        tool_id=_COUNT_TOOL,
        target_id=TARGET,
        call_id="c1",
        tool_name="count_orders",
        arguments=arguments,
    )

    async def run(validated: ToolRequest) -> ToolObservation:
        executed.append(validated)
        return ToolObservation(payload={"total": 1}, captured_at=START, truncated=False)

    async with ready_engine(postgres_url) as engine:
        tools = _count_catalog()
        governed = GovernedTools(store(engine, grants, Clock(), tools))
        with pytest.raises(ToolRejectedError):
            await governed.invoke(context(tools=frozenset({_COUNT_TOOL})), req, run)
    assert executed == []


async def test_execution_receives_only_validated_arguments(postgres_url: URL) -> None:
    grants = Grants()
    grants.grant("alice", _COUNT_TOOL)
    executed: list[ToolRequest] = []
    req = ToolRequest(
        tool_id=_COUNT_TOOL,
        target_id=TARGET,
        call_id="c1",
        tool_name="count_orders",
        arguments=_VALID_COUNT,
    )

    async def run(validated: ToolRequest) -> ToolObservation:
        executed.append(validated)
        return ToolObservation(payload={"total": 1}, captured_at=START, truncated=False)

    async with ready_engine(postgres_url) as engine:
        tools = _count_catalog()
        governed = GovernedTools(store(engine, grants, Clock(), tools))
        await governed.invoke(context(tools=frozenset({_COUNT_TOOL})), req, run)
    (seen,) = executed
    assert seen.arguments == {"count": 7, "window": {"days": 3}, "ratio": 1.0}
    assert type(seen.arguments["ratio"]) is float


class _Dependency:
    """代表客户端、连接或服务引用；RunContext 不能容纳。"""


@pytest.mark.parametrize(
    "extra",
    [
        {"client": _Dependency()},
        {"database_url": "postgresql+asyncpg://u:pw@h/db"},
        {"api_key": "sk-test"},
    ],
)
def test_context_rejects_dependencies(extra: dict[str, object]) -> None:
    fields: dict[str, Any] = dict(context())
    with pytest.raises(ValidationError):
        RunContext(**fields, **extra)

    # 字段本身也只接受声明的类型，不能借字段塞入对象；构造后不可修改。
    with pytest.raises(ValidationError):
        RunContext(**{**fields, "target_scope": frozenset({_Dependency()})})
    with pytest.raises(ValidationError):
        RunContext(**{**fields, "evidence_ids": (_Dependency(),)})
    with pytest.raises(ValidationError):
        Identity(subject_id="", session_id="s", turn_id="t", channel="web")
    with pytest.raises(ValidationError):
        Identity(subject_id="a", session_id="s", turn_id="t", channel="email")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Budget(max_turns=0, max_tool_calls=1, timeout_seconds=1.0)
    with pytest.raises(ValidationError):
        context().tool_scope = frozenset({QUERY_TOOL})  # type: ignore[misc]


async def test_diagnose_scope_hides_and_denies_query(postgres_url: URL) -> None:
    grants, adapter = Grants(), RecordingAdapter()
    # 用户本身拥有查询权限；本轮是诊断，入口生成的 Tool Scope 不含查询工具。
    grants.grant("alice", TOTAL_TOOL, QUERY_TOOL)
    diagnose = context(tools=frozenset({TOTAL_TOOL}))
    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock()))
        assert [c.tool_id for c in governed.allowed_contracts(diagnose)] == [TOTAL_TOOL]
        assert {c.tool_id for c in governed.allowed_contracts(context())} == {
            TOTAL_TOOL,
            QUERY_TOOL,
        }

        # 模型绕过展示强行调用查询工具：I/O 前拒绝，授权回调不会替诊断轮补发许可。
        model = ScriptedModel(
            [[function_call("run_query", {"region": "east"}, call_id="q1")], _DONE]
        )
        agent = Agent[RunContext](
            name="governed", model=model, tools=[sdk_tool(governed, adapter, QUERY_TOOL)]
        )
        await Runner.run(agent, "诊断一下", context=diagnose, max_turns=3)

    assert adapter.calls == []
    assert grants.checks == []
    (output,) = _outputs(model, 1)
    assert "本轮不允许使用该工具" in output


class _OtherArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    region: str
    limit: int


class _LooseArgs(BaseModel):
    region: str


class _DefaultArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 默认值不经校验：可以是参数模型声明的类型之外的值。
    region: str = 0  # type: ignore[assignment]


class _LooseWindow(BaseModel):
    days: int


class _NestedLooseArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window: _LooseWindow


class _Result(BaseModel):
    region: str
    total: int
    rows: int


class _OpenResult(_Result):
    model_config = ConfigDict(extra="allow")


class _Detail(BaseModel):
    model_config = ConfigDict(extra="allow")
    memo: str


class _NestedOpenResult(_Result):
    detail: _Detail


class _NarrowResult(BaseModel):
    region: str
    total: int


def _policy(**overrides: Any) -> ToolPolicy:
    fields: dict[str, Any] = {
        "policy_id": "synthetic.region",
        "arguments": RegionArgs,
        "projections": PROJECTIONS,
    }
    return ToolPolicy(**{**fields, **overrides})


def _with_schema(arguments: type[BaseModel]) -> tuple[ToolContract, ...]:
    return tuple(
        c.model_copy(update={"input_schema": arguments.model_json_schema()}) for c in CONTRACTS
    )


@pytest.mark.parametrize(
    ("contracts", "policies"),
    [
        pytest.param(CONTRACTS, (), id="unknown-policy"),
        pytest.param(
            CONTRACTS,
            (_policy(projections={k: v for k, v in PROJECTIONS.items() if k != "feishu"}),),
            id="missing-projection",
        ),
        pytest.param(
            CONTRACTS,
            (_policy(projections={**PROJECTIONS, "email": PROJECTIONS["web"]}),),
            id="unknown-projection",
        ),
        pytest.param(CONTRACTS, (_policy(arguments=_OtherArgs),), id="schema-drift"),
        pytest.param(
            tuple(
                c.model_copy(update={"input_schema": _LooseArgs.model_json_schema()})
                for c in CONTRACTS
            ),
            (_policy(arguments=_LooseArgs),),
            id="extra-args-allowed",
        ),
        pytest.param(
            _with_schema(_DefaultArgs), (_policy(arguments=_DefaultArgs),), id="argument-default"
        ),
        pytest.param(
            _with_schema(_NestedLooseArgs),
            (_policy(arguments=_NestedLooseArgs),),
            id="nested-extra-args-allowed",
        ),
        pytest.param((*CONTRACTS, CONTRACTS[0]), (_policy(),), id="duplicate-tool"),
        # 结果模型决定远端数据中哪些成为事实：不能接收未声明字段，投影也只能取声明的字段。
        pytest.param(CONTRACTS, (_policy(result=_OpenResult),), id="result-allows-extra"),
        pytest.param(CONTRACTS, (_policy(result=_NestedOpenResult),), id="nested-result-extra"),
        pytest.param(CONTRACTS, (_policy(result=_NarrowResult),), id="projection-not-in-result"),
    ],
)
def test_catalog_rejects_unregistered_contracts(
    contracts: tuple[ToolContract, ...], policies: tuple[ToolPolicy, ...]
) -> None:
    with pytest.raises(ValueError, match="工具目录"):
        ToolCatalog(contracts, policies)


def test_catalog_accepts_a_declared_result_model() -> None:
    ToolCatalog(CONTRACTS, (_policy(result=_Result),))


def test_projection_limits_are_positive_and_fit_the_envelope() -> None:
    with pytest.raises(ValueError):
        Projection(fields=("total",), max_bytes=16)
    with pytest.raises(ValueError):
        Projection(fields=(), max_bytes=1000)
