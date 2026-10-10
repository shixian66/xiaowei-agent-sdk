"""MCP 客户端接入：SDK 官方 Streamable HTTP 客户端 + 薄 FunctionTool，调用一律经过治理核心。

不把 ``MCPServer`` 挂到 Agent：SDK 的 MCP 工具把远端结果直接交给模型，输出护栏只能放行或
拒绝，不能过滤（Task 1 实测）。这里只用公开的 ``connect``/``list_tools``/``call_tool``：
启动时按静态登记核对远端工具，每个获准工具包装为 FunctionTool，调用前复用
``GovernedTools.invoke``，结果按登记的结果模型严格校验后进入 Evidence 投影。远端的只读标注、
说明文字与自带的证据标识都不参与授权，也不交给模型。

MCP 库会在本地过滤之前把完整的 JSON-RPC 消息（工具参数与远端结果）写入 DEBUG 日志。连接前
包装进程的 LogRecord 工厂：源自 ``mcp`` 包的记录在生成时就只含固定信息（logger 名字、级别、
异常类型），任何 logger 上的处理器、接入前后挂上的都看不到原始内容。按源文件而不是 logger
名字判断：MCP 库并非都用模块名作 logger（客户端会话用的是 ``"client"``）。之后替换工厂而
不串联原工厂的代码会解除这一约束。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from http import HTTPStatus
from types import TracebackType

import httpx2
import mcp
from agents import FunctionTool, Tool
from agents.mcp import MCPServerStreamableHttp
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool
from pydantic import BaseModel, SecretStr

from xiaowei.config import MCPServerConfig, resolve_secret_ref
from xiaowei.governance import (
    Execute,
    GovernedTools,
    MonitoringReadError,
    Prechecked,
    PromQLExpressionError,
    ToolRejectedError,
    contract_dump,
    schema_shape,
)
from xiaowei.models import (
    GRAFANA_READ_POLICIES,
    MONITORING_READ_POLICIES,
    PROMETHEUS_QUERY_POLICIES,
    PROMETHEUS_READ_POLICIES,
    MonitoringFailureReason,
    RunContext,
    ToolContract,
    ToolObservation,
    ToolRequest,
)
from xiaowei.tools import governed_function_tool

logger = logging.getLogger(__name__)

_READ_TIMEOUT_FACTOR = 2
_RECONNECT_MAX_SECONDS = 3.0
_RECONNECT_COOLDOWN_SECONDS = 2.0
_RECONNECT_MAX_COOLDOWN_SECONDS = 60.0
_PROMETHEUS_ERROR_PREFIXES = {
    "query": "failed making query api call: failed to execute instant query: ",
    "range_query": "failed making range query api call: failed to execute range query: ",
    "label_names": "failed making label names api call: failed to get label names: ",
    "label_values": "failed making label values api call: failed to get label values: ",
    "series": "failed making series api call: failed to get series: ",
    "metric_metadata": (
        "failed making metric metadata api call: failed to get metric metadata from Prometheus: "
    ),
    "list_rules": "failed making rules api call: failed to get rules from Prometheus: ",
}
_PROMETHEUS_READ_ERROR = re.compile(
    r"(?:(client_error: client error: (?:401|403))|"
    r"(server_error: server error: 5[0-9][0-9]))"
)
_PROMQL_SYNTAX = re.compile(
    r'bad_data: invalid parameter "query": ([1-9][0-9]{0,5}):([1-9][0-9]{0,5}): '
    r"parse error: [^\r\n]{1,1024}"
)
_TRUNCATION = re.compile(
    re.escape(
        "\n\nWarning: The result was truncated because the Prometheus MCP server was "
        "started with the flag '--prometheus.truncation-limit="
    )
    + r"[1-9][0-9]*"
    + re.escape(
        "'.\nYou may want to try optimizing your query by refining label filters or "
        "using aggregation functions to group results, where possible.\n"
        "If needed, several tools support a 'truncation_limit'/'limit' argument that can "
        "override the global truncation limit on a per-tool-call basis.\n"
        "This includes the ability to disable truncation on a tool call by setting the "
        "truncation limit to -1."
    )
    + r"\Z"
)
_DURATION = re.compile(r"(-?[0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d|w)")
_DURATION_SECONDS = {
    "ms": Decimal("0.001"),
    "s": Decimal(1),
    "m": Decimal(60),
    "h": Decimal(3600),
    "d": Decimal(86400),
    "w": Decimal(604800),
}

# MCP 库的源码目录：源自这里的日志记录可能含协议原文，生成时即换成固定信息。
_WIRE_SOURCE = os.path.join(os.path.dirname(mcp.__file__), "")


class MCPTransportError(httpx2.StreamError):
    """MCP HTTP 请求越出登记端点，或响应超过接收上限/使用了压缩编码。

    继承 ``StreamError``：MCP 客户端据此把本次请求解析为错误，而不是等到超时。
    """


@dataclass(frozen=True)
class _Binding:
    config: MCPServerConfig
    remote_name: str
    contract: ToolContract
    result: type[BaseModel]


@dataclass(frozen=True)
class _Snapshot:
    """一次已核约连接；工具闭包只引用此连接，不查询当前源状态。"""

    server: MCPServerStreamableHttp
    connection: AsyncExitStack
    tools: tuple[tuple[str, FunctionTool], ...]


@dataclass
class _Source:
    config: MCPServerConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    snapshot: _Snapshot | None = None
    status: str = "unavailable"
    retry_at: float = 0.0
    next_retry_seconds: float = _RECONNECT_COOLDOWN_SECONDS

    def back_off(self) -> None:
        self.retry_at = time.monotonic() + self.next_retry_seconds
        self.next_retry_seconds = min(self.next_retry_seconds * 2, _RECONNECT_MAX_COOLDOWN_SECONDS)

    def recovered(self) -> None:
        self.retry_at = 0.0
        self.next_retry_seconds = _RECONNECT_COOLDOWN_SECONDS


class MCPIntegration:
    """静态登记的 MCP Server 的异步生命周期与按轮工具集合。

    构造时核对登记与工具目录一致，否则拒绝装配；进入时逐个连接并核对远端工具，单个
    Server 不可用或工具契约不符只隐藏相应工具；退出时关闭全部连接。
    """

    def __init__(
        self,
        configs: Iterable[MCPServerConfig],
        governance: GovernedTools,
        *,
        resolve_secret: Callable[[str], SecretStr] = resolve_secret_ref,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._configs = tuple(configs)
        self._governance = governance
        self._resolve_secret = resolve_secret
        self._clock = clock
        self._planned = _bindings(self._configs, governance)
        self._sources = {config.server_id: _Source(config) for config in self._configs}
        # 连接之前构造全部工具：SDK 在构造时做严格 schema 转换，不支持的参数形状在这里作为
        # 登记错误拒绝，而不是在某个 Server 连上之后中止整个接入。
        for name, binding in self._planned.items():
            self._function_tool(name, binding, None, self._sources[binding.config.server_id])
        self._connections: set[AsyncExitStack] = set()
        self._entered = False
        self._closing = False

    async def __aenter__(self) -> MCPIntegration:
        if self._entered:
            raise RuntimeError("MCPIntegration 不能重复进入")
        self._entered = True
        try:
            for config in self._configs:
                await self._start(self._sources[config.server_id])
        except BaseException:
            await self._close_all()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._closing = True
        for source in self._sources.values():
            async with source.lock:
                source.snapshot = None
        await self._close_all()
        self._entered = False

    async def _close_all(self) -> None:
        for connection in tuple(self._connections):
            await self._close(connection)

    async def _close(self, connection: AsyncExitStack) -> None:
        await connection.aclose()
        self._connections.discard(connection)

    @property
    def governance(self) -> GovernedTools:
        return self._governance

    @property
    def available_tool_ids(self) -> frozenset[str]:
        """已连接并核对通过的远端工具的 ``tool_id``。"""
        return frozenset(
            self._planned[name].contract.tool_id
            for source in self._sources.values()
            if source.status == "available" and source.snapshot is not None
            for name, _ in source.snapshot.tools
        )

    @property
    def source_status(self) -> dict[str, str]:
        """供就绪检查读取的安全源状态；不含地址、认证或上游内容。"""
        now = time.monotonic()
        return {
            name: (
                "unavailable"
                if source.status == "cooldown" and now >= source.retry_at
                else source.status
            )
            for name, source in self._sources.items()
        }

    def tools_for(self, ctx: RunContext) -> list[Tool]:
        """本轮可展示的 MCP 工具：已核对的远端工具与本轮治理范围的交集；调用时仍复核。"""
        allowed = {contract.tool_id for contract in self._governance.allowed_contracts(ctx)}
        return [
            tool
            for source in self._sources.values()
            if source.status == "available" and source.snapshot is not None
            for name, tool in source.snapshot.tools
            if self._planned[name].contract.tool_id in allowed
        ]

    async def reconnect_for(
        self,
        allowed_tools: frozenset[str],
        *,
        target_scope: frozenset[str],
        turn_timeout_seconds: float,
    ) -> None:
        """本轮定范围前，只对当前获准且断开的源按需重连；不重放失败调用。"""
        if self._closing or not self._entered:
            return
        sources = [
            source
            for source in self._sources.values()
            if source.status != "available"
            and any(
                binding.config is source.config
                and binding.contract.tool_id in allowed_tools
                and binding.contract.target_id in target_scope
                for binding in self._planned.values()
            )
        ]
        await asyncio.gather(*(self._reconnect(source, turn_timeout_seconds) for source in sources))

    async def _start(self, source: _Source) -> None:
        try:
            snapshot = await self._connect(source, source.config.timeout_seconds)
        except Exception as exc:
            source.back_off()
            logger.warning(
                "MCP Server %s 不可用（%s），已隐藏其工具",
                source.config.server_id,
                type(exc).__name__,
            )
            return
        if snapshot.tools:
            source.snapshot = snapshot
            source.status = "available"
            source.recovered()
        else:
            source.status = "contract_mismatch"
            source.back_off()
            await self._close(snapshot.connection)

    async def _reconnect(self, source: _Source, turn_timeout_seconds: float) -> None:
        async with source.lock:
            if self._closing or source.status == "available" or time.monotonic() < source.retry_at:
                return
            old = source.snapshot
            limit = min(
                _RECONNECT_MAX_SECONDS, source.config.timeout_seconds, turn_timeout_seconds / 2
            )
            try:
                snapshot = await self._connect(source, limit)
            except Exception as exc:
                source.status = "cooldown"
                source.back_off()
                logger.warning(
                    "MCP Server %s 重连失败（%s），进入冷却",
                    source.config.server_id,
                    type(exc).__name__,
                )
                if old is not None:
                    source.snapshot = None
                    await self._close(old.connection)
                return
            if self._closing:
                await self._close(snapshot.connection)
                return
            source.snapshot = snapshot if snapshot.tools else None
            source.status = "available" if snapshot.tools else "contract_mismatch"
            if snapshot.tools:
                source.recovered()
            else:
                source.back_off()
                await self._close(snapshot.connection)
            if old is not None:
                await self._close(old.connection)

    async def _connect(self, source: _Source, limit: float) -> _Snapshot:
        _contain_wire_logs()
        config = source.config
        try:
            authorization = (
                None
                if config.auth_ref is None
                else f"Bearer {self._resolve_secret(config.auth_ref).get_secret_value()}"
            )
        except Exception:
            logger.warning("MCP Server %s 的认证引用无法解析，已隐藏其工具", config.server_id)
            raise
        server = MCPServerStreamableHttp(
            params={
                "url": config.url,
                "timeout": config.timeout_seconds,
                "sse_read_timeout": config.timeout_seconds,
                "terminate_on_close": True,
                "httpx_client_factory": _client_factory(config, authorization),
            },
            name=config.server_id,
            cache_tools_list=True,
            client_session_timeout_seconds=config.timeout_seconds,
            max_retry_attempts=0,
        )
        connection = AsyncExitStack()
        self._connections.add(connection)
        try:
            async with asyncio.timeout(limit):
                await connection.enter_async_context(server)
                # 新对象的工具缓存为空；每次都从新会话重新 list_tools 核约。
                verified = _verified(await server.list_tools(), self._planned, config)
                tools = tuple(
                    (name, self._function_tool(name, self._planned[name], server, source))
                    for name in verified
                )
        except BaseException:
            await self._close(connection)
            raise
        return _Snapshot(server, connection, tools)

    def _function_tool(
        self,
        name: str,
        binding: _Binding,
        server: MCPServerStreamableHttp | None,
        source: _Source,
    ) -> FunctionTool:
        async def execute(request: ToolRequest) -> ToolObservation:
            if server is None:  # 构造期只验证 SDK schema，不展示此工具
                raise RuntimeError("MCP 工具尚未绑定连接")
            try:
                result = await server.call_tool(binding.remote_name, request.arguments)
            except Exception as exc:
                if (
                    _connection_lost(exc)
                    and source.snapshot is not None
                    and source.snapshot.server is server
                ):
                    source.status = "disconnected"
                if binding.contract.policy_id in MONITORING_READ_POLICIES:
                    reason = _read_transport_failure(exc)
                    if reason is not None:
                        raise MonitoringReadError(reason) from None
                raise
            if binding.contract.policy_id in PROMETHEUS_READ_POLICIES and result.is_error:
                if binding.contract.policy_id in PROMETHEUS_QUERY_POLICIES:
                    position = _promql_error_position(result, binding.remote_name)
                    if position is not None:
                        raise PromQLExpressionError(*position)
                reason = _prometheus_query_failure(result, binding.remote_name)
                if reason is not None:
                    raise MonitoringReadError(reason)
            if binding.contract.policy_id in GRAFANA_READ_POLICIES and result.is_error:
                reason = _grafana_read_failure(result, binding.remote_name, request.arguments)
                if reason is not None:
                    raise MonitoringReadError(reason)
            if binding.contract.policy_id in PROMETHEUS_READ_POLICIES:
                payload, truncated = _prometheus_payload(
                    result, binding.result, binding.remote_name
                )
                # 仅补入实际请求字段：远端同名字段不能伪造表达式/冻结后的时间范围。
                for key in ("query", "start_time", "end_time", "step"):
                    if key in request.arguments:
                        payload[key] = request.arguments[key]
                if "step" in request.arguments:
                    payload["step"] = _seconds_text(_seconds(request.arguments["step"]))
            elif binding.contract.policy_id in GRAFANA_READ_POLICIES:
                payload, truncated = _grafana_payload(
                    result, binding.result, binding.remote_name, request.arguments
                )
            else:
                payload, truncated = _payload(result, binding.result), False
            if binding.contract.policy_id in PROMETHEUS_QUERY_POLICIES:
                # 只记录治理层规范化后实际发出的表达式；远端返回中的同名字段不能冒充它。
                payload["query"] = request.arguments["query"]
            return ToolObservation(
                payload=payload,
                captured_at=self._clock(),
                truncated=truncated,
            )

        if binding.contract.policy_id in PROMETHEUS_READ_POLICIES:
            checked: Execute = Prechecked(
                lambda request: _prepare_prometheus(
                    request, binding.contract.policy_id, self._clock()
                ),
                execute,
            )
        elif binding.contract.policy_id in GRAFANA_READ_POLICIES:
            checked = Prechecked(_prepare_grafana, execute)
        else:
            checked = execute
        return governed_function_tool(name, binding.contract, self._governance, checked)


def _prepare_grafana(request: ToolRequest) -> ToolRequest:
    """治理授权后检查单次读取资源；出站省略可选过滤，不改 Session 的原参数摘要。"""
    arguments = dict(request.arguments)
    for key, value in arguments.items():
        if isinstance(value, str):
            bound = 2000 if key == "query" else 256
            if len(value) > bound or (key == "uid" and not value.strip()):
                raise ToolRejectedError("查询文本最多2000字符，其他文本最多256字符，uid 不能为空")
        if key in {"tag", "tags"} and (
            not isinstance(value, list)
            or len(value) > 32
            or any(not isinstance(tag, str) or not tag or len(tag) > 256 for tag in value)
        ):
            raise ToolRejectedError("标签最多32个，每个1–256字符")
    for key, lower, upper in (
        ("limit", 1, 100),
        ("page", 1, 2147483647),
        ("offset", 0, 2147483647),
        ("panelId", 0, 2147483647),
    ):
        if key in arguments:
            value = arguments[key]
            if not isinstance(value, int) or not lower <= value <= upper:
                raise ToolRejectedError(
                    "limit 须为1–100；page 从1开始，offset/panelId 从0开始，不超过32位整数"
                )
    if "from" in arguments:
        start, end = arguments["from"], arguments["to"]
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or not 0 <= start <= end <= 253402300799000
            or end - start > 31 * 86400 * 1000
        ):
            raise ToolRejectedError("from/to 须为有序 Unix 毫秒，窗口不超过31天")
    if arguments.get("panelId") == 0:
        del arguments["panelId"]
    if arguments.get("dashboardUid") == "":
        del arguments["dashboardUid"]
    return request.model_copy(update={"arguments": arguments})


def _prepare_prometheus(request: ToolRequest, policy_id: str, now: datetime) -> ToolRequest:
    """授权之后、预算与网络之前冻结时间/检查资源；原模型参数仍用于 Session 摘要复核。"""
    arguments = dict(request.arguments)
    for key in ("query", "metric"):
        if key in arguments:
            value = arguments[key]
            if not isinstance(value, str) or len(value) > 8192 or (key == "query" and not value):
                raise ToolRejectedError("表达式/指标名须符合长度上限8192，查询表达式不能为空")
            if key == "metric" and not value.strip():
                raise ToolRejectedError(
                    "metric 指标名不能为空；请提供具体指标，未知名称可用 label_values 按需发现"
                )
    if "matches" in arguments:
        matches = arguments["matches"]
        if (
            not isinstance(matches, list)
            or len(matches) > 32
            or (policy_id == "prometheus.series" and not matches)
            or any(
                not isinstance(value, str) or not value or len(value) > 8192 for value in matches
            )
        ):
            raise ToolRejectedError("选择器最多32个、每个1–8192字符；series 至少一个选择器")
    if "label" in arguments:
        label = arguments["label"]
        if (
            not isinstance(label, str)
            or re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,255}", label) is None
        ):
            raise ToolRejectedError("label 须为1–256字符的标签名（字母/下划线开头）")
    if "start_time" in arguments:
        start = _timestamp(arguments["start_time"], now)
        end = _timestamp(arguments["end_time"], now)
        if start > end or end - start > 31 * 86400:
            raise ToolRejectedError("起止时间须有序，窗口不超过31天")
        arguments.update(start_time=_seconds_text(start), end_time=_seconds_text(end))
        if "step" in arguments:
            step = _seconds(arguments["step"])
            if step < 1 or ((end - start) // step) + 1 > 11000:
                raise ToolRejectedError("step 至少1秒、每序列最多11000点；请增大步长或缩小窗口")
            # 官方 range 工具只接受 Prometheus 时长；以整数毫秒发送，记录秒数。
            if step * 1000 != (step * 1000).to_integral_value():
                raise ToolRejectedError("step 须为整毫秒且至少1秒")
            arguments["step"] = f"{int(step * 1000)}ms"
    return request.model_copy(update={"arguments": arguments})


def _seconds(value: object) -> Decimal:
    if not isinstance(value, str) or len(value) > 100:
        raise ToolRejectedError("时间/步长格式无效")
    try:
        match = _DURATION.fullmatch(value)
        seconds = Decimal(match[1]) * _DURATION_SECONDS[match[2]] if match else Decimal(value)
        if not seconds.is_finite() or abs(seconds) > 253402300799:
            raise ValueError
        return seconds
    except (InvalidOperation, ValueError):
        raise ToolRejectedError("时间/步长须为有限秒数或单单位时长（如30s/5m）") from None


def _timestamp(value: object, now: datetime) -> Decimal:
    if not isinstance(value, str) or len(value) > 100:
        raise ToolRejectedError("时间格式无效")
    try:
        if value == "now":
            seconds = Decimal(str(now.timestamp()))
        elif value.startswith("-") and _DURATION.fullmatch(value):
            seconds = Decimal(str(now.timestamp())) + _seconds(value)
        else:
            try:
                seconds = _seconds(value)
            except ToolRejectedError:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError from None
                seconds = Decimal(str(stamp.timestamp()))
            else:
                if _DURATION.fullmatch(value):
                    raise ValueError
        # 官方 ParseTimestamp 以毫秒舍入，发送已舍入秒值防止记录更细的虚假精度。
        return seconds.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    except (ValueError, InvalidOperation, OverflowError):
        raise ToolRejectedError("时间须为Unix秒、含时区RFC3339、now或负的单单位时长") from None


def _seconds_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _connection_lost(exc: BaseException) -> bool:
    """MCP 断线只认锁版完整固定信号；超时与远端错误不是断线证据。"""
    if isinstance(exc, MCPError):
        return exc.data is None and (exc.code, exc.message) in (
            (-32000, "Connection closed"),
            (-32600, "Session terminated"),
        )
    if isinstance(exc, httpx2.NetworkError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_connection_lost(item) for item in exc.exceptions)
    return exc.__cause__ is not None and _connection_lost(exc.__cause__)


def _read_transport_failure(exc: BaseException) -> MonitoringFailureReason | None:
    """只识别锁版固定关闭信号与网络异常；端点/协议/投影异常保持整轮失败。"""
    if isinstance(exc, MCPError):
        if exc.code == -32001:
            return "timeout"
        if _connection_lost(exc):
            return "unavailable"
        return None
    if isinstance(exc, httpx2.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx2.NetworkError):
        return "unavailable"
    if isinstance(exc, BaseExceptionGroup):
        reasons = {_read_transport_failure(item) for item in exc.exceptions}
        return reasons.pop() if len(reasons) == 1 else None
    return _read_transport_failure(exc.__cause__) if exc.__cause__ is not None else None


def _prometheus_query_failure(
    result: CallToolResult, tool_name: str = "query"
) -> MonitoringFailureReason | None:
    """仅识别官方 v0.18.0 的单段错误类别；远端原文和其他 is_error 不交模型。"""
    if (
        not result.is_error
        or result.result_type != "complete"
        or result.structured_content is not None
        or len(result.content) != 1
    ):
        return None
    item = result.content[0]
    if not isinstance(item, TextContent):
        return None
    prefix = _PROMETHEUS_ERROR_PREFIXES.get(tool_name)
    if prefix is None or not item.text.startswith(prefix):
        return None
    matched = _PROMETHEUS_READ_ERROR.fullmatch(item.text[len(prefix) :])
    if matched is None:
        return None
    return "auth" if matched.group(1) is not None else "upstream_5xx"


def _grafana_text(result: CallToolResult) -> str | None:
    if (
        result.result_type == "complete"
        and result.structured_content is None
        and len(result.content) == 1
        and isinstance(result.content[0], TextContent)
    ):
        return result.content[0].text
    return None


def _grafana_read_failure(
    result: CallToolResult, tool_name: str, arguments: dict[str, object]
) -> MonitoringFailureReason | None:
    """只认 v2.0.2 选中工具的完整 HTTP 错误格式，不按任意文本里的状态码猜类别。"""
    text = _grafana_text(result)
    if not result.is_error or text is None:
        return None
    if tool_name == "search_dashboards":
        prefix = (
            r"search dashboards for &\{GrafanaHTTPAPI:0x[0-9a-f]+ "
            r"PublicURL:[^\s{}]* Version:[^\s{}]*\}: "
        )
        path, operation, typed = "/search", "search", "search"
    elif tool_name in {"get_dashboard_summary", "get_dashboard_panel_queries"}:
        namespace = re.fullmatch(
            r"get dashboard by uid: resolve grafana namespace from /api/frontend/settings: "
            r"frontend settings returned HTTP (401|403|5[0-9][0-9])",
            text,
        )
        if namespace is not None:
            return "auth" if namespace[1] in {"401", "403"} else "upstream_5xx"
        uid = str(arguments["uid"])
        prefixes = (
            re.escape("determine dashboard.grafana.app capability: "),
            re.escape(f"get dashboard {json.dumps(uid, ensure_ascii=False)} via k8s api: "),
            r"get native v2(?:(?:alpha|beta)[1-9][0-9]*)? dashboard via k8s api "
            + re.escape(uid)
            + r": ",
        )
        native = re.fullmatch(
            r"get dashboard by uid: (?:" + "|".join(prefixes) + r")"
            r"kubernetes API error: (?P<status>401|403|5[0-9][0-9]) (?P<phrase>[A-Za-z ]+) "
            r"\(HTTP (?P=status)\): (?P<body>\{[^\r\n]*\})",
            text,
        )
        if native is not None:
            try:
                http_status = HTTPStatus(int(native["status"]))
                body = json.loads(native["body"])
            except ValueError:
                return None
            if native["phrase"] != http_status.phrase or not isinstance(body, dict):
                return None
            return "auth" if http_status in {401, 403} else "upstream_5xx"
        prefix = re.escape(f"get dashboard by uid: get dashboard by uid {arguments['uid']}: ")
        path, operation, typed = "/dashboards/uid/{uid}", "getDashboardByUID", "getDashboardByUid"
    elif tool_name == "list_datasources":
        prefix = re.escape("list datasources: ")
        path, operation, typed = "/datasources", "getDataSources", "getDataSources"
    elif tool_name == "get_annotations":
        prefix = re.escape("get annotations: ")
        path, operation, typed = "/annotations", "getAnnotations", "getAnnotations"
    else:
        return None
    matched = re.fullmatch(prefix + r"(?P<error>[^\r\n]+)", text)
    if matched is None:
        return None
    error = matched["error"]
    endpoint = f"[GET {path}]"
    for status, suffix in ((401, "Unauthorized"), (403, "Forbidden"), (500, "InternalServerError")):
        start = f"{endpoint}[{status}] {typed}{suffix} "
        if error.startswith(start):
            try:
                body = json.loads(error[len(start) :])
            except ValueError:
                return None
            return (
                ("auth" if status in {401, 403} else "upstream_5xx")
                if isinstance(body, dict)
                else None
            )
    generic = re.fullmatch(
        re.escape(f"{endpoint} {operation} (status ") + r"(401|403|5[0-9][0-9])\): \{\}", error
    )
    if generic is None:
        return None
    return "auth" if generic[1] in {"401", "403"} else "upstream_5xx"


def _promql_error_position(result: CallToolResult, tool_name: str) -> tuple[int, int] | None:
    """仅锁版、单段且完整匹配的 query bad_data；step/selector/执行资源错误不可冒充语法错误。"""
    if (
        not result.is_error
        or result.result_type != "complete"
        or result.structured_content is not None
        or len(result.content) != 1
        or not isinstance(result.content[0], TextContent)
    ):
        return None
    prefix = _PROMETHEUS_ERROR_PREFIXES.get(tool_name)
    if prefix is None or tool_name not in {"query", "range_query"}:
        return None
    text = result.content[0].text
    if not text.startswith(prefix):
        return None
    matched = _PROMQL_SYNTAX.fullmatch(text[len(prefix) :])
    return (int(matched[1]), int(matched[2])) if matched else None


def _bindings(
    configs: tuple[MCPServerConfig, ...], governance: GovernedTools
) -> dict[str, _Binding]:
    """登记必须与工具目录一一对应：契约存在、策略一致、策略提供结果模型、SDK 名字不冲突。"""
    server_ids = [config.server_id for config in configs]
    if len(set(server_ids)) != len(server_ids):
        raise ValueError("MCP 登记：server_id 重复")
    catalog = governance.catalog
    bindings: dict[str, _Binding] = {}
    for config in configs:
        for remote_name, policy_id in config.allowed_tools.items():
            # MCP 工具只有一个目标：同一工具登记在多个目标上时不能确定发往哪个 Server。
            registered = catalog.contracts_for(config.tool_id(remote_name))
            contract = registered[0] if len(registered) == 1 else None
            if contract is None or contract.policy_id != policy_id:
                raise ValueError(f"MCP 登记：{config.tool_id(remote_name)} 与工具目录不一致")
            result = catalog.policy_for(contract).result
            if result is None:
                raise ValueError(f"MCP 登记：策略 {policy_id} 没有结果模型")
            bindings[config.sdk_tool_name(remote_name)] = _Binding(
                config, remote_name, contract, result
            )
    # 其他工具（本地工具）以 tool_id 的名字部分作为 SDK 函数名。
    mcp_tool_ids = {binding.contract.tool_id for binding in bindings.values()}
    other_names = {
        contract.tool_id.split("/", 1)[1]
        for contract in catalog.contracts
        if contract.tool_id not in mcp_tool_ids
    }
    if clash := other_names & bindings.keys():
        raise ValueError(f"MCP 登记：SDK 工具名与其他工具冲突：{sorted(clash)}")
    return bindings


def _verified(
    listed: list[MCPTool], planned: dict[str, _Binding], config: MCPServerConfig
) -> list[str]:
    """本 Server 上与登记一致的工具：远端恰有一个同名工具，且接受本地的参数形状。

    远端生成的标题/说明各不相同；远端对象是否接受额外字段也不影响调用，因为发出的参数总是
    先经策略参数模型（禁止额外字段）校验。远端可多出本地不开放的可选字段；新增必填
    字段、映射的值类型等其他差异一律视为不符。
    """
    verified = []
    for name, binding in planned.items():
        if binding.config is not config:
            continue
        matches = [tool for tool in listed if tool.name == binding.remote_name]
        if len(matches) == 1 and _accepts_input(
            binding.contract.input_schema, matches[0].input_schema
        ):
            verified.append(name)
        else:
            logger.warning("MCP 工具 %s 未发现或契约不符，已隐藏", binding.contract.tool_id)
    return verified


def _accepts_input(local: dict[str, object], remote: dict[str, object]) -> bool:
    """远端须接收本地字段；简单对象可多出本地不发送的可选字段。

    不校验这些额外字段的 schema（即使含无效 ``$ref``）：本地契约不向模型展示它们，
    严格参数校验也不允许发送它们。远端默认值仍可能改变行为，写工具启用前须另行核对。
    """
    expected = schema_shape(local, ignore_extra_flags=True)
    offered = schema_shape(remote, ignore_extra_flags=True)
    if expected == offered:
        return True
    if not isinstance(expected, dict) or not isinstance(offered, dict):
        return False
    simple_object = {"type", "properties", "required"}
    if set(offered) == {"type", "properties"}:
        offered = {**offered, "required": []}
    if set(expected) != simple_object or set(offered) != simple_object:
        return False
    if expected["type"] != "object" or offered["type"] != "object":
        return False
    local_fields, remote_fields = expected["properties"], offered["properties"]
    local_required, remote_required = expected["required"], offered["required"]
    if (
        not isinstance(local_fields, dict)
        or not isinstance(remote_fields, dict)
        or not isinstance(local_required, list)
        or not isinstance(remote_required, list)
        or not all(isinstance(name, str) for name in local_required)
        or not all(isinstance(name, str) for name in remote_required)
        or len(set(local_required)) != len(local_required)
        or len(set(remote_required)) != len(remote_required)
        or set(local_required) != set(local_fields)
        or not set(remote_required) <= set(local_fields) <= set(remote_fields)
    ):
        return False
    return all(_accepts_field(local_fields[name], remote_fields[name]) for name in local_fields)


def _accepts_field(local: object, remote: object) -> bool:
    if local == remote:
        return True
    return (
        isinstance(local, dict)
        and isinstance(remote, dict)
        and local.get("type") == "array"
        and remote.get("type") in (["null", "array"], ["array", "null"])
        and local == {**remote, "type": "array"}
    )


def _client_factory(
    config: MCPServerConfig, authorization: str | None
) -> Callable[..., httpx2.AsyncClient]:
    endpoint = httpx2.URL(config.url)

    def factory(
        headers: dict[str, str] | None = None, timeout: object = None, auth: object = None
    ) -> httpx2.AsyncClient:
        # SDK 传入的期限与认证不采用：期限来自登记，认证只由 transport 加到登记端点上。
        transport = _EndpointTransport(
            httpx2.AsyncHTTPTransport(trust_env=False, retries=0),
            endpoint=endpoint,
            authorization=authorization,
            max_response_bytes=config.max_response_bytes,
        )
        return httpx2.AsyncClient(
            headers=headers,
            # 单次调用的期限由 SDK 会话超时执行，并会中止对应的 POST；HTTP 读取期限只作兜底，
            # 须更长：它在 MCP 客户端的 POST 任务里触发时会关闭整个连接，使后续调用全部失败。
            timeout=httpx2.Timeout(
                config.timeout_seconds, read=config.timeout_seconds * _READ_TIMEOUT_FACTOR
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    return factory


class _EndpointTransport(httpx2.AsyncBaseTransport):
    """MCP HTTP 的唯一出入口：只发往登记端点，认证只加在这里，响应按实际字节限额。"""

    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        *,
        endpoint: httpx2.URL,
        authorization: str | None,
        max_response_bytes: int,
    ) -> None:
        self._inner = inner
        self._endpoint = endpoint
        self._authorization = authorization
        self._max_response_bytes = max_response_bytes

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        # 比较规范化后的完整 URL（含 userinfo、未解码的路径与 query）：MCP 客户端会跟随同源
        # 重定向，附加 query 或编码不同的路径都可能是同一域名下的其他服务。
        if request.url != self._endpoint:
            raise MCPTransportError("MCP 请求目标不是登记的端点")
        if self._authorization is not None:
            request.headers["authorization"] = self._authorization
        # 压缩内容无法在读取阶段按实际字节限额。
        request.headers["accept-encoding"] = "identity"

        response = await self._inner.handle_async_request(request)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise MCPTransportError("MCP 响应使用了压缩编码")
        stream = response.stream
        if not isinstance(stream, httpx2.AsyncByteStream):
            await response.aclose()
            raise MCPTransportError("MCP 响应不是异步字节流")
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_BoundedStream(stream, self._max_response_bytes),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


class _BoundedStream(httpx2.AsyncByteStream):
    """按实际读取的字节计数，超过上限即停止读取；不依赖可伪造的 Content-Length。"""

    def __init__(self, inner: httpx2.AsyncByteStream, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    async def __aiter__(self) -> AsyncIterator[bytes]:
        received = 0
        async for chunk in self._inner:
            received += len(chunk)
            if received > self._limit:
                raise MCPTransportError("MCP 响应超过接收字节上限")
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


def _payload(result: CallToolResult, model: type[BaseModel]) -> dict[str, object]:
    """只接受登记的 JSON 结果契约：结构化内容，或唯一一段 JSON 对象文本；其他一律拒绝。

    资源链接、图片等内容类型不读取。结果按 JSON 严格模式校验：不做字符串转数字之类的类型
    转换；校验调用强制忽略未声明字段（含嵌套模型，不论模型自身配置），输出经
    ``contract_dump`` 按声明字段与类型生成，结果模型的计算字段、serializer 与 validator 都不能
    加入其他内容。
    """
    if result.is_error or any(not isinstance(item, TextContent) for item in result.content):
        raise ValueError("MCP 结果不符合登记契约")
    if result.structured_content is not None:
        raw = json.dumps(result.structured_content)
    else:
        texts = [item.text for item in result.content if isinstance(item, TextContent)]
        if len(texts) != 1:
            raise ValueError("MCP 结果不符合登记契约")
        raw = texts[0]
    return contract_dump(model, model.model_validate_json(raw, strict=True, extra="ignore"))


def _prometheus_payload(
    result: CallToolResult, model: type[BaseModel], tool_name: str
) -> tuple[dict[str, object], bool]:
    if result.result_type != "complete":
        raise ValueError("MCP 结果不符合登记契约")
    if tool_name == "metric_metadata":
        if (
            result.is_error
            or result.structured_content is not None
            or len(result.content) != 1
            or not isinstance(result.content[0], TextContent)
        ):
            raise ValueError("MCP 结果不符合登记契约")
        raw = result.content[0].text.lstrip()
        metadata, end = json.JSONDecoder().raw_decode(raw)
        tail = raw[end:]
        truncated = bool(tail and _TRUNCATION.fullmatch(tail))
        if tail.strip() and not truncated:
            raise ValueError("MCP 元数据结果尾部不符合锁版契约")
        payload = contract_dump(
            model,
            model.model_validate_json(
                json.dumps({"metadata": metadata}), strict=True, extra="ignore"
            ),
        )
        return payload, truncated
    payload = _payload(result, model)
    text = payload.get("result")
    return payload, isinstance(text, str) and _TRUNCATION.search(text) is not None


def _grafana_payload(
    result: CallToolResult, model: type[BaseModel], tool_name: str, arguments: dict[str, object]
) -> tuple[dict[str, object], bool]:
    """锁版只读结果的薄投影；身份/查询范围由实际请求提供，不接受远端同名字段覆盖。"""
    text = _grafana_text(result)
    if result.is_error or text is None:
        raise ValueError("Grafana MCP 结果不符合锁版契约")
    data = json.loads(text)
    if tool_name == "get_dashboard_panel_queries":
        if not isinstance(data, list):
            raise ValueError("Grafana 面板查询不是数组")
        data = {"uid": arguments["uid"], "panelId": arguments.get("panelId", 0), "panels": data}
    elif tool_name == "get_annotations":
        if not isinstance(data, dict) or "Payload" not in data:
            raise ValueError("Grafana 标记结果缺少 Payload")
        data = {
            "from": arguments["from"],
            "to": arguments["to"],
            "dashboardUid": arguments.get("dashboardUid", ""),
            "annotations": data["Payload"],
        }
    elif not isinstance(data, dict):
        raise ValueError("Grafana MCP 结果不是对象")
    if tool_name == "get_dashboard_summary" and data.get("uid") != arguments["uid"]:
        raise ValueError("Grafana 仪表盘 UID 与请求不符")
    for key in ("limit", "page", "offset"):
        if key in arguments and tool_name in {"search_dashboards", "list_datasources"}:
            data[key] = arguments[key]
    payload = contract_dump(
        model, model.model_validate_json(json.dumps(data), strict=True, extra="ignore")
    )
    truncated = payload.get("hasMore") is True
    if tool_name == "get_annotations":
        annotations = payload["annotations"]
        limit = arguments["limit"]
        if not isinstance(limit, int):
            raise ValueError("Grafana 标记 limit 不符合契约")
        truncated = isinstance(annotations, list) and len(annotations) >= limit
    return payload, truncated


class _WireSafeFactory:
    """LogRecord 工厂包装：源自 MCP 库的记录只保留 logger 名字、级别与异常类型。"""

    def __init__(self, previous: Callable[..., logging.LogRecord]) -> None:
        self._previous = previous

    def __call__(
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: object,
        exc_info: object,
        func: str | None = None,
        sinfo: str | None = None,
        **kwargs: object,
    ) -> logging.LogRecord:
        if fn.startswith(_WIRE_SOURCE):
            exc = exc_info[1] if isinstance(exc_info, tuple) else None
            msg = "MCP 库日志（内容已省略）" + (
                "" if exc is None else f"，异常 {type(exc).__name__}"
            )
            args, exc_info, sinfo = (), None, None
        return self._previous(name, level, fn, lno, msg, args, exc_info, func, sinfo, **kwargs)


def _contain_wire_logs() -> None:
    """包装当前的 LogRecord 工厂（已包装时不重复），使源自 MCP 库的日志记录不携带任何内容。"""
    current = logging.getLogRecordFactory()
    if not isinstance(current, _WireSafeFactory):
        logging.setLogRecordFactory(_WireSafeFactory(current))
