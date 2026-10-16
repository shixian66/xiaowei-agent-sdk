"""全局 fixture。

**这里不放行任何 socket，也不要在这里加放行 fixture。** 默认路径必须无网络。

需要放行时唯一被许可的形态是 ``tests/integration/conftest.py`` 里那条：从 DSN 解析出
单个 host，用**逐 item 的 ``allow_hosts`` marker** 只放行那一个。pytest-socket 0.8.1
官方的那个"放行 fixture"不能用——它命中后 ``pytest_runtest_setup`` 直接 return，
``allow_hosts`` 再也不会被解析，看起来像放行，实际是对该用例全开。仓库禁用它，由
``tests/security/test_integration_network_boundary.py`` 承重。
"""

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.fakes.clock import ManualClock

    from xiaowei_agent.contracts import (
        AdmissionCertificate,
        ExecutionPlan,
        RequestContext,
        RequestEnvelope,
        TaskLookup,
        TaskRecord,
        TaskStatus,
        TaskSubmission,
        ToolCall,
    )
    from xiaowei_agent.persistence.evidence import InMemoryEvidenceLedger
    from xiaowei_agent.persistence.fake import InMemoryTaskStore
    from xiaowei_agent.persistence.memory import InMemoryPersistenceState
    from xiaowei_agent.persistence.plans import InMemoryPlanStore
    from xiaowei_agent.persistence.store import TaskStore
    from xiaowei_agent.tools.fake import RecordingToolAdapter
    from xiaowei_agent.tools.gateway import DeterministicToolGateway

_PREFIX = "XIAOWEI_"


@pytest.fixture(autouse=True)
def clean_xiaowei_env() -> Iterator[None]:
    """每个用例前清除全部 XIAOWEI_* 变量，结束后原样恢复。"""
    saved = {k: v for k, v in os.environ.items() if k.upper().startswith(_PREFIX)}
    for k in saved:
        del os.environ[k]
    try:
        yield
    finally:
        for k in [k for k in os.environ if k.upper().startswith(_PREFIX)]:
            del os.environ[k]
        os.environ.update(saved)


@pytest.fixture
def two_step_plan() -> "ExecutionPlan":
    """两步计划；用于证明 ordered_steps 的顺序进入 plan_hash。"""
    from tests.fakes.fixtures import FIXTURE_PLAN

    first = FIXTURE_PLAN.steps[0]
    second = first.model_copy(update={"step_id": "s2", "depends_on": ("s1",)})
    return FIXTURE_PLAN.model_copy(update={"steps": (first, second)})


@pytest.fixture
def context() -> "RequestContext":
    from xiaowei_agent.contracts import RequestContext

    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="0" * 32,
        policy_revision="policy-2026-09-01",
    )


@pytest.fixture
def ok_call() -> "ToolCall":
    from tests.fakes.fixtures import FIXTURE_TOOL_CALL

    return FIXTURE_TOOL_CALL


@pytest.fixture
def recording_adapter() -> "RecordingToolAdapter":
    from xiaowei_agent.contracts import AdapterStatus
    from xiaowei_agent.tools.adapter import AdapterResponse
    from xiaowei_agent.tools.fake import RecordingToolAdapter

    return RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.OK,
                payload=({"query_id": "q1"},),
                source="starrocks-fake",
                error=None,
                elapsed_ms=3,
            ),
        )
    )


@pytest.fixture
def gateway(recording_adapter: "RecordingToolAdapter") -> "DeterministicToolGateway":
    from xiaowei_agent.tools.gateway import DeterministicToolGateway

    return DeterministicToolGateway(adapters={"starrocks": recording_adapter})


def make_certificate(call: "ToolCall", **overrides: object) -> "AdmissionCertificate":
    """构造与给定调用匹配的准入凭证。

    默认路径是"合法"的；测试通过 overrides 制造各种不匹配，因此这个工厂本身
    不能有任何"聪明"的补偿逻辑。
    """
    from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET

    from xiaowei_agent.contracts import (
        EffectClass,
        PolicyDecision,
        RequestContext,
        RiskLevel,
    )
    from xiaowei_agent.governance import issue_admission_certificate
    from xiaowei_agent.planning import (
        compute_plan_hash,
        compute_target_fingerprint,
    )

    certificate = issue_admission_certificate(
        call=call,
        context=RequestContext(
            tenant_id="dev-local",
            actor="alice",
            environment_id="dev",
            trace_id="0" * 32,
            policy_revision="policy-2026-09-01",
        ),
        decision=PolicyDecision(
            allow=True,
            reason_code="readonly.allowed",
            risk=RiskLevel.LOW,
            policy_revision="policy-2026-09-01",
            obligations=(),
        ),
        effect_class=EffectClass.READ,
        plan_hash=compute_plan_hash(FIXTURE_PLAN),
        target_fingerprint=compute_target_fingerprint(FIXTURE_TARGET),
    )
    if not overrides:
        return certificate
    # 凭证不可复制（复制出的凭证不再由 StepAdmission 签发），因此制造"不匹配"
    # 的测试凭证必须重新签发。overrides 里的 tool_call_hash 无法经签发入口伪造
    # ——那正是这条边界的意义——需要它的用例改用 forge_certificate。
    return _reissue(certificate, overrides)


def _reissue(
    certificate: "AdmissionCertificate", overrides: dict[str, object]
) -> "AdmissionCertificate":
    """按 overrides 重新签发一张凭证，走与生产同一条签发路径。"""
    from xiaowei_agent.contracts import AdmissionCertificate
    from xiaowei_agent.contracts.approval import (
        _ADMISSION_WITNESS,
        _ADMISSION_WITNESS_KEY,
    )

    payload = {f: getattr(certificate, f) for f in AdmissionCertificate.model_fields}
    return AdmissionCertificate.model_validate(
        {_ADMISSION_WITNESS_KEY: _ADMISSION_WITNESS, **payload, **overrides}
    )


@pytest.fixture
def admission(ok_call: "ToolCall") -> "AdmissionCertificate":
    return make_certificate(ok_call)


def make_envelope(**overrides: object) -> "RequestEnvelope":
    from xiaowei_agent.contracts import Channel, RequestEnvelope

    base: dict[str, object] = {
        "request_id": "r1",
        "tenant_id": "dev-local",
        "actor": "alice",
        "channel": Channel.CLI,
        "text": "why is the query slow",
        "idempotency_key": "idem-1",
        "environment_id": "dev",
    }
    return RequestEnvelope(**(base | overrides))


def make_submission(
    context: "RequestContext",
    *,
    envelope: "RequestEnvelope | None" = None,
    as_of: object | None = None,
) -> "TaskSubmission":
    """构造提交事实；默认时间固定，避免测试隐式读取进程时钟。"""
    import datetime as dt

    from xiaowei_agent.contracts import TaskSubmission

    return TaskSubmission(
        envelope=make_envelope() if envelope is None else envelope,
        context=context,
        as_of=dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.UTC) if as_of is None else as_of,
    )


def lookup_for(record: "TaskRecord") -> "TaskLookup":
    from xiaowei_agent.contracts import TaskLookup

    return TaskLookup(
        task_id=record.task_id,
        tenant_id=record.tenant_id,
        environment_id=record.environment_id,
    )


def make_lookup(task_id: str, context: "RequestContext") -> "TaskLookup":
    from xiaowei_agent.contracts import TaskLookup

    return TaskLookup(
        task_id=task_id,
        tenant_id=context.tenant_id,
        environment_id=context.environment_id,
    )


@pytest.fixture
def clock() -> "ManualClock":
    from tests.fakes.clock import ManualClock

    return ManualClock()


@pytest.fixture
def memory_state() -> "InMemoryPersistenceState":
    from xiaowei_agent.persistence.memory import InMemoryPersistenceState

    return InMemoryPersistenceState()


@pytest.fixture
def store(clock: "ManualClock", memory_state: "InMemoryPersistenceState") -> "InMemoryTaskStore":
    from xiaowei_agent.persistence.fake import InMemoryTaskStore

    return InMemoryTaskStore(clock=clock, state=memory_state)


@pytest.fixture
def plan_store(memory_state: "InMemoryPersistenceState") -> "InMemoryPlanStore":
    from xiaowei_agent.persistence.plans import InMemoryPlanStore

    return InMemoryPlanStore(state=memory_state)


@pytest.fixture
def evidence_ledger(
    memory_state: "InMemoryPersistenceState",
) -> "InMemoryEvidenceLedger":
    from xiaowei_agent.persistence.evidence import InMemoryEvidenceLedger

    return InMemoryEvidenceLedger(state=memory_state)


@pytest.fixture
async def task(store: "InMemoryTaskStore", context: "RequestContext") -> "TaskRecord":
    return await store.create_task(submission=make_submission(context))


def _path_to(target: "TaskStatus") -> tuple["TaskStatus", ...]:
    """在 ALLOWED_TRANSITIONS 上做 BFS，求 CREATED → target 的一条最短合法路径。

    写成搜索而不是硬编码路径，是因为硬编码会在迁移表变化时悄悄失配；搜索失败本身
    也是一条断言——说明该终态从 CREATED 不可达，迁移表有问题。
    """
    from xiaowei_agent.contracts import ALLOWED_TRANSITIONS, TaskStatus

    queue: list[tuple[TaskStatus, tuple[TaskStatus, ...]]] = [(TaskStatus.CREATED, ())]
    seen = {TaskStatus.CREATED}
    while queue:
        current, path = queue.pop(0)
        if current is target:
            return path
        for nxt in sorted(ALLOWED_TRANSITIONS[current], key=lambda s: s.value):
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, (*path, nxt)))
    raise AssertionError(f"{target.value} is unreachable from CREATED")


async def drive_to_terminal(
    store: "TaskStore", lookup: "TaskLookup", terminal: "TaskStatus"
) -> None:
    """把任务沿一条合法路径推到指定终态，全程采纳存储层 winner。"""
    from xiaowei_agent.persistence.store import TransitionCommand

    record = await store.get(lookup=lookup)
    for status in _path_to(terminal):
        result = await store.transition(
            command=TransitionCommand(
                task_id=lookup.task_id,
                expected_version=record.version,
                to_status=status,
            )
        )
        assert result.applied, result.rejection
        record = result.winner
