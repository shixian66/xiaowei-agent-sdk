"""Runner 契约：采纳存储层 winner、不越过终态、不触碰 Gateway。"""

import inspect
from pathlib import Path

import pytest
from tests.conftest import lookup_for
from tests.fakes.admission import CONTEXT
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET

from xiaowei_agent.contracts import AttemptIntent, TaskStatus
from xiaowei_agent.persistence.store import TaskAttemptCommand
from xiaowei_agent.runners.fake import ScriptedRunner, TerminalOrLeasedTaskError
from xiaowei_agent.runners.runner import WorkflowRunner

# ``WorkflowRunner`` 的活输入（见 ARCHITECTURE §5.6）。ScriptedRunner 不消费它们，
# 但契约要求它们被传入——测试按契约调用，不按实现走捷径。
_LIVE = {"plan": FIXTURE_PLAN, "target": FIXTURE_TARGET, "context": CONTEXT}


async def _grant(store, task):
    result = await store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=task.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="scripted-runner",
            ttl_seconds=60,
            trace_id=CONTEXT.trace_id,
        )
    )
    assert result.grant is not None
    return result.grant


def test_scripted_runner_satisfies_the_workflow_runner_protocol(store) -> None:
    runner: WorkflowRunner = ScriptedRunner(store, outcome_status=TaskStatus.SUCCEEDED)
    assert runner is not None


async def test_runner_adopts_the_storage_winner_version(store, task) -> None:
    runner = ScriptedRunner(store, outcome_status=TaskStatus.SUCCEEDED)
    outcome = await runner.start(await _grant(store, task), **_LIVE)
    assert outcome.status is TaskStatus.SUCCEEDED
    final = await store.get(lookup=lookup_for(task))
    assert final.version == 3  # created→planning→running→succeeded


async def test_resume_on_a_terminal_task_does_not_overwrite_it(store, task) -> None:
    """终态保护由存储层承重；旧 grant 不能覆盖已经落下的终态。"""
    runner = ScriptedRunner(store, outcome_status=TaskStatus.SUCCEEDED)
    grant = await _grant(store, task)
    await runner.start(grant, **_LIVE)
    before = await store.get(lookup=lookup_for(task))
    with pytest.raises(TerminalOrLeasedTaskError):
        await runner.resume(grant, **_LIVE)
    after = await store.get(lookup=lookup_for(task))
    assert after.version == before.version
    assert after.status is TaskStatus.SUCCEEDED


async def test_runner_can_reach_rejected_from_running(store, task) -> None:
    runner = ScriptedRunner(store, outcome_status=TaskStatus.REJECTED)
    outcome = await runner.start(await _grant(store, task), **_LIVE)
    assert outcome.status is TaskStatus.REJECTED


def test_runner_rejects_a_non_terminal_outcome(store) -> None:
    with pytest.raises(ValueError, match="terminal outcome status"):
        ScriptedRunner(store, outcome_status=TaskStatus.RUNNING)


async def test_runner_preserves_the_grant_owner(store, task) -> None:
    """Runner 不接受第二份 owner；执行权只来自调度层给出的 grant。"""
    runner = ScriptedRunner(store, outcome_status=TaskStatus.SUCCEEDED)
    result = await store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=task.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="w-alpha",
            ttl_seconds=60,
            trace_id=CONTEXT.trace_id,
        )
    )
    assert result.grant is not None
    await runner.start(result.grant, **_LIVE)
    final = await store.get(lookup=lookup_for(task))
    assert final.lease_owner == "w-alpha"


async def test_two_runners_cannot_drive_the_same_task_concurrently(store, task, clock) -> None:
    """Runner 只接受调度层给出的 grant，不得自行竞争或接管租约。"""
    stale = await _grant(store, task)
    clock.advance(seconds=61)
    takeover = await store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=task.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="w2",
            ttl_seconds=60,
            trace_id=CONTEXT.trace_id,
        )
    )
    assert takeover.grant is not None

    stale_runner = ScriptedRunner(store, outcome_status=TaskStatus.SUCCEEDED)
    with pytest.raises(RuntimeError, match="transition rejected"):
        await stale_runner.start(stale, **_LIVE)

    winner = ScriptedRunner(store, outcome_status=TaskStatus.SUCCEEDED)
    await winner.start(takeover.grant, **_LIVE)
    final = await store.get(lookup=lookup_for(task))
    assert final.lease_owner == "w2"


def test_runner_never_imports_a_gateway_or_adapter() -> None:
    """M2 的 fake runner 不执行步骤，因此 adapter 调用次数恒为 0。

    **扫 AST 的 import 而不是原始文本**：文本扫描会被 docstring 里"不接触
    ToolGateway"这句话本身触发，断言的就不再是代码行为。
    """
    import ast

    from xiaowei_agent.runners import fake

    source = Path(inspect.getfile(fake)).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        if module:
            imported.add(module)
        if isinstance(node, ast.ImportFrom):
            imported |= {f"{node.module}.{a.name}" for a in node.names}
    assert not any("tools" in name for name in imported), imported
