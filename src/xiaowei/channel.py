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
  目标、渠道与当前数据权限经 ``EvidenceStore.validate_answer`` 重新生成 ``Delivery``（连同保存的
  本轮模型可见证据一起复核；没有该记录的旧格式回答不交付）；历史读取与显式重发
  同样复核视图事实的当前权限、对象类型与被读取列的类型。确定不可交付抛出
  ``ResultUnavailableError``，暂时无法复核抛出其子类
  ``ResultUnverifiableError``（结果与会话保留，之后可再读取或显式重发）。failed/interrupted 只给
  固定回执。发送紧邻在取得投递权之后，只发送一次，结果按 sent/failed/unknown 记录，不自动重试。

入口授权与 Evidence 授权必须是同一个 ``AccessPolicy`` 实例：装配时核对 ``EvidenceStore`` 的授权
函数是绑定在这个对象上的 ``authorize``（来源对象身份，而非 callable 相等），装配后不可替换。
HTTP、飞书 SDK、数据库客户端与凭据都不进入 RunContext。
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from xiaowei.actions import ACTION_SETTLE_TIMEOUT_SECONDS, ActionOutcomeError
from xiaowei.app import Application, Mode, PausedTurn, ResumeRejectedError, TurnError, TurnReason
from xiaowei.channel_store import (
    CallerFailureCode,
    ChannelSession,
    ChannelStore,
    ChannelStoreError,
    FailureCode,
    NotReadyError,
    RequestRecord,
    RequestUnavailableError,
    SendOutcome,
    SessionBusyError,
)
from xiaowei.evidence import (
    AnswerRejectedError,
    EvidenceStore,
    EvidenceStoreError,
    EvidenceUnverifiableError,
)
from xiaowei.governance import ToolExecutionError, ToolRejectedError
from xiaowei.image_input import ImageStoreError, ValidatedImage
from xiaowei.models import (
    Budget,
    Channel,
    Delivery,
    Identity,
    Label,
    Owner,
    RunContext,
    SessionSnapshot,
    ToolId,
    TurnAnswer,
    group_owner,
)
from xiaowei.progress import Progress

logger = logging.getLogger(__name__)

# 应用层失败原因到存储失败码（闭集）的映射；存储只保存类别，渠道据此给固定回执。
_FAILURE_CODES: Mapping[TurnReason, CallerFailureCode] = {
    "session_busy": "busy",
    "busy": "busy",
    "model_failed": "model_failed",
    "turn_limit": "model_failed",
    "timeout": "model_failed",
    "answer_rejected": "evidence_failed",
    "query_unfinished": "evidence_failed",
    "tool_failed": "evidence_failed",
    "session_unavailable": "session_failed",
    "scope_unverifiable": "scope_unverifiable",
    "content_rejected": "session_failed",
    "storage_failed": "session_failed",
    "storage_unavailable": "session_failed",
    "scope_rejected": "session_failed",
    "action_unresumable": "action_unresumable",
}
_ACTION_FAILURE_CODES: Mapping[str, CallerFailureCode] = {
    "pending": "action_not_approvable",
    "executing": "action_unknown",
    "succeeded": "action_feedback_failed",
    "unknown": "action_unknown",
    "rejected": "action_rejected",
}
_RECEIPTS: Mapping[FailureCode, str] = {
    "action_requester_invalid": "发起人已不在群或权限已变化，未执行，请重新提出",
    "action_session_invalid": (
        "原会话不可继续，未执行；暂停前调查未形成最终报告、未入历史，请新建会话重新调查并提出"
    ),
    "action_upgrade_invalidated": "已因升级作废、未执行，请重新提出",
    "action_unresumable": (
        "本轮未形成可恢复审批，动作已作废、未执行；请重新提出并明确请求执行"
        "（含图片的会话不能请求执行，需先发送 /新建 后重新提出）"
    ),
    "action_expired": "候选已过期，未执行；暂停前调查未形成最终报告、未入历史，请重新调查并提出",
    "busy": "系统繁忙，本轮未执行，请稍后用新消息重试",
    "model_failed": "模型未能完成本轮（调用失败、超时或次数达到上限）；已执行的工具不会自动重试",
    "evidence_failed": (
        "本轮未交付：工具结果不可用，或回答的证据引用未通过核对（后者不是数据源故障）；"
        "不会自动重试。可先原样重发；仍失败再缩小查询范围、核对数据源和问题条件"
    ),
    "session_failed": "本轮内容未被接受或会话不可继续，请调整后重试或新建会话",
    "scope_unverifiable": (
        "暂时无法确认数据当前权限（集群不可达或超时），本轮未交付；会话保留，请稍后重新发送"
    ),
    "access_denied": "未能确认你当前的使用权限或群成员身份，本轮未执行",
    "action_not_approvable": (
        "本次批准未执行：动作不存在、已过期或已被处理，材料未有效送达，或你当前无权批准。"
        "请先按原 Action ID 核对状态"
    ),
    "action_feedback_failed": (
        "动作已执行，但反馈未能保存或通过校验。请按本次批准消息中的 Action ID 查询状态，"
        "不要重复批准或重新提出同一动作"
    ),
    "action_unknown": (
        "动作执行结果未知，不会自动重试。请先人工核对远端，"
        "并按本次批准消息中的 Action ID 查询状态；不要再次批准或重新提出同一动作"
    ),
    "action_rejected": (
        "动作未执行：执行前复核或上游校验未通过。"
        "请核对权限、对象状态和动作参数（静默需检查匹配器）后重新提出动作"
    ),
    "result_not_saved": "结果保存失败，请新建会话后重试",
    "interrupted": "上次处理已中断，请重新发送",
}

Transmit = Callable[[Delivery], Awaitable[SendOutcome]]
"""渠道的单次发送：只发送一次，返回明确成功、明确失败或结果未知。"""


class _Trusted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GroupScope(_Trusted):
    """指定群：由渠道适配器从已验证的平台事件与可信配置建立，决定共享会话的群 owner。"""

    app_id: Label
    tenant_key: Label
    chat_id: Label

    @property
    def owner(self) -> Owner:
        return group_owner(self.app_id, self.tenant_key, self.chat_id)


def _group_conversation(channel: Channel, conversation_id: str, group: GroupScope | None) -> None:
    if group is not None and (channel != "feishu" or conversation_id != group.chat_id):
        raise ValueError("群请求只属于飞书渠道，会话语境就是该群")


class InboundRequest(_Trusted):
    """渠道适配器生成的可信信封。身份、会话语境与用途来自适配器；``message`` 仍是不可信内容。

    群请求另带 ``group``：``subject_id`` 是本条消息的发送者（actor），会话归属是群；
    ``channel_request_id`` 是原消息编号，也是回复目的地。
    """

    channel: Channel
    channel_request_id: Label
    subject_id: Label
    conversation_id: Label
    mode: Mode
    message: str = Field(min_length=1)
    received_at: AwareDatetime
    group: GroupScope | None = None

    @model_validator(mode="after")
    def _group_conversation(self) -> "InboundRequest":
        _group_conversation(self.channel, self.conversation_id, self.group)
        return self


class RequestRef(_Trusted):
    """按可信身份定位一条请求：读取、发送与重发都以此为准，不接受会话或轮次标识。

    群请求以群 owner 与原消息编号定位，并且只属于它的发起人。
    """

    channel: Channel
    subject_id: Label
    conversation_id: Label
    request_id: Label
    group: GroupScope | None = None

    @model_validator(mode="after")
    def _group_conversation(self) -> "RequestRef":
        _group_conversation(self.channel, self.conversation_id, self.group)
        return self


class AccessDecision(_Trusted):
    """当前授权：发起人（actor）、会话归属、可用目标集合、允许的工具与渠道数据策略版本。

    省略 ``owner`` 时为发起人本人的个人归属。
    """

    subject_id: Label
    target_ids: frozenset[Label] = Field(min_length=1)
    authorized_tools: frozenset[ToolId]
    policy_version: Label
    owner: Owner

    @model_validator(mode="before")
    @classmethod
    def _personal_by_default(cls, data: object) -> object:
        if isinstance(data, dict) and "owner" not in data and "subject_id" in data:
            return {**data, "owner": Owner(kind="personal", id=data["subject_id"])}
        return data


class AccessPolicy(Protocol):
    """启动时装配的唯一授权来源：``resolve`` / ``resolve_group`` 供入口计算本轮范围，``authorize``
    同时交给 ``EvidenceStore``。解析返回 ``None`` 或抛出任何异常都按拒绝处理。

    ``resolve_group`` 解析指定群内的发起人：``verify_member=False`` 只供接受请求，只核对可信
    事件与指定群配置，不查成员目录、不赋予查询权；开始运行、最终发送、历史读取与重发都以
    ``verify_member=True`` 各查一次当前成员资格，找不到或无法确认都拒绝。群授权不使发起人获得
    私聊或 Web 访问。``authorize`` 复核工具与目标，不再查成员目录：同一轮内沿用本轮已确认的
    成员资格。
    """

    async def resolve(self, channel: Channel, subject_id: str) -> AccessDecision | None: ...

    async def resolve_group(
        self, group: GroupScope, subject_id: str, *, verify_member: bool
    ) -> AccessDecision | None: ...

    async def authorize(self, identity: Identity, target_id: str, tool_id: str) -> bool: ...


class AccessDeniedError(ChannelStoreError):
    def __init__(self) -> None:
        super().__init__("当前身份无权使用小维")


class ResultUnavailableError(ChannelStoreError):
    """已保存的结果按当前身份、权限、目标或证据期限不能交付。"""

    def __init__(self, message: str = "结果当前不可用") -> None:
        super().__init__(message)


class ResultUnverifiableError(ResultUnavailableError):
    """暂时无法确认结果引用的数据当前仍可读（目标不可达、超时等）；本次不交付，结果与会话保留。"""

    def __init__(self) -> None:
        super().__init__("暂时无法确认数据当前权限，结果本次未交付，请稍后重试")


@dataclass(frozen=True)
class RequestReceipt:
    """接受结果：``record`` 为持久记录；只有 ``created`` 的请求会被运行。

    ``message`` 只在本进程内存中交给 ``process``，不持久化。receipt 不携带执行 context：``process``
    开始运行前按当前授权重新解析，并以数据库中的真实请求核对 receipt 的记录与正文，执行身份、
    用途与工具范围都由可信授权、持久记录与配置预算生成。群请求另带 ``group``，须与记录的群
    owner 与回复群一致。
    """

    record: RequestRecord
    created: bool
    message: str
    group: GroupScope | None = None


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
    if (
        not isinstance(decision, AccessDecision)
        or decision.subject_id != subject_id
        or decision.owner.kind != "personal"
        or decision.owner.id != subject_id
    ):
        raise AccessDeniedError
    return decision


async def _decide_group(
    access: AccessPolicy, group: GroupScope, subject_id: str, *, verify_member: bool
) -> AccessDecision:
    """群内发起人的当前授权：结果必须正是这个群、这个发起人，否则按拒绝处理。"""
    try:
        decision = await access.resolve_group(group, subject_id, verify_member=verify_member)
    except Exception:
        raise AccessDeniedError from None
    if (
        not isinstance(decision, AccessDecision)
        or decision.owner != group.owner
        or decision.subject_id != subject_id
    ):
        raise AccessDeniedError
    return decision


async def _decide_ref(access: AccessPolicy, ref: RequestRef) -> AccessDecision:
    if ref.group is None:
        return await _decide(access, ref.channel, ref.subject_id)
    return await _decide_group(access, ref.group, ref.subject_id, verify_member=True)


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

    async def view(self, ref: RequestRef, *, first: bool = False) -> RequestView:
        """按当前身份读取请求；completed 结果重新验证，不可交付时拒绝。

        ``first`` 只由渠道在本请求刚运行完时传入（首次交付）；其余读取都是历史读取。
        """
        record, decision = await self._load(ref)
        if record.state in {"completed", "paused"}:
            return RequestView(
                record.state, await self._validated(record, decision, history=not first)
            )
        if record.state in ("failed", "interrupted"):
            return RequestView(record.state, self._receipt(record))
        return RequestView(record.state, None)

    async def send(
        self, ref: RequestRef, transmit: Transmit, *, resend: bool = False
    ) -> SendOutcome | None:
        """取得投递权后发送一次；返回 None 表示不得发送（已发送、发送中、未结束或已被他人取得）。

        群结果的目的地是接受时保存的群与原消息：``ref`` 与它不一致时不发送。首次发送与事件重投只
        竞争 pending；``resend=True`` 只供显式重发（历史交付），只对 completed
        结果竞争 failed/unknown。completed 结果先按当前权限重新验证：不可交付或暂时无法复核时取得
        投递权并记为 failed（仍可显式重发），不发送旧内容，原样抛出拒绝。发送抛出异常或被取消时
        记为 unknown 并原样传播。落定只针对本次取得的尝试：
        尝试已被启动恢复作废或被更新的尝试取代时不改写状态，报错并锁低 readiness。
        """
        record, decision = await self._load(ref)
        if ref.group is not None and (
            record.reply_chat_id != ref.group.chat_id or record.reply_message_id != ref.request_id
        ):
            # 群结果只能发往接受时保存的群与原消息；调用方的目的地必须与之一致。
            raise RequestUnavailableError
        refused: ResultUnavailableError | None = None
        delivery: Delivery | None = None
        if record.state in {"completed", "paused"}:
            try:
                delivery = await self._validated(record, decision, history=resend)
            except ResultUnavailableError as exc:
                refused = exc
        elif record.state in ("failed", "interrupted") and not resend:
            delivery = self._receipt(record)
        else:
            return None
        claim = await self._store.claim_send(record, resend=resend)
        if claim is None:
            return None
        if delivery is None:
            await self._store.finish_send(claim, "failed")
            raise refused or ResultUnavailableError()
        if record.state in {"completed", "paused"}:
            delivery = delivery.model_copy(
                update={
                    "request_state": record.state,
                    "delivery_attempt": claim.attempt,
                    "delivery_version": record.delivery_version,
                }
            )
        try:
            outcome = await transmit(delivery)
        except BaseException as exc:
            try:
                await self._store.finish_send(claim, "unknown")
            except ChannelStoreError as lost:
                # 这次尝试已被启动恢复作废或被更新的尝试取代（readiness 已锁低）：不改写状态，
                # 原样传播发送本身的异常或取消。
                raise exc from lost
            raise
        return await self._store.finish_send(claim, outcome)

    async def _load(self, ref: RequestRef) -> tuple[RequestRecord, AccessDecision]:
        """先按当前授权解析（群请求在这里查一次当前成员资格），再按同一身份读取请求。"""
        decision = await _decide_ref(self._access, ref)
        record = await self._store.get(
            ref.channel,
            decision.subject_id,
            ref.conversation_id,
            ref.request_id,
            owner=decision.owner,
        )
        return record, decision

    async def _validated(
        self, record: RequestRecord, decision: AccessDecision, *, history: bool
    ) -> Delivery:
        if record.answer is None:  # 数据库约束保证不会发生；不按“有回答”继续
            raise ResultUnavailableError
        if record.context_evidence is None:
            # 此前格式保存的回答没有本轮模型可见证据的记录，无法确认它依赖的数据当前仍可读。
            raise ResultUnavailableError
        turn = TurnAnswer(
            answer=record.answer,
            answer_version=record.answer_version,
            context_evidence=record.context_evidence,
            monitoring_failures=record.monitoring_failures,
            limit_reached=record.limit_reached,
            material_only=record.material_only,
        )
        try:
            delivery = await self._evidence.validate_answer(
                turn, self._context(record, decision), history=history
            )
        except AnswerRejectedError:
            raise ResultUnavailableError from None
        except EvidenceUnverifiableError:
            raise ResultUnverifiableError from None
        facts = []
        content = delivery.content
        for fact in delivery.facts:
            command = fact.metadata.get("approval_command")
            action_id = fact.metadata.get("action_id")
            if not isinstance(command, str):
                facts.append(fact)
                continue
            if not isinstance(action_id, str):
                raise ResultUnavailableError
            try:
                state, failure = await self._store.action_delivery_state(action_id, record.owner.id)
            except ChannelStoreError:
                raise ResultUnverifiableError from None
            if state == "pending":
                facts.append(fact)
                continue
            status = (
                _RECEIPTS["action_unresumable"]
                if failure == "action_unresumable"
                else "候选当前不可批准；请按 Action ID 查询动作状态"
            )
            metadata = {
                key: value for key, value in fact.metadata.items() if key != "approval_command"
            }
            metadata["approval_status"] = status
            raw = json.loads(fact.result_json) if fact.result_json is not None else None
            if isinstance(raw, dict):
                raw.pop("approval_command", None)
                raw["approval_status"] = status
            facts.append(
                fact.model_copy(
                    update={
                        "metadata": metadata,
                        "result_json": (
                            json.dumps(raw, ensure_ascii=False) if raw is not None else None
                        ),
                    }
                )
            )
            original = (
                json.dumps("approval_command") + ": " + json.dumps(command, ensure_ascii=False)
            )
            replacement = (
                json.dumps("approval_status") + ": " + json.dumps(status, ensure_ascii=False)
            )
            content = content.replace(original, replacement).replace(command, status)
        return delivery.model_copy(
            update={
                "facts": tuple(facts),
                "content": content,
                "session_notice": self._session_warning(record.session_state, record.channel),
            }
        )

    @staticmethod
    def _session_warning(snapshot: SessionSnapshot | None, channel: Channel) -> str | None:
        """仅依据本请求保存的成功观察；不读取当前余额或再次回放历史。"""
        if snapshot is None:
            return None
        turns, used, expires = snapshot.committed_turns, snapshot.replay_bytes, snapshot.expires_at
        if snapshot.phase != "after_commit" or turns is None or used is None or expires is None:
            raise ResultUnavailableError
        limits = []
        if turns * 100 >= snapshot.turn_limit * 80:
            limits.append(f"会话已用 {turns}/{snapshot.turn_limit} 次")
        if used * 100 >= snapshot.byte_limit * 80:
            limits.append(f"历史容量接近上限（{used}/{snapshot.byte_limit} 字节）")
        remaining = expires - snapshot.observed_at
        if remaining <= timedelta(0):
            limits.append("会话已到固定保留期限")
        elif remaining <= timedelta(hours=1):
            limits.append("距固定保留期限不超过 1 小时")
        if not limits:
            return None
        last = (
            turns == snapshot.turn_limit or used == snapshot.byte_limit or remaining <= timedelta(0)
        )
        next_turn = "下轮需要新建会话" if last else "后续可能需新建会话"
        action = "点击“新建会话”" if channel == "web" else "发送 /新建"
        return "本轮结束时" + "；".join(limits) + f"。{next_turn}；{action}，并补充关键线索。"

    def _context(self, record: RequestRecord, decision: AccessDecision) -> RunContext:
        """交付用 context：结果所属会话与轮次、当前授权的目标集合；不授予任何工具。"""
        return RunContext(
            identity=Identity(
                subject_id=decision.subject_id,
                session_id=record.session_id,
                turn_id=record.run_turn_id or record.turn_id,
                channel=record.channel,
                owner=record.owner,
            ),
            target_scope=decision.target_ids,
            tool_scope=frozenset(),
            budget=self._budget,
        )

    @staticmethod
    def _receipt(record: RequestRecord) -> Delivery:
        if record.failure_code is None:  # 数据库约束保证不会发生
            raise ResultUnavailableError
        snapshot = record.session_state
        content = _RECEIPTS[record.failure_code]
        notice = None
        if snapshot is not None:
            messages = {
                "history_turn_limit": (
                    "会话已达交互轮数上限。请新建会话继续；之前的上下文不会自动带过去。"
                ),
                "history_bytes_limit": "会话历史已达容量上限。请新建会话，并补充目标和关键线索。",
                "expired": "会话已超过保留期限。请新建会话；原历史不能继续回放。",
                "binding_changed": "模型或数据策略已变化。请新建会话。",
                "unavailable": "会话历史不能安全继续，请新建会话；已执行操作不会自动重试。",
            }
            if snapshot.reason is None:
                raise ResultUnavailableError
            content = messages[snapshot.reason]
            if snapshot.reason == "history_bytes_limit" and snapshot.phase == "before_commit":
                content = (
                    "本轮执行后历史超出容量，回答未保存/交付，会话已封存；"
                    "已执行的工具不会自动重试。"
                )
            observation = [f"本轮结束时（{snapshot.observed_at.isoformat()}）"]
            if snapshot.committed_turns is not None:
                observation.append(f"已提交 {snapshot.committed_turns}/{snapshot.turn_limit} 轮")
            if snapshot.replay_bytes is not None:
                observation.append(f"完整回放 {snapshot.replay_bytes}/{snapshot.byte_limit} 字节")
            action = "点击“新建会话”" if record.channel == "web" else "发送 /新建"
            notice = "；".join(observation) + f"。{action}；新建上下文不等于立即删除保存记录。"
        return Delivery(
            content=content, evidence_ids=(), channel=record.channel, session_notice=notice
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

    @property
    def max_concurrent_turns(self) -> int:
        """应用的全局并发上限；渠道的消费者数不得超过它。"""
        return self._app.max_concurrent_turns

    def admits_input(self, text: str) -> bool:
        """附件准入仍使用当前应用的数据策略；持久化和模型前调用。"""
        return self._app.admits_input(text)

    async def image_has_capacity(self, ref: RequestRef) -> bool:
        """先重验身份，再取服务端 current 会话；满额时不下载图片。"""
        decision = await _decide_ref(self._access, ref)
        images = self._app.images
        if images is None or ref.channel != "feishu":
            raise ImageStoreError("图片能力未启用")
        session = await self._store.current_session(
            ref.channel, decision.subject_id, ref.conversation_id, owner=decision.owner
        )
        return await images.has_capacity(session)

    async def save_image(
        self,
        receipt: RequestReceipt,
        inbound: InboundRequest,
        source_digest: str,
        image: ValidatedImage,
    ) -> None:
        """请求已持久接受后，核对原会话并在排队前原子写入原图。"""
        images = self._app.images
        if images is None or not receipt.created or receipt.record.channel != "feishu":
            raise ImageStoreError("图片请求无法绑定")
        try:
            session = await self._store.current_session(
                inbound.channel,
                inbound.subject_id,
                inbound.conversation_id,
                owner=receipt.record.owner,
            )
            if (
                session.session_id != receipt.record.session_id
                or receipt.message != inbound.message
            ):
                raise ImageStoreError("图片请求会话不匹配")
            await images.save(session, source_digest, image, turn_id=receipt.record.turn_id)
        except asyncio.CancelledError:
            self._store.readiness.lock("image_accept_unsettled")
            raise
        except (ImageStoreError, ChannelStoreError) as exc:
            try:
                await self._store.fail(receipt.record, "session_failed")
            except BaseException:
                self._store.readiness.lock("image_accept_unsettled")
                raise
            raise ImageStoreError("图片请求未能绑定") from exc

    async def authorize_notice(self, ref: RequestRef) -> None:
        """无数据回执发送前复核当前身份及群成员；不创建请求、会话或工具执行范围。"""
        await _decide_ref(self._access, ref)

    async def accept(self, inbound: InboundRequest) -> RequestReceipt:
        """当前授权通过后持久接受请求；同一请求编号返回已有记录，不再运行。

        群请求只核对可信事件与指定群配置（不查成员目录），按群 owner 接受；接受不计算工具范围，
        ``process`` 开始运行前重新授权后再计算。
        """
        group = inbound.group
        if group is None:
            decision = await _decide(self._access, inbound.channel, inbound.subject_id)
        else:
            decision = await _decide_group(
                self._access, group, inbound.subject_id, verify_member=False
            )
        acceptance = await self._store.accept(
            channel=inbound.channel,
            subject_id=decision.subject_id,
            conversation=inbound.conversation_id,
            request_id=inbound.channel_request_id,
            mode=inbound.mode,
            message=inbound.message,
            policy_version=decision.policy_version,
            owner=decision.owner,
        )
        logger.info(
            "channel=%s request=%s turn=%s",
            inbound.channel,
            json.dumps(inbound.channel_request_id, ensure_ascii=True),
            acceptance.record.turn_id,
        )
        return RequestReceipt(acceptance.record, acceptance.created, inbound.message, group)

    async def _authorize_start(
        self, record: RequestRecord, group: GroupScope | None
    ) -> AccessDecision:
        """开始运行前的当前授权；是否为群请求以记录的 owner 为准，群范围须与记录一致。"""
        if record.owner.kind == "personal":
            if group is not None:
                raise AccessDeniedError
            return await _decide(self._access, record.channel, record.subject_id)
        if (
            group is None
            or record.channel != "feishu"
            or group.owner != record.owner
            or record.reply_chat_id != group.chat_id
        ):
            raise AccessDeniedError
        return await _decide_group(self._access, group, record.subject_id, verify_member=True)

    def _context(self, record: RequestRecord, decision: AccessDecision) -> RunContext:
        """本轮 context：数据库中的请求（身份、会话、轮次、用途）、当前授权与配置预算。"""
        scope = self._app.scope_for_turn(
            record.mode, decision.authorized_tools, self._app.available_tools
        )
        return RunContext(
            identity=Identity(
                subject_id=decision.subject_id,
                session_id=record.session_id,
                turn_id=record.turn_id,
                channel=record.channel,
                owner=record.owner,
            ),
            target_scope=decision.target_ids,
            tool_scope=scope,
            budget=self._results.budget,
        )

    async def process(
        self, receipt: RequestReceipt, *, progress: Progress | None = None
    ) -> RequestRecord:
        """运行新接受的请求并保存终态，返回保存后的记录；重复请求直接返回原记录。

        开始运行前按当前授权重新解析（群请求以记录的 owner 判断，并重新确认成员资格），再由
        ``ChannelStore.start`` 以数据库中的真实请求核对记录、用途、会话、回复目的地与正文摘要并
        原子启动；执行 context 只由数据库记录、当前授权与配置预算生成，receipt 不能扩大工具或
        预算。不能确认授权或绑定不一致时记为 failed/access_denied；身份与数据库请求不符时得到
        ``RequestUnavailableError``，不改动任何请求。这些拒绝都发生在 Runner 之前，模型与工具
        均不调用。同一会话已有请求在运行或等待保存时，本请求记为 failed/busy，同样不调用。
        """
        record = receipt.record
        if not receipt.created:
            return record
        try:
            decision = await self._authorize_start(record, receipt.group)
        except AccessDeniedError:
            return await self._store.fail(record, "access_denied")
        session_id = record.session_id
        # 检查与登记之间没有 await：同会话的并发请求不能同时通过。
        if session_id in self._running:
            return await self._store.fail(record, "busy")
        self._running.add(session_id)
        try:
            record = await self._store.start(
                record, message=receipt.message, policy_version=decision.policy_version
            )
            if record.state != "running":
                return record
            if progress is not None:
                progress.start()
            try:
                await self._app.prepare_tools(
                    record.mode,
                    decision.authorized_tools,
                    decision.target_ids,
                    timeout_seconds=self._results.budget.timeout_seconds,
                )
                answer = await self._app.run_turn(
                    self._context(record, decision),
                    receipt.message,
                    **({} if progress is None else {"progress": progress}),
                )
            except TurnError as exc:
                return await self._store.fail(
                    record, _FAILURE_CODES[exc.reason], session_state=exc.session_state
                )
            except asyncio.CancelledError:
                # 运行结果与 Session 状态不明：不在取消中补写，交给重启恢复。
                self._store.readiness.lock("turn_cancelled")
                raise
            if isinstance(answer, PausedTurn):
                if progress is not None:
                    progress.mark("approval")
                return await self._store.save_pause(record, answer)
            saved = await self._store.complete(record, answer)
            if progress is not None:
                progress.mark("saved")
            return saved
        finally:
            # 终态保存失败时 readiness 已锁低：会话保持封闭，等待停止并重启。
            if self._store.readiness.ok:
                self._running.discard(session_id)

    async def reject_busy(self, receipt: RequestReceipt) -> RequestRecord:
        """渠道无法排队时把新接受的请求记为 failed/busy，不运行；重复请求直接返回原记录。"""
        if not receipt.created:
            return receipt.record
        return await self._store.fail(receipt.record, "busy")

    async def approve(
        self,
        receipt: RequestReceipt,
        action_id: str,
        *,
        approve: bool = True,
        progress: Progress | None = None,
    ) -> RequestRecord:
        """确定性批准事件恢复原SDK轮次；入口身份、去重、回执仍属本请求。"""
        from xiaowei.feishu import attributed

        record = receipt.record
        if not receipt.created:
            return record
        if (
            self._app.actions is None
            or receipt.group is None
            or receipt.message
            != attributed(record.subject_id, f"/{'批准' if approve else '拒绝'} {action_id}")
        ):
            return await self._store.fail(record, "access_denied")
        try:
            decision = await self._authorize_start(record, receipt.group)
        except AccessDeniedError:
            return await self._store.fail(record, "access_denied")
        if record.session_id in self._running:
            return await self._store.fail(record, "busy")
        self._running.add(record.session_id)
        try:
            record = await self._store.start(
                record,
                message=receipt.message,
                policy_version=decision.policy_version,
            )
            if record.state != "running":
                return record
            if progress is not None:
                progress.start()
            executed = False

            async def fail_approval(code: CallerFailureCode) -> RequestRecord:
                return await self._store.fail(record, code, settle_action_id=action_id)

            try:
                async with asyncio.timeout(self._results.budget.timeout_seconds):
                    answer = await self._app.resume_action(
                        action_id,
                        self._context(record, decision),
                        approve=approve,
                        **({} if progress is None else {"progress": progress}),
                    )
                    executed = True
                    if not isinstance(answer, PausedTurn):
                        feedback_ctx = self._context(record, decision)
                        if answer.run_turn_id is not None:
                            feedback_ctx = feedback_ctx.model_copy(
                                update={
                                    "identity": feedback_ctx.identity.model_copy(
                                        update={"turn_id": answer.run_turn_id}
                                    )
                                }
                            )
                        await self._results.evidence.validate_answer(answer, feedback_ctx)
            except ResumeRejectedError as exc:
                return await fail_approval(exc.code)
            except TurnError:
                if not self._store.readiness.ok:
                    raise NotReadyError from None
                code = await self._approval_failure(action_id, self._context(record, decision))
                return await fail_approval(code)
            except ActionOutcomeError as exc:
                return await fail_approval(_ACTION_FAILURE_CODES[exc.state])
            except (ToolRejectedError, RequestUnavailableError):
                return await fail_approval("action_not_approvable")
            except (ToolExecutionError, AnswerRejectedError, EvidenceStoreError):
                return await fail_approval(
                    "action_feedback_failed" if executed else "action_not_approvable"
                )
            except TimeoutError:
                # 超时可能发生在写入后的反馈阶段；以同群应用表的事实给回执，不重放动作。
                try:
                    failure_code = await self._approval_failure(
                        action_id, self._context(record, decision)
                    )
                except asyncio.CancelledError:
                    await self._fail_cancelled_approval(
                        record, action_id, self._context(record, decision)
                    )
                    raise
                return await fail_approval(failure_code)
            except asyncio.CancelledError:
                await self._fail_cancelled_approval(
                    record, action_id, self._context(record, decision)
                )
                raise
            except Exception as exc:
                if not self._store.readiness.ok:
                    raise
                logger.warning("SDK审批恢复失败：%s", type(exc).__name__)
                return await fail_approval(
                    await self._approval_failure(action_id, self._context(record, decision))
                )
            if isinstance(answer, PausedTurn):
                return await self._store.save_pause(
                    record,
                    answer,
                    failure_code=await self._approval_failure(
                        action_id, self._context(record, decision)
                    ),
                )
            return await self._store.complete(record, answer, failure_code="action_feedback_failed")
        finally:
            if self._store.readiness.ok:
                self._running.discard(record.session_id)

    async def _approval_failure(self, action_id: str, ctx: RunContext) -> CallerFailureCode:
        try:
            async with asyncio.timeout(ACTION_SETTLE_TIMEOUT_SECONDS):
                action = await self._store.get_action(action_id, ctx.identity)
        except RequestUnavailableError:
            return "action_not_approvable"
        except Exception:
            return "action_unknown"
        return action.failure_code or _ACTION_FAILURE_CODES[action.state]

    async def _fail_cancelled_approval(
        self, record: RequestRecord, action_id: str, ctx: RunContext
    ) -> None:
        if not self._store.readiness.ok:
            return  # 关键写入/实例锁已失效，保持原有重启恢复边界。
        try:
            failure_code = await self._approval_failure(action_id, ctx)
            async with asyncio.timeout(ACTION_SETTLE_TIMEOUT_SECONDS):
                await self._store.fail(record, failure_code, settle_action_id=action_id)
        except BaseException:
            # 请求没有可靠落定，不能仅释放群占用继续接活。保留异常及全局故障保护。
            self._store.readiness.lock("action_settle_failed")
            raise

    async def new_session(
        self,
        channel: Channel,
        subject_id: str,
        conversation_id: str,
        *,
        group: GroupScope | None = None,
    ) -> ChannelSession:
        """新建会话：当前会话有请求在运行或等待保存时拒绝；不复制历史或查询许可。

        群会话轮换整个群：须确认发起人当前是群成员。
        """
        if not self._store.readiness.ok:
            raise NotReadyError
        ref = RequestRef(
            channel=channel,
            subject_id=subject_id,
            conversation_id=conversation_id,
            request_id="new-session",
            group=group,
        )
        decision = await _decide_ref(self._access, ref)
        current = await self._store.current_session(
            channel, decision.subject_id, conversation_id, owner=decision.owner
        )
        if current.session_id in self._running:
            raise SessionBusyError
        return await self._store.new_session(
            channel, decision.subject_id, conversation_id, owner=decision.owner
        )
