"""每个 DTO 的**全部**字段都必须进入对应指纹。

靠人记得同步是行不通的：漏一个字段，被批准的计划就能在恢复时执行不同的动作。
这里改由显式映射表承重——给 DTO 加字段却没加进映射表，本文件立刻转红。
**嵌套 DTO 同样纳入**：PlanBudget 与 StepCondition 的字段是手写展开的，
不覆盖它们就会重演同一个缺陷。
"""

import pytest
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET, FIXTURE_TOOL_CALL

from xiaowei_agent.contracts import (
    ExecutionPlan,
    PlanBudget,
    PlanStep,
    ResolvedTarget,
    StepCondition,
    ToolCall,
)
from xiaowei_agent.planning import (
    PLAN_BUDGET_FIELD_TO_HASH_KEY,
    PLAN_FIELD_TO_HASH_KEY,
    STEP_CONDITION_FIELD_TO_HASH_KEY,
    STEP_FIELD_TO_HASH_KEY,
    TARGET_FIELD_TO_HASH_KEY,
    TOOL_CALL_FIELD_TO_HASH_KEY,
)
from xiaowei_agent.planning.canonical import (
    _budget_payload,
    _condition_payload,
    _plan_payload,
    _target_payload,
    _tool_call_payload,
)

pytestmark = pytest.mark.security

_CASES = [
    (ExecutionPlan, PLAN_FIELD_TO_HASH_KEY),
    (PlanStep, STEP_FIELD_TO_HASH_KEY),
    (PlanBudget, PLAN_BUDGET_FIELD_TO_HASH_KEY),
    (StepCondition, STEP_CONDITION_FIELD_TO_HASH_KEY),
    (ResolvedTarget, TARGET_FIELD_TO_HASH_KEY),
    (ToolCall, TOOL_CALL_FIELD_TO_HASH_KEY),
]


@pytest.mark.parametrize(("model", "mapping"), _CASES, ids=lambda x: getattr(x, "__name__", ""))
def test_every_model_field_maps_to_a_hash_key(model: type, mapping: dict[str, str]) -> None:
    assert set(mapping) == set(model.model_fields), (
        f"{model.__name__} 的字段集与 hash 映射表不一致；新增字段必须同时决定它是否进入指纹"
    )


def test_payload_keys_equal_the_declared_hash_keys() -> None:
    """实现真的用了映射表声明的键，而不是另写一份。"""
    assert set(_plan_payload(FIXTURE_PLAN)) == set(PLAN_FIELD_TO_HASH_KEY.values())
    assert set(_target_payload(FIXTURE_TARGET)) == set(TARGET_FIELD_TO_HASH_KEY.values())
    assert set(_tool_call_payload(FIXTURE_TOOL_CALL)) == set(TOOL_CALL_FIELD_TO_HASH_KEY.values())


def test_nested_payload_keys_equal_their_declared_hash_keys() -> None:
    payload = _plan_payload(FIXTURE_PLAN)
    step_payload = payload["ordered_steps"][0]
    assert set(step_payload) == set(STEP_FIELD_TO_HASH_KEY.values())
    assert set(step_payload["condition"]) == set(STEP_CONDITION_FIELD_TO_HASH_KEY.values())
    assert set(payload["budget"]) == set(PLAN_BUDGET_FIELD_TO_HASH_KEY.values())
    assert set(_condition_payload(FIXTURE_PLAN.steps[0].condition)) == set(
        STEP_CONDITION_FIELD_TO_HASH_KEY.values()
    )
    assert set(_budget_payload(FIXTURE_PLAN.budget)) == set(PLAN_BUDGET_FIELD_TO_HASH_KEY.values())


_VOLATILE_OR_SENSITIVE = {
    "request_id",
    "trace_id",
    "timestamp",
    "created_at",
    "occurred_at",
    "model_text",
    "prompt",
    "raw_sql",
    "connection_string",
    "token",
    "secret",
    "password",
    "display_text",
    "answer",
    "log",
}


def test_no_volatile_or_sensitive_key_enters_any_fingerprint() -> None:
    keys: set[str] = set()
    for _, mapping in _CASES:
        keys |= set(mapping.values())
    assert not (keys & _VOLATILE_OR_SENSITIVE)
