"""受治理的 SDK FunctionTool：本地工具与 MCP 工具共用的薄包装。

只做三件事：把模型给出的参数解析为 JSON 对象，用 SDK 工具上下文中的调用标识构造
``ToolRequest``，经 ``GovernedTools.invoke`` 执行由应用绑定的 ``execute``。模型只能提供参数，
不能提供执行路径。

只有确定发生在 I/O 之前的治理拒绝（``ToolRejectedError``）按 SDK 公开的
``default_tool_error_function`` 以固定信息交给模型，模型可以修正后再调用。执行已开始后的
失败（执行错误、结果不合约、证据无法保存或执行期间撤权）结果未知，不能让模型在同一轮
重试：异常照常抛出，SDK 据此中止整轮（包装为 ``UserError``，原异常为 ``__cause__``）。
"""

import copy
import json
from typing import Any

from agents import FunctionTool, UserError, default_tool_error_function
from agents.tool_context import ToolContext

from xiaowei.governance import Execute, GovernedTools, ToolRejectedError
from xiaowei.models import ToolContract, ToolRequest


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
            result = await governance.invoke(tool_ctx.context, request, execute)
        except ToolRejectedError as exc:
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
