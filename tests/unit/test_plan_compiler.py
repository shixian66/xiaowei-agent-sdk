"""PlanCompiler：从候选 + 参数 + 目标编译出确定性 ``ExecutionPlan``。"""

import datetime as dt

import pytest

from xiaowei_agent.capabilities.effect import verify_plan_effects
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.capabilities.specs import (
    CAPABILITY_ID,
    CAPABILITY_VERSION,
    OP_COUNT,
    OP_LIST,
    POLICY_PROFILE,
)
from xiaowei_agent.capabilities.specs import (
    SLOW_QUERY_SURFACE as SURFACE,
)
from xiaowei_agent.capabilities.target import resolve_target
from xiaowei_agent.contracts import (
    Candidate,
    CapabilitySnapshot,
    CapabilitySpec,
    EffectClass,
    IntentDraft,
    IntentSource,
    OperationSpec,
    PlanBudget,
    RequestContext,
    StepConditionKind,
)
from xiaowei_agent.planning import compute_plan_hash
from xiaowei_agent.planning.starrocks.compiler import (
    COUNT_V1,
    LIST_V1,
    TEMPLATE_PARAM_KEYS,
    compile_plan,
    compile_sql,
)
from xiaowei_agent.planning.starrocks.params import SlowQueryParams

SNAPSHOT = StaticCapabilityRegistry().snapshot()
CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-01",
)
DRAFT = IntentDraft(
    intent=CAPABILITY_ID, slots={}, missing=(), confidence=0.9, source=IntentSource.USER
)
TARGET = resolve_target(context=CONTEXT, draft=DRAFT)

_PARAMS = SlowQueryParams(
    window_start=dt.datetime(2026, 9, 2, 11, 30, tzinfo=dt.UTC),
    window_end=dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC),
    min_query_time_ms=10_000,
    row_limit=20,
)


def _entry_candidate(snapshot: CapabilitySnapshot = SNAPSHOT) -> Candidate:
    candidates = DeterministicCapabilityResolver().resolve(
        draft=DRAFT, context=CONTEXT, snapshot=snapshot
    )
    return next(item for item in candidates.items if item.operation == OP_LIST)


def _plan(*, params: SlowQueryParams = _PARAMS, snapshot: CapabilitySnapshot = SNAPSHOT) -> object:
    return compile_plan(
        candidate=_entry_candidate(snapshot),
        params=params,
        target=TARGET,
        context=CONTEXT,
        snapshot=snapshot,
        surface=SURFACE,
    )


def _snapshot_with(operation: str, effect: EffectClass, *, side_effect: bool) -> CapabilitySnapshot:
    """把同名 operation 在快照里改声明成写操作。"""
    spec = SNAPSHOT.specs[0]
    return CapabilitySnapshot(
        snapshot_id=SNAPSHOT.snapshot_id,
        specs=(
            CapabilitySpec(
                capability_id=spec.capability_id,
                version=spec.version,
                domain=spec.domain,
                operations=tuple(
                    OperationSpec(
                        operation=op.operation,
                        effect_class=effect if op.operation == operation else op.effect_class,
                        side_effect=(side_effect if op.operation == operation else op.side_effect),
                        argument_schema_ref=op.argument_schema_ref,
                    )
                    for op in spec.operations
                ),
                policy_profile=spec.policy_profile,
                evidence_contract=spec.evidence_contract,
                eval_ref=spec.eval_ref,
            ),
        ),
    )


def test_plan_has_two_steps_with_the_declared_shape() -> None:
    plan = _plan()
    assert [s.step_id for s in plan.steps] == ["s1", "s2"]
    assert plan.steps[0].operation == OP_LIST
    assert plan.steps[1].operation == OP_COUNT
    assert plan.steps[0].depends_on == ()
    assert plan.steps[1].depends_on == ("s1",)


def test_plan_binds_the_single_capability_from_the_candidate() -> None:
    plan = _plan()
    assert (plan.capability_id, plan.capability_version) == (
        CAPABILITY_ID,
        CAPABILITY_VERSION,
    )
    assert plan.policy_profile == POLICY_PROFILE
    assert plan.policy_revision == CONTEXT.policy_revision


def test_optional_branch_condition_is_the_declared_closed_set_member() -> None:
    condition = _plan().steps[1].condition
    assert condition.kind is StepConditionKind.EVIDENCE_ROW_COUNT_BELOW
    assert (condition.ref_step_id, condition.threshold) == ("s1", 1)


def test_first_step_runs_unconditionally() -> None:
    assert _plan().steps[0].condition.kind is StepConditionKind.ALWAYS


def test_budget_is_declared_and_covers_the_plan() -> None:
    plan = _plan()
    assert plan.budget == PlanBudget(max_steps=2, max_tool_calls=2, max_model_tokens=4000)
    assert len(plan.steps) <= plan.budget.max_steps


def test_typed_arguments_match_the_template_key_sets_exactly() -> None:
    """多一个键或少一个键都要失败。

    typed_arguments 进 plan_hash，未被消费的键会成为"看起来生效但其实不影响执行"
    的第二真源。
    """
    plan = _plan()
    for step, template in zip(plan.steps, (LIST_V1, COUNT_V1), strict=True):
        assert set(step.typed_arguments) == {
            "sql",
            "sql_template_id",
            *TEMPLATE_PARAM_KEYS[template],
        }


def test_each_step_declares_the_template_it_was_compiled_from() -> None:
    plan = _plan()
    for step, template in zip(plan.steps, (LIST_V1, COUNT_V1), strict=True):
        assert step.typed_arguments["sql_template_id"] == template


def test_typed_arguments_are_json_scalars_only() -> None:
    for step in _plan().steps:
        for value in step.typed_arguments.values():
            assert value is None or isinstance(value, bool | int | float | str)


def test_effect_classification_is_derived_not_hardcoded() -> None:
    """把同名 operation 在快照里改声明成写操作，编译出的步骤必须随之变化。

    这证明分类确实来自 build_plan_step() 的派生，而不是编译器里写死的常量。
    """
    hostile = _snapshot_with(OP_LIST, EffectClass.MUTATE_TARGET, side_effect=True)
    step = _plan(snapshot=hostile).steps[0]
    assert step.effect_class is EffectClass.MUTATE_TARGET
    assert step.side_effect is True


def test_default_classification_is_read_only() -> None:
    """反例配对：未被篡改的快照必须编译出只读步骤。"""
    for step in _plan().steps:
        assert step.effect_class is EffectClass.READ
        assert step.side_effect is False


def test_plan_hash_is_stable_for_the_same_inputs() -> None:
    assert compute_plan_hash(_plan()) == compute_plan_hash(_plan())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window_start", dt.datetime(2026, 9, 2, 11, 45, tzinfo=dt.UTC)),
        ("window_end", dt.datetime(2026, 9, 2, 11, 59, tzinfo=dt.UTC)),
        ("min_query_time_ms", 20_000),
        ("row_limit", 50),
        ("database", "sales"),
        ("user_name", "app_user_1"),
        ("query_id", "q1"),
    ],
)
def test_changing_any_param_changes_the_plan_hash(field: str, value: object) -> None:
    """逐参数敏感性。

    任何一个参数不进 hash，都意味着一份已批准的计划能执行不同的查询。参数化覆盖
    全部字段，而不是抽查两个。
    """
    changed = _PARAMS.model_copy(update={field: value})
    assert compute_plan_hash(_plan()) != compute_plan_hash(_plan(params=changed))


def test_each_step_carries_the_sql_that_its_params_compile_to() -> None:
    """SQL 与参数的绑定：步骤里携带的 SQL 必须等于用该步骤参数重编译的结果。

    这是 SQLGuard 规则 0 在编译侧的对偶断言。
    """
    for step, template in zip(_plan().steps, (LIST_V1, COUNT_V1), strict=True):
        params = SlowQueryParams.from_typed_arguments(step.typed_arguments)
        assert step.typed_arguments["sql"] == compile_sql(
            template_id=template, params=params, surface=SURFACE
        )


def test_compiled_plan_passes_verify_plan_effects() -> None:
    verify_plan_effects(SNAPSHOT, _plan())


def test_candidate_for_a_non_entry_operation_is_refused() -> None:
    """步骤顺序是编译器的确定性决定，不由候选挑选。"""
    count_candidate = next(
        item
        for item in DeterministicCapabilityResolver()
        .resolve(draft=DRAFT, context=CONTEXT, snapshot=SNAPSHOT)
        .items
        if item.operation == OP_COUNT
    )
    with pytest.raises(ValueError, match="entry operation"):
        compile_plan(
            candidate=count_candidate,
            params=_PARAMS,
            target=TARGET,
            context=CONTEXT,
            snapshot=SNAPSHOT,
            surface=SURFACE,
        )


def test_target_from_another_environment_is_refused() -> None:
    """目标与上下文不一致时 fail-closed，不静默取其中一个。"""
    other = TARGET.model_copy(update={"environment_id": "test"})
    with pytest.raises(ValueError, match="environment"):
        compile_plan(
            candidate=_entry_candidate(),
            params=_PARAMS,
            target=other,
            context=CONTEXT,
            snapshot=SNAPSHOT,
            surface=SURFACE,
        )
