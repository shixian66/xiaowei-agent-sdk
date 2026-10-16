"""审计写入的守卫必须在副作用**之前**执行 —— 用 AST 钉，因为行为证不了。

``record_audit_event`` 必须先拒绝 ``task_id is None`` 的事件，再做任何分配或写入。
把顺序反过来是一个真实的缺陷：PostgreSQL 侧会先开事务、先取 advisory lock（参数是
``None``，直接炸在驱动层），或者更糟——落一行 ``task_id`` 是占位值的垃圾审计。

**但这条顺序没有独立的行为反证。** 没有 ``task_id`` 就没有桶可污染：任何"先分配再
校验"的内存实现都只会往一个占位桶里写，从 Protocol 上完全观察不到，于是把守卫挪到
后面**不会让任何用例转红**。这与 M4 §8.4 的 S2 是同一处境——纵深防御按定义没有独立
的行为反证——处置也相同：改用机械检查。

落盘层面另有一条断言（``tests/integration/test_audit_and_approval_ledgers.py`` 直接
读表确认拒绝没有留下任何行），但那条要有 PostgreSQL 才跑得了。本文件在默认路径上跑。
"""

import ast
import inspect
from pathlib import Path

import pytest

from xiaowei_agent.persistence import fake, postgres

pytestmark = pytest.mark.security

_IMPLEMENTATIONS = {
    "InMemoryTaskStore": fake,
    "PostgresTaskStore": postgres,
}


def _method_body(module: object, class_name: str, method: str) -> list[ast.stmt]:
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == method:
                    return child.body
    raise AssertionError(f"{class_name}.{method} 不存在")


def _is_guard(statement: ast.stmt) -> bool:
    """``if event.task_id is None: raise UnscopedAuditEventError(...)``。"""
    if not isinstance(statement, ast.If):
        return False
    return any(
        isinstance(node, ast.Name) and node.id == "UnscopedAuditEventError"
        for node in ast.walk(statement)
    )


def _has_side_effect(statement: ast.stmt) -> bool:
    """开事务、取锁、写入——任何 ``await`` 或 ``async with`` 都算。"""
    return any(isinstance(node, ast.Await | ast.AsyncWith) for node in ast.walk(statement))


@pytest.mark.parametrize("class_name", sorted(_IMPLEMENTATIONS))
def test_the_unscoped_guard_precedes_every_side_effect(class_name: str) -> None:
    body = _method_body(_IMPLEMENTATIONS[class_name], class_name, "record_audit_event")
    statements = [node for node in body if not isinstance(node, ast.Expr)]
    guard_positions = [index for index, node in enumerate(statements) if _is_guard(node)]
    assert guard_positions, f"{class_name}.record_audit_event 没有 task_id 守卫"
    effect_positions = [index for index, node in enumerate(statements) if _has_side_effect(node)]
    assert effect_positions, f"{class_name}.record_audit_event 没有副作用——扫描坏了"
    assert guard_positions[0] < effect_positions[0], (
        f"{class_name}.record_audit_event 的守卫排在第一个副作用之后"
    )


@pytest.mark.parametrize("class_name", sorted(_IMPLEMENTATIONS))
def test_both_implementations_raise_the_same_error(class_name: str) -> None:
    """两个实现必须抛同一个异常。

    一边抛 ``UnscopedAuditEventError``、另一边让驱动抛 ``IntegrityError``，调用方就得
    按实现分别 except——那正是"两个实现各自跑偏"的样子，且不会有任何用例失败。
    """
    body = _method_body(_IMPLEMENTATIONS[class_name], class_name, "record_audit_event")
    raised = {
        node.exc.func.id
        for statement in body
        for node in ast.walk(statement)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
    }
    assert raised == {"UnscopedAuditEventError"}


async def test_admission_rejection_is_durable_without_creating_evidence() -> None:
    """准入拒绝发生在 step begin 之前，只能由 durable sink 留下审计。"""
    from tests.fakes.recordings import GOLDEN
    from tests.fakes.runner import RunnerHarness

    from xiaowei_agent.contracts import PipelineStage, StageOutcome
    from xiaowei_agent.observability.durable_sink import DurableTraceSink

    harness = RunnerHarness(GOLDEN, tamper_sql=True)
    harness.runner._sink = DurableTraceSink(writer=harness.store, log_sink=harness.sink)
    with pytest.raises(Exception):  # noqa: B017 - 本条只承重 durable audit
        await harness.start()

    rejected = [
        event
        for event in harness.state.audit_events[harness.task_id]
        if event.stage is PipelineStage.ADMISSION and event.outcome is StageOutcome.REJECTED
    ]
    assert len(rejected) == 1
    assert await harness.ledger.load(task_id=harness.task_id) == ()
