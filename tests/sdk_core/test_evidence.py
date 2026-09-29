"""Task 2：Evidence 记录、四种数据投影、最终回答校验与应用表版本。真实 PostgreSQL + 真 Runner。"""

import json
from importlib.resources import files
from typing import Any

import pytest
from agents import Agent, Runner
from agents.exceptions import ModelBehaviorError
from agents.extensions.memory import SQLAlchemySession
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message, function_call
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.sdk_core.synthetic_tools import (
    OTHER_TARGET,
    PRIVATE_NOTE,
    PROJECTIONS,
    RETENTION_SECONDS,
    START,
    TOTAL_TOOL,
    Clock,
    Grants,
    RecordingAdapter,
    catalog,
    context,
    model_data,
    ready_engine,
    request,
    sdk_tool,
    secret,
    store,
)

from xiaowei.evidence import AnswerRejectedError, EvidenceStore, EvidenceUnavailableError
from xiaowei.governance import GovernedTools
from xiaowei.models import AgentAnswer, AnswerInference, RunContext, ToolObservation
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

_FACTS_HEADER = "查询结果"
_ANALYSIS_HEADER = "分析建议"


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


async def test_four_data_boundaries(postgres_url: URL) -> None:
    grants, clock, adapter = Grants(), Clock(), RecordingAdapter()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, clock)
        governed = GovernedTools(catalog(), evidence, authorize=grants)
        web = context()
        result = await governed.invoke(web, request(), lambda: adapter.execute(request()))

        model = model_data(result.model_content)
        assert model["evidence_id"] == result.evidence_id
        assert set(model["data"]) == {"total", "rows"}  # type: ignore[arg-type]
        views = {"model": result.model_content}
        views["session"] = await evidence.project(result.evidence_id, web, "session")
        views["web"] = await evidence.project(result.evidence_id, web, "web")

        feishu = context(channel="feishu", session="f1")
        feishu_id = await _record(evidence, feishu)
        views["feishu"] = await evidence.project(feishu_id, feishu, "feishu")

        # 每种用途只含各自声明的字段并满足各自的字节上限；原始禁止字段处处不出现。
        for audience, content in views.items():
            spec = PROJECTIONS[audience]
            assert set(model_data(content)["data"]) == set(spec.fields)  # type: ignore[arg-type]
            assert len(content.encode()) <= spec.max_bytes
            assert PRIVATE_NOTE not in content
        assert views["session"] != views["model"] != views["web"]

        # 渠道投影只交给相应渠道：Web 会话里的证据不能投影给飞书，反之亦然。
        with pytest.raises(EvidenceUnavailableError):
            await evidence.project(result.evidence_id, web, "feishu")
        with pytest.raises(EvidenceUnavailableError):
            await evidence.project(feishu_id, feishu, "web")

        # PostgreSQL 保存的是按用途投影后的内容，不含原始结果。
        saved = await _saved_text(engine)
        assert PRIVATE_NOTE not in saved
        assert "private_note" not in saved


async def test_truncated_and_empty_results_are_explicit(postgres_url: URL) -> None:
    grants, clock = Grants(), Clock()
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, clock)

        # 行数较多：模型上限放不下 rows，按字段整体省略并标明截断；Web 上限更大，仍完整。
        many = await evidence.record(
            context(), request(), await RecordingAdapter(rows=30).execute(request())
        )
        model = model_data(many.model_content)
        assert many.truncated and model["truncated"] is True
        assert set(model["data"]) == {"total"}  # type: ignore[arg-type]
        grants.grant("alice", TOTAL_TOOL)
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
            await evidence.validate_answer(answer, owner)

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
) -> AgentAnswer:
    return AgentAnswer(
        evidence_ids=ids,
        inferences=[AnswerInference(text=t, evidence_ids=refs) for t, refs in inferences],
        clarification=clarification,
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
                await evidence.validate_answer(answer, context())

        # 澄清：没有实际查询，也不展示任何结果。
        delivery = await evidence.validate_answer(_answer(clarification="请说明地区"), context())
        assert delivery.evidence_ids == ()
        assert "请说明地区" in delivery.content
        assert _FACTS_HEADER not in delivery.content


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
        governed = GovernedTools(catalog(), evidence, authorize=grants)

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
        delivery = await evidence.validate_answer(result.final_output, ctx)

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


async def test_app_schema_version_is_checked(postgres_url: URL) -> None:
    grants, clock = Grants(), Clock()
    async with open_engine(secret(postgres_url)) as engine:
        # 只有 SDK 表时应用表缺失，拒绝使用。
        sdk_only = SQLAlchemySession("probe", engine=engine, create_tables=True)
        await sdk_only.get_items(limit=0)
        with pytest.raises(StorageNotInitializedError):
            await check_storage(engine)

        await initialize_storage(engine)
        await initialize_storage(engine)  # 显式初始化可重复执行，不重复建表
        await check_storage(engine)
        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
        assert tables == {SDK_SESSIONS_TABLE, SDK_MESSAGES_TABLE, *APP_TABLES}
        assert all(name.startswith("xiaowei_") for name in APP_TABLES)

        # SDK 与应用表互不改写：写证据不产生 SDK 会话项，SDK 会话读写也不触碰证据。
        session = SQLAlchemySession("web:alice:s1", engine=engine)
        await session.add_items([{"role": "user", "content": "hi"}])
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
            await initialize_storage(engine)

    migration = files("xiaowei").joinpath("migrations/001_initial.sql").read_text(encoding="utf-8")
    assert "agent_" not in migration
