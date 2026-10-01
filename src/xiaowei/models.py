"""可信 context、工具请求/结果、Evidence 与结构化回答类型。

全部为不可变、禁止额外字段的 Pydantic 模型：RunContext 只能装声明的标识与范围，不能借
额外字段或字段类型放入客户端、连接或凭据；模型可提交的只有 ``AgentAnswer``。
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

Channel = Literal["web", "feishu"]
Audience = Literal["model", "session", "web", "feishu"]
AUDIENCES: tuple[Audience, ...] = ("model", "session", "web", "feishu")

Label = Annotated[str, StringConstraints(min_length=1, max_length=200)]
JsonScalar = None | bool | int | float | str
ToolId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,64}/[a-z0-9_.-]{1,64}$")]


class _Trusted(BaseModel):
    """由可信代码构造：禁止额外字段、构造后不可修改。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Identity(_Trusted):
    subject_id: Label
    session_id: Label
    turn_id: Label
    channel: Channel


class Budget(_Trusted):
    max_turns: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)


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


class ToolObservation(_Trusted):
    """Adapter 已限量读取的临时原始结果；只在内存中交给 Evidence 投影，不直接序列化给模型。"""

    payload: dict[str, object]
    captured_at: AwareDatetime
    truncated: bool


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
    """SDK ``output_type``：模型只选择引用并给出分析；事实区域由代码从 Evidence 生成。"""

    evidence_ids: tuple[str, ...]
    inferences: list[AnswerInference]
    clarification: Annotated[str, Field(min_length=1, max_length=2000)] | None


class DeliveryFact(_Trusted):
    """一条证据的结构化事实：只由 ``EvidenceStore`` 从当前 Web 投影生成，模型不能提交。

    ``columns``/``rows`` 是投影中的表格数据；``metadata`` 是投影中其余的标量字段（如实际 SQL、
    行数、耗时）。来源、目标、采集时间与截断来自证据记录。
    """

    evidence_id: Label
    tool_id: ToolId
    target_id: Label
    captured_at: datetime
    truncated: bool
    columns: tuple[str, ...]
    rows: tuple[dict[str, JsonScalar], ...]
    metadata: dict[str, JsonScalar]


class Delivery(_Trusted):
    """通过验证、按接收渠道生成的输出；``facts`` 只在 Web 渠道给出。"""

    content: str
    evidence_ids: tuple[str, ...]
    channel: Channel
    facts: tuple[DeliveryFact, ...] = ()
