"""Task 2：本地受治理工具。真 SDK Runner + ScriptedModel，治理拒绝时 recording adapter 零 I/O。"""

import asyncio
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

import pytest
from agents import Agent, Runner, UserError
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SerializerFunctionWrapHandler,
    ValidationError,
    computed_field,
    create_model,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)
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
    model_data,
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
    normalize_arguments,
)
from xiaowei.models import (
    Budget,
    Identity,
    RunContext,
    ToolContract,
    ToolObservation,
    ToolRequest,
)
from xiaowei.tools import routed_function_tool

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


class _LooseArgs(BaseModel):
    region: str


class _LooseWindow(BaseModel):
    days: int


class _NestedLooseArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window: _LooseWindow


class _DisguisedExtraArgs(BaseModel):
    # 运行时接收额外字段，schema 却声明禁止：目录不能凭 schema 判断。
    model_config = ConfigDict(extra="allow", json_schema_extra={"additionalProperties": False})
    region: str


class _DisguisedWindow(BaseModel):
    model_config = ConfigDict(extra="allow", json_schema_extra={"additionalProperties": False})
    days: int


class _NestedDisguisedArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window: _DisguisedWindow


def _disguise_required(schema: dict[str, Any]) -> None:
    schema["required"] = list(schema["properties"])


class _DisguisedDefaultArgs(BaseModel):
    # 运行时有默认值，schema 却声明必填：默认值不经校验，也不是模型给出的参数。
    model_config = ConfigDict(extra="forbid", json_schema_extra=_disguise_required)
    region: str = "west"


class _DefaultWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_disguise_required)
    days: int = 30


class _NestedDefaultArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window: _DefaultWindow


def _single_catalog(arguments: type[BaseModel]) -> ToolCatalog:
    contract = ToolContract(
        tool_id=_COUNT_TOOL,
        target_id=TARGET,
        input_schema=arguments.model_json_schema(),
        policy_id="synthetic.single",
    )
    policy = ToolPolicy(policy_id="synthetic.single", arguments=arguments, projections=PROJECTIONS)
    return ToolCatalog((contract,), (policy,))


@pytest.mark.parametrize(
    ("arguments", "sent"),
    [
        pytest.param(_DisguisedExtraArgs, {"region": "east", "admin": True}, id="root-extra"),
        pytest.param(
            _NestedDisguisedArgs, {"window": {"days": 3, "admin": True}}, id="nested-extra"
        ),
        pytest.param(_DisguisedDefaultArgs, {}, id="root-default"),
        pytest.param(_NestedDefaultArgs, {"window": {}}, id="nested-default"),
        # 模型配置为默认（忽略额外字段）时同样拒绝，而不是静默丢弃。
        pytest.param(_LooseArgs, {"region": "east", "admin": True}, id="root-ignore"),
        pytest.param(_NestedLooseArgs, {"window": {"days": 3, "admin": True}}, id="nested-ignore"),
    ],
)
async def test_runtime_argument_behaviour_is_not_taken_from_the_schema(
    postgres_url: URL, arguments: type[BaseModel], sent: dict[str, object]
) -> None:
    """schema 可以被覆盖；额外字段与默认值按 Pydantic 运行时行为约束，I/O 前拒绝。"""
    grants = Grants()
    grants.grant("alice", _COUNT_TOOL)
    executed: list[ToolRequest] = []

    async def run(validated: ToolRequest) -> ToolObservation:
        executed.append(validated)
        return ToolObservation(payload={"total": 1}, captured_at=START, truncated=False)

    req = ToolRequest(
        tool_id=_COUNT_TOOL,
        target_id=TARGET,
        call_id="c1",
        tool_name="count_orders",
        arguments=sent,
    )
    try:
        tools = _single_catalog(arguments)
    except ValueError as exc:
        assert "工具目录" in str(exc)  # 登记时拒绝：同样零 I/O
        return
    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock(), tools))
        with pytest.raises(ToolRejectedError):
            await governed.invoke(context(tools=frozenset({_COUNT_TOOL})), req, run)
    assert executed == []


class _ComputedArgs(BaseModel):
    region: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def privileged(self) -> bool:
        return True


class _ComputedWindow(BaseModel):
    days: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def privileged(self) -> bool:
        return True


class _NestedComputedArgs(BaseModel):
    window: _ComputedWindow


class _ValidatedArgs(BaseModel):
    region: str

    @field_validator("region")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return value.strip().upper()


def _drop_privileged(data: Any) -> Any:
    # 与增加字段的 serializer 配合：二次校验同一模型时先删掉它，``extra="forbid"`` 看不到。
    return {k: v for k, v in data.items() if k != "privileged"} if isinstance(data, dict) else data


class _FieldSerializedArgs(BaseModel):
    region: str

    @field_serializer("region")
    def _as_object(self, value: str) -> dict[str, object]:
        return {"region": value, "privileged": True}

    @field_validator("region", mode="before")
    @classmethod
    def _unwrap(cls, value: Any) -> Any:
        return value["region"] if isinstance(value, dict) else value


class _ModelSerializedArgs(BaseModel):
    region: str

    @model_serializer(mode="wrap")
    def _widen(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        return {**handler(self), "privileged": True}

    _mask = model_validator(mode="before")(_drop_privileged)


class _SerializedWindow(BaseModel):
    days: int

    @model_serializer(mode="wrap")
    def _widen(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        return {**handler(self), "privileged": True}

    _mask = model_validator(mode="before")(_drop_privileged)


class _NestedSerializedArgs(BaseModel):
    window: _SerializedWindow


class _AnnotatedSerializedArgs(BaseModel):
    region: Annotated[str, PlainSerializer(lambda value: {"privileged": True})]

    @field_validator("region", mode="before")
    @classmethod
    def _unwrap(cls, value: Any) -> Any:
        return "east" if isinstance(value, dict) else value


class _ExcludedArgs(BaseModel):
    region: str
    scope: str = Field(exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _fill(cls, data: Any) -> Any:
        # 二次校验时补回被 ``exclude`` 去掉的必填字段。
        return {"scope": "all", **data} if isinstance(data, dict) else data


class _PlainWindow(BaseModel):
    days: int


class _WiderWindow(_PlainWindow):
    privileged: bool


class _SubstitutedArgs(BaseModel):
    window: _PlainWindow

    @model_validator(mode="after")
    def _widen(self) -> "_SubstitutedArgs":
        self.window = _WiderWindow(days=self.window.days, privileged=True)
        return self


@pytest.mark.parametrize(
    ("arguments", "sent", "effective"),
    [
        pytest.param(_ComputedArgs, {"region": "east"}, {"region": "east"}, id="root-computed"),
        pytest.param(
            _NestedComputedArgs,
            {"window": {"days": 3}},
            {"window": {"days": 3}},
            id="nested-computed",
        ),
        pytest.param(
            _FieldSerializedArgs, {"region": "east"}, {"region": "east"}, id="field-serializer"
        ),
        pytest.param(
            _ModelSerializedArgs, {"region": "east"}, {"region": "east"}, id="root-model-serializer"
        ),
        pytest.param(
            _NestedSerializedArgs,
            {"window": {"days": 3}},
            {"window": {"days": 3}},
            id="nested-model-serializer",
        ),
        pytest.param(
            _AnnotatedSerializedArgs,
            {"region": "east"},
            {"region": "east"},
            id="annotated-serializer",
        ),
        pytest.param(
            _ExcludedArgs,
            {"region": "east", "scope": "narrow"},
            {"region": "east", "scope": "narrow"},
            id="excluded-field",
        ),
        # 对照：字段校验器的规范化照常生效，执行收到规范化后的值。
        pytest.param(_ValidatedArgs, {"region": " east "}, {"region": "EAST"}, id="validator"),
    ],
)
async def test_execution_receives_only_declared_field_values(
    postgres_url: URL,
    arguments: type[BaseModel],
    sent: dict[str, object],
    effective: dict[str, object],
) -> None:
    """有效参数按声明字段从已校验实例生成，不经模型自己的序列化与再次校验。

    计算字段、serializer、``exclude`` 以及能让同一模型二次校验通过的 validator 组合都不能
    向执行与证据加入或去掉字段、改变类型；证据按同一有效参数记录，回放时同样规范化。
    """
    grants = Grants()
    grants.grant("alice", _COUNT_TOOL)
    executed: list[ToolRequest] = []

    async def run(validated: ToolRequest) -> ToolObservation:
        executed.append(validated)
        return ToolObservation(payload={"total": 1}, captured_at=START, truncated=False)

    req = ToolRequest(
        tool_id=_COUNT_TOOL,
        target_id=TARGET,
        call_id="c1",
        tool_name="count_orders",
        arguments=sent,
    )
    tools = _single_catalog(arguments)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, Clock(), tools)
        await GovernedTools(evidence).invoke(context(tools=frozenset({_COUNT_TOOL})), req, run)
    (seen,) = executed
    assert seen.arguments == effective
    assert normalize_arguments(tools.policy_for(tools.contracts[0]), sent) == effective


class _RetypedArgs(BaseModel):
    region: str

    @model_validator(mode="after")
    def _retype(self) -> "_RetypedArgs":
        # 校验之后不经校验地改写字段（未开启 validate_assignment）：类型不再合约。
        self.__dict__["region"] = {"privileged": True}
        return self


class _RetypedWindow(BaseModel):
    days: int

    @model_validator(mode="after")
    def _retype(self) -> "_RetypedWindow":
        self.__dict__["days"] = "3"
        return self


class _NestedRetypedArgs(BaseModel):
    window: _RetypedWindow


class _ReplacedArgs(BaseModel):
    window: _PlainWindow

    @model_validator(mode="after")
    def _replace(self) -> "_ReplacedArgs":
        self.__dict__["window"] = {"days": 3, "privileged": True}
        return self


class _InfiniteArgs(BaseModel):
    ratio: float

    @model_validator(mode="after")
    def _overflow(self) -> "_InfiniteArgs":
        self.__dict__["ratio"] = float("inf")
        return self


class _BoolForIntArgs(BaseModel):
    days: int

    @model_validator(mode="after")
    def _retype(self) -> "_BoolForIntArgs":
        self.__dict__["days"] = True  # bool 是 int 的子类，但不是整数参数
        return self


class _InfiniteListArgs(BaseModel):
    ratios: dict[str, list[float]]

    @model_validator(mode="after")
    def _overflow(self) -> "_InfiniteListArgs":
        self.ratios["east"][0] = float("inf")
        return self


class _Scale(Enum):
    ONE = 1.0

    @classmethod
    def _missing_(cls, value: object) -> "_Scale":
        # 动态成员：取值不在登记时可见的成员中，只能在生成时核对。
        member = object.__new__(cls)
        member._value_ = value
        member._name_ = "DYNAMIC"
        return member


class _InfiniteEnumArgs(BaseModel):
    scale: _Scale

    @model_validator(mode="after")
    def _overflow(self) -> "_InfiniteEnumArgs":
        self.scale = _Scale(float("inf"))
        return self


class _RootArgs(BaseModel):
    region: str

    @model_validator(mode="after")
    def _widen(self) -> "_RootArgs":
        return _WiderRootArgs.model_construct(region=self.region, privileged=True)


class _WiderRootArgs(_RootArgs):
    privileged: bool


class _Unrelated(BaseModel):
    privileged: bool


class _UnrelatedRootArgs(BaseModel):
    region: str

    @model_validator(mode="after")
    def _replace(self) -> Any:
        return _Unrelated(privileged=True)


class _DictRootArgs(BaseModel):
    region: str

    @model_validator(mode="after")
    def _replace(self) -> Any:
        return {"region": self.region, "privileged": True}


class _RemovedFieldArgs(BaseModel):
    region: str

    @model_validator(mode="after")
    def _remove(self) -> "_RemovedFieldArgs":
        del self.region
        return self


@pytest.mark.parametrize(
    ("arguments", "sent"),
    [
        pytest.param(_RetypedArgs, {"region": "east"}, id="root-retyped"),
        pytest.param(_NestedRetypedArgs, {"window": {"days": 3}}, id="nested-retyped"),
        pytest.param(_InfiniteArgs, {"ratio": 1.5}, id="non-finite"),
        pytest.param(_ReplacedArgs, {"window": {"days": 3}}, id="nested-replaced"),
        pytest.param(_SubstitutedArgs, {"window": {"days": 3}}, id="nested-subclass"),
        pytest.param(_RootArgs, {"region": "east"}, id="root-subclass"),
        pytest.param(_UnrelatedRootArgs, {"region": "east"}, id="root-unrelated"),
        pytest.param(_DictRootArgs, {"region": "east"}, id="root-dict"),
        pytest.param(_RemovedFieldArgs, {"region": "east"}, id="missing-field"),
        pytest.param(_InfiniteListArgs, {"ratios": {"east": [1.5]}}, id="nested-non-finite"),
        pytest.param(_BoolForIntArgs, {"days": 3}, id="bool-for-int"),
        pytest.param(_InfiniteEnumArgs, {"scale": 1.0}, id="enum-non-finite"),
    ],
)
async def test_off_contract_values_never_reach_execution(
    postgres_url: URL, arguments: type[BaseModel], sent: dict[str, object]
) -> None:
    """已校验实例不是登记的模型、缺字段或字段值不符合声明类型：执行前受控拒绝，零 I/O。"""
    grants = Grants()
    grants.grant("alice", _COUNT_TOOL)
    executed: list[ToolRequest] = []

    async def run(validated: ToolRequest) -> ToolObservation:
        executed.append(validated)
        return ToolObservation(payload={"total": 1}, captured_at=START, truncated=False)

    req = ToolRequest(
        tool_id=_COUNT_TOOL,
        target_id=TARGET,
        call_id="c1",
        tool_name="count_orders",
        arguments=sent,
    )
    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock(), _single_catalog(arguments)))
        with pytest.raises(ToolRejectedError):
            await governed.invoke(context(tools=frozenset({_COUNT_TOOL})), req, run)
    assert executed == []


class _Query(BaseModel):
    region: str


class _LimitedQuery(_Query):
    limit: int


def _envelope(annotation: Any) -> ToolPolicy:
    tools = _single_catalog(create_model("_Envelope", value=(annotation, ...)))
    return tools.policy_for(tools.contracts[0])


_LIMITED = {"region": "east", "limit": 5}


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        pytest.param(_Query | _LimitedQuery, _LIMITED, id="base-first"),
        pytest.param(_LimitedQuery | _Query, _LIMITED, id="derived-first"),
        pytest.param(list[_Query | _LimitedQuery], [_LIMITED], id="list-of-union"),
        pytest.param(list[_Query] | list[_LimitedQuery], [_LIMITED], id="union-of-lists"),
        pytest.param(
            dict[str, _Query] | dict[str, _LimitedQuery], {"q": _LIMITED}, id="union-of-dicts"
        ),
        pytest.param(float | int, 1, id="int-after-float"),
        pytest.param(int | float, 1.5, id="float-after-int"),
        # 对照：选中基类分支时照常只有基类字段。
        pytest.param(_LimitedQuery | _Query, {"region": "east"}, id="base-selected"),
    ],
)
def test_union_keeps_the_branch_pydantic_selected(annotation: Any, value: object) -> None:
    """联合类型按已校验值的实际分支生成，不按声明顺序投影；不同参数的摘要因此可以区分。"""
    policy = _envelope(annotation)
    assert normalize_arguments(policy, {"value": value}) == {"value": value}
    if value == _LIMITED:
        other = {**_LIMITED, "limit": 99}
        assert normalize_arguments(policy, {"value": other}) == {"value": other}


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
        Budget(max_turns=0, max_tool_calls=1, timeout_seconds=1.0, max_scope_checks=1000)
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


class _DefaultArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 默认值不经校验：可以是参数模型声明的类型之外的值。
    region: str = 0  # type: ignore[assignment]


class _Result(BaseModel):
    region: str
    total: int
    rows: int


class _NarrowResult(BaseModel):
    region: str
    total: int


class _KeyedArgs(BaseModel):
    totals: dict[int, str]


class _Unbounded(Enum):
    SMALL = 1.0
    UNBOUNDED = float("inf")


class _InfiniteEnumMemberArgs(BaseModel):
    ratio: _Unbounded


class _InfiniteLiteralArgs(BaseModel):
    ratio: Literal[1.0, float("inf")]  # type: ignore[valid-type]


class _DatedResult(BaseModel):
    region: str
    total: int
    rows: int
    captured: datetime


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
            _with_schema(_DefaultArgs), (_policy(arguments=_DefaultArgs),), id="argument-default"
        ),
        pytest.param((*CONTRACTS, CONTRACTS[0]), (_policy(),), id="duplicate-tool"),
        # 结果模型决定远端数据中哪些成为事实：投影只能取它声明的字段（未声明字段在校验时忽略）。
        pytest.param(CONTRACTS, (_policy(result=_NarrowResult),), id="projection-not-in-result"),
        # 有效数据按声明类型生成：生成规则之外的类型在登记时拒绝，而不是每次调用时失败。
        pytest.param(
            _with_schema(_KeyedArgs), (_policy(arguments=_KeyedArgs),), id="argument-non-string-key"
        ),
        pytest.param(CONTRACTS, (_policy(result=_DatedResult),), id="result-unsupported-type"),
        pytest.param(
            _with_schema(_InfiniteLiteralArgs),
            (_policy(arguments=_InfiniteLiteralArgs),),
            id="non-finite-literal",
        ),
        pytest.param(
            _with_schema(_InfiniteEnumMemberArgs),
            (_policy(arguments=_InfiniteEnumMemberArgs),),
            id="non-finite-enum-member",
        ),
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


# ---- 多目标路由（P2.5 Task 1）：一套工具、cluster 必填、按 (tool_id, target_id) 查契约 ----------

ROUTED_TOOL = "local/region_total"
CLUSTERS = ("east-wh", "west-wh", "north-wh")


class ClusterRegionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cluster: str
    region: str


def _routed_policy(cluster: str, arguments: type[BaseModel] = ClusterRegionArgs) -> ToolPolicy:
    return ToolPolicy(
        policy_id=f"synthetic.{cluster}", arguments=arguments, projections=PROJECTIONS
    )


def _routed_contract(cluster: str, arguments: type[BaseModel] = ClusterRegionArgs) -> ToolContract:
    return ToolContract(
        tool_id=ROUTED_TOOL,
        target_id=cluster,
        input_schema=arguments.model_json_schema(),
        policy_id=f"synthetic.{cluster}",
    )


def _routed_catalog() -> ToolCatalog:
    return ToolCatalog(
        [_routed_contract(c) for c in CLUSTERS], [_routed_policy(c) for c in CLUSTERS]
    )


def _routed_ctx(targets: tuple[str, ...] = CLUSTERS, **kw: Any) -> RunContext:
    return context(tools=frozenset({ROUTED_TOOL}), targets=frozenset(targets), **kw)


def _routed_grants(*clusters: str) -> Grants:
    grants = Grants()
    for cluster in clusters:
        grants.grant("alice", ROUTED_TOOL, target=cluster)
    return grants


def _three_adapters() -> dict[str, RecordingAdapter]:
    """三个目标返回不同行数，串到别的目标就能被识别（模型投影只含 rows）。"""
    return {c: RecordingAdapter(rows=i + 1) for i, c in enumerate(CLUSTERS)}


async def test_one_tool_routes_each_call_to_the_named_target(postgres_url: URL) -> None:
    adapters, grants = _three_adapters(), _routed_grants(*CLUSTERS)
    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock(), _routed_catalog()))
        tool = routed_function_tool(
            ROUTED_TOOL, governed, {c: a.execute for c, a in adapters.items()}
        )
        assert tool.name == "region_total"
        assert tool.params_json_schema["required"] == ["cluster", "region"]
        # 一次返回三个调用：SDK 并行执行，各自只到达所指目标。
        model = ScriptedModel(
            [
                [
                    function_call("region_total", {"cluster": c, "region": "east"}, call_id=c)
                    for c in reversed(CLUSTERS)
                ],
                _DONE,
            ]
        )
        agent = Agent[RunContext](name="governed", model=model, tools=[tool])
        await Runner.run(agent, "三个集群的东区订单？", context=_routed_ctx(), max_turns=3)

        for cluster, adapter in adapters.items():
            (req,) = adapter.calls
            assert req.target_id == cluster and req.arguments["cluster"] == cluster
        outputs = _outputs(model, 1)
        assert len(outputs) == 3
        for output, cluster in zip(outputs, reversed(CLUSTERS), strict=True):
            rows = model_data(output)["data"]["rows"]  # type: ignore[index]
            assert len(rows) == adapters[cluster].rows
        # 证据按实际目标保存：只在含该目标的范围内可读。
        evidence = governed.evidence
        evidence_id = str(model_data(outputs[0])["evidence_id"])
        await evidence.project(evidence_id, _routed_ctx(), "model")
        with pytest.raises(EvidenceUnavailableError):
            await evidence.project(evidence_id, _routed_ctx(("east-wh",)), "model")
    # 调用前与读取证据时的授权都只针对实际目标。
    assert set(grants.checks) == {("alice", c, ROUTED_TOOL) for c in CLUSTERS}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param({"region": "east"}, "集群", id="missing-cluster"),
        pytest.param({"cluster": "south-wh", "region": "east"}, "集群", id="unknown-cluster"),
        pytest.param({"cluster": 1, "region": "east"}, "集群", id="non-string-cluster"),
        pytest.param({"cluster": "west-wh", "region": "east"}, "本轮不允许", id="out-of-scope"),
        pytest.param({"cluster": "north-wh", "region": "east"}, "当前无权", id="not-authorized"),
    ],
)
async def test_bad_cluster_is_rejected_before_any_target_io(
    postgres_url: URL, arguments: dict[str, object], message: str
) -> None:
    adapters, grants = _three_adapters(), _routed_grants("east-wh", "west-wh")
    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock(), _routed_catalog()))
        tool = routed_function_tool(
            ROUTED_TOOL, governed, {c: a.execute for c, a in adapters.items()}
        )
        model = ScriptedModel([[function_call("region_total", arguments, call_id="call-1")], _DONE])
        agent = Agent[RunContext](name="governed", model=model, tools=[tool])
        ctx = _routed_ctx(("east-wh", "north-wh"))
        await Runner.run(agent, "东区订单？", context=ctx, max_turns=3)

    (output,) = _outputs(model, 1)
    assert message in output
    assert all(a.calls == [] for a in adapters.values())
    # 不回退到其他集群；结构性拒绝不触发授权查询，只有未授权目标查过一次授权。
    assert grants.checks == ([("alice", "north-wh", ROUTED_TOOL)] if message == "当前无权" else [])


async def test_model_arguments_cannot_override_the_routed_target(postgres_url: URL) -> None:
    """请求目标与参数中的 cluster 不一致时拒绝：执行与证据都不能落到参数之外的目标。"""
    adapters, grants = _three_adapters(), _routed_grants(*CLUSTERS)
    async with ready_engine(postgres_url) as engine:
        governed = GovernedTools(store(engine, grants, Clock(), _routed_catalog()))
        forged = ToolRequest(
            tool_id=ROUTED_TOOL,
            target_id="east-wh",
            call_id="call-1",
            tool_name="region_total",
            arguments={"cluster": "west-wh", "region": "east"},
        )
        for adapter in adapters.values():
            with pytest.raises(ToolRejectedError):
                await governed.invoke(_routed_ctx(), forged, adapter.execute)
    assert all(a.calls == [] for a in adapters.values())
    assert grants.checks == []


def test_routed_catalog_looks_up_contracts_by_tool_and_target() -> None:
    catalog = _routed_catalog()
    for cluster in CLUSTERS:
        contract = catalog.contract(ROUTED_TOOL, cluster)
        assert contract is not None and contract.policy_id == f"synthetic.{cluster}"
    assert catalog.contract(ROUTED_TOOL, "south-wh") is None
    assert {c.target_id for c in catalog.contracts_for(ROUTED_TOOL)} == set(CLUSTERS)


@pytest.mark.parametrize(
    ("contracts", "policies"),
    [
        pytest.param(
            [_routed_contract(c, RegionArgs) for c in CLUSTERS[:2]],
            [_routed_policy(c, RegionArgs) for c in CLUSTERS[:2]],
            id="shared-tool-without-cluster-argument",
        ),
        pytest.param(
            [_routed_contract("east-wh"), _routed_contract("west-wh", RegionArgs)],
            [_routed_policy("east-wh"), _routed_policy("west-wh", RegionArgs)],
            id="different-schemas-per-target",
        ),
        pytest.param(
            [_routed_contract("east-wh"), _routed_contract("east-wh")],
            [_routed_policy("east-wh")],
            id="duplicate-tool-and-target",
        ),
    ],
)
def test_catalog_rejects_ambiguous_routing(
    contracts: list[ToolContract], policies: list[ToolPolicy]
) -> None:
    with pytest.raises(ValueError, match="工具目录"):
        ToolCatalog(contracts, policies)
