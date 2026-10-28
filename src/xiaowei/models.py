"""可信 context、工具请求/结果、Evidence 与结构化回答类型。

全部为不可变、禁止额外字段的 Pydantic 模型：RunContext 只能装声明的标识与范围，不能借
额外字段或字段类型放入客户端、连接或凭据；模型可提交的只有 ``AgentAnswer``。
"""

import json
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Channel = Literal["web", "feishu"]
Audience = Literal["model", "session", "web", "feishu"]
AUDIENCES: tuple[Audience, ...] = ("model", "session", "web", "feishu")

# 只枚举已核约的只读语义；失败续查不能凭命名前缀开放未来工具。
PROMETHEUS_QUERY_POLICIES = frozenset({"prometheus.query", "prometheus.range_query"})
PROMETHEUS_SELECTOR_POLICIES = frozenset(
    {"prometheus.label_names", "prometheus.label_values", "prometheus.series"}
)
"""按 matches 选择器读取的发现工具：上游 400 bad_data 可交模型有界修正。"""
PROMETHEUS_DISCOVERY_POLICIES = frozenset(
    {
        "prometheus.label_names",
        "prometheus.label_values",
        "prometheus.series",
        "prometheus.metric_metadata",
        "prometheus.list_rules",
    }
)
PROMETHEUS_READ_POLICIES = PROMETHEUS_QUERY_POLICIES | PROMETHEUS_DISCOVERY_POLICIES
GRAFANA_READ_POLICIES = frozenset(
    {
        "grafana.search_dashboards",
        "grafana.get_dashboard_summary",
        "grafana.get_dashboard_panel_queries",
        "grafana.list_datasources",
        "grafana.get_annotations",
    }
)
ALERTMANAGER_READ_POLICIES = frozenset(
    {
        "alertmanager.get_alerts",
        "alertmanager.get_alert_groups",
        "alertmanager.get_silences",
        "alertmanager.get_silence",
        "alertmanager.get_receivers",
        "alertmanager.get_status",
    }
)
ACTION_STATUS_POLICY = "monitoring.action_status"
"""动作状态查询：结果进入动作区域，不进“其余结果”；不因此获得完整审批材料保护。"""
DS_CONTROL_TARGET = "dolphinscheduler"
DS_SELECT_TOOL = "local/ds_select_clusters"
DS_METADATA_TOOLS = frozenset({
    "local/ds_list_projects", "local/ds_list_workflow_instances",
    "local/ds_get_workflow_instance", "local/ds_list_task_instances",
})
DS_READ_TOOLS = DS_METADATA_TOOLS | {"local/ds_read_task_log", "local/ds_search_task_log"}


MONITORING_READ_POLICIES = (
    PROMETHEUS_READ_POLICIES | GRAFANA_READ_POLICIES | ALERTMANAGER_READ_POLICIES
)

Label = Annotated[str, StringConstraints(min_length=1, max_length=200)]
JsonScalar = None | bool | int | float | str
ToolId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,64}/[a-z0-9_.-]{1,64}$")]
ClusterId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")]
"""配置中的稳定集群 ID：模型以它作为工具参数 ``cluster`` 选择目标。"""


class _Trusted(BaseModel):
    """由可信代码构造：禁止额外字段、构造后不可修改。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


OwnerKind = Literal["personal", "group"]


class Owner(_Trusted):
    """会话、历史与证据的共享归属：个人（``id`` 为内部 subject）或指定群（``id`` 由应用、租户与
    群标识组成）。与本轮发起人分开：群内不同成员共享同一 owner，各自仍是独立的 actor。"""

    kind: OwnerKind
    id: Label


def group_owner(app_id: str, tenant_key: str, chat_id: str) -> Owner:
    """指定群的规范 owner：``[app_id, tenant_key, chat_id]`` 的紧凑 JSON（已落库的归属键，不可改动
    编码）。编码超过 ``Owner.id`` 上限时抛出 ``ValidationError``；配置加载时即据此拒绝。"""
    return Owner(kind="group", id=json.dumps([app_id, tenant_key, chat_id], separators=(",", ":")))


class Identity(_Trusted):
    """一轮的可信身份：``subject_id`` 是本轮发起人（actor，授权按它复核），``owner`` 是会话与证据的
    归属。个人会话的 owner 就是发起人本人；省略 ``owner`` 时按个人归属补齐。"""

    subject_id: Label
    session_id: Label
    turn_id: Label
    channel: Channel
    owner: Owner

    @model_validator(mode="before")
    @classmethod
    def _personal_by_default(cls, data: object) -> object:
        if isinstance(data, dict) and "owner" not in data and "subject_id" in data:
            return {**data, "owner": Owner(kind="personal", id=data["subject_id"])}
        return data


class Budget(_Trusted):
    """一轮的上限。``max_scope_checks`` 是证据依赖的对象检查次数：一轮（或一次单独的历史读取、
    重发）内每次复核按批去重后的对象数累计，超出时在探测前按暂不可验证拒绝（见 ``evidence``）。"""

    max_turns: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    max_scope_checks: int = Field(gt=0, le=100_000)


class ApprovedAction(_Trusted):
    """应用占用执行机会后产生的绑定元数据；模型不能提供。"""

    action_id: Label
    attempt: Label
    tool_id: ToolId
    target_id: Label
    arguments_digest: Label
    approver: Identity | None = None
    sdk_call_id: Label | None = None


class RunContext(_Trusted):
    """SDK 本地 context：可信身份、目标与工具范围、预算及 Evidence 标识，不含任何依赖。"""

    identity: Identity
    target_scope: frozenset[Label]
    tool_scope: frozenset[ToolId]
    budget: Budget
    evidence_ids: tuple[Label, ...] = ()
    approved_action: ApprovedAction | None = None
    automatic: bool = False
    definition_job_id: Label | None = None
    content_expires_at: AwareDatetime | None = None


class ToolContract(_Trusted):
    """启动时获准的工具契约；参数与结果策略由 ``policy_id`` 对应的 Python 代码提供。"""

    tool_id: ToolId
    target_id: Label
    input_schema: dict[str, object]
    policy_id: Label
    # 交给模型的工具说明，来自可信登记；MCP 远端的说明文字不交给模型。
    description: Annotated[str, StringConstraints(max_length=1000)] = ""


class ToolRequest(_Trusted):
    """一次工具调用；``call_id`` 与 ``tool_name`` 取自 SDK 工具上下文，不来自模型参数。"""

    tool_id: ToolId
    target_id: Label
    call_id: Label
    tool_name: Label
    arguments: dict[str, object]


class ToolCall(_Trusted):
    """会话历史中的一次工具调用：SDK 函数名、调用标识与参数，用于核对证据来源。"""

    call_id: Label
    tool_name: Label
    arguments: dict[str, object]


ObjectName = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class ObjectDependency(_Trusted):
    """一条证据依赖的数据库对象；只由受治理工具的可信代码生成，不接受模型自报。

    ``use`` 为 ``listed`` 时事实只说明对象存在且当前可读（列表、审计源），复核只证明当前 SELECT
    权限；为 ``read`` 时事实来自对象的数据、计划或结构，另须当前对象类型、表 ID 与 ``columns``
    （被读取或判断的列及其类型）一致。``read`` 依赖的 ``replayable`` 记录对象是否为基表，
    ``listed`` 依赖固定为真；该字段兼容已保存的依赖，目前不限制历史读取。视图的表 ID 为 0，
    无法据此发现定义或间接依赖变化。
    """

    database: ObjectName
    name: ObjectName
    use: Literal["listed", "read"]
    type: str | None
    table_id: int | None
    columns: tuple[tuple[str, str], ...]
    replayable: bool

    @model_validator(mode="after")
    def _listed_has_no_version(self) -> "ObjectDependency":
        if self.use == "listed" and (self.type is not None or self.table_id is not None):
            raise ValueError("listed 依赖只证明当前权限，不携带对象版本")
        if self.use == "listed" and (self.columns or not self.replayable):
            raise ValueError("listed 依赖不携带列，也不限制回放")
        return self


class ToolObservation(_Trusted):
    """Adapter 已限量读取的临时原始结果；只在内存中交给 Evidence 投影，不直接序列化给模型。

    ``dependencies`` 由声明了数据范围的工具给出（可以为空元组）；其他工具为 ``None``。
    """

    payload: dict[str, object]
    captured_at: AwareDatetime
    truncated: bool
    dependencies: tuple[ObjectDependency, ...] | None = None
    content_expires_at: AwareDatetime | None = None
    """可信派生内容的最早截止；只能缩短 Evidence 保留期，不能续期。"""


class EvidenceRecord(_Trusted):
    """代码生成的证据记录：归属、来源、时间与各用途获准内容，不保留原始结果。"""

    evidence_id: Label
    identity: Identity
    target_id: Label
    tool_id: ToolId
    call_id: Label
    tool_name: Label
    arguments_digest: Label
    policy_id: Label
    policy_fingerprint: Label
    captured_at: datetime
    expires_at: datetime
    truncated: bool
    projections: dict[Audience, str]
    dependencies: tuple[ObjectDependency, ...] | None = None


class ToolResult(_Trusted):
    """交给 SDK 的模型可见内容。"""

    evidence_id: Label
    model_content: str
    truncated: bool


class _Answer(BaseModel):
    """模型提交的最终结构：禁止额外字段，因此不能夹带“已核实数值”等事实字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


ANSWER_VERSION: Final = 4
"""可信保存版本：4 选择所需事实，3/2 保留旧补全；模型不输出，缺失只表示 LegacyAnswer。"""
MAX_ANSWER_TEXT_BYTES: Final = 16_000
"""模型文字累计上限（UTF-8 字节）；只约束模型输出规模，不保证卡片、报告或 Session 放得下。"""
MAX_ANSWER_BLOCKS: Final = 64


class TextBlock(_Answer):
    """模型自由文字：结论、解释、建议、追问或闲聊；不获得系统已核实事实的身份。"""

    type: Literal["text"]
    text: str


class FactBlock(_Answer):
    """模型只选择一条可见证据与展示方式；事实内容由代码从获准投影生成。"""

    type: Literal["fact"]
    evidence_id: str
    view: Literal["full", "brief"]


AnswerBlock = TextBlock | FactBlock


class AgentAnswer(_Answer):
    """SDK ``output_type``：按顺序排列的文字与事实块，外加文件格式与“等待用户”声明。

    回答结构、顺序与语气由模型决定；可信事实、动作材料与系统说明由代码生成；旧 v2/v3
    另补全未引用结果（``EvidenceStore.validate_answer``）。块的字段组合由类型约束；文字累计
    字节与块数在这里校验，超限属于可改写的格式错误；这一预算只约束新模型输出，旧记录经
    ``legacy_blocks`` 适配，不受此限。``awaiting_user`` 为真时忽略 ``report_format``，只交付
    普通消息。
    """

    blocks: tuple[AnswerBlock, ...]
    report_format: Literal["md", "html"] | None
    """飞书用户明确要结果或解释/建议文件时选定格式；``awaiting_user`` 为真时忽略。"""
    awaiting_user: bool
    """本条在等待用户补充信息：只决定载体与超容量行为，不跳过验证、补全或动作置顶。"""

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        """模型显式放进正文的证据，按块顺序（不含交付时补全的动作与其余结果）。"""
        return tuple(b.evidence_id for b in self.blocks if isinstance(b, FactBlock))

    @property
    def text(self) -> str:
        """全部模型文字，按块顺序以空行连接。"""
        return "\n\n".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    @model_validator(mode="after")
    def _bounded(self) -> "AgentAnswer":
        if len(self.blocks) > MAX_ANSWER_BLOCKS:
            raise ValueError(f"回答块数超出上限 {MAX_ANSWER_BLOCKS}")
        size = sum(len(b.text.encode()) for b in self.blocks if isinstance(b, TextBlock))
        if size > MAX_ANSWER_TEXT_BYTES:
            raise ValueError(f"回答文字累计超出 {MAX_ANSWER_TEXT_BYTES} 字节")
        return self


class AnswerInference(_Answer):
    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: tuple[str, ...]


class AnswerFactView(_Answer):
    """只选择已有引用的显示方式，不接受模型提供的摘要数值。"""

    evidence_id: str
    view: Literal["full", "brief"]


class LegacyAnswer(_Answer):
    """S3 之前保存的回答格式，只为严格解析历史记录；新轮次不再生成。

    交付时由 ``legacy_blocks`` 转为当前块结构，再走同一验证与投影。``advice`` 的默认值只为
    解析更早、没有该字段的回答（它们没有上下文证据记录，不再交付，见 ``ChannelStore``）。
    """

    evidence_ids: tuple[str, ...]
    fact_views: tuple[AnswerFactView, ...] = ()
    inferences: list[AnswerInference]
    clarification: Annotated[str, Field(min_length=1, max_length=2000)] | None
    advice: Annotated[str, Field(min_length=1, max_length=4000)] | None = None
    report_format: Literal["md", "html"] | None = None


def legacy_blocks(answer: LegacyAnswer) -> AgentAnswer:
    """旧回答按原语义转为块：澄清即等待用户；引用的事实在前、分析文字在后。

    这是已保存记录的投影适配，不是新的模型输出：旧回答已按旧版结构严格解析，每个块也逐一
    校验，但不再套用 ``AgentAnswer`` 的新输出预算（文字字节与块数），否则旧版合法的长分析
    或多段分析会在历史读取时失效。证据、权限与各渠道容量仍由交付链路照常检查。
    """
    if answer.clarification is not None:
        return AgentAnswer.model_construct(
            blocks=(TextBlock(type="text", text=answer.clarification),),
            report_format=None,
            awaiting_user=True,
        )
    views = {item.evidence_id: item.view for item in answer.fact_views}
    blocks: list[AnswerBlock] = [
        FactBlock(type="fact", evidence_id=e, view=views.get(e, "full"))
        for e in answer.evidence_ids
    ]
    blocks.extend(TextBlock(type="text", text=i.text) for i in answer.inferences)
    if answer.advice is not None:
        blocks.append(TextBlock(type="text", text=answer.advice))
    return AgentAnswer.model_construct(
        blocks=tuple(blocks), report_format=answer.report_format, awaiting_user=False
    )


MonitoringFailureReason = Literal[
    "auth",
    "timeout",
    "unavailable",
    "upstream_5xx",
    "invalid_expression",
    "invalid_selector",
    "response_limit",
    "invalid_matcher",
    "silence_missing",
    "object_missing",
    "unclassified",
    "ds_sample_expired",
    "ds_auth", "ds_connect", "ds_timeout", "ds_unavailable", "ds_object_missing",
    "ds_response_limit", "ds_invalid_response",
]


class MonitoringFailure(_Trusted):
    """可信执行链记录的监控读取失败；不来自模型或远端正文。"""

    tool_id: ToolId
    target_id: Label
    reason: MonitoringFailureReason
    object_ref: str | None = Field(default=None, max_length=128, exclude_if=lambda v: v is None)


SessionRejectReason = Literal[
    "history_turn_limit", "history_bytes_limit", "expired", "binding_changed", "unavailable"
]
SESSION_STATE_MAX_BYTES = 1024


class SessionSnapshot(_Trusted):
    """可信执行链在已有检查处取得的状态；不含历史内容，不交给模型。

    提交前的 replay_bytes 包含待提交轮次，committed_turns 始终只计成功提交的轮次。
    未完成完整回放或无法确认归属时相应计数留空。
    """

    observed_at: AwareDatetime
    phase: Literal["before_start", "before_commit", "after_commit"]
    committed_turns: int | None = Field(ge=0, strict=True)
    turn_limit: int = Field(gt=0, strict=True)
    replay_bytes: int | None = Field(ge=0, strict=True)
    byte_limit: int = Field(gt=0, strict=True)
    expires_at: AwareDatetime | None
    reason: SessionRejectReason | None = None

    @model_validator(mode="after")
    def _bounded(self) -> "SessionSnapshot":
        if self.phase == "after_commit" and (
            self.reason is not None
            or self.committed_turns is None
            or self.replay_bytes is None
            or self.expires_at is None
            or self.committed_turns > self.turn_limit
            or self.replay_bytes > self.byte_limit
        ):
            raise ValueError("提交后的会话状态不完整或超出上限")
        if self.reason == "history_turn_limit" and (
            self.committed_turns is None or self.committed_turns < self.turn_limit
        ):
            raise ValueError("交互轮数上限与会话状态不一致")
        if self.reason == "history_bytes_limit" and (
            self.replay_bytes is None
            or self.replay_bytes < self.byte_limit
            or (self.phase == "before_commit" and self.replay_bytes == self.byte_limit)
        ):
            raise ValueError("历史容量上限与会话状态不一致")
        if len(self.model_dump_json().encode()) > SESSION_STATE_MAX_BYTES:
            raise ValueError("会话状态超过容量")
        return self


SecurityFailureReason = Literal[
    "object_not_readable",
    "schema_unavailable",
    "unsupported",
    "busy",
    "budget_exhausted",
    "connect_failed",
    "auth_failed",
    "session_setup_failed",
    "permission_denied",
    "object_missing",
    "object_unreadable",
    "query_failed",
    "server_timeout",
    "timeout",
    "connection_lost",
]


class SecurityInspectionFailure(_Trusted):
    """实际巡检调用的可信失败；没有成功事实、SQL 或异常正文。"""

    tool_id: Literal["local/inspect_audit_security"] = "local/inspect_audit_security"
    target_id: Label
    call_id: Label
    start: AwareDatetime
    end: AwareDatetime
    time_zone: str
    reason: SecurityFailureReason
    sql_started: bool
    counted: bool
    after_evidence_id: Label | None = None
    """失败前最后一个成功 Evidence；恢复时复用原产生顺序，不另建成功账本。"""


class TurnAnswer(_Trusted):
    """一轮的最终回答与本轮模型可见的证据；后者由可信代码记录，不来自模型。

    ``context_evidence`` 是本轮交给模型的全部工具证据：回放的会话历史与本轮工具结果。模型文字
    可能复述其中任何一条，因此回答只能引用其中的证据，每次交付都按当前权限复核全部这些证据；
    任一条确定不可读时整条回答不交付。

    ``answer_version`` 由可信代码写入：``2/3/4`` 只接受 ``AgentAnswer``；缺失只接受严格的旧
    ``LegacyAnswer``；其他取值、两者不符或新格式解析失败都拒绝，不回退旧格式。代码直接以
    ``AgentAnswer`` 对象构造时自动写入当前版本；JSON 输入不补版本。
    """

    answer: AgentAnswer | LegacyAnswer
    answer_version: Literal[2, 3, 4] | None = None
    context_evidence: tuple[Label, ...]
    monitoring_failures: tuple[MonitoringFailure, ...] = ()
    inspection_failures: tuple[SecurityInspectionFailure, ...] = ()
    run_turn_id: Label | None = None
    limit_reached: bool = False
    """可信账本记录：步数用完后由无工具收尾生成本回答；交付时由代码说明可能不完整。"""
    material_only: bool = False
    """暂停材料：动作区域只含显式列出的（本次请求执行的）动作，不补全同轮其他提案。"""
    content_expires_at: AwareDatetime | None = Field(default=None, exclude=True)
    """本轮Session最终校验得到的原数据期限，仅用于可信调用方保存结果，不写入回答JSON。"""
    session_state: SessionSnapshot | None = Field(default=None, exclude=True)
    """成功提交后由应用附加；请求表单独保存，不进入回答 JSON 或 SDK Session。"""

    @model_validator(mode="before")
    @classmethod
    def _version_for_code(cls, data: object) -> object:
        if (
            isinstance(data, dict)
            and isinstance(data.get("answer"), AgentAnswer)
            and "answer_version" not in data
        ):
            return {**data, "answer_version": ANSWER_VERSION}
        return data

    @model_validator(mode="after")
    def _version_matches(self) -> "TurnAnswer":
        expected = AgentAnswer if self.answer_version in (2, 3, 4) else LegacyAnswer
        if type(self.answer) is not expected:
            raise ValueError("回答版本与内容不符")
        return self

    @property
    def blocks_answer(self) -> AgentAnswer:
        """当前块结构：新回答原样，旧回答按原语义转换。"""
        if isinstance(self.answer, AgentAnswer):
            return self.answer
        return legacy_blocks(self.answer)


FactSection = Literal["action", "body", "remaining"]


class DeliveryFact(_Trusted):
    """一条证据的结构化事实：只由 ``EvidenceStore`` 从接收渠道的获准投影生成，模型不能提交。

    ``columns``/``rows`` 是投影中的表格数据；``metadata`` 保留其余字段，嵌套值为 JSON 文本。
    超出 JavaScript 安全整数范围的值以字符串展示；``result_json`` 保留当前渠道获准数据的原值与类型，
    不增加读取范围。来源、目标、采集时间与截断来自证据记录；``note`` 是当前策略的固定说明。
    """

    evidence_id: Label
    tool_id: ToolId
    target_id: Label
    captured_at: datetime
    truncated: bool
    columns: tuple[str, ...]
    rows: tuple[dict[str, JsonScalar], ...]
    metadata: dict[str, JsonScalar]
    note: str | None = None
    note_summary: str | None = None
    result_json: str | None = None
    requires_complete: bool = Field(default=False, exclude=True)
    view: Literal["full", "brief"] = "full"
    summary: str | None = None
    """仅历史 brief 的代码计数摘要；本轮 brief 不带摘要，原表与 JSON 保留。"""
    section: FactSection = "body"
    """展示区域：动作区域置顶，正文由模型放置，其余结果由代码补全。"""

    @property
    def historical_brief(self) -> bool:
        """只有代码生成的历史摘要可省略原值；普通本轮 brief 不带 summary。"""
        return (
            self.view == "brief"
            and self.summary is not None
            and self.section != "action"
            and not self.requires_complete
        )


class DeliveryText(_Trusted):
    """一段模型文字（已去除空白块）；渠道按模型说明呈现，不获得系统事实区域的身份。"""

    kind: Literal["text"] = "text"
    text: str


class DeliveryFactRef(_Trusted):
    """正文中由模型放置的一条代码事实；内容见 ``Delivery.facts`` 中同一编号的事实。"""

    kind: Literal["fact"] = "fact"
    evidence_id: str


DeliveryBlock = Annotated[DeliveryText | DeliveryFactRef, Field(discriminator="kind")]


class Delivery(_Trusted):
    """通过验证、按接收渠道自身投影生成的输出。

    ``facts`` 按展示顺序排列：动作区域（置顶，模型块不能移除或改位）→ 正文中模型放置的事实
    （与 ``blocks`` 的顺序一致）；旧 v2/v3 另有代码补全的“其余结果”。
    ``blocks`` 只含正文：模型文字与正文事实的引用。系统说明在固定字段中，由代码生成。
    """

    answer_version: Literal[2, 3, 4] | None = None
    """可信保存语义，贯穿重发和渲染；缺失仍为旧布局。"""
    content: str
    evidence_ids: tuple[str, ...]
    channel: Channel
    facts: tuple[DeliveryFact, ...] = ()
    blocks: tuple[DeliveryBlock, ...] = ()
    inspection_notice: str | None = None
    """可信巡检失败生成，置于模型正文前。"""
    monitoring_notice: str | None = None
    """系统按可信失败记录生成的未核实源说明；Web 与飞书均须展示。"""
    session_notice: str | None = None
    """当前身份读取后，由可信请求状态生成的固定系统提示；不来自模型。"""
    report_notice: str | None = None
    """报告不能作为文件交付或需要说明载体时的可信固定说明（含非飞书渠道的文件请求）。"""
    notification_notice: str | None = None
    """代码生成的通知回执或审批后通知限制；后续失败不抹掉已发送事实。"""
    limit_notice: str | None = None
    """步数用完后收尾生成的回答：代码说明结果可能不完整，不写进模型文字。"""
    requires_complete: bool = Field(default=False, exclude=True)
    """包含待批准动作时，所有代码生成的材料必须完整发送；容量提示不算动作送达。"""
    awaiting_user: bool = Field(default=False, exclude=True)
    """由已验证回答生成；等待用户补充时只发普通消息，容量不足也不转附件。"""
    report_format: Literal["md", "html"] | None = Field(default=None, exclude=True)
    """模型在本轮选择的飞书报告格式；事实和文件内容仍由代码生成。"""
    report_native: bool = Field(default=False, exclude=True)
    """可信保存版本允许原生报告文字；旧纯文本和自动附件不能由内容猜测升级。"""
    request_state: Literal["completed", "paused"] | None = Field(default=None, exclude=True)
    """发送时由已保存请求附加，供报告文件与卡片选择；不进入回答或渠道 JSON。"""
    delivery_attempt: str | None = Field(default=None, exclude=True)
    delivery_version: int = Field(default=0, exclude=True)
    """渠道取得的投递权；不进入模型、Session 或 Web 输出。"""
    basis: tuple[tuple[str, str], ...] = Field(default=(), exclude=True)
    """未展示的本轮其他成功调查（诊断、发现读取）：仅工具/对象，不附原值。"""
    content_expires_at: datetime | None = Field(default=None, exclude=True)
    """本次已复核内容的最早保留期限，供投递途中廉价检查。"""

    def result_label(self, evidence_id: str) -> str:
        """用户可见的结果编号：按展示顺序，拆分审批材料后仍不变；不展示内部证据编号。"""
        if evidence_id in self.evidence_ids:
            return f"结果 {self.evidence_ids.index(evidence_id) + 1}"
        return "历史结果"

    @property
    def texts(self) -> tuple[str, ...]:
        """正文中的模型文字，按顺序。"""
        return tuple(b.text for b in self.blocks if isinstance(b, DeliveryText))

    def section(self, name: FactSection) -> tuple[DeliveryFact, ...]:
        return tuple(f for f in self.facts if f.section == name)

    @property
    def notices(self) -> tuple[str, ...]:
        """代码生成的固定说明，按展示顺序。"""
        return tuple(
            n
            for n in (
                self.inspection_notice,
                self.report_notice,
                self.notification_notice,
                self.limit_notice,
                self.monitoring_notice,
                self.session_notice,
            )
            if n
        )

    @property
    def has_system_content(self) -> bool:
        """是否含任何代码生成的内容：事实、动作、补全结果或系统说明。"""
        return bool(
            self.facts
            or self.inspection_notice
            or self.notification_notice
            or self.monitoring_notice
            or self.session_notice
            or self.report_notice
            or self.limit_notice
            or (self.basis and self.answer_version != 4)
        )
