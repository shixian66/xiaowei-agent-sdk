"""调用前治理：工具目录、范围与参数复核、当前授权、原子预算，以及共享的结果入口。

工具可见性（``allowed_contracts``）只回答“这轮可以考虑什么”；``invoke`` 在实际 I/O 前重新
检查契约、范围、参数、当前权限和预算。拒绝与失败使用固定信息：SDK 默认把异常文本交给模型，
因此这里的错误不能携带原始参数、结果、连接信息或下层异常。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

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

# 最小投影上限：须容纳证据标识与截断/空结果标记组成的外层结构。
MIN_PROJECTION_BYTES = 256


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
    """``policy_id`` 对应的明确策略：参数模型与四种用途各自的投影。"""

    policy_id: str
    arguments: type[BaseModel]
    projections: Mapping[str, Projection]


class ToolCatalog:
    """启动时获准的契约与策略；登记不完整或不一致时拒绝装配。"""

    def __init__(self, contracts: Iterable[ToolContract], policies: Iterable[ToolPolicy]) -> None:
        self._policies = {p.policy_id: p for p in policies}
        self._contracts: dict[str, ToolContract] = {}
        for policy in self._policies.values():
            if set(policy.projections) != set(AUDIENCES):
                raise ValueError(f"工具目录：策略 {policy.policy_id} 必须且只能定义四种用途投影")
            if policy.arguments.model_config.get("extra") != "forbid":
                raise ValueError(f"工具目录：策略 {policy.policy_id} 的参数模型必须禁止额外字段")
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
    """本地与 MCP 工具共用的治理入口；依赖由应用装配，``execute`` 只由应用绑定。"""

    def __init__(
        self, catalog: ToolCatalog, evidence: EvidenceStore, *, authorize: Authorizer
    ) -> None:
        self._catalog = catalog
        self._evidence = evidence
        self._authorize = authorize
        self._used: dict[tuple[str, str, str], int] = {}

    def allowed_contracts(self, ctx: RunContext) -> list[ToolContract]:
        """本轮可展示的工具：可信配置与本轮 Tool Scope、Target Scope 的交集。"""
        return [c for c in self._catalog.contracts if _in_scope(ctx, c)]

    async def invoke(
        self,
        ctx: RunContext,
        request: ToolRequest,
        execute: Callable[[], Awaitable[ToolObservation]],
    ) -> ToolResult:
        contract = self._catalog.contract(request.tool_id)
        if contract is None or contract.target_id != request.target_id:
            raise ToolRejectedError("工具不可用")
        if not _in_scope(ctx, contract):
            raise ToolRejectedError("本轮不允许使用该工具")
        try:
            self._catalog.policy_for(contract).arguments.model_validate(request.arguments)
        except ValidationError:
            raise ToolRejectedError("参数不符合工具契约") from None
        if not await self._currently_authorized(ctx.identity, contract):
            raise ToolRejectedError("当前无权调用该工具")
        # 授权回调之后、执行之前不再 await：检查与计数在同一步完成，并行调用不能同时越过上限。
        self._reserve(ctx)

        try:
            observation = await execute()
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


def _in_scope(ctx: RunContext, contract: ToolContract) -> bool:
    return contract.tool_id in ctx.tool_scope and contract.target_id in ctx.target_scope


def _turn_key(identity: Identity) -> tuple[str, str, str]:
    return (identity.subject_id, identity.session_id, identity.turn_id)
