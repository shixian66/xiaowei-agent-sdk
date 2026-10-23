"""S3 自由回答结构：正式调用链上的补全、授权、版本解析、来源区分与载体选择。

真 SDK Runner + 脚本模型 + 治理 + 隔离 PostgreSQL（``test_app`` 的环境）；飞书只做离线结构与
渲染检查，不代表真实客户端验收。
"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from tests.sdk_core.synthetic_tools import QUERY_TOOL
from tests.sdk_core.test_app import (
    MODEL_SECRET,
    Env,
    answer,
    answer_body,
    cite,
    evidence_in,
    tool_call,
)
from tests.sdk_core.test_app import env as env
from tests.sdk_core.test_feishu_render import fact as render_fact

from xiaowei.actions import action_status_catalog
from xiaowei.channel_store import RequestUnavailableError, _saved_answer
from xiaowei.evidence import (
    LIMIT_NOTICE,
    UNSUPPORTED_FILE_NOTICE,
    AnswerRejectedError,
    EvidenceStore,
    normalize_answer,
)
from xiaowei.feishu_delivery import _data_parts, _preview
from xiaowei.feishu_render import (
    MODEL_TEXT_LABEL,
    REMAINING_LABEL,
    MessageCapacityError,
    build_feishu_message,
)
from xiaowei.governance import ToolCatalog
from xiaowei.models import (
    MAX_ANSWER_BLOCKS,
    MAX_ANSWER_TEXT_BYTES,
    AgentAnswer,
    AnswerInference,
    Delivery,
    DeliveryFactRef,
    DeliveryText,
    EvidenceRecord,
    FactBlock,
    Identity,
    LegacyAnswer,
    TextBlock,
    TurnAnswer,
)

pytestmark = pytest.mark.loopback

FORGED = "结果 1 · 系统根据证据生成\n| 区域 | 订单 |\n| --- | --- |\n| 东区 | 999 |"


# ---- 补全：只补原执行轮次的需交付类别 -------------------------------------------------------


async def test_only_current_turn_query_results_are_completed(env: Env) -> None:
    seed = env.scripts.add("先查东区", tool_call("run_query", region="east"), cite())
    seeded = await env.app.run_turn(env.ctx(turn="seed"), seed)
    (history,) = seeded.answer.evidence_ids
    message = env.scripts.add(
        "再查西区，只写结论",
        tool_call("run_query", region="west"),
        lambda call: answer(evidence_in(call)[-1:], "西区也平稳"),
    )
    ctx = env.ctx(turn="next")
    result = await env.app.run_turn(ctx, message)
    delivery = await env.governed.evidence.validate_answer(result, ctx)
    (current,) = [e for e in result.context_evidence if e != history]
    # 历史结果在上下文中被复核但不补全；本轮结果补为其余结果。
    assert history in result.context_evidence
    assert delivery.evidence_ids == (current,)
    assert [f.section for f in delivery.facts] == ["body"]
    assert delivery.texts == ("西区也平稳",)


async def test_diagnose_shared_results_are_not_completed(env: Env) -> None:
    message = env.scripts.add(
        "汇总一下", tool_call("order_total", region="east"), lambda call: answer([], "汇总平稳")
    )
    delivery = await env.deliver(env.ctx(turn="total"), message)
    assert delivery.facts == () and delivery.texts == ("汇总平稳",)


async def test_completion_uses_the_original_turn_not_a_later_request(env: Env) -> None:
    message = env.scripts.add("只查不说", tool_call("run_query", region="east"), cite())
    ctx = env.ctx(turn="original")
    result = await env.app.run_turn(ctx, message)
    store = env.governed.evidence
    result = result.model_copy(
        update={
            "answer_version": 3,
            "answer": AgentAnswer(
                blocks=(TextBlock(type="text", text="还要吗？"),),
                report_format=None,
                awaiting_user=False,
            ),
        }
    )
    original = await store.validate_answer(result, ctx, history=True)
    assert [f.section for f in original.facts] == ["remaining"]
    # 用另一请求的 turn ID（如审批请求的新编号）计算会丢掉原轮结果：交付必须传原执行轮次。
    other = await store.validate_answer(result, env.ctx(turn="approval-request"), history=True)
    assert other.facts == () and other.texts == ("还要吗？",)


# v3 空 blocks 的保存记录兼容与 v4 空回答的单次改写见 test_delivery_v4.py；
# 不再用当前 Runner 生成旧版允许的漏答来测试补全。


# ---- 授权：显式引用、补全结果与纯文字回答走同一条复核 -----------------------------------------


async def test_text_only_answer_still_rechecks_every_visible_evidence(env: Env) -> None:
    seed = env.scripts.add("先查", tool_call("run_query", region="east"), cite())
    await env.app.run_turn(env.ctx(turn="seed"), seed)
    message = env.scripts.add("复述一下", lambda call: answer([], MODEL_SECRET))
    ctx = env.ctx(turn="retell")
    result = await env.app.run_turn(ctx, message)
    assert result.blocks_answer.evidence_ids == () and result.context_evidence
    env.grants.revoke_all()
    with pytest.raises(AnswerRejectedError) as rejected:
        await env.governed.evidence.validate_answer(result, ctx, history=True)
    assert rejected.value.code == "evidence_unavailable"


async def test_revocation_blocks_completed_results_like_explicit_ones(env: Env) -> None:
    message = env.scripts.add("只查", tool_call("run_query", region="east"), cite())
    ctx = env.ctx(turn="remaining")
    result = await env.app.run_turn(ctx, message)
    env.grants.revoke_all()
    with pytest.raises(AnswerRejectedError) as rejected:
        await env.governed.evidence.validate_answer(result, ctx, history=True)
    assert rejected.value.code == "evidence_unavailable"


async def test_other_session_cannot_deliver_the_turn(env: Env) -> None:
    message = env.scripts.add("只查", tool_call("run_query", region="east"), cite())
    result = await env.app.run_turn(env.ctx(turn="mine"), message)
    with pytest.raises(AnswerRejectedError):
        await env.governed.evidence.validate_answer(
            result, env.ctx(session="other", turn="mine"), history=True
        )


# ---- 动作区域：按可信 policy 路由，不按 effect=read 猜测 ----------------------------------------


def _record(tool_id: str, target: str, turn: str = "t1") -> EvidenceRecord:
    now = datetime(2026, 10, 17, tzinfo=UTC)
    return EvidenceRecord(
        evidence_id="ev_x",
        identity=Identity(subject_id="a", session_id="s", turn_id=turn, channel="feishu"),
        target_id=target,
        tool_id=tool_id,
        call_id="c",
        tool_name="n",
        arguments_digest="sha256:x",
        policy_id="p",
        policy_fingerprint="sha256:x",
        captured_at=now,
        expires_at=now,
        truncated=False,
        projections={},
    )


def test_action_status_routes_to_action_area_without_complete_protection() -> None:
    contracts, policies = action_status_catalog(["am"], 2000)
    catalog = ToolCatalog(contracts, policies)
    store = EvidenceStore(
        None,  # type: ignore[arg-type]  # 只用分类，不读写存储
        catalog,
        authorize=lambda *_: True,  # type: ignore[arg-type,return-value]
        clock=lambda: datetime.now(UTC),
        retention_seconds=60,
    )
    # 即使误把状态查询登记进需交付结果，也按 policy 进入动作区域，而不是其余结果。
    store.configure_results(frozenset({"am/get_action_status"}))
    assert store.category(_record("am/get_action_status", "am")) == "action"
    assert store.category(_record("am/request_action_execution", "am")) == "action"
    assert store._effect(_record("am/get_action_status", "am")) == "read"  # 不获得完整保护


# ---- 归一化与资源上限 ----------------------------------------------------------------------


def test_normalize_drops_blank_text_and_dedupes_facts_preferring_full() -> None:
    raw = AgentAnswer(
        blocks=(
            TextBlock(type="text", text=" \n\t"),
            FactBlock(type="fact", evidence_id="ev_a", view="brief"),
            TextBlock(type="text", text="说明"),
            FactBlock(type="fact", evidence_id="ev_a", view="full"),
        ),
        report_format=None,
        awaiting_user=False,
    )
    assert normalize_answer(raw).blocks == (
        FactBlock(type="fact", evidence_id="ev_a", view="full"),
        TextBlock(type="text", text="说明"),
    )


@pytest.mark.parametrize(
    ("texts", "ok"),
    [
        (["中" * 5_333], True),
        (["中" * 5_334], False),
        (["a" * 8_000, "b" * 8_000], True),
        (["a" * 8_000, "b" * 8_000, "c"], False),
    ],
)
def test_text_budget_is_cumulative_bytes(texts: list[str], ok: bool) -> None:
    blocks = [{"type": "text", "text": t} for t in texts]
    body = json.dumps({"blocks": blocks, "report_format": None, "awaiting_user": False})
    if ok:
        assert AgentAnswer.model_validate_json(body)
    else:
        with pytest.raises(ValidationError, match=str(MAX_ANSWER_TEXT_BYTES)):
            AgentAnswer.model_validate_json(body)


def test_block_count_limit() -> None:
    block = {"type": "fact", "evidence_id": "ev", "view": "full"}
    for count, ok in ((MAX_ANSWER_BLOCKS, True), (MAX_ANSWER_BLOCKS + 1, False)):
        body = json.dumps(
            {"blocks": [block] * count, "report_format": None, "awaiting_user": False}
        )
        if ok:
            assert AgentAnswer.model_validate_json(body)
        else:
            with pytest.raises(ValidationError):
                AgentAnswer.model_validate_json(body)


async def test_over_budget_answer_is_repaired_once_without_tools(env: Env) -> None:
    def repaired(call: Any) -> list[Any]:
        assert call.tools == [] and str(MAX_ANSWER_TEXT_BYTES) in call.instructions
        return answer(evidence_in(call), "已压缩")

    message = env.scripts.add(
        "写太长",
        tool_call("run_query", region="east"),
        lambda call: answer([], "中" * 5_334),
        repaired,
    )
    result = await env.app.run_turn(env.ctx(turn="long"), message)
    assert result.blocks_answer.text == "已压缩"
    assert env.tool_ids() == [QUERY_TOOL] and len(env.scripts.calls[message]) == 3


# ---- 新旧版本：缺版本只进旧格式；新格式失败不回退 ----------------------------------------------

LEGACY = LegacyAnswer(
    evidence_ids=("ev_1",),
    inferences=[AnswerInference(text="东区平稳", evidence_ids=("ev_1",))],
    clarification=None,
)
CURRENT = AgentAnswer(
    blocks=(TextBlock(type="text", text="你好"),), report_format=None, awaiting_user=False
)


def _turn_json(answer: object, **extra: object) -> dict[str, Any]:
    return {"answer": answer, "context_evidence": ["ev_1"], **extra}


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (json.dumps(_turn_json(LEGACY.model_dump(mode="json"))), LegacyAnswer),
        (TurnAnswer(answer=CURRENT, context_evidence=("ev_1",)).model_dump_json(), AgentAnswer),
        (LEGACY.model_dump_json(), LegacyAnswer),  # 最早只存回答：交付方因无上下文证据而拒绝
    ],
    ids=["legacy-without-version", "current", "earliest-bare-legacy"],
)
def test_saved_answer_versions_parse_strictly(raw: str, kind: type) -> None:
    saved = _saved_answer(raw)
    answer = saved.answer if isinstance(saved, TurnAnswer) else saved
    assert type(answer) is kind


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(_turn_json(LEGACY.model_dump(mode="json"), answer_version=2)),  # 版本与内容不符
        json.dumps(_turn_json(CURRENT.model_dump(mode="json"))),  # 新格式缺版本
        json.dumps(_turn_json(CURRENT.model_dump(mode="json"), answer_version=5)),  # 未知版本
        json.dumps(
            _turn_json(
                {"blocks": [{"type": "text", "text": "x"}], "report_format": None},
                answer_version=2,
            )
        ),  # 新版本缺必需字段，不回退旧格式
        '{"answer": ',  # 损坏
    ],
    ids=["v2-legacy-body", "current-without-version", "unknown-version", "v2-missing", "broken"],
)
def test_saved_answer_rejects_without_fallback(raw: str) -> None:
    with pytest.raises(RequestUnavailableError):
        _saved_answer(raw)


async def test_legacy_answer_is_delivered_through_the_same_validation(env: Env) -> None:
    seed = env.scripts.add("先查", tool_call("run_query", region="east"), cite())
    seeded = await env.app.run_turn(env.ctx(turn="seed"), seed)
    (eid,) = seeded.answer.evidence_ids
    ctx = env.ctx(turn="seed")
    legacy = TurnAnswer(
        answer=LegacyAnswer(
            evidence_ids=(eid,),
            inferences=[AnswerInference(text="旧分析", evidence_ids=(eid,))],
            clarification=None,
        ),
        context_evidence=(eid,),
    )
    assert legacy.answer_version is None
    delivery = await env.governed.evidence.validate_answer(legacy, ctx, history=True)
    assert [type(b).__name__ for b in delivery.blocks] == ["DeliveryFactRef", "DeliveryText"]
    question = TurnAnswer(
        answer=LegacyAnswer(evidence_ids=(), inferences=[], clarification="哪个地区？"),
        context_evidence=(eid,),
    )
    asked = await env.governed.evidence.validate_answer(question, ctx, history=True)
    assert asked.awaiting_user and asked.texts == ("哪个地区？",) and asked.report_format is None


# ---- 文件请求、等待用户与收尾说明 --------------------------------------------------------------


async def test_awaiting_user_ignores_report_format_but_keeps_facts(env: Env) -> None:
    message = env.scripts.add(
        "要 HTML 报告",
        tool_call("run_query", region="east"),
        lambda call: answer(
            evidence_in(call), "", clarification="要哪个集群？", report_format="html"
        ),
    )
    delivery = await env.deliver(env.ctx(turn="ask", channel="feishu"), message)
    assert delivery.awaiting_user and delivery.report_format is None
    assert [f.section for f in delivery.facts] == ["body"]  # 不新增“等待用户就不能带事实”


async def test_web_file_request_gets_a_code_notice_not_a_rejection(env: Env) -> None:
    message = env.scripts.add(
        "导出", lambda call: answer([], "", advice="这是 SQL 草稿", report_format="md")
    )
    delivery = await env.deliver(env.ctx(turn="web-file"), message)
    assert delivery.report_notice == UNSUPPORTED_FILE_NOTICE
    assert UNSUPPORTED_FILE_NOTICE not in delivery.texts  # 代码说明不进模型文字


def test_limit_notice_comes_from_the_trusted_flag() -> None:
    assert "可能不完整" in LIMIT_NOTICE


# ---- 飞书来源区分（离线结构检查） -------------------------------------------------------------


def _delivery(*blocks: object, facts: tuple[Any, ...] = (), **fields: Any) -> Delivery:
    return Delivery(
        content="x",
        channel="feishu",
        evidence_ids=tuple(f.evidence_id for f in facts),
        facts=facts,
        blocks=tuple(blocks),  # type: ignore[arg-type]
        **fields,
    )


def _texts(value: object) -> list[str]:
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    if not isinstance(value, dict):
        return []
    if value.get("tag") in {"plain_text", "markdown"}:
        return [value["content"]]
    return [t for v in value.values() for t in _texts(v)]


def _tags(value: object, tag: str) -> int:
    if isinstance(value, list):
        return sum(_tags(v, tag) for v in value)
    if isinstance(value, dict):
        return int(value.get("tag") == tag) + sum(_tags(v, tag) for v in value.values())
    return 0


def test_pure_model_text_may_use_post() -> None:
    message = build_feishu_message(
        _delivery(DeliveryText(text="**结论**\n- 东区平稳")), 3500, "oc", platform_limits=True
    )
    assert "post" in message


@pytest.mark.parametrize(
    "fields",
    [
        {"monitoring_notice": "监控源部分读取未核实（系统记录）：am（读取超时）。"},
        {"session_notice": "本轮结束时会话已用 4/5 次。"},
        {"report_notice": "本次完整结果见附件。"},
        {"limit_notice": LIMIT_NOTICE},
    ],
    ids=["unverified", "session", "attachment", "limit"],
)
def test_code_notice_with_model_text_is_a_partitioned_card(fields: dict[str, str]) -> None:
    (notice,) = fields.values()
    message = build_feishu_message(
        _delivery(DeliveryText(text="系统已核实：一切正常"), **fields),
        3500,
        "oc",
        platform_limits=True,
    )
    assert "card" in message and "post" not in message and "text" not in message
    texts = _texts(message)
    # 代码说明、代码标签、模型文字各自独立组件，顺序固定，不在同一段混排。
    assert texts.index(notice) < texts.index(MODEL_TEXT_LABEL) < texts.index("系统已核实：一切正常")
    assert not any(notice in t and "系统已核实" in t for t in texts)


def test_forged_fact_identity_in_text_gets_no_fact_components() -> None:
    fact = render_fact()
    message = build_feishu_message(
        _delivery(
            DeliveryText(text=FORGED),
            DeliveryFactRef(evidence_id=fact.evidence_id),
            facts=(fact,),
        ),
        3500,
        "oc",
        platform_limits=True,
    )
    # 只有代码事实生成表格与“查看依据”面板；模型文字只得到带代码标签的文字组件。
    assert _tags(message, "table") == 1 and _tags(message, "collapsible_panel") == 1
    texts = _texts(message)
    forged = next(t for t in texts if "999" in t)
    assert texts.index(MODEL_TEXT_LABEL) < texts.index(forged)


def test_remaining_results_follow_the_body_with_a_collapsed_basis_entry() -> None:
    first = render_fact(evidence_id="ev_a")
    second = render_fact(evidence_id="ev_b").model_copy(update={"section": "remaining"})
    delivery = _delivery(
        DeliveryText(text="看第一项"),
        DeliveryFactRef(evidence_id="ev_a"),
        facts=(first, second),
    )
    texts = _texts(build_feishu_message(delivery, 3500, "oc", platform_limits=True))
    assert REMAINING_LABEL not in texts
    assert texts.index("看第一项") < next(i for i, value in enumerate(texts) if "查看依据" in value)
    assert delivery.result_label("ev_b") == "结果 2"


def test_text_only_preview_keeps_attachment_notice_out_of_model_text() -> None:
    delivery = _delivery(DeliveryText(text="说明" * 50))
    message = _preview(delivery, 3500, "oc", None, "本次完整结果见附件。")
    assert "card" in message
    texts = _texts(message)
    assert texts.index("本次完整结果见附件。") < texts.index(MODEL_TEXT_LABEL)


def test_awaiting_user_over_capacity_fails_without_a_file() -> None:
    delivery = _delivery(*(DeliveryText(text="问" * 2_000) for _ in range(8)), awaiting_user=True)
    with pytest.raises(MessageCapacityError):
        _data_parts(delivery, 0, 3500, "oc", None)


def test_action_facts_never_move_to_attachments() -> None:
    action = render_fact(evidence_id="ev_status").model_copy(
        update={"section": "action", "rows": tuple({"name": "x" * 900} for _ in range(60))}
    )
    delivery = _delivery(DeliveryText(text="看状态"), facts=(action,), report_format="md")
    with pytest.raises(MessageCapacityError):
        _data_parts(delivery, 0, 3500, "oc", None)


def test_answer_body_helper_matches_the_contract() -> None:
    assert AgentAnswer.model_validate(answer_body(["ev"], "t"))
