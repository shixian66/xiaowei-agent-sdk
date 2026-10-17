"""S2 回答结构协议与恢复实验（锁定 openai-agents 0.22.3）；不实现产品契约。

两种候选 schema（平铺 / 嵌套联合）只在本文件定义，对比 SDK 严格 schema、Vertex 转发、
非法组合、字节预算、一次改写、旧审批 RunState 恢复与旧历史回放。真实 provider 是否接受、
真实模型是否遵守，本文件不证明（见提案 6.10 的四层）。
"""

import json
from typing import Annotated, Any, Literal

import httpx2
import pytest
from agents import (
    Agent,
    AgentOutputSchema,
    ModelBehaviorError,
    RunErrorHandlerInput,
    RunErrorHandlerResult,
    Runner,
    RunState,
    function_tool,
)
from agents.extensions.memory import SQLAlchemySession
from agents.testing import ScriptedModel, assistant_message, function_call
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import text
from tests.sdk_core.synthetic_tools import ready_engine
from tests.sdk_core.test_vertex_model import KEY, VERTEX, Endpoint, _answer

from xiaowei.app import safe_run_config
from xiaowei.model_api import open_model
from xiaowei.models import AgentAnswer

# 由现有容量推导（提案 6.9）：飞书卡片请求 30_000 字节须同时容纳代码生成的事实与卡片结构，
# 模型文字合计取 16_000 字节（约 5300 个汉字，高于旧 advice 4000 字的上限），占 Session
# 历史预算 400_000 的 4%。块数 64：每个事实块渲染为数个组件，留在卡片 200 组件之内。
MAX_TEXT_BYTES = 16_000
MAX_BLOCKS = 64


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FlatBlock(_Strict):
    type: Literal["text", "fact"]
    text: str | None
    evidence_id: str | None
    view: Literal["full", "brief"] | None


def _budget(blocks: tuple[Any, ...]) -> None:
    if len(blocks) > MAX_BLOCKS:
        raise ValueError("块数超出上限")
    size = sum(len((b.text or "").encode()) for b in blocks if b.type == "text")
    if size > MAX_TEXT_BYTES:
        raise ValueError("文字总字节超出上限")


class FlatUnchecked(_Strict):
    blocks: tuple[FlatBlock, ...]
    report_format: Literal["md", "html"] | None
    awaiting_user: bool


class Flat(FlatUnchecked):
    @model_validator(mode="after")
    def _combination(self) -> "Flat":
        for block in self.blocks:
            present = (
                block.text is not None,
                block.evidence_id is not None,
                block.view is not None,
            )
            if present != {"text": (True, False, False), "fact": (False, True, True)}[block.type]:
                raise ValueError("块字段组合非法")
        _budget(self.blocks)
        return self


class TextBlock(_Strict):
    type: Literal["text"]
    text: str


class FactBlock(_Strict):
    type: Literal["fact"]
    evidence_id: str
    view: Literal["full", "brief"]


class Nested(_Strict):
    # 不用 Field(discriminator=...)：它会把 OpenAPI 的 discriminator 关键字写进 schema。
    blocks: tuple[TextBlock | FactBlock, ...]
    report_format: Literal["md", "html"] | None
    awaiting_user: bool

    @model_validator(mode="after")
    def _limits(self) -> "Nested":
        _budget(self.blocks)
        return self


def keywords(schema: object) -> set[str]:
    """schema 中出现的 JSON Schema 关键字（不含属性名与 $defs 下的类型名）。"""
    found: set[str] = set()

    def walk(node: object, *, names: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if not names:
                    found.add(key)
                walk(value, names=key in {"properties", "$defs"} and not names)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found


PRODUCTION = keywords(AgentOutputSchema(AgentAnswer).json_schema())


def dump(**fields: Any) -> str:
    return json.dumps({"report_format": None, "awaiting_user": False, **fields})


def text_block(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value, "evidence_id": None, "view": None}


def fact_block(evidence_id: str) -> dict[str, Any]:
    return {"type": "fact", "text": None, "evidence_id": evidence_id, "view": "full"}


# ---- 第一层：SDK 严格 schema 与关键字 ------------------------------------------------------


def test_flat_uses_only_keywords_the_production_schema_already_sends() -> None:
    schema = AgentOutputSchema(Flat)
    assert schema.is_strict_json_schema()
    assert keywords(schema.json_schema()) <= PRODUCTION


def test_nested_adds_const_and_an_object_union() -> None:
    schema = AgentOutputSchema(Nested)
    assert schema.is_strict_json_schema()
    assert keywords(schema.json_schema()) - PRODUCTION == {"const"}
    union = schema.json_schema()["properties"]["blocks"]["items"]["anyOf"]
    assert union == [{"$ref": "#/$defs/TextBlock"}, {"$ref": "#/$defs/FactBlock"}]
    # 现有回答的 anyOf 只是“某类型或 null”；对象之间的联合是新形态。
    assert "discriminator" not in json.dumps(schema.json_schema())


class EnumTagged(AgentOutputSchema):
    """只改发给 provider 的 schema：单值 ``const`` 写成单值 ``enum``；解析仍按 Literal 类型。"""

    def json_schema(self) -> dict[str, Any]:
        def rewrite(node: Any) -> Any:
            if isinstance(node, dict):
                out = {k: rewrite(v) for k, v in node.items() if k != "const"}
                if "const" in node:
                    out["enum"] = [node["const"]]
                return out
            return [rewrite(v) for v in node] if isinstance(node, list) else node

        return rewrite(super().json_schema())


def test_nested_with_single_value_enum_needs_no_new_keyword() -> None:
    schema = EnumTagged(Nested)
    assert schema.is_strict_json_schema()
    assert keywords(schema.json_schema()) <= PRODUCTION
    text_type = schema.json_schema()["$defs"]["TextBlock"]["properties"]["type"]
    assert text_type == {"enum": ["text"], "title": "Type", "type": "string"}
    # 剩下唯一的新形态：对象之间的 anyOf（现有回答只有“类型或 null”）。
    assert len(schema.json_schema()["properties"]["blocks"]["items"]["anyOf"]) == 2
    with pytest.raises(ModelBehaviorError):
        schema.validate_json(dump(blocks=[{"type": "fact", "text": "仿写", "evidence_id": "e"}]))


def test_pydantic_discriminator_would_leak_an_openapi_keyword() -> None:
    class Tagged(_Strict):
        blocks: tuple[Annotated[TextBlock | FactBlock, Field(discriminator="type")], ...]

    assert "discriminator" in keywords(AgentOutputSchema(Tagged).json_schema())


# ---- 非法组合与字节预算 ----------------------------------------------------------------------

ILLEGAL = [
    {"type": "fact", "text": None, "evidence_id": None, "view": None},
    {"type": "text", "text": "结论", "evidence_id": "ev_1", "view": "full"},
    {"type": "fact", "text": "仿写事实", "evidence_id": "ev_1", "view": "full"},
    {"type": "text", "text": None, "evidence_id": None, "view": None},
    # 只缺一个字段的半合法组合：首版校验漏掉过它，平铺写法的组合校验须逐字段写全。
    {"type": "text", "text": "结论", "evidence_id": "ev_1", "view": None},
    {"type": "fact", "text": None, "evidence_id": "ev_1", "view": None},
]


@pytest.mark.parametrize("block", ILLEGAL)
def test_flat_schema_alone_admits_illegal_combinations(block: dict[str, Any]) -> None:
    AgentOutputSchema(FlatUnchecked).validate_json(dump(blocks=[block]))
    with pytest.raises(ModelBehaviorError):
        AgentOutputSchema(Flat).validate_json(dump(blocks=[block]))


@pytest.mark.parametrize(
    "block",
    [
        {"type": "fact", "evidence_id": None, "view": None},
        {"type": "text", "text": "结论", "evidence_id": "ev_1"},
        {"type": "fact", "text": "仿写事实", "evidence_id": "ev_1", "view": "full"},
        {"type": "text"},
    ],
)
def test_nested_types_reject_illegal_combinations(block: dict[str, Any]) -> None:
    with pytest.raises(ModelBehaviorError):
        AgentOutputSchema(Nested).validate_json(dump(blocks=[block]))


def test_max_length_counts_characters_so_the_budget_is_checked_in_bytes() -> None:
    class Chars(_Strict):
        text: str = Field(max_length=MAX_TEXT_BYTES)

    cjk = "中" * (MAX_TEXT_BYTES // 3 + 1)
    assert Chars(text=cjk)  # schema 的 maxLength 只数字符，挡不住 3 倍字节
    with pytest.raises(ValidationError, match="字节"):
        Flat.model_validate_json(dump(blocks=[text_block(cjk)]))
    with pytest.raises(ModelBehaviorError):
        AgentOutputSchema(Flat).validate_json(dump(blocks=[text_block(cjk)]))
    fits = "中" * (MAX_TEXT_BYTES // 3)
    assert AgentOutputSchema(Flat).validate_json(dump(blocks=[text_block(fits)]))
    with pytest.raises(ValidationError, match="块数"):
        Nested.model_validate_json(dump(blocks=[{"type": "text", "text": "x"}] * (MAX_BLOCKS + 1)))


# ---- 第二层：本地 VertexModel 原样转发 -------------------------------------------------------


@pytest.mark.parametrize("candidate", [Flat, Nested, EnumTagged])
async def test_vertex_forwards_candidate_schema_verbatim(
    monkeypatch: pytest.MonkeyPatch, candidate: type[Any]
) -> None:
    monkeypatch.setenv("XIAOWEI_TEST_VERTEX_KEY", KEY)
    block = text_block("东区正常") if candidate is Flat else {"type": "text", "text": "东区正常"}
    schema = EnumTagged(Nested) if candidate is EnumTagged else AgentOutputSchema(candidate)
    endpoint = Endpoint([_answer(dump(blocks=[block]))])
    async with open_model(VERTEX, transport=endpoint.transport) as binding:
        agent = Agent(
            name="s2-vertex",
            model=binding.model,
            model_settings=binding.settings,
            output_type=schema,
        )
        result = await Runner.run(agent, "东区状态？", run_config=safe_run_config())
    sent = endpoint.bodies()[0]["generationConfig"]["responseJsonSchema"]
    assert sent == schema.json_schema()
    assert result.final_output.blocks[0].text == "东区正常"


async def test_vertex_does_not_translate_a_provider_rejection_into_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 provider 若拒绝 const 等关键字，本地适配器如实失败，不降级为无约束输出。"""
    monkeypatch.setenv("XIAOWEI_TEST_VERTEX_KEY", KEY)
    rejected = httpx2.Response(
        400, json={"error": {"code": 400, "message": "schema", "status": "INVALID_ARGUMENT"}}
    )
    endpoint = Endpoint([rejected])
    async with open_model(VERTEX, transport=endpoint.transport) as binding:
        agent = Agent(
            name="s2-vertex",
            model=binding.model,
            model_settings=binding.settings,
            output_type=AgentOutputSchema(Nested),
        )
        with pytest.raises(Exception) as caught:
            await Runner.run(agent, "东区状态？", run_config=safe_run_config())
    assert not isinstance(caught.value, AssertionError)
    assert len(endpoint.requests) == 1


# ---- 一次无工具改写（与 app.repair_final_output 同一 SDK 用法） -----------------------------

VISIBLE = frozenset({"ev_1"})


class Cited(Flat):
    """把“引用当前上下文外的证据”放进 schema 预检，模拟每轮可信记录检查。"""

    @model_validator(mode="after")
    def _known(self) -> "Cited":
        if any(b.evidence_id not in VISIBLE for b in self.blocks if b.type == "fact"):
            raise ValueError("unknown_evidence")
        return self


def repairing(agent: Agent[Any]) -> Any:
    async def repair(failure: RunErrorHandlerInput[Any]) -> RunErrorHandlerResult | None:
        fixer = agent.clone(tools=[], instructions="上一条最终回答未通过校验，只改写，不调用工具。")
        try:
            fixed = await Runner.run(
                fixer, failure.run_data.history, max_turns=1, run_config=safe_run_config()
            )
        except ModelBehaviorError:
            return None
        return RunErrorHandlerResult(final_output=fixed.final_output)

    return repair


def query_agent(script: list[Any], executed: list[str]) -> Agent[Any]:
    @function_tool
    def order_total(region: str) -> str:
        """合成地区订单总数。"""
        executed.append(region)
        return json.dumps({"evidence_id": "ev_1", "total": 100})

    return Agent(
        name="s2-repair",
        model=ScriptedModel(script),
        tools=[order_total],
        output_type=AgentOutputSchema(Cited),
    )


@pytest.mark.parametrize(
    "invalid",
    [
        dump(blocks=[fact_block("ev_9")]),  # 未知引用
        dump(blocks=[{**text_block("结论"), "evidence_id": "ev_1"}]),  # 字段组合
        dump(blocks=[text_block("中" * (MAX_TEXT_BYTES // 3 + 1))]),  # 字节预算
    ],
    ids=["unknown", "combination", "bytes"],
)
async def test_invalid_answer_is_repaired_once_without_another_tool_call(invalid: str) -> None:
    executed: list[str] = []
    valid = dump(blocks=[text_block("东区 100 单"), fact_block("ev_1")])
    agent = query_agent(
        [
            [function_call("order_total", {"region": "east"}, call_id="q1")],
            [assistant_message(invalid)],
            [assistant_message(valid)],
        ],
        executed,
    )
    result = await Runner.run(
        agent,
        "东区订单？",
        run_config=safe_run_config(),
        error_handlers={"invalid_final_output": repairing(agent)},
    )
    assert executed == ["east"]
    assert [b.type for b in result.final_output.blocks] == ["text", "fact"]


async def test_failed_repair_surfaces_the_original_error() -> None:
    executed: list[str] = []
    invalid = dump(blocks=[fact_block("ev_9")])
    agent = query_agent(
        [
            [function_call("order_total", {"region": "east"}, call_id="q1")],
            [assistant_message(invalid)],
            [assistant_message(invalid)],
        ],
        executed,
    )
    with pytest.raises(ModelBehaviorError):
        await Runner.run(
            agent,
            "东区订单？",
            run_config=safe_run_config(),
            error_handlers={"invalid_final_output": repairing(agent)},
        )
    assert executed == ["east"]


async def test_turn_limit_wrap_up_uses_the_same_schema() -> None:
    executed: list[str] = []
    agent = query_agent(
        [
            [function_call("order_total", {"region": "east"}, call_id="q1")],
            [
                assistant_message(
                    dump(blocks=[text_block("步骤用完，基于已有结果"), fact_block("ev_1")])
                )
            ],
        ],
        executed,
    )

    async def finish(failure: RunErrorHandlerInput[Any]) -> RunErrorHandlerResult:
        closing = agent.clone(tools=[], instructions="步骤已用完，只基于已有证据收尾。")
        done = await Runner.run(
            closing, failure.run_data.history, max_turns=1, run_config=safe_run_config()
        )
        return RunErrorHandlerResult(final_output=done.final_output)

    result = await Runner.run(
        agent,
        "东区订单？",
        max_turns=1,
        run_config=safe_run_config(),
        error_handlers={"max_turns": finish},
    )
    assert executed == ["east"]
    assert isinstance(result.final_output, Cited)


# ---- 旧格式恢复：审批暂停 RunState 与 Session 历史（真实 PostgreSQL） ------------------------

OLD_FINAL = AgentAnswer(
    evidence_ids=("ev_1",), inferences=[], clarification=None, advice=None
).model_dump_json()


def approval_agent(output: type[BaseModel], script: list[Any], effects: list[str]) -> Agent[Any]:
    @function_tool(needs_approval=True, failure_error_function=None)
    async def create_silence(alert: str) -> str:
        """合成静默写入。"""
        effects.append(alert)
        return json.dumps({"evidence_id": "ev_1", "state": "succeeded"})

    return Agent(
        name="xiaowei",  # 恢复按名称匹配 Agent，产品沿用同名
        model=ScriptedModel(script),
        tools=[create_silence],
        output_type=AgentOutputSchema(output),
    )


@pytest.mark.loopback
async def test_old_paused_run_state_resumes_into_the_new_schema(postgres_url: str) -> None:
    effects: list[str] = []
    old = approval_agent(
        AgentAnswer,
        [[function_call("create_silence", {"alert": "disk"}, call_id="c1")]],
        effects,
    )
    async with ready_engine(postgres_url) as engine:
        session = SQLAlchemySession("s2-old", engine=engine, create_tables=True)
        paused = await Runner.run(
            old, "静默磁盘告警", session=session, run_config=safe_run_config()
        )
        assert len(paused.interruptions) == 1 and effects == []
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE s2_state (snapshot TEXT NOT NULL)"))
            await conn.execute(
                text("INSERT INTO s2_state VALUES (:s)"), {"s": paused.to_state().to_string()}
            )
            saved = (await conn.execute(text("SELECT snapshot FROM s2_state"))).scalar_one()

        new = approval_agent(
            Flat,
            [[assistant_message(dump(blocks=[text_block("已静默"), fact_block("ev_1")]))]],
            effects,
        )
        state = await RunState.from_string(new, saved)
        state.approve(state.get_interruptions()[0], always_approve=False)
        resumed = await Runner.run(new, state, run_config=safe_run_config())
    assert effects == ["disk"]
    assert isinstance(resumed.final_output, Flat)
    assert resumed.final_output.blocks[1].evidence_id == "ev_1"


@pytest.mark.loopback
async def test_old_paused_run_state_rejected_by_new_agent_runs_nothing(postgres_url: str) -> None:
    effects: list[str] = []
    old = approval_agent(
        AgentAnswer,
        [[function_call("create_silence", {"alert": "disk"}, call_id="c1")]],
        effects,
    )
    paused = await Runner.run(old, "静默磁盘告警", run_config=safe_run_config())
    new = approval_agent(Flat, [[assistant_message(dump(blocks=[text_block("未执行")]))]], effects)
    state = await RunState.from_string(new, paused.to_state().to_string())
    state.reject(state.get_interruptions()[0])
    resumed = await Runner.run(new, state, run_config=safe_run_config())
    assert effects == []
    assert resumed.final_output.blocks[0].text == "未执行"


@pytest.mark.loopback
async def test_old_answer_history_replays_and_new_turn_answers_in_new_schema(
    postgres_url: str,
) -> None:
    async with ready_engine(postgres_url) as engine:
        session = SQLAlchemySession("s2-history", engine=engine, create_tables=True)
        old = Agent(
            name="xiaowei",
            model=ScriptedModel([[assistant_message(OLD_FINAL)]]),
            output_type=AgentOutputSchema(AgentAnswer),
        )
        await Runner.run(old, "东区订单？", session=session, run_config=safe_run_config())
        model = ScriptedModel([[assistant_message(dump(blocks=[text_block("和上次一致")]))]])
        new = Agent(name="xiaowei", model=model, output_type=AgentOutputSchema(Flat))
        result = await Runner.run(
            new, "和上次比呢？", session=session, run_config=safe_run_config()
        )
    replayed = [i for i in model.first_call.input if i.get("role") == "assistant"]
    assert [i["content"][0]["text"] for i in replayed] == [OLD_FINAL]
    assert isinstance(result.final_output, Flat)
