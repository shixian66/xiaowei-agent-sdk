"""可信模型 Profile 与 SDK 模型装配。

Agent Loop、工具续轮与最终输出校验全部由 SDK ``Runner`` 完成；这里只负责：

- 用静态 Profile 装配锁定版 SDK 的 ``OpenAIResponsesModel`` 或 ``OpenAIChatCompletionsModel``；
- 为每个 Profile 建立独立 HTTP 客户端：明确期限、客户端与 Runner 重试均为 0、不跟随重定向；
- 在 transport 上限制请求/响应字节；请求头全部由可信值重新生成，凭据只用本 Profile 的；
- 成功响应必须是 JSON，Chat Completions 还须是完整终态，否则在 SDK 解析和工具执行前拒绝；
- ``json_object`` 模式用薄 ``Model`` 委托改写请求编码，外层 ``output_type`` 校验保持不变。
"""

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Annotated, Literal, cast

import httpx2
from agents import (
    AgentOutputSchemaBase,
    Handoff,
    Model,
    ModelResponse,
    ModelRetrySettings,
    ModelSettings,
    ModelTracing,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    Tool,
    TResponseInputItem,
)
from agents.items import TResponseStreamEvent
from openai import AsyncOpenAI
from openai.types.responses.response_prompt_param import ResponsePromptParam
from openai.types.shared import Reasoning, ReasoningEffort
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from xiaowei.config import is_secret_ref

Provider = Literal["openai", "gemini", "deepseek", "openai_compatible"]

# 推理强度只接受各供应商公开文档列出的值；真实 API 尚未逐一验证，未列出的供应商不开放。
_REASONING_EFFORTS: dict[Provider, frozenset[str]] = {
    "openai": frozenset({"minimal", "low", "medium", "high"}),
    "gemini": frozenset({"low", "medium", "high"}),
    "deepseek": frozenset(),
    "openai_compatible": frozenset(),
}

_JSON = "application/json"

# Chat Completions 中表示“正常完成”的终态；截断（length）、过滤（content_filter）、缺失或
# 供应商私有值都不交给 SDK，因为 SDK 转换为 ModelResponse 后会丢失该终态。
_COMPLETE_CHAT_FINISH_REASONS = frozenset({"stop", "tool_calls"})

_JSON_OBJECT_HINT = "\n\n只输出一个 JSON 对象，不要输出其他文字。该对象必须满足以下 JSON Schema：\n"

PositiveInt = Annotated[int, Field(gt=0)]


class ModelProfile(BaseModel):
    """一个经过审定的“端点 + 协议 + 模型”组合；不含凭据值。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(min_length=1)
    provider: Provider
    base_url: str
    api_mode: Literal["responses", "chat_completions"]
    model: str = Field(min_length=1)
    api_key_ref: str
    output_mode: Literal["json_schema", "json_object"]
    request_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_output_tokens: PositiveInt
    max_request_bytes: PositiveInt
    max_response_bytes: PositiveInt
    data_policy_id: str = Field(min_length=1)
    reasoning_effort: str | None

    @field_validator("base_url")
    @classmethod
    def _trusted_https_endpoint(cls, value: str) -> str:
        try:
            url = httpx2.URL(value)
        except httpx2.InvalidURL:
            raise ValueError("base_url 不是合法 URL") from None
        if url.scheme != "https" or not url.host:
            raise ValueError("base_url 必须是带主机名的 HTTPS 地址")
        if url.userinfo or url.query or url.fragment:
            raise ValueError("base_url 不能包含用户信息、查询参数或片段")
        return value.rstrip("/")

    @field_validator("api_key_ref")
    @classmethod
    def _reference_not_value(cls, value: str) -> str:
        if not is_secret_ref(value):
            raise ValueError("api_key_ref 必须是 env:NAME 形式的凭据引用")
        return value

    @model_validator(mode="after")
    def _supported_combination(self) -> "ModelProfile":
        if self.api_mode == "responses" and self.provider not in ("openai", "openai_compatible"):
            raise ValueError(f"{self.provider} 不提供 Responses 协议")
        if self.output_mode == "json_object" and self.api_mode != "chat_completions":
            raise ValueError("json_object 仅用于 Chat Completions")
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in _REASONING_EFFORTS[self.provider]
        ):
            raise ValueError(f"{self.provider} 不接受该 reasoning_effort")
        return self


class ModelAPIRejectedError(Exception):
    """模型请求或响应越过 Profile 边界；消息固定，不包含内容、地址或凭据。"""


class ModelRequestRejectedError(ModelAPIRejectedError):
    """请求在发出前被拒绝。"""


class ModelResponseRejectedError(ModelAPIRejectedError):
    """响应在交给 SDK 解析前被拒绝：超限、压缩、非 JSON 或非完整终态。"""


def profile_fingerprint(profile: ModelProfile) -> str:
    """Profile 的稳定配置版本，用于把会话绑定到“端点 + 协议 + 模型”组合。"""
    canonical = json.dumps(profile.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def settings_for(profile: ModelProfile) -> ModelSettings:
    """把 Profile 映射为 SDK 设置；不透传任意供应商参数。"""
    return ModelSettings(
        max_tokens=profile.max_output_tokens,
        reasoning=(
            Reasoning(effort=cast(ReasoningEffort, profile.reasoning_effort))
            if profile.reasoning_effort is not None
            else None
        ),
        # Responses 默认在服务端保存对话；Chat Completions 不发送该字段。
        store=False if profile.api_mode == "responses" else None,
        # 并行工具调用未经验证，不使用供应商默认值（通常为开启）。
        parallel_tool_calls=False,
        retry=ModelRetrySettings(max_retries=0),
        timeout=profile.request_timeout_seconds,
        # SDK 会把缺失的 usage 规范化为 0；保留原始 usage 以区分“未上报”和“为 0”。
        preserve_raw_usage=True,
    )


@asynccontextmanager
async def open_model(
    profile: ModelProfile,
    *,
    api_key: SecretStr,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncIterator[Model]:
    """为一个 Profile 装配独立客户端与 SDK Model，退出时关闭客户端。

    ``transport`` 仅替换最底层的网络发送（测试使用 mock transport）；限额与请求头约束始终生效。
    """
    guarded = _GuardedTransport(
        # 证书与代理不从环境读取；外层 AsyncClient 的 trust_env 不作用于这里创建的 transport。
        transport or httpx2.AsyncHTTPTransport(retries=0, trust_env=False),
        authorization=f"Bearer {api_key.get_secret_value()}",
        max_request_bytes=profile.max_request_bytes,
        max_response_bytes=profile.max_response_bytes,
        require_complete_chat=profile.api_mode == "chat_completions",
    )
    timeout = httpx2.Timeout(profile.request_timeout_seconds)
    http_client = httpx2.AsyncClient(
        transport=guarded, timeout=timeout, follow_redirects=False, trust_env=False
    )
    client = AsyncOpenAI(
        api_key=api_key.get_secret_value(),
        base_url=profile.base_url,
        timeout=timeout,
        max_retries=0,
        http_client=http_client,
    )
    try:
        model: Model
        if profile.api_mode == "responses":
            model = OpenAIResponsesModel(profile.model, client)
        else:
            model = OpenAIChatCompletionsModel(profile.model, client)
        if profile.output_mode == "json_object":
            model = _JsonObjectModel(model)
        yield model
    finally:
        await client.close()


class _GuardedTransport(httpx2.AsyncBaseTransport):
    """模型 HTTP 的唯一出入口：请求在发出前、响应在交给 SDK 前各检查一次。"""

    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        *,
        authorization: str,
        max_request_bytes: int,
        max_response_bytes: int,
        require_complete_chat: bool,
    ) -> None:
        self._inner = inner
        self._authorization = authorization
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._require_complete_chat = require_complete_chat

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = await request.aread()
        if len(body) > self._max_request_bytes:
            raise ModelRequestRejectedError("模型请求超过 Profile 的请求字节上限")
        # 不沿用 SDK 请求中的任何头值：SDK 客户端会把环境变量中的自定义头合并进来。
        # Host 与 Content-Length 由 httpx 从可信 URL 与实际 body 生成，其余为固定协议值。
        forwarded = httpx2.Request(
            request.method,
            request.url,
            headers={
                "authorization": self._authorization,
                "accept": _JSON,
                "content-type": _JSON,
                # 压缩内容无法在读取阶段按实际字节限额，只接受未压缩响应。
                "accept-encoding": "identity",
            },
            content=body,
            extensions=request.extensions,
        )

        response = await self._inner.handle_async_request(forwarded)
        try:
            content = await self._read_bounded(response)
        finally:
            await response.aclose()
        if httpx2.codes.is_success(response.status_code):
            self._check_success_body(content)
        headers = [
            (k, v)
            for k, v in response.headers.multi_items()
            if k.lower() not in ("content-length", "transfer-encoding")
        ]
        return httpx2.Response(
            response.status_code,
            headers=headers,
            content=content,
            extensions=response.extensions,
        )

    async def _read_bounded(self, response: httpx2.Response) -> bytes:
        """按实际读取的字节计数；不依赖可伪造的 Content-Length。"""
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ModelResponseRejectedError("模型响应使用了压缩编码，无法按字节上限读取")
        stream = response.stream
        if not isinstance(stream, httpx2.AsyncByteStream):
            raise ModelResponseRejectedError("模型响应不是异步字节流")
        chunks: list[bytes] = []
        received = 0
        async for chunk in stream:
            received += len(chunk)
            if received > self._max_response_bytes:
                raise ModelResponseRejectedError("模型响应超过 Profile 的响应字节上限")
            chunks.append(chunk)
        return b"".join(chunks)

    def _check_success_body(self, content: bytes) -> None:
        # 流式（SSE）与非 JSON 成功响应未经验证，不交给 SDK 解析。
        try:
            payload = json.loads(content)
        except ValueError:
            raise ModelResponseRejectedError("模型成功响应不是合法 JSON") from None
        if self._require_complete_chat:
            _require_complete_chat(payload)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _require_complete_chat(payload: object) -> None:
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise ModelResponseRejectedError("模型响应没有完整的 Chat Completions 终态")
    for choice in choices:
        reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        if reason not in _COMPLETE_CHAT_FINISH_REASONS:
            raise ModelResponseRejectedError("模型响应没有完整的 Chat Completions 终态")


class _JsonObjectModel(Model):
    """把结构化输出请求改为 ``json_object`` 编码，其余全部委托给 SDK 模型。

    JSON mode 只保证返回合法 JSON，不携带 schema；schema 通过请求说明提供。Runner 仍按
    Agent 的 ``output_type`` 校验最终输出，缺字段、错类型或空内容都不会成为成功结果。
    """

    def __init__(self, inner: Model) -> None:
        self._inner = inner

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
        system_instructions, model_settings, output_schema = _as_json_object(
            system_instructions, model_settings, output_schema
        )
        return await self._inner.get_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )

    def stream_response(
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
        system_instructions, model_settings, output_schema = _as_json_object(
            system_instructions, model_settings, output_schema
        )
        return self._inner.stream_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )

    async def close(self) -> None:
        await self._inner.close()


def _as_json_object(
    system_instructions: str | None,
    model_settings: ModelSettings,
    output_schema: AgentOutputSchemaBase | None,
) -> tuple[str | None, ModelSettings, AgentOutputSchemaBase | None]:
    if output_schema is None or output_schema.is_plain_text():
        return system_instructions, model_settings, output_schema
    schema = json.dumps(output_schema.json_schema(), ensure_ascii=False, sort_keys=True)
    instructions = (system_instructions or "") + _JSON_OBJECT_HINT + schema
    settings = replace(
        model_settings,
        extra_args={
            **(model_settings.extra_args or {}),
            "response_format": {"type": "json_object"},
        },
    )
    # 不把 schema 交给内层模型，避免其生成 json_schema 编码；最终校验仍由 Runner 执行。
    return instructions, settings, None
