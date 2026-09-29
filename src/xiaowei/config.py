"""运行时配置。"""

import os
import re

from agents import set_trace_processors, set_tracing_disabled
from pydantic import SecretStr

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
