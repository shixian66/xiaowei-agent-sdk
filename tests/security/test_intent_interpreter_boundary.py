"""IntentDraft 是全项目不可信度最高的 DTO，解释器是它的唯一生产者。

模型（或直接把用户原文当模型输出的规则实现）可以往槽位里塞任何键。解释器必须
只保留闭集内的槽位——**多余的键在结构上就不存在**，而不是靠下游记得忽略。
"""

import pytest

from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
from xiaowei_agent.contracts import IntentDraft, RequestContext

pytestmark = pytest.mark.security

CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-01",
)


@pytest.mark.parametrize(
    "hostile",
    [
        "sql",
        "operation",
        "capability_id",
        "effect_class",
        "side_effect",
        "policy_profile",
        "approval_ref",
    ],
)
def test_interpreter_never_emits_execution_authority_slots(hostile: str) -> None:
    """模型可以往 slots 塞任何键，解释器必须只保留闭集内的槽位。"""
    draft = RuleBasedIntentInterpreter().interpret(text=f"{hostile}=x 慢查询", context=CONTEXT)
    assert hostile not in draft.slots


def test_interpreter_slot_allowlist_is_closed() -> None:
    """反向：断言允许的槽位集合恰为声明的闭集，新增槽位必须显式过评审。"""
    assert RuleBasedIntentInterpreter.ALLOWED_SLOTS == frozenset(
        {"environment_id", "database", "user_name", "query_id", "window_minutes"}
    )


def test_every_emitted_slot_is_inside_the_allowlist() -> None:
    """对一条同时命中全部槽位的输入，断言产出仍不越界。"""
    draft = RuleBasedIntentInterpreter().interpret(
        text=(
            "慢查询 database=sales user=app_user_1 query_id=q1 "
            "最近30分钟 environment_id=dev sql=DROP approval_ref=a1"
        ),
        context=CONTEXT,
    )
    assert set(draft.slots) <= RuleBasedIntentInterpreter.ALLOWED_SLOTS


def test_interpreter_output_is_an_intent_draft_with_no_extra_fields() -> None:
    """契约层已 extra='forbid'；这条确认解释器确实经由契约构造，而不是返回裸 dict。"""
    draft = RuleBasedIntentInterpreter().interpret(text="慢查询", context=CONTEXT)
    assert isinstance(draft, IntentDraft)


def test_interpretation_is_deterministic_for_the_same_text() -> None:
    """同一输入必须产生逐字段相同的草案：否则计划不可能稳定。"""
    interpreter = RuleBasedIntentInterpreter()
    text = "查一下 sales 库最近30分钟的慢查询"
    assert interpreter.interpret(text=text, context=CONTEXT) == interpreter.interpret(
        text=text, context=CONTEXT
    )


@pytest.mark.parametrize(
    "text",
    [
        "慢查询",
        "最近30分钟有哪些慢查询",
        "查一下 sales 库的慢查询",
        "starrocks 慢 SQL 排查",
        "哪些查询很慢",
        "帮我看下慢查询情况",
    ],
)
def test_slow_query_phrasings_are_recognised(text: str) -> None:
    draft = RuleBasedIntentInterpreter().interpret(text=text, context=CONTEXT)
    assert draft.intent == "starrocks.slow_query.diagnose"


@pytest.mark.parametrize(
    "text",
    [
        "导入任务失败了怎么办",
        "这张表的表结构是什么",
        "Prometheus 有哪些告警",
        "帮我重启一下集群",
    ],
)
def test_near_miss_phrasings_do_not_claim_the_slow_query_intent(text: str) -> None:
    """near-miss 必须**不**匹配本能力：匹配过宽等于把别的领域误路由过来。"""
    draft = RuleBasedIntentInterpreter().interpret(text=text, context=CONTEXT)
    assert draft.intent != "starrocks.slow_query.diagnose"


def test_hostile_slot_text_does_not_change_the_recognised_intent() -> None:
    """A25 的解释器侧：塞 sql 槽位不得改变意图判定，也不得进入槽位。"""
    plain = RuleBasedIntentInterpreter().interpret(text="慢查询", context=CONTEXT)
    polluted = RuleBasedIntentInterpreter().interpret(
        text="慢查询 sql=SELECT 1; DROP TABLE t", context=CONTEXT
    )
    assert polluted.intent == plain.intent
    assert "sql" not in polluted.slots


def test_slot_values_never_carry_sql_punctuation() -> None:
    """槽位取值只能是朴素标识符形状；引号、分号、注释符一律不得出现。

    这一层不是最终防线（参数层的 Identifier 才是），但让恶意载荷根本进不了
    ``IntentDraft``，可以使后续每一层都不必先做清洗。
    """
    draft = RuleBasedIntentInterpreter().interpret(
        text="慢查询 database='; DROP TABLE t; --", context=CONTEXT
    )
    for value in draft.slots.values():
        assert not (set(value) & set("'\"`;()* /\\"))
