"""Answerability：只读消费已生成的证据，产出建议性结论。

判定表是**确定性的，不使用 LLM**（ARCHITECTURE §13.2）：安全、权限、SQL、审批与
终态一律由确定性断言验收。
"""

import datetime as dt

import pytest

from xiaowei_agent.capabilities.specs import CAPABILITY_ID, CAPABILITY_VERSION
from xiaowei_agent.contracts import EvidenceEnvelope, ExternalSource, TaskStatus
from xiaowei_agent.reflection.answerability import assess, terminal_status_for

_AT = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)


def _evidence(step_id: str, facts: tuple[dict[str, object], ...]) -> EvidenceEnvelope:
    return EvidenceEnvelope(
        evidence_id=f"task-1:{step_id}",
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_VERSION,
        facts=facts,
        source="starrocks-fake",
        source_kind=ExternalSource.TOOL,
        captured_at=_AT,
        sampled=False,
        limitations=("window: [11:30, 12:00)",),
    )


def _slow_rows() -> EvidenceEnvelope:
    return _evidence("s1", ({"queryId": "q1", "queryTime": 32_000},))


def _empty_s1() -> EvidenceEnvelope:
    return _evidence("s1", ())


def _count(value: int) -> EvidenceEnvelope:
    return _evidence("s2", ({"query_count": value},))


def test_slow_queries_found_is_answerable() -> None:
    verdict = assess(evidences=(_slow_rows(),))
    assert verdict.sufficient is True
    assert verdict.downgrade_suggestion is False
    assert terminal_status_for(verdict) is TaskStatus.SUCCEEDED


def test_empty_slow_queries_with_traffic_in_scope_is_answerable() -> None:
    """ "该范围内没有慢查询"是一个确定的答案，不是失败。"""
    verdict = assess(evidences=(_empty_s1(), _count(42)))
    assert verdict.sufficient is True
    assert verdict.downgrade_suggestion is False
    assert terminal_status_for(verdict) is TaskStatus.SUCCEEDED


def test_empty_slow_queries_without_traffic_in_scope_is_indeterminate() -> None:
    """范围内无任何审计数据 → 不能宣称"没有慢查询"，必须降级。"""
    verdict = assess(evidences=(_empty_s1(), _count(0)))
    assert verdict.sufficient is False
    assert verdict.downgrade_suggestion is True
    assert terminal_status_for(verdict) is TaskStatus.INDETERMINATE


def test_traffic_from_other_databases_does_not_make_the_answer_confident() -> None:
    """B1 的回归用例。

    请求指定了 database=sales，窗口内 sales 没有任何审计数据，但别的库有大量流量。
    s2 若不带过滤，count>0 会让系统自信地回答"sales 没有慢查询"——而正确答案是
    "拿不到 sales 的审计数据"。

    这条断言看的是 Answerability 一侧：**只要 count 证据来自带过滤的 s2**，
    它读到的就是 0，因此必须降级。count 带不带过滤由 T4 的集合等式断言承重。
    """
    verdict = assess(evidences=(_empty_s1(), _count(0)))
    assert verdict.sufficient is False
    assert verdict.downgrade_suggestion is True


def test_empty_s1_without_the_optional_branch_is_indeterminate() -> None:
    """s2 未执行（失败/超时/被跳过）时，"有没有慢查询"没有依据。"""
    verdict = assess(evidences=(_empty_s1(),))
    assert verdict.sufficient is False
    assert verdict.downgrade_suggestion is True
    assert terminal_status_for(verdict) is TaskStatus.INDETERMINATE


def test_no_evidence_at_all_is_indeterminate() -> None:
    """空证据**不得**渲染成成功。"""
    verdict = assess(evidences=())
    assert verdict.sufficient is False
    assert verdict.downgrade_suggestion is True
    assert verdict.missing
    assert terminal_status_for(verdict) is TaskStatus.INDETERMINATE


def test_verdict_carries_the_evidence_limitations() -> None:
    """限制必须传递到结论：可答性结论不能比证据更自信。"""
    verdict = assess(evidences=(_slow_rows(),))
    assert verdict.limitations


def test_sampled_evidence_is_reported_as_a_limitation() -> None:
    sampled = _slow_rows().model_copy(update={"sampled": True})
    assert any(
        "sampl" in item.lower() or "截断" in item
        for item in assess(evidences=(sampled,)).limitations
    )


def test_assessment_is_deterministic() -> None:
    evidences = (_empty_s1(), _count(0))
    assert assess(evidences=evidences) == assess(evidences=evidences)


def test_verdict_never_carries_execution_authority() -> None:
    """字段集之外的任何键在契约层无法表达。"""
    from xiaowei_agent.contracts import AnswerabilityVerdict

    assert set(AnswerabilityVerdict.model_fields) == {
        "sufficient",
        "limitations",
        "missing",
        "downgrade_suggestion",
        "needs_user_input",
    }
    assert not (
        {"steps", "tool", "sql", "approval_ref", "target", "policy_profile"}
        & set(AnswerabilityVerdict.model_fields)
    )


@pytest.mark.parametrize("hostile", ["steps", "tool", "sql", "approval_ref", "effect_class"])
def test_verdict_rejects_execution_authority_fields(hostile: str) -> None:
    from pydantic import ValidationError

    from xiaowei_agent.contracts import AnswerabilityVerdict

    with pytest.raises(ValidationError):
        AnswerabilityVerdict(
            sufficient=True,
            limitations=(),
            missing=(),
            downgrade_suggestion=False,
            needs_user_input=False,
            **{hostile: "x"},
        )
