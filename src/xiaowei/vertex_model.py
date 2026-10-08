"""Vertex Express Mode ``generateContent`` 的 SDK ``Model`` 适配器。

只做 SDK 输入输出与 Vertex REST 的双向映射：不执行工具、不续轮、不重试，Agent Loop 仍是 SDK
``Runner``。请求只来自本次调用的参数（不在实例上保存调用映射，并发运行互不影响）；响应只接受单
候选、完整 ``STOP``、纯文本或恰好一个带 thought signature 的函数调用，其余在交给 Runner 之前拒绝。

传输层（期限、字节上限、无重定向、无压缩、API Key 请求头）由 ``model_api`` 的受控 transport 负责，
这里得到的成功响应已是合法 JSON 且是单候选 ``STOP`` 终态。适配器再按严格 JSON 解析一次（拒绝
``NaN``/``Infinity`` 与溢出为无穷的数字），并在任何集合或字典查找前检查字段的运行时类型。
"""

import binascii
import json
import math
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any, NoReturn

import httpx2
from agents import (
    AgentOutputSchemaBase,
    FunctionTool,
    Handoff,
    Model,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Tool,
    TResponseInputItem,
    Usage,
)
from agents.items import TResponseStreamEvent
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam
from openai.types.responses.response_usage import OutputTokensDetails

from xiaowei.model_api import (
    ModelAPIRejectedError,
    ModelRequestRejectedError,
    ModelResponseRejectedError,
)

# Vertex 不返回调用 ID 时由适配器生成；带此前缀的 ID 从未发给 Vertex，回放时也不发送。
_GENERATED_CALL_ID_PREFIX = "xw-vertex-"
# signature 是带填充的 base64 opaque 字符串（标准或 URL 安全字母表，不混用），只校验、原样回传；
# 长度上限防止单个字段占满会话历史。
_URL_SAFE = str.maketrans("-_", "+/")
_MAX_SIGNATURE_CHARS = 65_536
_MAX_CALL_ID_CHARS = 128
_FAKE_ID = "__fake_id__"

# 适配器能如实表达的 SDK 设置；其余设置一旦取值即拒绝，不静默丢弃。
_MAPPED_SETTINGS = frozenset({"max_tokens", "parallel_tool_calls", "retry", "timeout", "store"})
_REPORTING_SETTINGS = frozenset({"preserve_raw_usage"})
_USAGE_COUNTS = (
    "promptTokenCount",
    "candidatesTokenCount",
    "thoughtsTokenCount",
    "totalTokenCount",
)


class ModelAPIStatusError(ModelAPIRejectedError):
    """模型服务返回非成功状态；只保留状态码，不含上游正文。"""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"模型服务返回 HTTP {status_code}")
        self.status_code = status_code


class ModelAPITransportError(ModelAPIRejectedError):
    """连接、超时或读取失败；不含地址、凭据或上游信息。"""


class ModelAPITimeoutError(ModelAPITransportError):
    """模型请求超时；用类型区分，日志不解析异常文字。"""


class VertexModel(Model):
    """非流式 Vertex 适配器；HTTP 客户端由 ``open_model`` 装配并负责关闭。"""

    def __init__(self, client: httpx2.AsyncClient, endpoint: str) -> None:
        self._client = client
        self._endpoint = endpoint

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        if previous_response_id is not None or conversation_id is not None or prompt is not None:
            _reject_request("Vertex 不使用服务端会话或提示词模板")
        if handoffs:
            _reject_request("Vertex 适配器不支持 handoff")
        if output_schema is None or output_schema.is_plain_text():
            _reject_request("Vertex 请求必须携带结构化最终输出约束")
        _check_settings(model_settings)
        functions = _function_tools(tools)
        generation: dict[str, Any] = {
            "candidateCount": 1,
            "responseMimeType": "application/json",
            "responseJsonSchema": output_schema.json_schema(),
        }
        if model_settings.max_tokens is not None:
            generation["maxOutputTokens"] = model_settings.max_tokens
        body: dict[str, Any] = {"contents": _contents(input), "generationConfig": generation}
        if functions:
            body["tools"] = [{"functionDeclarations": [_declaration(t) for t in functions]}]
        if system_instructions:
            body["systemInstruction"] = {"parts": [{"text": system_instructions}]}

        payload = await self._post(body)
        output, usage, raw_usage = _model_output(payload, {t.name for t in functions})
        return ModelResponse(
            output=output,
            usage=usage,
            response_id=None,
            raw_usage=raw_usage if model_settings.preserve_raw_usage else None,
        )

    async def _post(self, body: dict[str, Any]) -> object:
        try:
            response = await self._client.post(self._endpoint, json=body)
        except httpx2.TimeoutException:
            raise ModelAPITimeoutError("模型请求超时") from None
        except httpx2.TransportError:
            raise ModelAPITransportError("模型服务连接失败") from None
        if not httpx2.codes.is_success(response.status_code):
            raise ModelAPIStatusError(response.status_code) from None
        # 受控 transport 已确认是 JSON；这里再拒绝非有限数字，避免它们进入工具参数或下一步请求。
        payload = _strict_json(response.content)
        if payload is None:
            _reject_response("模型成功响应不是严格 JSON")
        return payload

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        _reject_request("Vertex 适配器只支持非流式运行")
        yield  # pragma: no cover - 使本方法成为异步生成器


# ---- 请求映射 ---------------------------------------------------------------------------------


def _reject_request(message: str) -> NoReturn:
    raise ModelRequestRejectedError(message)


def _reject_response(message: str) -> NoReturn:
    raise ModelResponseRejectedError(message)


def _check_settings(settings: ModelSettings) -> None:
    for name, value in vars(settings).items():
        if value is None or name in _REPORTING_SETTINGS:
            continue
        if name not in _MAPPED_SETTINGS:
            _reject_request("Vertex 适配器不支持该模型设置")
    if settings.parallel_tool_calls or settings.store:
        _reject_request("Vertex 适配器不支持并行工具调用或服务端保存")
    if settings.retry is not None and settings.retry.max_retries:
        _reject_request("Vertex 适配器不自动重试")


def _function_tools(tools: list[Tool]) -> list[FunctionTool]:
    functions: list[FunctionTool] = []
    for tool in tools:
        if not isinstance(tool, FunctionTool):
            _reject_request("Vertex 适配器只支持 function tool")
        functions.append(tool)
    return functions


def _declaration(tool: FunctionTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parametersJsonSchema": tool.params_json_schema,
    }


def _contents(input: str | list[TResponseInputItem]) -> list[dict[str, Any]]:
    """按输入顺序映射；调用结果只按同一输入中的 ``call_id → 函数名`` 关联。"""
    if isinstance(input, str):
        return [_text_content("user", input)]
    contents: list[dict[str, Any]] = []
    calls: dict[str, str] = {}
    for item in input:
        raw = item if isinstance(item, Mapping) else None
        if raw is None:
            _reject_request("Vertex 适配器不支持该输入项")
        kind = raw.get("type", "message")
        if kind == "message":
            contents.append(_message(raw))
        elif kind == "function_call":
            call_id = _string(raw.get("call_id"))
            name = _string(raw.get("name"))
            if call_id in calls:
                _reject_request("输入中的调用 ID 重复")
            calls[call_id] = name
            contents.append({"role": "model", "parts": [_function_call_part(raw, call_id, name)]})
        elif kind == "function_call_output":
            call_id = _string(raw.get("call_id"))
            if call_id not in calls:
                _reject_request("工具结果没有对应的函数调用")
            output = raw.get("output")
            if not isinstance(output, str):
                _reject_request("Vertex 适配器只回传文本工具结果")
            response: dict[str, Any] = {"name": calls[call_id], "response": {"output": output}}
            if not call_id.startswith(_GENERATED_CALL_ID_PREFIX):
                response = {"id": call_id, **response}
            contents.append({"role": "user", "parts": [{"functionResponse": response}]})
        else:
            _reject_request("Vertex 适配器不支持该输入项")
    return contents


_ROLES: dict[object, str] = {"user": "user", "assistant": "model"}


def _message(raw: Mapping[str, Any]) -> dict[str, Any]:
    raw_role = raw.get("role")
    role = _ROLES.get(raw_role) if isinstance(raw_role, str) else None
    if role is None:
        _reject_request("Vertex 适配器只映射用户与助手消息")
    content = raw.get("content")
    if isinstance(content, str):
        return _text_content(role, content)
    if not isinstance(content, list) or not content:
        _reject_request("Vertex 适配器只映射文本消息")
    texts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping) or part.get("type") not in ("input_text", "output_text"):
            _reject_request("Vertex 适配器只映射文本消息")
        texts.append(_string(part.get("text"), allow_empty=True))
    return {"role": role, "parts": [{"text": text} for text in texts]}


def _text_content(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "parts": [{"text": text}]}


def _function_call_part(raw: Mapping[str, Any], call_id: str, name: str) -> dict[str, Any]:
    args = _strict_json(_string(raw.get("arguments"), allow_empty=True) or "{}")
    if not isinstance(args, dict):
        _reject_request("函数调用参数不是 JSON 对象")
    call: dict[str, Any] = {"name": name, "args": args}
    if not call_id.startswith(_GENERATED_CALL_ID_PREFIX):
        call = {"id": call_id, **call}
    part: dict[str, Any] = {"functionCall": call}
    provider_data = raw.get("provider_data")
    if isinstance(provider_data, Mapping) and "thought_signature" in provider_data:
        signature = provider_data["thought_signature"]
        if not _valid_signature(signature):
            _reject_request("函数调用的 thought signature 无效")
        part["thoughtSignature"] = signature
    return part


def _string(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        _reject_request("Vertex 适配器收到无效的输入项")
    return value


def _valid_signature(value: object) -> bool:
    # 非 ASCII 字符会让 a2b_base64 抛普通 ValueError，先在这里拒绝。
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= _MAX_SIGNATURE_CHARS
        or not value.isascii()
    ):
        return False
    if any(c in value for c in "+/") and any(c in value for c in "-_"):
        return False  # 混用两种字母表
    try:
        decoded = binascii.a2b_base64(value.translate(_URL_SAFE), strict_mode=True)
    except binascii.Error:
        return False
    # strict_mode 不拒绝多余的 "="；带填充的编码长度只能是 4 * ceil(字节数 / 3)。
    return len(value) == 4 * -(-len(decoded) // 3)


def _reject_constant(literal: str) -> NoReturn:
    raise ValueError("non-finite JSON number")


def _finite_float(literal: str) -> float:
    value = float(literal)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def _strict_json(text: str | bytes) -> object | None:
    """严格解析 JSON；不合法或含非有限数字时返回 ``None``，由调用方给出固定错误。"""
    try:
        value: object = json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)
    except ValueError:
        return None
    return value


# ---- 响应映射 ---------------------------------------------------------------------------------


def _model_output(
    payload: object, tool_names: set[str]
) -> tuple[list[Any], Usage, dict[str, Any] | None]:
    # 单候选与 STOP 终态只由受控 transport 检查一次；这里仅做类型收窄。
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not isinstance(candidates, list) or len(candidates) != 1:
        _reject_response("模型响应不是单个候选")
    (candidate,) = candidates
    if not isinstance(candidate, dict):
        _reject_response("模型响应不是单个候选")
    content = candidate.get("content")
    if not isinstance(content, dict) or content.get("role", "model") != "model":
        _reject_response("模型响应没有可用的内容")
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        _reject_response("模型响应没有可用的内容")

    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            _reject_response("模型响应包含不支持的内容")
        keys = set(part) - {"thoughtSignature"}
        if keys == {"text"} and isinstance(part["text"], str):
            texts.append(part["text"])  # 文本上的 signature 可省略回传，不进入 Session
        elif keys == {"functionCall"}:
            calls.append(part)
        else:
            _reject_response("模型响应包含不支持的内容")
    if calls and texts:
        _reject_response("模型响应同时包含文本与函数调用")
    if len(calls) > 1:
        _reject_response("模型一次返回了多个函数调用")

    output: list[Any]
    if calls:
        output = [_function_call_item(calls[0], tool_names)]
    else:
        output = [
            ResponseOutputMessage(
                id=_FAKE_ID,
                type="message",
                role="assistant",
                status="completed",
                content=[
                    ResponseOutputText(type="output_text", text="".join(texts), annotations=[])
                ],
            )
        ]
    usage, raw_usage = _usage(payload.get("usageMetadata"))
    return output, usage, raw_usage


def _function_call_item(part: dict[str, Any], tool_names: set[str]) -> ResponseFunctionToolCall:
    call = part["functionCall"]
    if not isinstance(call, dict):
        _reject_response("模型返回的函数调用无效")
    name = call.get("name")
    if not isinstance(name, str) or name not in tool_names:
        _reject_response("模型调用了本轮未提供的工具")
    args = call.get("args", {})
    if not isinstance(args, dict):
        _reject_response("模型返回的函数调用参数不是 JSON 对象")
    signature = part.get("thoughtSignature")
    if not _valid_signature(signature):
        _reject_response("模型返回的函数调用缺少有效的 thought signature")
    call_id = call.get("id")
    if call_id is None:
        call_id = _GENERATED_CALL_ID_PREFIX + uuid.uuid4().hex
    elif (
        not isinstance(call_id, str)
        or not 0 < len(call_id) <= _MAX_CALL_ID_CHARS
        or call_id.startswith(_GENERATED_CALL_ID_PREFIX)
    ):
        _reject_response("模型返回的函数调用 ID 无效")
    # ``provider_data`` 是 SDK 在调用项上保存供应商附加字段的约定（同 SDK 的 Chat 转换器），
    # 不是 OpenAI 类型声明的字段，因此按关键字字典传入。
    fields: dict[str, Any] = {
        "id": _FAKE_ID,
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
        "provider_data": {"thought_signature": signature},
    }
    return ResponseFunctionToolCall(**fields)


def _usage(metadata: object) -> tuple[Usage, dict[str, Any] | None]:
    if not isinstance(metadata, dict):
        return Usage(requests=1), None
    counts = {
        key: value
        for key in _USAGE_COUNTS
        if isinstance(value := metadata.get(key), int) and not isinstance(value, bool)
    }
    prompt = counts.get("promptTokenCount", 0)
    thoughts = counts.get("thoughtsTokenCount", 0)
    output = counts.get("candidatesTokenCount", 0) + thoughts
    usage = Usage(
        requests=1,
        input_tokens=prompt,
        output_tokens=output,
        total_tokens=counts.get("totalTokenCount", prompt + output),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=thoughts),
    )
    return usage, counts or None
