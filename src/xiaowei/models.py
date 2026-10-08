"""可信 context、工具请求/结果、Evidence 与结构化回答类型。

全部为不可变、禁止额外字段的 Pydantic 模型：RunContext 只能装声明的标识与范围，不能借
额外字段或字段类型放入客户端、连接或凭据；模型可提交的只有 ``AgentAnswer``。
"""

import json
from datetime import datetime
from typing import Annotated, Literal

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

Label = Annotated[str, StringConstraints(min_length=1, max_length=200)]
JsonScalar = None | bool | int | float | str
ToolId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,64}/[a-z0-9_.-]{1,64}$")]
ClusterId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")]
"""配置中的稳定集群 ID：模型以它作为工具参数 ``cluster`` 选择目标。"""
# 已渲染的一行交付内容：分行字符在渲染时已转义（``_one_line``），行内不再含任何会另起一行的
# 字符（``str.splitlines`` 的全部分行符）。
ContentLine = Annotated[
    str, StringConstraints(pattern="^[^\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]*$")
]


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


class RunContext(_Trusted):
    """SDK 本地 context：可信身份、目标与工具范围、预算及 Evidence 标识，不含任何依赖。"""

    identity: Identity
    target_scope: frozenset[Label]
    tool_scope: frozenset[ToolId]
    budget: Budget
    evidence_ids: tuple[Label, ...] = ()


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
    （被读取或判断的列及其类型）一致。``replayable`` 为假（视图等无法证明间接依赖未变的对象）的
    证据只在产生它的这一轮首次交付，跨轮回放、历史读取与重发一律按确定失效拒绝。
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


class AnswerInference(_Answer):
    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: tuple[str, ...]


class AgentAnswer(_Answer):
    """SDK ``output_type``：模型只选择引用并给出分析；事实区域由代码从 Evidence 生成。

    三种回答互斥（见 ``EvidenceStore.validate_answer``）：引用证据的回答（可带分析）、澄清、
    未执行建议（``advice``：只解释、只写 SQL、不要执行等场景的说明或 SQL 草稿，不含查询事实）。
    ``advice`` 有默认值，只为解析此前保存、没有该字段的回答（它们没有可信的上下文证据记录，
    不再交付，见 ``ChannelStore``）；SDK 严格模式下模型仍须显式给出它（可为 null）。
    """

    evidence_ids: tuple[str, ...]
    inferences: list[AnswerInference]
    clarification: Annotated[str, Field(min_length=1, max_length=2000)] | None
    advice: Annotated[str, Field(min_length=1, max_length=4000)] | None = None


class TurnAnswer(_Trusted):
    """一轮的最终回答与本轮模型可见的证据；后者由可信代码记录，不来自模型。

    ``context_evidence`` 是本轮交给模型的全部工具证据：回放的会话历史与本轮工具结果。模型文字
    （分析、澄清、建议）可能复述其中任何一条，因此回答只能引用其中的证据，每次交付都按当前权限
    复核全部这些证据；只展示 ``answer`` 引用的事实，任一条确定不可读时整条回答不交付。
    """

    answer: AgentAnswer
    context_evidence: tuple[Label, ...]


class DeliveryFact(_Trusted):
    """一条证据的结构化事实：只由 ``EvidenceStore`` 从当前 Web 投影生成，模型不能提交。

    ``columns``/``rows`` 是投影中的表格数据；``metadata`` 保留其余字段，嵌套值为 JSON 文本。
    超出 JavaScript 安全整数范围的值以字符串展示；``result_json`` 保留获准 Web 数据的原值与类型，
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
    result_json: str | None = None


class FactLines(_Trusted):
    """一条事实的渲染行：``head`` 是来源与说明，``body`` 是结果（标量与表格行）。"""

    head: tuple[ContentLine, ...]
    body: tuple[ContentLine, ...] = ()


class DeliveryLayout(_Trusted):
    """``content`` 的分段，供有单条长度上限的渠道按优先级截断。

    只由 ``EvidenceStore`` 与 ``content`` 一同生成；截断方据此取分段，不在文字中查找标题。
    """

    facts_header: ContentLine
    facts: tuple[FactLines, ...]
    analysis: tuple[ContentLine, ...] = ()
    """分析标题及各条分析；没有分析时为空。"""

    def lines(self) -> tuple[str, ...]:
        fact_lines = (line for f in self.facts for line in (*f.head, *f.body))
        return (self.facts_header, *fact_lines, *self.analysis)


class Delivery(_Trusted):
    """通过验证、按接收渠道生成的输出；``facts`` 只在 Web 渠道给出。

    ``layout`` 只在飞书渠道给出，与 ``content`` 逐行一致，不进入序列化输出。
    """

    content: str
    evidence_ids: tuple[str, ...]
    channel: Channel
    facts: tuple[DeliveryFact, ...] = ()
    analysis: tuple[AnswerInference, ...] = ()
    """Web 独立渲染的模型分析；仅由证据验证后的回答生成，不在事实表格中混排。"""
    web_text: str | None = None
    """Web 澄清或未执行建议的原文；飞书仍使用单行转义的 ``content``。"""
    layout: DeliveryLayout | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _layout_matches_content(self) -> "Delivery":
        if self.layout is not None and "\n".join(self.layout.lines()) != self.content:
            raise ValueError("交付分段与内容不一致")
        return self
