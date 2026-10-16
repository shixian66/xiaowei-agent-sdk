"""``PlanStore`` 的行为用例：**一份定义，两处绑定**。

与 ``tests/suites/task_store.py`` 同一个理由与同一套机制：M4 要让 ``InMemoryPlanStore``
与 ``PostgresPlanStore`` 通过同一批断言，而"两个实现各自跑偏"不会被任何单实现的用例
发现。

fixture 名是 ``plan_store`` 而不是 ``store``——后者已被 TaskStore 占用，两者在同一个
conftest 树下会互相覆盖。这是纯改名，一行行为断言未改。

**新增用例必须同时加进下面的分组**，否则它在所有绑定里都收不到。
"""

import pytest
from tests.fakes.admission import TARGET, slow_query_plan
from tests.suites.task_store import bind

from xiaowei_agent.persistence.plans import PlanConflictError, PlanNotFoundError
from xiaowei_agent.planning import compute_plan_hash

_TASK = "task-1"

__all__ = ["ALL_GROUPS", "PLAN_STORE_CASES", "bind"]


async def test_saved_plan_round_trips(plan_store) -> None:
    plan = slow_query_plan()
    await plan_store.save(task_id=_TASK, plan=plan, target=TARGET)
    stored = await plan_store.load(task_id=_TASK)
    assert stored.plan == plan
    assert stored.target == TARGET


async def test_reloaded_plan_recomputes_to_the_same_hash(plan_store) -> None:
    """往返必须保 hash：否则 resume 的漂移检测会把每一次恢复都判成漂移。"""
    plan = slow_query_plan()
    await plan_store.save(task_id=_TASK, plan=plan, target=TARGET)
    stored = await plan_store.load(task_id=_TASK)
    assert compute_plan_hash(stored.plan) == compute_plan_hash(plan)


async def test_saving_the_same_plan_twice_is_idempotent(plan_store) -> None:
    plan = slow_query_plan()
    await plan_store.save(task_id=_TASK, plan=plan, target=TARGET)
    await plan_store.save(task_id=_TASK, plan=plan, target=TARGET)
    assert (await plan_store.load(task_id=_TASK)).plan == plan


async def test_saving_a_different_plan_for_the_same_task_is_refused(plan_store) -> None:
    """计划一旦被执行或送审，它就是这次任务的事实。

    静默覆盖等于给自己开一条计划漂移的口子：审批批的是旧计划，执行的是新计划。
    """
    plan = slow_query_plan()
    await plan_store.save(task_id=_TASK, plan=plan, target=TARGET)
    other = plan.model_copy(update={"policy_revision": "policy-2026-10-01"})
    with pytest.raises(PlanConflictError):
        await plan_store.save(task_id=_TASK, plan=other, target=TARGET)
    assert (await plan_store.load(task_id=_TASK)).plan == plan


async def test_saving_a_different_target_for_the_same_task_is_refused(plan_store) -> None:
    plan = slow_query_plan()
    await plan_store.save(task_id=_TASK, plan=plan, target=TARGET)
    other = TARGET.model_copy(update={"environment_id": "test"})
    with pytest.raises(PlanConflictError):
        await plan_store.save(task_id=_TASK, plan=plan, target=other)


async def test_loading_an_unknown_task_raises(plan_store) -> None:
    with pytest.raises(PlanNotFoundError):
        await plan_store.load(task_id="never-saved")


async def test_conflict_error_does_not_echo_the_task_id(plan_store) -> None:
    """与 TaskIdCarryingError 同一模式：诊断值放结构化属性，不进错误文本。"""
    canary = "task-canary-8821"
    plan = slow_query_plan()
    await plan_store.save(task_id=canary, plan=plan, target=TARGET)
    other = plan.model_copy(update={"policy_revision": "policy-2026-10-01"})
    with pytest.raises(PlanConflictError) as err:
        await plan_store.save(task_id=canary, plan=other, target=TARGET)
    assert canary not in str(err.value)
    assert err.value.task_id == canary


async def test_tasks_are_isolated_from_each_other(plan_store) -> None:
    plan = slow_query_plan()
    await plan_store.save(task_id="a", plan=plan, target=TARGET)
    with pytest.raises(PlanNotFoundError):
        await plan_store.load(task_id="b")


PLAN_STORE_CASES = (
    test_saved_plan_round_trips,
    test_reloaded_plan_recomputes_to_the_same_hash,
    test_saving_the_same_plan_twice_is_idempotent,
    test_saving_a_different_plan_for_the_same_task_is_refused,
    test_saving_a_different_target_for_the_same_task_is_refused,
    test_loading_an_unknown_task_raises,
    test_conflict_error_does_not_echo_the_task_id,
    test_tasks_are_isolated_from_each_other,
)

ALL_GROUPS = {"plan_store": PLAN_STORE_CASES}
