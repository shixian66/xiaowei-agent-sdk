"""调用前治理：工具目录、范围与参数复核、当前授权、原子预算，以及共享的结果入口。

工具可见性（``allowed_contracts``）只回答“这轮可以考虑什么”；``invoke`` 在实际 I/O 前重新
检查契约、范围、参数、当前权限和预算；I/O 之后由 Evidence 结果入口按当前权限重新读取，
执行期间撤权时结果不交给模型。拒绝与失败使用固定信息：SDK 默认把异常文本交给模型，
因此这里的错误不能携带原始参数、结果、连接信息或下层异常。

参数按 JSON 严格模式校验（不做字符串转数字等转换，拒绝非有限数值），实际执行只收到
校验后规范化的参数；参数模型不能有默认值或接收未声明字段（含嵌套），因此模型给出的参数
就是完整的执行参数。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from xiaowei.models import (
    AUDIENCES,
    Identity,
    RunContext,
    ToolContract,
    ToolObservation,
    ToolRequest,
    ToolResult,
)

if TYPE_CHECKING:
    from xiaowei.evidence import EvidenceStore

Authorizer = Callable[[Identity, str, str], Awaitable[bool]]
"""应用提供的当前授权查询：``(identity, target_id, tool_id) -> 是否仍获准``。"""

Execute = Callable[[ToolRequest], Awaitable[ToolObservation]]
"""应用绑定的实际 I/O：只在治理通过后以校验、规范化后的请求调用。"""

# 最小投影上限：须容纳证据标识与截断/空结果标记组成的外层结构。
MIN_PROJECTION_BYTES = 256

# JSON schema 中只供阅读的文字：不改变数据形状。
_SCHEMA_TEXT = frozenset({"title", "description"})


class ToolRejectedError(Exception):
    """调用前治理拒绝；未发生任何工具 I/O。"""


class ToolExecutionError(Exception):
    """工具已开始执行但失败；结果未知，不自动重试。"""


@dataclass(frozen=True)
class Projection:
    """一种用途可接收的顶层字段（按优先顺序）与序列化后的字节上限。"""

    fields: tuple[str, ...]
    max_bytes: int

    def __post_init__(self) -> None:
        if not self.fields or len(set(self.fields)) != len(self.fields):
            raise ValueError("投影字段不能为空或重复")
        if self.max_bytes < MIN_PROJECTION_BYTES:
            raise ValueError(f"投影上限不能小于 {MIN_PROJECTION_BYTES} 字节")


@dataclass(frozen=True)
class ToolPolicy:
    """``policy_id`` 对应的明确策略：参数模型、四种用途各自的投影，以及不可信来源的结果模型。

    ``result`` 用于 MCP 等远端结果：按严格类型校验，只保留其声明的字段，不合约的结果整体
    拒绝；它不能接收未声明字段（含嵌套模型），投影也只能取它声明的字段。本地 Adapter 由
    可信代码产生结果，可不提供。
    """

    policy_id: str
    arguments: type[BaseModel]
    projections: Mapping[str, Projection]
    result: type[BaseModel] | None = None


class ToolCatalog:
    """启动时获准的契约与策略；登记不完整或不一致时拒绝装配。"""

    def __init__(self, contracts: Iterable[ToolContract], policies: Iterable[ToolPolicy]) -> None:
        self._policies = {p.policy_id: p for p in policies}
        self._contracts: dict[str, ToolContract] = {}
        for policy in self._policies.values():
            if set(policy.projections) != set(AUDIENCES):
                raise ValueError(f"工具目录：策略 {policy.policy_id} 必须且只能定义四种用途投影")
            arguments = policy.arguments.model_json_schema()
            if not _forbids_undeclared(arguments) or _has_optional(arguments):
                raise ValueError(
                    f"工具目录：策略 {policy.policy_id} 的参数模型必须禁止额外字段且全部字段必填"
                )
            if policy.result is not None:
                _check_result_model(policy, policy.result)
        for contract in contracts:
            registered = self._policies.get(contract.policy_id)
            if registered is None:
                raise ValueError(f"工具目录：{contract.tool_id} 引用了未登记的策略")
            if contract.input_schema != registered.arguments.model_json_schema():
                raise ValueError(f"工具目录：{contract.tool_id} 的参数 schema 与策略不一致")
            if contract.tool_id in self._contracts:
                raise ValueError(f"工具目录：{contract.tool_id} 重复登记")
            self._contracts[contract.tool_id] = contract

    @property
    def contracts(self) -> tuple[ToolContract, ...]:
        return tuple(self._contracts.values())

    def contract(self, tool_id: str) -> ToolContract | None:
        return self._contracts.get(tool_id)

    def policy_for(self, contract: ToolContract) -> ToolPolicy:
        return self._policies[contract.policy_id]


class GovernedTools:
    """本地与 MCP 工具共用的治理入口；依赖由应用装配，``execute`` 只由应用绑定。

    工具目录与授权查询取自证据存储本身：调用前治理、结果入口、回放与最终交付因此只有
    一套契约与一个授权来源。
    """

    def __init__(self, evidence: EvidenceStore) -> None:
        self._evidence = evidence
        self._catalog = evidence.catalog
        self._authorize = evidence.authorize
        self._used: dict[tuple[str, str, str], int] = {}

    @property
    def catalog(self) -> ToolCatalog:
        return self._catalog

    @property
    def evidence(self) -> EvidenceStore:
        return self._evidence

    def allowed_contracts(self, ctx: RunContext) -> list[ToolContract]:
        """本轮可展示的工具：可信配置与本轮 Tool Scope、Target Scope 的交集。"""
        return [c for c in self._catalog.contracts if _in_scope(ctx, c)]

    async def invoke(
        self,
        ctx: RunContext,
        request: ToolRequest,
        execute: Execute,
    ) -> ToolResult:
        """治理通过后以规范化参数执行；证据按模型给出的调用（与会话历史一致）记录。"""
        contract = self._catalog.contract(request.tool_id)
        if contract is None or contract.target_id != request.target_id:
            raise ToolRejectedError("工具不可用")
        if not _in_scope(ctx, contract):
            raise ToolRejectedError("本轮不允许使用该工具")
        try:
            # 不依赖参数模型自身的配置：JSON 严格模式不做类型转换，非有限数值在序列化时拒绝。
            validated = self._catalog.policy_for(contract).arguments.model_validate_json(
                json.dumps(request.arguments, allow_nan=False), strict=True
            )
        except (ValueError, TypeError):
            raise ToolRejectedError("参数不符合工具契约") from None
        normalized = request.model_copy(update={"arguments": validated.model_dump(mode="json")})
        if not await self._currently_authorized(ctx.identity, contract):
            raise ToolRejectedError("当前无权调用该工具")
        # 授权回调之后、执行之前不再 await：检查与计数在同一步完成，并行调用不能同时越过上限。
        self._reserve(ctx)

        try:
            observation = await execute(normalized)
        except Exception:
            raise ToolExecutionError("工具执行失败") from None
        if not isinstance(observation, ToolObservation):
            raise ToolExecutionError("工具执行失败")
        return await self._evidence.record(ctx, request, observation)

    def end_turn(self, identity: Identity) -> None:
        """轮次结束时由应用调用，清理该轮计数。"""
        self._used.pop(_turn_key(identity), None)

    async def _currently_authorized(self, identity: Identity, contract: ToolContract) -> bool:
        try:
            return await self._authorize(identity, contract.target_id, contract.tool_id) is True
        except Exception:
            # 授权查询失败时拒绝调用，并以固定信息报告，不把下层异常交给模型。
            raise ToolRejectedError("权限检查失败，未执行工具") from None

    def _reserve(self, ctx: RunContext) -> None:
        key = _turn_key(ctx.identity)
        used = self._used.get(key, 0)
        if used >= ctx.budget.max_tool_calls:
            raise ToolRejectedError("本轮工具调用次数已用完")
        self._used[key] = used + 1


def schema_shape(node: object, *, ignore_extra_flags: bool = False) -> object:
    """决定数据形状的 JSON schema：去掉标题与说明文字（``properties`` 中的字段名本身保留）。

    ``ignore_extra_flags`` 另外忽略布尔的 ``additionalProperties``（对象是否接受额外字段）；
    值为 schema 的 ``additionalProperties`` 描述映射的值类型，始终保留。
    """
    if isinstance(node, list):
        return [schema_shape(value, ignore_extra_flags=ignore_extra_flags) for value in node]
    if not isinstance(node, dict):
        return node
    shape: dict[str, object] = {}
    for key, value in node.items():
        if key in _SCHEMA_TEXT or (
            ignore_extra_flags and key == "additionalProperties" and isinstance(value, bool)
        ):
            continue
        if key == "properties" and isinstance(value, dict):
            shape[key] = {
                name: schema_shape(spec, ignore_extra_flags=ignore_extra_flags)
                for name, spec in value.items()
            }
        else:
            shape[key] = schema_shape(value, ignore_extra_flags=ignore_extra_flags)
    return shape


def _check_result_model(policy: ToolPolicy, result: type[BaseModel]) -> None:
    declared = set(result.model_fields)
    if any(name not in declared for spec in policy.projections.values() for name in spec.fields):
        raise ValueError(f"工具目录：策略 {policy.policy_id} 的投影字段不在结果模型中")
    if _accepts_undeclared(result.model_json_schema()):
        raise ValueError(f"工具目录：策略 {policy.policy_id} 的结果模型不能接收未声明字段")


def _accepts_undeclared(node: object) -> bool:
    """声明了字段的对象（模型）是否还接收未声明字段；映射（没有 ``properties``）不算。"""
    if isinstance(node, list):
        return any(_accepts_undeclared(value) for value in node)
    if not isinstance(node, dict):
        return False
    if "properties" in node and node.get("additionalProperties", False) is not False:
        return True
    return any(_accepts_undeclared(value) for value in node.values())


def _forbids_undeclared(node: object) -> bool:
    """每个声明了字段的对象（含嵌套模型）都显式禁止未声明字段；默认的忽略也不行。"""
    if isinstance(node, list):
        return all(_forbids_undeclared(value) for value in node)
    if not isinstance(node, dict):
        return True
    if "properties" in node and node.get("additionalProperties") is not False:
        return False
    return all(_forbids_undeclared(value) for value in node.values())


def _has_optional(node: object) -> bool:
    """声明了字段的对象是否有非必填字段（即带默认值，默认值不经校验）。"""
    if isinstance(node, list):
        return any(_has_optional(value) for value in node)
    if not isinstance(node, dict):
        return False
    properties = node.get("properties")
    if isinstance(properties, dict) and set(properties) != set(node.get("required", ())):
        return True
    return any(_has_optional(value) for value in node.values())


def _in_scope(ctx: RunContext, contract: ToolContract) -> bool:
    return contract.tool_id in ctx.tool_scope and contract.target_id in ctx.target_scope


def _turn_key(identity: Identity) -> tuple[str, str, str]:
    return (identity.subject_id, identity.session_id, identity.turn_id)
