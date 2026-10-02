"""调用前治理：工具目录、范围与参数复核、当前授权、原子预算，以及共享的结果入口。

工具可见性（``allowed_contracts``）只回答“这轮可以考虑什么”；``invoke`` 在实际 I/O 前重新
检查契约、范围、参数、当前权限和预算；I/O 之后由 Evidence 结果入口按当前权限重新读取，
执行期间撤权时结果不交给模型。拒绝与失败使用固定信息：SDK 默认把异常文本交给模型，
因此这里的错误不能携带原始参数、结果、连接信息或下层异常。

参数经 ``normalize_arguments`` 按 JSON 严格模式校验：不做字符串转数字等转换，拒绝非有限
数值，并在校验调用上强制禁止未声明字段（含嵌套模型，不依赖模型自身配置或可被覆盖的
JSON schema）。实际执行与证据都只使用规范化后的有效参数；会话历史中的调用经同一函数
规范化后再与证据核对。参数模型的字段（含嵌套）必须全部必填：默认值不经校验，也不是
模型给出的参数。有效参数与 MCP 结果都经 ``contract_dump`` 按声明字段与类型从已校验实例生成，不经模型
自己的序列化（计算字段、serializer、``exclude``），也不交回同一模型再校验。
"""

from __future__ import annotations

import enum
import json
import math
import types
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Generic,
    Literal,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

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

Prepared = TypeVar("Prepared")


@dataclass(frozen=True)
class Prechecked(Generic[Prepared]):
    """带同步前置检查的执行：``check`` 在授权之后、预算预留之前运行，``run`` 只收到它的结果。

    ``check`` 必须是零 I/O 的纯函数：把规范化请求转换为执行所需的受信对象，拒绝时抛出信息
    固定、不含参数原文的 ``ToolRejectedError``；此时本次调用不占预算，模型可以修正后再调用。
    """

    check: Callable[[ToolRequest], Prepared]
    run: Callable[[Prepared], Awaitable[ToolObservation]]


Execute = Callable[[ToolRequest], Awaitable[ToolObservation]] | Prechecked[Any]
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

    ``result`` 用于 MCP 等远端结果：按严格类型校验，校验时强制忽略未声明字段（含嵌套模型，
    不论模型自身配置），不合约的结果整体拒绝；投影只能取它声明的字段。本地 Adapter 由可信
    代码产生结果，可不提供。

    ``required`` 是每种用途都必须完整保留的字段：结果缺少它们，或任一投影放不下它们时，
    证据生成失败，结果不交给模型，而不是静默省略。

    ``data_scope`` 是结果所依赖的当前数据范围（对象、列、函数与读取上限）的稳定摘要，进入
    策略指纹：以改变了范围的配置装配后，旧证据不再可读。不依赖目标数据范围的工具为 ``None``。

    ``fact_note`` 是交付事实时由代码附加的固定说明（如执行计划未执行原查询）；只影响渲染，
    不进入策略指纹，也不保存在证据记录中：渲染时总是取当前登记的说明。
    """

    policy_id: str
    arguments: type[BaseModel]
    projections: Mapping[str, Projection]
    result: type[BaseModel] | None = None
    required: tuple[str, ...] = ()
    data_scope: str | None = None
    fact_note: str | None = None


class ToolCatalog:
    """启动时获准的契约与策略；登记不完整或不一致时拒绝装配。"""

    def __init__(self, contracts: Iterable[ToolContract], policies: Iterable[ToolPolicy]) -> None:
        self._policies = {p.policy_id: p for p in policies}
        self._contracts: dict[str, ToolContract] = {}
        for policy in self._policies.values():
            if set(policy.projections) != set(AUDIENCES):
                raise ValueError(f"工具目录：策略 {policy.policy_id} 必须且只能定义四种用途投影")
            if not _supported_model(policy.arguments, required=True):
                raise ValueError(
                    f"工具目录：策略 {policy.policy_id} 的参数模型字段必须全部必填，"
                    "且只由基本类型、枚举与嵌套模型组成"
                )
            if any(
                name not in spec.fields
                for spec in policy.projections.values()
                for name in policy.required
            ):
                raise ValueError(
                    f"工具目录：策略 {policy.policy_id} 的必需字段必须属于每种用途投影"
                )
            result = policy.result
            if result is not None and not _supported_model(result, required=False):
                raise ValueError(
                    f"工具目录：策略 {policy.policy_id} 的结果模型字段只能由基本类型、枚举与"
                    "嵌套模型组成"
                )
            if result is not None and any(
                name not in result.model_fields
                for spec in policy.projections.values()
                for name in spec.fields
            ):
                raise ValueError(f"工具目录：策略 {policy.policy_id} 的投影字段不在结果模型中")
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
        """治理通过后以规范化的有效参数执行，并按同一有效请求记录证据。"""
        contract = self._catalog.contract(request.tool_id)
        if contract is None or contract.target_id != request.target_id:
            raise ToolRejectedError("工具不可用")
        if not _in_scope(ctx, contract):
            raise ToolRejectedError("本轮不允许使用该工具")
        arguments = normalize_arguments(self._catalog.policy_for(contract), request.arguments)
        effective = request.model_copy(update={"arguments": arguments})
        if not await self._currently_authorized(ctx.identity, contract):
            raise ToolRejectedError("当前无权调用该工具")
        # 授权回调之后、执行之前不再 await：前置检查与计数在同一步完成，并行调用不能同时越过
        # 上限；前置检查拒绝时尚未占用预算。
        run = _bind(execute, effective)
        self._reserve(ctx)

        try:
            observation = await run()
        except Exception:
            raise ToolExecutionError("工具执行失败") from None
        if not isinstance(observation, ToolObservation):
            raise ToolExecutionError("工具执行失败")
        return await self._evidence.record(ctx, effective, observation)

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


def _bind(execute: Execute, request: ToolRequest) -> Callable[[], Awaitable[ToolObservation]]:
    if not isinstance(execute, Prechecked):
        return lambda: execute(request)
    rejected: ToolRejectedError | None = None
    try:
        prepared = execute.check(request)
    except ToolRejectedError as exc:
        rejected = exc
    except Exception:
        rejected = ToolRejectedError("工具参数检查失败，未执行")
    if rejected is not None:
        # 在 except 之外抛出：拒绝不带下层异常的上下文。
        raise rejected
    return lambda: execute.run(prepared)


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


def normalize_arguments(policy: ToolPolicy, arguments: Mapping[str, object]) -> dict[str, object]:
    """有效参数：JSON 严格模式、强制禁止未声明字段（含嵌套）后的规范化结果。

    不依赖参数模型自身的 ``extra`` 配置：校验调用本身强制 ``forbid``。非有限数值在序列化时
    拒绝。字段校验器可以改写取值，执行与证据都以改写后的结果为准；改写后的值须符合
    声明类型（见 ``contract_dump``），否则同样拒绝。
    """
    try:
        validated = policy.arguments.model_validate_json(
            json.dumps(arguments, allow_nan=False), strict=True, extra="forbid"
        )
        return contract_dump(policy.arguments, validated)
    except (ValueError, TypeError):
        raise ToolRejectedError("参数不符合工具契约") from None


def contract_dump(model: type[BaseModel], validated: object) -> dict[str, object]:
    """已校验数据交给 I/O、模型与证据的 JSON：按登记模型的声明字段与类型递归生成。

    不调用模型自己的序列化（计算字段、自定义 serializer、``exclude`` 都不参与），也不把
    输出交回同一模型校验（它的 validator 可以掩盖不合约的输出）。字段取自登记的 ``model``
    而不是实例的运行时类型；每个值按声明类型独立核对，模型、枚举与基本类型要求类型完全
    一致（子类或替换的对象不算），联合类型因此只匹配实际校验出的分支；数值须有限。
    不符时抛出 ``ValueError``。
    """
    return _model_data(validated, model)


def _model_data(value: object, model: type[BaseModel]) -> dict[str, object]:
    if type(value) is not model:
        raise ValueError("字段值不符合声明类型")
    data = {}
    for name, field in model.model_fields.items():
        try:
            item = getattr(value, name)
        except AttributeError:
            raise ValueError("字段值不符合声明类型") from None
        data[name] = _json_value(item, field.annotation)
    return data


def _json_value(value: object, annotation: object) -> object:
    """``value`` 按 ``annotation``（已由目录限定为 ``_supported_type`` 的范围）转为 JSON 值。"""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return _json_value(value, args[0])
    if origin is Literal:
        for option in args:
            if type(value) is type(option) and value == option:
                # 选项在登记时已核对为有限 JSON 基本值。
                return option.value if isinstance(option, enum.Enum) else option
    elif origin in (Union, types.UnionType):
        # 类型完全一致时，同一个值能匹配的分支输出相同：顺序不影响结果。
        for option in args:
            try:
                return _json_value(value, option)
            except ValueError:
                continue
    elif origin in (list, set, frozenset) and isinstance(value, origin):
        return [_json_value(item, args[0]) for item in value]
    elif origin is tuple and isinstance(value, tuple):
        if len(args) == 2 and args[1] is Ellipsis:
            return [_json_value(item, args[0]) for item in value]
        if len(args) == len(value):
            return [_json_value(item, arg) for item, arg in zip(value, args, strict=True)]
    elif origin is dict and isinstance(value, dict):
        return {_json_value(k, args[0]): _json_value(v, args[1]) for k, v in value.items()}
    elif isinstance(annotation, type) and type(value) is annotation:
        if issubclass(annotation, BaseModel):
            return _model_data(value, annotation)
        if issubclass(annotation, enum.Enum) or annotation in _SCALARS:
            return _json_scalar(value)
    raise ValueError("字段值不符合声明类型")


_SCALARS: tuple[type, ...] = (str, int, float, bool, type(None))


def _supported_model(model: type[BaseModel], *, required: bool) -> bool:
    """按 Pydantic 运行时字段判断（不读可被覆盖的 schema）：字段（含嵌套模型）只由
    ``_supported_type`` 组成；``required`` 时（参数模型）还须全部必填。"""
    return all(
        (field.is_required() or not required) and _supported_type(field.annotation, required)
        for field in model.model_fields.values()
    )


def _supported_type(annotation: object, required: bool) -> bool:
    """JSON 基本类型、取值为有限 JSON 基本值的枚举与 Literal、嵌套模型，及它们的容器/联合
    （映射的键只能是 ``str``）。``contract_dump`` 只能按这些类型核对与生成数据；其他结构
    （数据类等）的默认值与额外字段规则也无法在这里核对，不接受。"""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        return all(_is_json_scalar(option) for option in args)
    if origin is Annotated:
        return _supported_type(args[0], required)
    if origin is dict:
        return len(args) == 2 and args[0] is str and _supported_type(args[1], required)
    if origin in (Union, types.UnionType, list, tuple, set, frozenset):
        return bool(args) and all(arg is Ellipsis or _supported_type(arg, required) for arg in args)
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return _supported_model(annotation, required=required)
        if issubclass(annotation, enum.Enum):
            return all(_is_json_scalar(member) for member in annotation)
        return annotation in _SCALARS
    return False


def _json_scalar(value: object) -> object:
    """JSON 基本值（枚举取其值）；非基本类型或非有限数值抛出 ``ValueError``。登记与生成共用。"""
    if isinstance(value, enum.Enum):
        value = value.value
    if type(value) not in _SCALARS or (type(value) is float and not math.isfinite(value)):
        raise ValueError("字段值不符合声明类型")
    return value


def _is_json_scalar(value: object) -> bool:
    try:
        _json_scalar(value)
    except ValueError:
        return False
    return True


def _in_scope(ctx: RunContext, contract: ToolContract) -> bool:
    return contract.tool_id in ctx.tool_scope and contract.target_id in ctx.target_scope


def _turn_key(identity: Identity) -> tuple[str, str, str]:
    return (identity.subject_id, identity.session_id, identity.turn_id)
