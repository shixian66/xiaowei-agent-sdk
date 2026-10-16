"""候选集必须无歧义、可确定性排序。"""

import math

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import Candidate, CandidateSet, Rejection


def _candidate(**overrides: object) -> Candidate:
    base: dict[str, object] = {
        "capability_id": "starrocks.slow_query.diagnose",
        "capability_version": "1.0.0",
        "operation": "list_slow_queries",
        "score": 0.9,
        "match_evidence": ("domain match",),
        "required_context": ("environment_id",),
    }
    return Candidate(**(base | overrides))


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, -0.1, 1.1])
def test_score_must_be_a_finite_unit_interval_value(bad: float) -> None:
    with pytest.raises(ValidationError):
        _candidate(score=bad)


def test_duplicate_candidate_is_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate candidate"):
        CandidateSet(
            resolver_version="1",
            snapshot_id="s",
            items=(_candidate(), _candidate()),
            rejections=(),
        )


def _rejection(**overrides: object) -> Rejection:
    base: dict[str, object] = {
        "capability_id": "starrocks.slow_query.diagnose",
        "capability_version": "1.0.0",
        "operation": "list_slow_queries",
        "reason_code": "x",
    }
    return Rejection(**(base | overrides))


def test_rejection_granularity_matches_candidate_granularity() -> None:
    """两者必须同为三元组。

    若拒绝只到 capability/version 级，同一 capability 的一个 operation 被拒就会
    与另一个 operation 的候选冲突，"这个能力到底是候选还是被拒"没有确定答案。
    """
    assert {"capability_id", "capability_version", "operation"} <= set(Rejection.model_fields)


def test_duplicate_rejection_is_rejected() -> None:
    reject = _rejection()
    with pytest.raises(ValidationError, match="duplicate rejection"):
        CandidateSet(resolver_version="1", snapshot_id="s", items=(), rejections=(reject, reject))


def test_operation_cannot_be_both_candidate_and_rejection() -> None:
    with pytest.raises(ValidationError, match="both candidate and rejection"):
        CandidateSet(
            resolver_version="1",
            snapshot_id="s",
            items=(_candidate(),),
            rejections=(_rejection(),),
        )


def test_another_operation_of_the_same_capability_may_be_rejected() -> None:
    """同一 capability 的不同 operation 可以一个入选、一个被拒——这正是需要三元组
    粒度的原因。"""
    got = CandidateSet(
        resolver_version="1",
        snapshot_id="s",
        items=(_candidate(),),
        rejections=(_rejection(operation="describe_profile"),),
    )
    assert len(got.items) == 1
    assert len(got.rejections) == 1


def test_same_capability_with_different_operations_is_allowed() -> None:
    got = CandidateSet(
        resolver_version="1",
        snapshot_id="s",
        items=(_candidate(), _candidate(operation="describe_profile")),
        rejections=(),
    )
    assert len(got.items) == 2
