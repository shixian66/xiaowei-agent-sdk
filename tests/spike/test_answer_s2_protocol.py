"""S2 回答结构协议与恢复实验（锁定 openai-agents 0.22.3）；不实现产品契约。

两种候选 schema（平铺 / 嵌套联合）只在本文件定义。最终选定的是 ``SELECTED``：嵌套类型
＋发送前单值 ``const`` 改写为单值 ``enum``＋合成的引用预检。改写、收尾、预算、历史回放与
审批恢复都对两种候选参数化，平铺作为退路同样覆盖。

验证边界：
- 只覆盖提案 6.10 的前两层（SDK 生成与本地校验、本地 ``VertexModel`` 转发）；真实 provider
  是否接受、真实模型是否遵守不由本文件证明，也不能由“关键字集合相同”推导。
- 引用预检用合成可见集合 ``VISIBLE``，不代表正式 Evidence 授权链。
- 审批恢复只证明 SDK 机制与实验中的 PostgreSQL 存取；不证明正式 Action claim、Evidence、
  PolicySession、权限复核与并发防重。单次执行一次不等于重复恢复的全局幂等。
- 容量反例用现有渲染器，不是 S3 新渲染器的测量。
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
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
from tests.sdk_core.test_feishu_render import fact as render_fact
from tests.sdk_core.test_vertex_model import KEY, VERTEX, Endpoint, _answer

from xiaowei.app import safe_run_config
from xiaowei.feishu_render import MAX_REQUEST_BYTES, build_feishu_message, request_bytes
from xiaowei.model_api import open_model
from xiaowei.models import Delivery, DeliveryFactRef, DeliveryText

# S2 对比的“现有生产 schema”是 S3 之前的回答格式，S3 后以 LegacyAnswer 保留。
from xiaowei.models import LegacyAnswer as AgentAnswer

# 初始资源上限，只约束模型输出规模（提案 8.1）。能否内联发送由完整渲染后的请求字节与组件数
# 决定；Session 容量按实际序列化历史与配置上限计算；放不下时转附件或明确失败由 S3 交付链验证。
MAX_TEXT_BYTES = 16_000
MAX_BLOCKS = 64
CARD_COMPONENTS = 200  # 现有渲染器的卡片组件上限（feishu_render.build_feishu_message）


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _budget(blocks: tuple[Any, ...]) -> None:
    if len(blocks) > MAX_BLOCKS:
        raise ValueError("块数超出上限")
    size = sum(len(b.text.encode()) for b in blocks if b.type == "text" and b.text is not None)
    if size > MAX_TEXT_BYTES:
        raise ValueError("文字总字节超出上限")


VISIBLE = frozenset({"ev_1"})


def _known(blocks: tuple[Any, ...]) -> None:
    """合成引用预检：模拟每轮可信记录检查，不是正式 Evidence 授权链。"""
    if any(b.evidence_id not in VISIBLE for b in blocks if b.type == "fact"):
        raise ValueError("unknown_evidence")


class FlatBlock(_Strict):
    type: Literal["text", "fact"]
    text: str | None
    evidence_id: str | None
    view: Literal["full", "brief"] | None


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


class CitedFlat(Flat):
    @model_validator(mode="after")
    def _cited(self) -> "CitedFlat":
        _known(self.blocks)
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


class CitedNested(Nested):
    @model_validator(mode="after")
    def _cited(self) -> "CitedNested":
        _known(self.blocks)
        return self


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


@dataclass(frozen=True)
class Candidate:
    name: str
    output: Callable[[], AgentOutputSchema]
    answer_type: type[BaseModel]
    text: Callable[[str], dict[str, Any]]
    fact: Callable[[str], dict[str, Any]]
    mixed: dict[str, Any]  # 非法字段组合：文字块夹带证据编号

    def answer(self, *blocks: dict[str, Any]) -> str:
        return dump(blocks=list(blocks))


SELECTED = Candidate(
    "selected",
    lambda: EnumTagged(CitedNested),
    CitedNested,
    lambda v: {"type": "text", "text": v},
    lambda e: {"type": "fact", "evidence_id": e, "view": "full"},
    {"type": "text", "text": "结论", "evidence_id": "ev_1"},
)
FLAT = Candidate(
    "flat",
    lambda: AgentOutputSchema(CitedFlat),
    CitedFlat,
    text_block,
    fact_block,
    {"type": "text", "text": "结论", "evidence_id": "ev_1", "view": None},
)
CANDIDATES = pytest.mark.parametrize("cand", [SELECTED, FLAT], ids=lambda c: c.name)


def assert_sent_schema(model: ScriptedModel, cand: Candidate) -> None:
    """每次模型调用（含改写、收尾）拿到的都是同一选定 schema，且无生产以外的关键字。"""
    expected = cand.output().json_schema()
    for call in model.calls:
        assert call.output_schema is not None
        sent = call.output_schema.json_schema()
        assert sent == expected
        assert keywords(sent) <= PRODUCTION


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


def test_selected_schema_needs_no_new_keyword_and_keeps_the_precheck() -> None:
    schema = SELECTED.output()
    assert schema.is_strict_json_schema()
    assert keywords(schema.json_schema()) <= PRODUCTION
    text_type = schema.json_schema()["$defs"]["TextBlock"]["properties"]["type"]
    assert text_type == {"enum": ["text"], "title": "Type", "type": "string"}
    # 剩下唯一的新形态：对象之间的 anyOf；它在真实 Vertex 上是否被接受仍待验证。
    assert len(schema.json_schema()["properties"]["blocks"]["items"]["anyOf"]) == 2
    # schema 转换不影响本地解析：组合仍按类型拒绝，引用预检仍生效。
    with pytest.raises(ModelBehaviorError):
        schema.validate_json(dump(blocks=[{"type": "fact", "text": "仿写", "evidence_id": "e"}]))
    with pytest.raises(ModelBehaviorError):
        schema.validate_json(SELECTED.answer(SELECTED.fact("ev_9")))
    assert isinstance(schema.validate_json(SELECTED.answer(SELECTED.fact("ev_1"))), CitedNested)


def test_pydantic_discriminator_would_leak_an_openapi_keyword() -> None:
    class Tagged(_Strict):
        blocks: tuple[Annotated[TextBlock | FactBlock, Field(discriminator="type")], ...]

    assert "discriminator" in keywords(AgentOutputSchema(Tagged).json_schema())


# ---- 非法组合 --------------------------------------------------------------------------------

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
        FLAT.output().validate_json(dump(blocks=[block]))


@pytest.mark.parametrize(
    "block",
    [
        {"type": "fact", "evidence_id": None, "view": None},
        {"type": "text", "text": "结论", "evidence_id": "ev_1"},
        {"type": "fact", "text": "仿写事实", "evidence_id": "ev_1", "view": "full"},
        {"type": "text"},
        {"type": "fact", "evidence_id": "ev_1"},
    ],
)
def test_selected_types_reject_illegal_combinations(block: dict[str, Any]) -> None:
    with pytest.raises(ModelBehaviorError):
        SELECTED.output().validate_json(dump(blocks=[block]))


# ---- 资源上限：字节与块数在代码校验 ----------------------------------------------------------


def test_max_length_counts_characters_not_bytes() -> None:
    class Chars(_Strict):
        text: str = Field(max_length=MAX_TEXT_BYTES)

    cjk = "中" * (MAX_TEXT_BYTES // 3 + 1)
    assert Chars(text=cjk)  # schema 的 maxLength 只数字符，挡不住 3 倍字节
    with pytest.raises(ValidationError, match="字节"):
        CitedNested.model_validate_json(SELECTED.answer(SELECTED.text(cjk)))


@CANDIDATES
@pytest.mark.parametrize(
    ("pieces", "fits"),
    [
        (["a" * MAX_TEXT_BYTES], True),
        (["a" * (MAX_TEXT_BYTES + 1)], False),
        (["a" * 8_000, "b" * 8_000], True),  # 多块累计
        (["a" * 8_000, "b" * 8_000, "c"], False),
        (["中" * 5_333], True),  # 5333 字符 = 15_999 字节
        (["中" * 5_334], False),  # 5334 字符 = 16_002 字节，字符数远低于上限
        (["😀" * 4_000], True),  # 4000 字符 = 16_000 字节
        (["😀" * 4_000, "x"], False),
    ],
    ids=[
        "ascii-max",
        "ascii-over",
        "sum-max",
        "sum-over",
        "cjk-max",
        "cjk-over",
        "emoji-max",
        "emoji-over",
    ],
)
def test_text_budget_is_cumulative_utf8_bytes(
    cand: Candidate, pieces: list[str], fits: bool
) -> None:
    payload = cand.answer(*map(cand.text, pieces))
    if fits:
        assert isinstance(cand.output().validate_json(payload), cand.answer_type)
    else:
        with pytest.raises(ModelBehaviorError):
            cand.output().validate_json(payload)
        with pytest.raises(ValidationError, match="字节"):
            cand.answer_type.model_validate_json(payload)


@CANDIDATES
@pytest.mark.parametrize(("count", "fits"), [(MAX_BLOCKS, True), (MAX_BLOCKS + 1, False)])
def test_block_count_limit(cand: Candidate, count: int, fits: bool) -> None:
    payload = cand.answer(*[cand.fact("ev_1")] * count)
    if fits:
        assert len(cand.output().validate_json(payload).blocks) == MAX_BLOCKS
    else:
        with pytest.raises(ValidationError, match="块数"):
            cand.answer_type.model_validate_json(payload)


# ---- 容量反例：输出上限不保证内联交付（现有渲染器） ------------------------------------------


def _delivery(facts: tuple[Any, ...], texts: tuple[str, ...] = ()) -> Delivery:
    # S3 后渲染器读取正文块：文字在前，事实按块放置。
    return Delivery(
        content="x",
        channel="feishu",
        evidence_ids=tuple(f.evidence_id for f in facts),
        facts=facts,
        blocks=(
            *(DeliveryText(text=t) for t in texts),
            *(DeliveryFactRef(evidence_id=f.evidence_id) for f in facts),
        ),
    )


def _components(value: object) -> int:
    if isinstance(value, list):
        return sum(_components(v) for v in value)
    if isinstance(value, dict):
        return int("tag" in value) + sum(_components(v) for v in value.values())
    return 0


def _render(delivery: Delivery, max_bytes: int = MAX_REQUEST_BYTES) -> dict[str, Any]:
    return build_feishu_message(
        delivery, 3500, "oc_test", max_bytes=max_bytes, platform_limits=True
    )


def test_text_within_budget_can_exceed_the_card_request_limit() -> None:
    """16_000 字节双引号文字 + 一条事实：JSON 两次转义与请求包装使请求远超 30_000 字节。"""
    fact = render_fact()
    quotes = tuple('"' * 2_000 for _ in range(8))
    assert sum(len(q.encode()) for q in quotes) == MAX_TEXT_BYTES
    unbounded = _render(_delivery((fact,), quotes), max_bytes=10**7)
    assert request_bytes(unbounded, "oc_test") > 2 * MAX_REQUEST_BYTES
    # 现有渲染器在真实上限下降级，不发超限请求；新契约的降级与说明由 S3 验证。
    bounded = _render(_delivery((fact,), quotes))
    assert request_bytes(bounded, "oc_test") <= MAX_REQUEST_BYTES
    assert bounded != unbounded


def test_block_cap_does_not_keep_facts_within_card_components() -> None:
    """64 条小事实：每条事实渲染为多个组件，总数远超 200，现有渲染器只能发容量说明。"""

    def facts(n: int) -> tuple[Any, ...]:
        return tuple(render_fact(evidence_id=f"ev_{i}") for i in range(n))

    one = _components(_render(_delivery(facts(1)), max_bytes=10**7))
    two = _components(_render(_delivery(facts(2)), max_bytes=10**7))
    assert MAX_BLOCKS * (two - one) > CARD_COMPONENTS
    notice = _render(_delivery(facts(MAX_BLOCKS)), max_bytes=10**7)
    assert "card" not in notice and "容量" in notice["text"]


def test_answer_json_is_larger_than_its_raw_text_bytes() -> None:
    """模型文字原始字节不等于序列化后的回答或 Session 占用；Session 按实际历史计算。"""
    payload = SELECTED.answer(SELECTED.text('"' * MAX_TEXT_BYTES))
    assert isinstance(SELECTED.output().validate_json(payload), CitedNested)
    assert len(payload.encode()) > 2 * MAX_TEXT_BYTES


# ---- 第二层：本地 VertexModel 原样转发 -------------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "block"),
    [
        (AgentOutputSchema(Flat), text_block("东区正常")),
        (AgentOutputSchema(Nested), {"type": "text", "text": "东区正常"}),
        (SELECTED.output(), SELECTED.text("东区正常")),
    ],
    ids=["flat", "nested-const", "selected"],
)
async def test_vertex_forwards_candidate_schema_verbatim(
    monkeypatch: pytest.MonkeyPatch, schema: AgentOutputSchema, block: dict[str, Any]
) -> None:
    monkeypatch.setenv("XIAOWEI_TEST_VERTEX_KEY", KEY)
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
    """真实 provider 若拒绝对象 anyOf 等形态，本地适配器如实失败，不降级为无约束输出。"""
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
            output_type=SELECTED.output(),
        )
        with pytest.raises(Exception) as caught:
            await Runner.run(agent, "东区状态？", run_config=safe_run_config())
    assert not isinstance(caught.value, AssertionError)
    assert len(endpoint.requests) == 1


# ---- 一次无工具改写与收尾（与 app.repair_final_output / finish_on_limit 同一 SDK 用法） -------


def query_agent(cand: Candidate, model: ScriptedModel, executed: list[str]) -> Agent[Any]:
    @function_tool
    def order_total(region: str) -> str:
        """合成地区订单总数。"""
        executed.append(region)
        return json.dumps({"evidence_id": "ev_1", "total": 100})

    return Agent(name="s2-repair", model=model, tools=[order_total], output_type=cand.output())


def repairing(agent: Agent[Any], repairs: list[int]) -> Any:
    async def repair(failure: RunErrorHandlerInput[Any]) -> RunErrorHandlerResult | None:
        repairs.append(1)
        fixer = agent.clone(tools=[], instructions="上一条最终回答未通过校验，只改写，不调用工具。")
        try:
            fixed = await Runner.run(
                fixer, failure.run_data.history, max_turns=1, run_config=safe_run_config()
            )
        except ModelBehaviorError:
            return None
        return RunErrorHandlerResult(final_output=fixed.final_output)

    return repair


QUERY = [function_call("order_total", {"region": "east"}, call_id="q1")]


def invalid_answer(cand: Candidate, kind: str) -> str:
    return {
        "unknown": lambda: cand.answer(cand.fact("ev_9")),
        "combination": lambda: cand.answer(cand.mixed),
        "bytes": lambda: cand.answer(cand.text("中" * 4_000), cand.text("中" * 1_334)),
    }[kind]()


@CANDIDATES
@pytest.mark.parametrize("kind", ["unknown", "combination", "bytes"])
async def test_invalid_answer_is_repaired_once_without_another_tool_call(
    cand: Candidate, kind: str
) -> None:
    executed: list[str] = []
    repairs: list[int] = []
    valid = cand.answer(cand.text("东区 100 单"), cand.fact("ev_1"))
    spare = cand.answer(cand.text("不应被调用"))
    model = ScriptedModel(
        [
            QUERY,
            [assistant_message(invalid_answer(cand, kind))],
            [assistant_message(valid)],
            [assistant_message(spare)],
        ]
    )
    agent = query_agent(cand, model, executed)
    result = await Runner.run(
        agent,
        "东区订单？",
        run_config=safe_run_config(),
        error_handlers={"invalid_final_output": repairing(agent, repairs)},
    )
    assert executed == ["east"]
    assert repairs == [1] and len(model.calls) == 3 and model.remaining_steps == 1
    assert type(result.final_output) is cand.answer_type
    assert [b.type for b in result.final_output.blocks] == ["text", "fact"]
    assert model.calls[-1].tools == []  # 改写调用不带业务工具
    assert_sent_schema(model, cand)


@CANDIDATES
async def test_failed_repair_surfaces_the_original_error(cand: Candidate) -> None:
    executed: list[str] = []
    repairs: list[int] = []
    invalid = invalid_answer(cand, "unknown")
    spare = cand.answer(cand.text("不应被调用"))
    model = ScriptedModel(
        [
            QUERY,
            [assistant_message(invalid)],
            [assistant_message(invalid)],
            [assistant_message(spare)],
        ]
    )
    agent = query_agent(cand, model, executed)
    with pytest.raises(ModelBehaviorError):
        await Runner.run(
            agent,
            "东区订单？",
            run_config=safe_run_config(),
            error_handlers={"invalid_final_output": repairing(agent, repairs)},
        )
    assert executed == ["east"]
    assert repairs == [1] and len(model.calls) == 3 and model.remaining_steps == 1
    assert_sent_schema(model, cand)


@CANDIDATES
async def test_turn_limit_wrap_up_uses_the_same_schema(cand: Candidate) -> None:
    executed: list[str] = []
    closing_answer = cand.answer(cand.text("步骤用完，基于已有结果"), cand.fact("ev_1"))
    model = ScriptedModel([QUERY, [assistant_message(closing_answer)]])
    agent = query_agent(cand, model, executed)

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
    assert len(model.calls) == 2 and model.calls[-1].tools == []
    assert type(result.final_output) is cand.answer_type
    assert_sent_schema(model, cand)


# ---- 旧格式恢复：Session 历史与审批暂停 RunState（隔离 PostgreSQL） --------------------------

OLD_FINAL = AgentAnswer(
    evidence_ids=("ev_1",), inferences=[], clarification=None, advice=None
).model_dump_json()


def assistant_texts(items: list[Any]) -> list[str]:
    return [
        part["text"]
        for item in items
        if isinstance(item, dict) and item.get("role") == "assistant"
        for part in item["content"]
    ]


async def old_answer_turn(session: SQLAlchemySession) -> None:
    old = Agent(
        name="xiaowei",  # 恢复与回放按名称匹配 Agent，产品沿用同名
        model=ScriptedModel([[assistant_message(OLD_FINAL)]]),
        output_type=AgentOutputSchema(AgentAnswer),
    )
    await Runner.run(old, "东区订单？", session=session, run_config=safe_run_config())


@CANDIDATES
@pytest.mark.loopback
async def test_old_answer_history_replays_and_new_turn_answers_in_new_schema(
    postgres_url: str, cand: Candidate
) -> None:
    async with ready_engine(postgres_url) as engine:
        session = SQLAlchemySession(f"s2-history-{cand.name}", engine=engine, create_tables=True)
        await old_answer_turn(session)
        model = ScriptedModel([[assistant_message(cand.answer(cand.text("和上次一致")))]])
        new = Agent(name="xiaowei", model=model, output_type=cand.output())
        result = await Runner.run(
            new, "和上次比呢？", session=session, run_config=safe_run_config()
        )
    assert assistant_texts(model.calls[0].input) == [OLD_FINAL]
    assert type(result.final_output) is cand.answer_type
    assert_sent_schema(model, cand)


def approval_agent(
    output: AgentOutputSchema, model: ScriptedModel, effects: list[str]
) -> Agent[Any]:
    @function_tool(needs_approval=True, failure_error_function=None)
    async def create_silence(alert: str) -> str:
        """合成静默写入。"""
        effects.append(alert)
        return json.dumps({"evidence_id": "ev_1", "state": "succeeded"})

    return Agent(name="xiaowei", model=model, tools=[create_silence], output_type=output)


@CANDIDATES
@pytest.mark.parametrize("approve", [True, False], ids=["approve", "reject"])
@pytest.mark.loopback
async def test_old_paused_run_state_resumes_into_the_new_schema(
    postgres_url: str, cand: Candidate, approve: bool
) -> None:
    effects: list[str] = []
    key = str(uuid.uuid4())
    async with ready_engine(postgres_url) as engine:
        # 1–2. 同一会话先存一条旧格式回答，下一轮调用需审批的工具并暂停（旧 Agent、旧 schema）。
        session = SQLAlchemySession(f"s2-pause-{key}", engine=engine, create_tables=True)
        await old_answer_turn(session)
        old = approval_agent(
            AgentOutputSchema(AgentAnswer),
            ScriptedModel([[function_call("create_silence", {"alert": "disk"}, call_id="c1")]]),
            effects,
        )
        paused = await Runner.run(
            old, "静默磁盘告警", session=session, run_config=safe_run_config()
        )
        assert len(paused.interruptions) == 1 and effects == []
        snapshot = paused.to_state().to_string()
        # 暂停状态的输入历史本身就含旧格式回答，不能以“暂停状态不含旧回答”为兼容前提。
        assert assistant_texts(json.loads(snapshot)["original_input"]) == [OLD_FINAL]

        # 3–4. 快照写入 PostgreSQL 并提交，再从新连接读回。
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE IF NOT EXISTS s2_state (id TEXT PRIMARY KEY, snapshot TEXT)")
            )
            await conn.execute(
                text("INSERT INTO s2_state VALUES (:id, :s)"), {"id": key, "s": snapshot}
            )
        async with engine.connect() as conn:
            saved = (
                await conn.execute(text("SELECT snapshot FROM s2_state WHERE id=:id"), {"id": key})
            ).scalar_one()
    assert saved == snapshot

    # 5–6. 同名、选定 schema 的新 Agent 恢复；批准与拒绝各自独立运行。
    reply = cand.answer(cand.text("已静默" if approve else "未执行"), cand.fact("ev_1"))
    model = ScriptedModel([[assistant_message(reply)]])
    new = approval_agent(cand.output(), model, effects)
    state = await RunState.from_string(new, saved)
    (pending,) = state.get_interruptions()
    if approve:
        state.approve(pending, always_approve=False)
    else:
        state.reject(pending)
    resumed = await Runner.run(new, state, run_config=safe_run_config())

    assert effects == (["disk"] if approve else [])
    assert type(resumed.final_output) is cand.answer_type
    assert resumed.final_output.blocks[1].evidence_id == "ev_1"
    assert assistant_texts(model.calls[0].input)[0] == OLD_FINAL  # 旧格式历史带入恢复
    assert len(model.calls) == 1
    assert_sent_schema(model, cand)
