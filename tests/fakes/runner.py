"""Runner 的测试夹具：装配一整条真实链路，只把 adapter 换成 recording。

夹具刻意**不 mock 任何治理组件**：admit_step、SQLGuard、ApprovalGate、Gateway 全部
是生产实现。被替换的只有 adapter（不触达任何被管运维目标）与时钟。
"""

import datetime as dt
from collections.abc import Mapping
from typing import Any

from tests.fakes.admission import (
    CONTEXT,
    PARAMS,
    POLICY_SNAPSHOT,
    TASK_ID,
    WRITE_PROFILE,
    synthetic_write_plan,
)
from tests.fakes.clock import ManualClock
from tests.fakes.fixtures import SNAPSHOT as WRITE_SNAPSHOT
from tests.fakes.sinks import RecordingTraceSink

from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.specs import SLOW_QUERY_SURFACE
from xiaowei_agent.capabilities.target import resolve_target
from xiaowei_agent.contracts import (
    AdmissionCertificate,
    AttemptIntent,
    Channel,
    EffectClass,
    RequestContext,
    RequestEnvelope,
    TaskLookup,
    TaskStatus,
    TaskSubmission,
    ToolCall,
    ToolResult,
)
from xiaowei_agent.governance.approval import NeverGrantingApprovalGate
from xiaowei_agent.governance.profiles import SLOW_QUERY_READONLY_PROFILE
from xiaowei_agent.persistence.evidence import InMemoryEvidenceLedger
from xiaowei_agent.persistence.fake import InMemoryTaskStore
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.plans import InMemoryPlanStore
from xiaowei_agent.persistence.store import (
    TaskAttemptCommand,
    TaskAttemptGrant,
    TransitionCommand,
)
from xiaowei_agent.planning.starrocks.compiler import PLAN_BUDGET
from xiaowei_agent.runners.deterministic import DeterministicStepRunner, LifecycleError
from xiaowei_agent.tools.gateway import DeterministicToolGateway
from xiaowei_agent.tools.starrocks_fake import StarRocksRecordingAdapter

_DRAFT_INTENT = "starrocks.slow_query.diagnose"


class CountingGateway:
    """真实 Gateway 的**计数包装**，不改变任何判定。

    只记录调用次数与结果，用于断言"Gateway 调用次数为 0"。它不构造 ToolResult，
    也不放行任何调用——全部委托给真实实现。
    """

    def __init__(self, inner: DeterministicToolGateway) -> None:
        self._inner = inner
        self.invocations = 0
        self.side_effect_invocations = 0
        self.results: list[ToolResult] = []

    async def invoke(
        self,
        call: ToolCall,
        *,
        context: RequestContext,
        admission: AdmissionCertificate,
    ) -> ToolResult:
        self.invocations += 1
        if admission.effect_class is not EffectClass.READ:
            self.side_effect_invocations += 1
        result = await self._inner.invoke(call, context=context, admission=admission)
        self.results.append(result)
        return result


class CountingApprovalGate:
    def __init__(self) -> None:
        self.calls = 0
        self._inner = NeverGrantingApprovalGate()

    def require(self, **kwargs: Any) -> str:
        self.calls += 1
        return self._inner.require(**kwargs)


class RecordingTaskStore(InMemoryTaskStore):
    """记录每次 transition 的入参，用于断言 CAS 与 fencing 的携带情况。"""

    def __init__(
        self, *, clock: ManualClock, state: InMemoryPersistenceState | None = None
    ) -> None:
        super().__init__(clock=clock, state=state)
        self.transitions: list[TransitionCommand] = []
        self.approvals: list[Any] = []
        self.renewals: list[tuple[str, str, int, int]] = []

    async def transition(self, *, command: TransitionCommand) -> Any:
        self.transitions.append(command)
        return await super().transition(command=command)

    async def record_approval(self, *, request: Any) -> int:
        self.approvals.append(request)
        return await super().record_approval(request=request)

    async def renew_lease(
        self, *, task_id: str, owner: str, fencing_token: int, ttl_seconds: int
    ) -> Any:
        self.renewals.append((task_id, owner, fencing_token, ttl_seconds))
        return await super().renew_lease(
            task_id=task_id,
            owner=owner,
            fencing_token=fencing_token,
            ttl_seconds=ttl_seconds,
        )


class BlindEvidenceLedger(InMemoryEvidenceLedger):
    """``load`` 永远返回空的变异实现。

    用于反证"条件求值确实读 ledger"：s1 明明取到了行，但读回来是空，因此
    ``EVIDENCE_ROW_COUNT_BELOW`` 成立，s2 必须**被执行**。若 Runner 用的是本地
    变量，s2 会被跳过。
    """

    async def load(self, *, task_id: str) -> tuple[Any, ...]:
        return ()


class RunnerHarness:
    """装配一次可运行的 Runner。"""

    def __init__(
        self,
        recording: Mapping[Any, Any],
        *,
        synthetic_write: bool = False,
        blind_ledger: bool = False,
        max_tool_calls: int | None = None,
        database: str | None = None,
        tamper_sql: bool = False,
    ) -> None:
        self.task_id = TASK_ID
        self.clock = ManualClock(start=dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC))
        self.state = InMemoryPersistenceState()
        self.store = RecordingTaskStore(clock=self.clock, state=self.state)
        self.plan_store = InMemoryPlanStore(state=self.state)
        self.ledger = (
            BlindEvidenceLedger(state=self.state)
            if blind_ledger
            else InMemoryEvidenceLedger(state=self.state)
        )
        self.adapter = StarRocksRecordingAdapter(recording)
        self.gateway = CountingGateway(
            DeterministicToolGateway(adapters={"starrocks": self.adapter})
        )
        self.approval_gate = CountingApprovalGate()
        self.context = CONTEXT
        self.target = resolve_target(context=CONTEXT, draft=_draft())
        self.recomputed_fingerprints = 0
        self.sink = RecordingTraceSink()

        if synthetic_write:
            self.plan = synthetic_write_plan()
            snapshot = WRITE_SNAPSHOT
            profile = WRITE_PROFILE
        else:
            params = (
                PARAMS if database is None else PARAMS.model_copy(update={"database": database})
            )
            self.plan = _slow_query_plan_with(params)
            snapshot = StaticCapabilityRegistry().snapshot()
            profile = SLOW_QUERY_READONLY_PROFILE
        if tamper_sql:
            # 模拟"计划在存储里被篡改"：SQL 与参数不再自洽，准入必须拒绝。
            step = self.plan.steps[0]
            tampered = dict(step.typed_arguments)
            tampered["sql"] = str(tampered["sql"]).replace("LIMIT 20", "LIMIT 200")
            self.plan = self.plan.model_copy(
                update={
                    "steps": (
                        step.model_copy(update={"typed_arguments": tampered}),
                        *self.plan.steps[1:],
                    )
                }
            )
        if max_tool_calls is not None:
            self.plan = self.plan.model_copy(
                update={"budget": PLAN_BUDGET.model_copy(update={"max_tool_calls": max_tool_calls})}
            )
        self.runner = DeterministicStepRunner(
            task_store=self.store,
            plan_store=self.plan_store,
            ledger=self.ledger,
            gateway=self.gateway,
            approval_gate=self.approval_gate,
            snapshot=snapshot,
            policy_snapshot=POLICY_SNAPSHOT,
            profile=profile,
            surface=SLOW_QUERY_SURFACE,
            clock=self.clock,
            sink=self.sink,
        )
        self._created = False
        self.grant: TaskAttemptGrant | None = None

    async def ensure_task(self) -> None:
        if self._created:
            return
        record = await self.store.create_task(
            submission=TaskSubmission(
                envelope=RequestEnvelope(
                    request_id="r1",
                    tenant_id=self.context.tenant_id,
                    actor=self.context.actor,
                    channel=Channel.CLI,
                    text="慢查询",
                    idempotency_key="idem-1",
                    environment_id=self.context.environment_id,
                ),
                context=self.context,
                as_of=self.clock(),
            )
        )
        # task_id 由存储层生成，不由调用方指定。
        self.task_id = record.task_id
        self._created = True

    async def start(self) -> Any:
        await self.ensure_task()
        attempt = await self.store.begin_task_attempt(
            command=TaskAttemptCommand(
                task_id=self.task_id,
                intent=AttemptIntent.DISPATCH,
                owner="worker-1",
                ttl_seconds=60,
                trace_id=self.context.trace_id,
            )
        )
        if attempt.grant is None:
            raise LifecycleError("task attempt was not granted", rejection=attempt.rejection)
        self.grant = attempt.grant
        return await self.runner.start(
            attempt.grant, plan=self.plan, target=self.target, context=self.context
        )

    async def resume(self, external_input: Any = None, *, approval: Any = None) -> Any:
        self.recomputed_fingerprints += 1
        self.clock.advance(seconds=61)
        attempt = await self.store.begin_task_attempt(
            command=TaskAttemptCommand(
                task_id=self.task_id,
                intent=AttemptIntent.APPROVAL_RESUME,
                owner="worker-1",
                ttl_seconds=60,
                trace_id=self.context.trace_id,
            )
        )
        if attempt.grant is None:
            raise LifecycleError("task attempt was not granted", rejection=attempt.rejection)
        self.grant = attempt.grant
        return await self.runner.resume(
            attempt.grant,
            external_input,
            plan=self.plan,
            context=self.context,
            target=self.target,
            approval=approval,
        )

    async def drive_to(self, status: TaskStatus) -> None:
        from tests.conftest import drive_to_terminal

        await self.ensure_task()
        await drive_to_terminal(self.store, self.lookup, status)

    @property
    def lookup(self) -> TaskLookup:
        return TaskLookup(
            task_id=self.task_id,
            tenant_id=self.context.tenant_id,
            environment_id=self.context.environment_id,
        )

    def reset_call_counters(self) -> None:
        self.adapter.call_count = 0
        self.adapter.calls.clear()
        self.gateway.invocations = 0
        self.gateway.side_effect_invocations = 0

    def introduce_drift(self, kind: str) -> None:
        """在恢复前制造一次漂移。"""
        if kind == "target":
            self.target = self.target.model_copy(update={"environment_id": "test"})
        elif kind == "policy_revision":
            self.context = self.context.model_copy(update={"policy_revision": "policy-2026-10-01"})
        elif kind == "plan":
            # 当前编译结果变化；存储中仍保留最初获准执行的计划。
            self.plan = self.plan.model_copy(
                update={
                    "budget": self.plan.budget.model_copy(
                        update={"max_tool_calls": self.plan.budget.max_tool_calls + 1}
                    )
                }
            )
        else:  # pragma: no cover - 参数化只用上面三种
            raise AssertionError(kind)


def _draft() -> Any:
    from xiaowei_agent.contracts import IntentDraft, IntentSource

    return IntentDraft(
        intent=_DRAFT_INTENT,
        slots={},
        missing=(),
        confidence=0.9,
        source=IntentSource.USER,
    )


def _slow_query_plan_with(params: Any) -> Any:
    from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
    from xiaowei_agent.capabilities.specs import OP_LIST
    from xiaowei_agent.planning.starrocks.compiler import compile_plan

    snapshot = StaticCapabilityRegistry().snapshot()
    candidate = next(
        item
        for item in DeterministicCapabilityResolver()
        .resolve(draft=_draft(), context=CONTEXT, snapshot=snapshot)
        .items
        if item.operation == OP_LIST
    )
    return compile_plan(
        candidate=candidate,
        params=params,
        target=resolve_target(context=CONTEXT, draft=_draft()),
        context=CONTEXT,
        snapshot=snapshot,
        surface=SLOW_QUERY_SURFACE,
    )


__all__ = ["CountingApprovalGate", "CountingGateway", "RunnerHarness"]
