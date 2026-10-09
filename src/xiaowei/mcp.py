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

import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import httpx2
import mcp
from agents import FunctionTool, Tool
from agents.mcp import MCPServerStreamableHttp
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool
from pydantic import BaseModel, SecretStr

from xiaowei.config import MCPServerConfig, resolve_secret_ref
from xiaowei.governance import GovernedTools, contract_dump, schema_shape
from xiaowei.models import RunContext, ToolContract, ToolObservation, ToolRequest
from xiaowei.tools import governed_function_tool

logger = logging.getLogger(__name__)

_READ_TIMEOUT_FACTOR = 2

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
        # 连接之前构造全部工具：SDK 在构造时做严格 schema 转换，不支持的参数形状在这里作为
        # 登记错误拒绝，而不是在某个 Server 连上之后中止整个接入。
        self._prepared = {
            name: self._function_tool(name, binding) for name, binding in self._planned.items()
        }
        self._servers: dict[str, MCPServerStreamableHttp] = {}
        self._tools: dict[str, FunctionTool] = {}
        self._status: dict[str, str] = {config.server_id: "unavailable" for config in self._configs}
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> MCPIntegration:
        if self._stack is not None:
            raise RuntimeError("MCPIntegration 不能重复进入")
        stack = AsyncExitStack()
        try:
            for config in self._configs:
                await self._start(config, stack)
        except BaseException:
            self._tools.clear()
            self._servers.clear()
            await stack.aclose()
            raise
        self._stack = stack
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._tools.clear()
        self._servers.clear()
        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()

    @property
    def governance(self) -> GovernedTools:
        return self._governance

    @property
    def available_tool_ids(self) -> frozenset[str]:
        """已连接并核对通过的远端工具的 ``tool_id``。"""
        return frozenset(self._planned[name].contract.tool_id for name in self._tools)

    @property
    def source_status(self) -> dict[str, str]:
        """供就绪检查读取的安全源状态；不含地址、认证或上游内容。"""
        return dict(self._status)

    def tools_for(self, ctx: RunContext) -> list[Tool]:
        """本轮可展示的 MCP 工具：已核对的远端工具与本轮治理范围的交集；调用时仍复核。"""
        allowed = {contract.tool_id for contract in self._governance.allowed_contracts(ctx)}
        return [
            tool
            for name, tool in self._tools.items()
            if self._planned[name].contract.tool_id in allowed
        ]

    async def _start(self, config: MCPServerConfig, stack: AsyncExitStack) -> None:
        _contain_wire_logs()
        try:
            authorization = (
                None
                if config.auth_ref is None
                else f"Bearer {self._resolve_secret(config.auth_ref).get_secret_value()}"
            )
        except Exception:
            logger.warning("MCP Server %s 的认证引用无法解析，已隐藏其工具", config.server_id)
            return
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
        # 远端相关的处理都在本 Server 的边界内：SDK 在连接失败时自行清理；之后的失败由本
        # Server 自己的栈关闭连接，只隐藏它的工具。
        connection = AsyncExitStack()
        try:
            await connection.enter_async_context(server)
            verified = _verified(await server.list_tools(), self._planned, config)
        except Exception as exc:
            await connection.aclose()
            # 只记录类型：下层异常消息可能包含远端返回的内容。
            logger.warning(
                "MCP Server %s 不可用（%s），已隐藏其工具", config.server_id, type(exc).__name__
            )
            return
        stack.push_async_callback(connection.aclose)
        self._servers[config.server_id] = server
        self._status[config.server_id] = "available" if verified else "contract_mismatch"
        for name in verified:
            self._tools[name] = self._prepared[name]

    def _function_tool(self, name: str, binding: _Binding) -> FunctionTool:
        async def execute(request: ToolRequest) -> ToolObservation:
            # 只有已连接并核对过的 Server 的工具会展示；关闭之后的调用按执行失败处理。
            server = self._servers[binding.config.server_id]
            try:
                result = await server.call_tool(binding.remote_name, request.arguments)
            except Exception:
                self._status[binding.config.server_id] = "unavailable"
                raise
            payload = _payload(result, binding.result)
            if binding.contract.policy_id == "prometheus.query":
                # 只记录治理层规范化后实际发出的表达式；远端返回中的同名字段不能冒充它。
                payload["query"] = request.arguments["query"]
            return ToolObservation(
                payload=payload,
                captured_at=self._clock(),
                truncated=False,
            )

        return governed_function_tool(name, binding.contract, self._governance, execute)


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
    return all(local_fields[name] == remote_fields[name] for name in local_fields)


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
