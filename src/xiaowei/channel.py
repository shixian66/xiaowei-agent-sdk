"""Web 与飞书共用的渠道业务路径：可信入口、当前授权、请求去重、单会话运行、结果保存与交付。

渠道适配器只负责协议与可信身份，把 ``InboundRequest`` 交给 ``ChannelService``；用途、身份、
会话、去重、运行、保存、重验与新建会话只有这一条路径：

- ``accept``：按 ``AccessPolicy`` 解析当前授权（失败关闭），再持久接受或返回已有请求。本轮的
  Tool Scope 在这里按显式用途与当前授权重新计算，不从上一轮或模型继承。
- ``process``：只运行新接受的请求。进程内会话互斥覆盖 ``Application.run_turn`` 及随后的
  completed/failed 持久化；终态保存失败（readiness 已锁低）时该会话不再开放。运行中被取消时
  结果不明：锁低 readiness 后原样传播，交给重启恢复标为 interrupted。
- ``ResultDelivery``：读取与发送已保存的结果，只依赖请求存储、``EvidenceStore`` 与同一个
  ``AccessPolicy``，不持有 ``Application``、模型绑定或模型凭据。completed 结果每次都按当前身份、
  目标与渠道经 ``EvidenceStore.validate_answer`` 重新生成 ``Delivery``；failed/interrupted 只给
  固定回执。发送紧邻在取得投递权之后，只发送一次，结果按 sent/failed/unknown 记录，不自动重试。

入口授权与 Evidence 授权必须是同一个 ``AccessPolicy`` 实例：装配时核对 ``EvidenceStore`` 的授权
函数是绑定在这个对象上的 ``authorize``（来源对象身份，而非 callable 相等），装配后不可替换。
HTTP、飞书 SDK、数据库客户端与凭据都不进入 RunContext。
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from xiaowei.app import Application, Mode, TurnError, TurnReason
from xiaowei.channel_store import (
    CallerFailureCode,
    ChannelSession,
    ChannelStore,
    ChannelStoreError,
    FailureCode,
    NotReadyError,
    RequestRecord,
    SendOutcome,
    SessionBusyError,
)
from xiaowei.evidence import AnswerRejectedError, EvidenceStore
from xiaowei.models import Budget, Channel, Delivery, Identity, Label, RunContext, ToolId

# 应用层失败原因到存储失败码（闭集）的映射；存储只保存类别，渠道据此给固定回执。
_FAILURE_CODES: Mapping[TurnReason, CallerFailureCode] = {
    "session_busy": "busy",
    "busy": "busy",
    "model_failed": "model_failed",
    "turn_limit": "model_failed",
    "timeout": "model_failed",
    "answer_rejected": "evidence_failed",
    "tool_failed": "evidence_failed",
    "session_unavailable": "session_failed",
    "content_rejected": "session_failed",
    "storage_failed": "session_failed",
    "storage_unavailable": "session_failed",
    "scope_rejected": "session_failed",
}
_RECEIPTS: Mapping[FailureCode, str] = {
    "busy": "系统繁忙，本轮未执行，请稍后用新消息重试",
    "model_failed": "模型未能完成本轮（调用失败、超时或次数达到上限）；已执行的工具不会自动重试",
    "evidence_failed": "工具结果或回答未通过证据校验，本轮未交付；不会自动重试",
    "session_failed": "本轮内容未被接受或会话不可继续，请调整后重试或新建会话",
    "result_not_saved": "结果保存失败，请新建会话后重试",
    "interrupted": "上次处理已中断，请重新发送",
}

Transmit = Callable[[Delivery], Awaitable[SendOutcome]]
"""渠道的单次发送：只发送一次，返回明确成功、明确失败或结果未知。"""


class _Trusted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InboundRequest(_Trusted):
    """渠道适配器生成的可信信封。身份、会话语境与用途来自适配器；``message`` 仍是不可信内容。"""

    channel: Channel
    channel_request_id: Label
    subject_id: Label
    conversation_id: Label
    mode: Mode
    message: str = Field(min_length=1)
    received_at: AwareDatetime


class RequestRef(_Trusted):
    """按可信身份定位一条请求：读取、发送与重发都以此为准，不接受会话或轮次标识。"""

    channel: Channel
    subject_id: Label
    conversation_id: Label
    request_id: Label


class AccessDecision(_Trusted):
    """当前授权：内部 subject、唯一目标、允许的工具与渠道数据策略版本。"""

    subject_id: Label
    target_id: Label
    authorized_tools: frozenset[ToolId]
    policy_version: Label


class AccessPolicy(Protocol):
    """启动时装配的唯一授权来源：``resolve`` 供入口计算本轮范围，``authorize`` 同时交给
    ``EvidenceStore``。``resolve`` 返回 ``None`` 或抛出任何异常都按拒绝处理。"""

    async def resolve(self, channel: Channel, subject_id: str) -> AccessDecision | None: ...

    async def authorize(self, identity: Identity, target_id: str, tool_id: str) -> bool: ...


class AccessDeniedError(ChannelStoreError):
    def __init__(self) -> None:
        super().__init__("当前身份无权使用小维")


class ResultUnavailableError(ChannelStoreError):
    """已保存的结果按当前身份、权限、目标或证据期限不能交付。"""

    def __init__(self) -> None:
        super().__init__("结果当前不可用")


@dataclass(frozen=True)
class RequestReceipt:
    """接受结果：``record`` 为持久记录；只有 ``created`` 的请求会被运行。

    ``message`` 与 ``context`` 只在本进程内存中交给 ``process``，不持久化。
    """

    record: RequestRecord
    created: bool
    message: str
    context: RunContext


@dataclass(frozen=True)
class RequestView:
    """GET 的安全视图：completed 为重新生成的交付，failed/interrupted 为固定回执，其余为 None。"""

    state: str
    delivery: Delivery | None


async def _decide(access: AccessPolicy, channel: Channel, subject_id: str) -> AccessDecision:
    try:
        decision = await access.resolve(channel, subject_id)
    except Exception:
        raise AccessDeniedError from None
    if not isinstance(decision, AccessDecision):
        raise AccessDeniedError
    return decision


def _bound_to(authorize: object, access: AccessPolicy) -> bool:
    """``authorize`` 是绑定在 ``access`` 本身上的 ``authorize`` 方法。

    只比较 callable 不够：另一个对象可以把原策略的绑定方法存为自己的属性，再自行解析身份。
    """
    return getattr(authorize, "__self__", None) is access and authorize == access.authorize


class ResultDelivery:
    """已保存结果的读取、首次发送与显式重发；不运行 Agent，不需要模型。"""

    def __init__(
        self,
        store: ChannelStore,
        evidence: EvidenceStore,
        access: AccessPolicy,
        *,
        budget: Budget,
    ) -> None:
        if not _bound_to(evidence.authorize, access):
            raise ValueError("Evidence 的授权必须来自同一个 AccessPolicy")
        self._store = store
        self._evidence = evidence
        self._access = access
        self._budget = budget

    @property
    def store(self) -> ChannelStore:
        return self._store

    @property
    def evidence(self) -> EvidenceStore:
        return self._evidence

    @property
    def access(self) -> AccessPolicy:
        """装配时核验过的唯一授权来源。"""
        return self._access

    @property
    def budget(self) -> Budget:
        return self._budget

    async def view(self, ref: RequestRef) -> RequestView:
        """按当前身份读取请求；completed 结果重新验证，不可交付时拒绝。"""
        record, decision = await self._load(ref)
        if record.state == "completed":
            return RequestView(record.state, await self._validated(record, decision))
        if record.state in ("failed", "interrupted"):
            return RequestView(record.state, self._receipt(record))
        return RequestView(record.state, None)

    async def send(
        self, ref: RequestRef, transmit: Transmit, *, resend: bool = False
    ) -> SendOutcome | None:
        """取得投递权后发送一次；返回 None 表示不得发送（已发送、发送中、未结束或已被他人取得）。

        首次发送与事件重投只竞争 pending；``resend=True`` 只供显式重发，只对 completed 结果竞争
        failed/unknown。completed 结果先按当前权限重新验证：不可交付时取得投递权并记为 failed，
        不发送旧内容。发送抛出异常或被取消时记为 unknown 并原样传播。
        """
        record, decision = await self._load(ref)
        if record.state == "completed":
            try:
                delivery: Delivery | None = await self._validated(record, decision)
            except ResultUnavailableError:
                delivery = None
        elif record.state in ("failed", "interrupted") and not resend:
            delivery = self._receipt(record)
        else:
            return None
        if not await self._store.claim_send(record, resend=resend):
            return None
        if delivery is None:
            await self._store.finish_send(record, "failed")
            raise ResultUnavailableError
        try:
            outcome = await transmit(delivery)
        except BaseException:
            await self._store.finish_send(record, "unknown")
            raise
        await self._store.finish_send(record, outcome)
        return outcome

    async def _load(self, ref: RequestRef) -> tuple[RequestRecord, AccessDecision]:
        decision = await _decide(self._access, ref.channel, ref.subject_id)
        record = await self._store.get(
            ref.channel, decision.subject_id, ref.conversation_id, ref.request_id
        )
        return record, decision

    async def _validated(self, record: RequestRecord, decision: AccessDecision) -> Delivery:
        if record.answer is None:  # 数据库约束保证不会发生；不按“有回答”继续
            raise ResultUnavailableError
        try:
            return await self._evidence.validate_answer(
                record.answer, self._context(record, decision)
            )
        except AnswerRejectedError:
            raise ResultUnavailableError from None

    def _context(self, record: RequestRecord, decision: AccessDecision) -> RunContext:
        """交付用 context：结果所属会话与轮次、当前授权的目标；不授予任何工具。"""
        return RunContext(
            identity=Identity(
                subject_id=decision.subject_id,
                session_id=record.session_id,
                turn_id=record.turn_id,
                channel=record.channel,
            ),
            target_scope=frozenset({decision.target_id}),
            tool_scope=frozenset(),
            budget=self._budget,
        )

    @staticmethod
    def _receipt(record: RequestRecord) -> Delivery:
        if record.failure_code is None:  # 数据库约束保证不会发生
            raise ResultUnavailableError
        return Delivery(
            content=_RECEIPTS[record.failure_code], evidence_ids=(), channel=record.channel
        )


class ChannelService:
    """两个渠道共用的请求处理；``results`` 提供与运行无关的读取与发送。"""

    def __init__(self, app: Application, results: ResultDelivery) -> None:
        if app.evidence is not results.evidence:
            raise ValueError("应用与交付必须使用同一个 EvidenceStore")
        self._results = results
        self._app = app
        self._store = results.store
        self._access = results.access
        self._running: set[str] = set()

    @property
    def results(self) -> ResultDelivery:
        return self._results

    async def accept(self, inbound: InboundRequest) -> RequestReceipt:
        """当前授权通过后持久接受请求；同一请求编号返回已有记录，不再运行。"""
        decision = await _decide(self._access, inbound.channel, inbound.subject_id)
        acceptance = await self._store.accept(
            channel=inbound.channel,
            subject_id=decision.subject_id,
            conversation=inbound.conversation_id,
            request_id=inbound.channel_request_id,
            mode=inbound.mode,
            message=inbound.message,
            policy_version=decision.policy_version,
        )
        record = acceptance.record
        scope = self._app.scope_for_turn(
            inbound.mode, decision.authorized_tools, self._app.available_tools
        )
        context = RunContext(
            identity=Identity(
                subject_id=decision.subject_id,
                session_id=record.session_id,
                turn_id=record.turn_id,
                channel=inbound.channel,
            ),
            target_scope=frozenset({decision.target_id}),
            tool_scope=scope,
            budget=self._results.budget,
        )
        return RequestReceipt(record, acceptance.created, inbound.message, context)

    async def process(self, receipt: RequestReceipt) -> RequestRecord:
        """运行新接受的请求并保存终态，返回保存后的记录；重复请求直接返回原记录。

        同一会话已有请求在运行或等待保存时，本请求记为 failed/busy，模型与工具均不调用。
        """
        record = receipt.record
        if not receipt.created:
            return record
        session_id = record.session_id
        # 检查与登记之间没有 await：同会话的并发请求不能同时通过。
        if session_id in self._running:
            return await self._store.fail(record, "busy")
        self._running.add(session_id)
        try:
            record = await self._store.start(record)
            try:
                answer = await self._app.run_turn(receipt.context, receipt.message)
            except TurnError as exc:
                return await self._store.fail(record, _FAILURE_CODES[exc.reason])
            except asyncio.CancelledError:
                # 运行结果与 Session 状态不明：不在取消中补写，交给重启恢复。
                self._store.readiness.lock("turn_cancelled")
                raise
            return await self._store.complete(record, answer)
        finally:
            # 终态保存失败时 readiness 已锁低：会话保持封闭，等待停止并重启。
            if self._store.readiness.ok:
                self._running.discard(session_id)

    async def new_session(
        self, channel: Channel, subject_id: str, conversation_id: str
    ) -> ChannelSession:
        """新建会话：当前会话有请求在运行或等待保存时拒绝；不复制历史或查询许可。"""
        if not self._store.readiness.ok:
            raise NotReadyError
        decision = await _decide(self._access, channel, subject_id)
        current = await self._store.current_session(channel, decision.subject_id, conversation_id)
        if current.session_id in self._running:
            raise SessionBusyError
        return await self._store.new_session(channel, decision.subject_id, conversation_id)
