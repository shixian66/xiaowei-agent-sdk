"""运行时配置。"""

import os
import re
from typing import Annotated
from urllib.parse import urlsplit

from agents import set_trace_processors, set_tracing_disabled
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from xiaowei.models import group_owner

# 凭据只以引用形式出现在配置中；目前唯一支持的来源是具名环境变量。
_ENV_REF = re.compile(r"env:([A-Z][A-Z0-9_]*)")


class SecretRefError(Exception):
    """凭据引用无效或无法解析；消息不包含凭据值或原始引用。"""


def configure_runtime() -> None:
    """显式关闭 SDK tracing，并移除默认的 trace 导出处理器。

    SDK 默认开启 tracing，并在首次使用时注册向 OpenAI 后端导出的处理器。这里同时做两件事：
    关闭 trace 生成，并清空处理器列表，使默认导出器不再挂在 provider 上。
    """
    set_tracing_disabled(True)
    set_trace_processors([])


def is_secret_ref(ref: str) -> bool:
    return _ENV_REF.fullmatch(ref) is not None


def resolve_secret_ref(ref: str) -> SecretStr:
    """把 ``env:NAME`` 引用解析为凭据；未设置或为空时失败，不回退到其他来源。"""
    match = _ENV_REF.fullmatch(ref)
    if match is None:
        raise SecretRefError("凭据引用必须是 env:NAME 形式")
    name = match.group(1)
    value = os.environ.get(name)
    if not value:
        raise SecretRefError(f"环境变量 {name} 未设置或为空")
    return SecretStr(value)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_MCP_TOOL_NAME = re.compile(r"[a-z0-9_-]{1,64}")
_SDK_TOOL_NAME_MAX = 64


class MCPServerConfig(BaseModel):
    """静态可信的 MCP Server 登记：端点、认证引用、期限、接收上限与获准工具。

    ``allowed_tools`` 把远端工具名映射到已登记的 ``policy_id``；端点只来自这里，不来自用户或
    模型参数。远端须 HTTPS，明文 HTTP 只允许 loopback 测试地址；地址不能携带用户信息。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 不含下划线：SDK 工具名 ``<server_id>__<tool>`` 因此能唯一还原到 server/tool。
    server_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,32}$")]
    url: str
    auth_ref: str | None
    timeout_seconds: float = Field(gt=0)
    max_response_bytes: int = Field(gt=0)
    allowed_tools: dict[str, Annotated[str, StringConstraints(min_length=1, max_length=200)]]

    @field_validator("server_id")
    @classmethod
    def _not_local(cls, value: str) -> str:
        if value == "local":
            raise ValueError("server_id 不能使用本地工具命名空间 local")
        return value

    @field_validator("url")
    @classmethod
    def _trusted_endpoint(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.username is not None or parts.password is not None:
            raise ValueError("MCP 地址不能携带用户信息")
        if parts.query or parts.fragment or not parts.hostname:
            raise ValueError("MCP 地址必须是不含查询与片段的完整地址")
        if parts.scheme == "https" or (
            parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS
        ):
            return value
        raise ValueError("MCP 地址必须使用 HTTPS；HTTP 只允许 loopback 测试地址")

    @field_validator("auth_ref")
    @classmethod
    def _secret_reference(cls, value: str | None) -> str | None:
        if value is not None and not is_secret_ref(value):
            raise ValueError("auth_ref 必须是 env:NAME 形式的凭据引用")
        return value

    @model_validator(mode="after")
    def _tool_names(self) -> "MCPServerConfig":
        if not self.allowed_tools:
            raise ValueError("MCP Server 至少登记一个获准工具")
        for name in self.allowed_tools:
            if _MCP_TOOL_NAME.fullmatch(name) is None:
                raise ValueError("MCP 工具名只能包含小写字母、数字、下划线与连字符")
            if len(self.sdk_tool_name(name)) > _SDK_TOOL_NAME_MAX:
                raise ValueError("MCP 工具名与 server_id 组合后过长")
        return self

    def sdk_tool_name(self, remote_name: str) -> str:
        """交给模型的函数名；server_id 不含下划线，第一个 ``__`` 之前恰好是 server_id。"""
        return f"{self.server_id}__{remote_name}"

    def tool_id(self, remote_name: str) -> str:
        return f"{self.server_id}/{remote_name}"


_WEB_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# 浏览器在 Host 与 Origin 中省略 scheme 的默认端口，按字面匹配时这样的地址永远对不上。
_WEB_DEFAULT_PORTS = {"http": 80, "https": 443}


class WebConfig(BaseModel):
    """本机 Web 入口：固定操作者、允许的同源地址、cookie ``Secure`` 与请求体上限。

    ``allowed_origins`` 写成规范形式 ``scheme://host:port``（端口必须显式）；请求的 Host 与 Origin
    按字面精确匹配，不做 DNS 解析或别名归一，尾点、大小写变体、其他 IP 写法都不放行。端口不能是
    0 或该 scheme 的默认端口（浏览器会省略后者），否则没有请求能匹配。首版只允许 loopback 地址；
    经 HTTPS/SSH 入口使用时由 G6 决定是否扩展并打开 ``secure_cookie``。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operator_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    allowed_origins: frozenset[str] = Field(min_length=1)
    secure_cookie: bool
    max_body_bytes: int = Field(gt=0, le=1_048_576)

    @field_validator("allowed_origins")
    @classmethod
    def _canonical_loopback(cls, value: frozenset[str]) -> frozenset[str]:
        for origin in value:
            parts = urlsplit(origin)
            try:
                port = parts.port
            except ValueError:
                port = None
            host = parts.hostname
            netloc = f"[{host}]" if host == "::1" else host
            if (
                parts.scheme not in ("http", "https")
                or host not in _WEB_LOOPBACK_HOSTS
                or port is None
                or origin != f"{parts.scheme}://{netloc}:{port}"
            ):
                raise ValueError("允许的地址必须是 loopback 的规范 scheme://host:port")
            if port == 0 or port == _WEB_DEFAULT_PORTS[parts.scheme]:
                raise ValueError("允许的地址端口不能为 0 或 scheme 的默认端口")
        if len({origin.split("://", 1)[1] for origin in value}) != len(value):
            raise ValueError("同一 host:port 只能对应一个 scheme")
        return value


_GROUP_MIN_STOP_SECONDS = 7
_Identifier = Annotated[str, StringConstraints(min_length=1, max_length=200)]
_OpenId = Annotated[str, StringConstraints(pattern=r"^ou_[0-9A-Za-z_-]{1,64}$")]


class FeishuGroupConfig(BaseModel):
    """可选的指定群：群标识、全员同权的共享工具与成员目录查询的上界。

    群内只处理 @本机器人 的消息；发送者不需要出现在单聊 ``users`` 中，群授权也不赋予单聊或 Web
    访问。成员资格在开始运行与每次交付前各查一次（SDK ``get_chat_members(force=True)``），最多
    ``member_max_pages`` 页、每页 ``member_page_size`` 人、整次不超过 ``member_timeout_seconds``；
    取到的名单中找到发送者即为本次成员证据（群规模超过上界时只是部分名单，其中找到也算）；找不到、
    失败或超时都按无法确认拒绝，不执行、不回复。
    ``tools`` 只能是已登记的 StarRocks 工具，由运行配置核对。

    同群同一时刻只运行一轮，其余按接受顺序等待：最多 ``max_waiting`` 条（只不计正在运行的一条；
    尚未开始的请求，包括已轮到、仍在等处理能力的一条，以及回执尚未发完的到期请求都计入），
    每条从接受起最多等待 ``max_wait_seconds``；尚未开始运行的请求每 ``wait_check_seconds`` 检查
    一次，到期即记为 failed/busy，固定回执另行有界发送，平台何时收到还受成员查询、网络与发送期限
    影响。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chat_id: Annotated[str, StringConstraints(pattern=r"^oc_[0-9A-Za-z_-]{1,64}$")]
    tools: frozenset[_Identifier] = Field(min_length=1)
    member_page_size: int = Field(gt=0, le=100)
    member_max_pages: int = Field(gt=0, le=20)
    member_timeout_seconds: float = Field(gt=0, le=30)
    max_waiting: int = Field(gt=0, le=100)
    max_wait_seconds: float = Field(gt=0, le=86_400)
    wait_check_seconds: float = Field(gt=0, le=60)

    @model_validator(mode="after")
    def _check_within_wait(self) -> "FeishuGroupConfig":
        if self.wait_check_seconds > self.max_wait_seconds:
            raise ValueError("wait_check_seconds 不得超过 max_wait_seconds")
        return self


class FeishuConfig(BaseModel):
    """飞书入口：应用、唯一租户、获准单聊用户、可选指定群、事件时效、文本上限、队列与期限。

    ``users`` 把单聊发送者 ``open_id`` 映射为内部 subject（再交给 ``AccessPolicy``），可以为空；
    不在映射中的单聊发送者在持久化与模型前处理，不获得任何授权。``group`` 为空时群消息一律
    丢弃。``consumer_count`` 不得超过应用的全局并发上限，由 ``FeishuGateway`` 装配时核对。
    ``queue_size`` 同时是 SDK 回调转交给应用循环的在途事件上限。凭据只以 ``env:NAME`` 引用出现。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    app_id: Annotated[str, StringConstraints(pattern=r"^cli_[0-9A-Za-z]{1,64}$")]
    app_secret_ref: str
    tenant_key: _Identifier
    users: dict[_OpenId, _Identifier]
    max_event_age_seconds: int = Field(gt=0, le=86_400)
    max_message_chars: int = Field(gt=0, le=10_000)
    max_reply_chars: int = Field(ge=200, le=3_500)
    queue_size: int = Field(gt=0, le=1_000)
    consumer_count: int = Field(gt=0)
    send_timeout_seconds: float = Field(gt=0, le=120)
    connect_timeout_seconds: float = Field(gt=0, le=300)
    stop_timeout_seconds: float = Field(gt=0, le=60)
    group: FeishuGroupConfig | None = None

    @field_validator("app_secret_ref")
    @classmethod
    def _secret_reference(cls, value: str) -> str:
        if not is_secret_ref(value):
            raise ValueError("app_secret_ref 必须是 env:NAME 形式的凭据引用")
        return value

    @field_validator("users")
    @classmethod
    def _distinct_subjects(cls, value: dict[str, str]) -> dict[str, str]:
        if len(set(value.values())) != len(value):
            raise ValueError("每个获准用户必须对应不同的内部 subject")
        return value

    @model_validator(mode="after")
    def _group(self) -> "FeishuConfig":
        if self.group is None:
            return self
        # 锁版 SDK 关闭时可能固定等待 5 s + 1 s 清理后台任务（单群计划 F0 证据 6）。
        if self.stop_timeout_seconds < _GROUP_MIN_STOP_SECONDS:
            raise ValueError("启用指定群时 stop_timeout_seconds 不得小于 7")
        if self.group.max_waiting > self.queue_size:
            raise ValueError("group.max_waiting 不得超过 queue_size")
        # 群 owner 是应用、租户与群标识的规范编码；超过归属键上限时群请求必然无法授权，启动时拒绝。
        try:
            group_owner(self.app_id, self.tenant_key, self.group.chat_id)
        except ValidationError:
            raise ValueError(
                "app_id、tenant_key 与 group.chat_id 组成的群归属超过 200 字符上限"
            ) from None
        return self
