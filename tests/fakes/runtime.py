"""Runtime 的测试夹具：装配一整条真实链路。

与 Runner 夹具同一原则：**不 mock 任何治理组件**。被替换的只有 adapter（不触达
任何被管运维目标）与时钟。
"""

import datetime as dt
from collections.abc import Mapping
from typing import Any

from tests.fakes.admission import CONTEXT, POLICY_SNAPSHOT, WRITE_PROFILE
from tests.fakes.clock import ManualClock
from tests.fakes.fixtures import SNAPSHOT as WRITE_SNAPSHOT
from tests.fakes.recordings import EMPTY_WITHOUT_TRAFFIC, GOLDEN, TIMEOUT
from tests.fakes.runner import CountingApprovalGate, CountingGateway, RecordingTaskStore
from tests.fakes.sinks import RecordingTraceSink

from xiaowei_agent.application.runtime import XiaoweiRuntime
from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.capabilities.specs import SLOW_QUERY_SURFACE
from xiaowei_agent.contracts import (
    Channel,
    RenderPayload,
    RequestEnvelope,
    TaskLookup,
    TaskSubmission,
)
from xiaowei_agent.governance.profiles import SLOW_QUERY_READONLY_PROFILE
from xiaowei_agent.persistence.evidence import InMemoryEvidenceLedger
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.plans import InMemoryPlanStore
from xiaowei_agent.runners.deterministic import DeterministicStepRunner
from xiaowei_agent.tools.gateway import DeterministicToolGateway
from xiaowei_agent.tools.starrocks_fake import StarRocksRecordingAdapter

_AS_OF = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)


class _TrackingTaskStore(RecordingTaskStore):
    """额外记录被创建过的 task_id，用于断言幂等只产生一个任务事实。"""

    def __init__(
        self, *, clock: ManualClock, state: InMemoryPersistenceState | None = None
    ) -> None:
        super().__init__(clock=clock, state=state)
        self.created_task_ids: list[str] = []

    async def create_task(self, **kwargs: Any) -> Any:
        record = await super().create_task(**kwargs)
        if record.task_id not in self.created_task_ids:
            self.created_task_ids.append(record.task_id)
        return record


class _ClearingRunner:
    """在 Runner 返回后、渲染前清空 ledger 的包装。

    **只用于反证** Runtime 确实按引用从 ledger 读回：若它读的是 Runner 的内部
    变量，清空后仍会照常渲染出证据。
    """

    def __init__(self, inner: DeterministicStepRunner, ledger: Any) -> None:
        self._inner = inner
        self._ledger = ledger

    async def start(self, grant: Any, **kwargs: Any) -> Any:
        outcome = await self._inner.start(grant, **kwargs)
        self._ledger._entries.pop(grant.task_id, None)
        return outcome

    async def resume(self, grant: Any, **kwargs: Any) -> Any:
        return await self._inner.resume(grant, **kwargs)


class RuntimeHarness:
    """装配一次可运行的 Runtime。"""

    def __init__(
        self,
        recording: Mapping[Any, Any],
        *,
        synthetic_write: bool = False,
        clear_ledger_before_render: bool = False,
    ) -> None:
        self.clock = ManualClock(start=_AS_OF)
        self.state = InMemoryPersistenceState()
        self.store = _TrackingTaskStore(clock=self.clock, state=self.state)
        self.plan_store = InMemoryPlanStore(state=self.state)
        self.ledger = InMemoryEvidenceLedger(state=self.state)
        self.adapter = StarRocksRecordingAdapter(recording)
        self.gateway = CountingGateway(
            DeterministicToolGateway(adapters={"starrocks": self.adapter})
        )
        self.approval_gate = CountingApprovalGate()
        self.sink = RecordingTraceSink()
        self.context = CONTEXT
        self.snapshot = WRITE_SNAPSHOT if synthetic_write else StaticCapabilityRegistry().snapshot()
        runner: Any = DeterministicStepRunner(
            task_store=self.store,
            plan_store=self.plan_store,
            ledger=self.ledger,
            gateway=self.gateway,
            approval_gate=self.approval_gate,
            snapshot=self.snapshot,
            policy_snapshot=POLICY_SNAPSHOT,
            profile=WRITE_PROFILE if synthetic_write else SLOW_QUERY_READONLY_PROFILE,
            surface=SLOW_QUERY_SURFACE,
            clock=self.clock,
            sink=self.sink,
        )
        if synthetic_write:
            runner = _SyntheticWriteRunner(runner)
        if clear_ledger_before_render:
            runner = _ClearingRunner(runner, self.ledger)
        self.runtime = XiaoweiRuntime(
            interpreter=RuleBasedIntentInterpreter(),
            resolver=DeterministicCapabilityResolver(),
            snapshot=StaticCapabilityRegistry().snapshot(),
            task_store=self.store,
            ledger=self.ledger,
            runner=runner,
            sink=self.sink,
            clock=self.clock,
        )
        self._injection: str | None = None
        self.task_id = ""

    async def handle(self, text: str, *, idempotency_key: str = "idem-1") -> RenderPayload:
        submission = self.submission(text, idempotency_key=idempotency_key)
        payload = await self.runtime.handle(
            envelope=submission.envelope,
            context=submission.context,
            as_of=submission.as_of,
        )
        if self.store.created_task_ids:
            self.task_id = self.store.created_task_ids[-1]
        return payload

    def submission(
        self,
        text: str,
        *,
        idempotency_key: str = "idem-1",
        as_of: dt.datetime = _AS_OF,
    ) -> TaskSubmission:
        envelope = RequestEnvelope(
            request_id="r1",
            tenant_id=self.context.tenant_id,
            actor=self.context.actor,
            channel=Channel.CLI,
            text=text,
            idempotency_key=idempotency_key,
            environment_id=self.context.environment_id,
        )
        return TaskSubmission(
            envelope=envelope,
            context=self.context,
            as_of=as_of,
        )

    @property
    def lookup(self) -> TaskLookup:
        return TaskLookup(
            task_id=self.task_id,
            tenant_id=self.context.tenant_id,
            environment_id=self.context.environment_id,
        )

    # --- 故障注入 -----------------------------------------------------------

    @classmethod
    def for_injection(cls, injected: str) -> "RuntimeHarness":
        recordings = {
            "unknown_capability": GOLDEN,
            "window_too_wide": GOLDEN,
            "adapter_timeout": TIMEOUT,
            "empty_audit_window": EMPTY_WITHOUT_TRAFFIC,
        }
        harness = cls(recordings[injected])
        harness._injection = injected
        return harness

    async def run_injected(self) -> None:
        """按注入类型跑一次，吞掉确定性拒绝——本轮只看 trace 的归因。"""
        texts = {
            "unknown_capability": "帮我重启一下集群",
            "window_too_wide": "最近9999分钟有哪些慢查询",
            "adapter_timeout": "最近30分钟有哪些慢查询",
            "empty_audit_window": "最近30分钟有哪些慢查询",
        }
        from xiaowei_agent.application.runtime import RequestRejectedError

        assert self._injection is not None
        try:
            await self.handle(texts[self._injection])
        except RequestRejectedError:
            return


class _SyntheticWriteRunner:
    """把 Runtime 编译出的只读计划替换成合成副作用计划。

    合成写能力**不在生产 registry 里**，因此 Runtime 永远不会自己编译出这样的计划；
    要反证"未审批时暂停且调用次数为 0"，只能在 Runner 边界上注入。
    """

    def __init__(self, inner: DeterministicStepRunner) -> None:
        self._inner = inner

    async def start(self, grant: Any, **kwargs: Any) -> Any:
        from tests.fakes.admission import synthetic_write_plan

        kwargs["plan"] = synthetic_write_plan()
        return await self._inner.start(grant, **kwargs)

    async def resume(self, grant: Any, **kwargs: Any) -> Any:
        return await self._inner.resume(grant, **kwargs)
