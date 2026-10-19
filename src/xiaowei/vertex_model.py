"""Vertex Express Mode ``generateContent`` 的 SDK ``Model`` 适配器。

只做 SDK 输入输出与 Vertex REST 的双向映射：不执行工具、不续轮、自身不重试，Agent Loop 仍是 SDK
``Runner``；429/503 的有限重试由 Runner 按 ``ModelSettings.retry`` 重新调用本适配器。请求只来自
本次调用的参数（不在实例上保存调用映射，并发运行互不影响）；响应接受单候选、完整 ``STOP`` 的
文本或一组函数调用，首个调用须有 thought signature。

传输层（期限、字节上限、无重定向、无压缩、API Key 请求头）由 ``model_api`` 的受控 transport 负责，
这里得到的成功响应已是合法 JSON 且是单候选 ``STOP`` 终态。适配器再按严格 JSON 解析一次（拒绝
``NaN``/``Infinity`` 与溢出为无穷的数字），并在任何集合或字典查找前检查字段的运行时类型。
"""

import base64
import binascii
import json
import logging
import math
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
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
    MODEL_RETRY_AFTER_CAP_SECONDS,
    ModelAPIRejectedError,
    ModelRequestRejectedError,
    ModelResponseRejectedError,
    ResponseRejectReason,
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
_RETRY_HEADERS = frozenset({"retry-after", "retry-after-ms"})
_USAGE_COUNTS = (
    "promptTokenCount",
    "candidatesTokenCount",
    "thoughtsTokenCount",
    "totalTokenCount",
)
_SAFE_FIELD = re.compile(r"[A-Za-z0-9._/-]{1,128}\Z")
# google.rpc.Code；只记录公开的枚举名，不回传任意 provider 字符串。
_RPC_CODES = frozenset(
    "OK CANCELLED UNKNOWN INVALID_ARGUMENT DEADLINE_EXCEEDED NOT_FOUND ALREADY_EXISTS "
    "PERMISSION_DENIED RESOURCE_EXHAUSTED FAILED_PRECONDITION ABORTED OUT_OF_RANGE "
    "UNIMPLEMENTED INTERNAL UNAVAILABLE DATA_LOSS UNAUTHENTICATED".split()
)
_QUOTA_REASONS = frozenset({"QUOTA_EXCEEDED", "RATE_LIMIT_EXCEEDED"})
_SAFE_DOMAINS = frozenset({"googleapis.com", "aiplatform.googleapis.com"})
_SAFE_QUOTA_METRICS = frozenset({"aiplatform.googleapis.com/predict_requests"})
_SAFE_QUOTA_IDS = frozenset({"PredictRequestsPerMinute"})
_DURATION = re.compile(r"(\d{1,20})(?:\.(\d{1,9}))?s\Z")
_RETRY_AFTER_NUMBER = re.compile(r"\d{1,4}(?:\.\d{1,3})?\Z")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateDetail:
    rate_kind: str = "unknown"
    status: str = "unknown"
    reason: str = "unknown"
    domain: str = "unknown"
    quota_metric: str = "unknown"
    quota_id: str = "unknown"
    retry_delay_s: float | None = None
    retry_after: str = "unknown"


@dataclass
class VertexTurnMetrics:
    """单轮 Vertex HTTP 计数；不保存请求/响应内容或服务依赖。"""

    model_requests: int = 0
    http_429: int = 0
    retries: int = 0
    request_bytes_max: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    last_429: RateDetail | None = None
    _retry_pending: bool = False
    _successes: int = 0

    def attempt(self, request_bytes: int) -> None:
        if self._retry_pending:
            self.retries += 1
        self._retry_pending = False
        self.model_requests += 1
        self.request_bytes_max = max(self.request_bytes_max, request_bytes)

    def failure(self, status: int, detail: RateDetail | None = None) -> None:
        self._retry_pending = status in (429, 503)
        if status == 429:
            self.http_429 += 1
            self.last_429 = detail

    def success(self, prompt: int | None, candidates: int | None, thoughts: int | None) -> None:
        output = candidates + thoughts if candidates is not None and thoughts is not None else None
        if self._successes == 0:
            self.input_tokens, self.output_tokens = prompt, output
        else:
            self.input_tokens = (
                self.input_tokens + prompt
                if self.input_tokens is not None and prompt is not None
                else None
            )
            self.output_tokens = (
                self.output_tokens + output
                if self.output_tokens is not None and output is not None
                else None
            )
        self._successes += 1


active_vertex_metrics: ContextVar[VertexTurnMetrics | None] = ContextVar(
    "active_vertex_metrics", default=None
)
active_vertex_turn: ContextVar[str | None] = ContextVar("active_vertex_turn", default=None)


def _safe_field(value: object, allowed: frozenset[str]) -> str:
    if value is None:
        return "unknown"
    # 仅检查字符/长度仍会放行 projects/项目号或类 Key 值；只输出已知的公开字段值。
    if isinstance(value, str) and _SAFE_FIELD.fullmatch(value) and value in allowed:
        return value
    return "other"


def _retry_delay(value: object) -> float | None:
    if not isinstance(value, str) or _DURATION.fullmatch(value) is None:
        return None
    delay = float(value[:-1])
    if not math.isfinite(delay) or delay < 0:
        return None
    return min(delay, MODEL_RETRY_AFTER_CAP_SECONDS)


def _safe_retry_after(value: str | None) -> str:
    if value is None:
        return "unknown"
    # SDK 可读原始头；诊断只记有界的数字秒，HTTP-date/任意文本不进入日志。
    if not _RETRY_AFTER_NUMBER.fullmatch(value):
        return "other"
    return f"{min(float(value), MODEL_RETRY_AFTER_CAP_SECONDS):g}"


def _rate_detail(content: bytes, headers: Mapping[str, str]) -> RateDetail:
    retry_after = _safe_retry_after(headers.get("retry-after"))
    payload = _strict_json(content)
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return RateDetail(retry_after=retry_after)
    raw_status = error.get("status")
    status = (
        raw_status
        if isinstance(raw_status, str) and raw_status in _RPC_CODES
        else "unknown"
        if raw_status is None
        else "other"
    )
    details = error.get("details", [])
    if not isinstance(details, list):
        return RateDetail(retry_after=retry_after)
    reason = domain = quota_metric = quota_id = "unknown"
    delay: float | None = None
    quota = False
    malformed = False
    unrecognized_field = False
    for detail in details:
        if not isinstance(detail, dict):
            malformed = True
            break
        kind = detail.get("@type")
        if kind == "type.googleapis.com/google.rpc.ErrorInfo":
            reason = _safe_field(detail.get("reason"), _QUOTA_REASONS)
            domain = _safe_field(detail.get("domain"), _SAFE_DOMAINS)
            unrecognized_field |= "other" in (reason, domain)
            metadata = detail.get("metadata", {})
            if not isinstance(metadata, dict):
                malformed = True
                break
            quota |= reason in _QUOTA_REASONS or any(
                key in metadata for key in ("quota_metric", "quota_limit", "quotaMetric", "quotaId")
            )
        elif kind == "type.googleapis.com/google.rpc.QuotaFailure":
            violations = detail.get("violations", [])
            if not isinstance(violations, list):
                malformed = True
                break
            quota = True
            if violations:
                first = violations[0]
                if not isinstance(first, dict):
                    malformed = True
                    break
                quota_metric = _safe_field(first.get("quotaMetric"), _SAFE_QUOTA_METRICS)
                quota_id = _safe_field(first.get("quotaId"), _SAFE_QUOTA_IDS)
        elif kind == "type.googleapis.com/google.rpc.RetryInfo":
            delay = _retry_delay(detail.get("retryDelay"))
            malformed |= delay is None
        # 其他 google.rpc detail（如 Help）不采集任何字段。
    rate_kind = "unknown"
    if not malformed:
        if quota:
            rate_kind = "quota"
        elif status == "RESOURCE_EXHAUSTED" and not unrecognized_field:
            rate_kind = "no_quota_detail"
    return RateDetail(rate_kind, status, reason, domain, quota_metric, quota_id, delay, retry_after)


def _token_count(metadata: object, key: str) -> int | None:
    value = metadata.get(key) if isinstance(metadata, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def vertex_http_sent(request_bytes: int) -> None:
    """受控 transport 完成请求检查后，记录实际发往底层 transport 的尝试。"""
    if metrics := active_vertex_metrics.get():
        metrics.attempt(request_bytes)


def vertex_response_rejected(status: int, request_bytes: int) -> None:
    """响应被受控 transport 拒绝时，仍记录已经收到的状态码，不读取正文。"""
    if metrics := active_vertex_metrics.get():
        metrics.failure(status, RateDetail() if status == 429 else None)
    logger.info(
        "vertex_http http_status=%d request_bytes=%d outcome=rejected rate_kind=%s turn=%s",
        status,
        request_bytes,
        "unknown" if status == 429 else "-",
        active_vertex_turn.get() or "-",
    )


class ModelAPIStatusError(ModelAPIRejectedError):
    """模型服务返回非成功状态；只保留状态码和重试等待头，不含上游正文。

    ``headers`` 供 SDK Runner 读取 Retry-After（等待上限由重试策略决定）；其他响应头不保留。
    """

    def __init__(self, status_code: int, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(f"模型服务返回 HTTP {status_code}")
        self.status_code = status_code
        self.headers = {
            name: value for name, value in (headers or {}).items() if name.lower() in _RETRY_HEADERS
        }


class ModelAPITransportError(ModelAPIRejectedError):
    """连接、超时或读取失败；不含地址、凭据或上游信息。"""


class ModelAPITimeoutError(ModelAPITransportError):
    """模型请求超时；用类型区分，日志不解析异常文字。"""


class VertexModel(Model):
    """非流式 Vertex 适配器；HTTP 客户端由 ``open_model`` 装配并负责关闭。"""

    def __init__(
        self, client: httpx2.AsyncClient, endpoint: str, *, image_input_enabled: bool = False
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._image_input_enabled = image_input_enabled

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
        body: dict[str, Any] = {
            "contents": _contents(input, image_input_enabled=self._image_input_enabled),
            "generationConfig": generation,
        }
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
        request = self._client.build_request("POST", self._endpoint, json=body)
        request_bytes = len(request.content)
        metrics = active_vertex_metrics.get()
        try:
            response = await self._client.send(request)
        except httpx2.TimeoutException:
            logger.info(
                "vertex_http http_status=- request_bytes=%d outcome=timeout turn=%s",
                request_bytes,
                active_vertex_turn.get() or "-",
            )
            raise ModelAPITimeoutError("模型请求超时") from None
        except httpx2.TransportError:
            logger.info(
                "vertex_http http_status=- request_bytes=%d outcome=connection turn=%s",
                request_bytes,
                active_vertex_turn.get() or "-",
            )
            raise ModelAPITransportError("模型服务连接失败") from None
        except ModelRequestRejectedError:
            logger.info(
                "vertex_request_rejected request_bytes=%d turn=%s",
                request_bytes,
                active_vertex_turn.get() or "-",
            )
            raise
        if not httpx2.codes.is_success(response.status_code):
            detail = (
                _rate_detail(response.content, response.headers)
                if response.status_code == 429
                else None
            )
            if metrics is not None:
                metrics.failure(response.status_code, detail)
            if detail is not None:
                logger.info(
                    "vertex_http http_status=429 request_bytes=%d rate_kind=%s status=%s"
                    " reason=%s domain=%s quota_metric=%s quota_id=%s retry_delay_s=%s"
                    " retry_after=%s turn=%s",
                    request_bytes,
                    detail.rate_kind,
                    detail.status,
                    detail.reason,
                    detail.domain,
                    detail.quota_metric,
                    detail.quota_id,
                    detail.retry_delay_s if detail.retry_delay_s is not None else "unknown",
                    detail.retry_after,
                    active_vertex_turn.get() or "-",
                )
            else:
                logger.info(
                    "vertex_http http_status=%d request_bytes=%d turn=%s",
                    response.status_code,
                    request_bytes,
                    active_vertex_turn.get() or "-",
                )
            raise ModelAPIStatusError(response.status_code, response.headers) from None
        # 受控 transport 已确认是 JSON；这里再拒绝非有限数字，避免它们进入工具参数或下一步请求。
        payload = _strict_json(response.content)
        metadata = payload.get("usageMetadata") if isinstance(payload, dict) else None
        prompt = _token_count(metadata, "promptTokenCount")
        candidates = _token_count(metadata, "candidatesTokenCount")
        thoughts = _token_count(metadata, "thoughtsTokenCount")
        if metrics is not None:
            metrics.success(prompt, candidates, thoughts)
        logger.info(
            "vertex_http http_status=%d request_bytes=%d prompt_tokens=%s"
            " candidates_tokens=%s thoughts_tokens=%s turn=%s",
            response.status_code,
            request_bytes,
            prompt if prompt is not None else "unknown",
            candidates if candidates is not None else "unknown",
            thoughts if thoughts is not None else "unknown",
            active_vertex_turn.get() or "-",
        )
        if payload is None:
            _reject_response("模型成功响应不是严格 JSON", ResponseRejectReason.INVALID_JSON)
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


def _reject_response(message: str, reason: ResponseRejectReason) -> NoReturn:
    raise ModelResponseRejectedError(message, reason)


def _check_settings(settings: ModelSettings) -> None:
    for name, value in vars(settings).items():
        if value is None or name in _REPORTING_SETTINGS:
            continue
        if name not in _MAPPED_SETTINGS:
            _reject_request("Vertex 适配器不支持该模型设置")
    if settings.parallel_tool_calls or settings.store:
        _reject_request("Vertex 适配器不支持并行工具调用或服务端保存")


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


def _contents(
    input: str | list[TResponseInputItem], *, image_input_enabled: bool = False
) -> list[dict[str, Any]]:
    """按输入顺序映射；调用结果只按同一输入中的 ``call_id → 函数名`` 关联。"""
    if isinstance(input, str):
        return [_text_content("user", input)]
    contents: list[dict[str, Any]] = []
    calls: dict[str, str] = {}
    previous_kind: str | None = None
    for item in input:
        raw = item if isinstance(item, Mapping) else None
        if raw is None:
            _reject_request("Vertex 适配器不支持该输入项")
        kind = raw.get("type", "message")
        if kind == "message":
            contents.append(_message(raw, image_input_enabled=image_input_enabled))
        elif kind == "function_call":
            call_id = _string(raw.get("call_id"))
            name = _string(raw.get("name"))
            if call_id in calls:
                _reject_request("输入中的调用 ID 重复")
            calls[call_id] = name
            part = _function_call_part(raw, call_id, name)
            if previous_kind == kind:
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "model", "parts": [part]})
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
            part = {"functionResponse": response}
            if previous_kind == kind:
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
        else:
            _reject_request("Vertex 适配器不支持该输入项")
        previous_kind = kind
    return contents


_ROLES: dict[object, str] = {"user": "user", "assistant": "model"}


def _message(raw: Mapping[str, Any], *, image_input_enabled: bool = False) -> dict[str, Any]:
    raw_role = raw.get("role")
    role = _ROLES.get(raw_role) if isinstance(raw_role, str) else None
    if role is None:
        _reject_request("Vertex 适配器只映射用户与助手消息")
    content = raw.get("content")
    if isinstance(content, str):
        return _text_content(role, content)
    if not isinstance(content, list) or not content:
        _reject_request("Vertex 适配器只映射文本消息")
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, Mapping):
            _reject_request("Vertex 适配器只映射文本消息")
        if part.get("type") in ("input_text", "output_text"):
            parts.append({"text": _string(part.get("text"), allow_empty=True)})
        elif part.get("type") == "input_image" and role == "user" and image_input_enabled:
            url = part.get("image_url")
            if not isinstance(url, str) or part.get("detail") not in (None, "auto"):
                _reject_request("图片输入形态无效")
            mime, separator, encoded = url.partition(";base64,")
            if separator != ";base64," or mime not in ("data:image/png", "data:image/jpeg"):
                _reject_request("图片输入形态无效")
            try:
                raw_image = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                _reject_request("图片编码无效")
            if not raw_image or len(raw_image) > 2 * 1024 * 1024:
                _reject_request("图片超过单张上限")
            parts.append({"inlineData": {"mimeType": mime[5:], "data": encoded}})
        else:
            _reject_request("Vertex 适配器只映射文本消息")
    return {"role": role, "parts": parts}


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
    except (ValueError, RecursionError):
        return None
    return value


# ---- 响应映射 ---------------------------------------------------------------------------------


def _model_output(
    payload: object, tool_names: set[str]
) -> tuple[list[Any], Usage, dict[str, Any] | None]:
    # 单候选与 STOP 终态只由受控 transport 检查一次；这里仅做类型收窄。
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not isinstance(candidates, list) or len(candidates) != 1:
        _reject_response("模型响应不是单个候选", ResponseRejectReason.NOT_SINGLE_CANDIDATE)
    (candidate,) = candidates
    if not isinstance(candidate, dict):
        _reject_response("模型响应不是单个候选", ResponseRejectReason.NOT_SINGLE_CANDIDATE)
    content = candidate.get("content")
    if not isinstance(content, dict) or content.get("role", "model") != "model":
        _reject_response("模型响应没有可用的内容", ResponseRejectReason.INVALID_CONTENT)
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        _reject_response("模型响应没有可用的内容", ResponseRejectReason.INVALID_CONTENT)

    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    content_fields = {
        "text",
        "functionCall",
        "functionResponse",
        "inlineData",
        "fileData",
        "executableCode",
        "codeExecutionResult",
        "toolCall",
        "toolResponse",
    }
    metadata_fields = {
        "thoughtSignature",
        "thought",
        "partMetadata",
        "mediaResolution",
        "mediaProcessing",
        "audioTranscription",
        "speechMetadata",
        "videoMetadata",
    }
    for part in parts:
        if not isinstance(part, dict):
            _reject_response("模型响应包含不支持的内容", ResponseRejectReason.UNSUPPORTED_PART)
        fields = set(part)
        if fields - content_fields - metadata_fields:
            _reject_response("模型响应包含不支持的内容", ResponseRejectReason.UNSUPPORTED_PART)
        if "thought" in part and type(part["thought"]) is not bool:
            _reject_response("模型响应包含不支持的内容", ResponseRejectReason.UNSUPPORTED_PART)
        kind = fields & content_fields
        if kind == {"text"} and isinstance(part["text"], str):
            if part.get("thought") is not True and part["text"]:
                texts.append(part["text"])  # 思考与空文本不属于最终回答；签名不进入 Session
        elif kind == {"functionCall"}:
            if part.get("thought") is True:
                _reject_response("模型响应包含不支持的内容", ResponseRejectReason.UNSUPPORTED_PART)
            calls.append(part)
        elif not kind:
            continue  # 纯元数据 Part 没有正文；不影响工具调用或最终文本
        else:
            _reject_response("模型响应包含不支持的内容", ResponseRejectReason.UNSUPPORTED_PART)

    output: list[Any]
    if calls:
        output = [
            _function_call_item(part, tool_names, first=index == 0)
            for index, part in enumerate(calls)
        ]
        if len({item.call_id for item in output}) != len(output):
            _reject_response("模型返回的函数调用 ID 重复", ResponseRejectReason.INVALID_CALL_ID)
    else:
        if not texts:
            _reject_response("模型响应没有可用的内容", ResponseRejectReason.INVALID_CONTENT)
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


def _function_call_item(
    part: dict[str, Any], tool_names: set[str], *, first: bool
) -> ResponseFunctionToolCall:
    call = part["functionCall"]
    if not isinstance(call, dict):
        _reject_response("模型返回的函数调用无效", ResponseRejectReason.INVALID_CONTENT)
    name = call.get("name")
    if not isinstance(name, str) or name not in tool_names:
        _reject_response("模型调用了本轮未提供的工具", ResponseRejectReason.UNKNOWN_TOOL)
    args = call.get("args", {})
    if not isinstance(args, dict):
        _reject_response("模型返回的函数调用参数不是 JSON 对象", ResponseRejectReason.INVALID_ARGS)
    signature = part.get("thoughtSignature")
    if (first or signature is not None) and not _valid_signature(signature):
        _reject_response(
            "模型返回的函数调用缺少有效的 thought signature",
            ResponseRejectReason.INVALID_SIGNATURE,
        )
    call_id = call.get("id")
    if call_id is None:
        call_id = _GENERATED_CALL_ID_PREFIX + uuid.uuid4().hex
    elif (
        not isinstance(call_id, str)
        or not 0 < len(call_id) <= _MAX_CALL_ID_CHARS
        or call_id.startswith(_GENERATED_CALL_ID_PREFIX)
    ):
        _reject_response("模型返回的函数调用 ID 无效", ResponseRejectReason.INVALID_CALL_ID)
    # ``provider_data`` 是 SDK 在调用项上保存供应商附加字段的约定（同 SDK 的 Chat 转换器），
    # 不是 OpenAI 类型声明的字段，因此按关键字字典传入。
    fields: dict[str, Any] = {
        "id": _FAKE_ID,
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
    }
    if signature is not None:
        fields["provider_data"] = {"thought_signature": signature}
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
