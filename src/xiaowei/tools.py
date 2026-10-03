"""受治理的 SDK FunctionTool：本地工具与 MCP 工具共用的薄包装。

只做三件事：把模型给出的参数解析为 JSON 对象，用 SDK 工具上下文中的调用标识构造
``ToolRequest``，经 ``GovernedTools.invoke`` 执行由应用绑定的 ``execute``。模型只能提供参数，
不能提供执行路径。

只有确定发生在 I/O 之前的治理拒绝（``ToolRejectedError``）按 SDK 公开的
``default_tool_error_function`` 以固定信息交给模型，模型可以修正后再调用。执行已开始后的
失败（执行错误、结果不合约、证据无法保存或执行期间撤权）结果未知，不能让模型在同一轮
重试：异常照常抛出，SDK 据此中止整轮（包装为 ``UserError``，原异常为 ``__cause__``）。

多目标工具（``routed_function_tool``）对模型只有一个函数，参数 ``cluster`` 必填：按它选取该目标
的契约与执行函数；缺少、不是字符串或没有登记该目标时以固定信息拒绝，不回退到其他目标。
目标与参数的一致性由 ``GovernedTools.invoke`` 再核对一次。
"""

import copy
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agents import FunctionTool, UserError, default_tool_error_function
from agents.tool_context import ToolContext

from xiaowei.governance import TARGET_ARGUMENT, Execute, GovernedTools, ToolRejectedError
from xiaowei.models import ToolContract, ToolRequest, ToolResult

_UNKNOWN_CLUSTER = "集群不存在或该集群不提供此工具，未执行；请使用工具说明中列出的集群 ID"


def governed_function_tool(
    name: str, contract: ToolContract, governance: GovernedTools, execute: Execute
) -> FunctionTool:
    """为已登记契约构造 SDK 函数工具；参数 schema 不受 SDK 严格模式支持时作为登记错误拒绝。"""

    def route(arguments: dict[str, Any]) -> tuple[ToolContract, Execute]:
        return contract, execute

    return _function_tool(name, contract, contract.description, governance, route)


def routed_function_tool(
    tool_id: str, governance: GovernedTools, executes: Mapping[str, Execute]
) -> FunctionTool:
    """多目标工具：每个登记目标一个执行函数，模型以参数 ``cluster`` 选择目标。

    函数名取 ``tool_id`` 的名字部分；说明在各目标相同时直接使用，不同时逐个目标列出。
    """
    catalog = governance.catalog
    contracts = {c.target_id: c for c in catalog.contracts_for(tool_id)}
    if not catalog.routed(tool_id) or set(executes) != set(contracts):
        raise ValueError(f"工具登记：{tool_id} 的目标与执行函数必须一一对应且声明 cluster")
    descriptions = {c.description for c in contracts.values()}
    if len(descriptions) == 1:
        description = descriptions.pop()
    else:
        description = "\n".join(f"集群 {t}：{c.description}" for t, c in contracts.items())
    choices = f"{TARGET_ARGUMENT} 必填，取值：{'、'.join(contracts)}。"
    description = f"{description}\n{choices}" if description else choices

    def route(arguments: dict[str, Any]) -> tuple[ToolContract, Execute]:
        cluster = arguments.get(TARGET_ARGUMENT)
        if not isinstance(cluster, str) or cluster not in contracts:
            raise ToolRejectedError(_UNKNOWN_CLUSTER)
        return contracts[cluster], executes[cluster]

    first = next(iter(contracts.values()))
    return _function_tool(tool_id.split("/", 1)[1], first, description, governance, route)


def _function_tool(
    name: str,
    schema_from: ToolContract,
    description: str,
    governance: GovernedTools,
    route: Callable[[dict[str, Any]], tuple[ToolContract, Execute]],
) -> FunctionTool:
    async def invoke(tool_ctx: ToolContext[Any], raw_arguments: str) -> str:
        try:
            arguments = json.loads(raw_arguments or "{}")
        except ValueError:
            arguments = None
        if not isinstance(arguments, dict):
            return default_tool_error_function(tool_ctx, ToolRejectedError("参数不符合工具契约"))
        try:
            contract, execute = route(arguments)
            result = await _governed(tool_ctx, contract, arguments, governance, execute)
        except ToolRejectedError as exc:
            return default_tool_error_function(tool_ctx, exc)
        return result.model_content

    try:
        return FunctionTool(
            name=name,
            description=description,
            params_json_schema=copy.deepcopy(schema_from.input_schema),
            on_invoke_tool=invoke,
            strict_json_schema=True,
        )
    except UserError:
        raise ValueError(
            f"工具登记：{schema_from.tool_id} 的参数 schema 不受 SDK 严格模式支持"
        ) from None


def _governed(
    tool_ctx: ToolContext[Any],
    contract: ToolContract,
    arguments: dict[str, Any],
    governance: GovernedTools,
    execute: Execute,
) -> Awaitable[ToolResult]:
    request = ToolRequest(
        tool_id=contract.tool_id,
        target_id=contract.target_id,
        call_id=tool_ctx.tool_call_id,
        tool_name=tool_ctx.tool_name,
        arguments=arguments,
    )
    return governance.invoke(tool_ctx.context, request, execute)
