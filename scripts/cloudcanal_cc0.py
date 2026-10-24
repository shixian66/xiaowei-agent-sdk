"""公司现场 CC0 取证；默认只列目录，显式选择已确认只读的工具才单次调用。

用法见 README 的 CloudCanal CC0 小节。本脚本不进入产品入口、不调用模型。
"""

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
from collections.abc import AsyncIterator
from importlib.metadata import version
from pathlib import Path
from typing import Any, Never, cast
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx2
from agents import set_tracing_disabled
from agents.mcp import MCPServerStreamableHttp
from mcp.types import CallToolResult

SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "items",
    "prefixItems",
    "additionalProperties",
    "$defs",
    "definitions",
    "dependentSchemas",
    "$ref",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
}


class ProbeError(Exception):
    """只使用本地固定原因码，不传播远端异常文字。"""


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ProbeError("invalid_configuration")


class BoundedStream(httpx2.AsyncByteStream):
    def __init__(
        self, inner: httpx2.AsyncByteStream, transport: "ProbeTransport", *, closing: bool
    ) -> None:
        self.inner, self.transport = inner, transport
        self.closing = closing

    async def __aiter__(self) -> AsyncIterator[bytes]:
        received = 0
        try:
            async with asyncio.timeout_at(self.transport.deadline):
                async for chunk in self.inner:
                    self.transport.check(closing=self.closing)
                    received += len(chunk)
                    self.transport.received += len(chunk)
                    if received > self.transport.limit:
                        self.transport.failure = self.transport.failure or "response_limit"
                        raise ProbeError(self.transport.failure)
                    yield chunk
        except TimeoutError:
            self.transport.failure = self.transport.failure or "timeout"
            raise ProbeError(self.transport.failure) from None

    async def aclose(self) -> None:
        await self.inner.aclose()


class ProbeTransport(httpx2.AsyncHTTPTransport):
    """SDK 可跟随同源跳转；在实际发送边界固定完整端点并限制实际接收字节。"""

    def __init__(self, url: str, limit: int, deadline: float) -> None:
        super().__init__(trust_env=False, retries=0)
        self.endpoint, self.limit = httpx2.URL(url), limit
        self.received = 0
        self.failure: str | None = None
        self.deadline = deadline

    def check(self, *, closing: bool = False) -> None:
        if asyncio.get_running_loop().time() >= self.deadline:
            self.failure = self.failure or "timeout"
            raise ProbeError(self.failure)
        if self.failure and not closing:
            raise ProbeError(self.failure)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        # MCP 的 SSE 续接不受工具零重试控制；失败后只允许期限内同端点的会话关闭。
        closing = request.method == "DELETE"
        self.check(closing=closing)
        if request.url != self.endpoint:
            self.failure = "endpoint_changed"
            raise ProbeError("endpoint_changed")
        request.headers["accept-encoding"] = "identity"
        try:
            async with asyncio.timeout_at(self.deadline):
                response = await super().handle_async_request(request)
        except TimeoutError:
            self.failure = self.failure or "timeout"
            raise ProbeError(self.failure) from None
        if (
            response.is_redirect
            or response.headers.get("content-encoding", "identity") != "identity"
        ):
            self.failure = "redirect_or_encoding_rejected"
            await response.aclose()
            raise ProbeError(self.failure)
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=BoundedStream(
                cast(httpx2.AsyncByteStream, response.stream), self, closing=closing
            ),
            extensions=response.extensions,
        )


def schema_summary(value: object) -> object:
    """保留参数结构/约束；隐藏可能夹带业务内容的说明、示例和枚举值。"""
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if key not in SCHEMA_KEYS:
                continue
            if key in {"properties", "$defs", "definitions", "dependentSchemas"}:
                result[key] = {name: schema_summary(child) for name, child in item.items()}
            elif key == "$ref" and isinstance(item, str) and not item.startswith("#/"):
                result[key] = "<redacted-reference>"
            else:
                result[key] = schema_summary(item)
        return result
    if isinstance(value, list):
        return [schema_summary(item) for item in value]
    return value


def schema_fields(value: object) -> set[str]:
    if isinstance(value, dict):
        fields = set(value.get("properties", {}))
        for item in value.values():
            fields.update(schema_fields(item))
        return fields
    if isinstance(value, list):
        return set().union(*(schema_fields(item) for item in value))
    return set()


def shape(value: object, fields: set[str], depth: int = 0) -> dict[str, Any]:
    """不输出业务叶子值；未在 schema 声明的键也隐藏，避免动态业务键泄漏。"""
    if depth >= 8:
        return {"type": "omitted", "reason": "depth_limit"}
    if isinstance(value, dict):
        return {
            "type": "object",
            "fields": {
                key if key in fields else f"<field-{i}>": shape(item, fields, depth + 1)
                for i, (key, item) in enumerate(value.items(), 1)
            },
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "length": len(value),
            "sampled": min(len(value), 3),
            "items": [shape(item, fields, depth + 1) for item in value[:3]],
        }
    if isinstance(value, str):
        result: dict[str, Any] = {"type": "string", "utf8_bytes": len(value.encode())}
        if value.lstrip().startswith(("{", "[")):
            try:
                result["json_shape"] = shape(json.loads(value), fields, depth + 1)
            except (ValueError, RecursionError):
                pass  # 普通日志/非 JSON 文本仍只保留类型和长度。
        return result
    return {
        "type": "null"
        if value is None
        else "boolean"
        if isinstance(value, bool)
        else "integer"
        if isinstance(value, int)
        else "number"
    }


async def probe(url: str, args: argparse.Namespace, arguments: dict[str, Any]) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + args.timeout
    transport = ProbeTransport(url, args.max_response_bytes, deadline)

    def client_factory(
        headers: dict[str, str] | None = None, timeout: object = None, auth: object = None
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            headers=headers,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx2.Timeout(args.timeout, read=args.timeout * 2),
        )

    report: dict[str, Any] = {
        "format": "cloudcanal-cc0-v1",
        "declared_cloudcanal_version": "6.4.0.2",
        "client_versions": {name: version(name) for name in ("openai-agents", "mcp", "httpx2")},
        "status": "ok",
    }
    phase, started = "connect", time.monotonic()
    try:
        async with (
            asyncio.timeout_at(deadline),
            MCPServerStreamableHttp(
                params={
                    "url": url,
                    "httpx_client_factory": client_factory,
                    "terminate_on_close": True,
                },
                name="cc0",
                client_session_timeout_seconds=args.timeout,
                max_retry_attempts=0,
            ) as server,
        ):
            transport.check()
            report["connect_ms"] = round((time.monotonic() - started) * 1000, 2)
            phase, started, before = "catalog", time.monotonic(), transport.received
            tools = await server.list_tools()
            transport.check()
            report["catalog"] = {
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                "wire_bytes_observed": transport.received - before,
                "tools": [
                    {
                        "name": tool.name,
                        "input_schema": schema_summary(tool.input_schema),
                        "output_schema": schema_summary(tool.output_schema),
                    }
                    for tool in tools
                ],
            }
            if args.read_tool:
                tool = next((tool for tool in tools if tool.name == args.read_tool), None)
                if tool is None:
                    raise ProbeError("tool_not_listed")
                phase, started, before = "call", time.monotonic(), transport.received
                result = await server.call_tool(tool.name, arguments)
                transport.check()
                payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                fields = schema_fields(CallToolResult.model_json_schema()) | schema_fields(
                    tool.output_schema
                )
                report["call"] = {
                    "tool": tool.name,
                    "status": "mcp_error" if result.is_error else "received",
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                    "wire_bytes_observed": transport.received - before,
                    "shape": shape(payload, fields),
                }
                if result.is_error:
                    report["status"] = "failed"
            phase = "close"
        transport.check()
    except Exception as exc:
        report["status"] = "failed"
        report["failure"] = {
            "phase": phase,
            "reason": transport.failure
            or (
                str(exc)
                if isinstance(exc, ProbeError)
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "connection_or_protocol_error"
            ),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
            "wire_bytes_observed": transport.received,
        }
    return report


def main(argv: list[str] | None = None) -> int:
    logging.disable(logging.CRITICAL)
    set_tracing_disabled(True)
    parser = SafeParser(description=__doc__)
    parser.add_argument(
        "--link-env", default="CLOUDCANAL_MCP_URL", help="完整 MCP 链接的环境变量名"
    )
    parser.add_argument("--read-tool", help="用户已确认无业务副作用的准确工具名；省略则只列目录")
    parser.add_argument("--arguments", type=Path, help="该工具的 JSON 参数对象文件；不输出参数值")
    parser.add_argument("--timeout", type=float, default=60, help="连接/读取/关闭的总秒数，默认 60")
    parser.add_argument(
        "--max-response-bytes", type=int, default=1048576, help="每个 HTTP 响应的接收上限"
    )
    try:
        args = parser.parse_args(argv)
        url = os.environ.get(args.link_env, "")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not math.isfinite(args.timeout)
            or args.timeout <= 0
            or args.max_response_bytes <= 0
            or bool(args.read_tool) != bool(args.arguments)
        ):
            raise ProbeError("invalid_configuration")
        arguments = json.loads(args.arguments.read_text()) if args.arguments else {}
        if not isinstance(arguments, dict):
            raise ProbeError("invalid_configuration")
        secret_parts = {
            url,
            unquote(url),
            parsed.query,
            unquote(parsed.query),
        }
        # 短片段常是 mcp/json 等协议词，不据此拒绝报告；仍须人工复核。
        secret_parts.update(
            text
            for part in (*parsed.path.split("/"), *(v for _, v in parse_qsl(parsed.query)))
            for text in (part, unquote(part))
            if len(text) >= 8
        )
        report = asyncio.run(probe(url, args, arguments))
    except (ValueError, OSError, ProbeError, httpx2.InvalidURL):
        print(json.dumps({"status": "failed", "failure": {"reason": "invalid_configuration"}}))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "failed", "failure": {"reason": "interrupted_result_unknown"}}))
        return 130
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if any(
        json.dumps(fragment, ensure_ascii=False)[1:-1] in output
        for fragment in secret_parts
        if fragment
    ):
        print(json.dumps({"status": "failed", "failure": {"reason": "secret_in_output"}}))
        return 1
    print(output)
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
