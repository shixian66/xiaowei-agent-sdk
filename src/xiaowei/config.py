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
    field_validator,
    model_validator,
)

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


class WebConfig(BaseModel):
    """本机 Web 入口：固定操作者、允许的同源地址、cookie ``Secure`` 与请求体上限。

    ``allowed_origins`` 写成规范形式 ``scheme://host:port``（端口必须显式）；请求的 Host 与 Origin
    按字面精确匹配，不做 DNS 解析或别名归一，尾点、大小写变体、其他 IP 写法都不放行。首版只允许
    loopback 地址；经 HTTPS/SSH 入口使用时由 G6 决定是否扩展并打开 ``secure_cookie``。
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
        if len({origin.split("://", 1)[1] for origin in value}) != len(value):
            raise ValueError("同一 host:port 只能对应一个 scheme")
        return value
