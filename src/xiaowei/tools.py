"""受治理的 SDK FunctionTool：本地工具与 MCP 工具共用的薄包装。

只做三件事：把模型给出的参数解析为 JSON 对象，用 SDK 工具上下文中的调用标识构造
``ToolRequest``，经 ``GovernedTools.invoke`` 执行由应用绑定的 ``execute``。模型只能提供参数，
不能提供执行路径；治理与证据的受控失败按 SDK 公开的 ``default_tool_error_function`` 交给模型
（直接构造的 FunctionTool 抛错会中止整轮），消息固定。
"""

import copy
import json
from collections.abc import Awaitable, Callable
from typing import Any

from agents import FunctionTool, UserError, default_tool_error_function
from agents.tool_context import ToolContext

from xiaowei.evidence import EvidenceError
from xiaowei.governance import GovernedTools, ToolExecutionError, ToolRejectedError
from xiaowei.models import ToolContract, ToolObservation, ToolRequest

Execute = Callable[[ToolRequest], Awaitable[ToolObservation]]
"""应用绑定的实际 I/O：只在治理通过后以已校验的请求调用。"""

_TOOL_FAILURES = (ToolRejectedError, ToolExecutionError, EvidenceError)


def governed_function_tool(
    name: str, contract: ToolContract, governance: GovernedTools, execute: Execute
) -> FunctionTool:
    """为已登记契约构造 SDK 函数工具；参数 schema 不受 SDK 严格模式支持时作为登记错误拒绝。"""

    async def invoke(tool_ctx: ToolContext[Any], raw_arguments: str) -> str:
        try:
            arguments = json.loads(raw_arguments or "{}")
        except ValueError:
            arguments = None
        if not isinstance(arguments, dict):
            return default_tool_error_function(tool_ctx, ToolRejectedError("参数不符合工具契约"))
        request = ToolRequest(
            tool_id=contract.tool_id,
            target_id=contract.target_id,
            call_id=tool_ctx.tool_call_id,
            tool_name=tool_ctx.tool_name,
            arguments=arguments,
        )
        try:
            result = await governance.invoke(tool_ctx.context, request, lambda: execute(request))
        except _TOOL_FAILURES as exc:
            return default_tool_error_function(tool_ctx, exc)
        return result.model_content

    try:
        return FunctionTool(
            name=name,
            description=contract.description,
            params_json_schema=copy.deepcopy(contract.input_schema),
            on_invoke_tool=invoke,
            strict_json_schema=True,
        )
    except UserError:
        raise ValueError(
            f"工具登记：{contract.tool_id} 的参数 schema 不受 SDK 严格模式支持"
        ) from None
