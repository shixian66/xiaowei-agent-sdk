"""Task 2：Evidence 记录、四种数据投影、最终回答校验与应用表版本。真实 PostgreSQL + 真 Runner。"""

import json
from collections.abc import Callable
from importlib.resources import files
from typing import Any

import pytest
from agents import Agent, Runner
from agents.exceptions import ModelBehaviorError
from agents.extensions.memory import SQLAlchemySession
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message, function_call
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.sdk_core.synthetic_tools import (
    CONTRACTS,
    OTHER_TARGET,
    POLICIES,
    PRIVATE_NOTE,
    PROJECTIONS,
    RETENTION_SECONDS,
    START,
    TOTAL_TOOL,
    Clock,
    Grants,
    RecordingAdapter,
    RegionArgs,
    catalog,
    cited,
    context,
    model_data,
    ready_engine,
    request,
    sdk_tool,
    secret,
    store,
)

from xiaowei.evidence import AnswerRejectedError, EvidenceStore, EvidenceUnavailableError
from xiaowei.governance import GovernedTools, Projection, ToolCatalog, ToolPolicy
from xiaowei.models import (
    AgentAnswer,
    AnswerInference,
    Audience,
    Channel,
    RunContext,
    ToolCall,
    ToolObservation,
)
from xiaowei.storage import (
    APP_TABLES,
    SDK_MESSAGES_TABLE,
    SDK_SESSIONS_TABLE,
    StorageNotInitializedError,
    StorageVersionMismatchError,
    check_storage,
    initialize_storage,
    open_engine,
)

pytestmark = pytest.mark.loopback

_FACTS_HEADER = "工具结果"
_ANALYSIS_HEADER = "分析建议"
_ADVICE_HEADER = "建议（本轮未执行业务查询；模型生成，未经系统核实）"


async def _record(
    evidence: EvidenceStore, ctx: RunContext | None = None, adapter: RecordingAdapter | None = None
) -> str:
    ctx = ctx or context()
    req = request()
    observation = await (adapter or RecordingAdapter()).execute(req)
    return (await evidence.record(ctx, req, observation)).evidence_id


async def _saved_text(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT * FROM xiaowei_evidence"))).all()
    return json.dumps([[str(v) for v in row] for row in rows], ensure_ascii=False)


def _expected_fields(audience: Audience, channel: Channel) -> set[str]:
    """模型文字会写入 Session 并到达渠道，Session 又回放给模型：两者受三方共同字段约束。"""
    declared = set(PROJECTIONS[audience].fields)
    if audience in ("model", "session"):
        return declared.intersection(
            *(PROJECTIONS[a].fields for a in ("model", "session", channel))
        )
    return declared


async def test_four_data_boundaries(postgres_url: URL) -> None:
    grants, clock, adapter = Grants(), Clock(), RecordingAdapter()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, clock)
        governed = GovernedTools(evidence)
        channel_views: dict[Channel, dict[Audience, str]] = {}
        ids: dict[Channel, str] = {}
        channel: Channel
        for channel in ("web", "feishu"):
            ctx = context(channel=channel, session=f"{channel}-1")
            result = await governed.invoke(ctx, request(), adapter.execute)
            assert model_data(result.model_content)["evidence_id"] == result.evidence_id
            ids[channel] = result.evidence_id
            channel_views[channel] = {
                "model": result.model_content,
                "session": await evidence.project(result.evidence_id, ctx, "session"),
                channel: await evidence.project(result.evidence_id, ctx, channel),
            }

        # 每种用途只含各自获准的字段并满足各自的字节上限；原始禁止字段处处不出现。
        for channel, views in channel_views.items():
            for audience, content in views.items():
                assert set(model_data(content)["data"]) == _expected_fields(audience, channel)  # type: ignore[arg-type]
                assert len(content.encode()) <= PROJECTIONS[audience].max_bytes
                assert PRIVATE_NOTE not in content
            # 模型与 Session 的有效范围相同（模型可达内容）；渠道展示是另一份投影。
            assert views["model"] == views["session"] != views[channel]

        # 渠道投影只交给相应渠道：Web 会话里的证据不能投影给飞书，反之亦然。
        with pytest.raises(EvidenceUnavailableError):
            await evidence.project(ids["web"], context(session="web-1"), "feishu")
        with pytest.raises(EvidenceUnavailableError):
            await evidence.project(
                ids["feishu"], context(channel="feishu", session="feishu-1"), "web"
            )

        # PostgreSQL 保存的是按用途投影后的内容，不含原始结果。
        saved = await _saved_text(engine)
        assert PRIVATE_NOTE not in saved
        assert "private_note" not in saved


async def test_truncated_and_empty_results_are_explicit(postgres_url: URL) -> None:
    grants, clock = Grants(), Clock()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, clock)

        # 行数多：模型上限放不下 rows，整体省略并标明截断（不是空结果）；Web 上限更大，仍完整。
        many = await evidence.record(
            context(), request(), await RecordingAdapter(rows=30).execute(request())
        )
        model = model_data(many.model_content)
        assert many.truncated and model["truncated"] is True and model["empty"] is False
        assert model["data"] == {}
        web = model_data(await evidence.project(many.evidence_id, context(), "web"))
        assert web["truncated"] is False and len(web["data"]["rows"]) == 30  # type: ignore[index]

        # 来源已截断：所有用途都保留截断标记。
        source_cut = ToolObservation(payload={"total": 1}, captured_at=START, truncated=True)
        cut = await evidence.record(context(), request(), source_cut)
        assert cut.truncated and model_data(cut.model_content)["truncated"] is True
        session = model_data(await evidence.project(cut.evidence_id, context(), "session"))
        assert session["truncated"] is True

        # 空结果：没有任何获准字段，明确标为 empty，而不是一段空文本。
        empty = ToolObservation(payload={"private_note": "x"}, captured_at=START, truncated=False)
        nothing = model_data((await evidence.record(context(), request(), empty)).model_content)
        assert nothing["empty"] is True and nothing["data"] == {}
        assert nothing["truncated"] is False


async def test_model_reachable_content_uses_the_smaller_channel_limit(postgres_url: URL) -> None:
    """渠道容量小于模型容量时，模型与 Session 内容按渠道容量截断，模型不能多拿再复述。"""
    grants, clock = Grants(), Clock()
    grants.grant("alice", TOTAL_TOOL)
    wide = Projection(fields=("region", "total", "rows"), max_bytes=4000)
    narrow_channel = _narrowed(model=wide, session=wide, feishu=Projection(wide.fields, 300))
    feishu = context(channel="feishu", session="f1")
    async with ready_engine(postgres_url) as engine:
        evidence = EvidenceStore(
            engine, narrow_channel, authorize=grants, clock=clock, retention_seconds=60
        )
        result = await evidence.record(
            feishu, request(), await RecordingAdapter(rows=10).execute(request())
        )
        session = await evidence.project(result.evidence_id, feishu, "session")

    for content in (result.model_content, session):
        assert len(content.encode()) <= 300
        assert model_data(content)["truncated"] is True
        assert "rows" not in model_data(content)["data"]  # type: ignore[operator]


async def test_forged_foreign_expired_evidence_is_denied(postgres_url: URL) -> None:
    grants, clock = Grants(), Clock()
    grants.grant("alice", TOTAL_TOOL)
    grants.grant("bob", TOTAL_TOOL)
    owner = context()
    denied = [
        ("forged", owner),
        ("own", context(subject="bob")),
        ("own", context(session="s2")),
        ("own", context(channel="feishu")),
        ("own", context(targets=frozenset({OTHER_TARGET}))),
    ]

    async with ready_engine(postgres_url) as engine:
        evidence_id = await _record(store(engine, grants, clock), owner)

    # 重建引擎与存储对象后，归属、范围、授权和过期检查仍然成立。
    async with open_engine(secret(postgres_url)) as engine:
        await check_storage(engine)
        evidence = store(engine, grants, clock)
        assert model_data(await evidence.project(evidence_id, owner, "web"))["evidence_id"] == (
            evidence_id
        )
        messages = set()
        for which, ctx in denied:
            target = "ev_" + "0" * 32 if which == "forged" else evidence_id
            with pytest.raises(EvidenceUnavailableError) as excinfo:
                await evidence.project(target, ctx, "web")
            messages.add(str(excinfo.value))

        # 撤权后已保存内容不可再读，最终回答也无法引用。
        grants.revoke_all()
        with pytest.raises(EvidenceUnavailableError) as excinfo:
            await evidence.project(evidence_id, owner, "web")
        messages.add(str(excinfo.value))
        answer = AgentAnswer(evidence_ids=(evidence_id,), inferences=[], clarification=None)
        with pytest.raises(AnswerRejectedError):
            await evidence.validate_answer(cited(answer), owner)

        # 过期：物理清理与否无关，读取时立即拒绝。
        grants.grant("alice", TOTAL_TOOL)
        clock.advance(RETENTION_SECONDS - 1)
        await evidence.project(evidence_id, owner, "web")
        clock.advance(1)
        with pytest.raises(EvidenceUnavailableError) as excinfo:
            await evidence.project(evidence_id, owner, "web")
        messages.add(str(excinfo.value))

    # 伪造、他人、撤权与过期返回同一条受控信息，不泄露记录是否存在。
    assert len(messages) == 1


def _answer(
    ids: tuple[str, ...] = (),
    inferences: tuple[tuple[str, tuple[str, ...]], ...] = (),
    *,
    clarification: str | None = None,
    advice: str | None = None,
) -> AgentAnswer:
    return AgentAnswer(
        evidence_ids=ids,
        inferences=[AnswerInference(text=t, evidence_ids=refs) for t, refs in inferences],
        clarification=clarification,
        advice=advice,
    )


async def test_query_claim_without_evidence_is_denied(postgres_url: URL) -> None:
    grants, clock = Grants(), Clock()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, clock)
        real = await _record(evidence)
        forged = "ev_" + "f" * 32
        rejected = [
            _answer(inferences=(("东区订单总数是 200", ()),)),
            _answer((real,), (("东区订单总数是 200", ()),)),
            _answer((real,), (("东区订单总数是 200", (forged,)),)),
            _answer((forged,), (("东区订单总数是 200", (forged,)),)),
            _answer((real, real)),
            _answer(),
            _answer((real,), clarification="请说明地区"),
            _answer(inferences=(("可能是重复计数", ()),), clarification="请说明地区"),
        ]
        for answer in rejected:
            with pytest.raises(AnswerRejectedError):
                await evidence.validate_answer(cited(answer), context())

        # 澄清：没有实际查询，也不展示任何结果。
        delivery = await evidence.validate_answer(
            cited(_answer(clarification="请说明地区")), context()
        )
        assert delivery.evidence_ids == ()
        assert "请说明地区" in delivery.content
        assert _FACTS_HEADER not in delivery.content


async def test_advice_is_a_separate_answer_without_facts(postgres_url: URL) -> None:
    """未执行建议（P2.5 Task 7）：与澄清、证据回答互斥，单独标明未执行查询、未经核实。"""
    grants, clock = Grants(), Clock()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, clock)
        real = await _record(evidence)
        sql = "SELECT region, SUM(total) AS total\nFROM sales GROUP BY region"
        for mixed in (
            _answer((real,), advice=sql),
            _answer(inferences=(("按地区汇总", ()),), advice=sql),
            _answer(clarification="请说明地区", advice=sql),
        ):
            with pytest.raises(AnswerRejectedError):
                await evidence.validate_answer(cited(mixed), context())

        for channel in ("web", "feishu"):
            ctx = context(channel=channel, session=f"{channel}-advice")
            delivery = await evidence.validate_answer(cited(_answer(advice=sql)), ctx)
            first, *rest = delivery.content.split("\n")
            assert first == _ADVICE_HEADER and "未执行业务查询" in first and "未经系统核实" in first
            # 建议原文保持在一行内（换行写成转义），不能伪造标题或事实区。
            assert rest == ["SELECT region, SUM(total) AS total\\nFROM sales GROUP BY region"]
            assert (delivery.evidence_ids, delivery.facts, delivery.layout) == ((), (), None)
            assert _FACTS_HEADER not in delivery.content


def test_answers_saved_before_advice_existed_still_load() -> None:
    old = '{"evidence_ids": [], "inferences": [], "clarification": "请说明地区"}'
    assert AgentAnswer.model_validate_json(old).advice is None


def _cite_evidence(inference: str, **extra: object) -> ModelStep:
    """脚本模型读取本次收到的工具结果，从中选出证据引用；它不能改写事实区域。"""

    def respond(call: ModelCall) -> Any:
        items = call.input
        assert isinstance(items, list)
        outputs = [i for i in items if i.get("type") == "function_call_output"]
        evidence_id = json.loads(outputs[-1]["output"])["evidence_id"]
        answer = {
            "evidence_ids": [evidence_id],
            "inferences": [{"text": inference, "evidence_ids": [evidence_id]}],
            "clarification": None,
            **extra,
        }
        return [assistant_message(json.dumps(answer, ensure_ascii=False))]

    return ModelStep.respond(respond)


async def test_facts_are_rendered_from_evidence(postgres_url: URL) -> None:
    grants, clock, adapter = Grants(), Clock(), RecordingAdapter(total=100)
    grants.grant("alice", TOTAL_TOOL)
    inference = "推测真实值应为 200，可能存在重复计数"
    ctx = context()
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, clock)
        governed = GovernedTools(evidence)

        def agent(model: ScriptedModel) -> Agent[RunContext]:
            return Agent[RunContext](
                name="governed",
                model=model,
                tools=[sdk_tool(governed, adapter, TOTAL_TOOL)],
                output_type=AgentAnswer,
            )

        call = [function_call("order_total", {"region": "east"}, call_id="c1")]
        result = await Runner.run(
            agent(ScriptedModel([call, _cite_evidence(inference)])), "东区？", context=ctx
        )
        delivery = await evidence.validate_answer(cited(result.final_output), ctx)

        # 模型提交额外的“已核实数值”字段：最终类型校验即拒绝，不进入 Evidence 校验。
        with pytest.raises(ModelBehaviorError):
            await Runner.run(
                agent(ScriptedModel([call, _cite_evidence(inference, verified_total=999)])),
                "东区？",
                context=context(turn="t2"),
            )

    facts, analysis = delivery.content.split(_ANALYSIS_HEADER)
    assert facts.startswith(_FACTS_HEADER)
    assert '"total": 100' in facts
    assert "200" not in facts and "重复计数" not in facts
    assert inference in analysis and "推断" in analysis
    assert result.final_output.evidence_ids[0] in analysis
    assert delivery.channel == "web"
    assert delivery.evidence_ids == result.final_output.evidence_ids
    assert PRIVATE_NOTE not in delivery.content


MODEL_ONLY = "MODEL_ONLY_SYNTHETIC_VALUE"


def _copy_tool_output(into: str) -> ModelStep:
    """脚本模型把收到的工具结果原样抄进回答文字，模拟模型复述它看到的一切。"""

    def respond(call: ModelCall) -> Any:
        items = call.input
        assert isinstance(items, list)
        raw = [i for i in items if i.get("type") == "function_call_output"][-1]["output"]
        evidence_id = json.loads(raw)["evidence_id"]
        if into == "inference":
            answer = {
                "evidence_ids": [evidence_id],
                "inferences": [{"text": raw, "evidence_ids": [evidence_id]}],
                "clarification": None,
            }
        else:
            answer = {"evidence_ids": [], "inferences": [], "clarification": None, into: raw}
        return [assistant_message(json.dumps(answer, ensure_ascii=False))]

    return ModelStep.respond(respond)


@pytest.mark.parametrize("into", ["inference", "clarification", "advice"])
async def test_model_text_cannot_carry_fields_the_channel_forbids(
    postgres_url: URL, into: str
) -> None:
    """``rows`` 允许给模型和 Web，不允许给飞书；飞书会话中模型根本拿不到它。"""
    grants, clock = Grants(), Clock()
    grants.grant("alice", TOTAL_TOOL)
    adapter = RecordingAdapter(memo=MODEL_ONLY)
    deliveries: dict[str, str] = {}
    sessions: dict[str, str] = {}
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, clock)
        governed = GovernedTools(evidence)
        channel: Channel
        for channel in ("feishu", "web"):
            ctx = context(channel=channel, session=f"{channel}-1")
            model = ScriptedModel(
                [
                    [function_call("order_total", {"region": "east"}, call_id="c1")],
                    _copy_tool_output(into),
                ]
            )
            agent = Agent[RunContext](
                name="governed",
                model=model,
                tools=[sdk_tool(governed, adapter, TOTAL_TOOL)],
                output_type=AgentAnswer,
            )
            result = await Runner.run(agent, "东区？", context=ctx)
            deliveries[channel] = (
                await evidence.validate_answer(cited(result.final_output), ctx)
            ).content
            items = model.calls[1].input
            assert isinstance(items, list)
            (raw,) = [i["output"] for i in items if i.get("type") == "function_call_output"]
            evidence_id = str(model_data(str(raw))["evidence_id"])
            sessions[channel] = await evidence.project(evidence_id, ctx, "session")

    assert MODEL_ONLY not in deliveries["feishu"]
    # Session 内容会在后续轮次回放给模型，同样不能含飞书禁止的字段。
    assert MODEL_ONLY not in sessions["feishu"]
    # 对照：Web 允许 rows，该标记是真实可交付的数据，而不是被测试本身滤掉。
    assert MODEL_ONLY in deliveries["web"]
    assert MODEL_ONLY in sessions["web"]


def _narrowed(**projections: Projection) -> ToolCatalog:
    policy = ToolPolicy(
        policy_id="synthetic.region",
        arguments=RegionArgs,
        projections={**PROJECTIONS, **projections},
    )
    return ToolCatalog(CONTRACTS, (policy,))


def _renamed() -> ToolCatalog:
    policy = ToolPolicy(
        policy_id="synthetic.region.v2", arguments=RegionArgs, projections=PROJECTIONS
    )
    contracts = tuple(c.model_copy(update={"policy_id": policy.policy_id}) for c in CONTRACTS)
    return ToolCatalog(contracts, (policy,))


@pytest.mark.parametrize(
    "current",
    [
        pytest.param(
            lambda: _narrowed(web=Projection(fields=("region",), max_bytes=2000)), id="fields"
        ),
        pytest.param(
            lambda: _narrowed(model=Projection(fields=("total", "rows"), max_bytes=256)), id="bytes"
        ),
        pytest.param(lambda: ToolCatalog(CONTRACTS[1:], POLICIES), id="tool-removed"),
        pytest.param(_renamed, id="policy-replaced"),
    ],
)
async def test_stored_evidence_follows_current_policy(
    postgres_url: URL, current: Callable[[], ToolCatalog]
) -> None:
    grants, clock = Grants(), Clock()
    grants.grant("alice", TOTAL_TOOL)
    web, feishu = context(), context(channel="feishu", session="f1")
    async with ready_engine(postgres_url) as engine:
        original = store(engine, grants, clock)
        saved = {"web": await _record(original, web), "feishu": await _record(original, feishu)}

    # 重建引擎与存储对象（相当于重启或恢复）后读取。
    async with open_engine(secret(postgres_url)) as engine:
        answer = {
            ch: AgentAnswer(evidence_ids=(eid,), inferences=[], clarification=None)
            for ch, eid in saved.items()
        }
        cases: list[tuple[RunContext, str, Audience]] = [
            (web, saved["web"], "model"),
            (web, saved["web"], "session"),
            (web, saved["web"], "web"),
            (feishu, saved["feishu"], "feishu"),
        ]
        # 对照：同一份策略重建后照常可读。
        same = EvidenceStore(
            engine, catalog(), authorize=grants, clock=clock, retention_seconds=RETENTION_SECONDS
        )
        for ctx, eid, audience in cases:
            await same.project(eid, ctx, audience)
        await same.validate_answer(cited(answer["web"]), web)

        changed = EvidenceStore(
            engine, current(), authorize=grants, clock=clock, retention_seconds=RETENTION_SECONDS
        )
        for ctx, eid, audience in cases:
            with pytest.raises(EvidenceUnavailableError):
                await changed.project(eid, ctx, audience)
        for ctx, ch in ((web, "web"), (feishu, "feishu")):
            with pytest.raises(AnswerRejectedError):
                await changed.validate_answer(cited(answer[ch]), ctx)


class _TotalResult(BaseModel):
    region: str
    total: int
    rows: int


class _DocumentedResult(BaseModel):
    """与 ``_TotalResult`` 相同的结果契约，只多了标题与说明文字。"""

    region: str = Field(description="地区")
    total: int = Field(description="订单总额")
    rows: int


class _TextTotalResult(BaseModel):
    region: str
    total: str
    rows: int


def _with_result(result: type[BaseModel], description: str) -> ToolCatalog:
    policy = ToolPolicy(
        policy_id="synthetic.region", arguments=RegionArgs, projections=PROJECTIONS, result=result
    )
    contracts = tuple(c.model_copy(update={"description": description}) for c in CONTRACTS)
    return ToolCatalog(contracts, (policy,))


async def test_evidence_is_bound_to_the_result_contract(postgres_url: URL) -> None:
    grants, clock = Grants(), Clock()
    grants.grant("alice", TOTAL_TOOL)
    ctx = context()
    async with ready_engine(postgres_url) as engine:
        saved = await _record(store(engine, grants, clock, _with_result(_TotalResult, "汇总")), ctx)

    async with open_engine(secret(postgres_url)) as engine:
        # 只改交给模型的说明文字与 schema 标题/说明：结果契约没变，证据照常可读。
        documented = store(engine, grants, clock, _with_result(_DocumentedResult, "按地区汇总"))
        await documented.project(saved, ctx, "model")
        # 结果字段改型：旧证据是按旧契约接收的事实，不能按新契约继续使用。
        retyped = store(engine, grants, clock, _with_result(_TextTotalResult, "汇总"))
        with pytest.raises(EvidenceUnavailableError):
            await retyped.project(saved, ctx, "model")


class _NormalizedRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: str

    @field_validator("region")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return value.strip().upper()


async def test_evidence_is_bound_to_the_executed_arguments(postgres_url: URL) -> None:
    """校验器会改写参数：执行与证据都使用改写后的有效参数，历史中的原始调用仍可核对。"""
    grants, clock, adapter = Grants(), Clock(), RecordingAdapter()
    grants.grant("alice", TOTAL_TOOL)
    contract = CONTRACTS[0].model_copy(
        update={"input_schema": _NormalizedRegion.model_json_schema(), "policy_id": "normalized"}
    )
    policy = ToolPolicy(
        policy_id="normalized", arguments=_NormalizedRegion, projections=PROJECTIONS
    )
    tools = ToolCatalog((contract,), (policy,))
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, clock, tools)
        ctx = context()
        result = await GovernedTools(evidence).invoke(
            ctx, request(region=" east "), adapter.execute
        )
        assert [r.arguments for r in adapter.calls] == [{"region": "EAST"}]

        def call(region: str) -> ToolCall:
            return ToolCall(call_id="call-1", tool_name="order_total", arguments={"region": region})

        for same in (" east ", "EAST"):
            await evidence.project(result.evidence_id, ctx, "session", call=call(same))
        with pytest.raises(EvidenceUnavailableError):
            await evidence.project(result.evidence_id, ctx, "session", call=call("west"))


async def test_app_schema_version_is_checked(postgres_url: URL) -> None:
    grants, clock = Grants(), Clock()
    async with open_engine(secret(postgres_url)) as engine:
        # 只有 SDK 表时应用表缺失，拒绝使用。
        sdk_only = SQLAlchemySession("probe", engine=engine, create_tables=True)
        await sdk_only.get_items(limit=0)
        with pytest.raises(StorageNotInitializedError):
            await check_storage(engine)

        key = SecretStr("evidence-test-digest-key")
        await initialize_storage(engine, digest_key=key)
        await initialize_storage(engine, digest_key=key)  # 显式初始化可重复执行，不重复建表
        await check_storage(engine)
        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
        assert tables == {SDK_SESSIONS_TABLE, SDK_MESSAGES_TABLE, *APP_TABLES}
        assert all(name.startswith("xiaowei_") for name in APP_TABLES)

        # SDK 与应用表互不改写：写证据不产生 SDK 会话项，SDK 会话读写也不触碰证据。
        session = SQLAlchemySession("web:alice:s1", engine=engine)
        await session.add_items([{"role": "user", "content": "hi"}])
        grants.grant("alice", TOTAL_TOOL)
        await _record(store(engine, grants, clock))
        assert len(await session.get_items()) == 1
        async with engine.connect() as conn:
            count = (await conn.execute(text("SELECT count(*) FROM xiaowei_evidence"))).scalar()
        assert count == 1

        # 版本不符：就绪检查拒绝，显式初始化也不自动升级或降级。
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE xiaowei_schema_version SET version = 99"))
        with pytest.raises(StorageVersionMismatchError):
            await check_storage(engine)
        with pytest.raises(StorageVersionMismatchError):
            await initialize_storage(engine, digest_key=key)

    migration = files("xiaowei").joinpath("migrations/001_initial.sql").read_text(encoding="utf-8")
    assert "agent_" not in migration


async def test_non_starrocks_fingerprints_are_unchanged(postgres_url: URL) -> None:
    """不依赖数据范围的工具（``data_scope=None``）保存的策略指纹与 P1-B 基线公式的值相同。

    期望值在 ``949cb32`` 上由同一合成策略算出；改变它会使已保存的 MCP 与合成工具证据失效。
    """
    grants = Grants()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        await _record(store(engine, grants, Clock()))
        async with engine.connect() as conn:
            saved = await conn.scalar(text("SELECT policy_fingerprint FROM xiaowei_evidence"))
    assert saved == "sha256:634ef1d4f2174b006535f3d045742a9c8d9b1148fc5afc82fc54cba135be6750"
