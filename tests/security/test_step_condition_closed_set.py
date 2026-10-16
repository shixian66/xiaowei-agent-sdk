"""可选只读分支的条件必须是闭集，不能表达任意谓词。

这是"被实现成变相动态扩计划"这一残余风险的直接防线。
"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    StepCondition,
    StepConditionKind,
    StepResultStatus,
    TaskStatus,
)

pytestmark = pytest.mark.security


def test_condition_kinds_are_exactly_four() -> None:
    assert {m.value for m in StepConditionKind} == {
        "always",
        "evidence_field_absent",
        "evidence_row_count_below",
        "prior_step_result_is",
    }


def test_step_result_and_task_status_values_are_disjoint() -> None:
    """两个枚举的**取值**必须不相交。

    只把类型分开不够：StrEnum 成员就是字符串，取值相同时把 TaskStatus 传给步骤
    条件会在 lax 模式下被静默接受，两个层级在数据上重新混为一谈。
    """
    assert not ({m.value for m in StepResultStatus} & {m.value for m in TaskStatus})


def test_condition_references_step_result_not_task_status() -> None:
    """步骤结果与任务状态分属两个层级，混用会让两者不可区分。"""
    ok = StepCondition(
        kind=StepConditionKind.PRIOR_STEP_RESULT_IS,
        ref_step_id="s1",
        expected_result=StepResultStatus.FAILED,
    )
    assert ok.expected_result is StepResultStatus.FAILED
    with pytest.raises(ValidationError):
        StepCondition(
            kind=StepConditionKind.PRIOR_STEP_RESULT_IS,
            ref_step_id="s1",
            expected_result=TaskStatus.FAILED,
        )


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        StepCondition(kind="model_says_so")


def test_condition_cannot_carry_free_form_expression() -> None:
    with pytest.raises(ValidationError):
        StepCondition(kind=StepConditionKind.ALWAYS, expression="len(rows) > 0")


def test_always_forbids_all_operands() -> None:
    """ALWAYS 携带操作数说明构造方混淆了语义，拒绝而非忽略。"""
    with pytest.raises(ValidationError):
        StepCondition(kind=StepConditionKind.ALWAYS, ref_step_id="s1")


def test_each_kind_requires_exactly_its_operands() -> None:
    with pytest.raises(ValidationError):
        StepCondition(kind=StepConditionKind.EVIDENCE_FIELD_ABSENT, ref_step_id="s1")
    with pytest.raises(ValidationError):
        StepCondition(
            kind=StepConditionKind.EVIDENCE_FIELD_ABSENT,
            ref_step_id="s1",
            field="db",
            threshold=1,
        )
    ok = StepCondition(kind=StepConditionKind.EVIDENCE_FIELD_ABSENT, ref_step_id="s1", field="db")
    assert ok.field == "db"


@pytest.mark.parametrize("bad", [-1, True, "1"])
def test_row_count_threshold_must_be_a_non_negative_strict_int(bad: object) -> None:
    with pytest.raises(ValidationError):
        StepCondition(
            kind=StepConditionKind.EVIDENCE_ROW_COUNT_BELOW,
            ref_step_id="s1",
            threshold=bad,
        )


def test_copy_cannot_produce_an_operand_mismatch() -> None:
    ok = StepCondition(kind=StepConditionKind.EVIDENCE_FIELD_ABSENT, ref_step_id="s1", field="db")
    with pytest.raises(ValidationError):
        ok.model_copy(update={"kind": StepConditionKind.ALWAYS})
