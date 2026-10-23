"""v4 仅改变新交付语义；旧保存回答仍按保存版本验证。真 Runner/隔离 PG。"""

import pytest
from tests.sdk_core.synthetic_tools import QUERY_TOOL, TARGET, TOTAL_TOOL
from tests.sdk_core.test_app import Env, answer, cite, evidence_in, tool_call
from tests.sdk_core.test_app import env as env

from xiaowei.evidence import AnswerRejectedError
from xiaowei.models import AgentAnswer, FactBlock, TextBlock, TurnAnswer

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize("partial", [False, True], ids=["v3-empty", "v3-partial"])
async def test_saved_v3_keeps_remaining_results(env: Env, partial: bool) -> None:
    message = env.scripts.add(
        "旧轮次两个结果",
        tool_call("run_query", region="east"),
        tool_call("run_query", region="west"),
        cite(),
    )
    ctx = env.ctx(turn="old")
    turn = await env.app.run_turn(ctx, message)
    ids = turn.context_evidence
    saved = TurnAnswer(
        answer=AgentAnswer(
            blocks=(FactBlock(type="fact", evidence_id=ids[0], view="full"),) if partial else (),
            report_format=None,
            awaiting_user=False,
        ),
        answer_version=3,
        context_evidence=ids,
    )
    # 模拟请求读取的 JSON 边界，不能按当前版本重建。
    saved = TurnAnswer.model_validate_json(saved.model_dump_json())
    output = await env.governed.evidence.validate_answer(saved, ctx, history=True)
    assert output.evidence_ids == ids
    assert len(output.section("remaining")) == (1 if partial else 2)


async def test_new_v4_selects_only_cited_but_rechecks_unshown(env: Env) -> None:
    message = env.scripts.add(
        "选所需结果",
        tool_call("order_total", region="east"),
        tool_call("run_query", region="west"),
        lambda c: answer(evidence_in(c)[:1], "结论"),
    )
    ctx = env.ctx(turn="v4")
    turn = await env.app.run_turn(ctx, message)
    assert turn.answer_version == 4
    output = await env.governed.evidence.validate_answer(turn, ctx)
    assert output.evidence_ids == turn.context_evidence[:1]
    assert not output.section("remaining")
    assert output.answer_version == 4
    env.grants.allowed.remove(("alice", TARGET, QUERY_TOOL))
    assert ("alice", TARGET, TOTAL_TOOL) in env.grants.allowed
    with pytest.raises(AnswerRejectedError):
        await env.governed.evidence.validate_answer(turn, ctx, history=True)


@pytest.mark.parametrize("text", ["", "只有结论"])
async def test_new_query_without_fact_is_rewritten_once_without_requery(
    env: Env, text: str
) -> None:
    def repaired(call):
        assert call.tools == []
        return answer(evidence_in(call), "补充所需事实")

    message = env.scripts.add(
        "查询不能漏事实" + text,
        tool_call("run_query", region="east"),
        lambda c: answer([], text),
        repaired,
    )
    turn = await env.app.run_turn(env.ctx(), message)
    assert turn.answer.evidence_ids
    assert len(env.scripts.calls[message]) == 3
    assert len(env.adapter.calls) == 1


async def test_no_query_explanation_stays_text_only(env: Env) -> None:
    message = env.scripts.add("解释，不查询", lambda c: answer([], "这是未执行的建议"))
    turn = await env.app.run_turn(env.ctx(), message)
    assert turn.blocks_answer.blocks == (TextBlock(type="text", text="这是未执行的建议"),)
    assert not env.adapter.calls


async def test_new_query_without_fact_twice_fails_without_tool_replay(env: Env) -> None:
    from xiaowei.app import TurnError

    message = env.scripts.add(
        "两次漏引",
        tool_call("run_query", region="east"),
        lambda c: answer([], "未引用"),
        lambda c: answer([], "仍未引用"),
    )
    with pytest.raises(TurnError) as failure:
        await env.app.run_turn(env.ctx(), message)
    assert failure.value.reason == "answer_rejected"
    assert len(env.scripts.calls[message]) == 3 and len(env.adapter.calls) == 1
    assert env.scripts.calls[message][-1].tools == []


async def test_empty_limit_finish_uses_one_repair_and_the_original_queries(env: Env) -> None:
    message = env.scripts.add(
        "空收尾",
        tool_call("run_query", region="east"),
        lambda c: answer([], ""),
        cite("基于已取得结果"),
    )
    ctx = env.ctx()
    ctx = ctx.model_copy(update={"budget": ctx.budget.model_copy(update={"max_turns": 1})})
    turn = await env.app.run_turn(ctx, message)
    assert turn.limit_reached and turn.answer.evidence_ids
    assert len(env.scripts.calls[message]) == 3 and len(env.adapter.calls) == 1
    assert all(call.tools == [] for call in env.scripts.calls[message][1:])
