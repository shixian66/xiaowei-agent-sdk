"""Task 1B：模型 API Profile 与真实 SDK Model 的 HTTP 契约。

全部经过真实 ``OpenAIResponsesModel`` / ``OpenAIChatCompletionsModel`` 与 ``Runner``；供应商端点由
``httpx2.MockTransport`` 模拟，不建立任何网络连接。HTTP mock 通过不等于供应商真实验证通过。
"""

import asyncio
import gzip
import inspect
import json
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from agents import Agent, Model, Runner, function_tool
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from openai import APIStatusError, APITimeoutError
from pydantic import BaseModel, ValidationError

from xiaowei import model_api as model_api_module
from xiaowei.config import SecretRefError, resolve_secret_ref
from xiaowei.model_api import (
    ModelAPIRejectedError,
    ModelProfile,
    ModelRequestRejectedError,
    ModelResponseRejectedError,
    open_model,
    profile_fingerprint,
    settings_for,
)

OPENAI_KEY = "fake-openai-key"
GEMINI_KEY = "fake-gemini-key"
DEEPSEEK_KEY = "fake-deepseek-key"


class TotalAnswer(BaseModel):
    total: int
    note: str


def _profile(**overrides: Any) -> ModelProfile:
    values: dict[str, Any] = {
        "profile_id": "openai-main",
        "provider": "openai",
        "base_url": "https://api.openai.test/v1",
        "api_mode": "responses",
        "model": "model-under-test",
        "api_key_ref": "env:XIAOWEI_TEST_OPENAI_KEY",
        "output_mode": "json_schema",
        "request_timeout_seconds": 7.5,
        "max_output_tokens": 256,
        "max_request_bytes": 64_000,
        "max_response_bytes": 64_000,
        "data_policy_id": "synthetic-only",
        "reasoning_effort": None,
    }
    values.update(overrides)
    return ModelProfile(**values)


OPENAI = _profile()
GEMINI = _profile(
    profile_id="gemini-main",
    provider="gemini",
    base_url="https://gemini.test/v1beta/openai",
    api_mode="chat_completions",
    api_key_ref="env:XIAOWEI_TEST_GEMINI_KEY",
)
DEEPSEEK = _profile(
    profile_id="deepseek-main",
    provider="deepseek",
    base_url="https://api.deepseek.test/v1",
    api_mode="chat_completions",
    api_key_ref="env:XIAOWEI_TEST_DEEPSEEK_KEY",
    output_mode="json_object",
)


# ---- 供应商协议的最小响应 -------------------------------------------------------------


def _responses_body(output: list[dict[str, Any]], usage: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "model-under-test",
        "status": "completed",
        "output": output,
        "tool_choice": "auto",
        "tools": [],
        "parallel_tool_calls": False,
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _responses_tool_call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    return _responses_body(
        [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
                "status": "completed",
            }
        ],
        usage=None,
    )


def _responses_text(text: str, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    return _responses_body(
        [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        usage=usage,
    )


def _chat_body(message: dict[str, Any], usage: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chat_1",
        "object": "chat.completion",
        "created": 0,
        "model": "model-under-test",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _chat_tool_call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    return _chat_body(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }
    )


def _chat_text(text: str | None, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    return _chat_body({"role": "assistant", "content": text}, usage=usage)


def _tool_call(profile: ModelProfile, call_id: str = "call-1") -> dict[str, Any]:
    if profile.api_mode == "responses":
        return _responses_tool_call("synthetic_total", {"region": "east"}, call_id)
    return _chat_tool_call("synthetic_total", {"region": "east"}, call_id)


def _text(profile: ModelProfile, text: str | None, usage: dict[str, Any] | None = None) -> Any:
    if profile.api_mode == "responses":
        return _responses_text(text or "", usage)
    return _chat_text(text, usage)


FINAL = json.dumps({"total": 100, "note": "ok"})


# ---- 记录请求的 mock 端点 --------------------------------------------------------------

Reply = dict[str, Any] | httpx2.Response | Callable[[httpx2.Request], httpx2.Response]


@dataclass
class Endpoint:
    """按顺序回放脚本化响应，并记录收到的每个请求。"""

    replies: list[Reply]
    requests: list[httpx2.Request] = field(default_factory=list)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError(f"未预期的额外请求：{request.url}")
        reply = self.replies.pop(0)
        if callable(reply):
            return reply(request)
        if isinstance(reply, httpx2.Response):
            return reply
        return httpx2.Response(200, json=reply)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)


def _agent(model: Model, profile: ModelProfile, executed: list[str]) -> Agent[None]:
    @function_tool
    def synthetic_total(region: str) -> str:
        """返回合成地区的订单总数。"""
        executed.append(region)
        return json.dumps({"region": region, "total": 100})

    return Agent(
        name="model-api-contract",
        instructions="使用工具回答。",
        model=model,
        model_settings=settings_for(profile),
        tools=[synthetic_total],
        output_type=TotalAnswer,
    )


_KEYS = {
    OPENAI.profile_id: OPENAI_KEY,
    GEMINI.profile_id: GEMINI_KEY,
    DEEPSEEK.profile_id: DEEPSEEK_KEY,
}


@pytest.fixture(autouse=True)
def profile_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个 Profile 的凭据引用指向各自的假密钥；open_model 只按引用解析。"""
    for profile in (OPENAI, GEMINI, DEEPSEEK):
        monkeypatch.setenv(profile.api_key_ref.removeprefix("env:"), _KEYS[profile.profile_id])


@pytest.fixture
def no_ambient_openai_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_ADMIN_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_CUSTOM_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


# ---- 工具续轮 ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", [OPENAI, GEMINI], ids=["responses", "chat_completions"])
async def test_sdk_responses_and_chat_completions_tool_roundtrip(
    profile: ModelProfile, no_ambient_openai_env: None
) -> None:
    endpoint = Endpoint([_tool_call(profile), _text(profile, FINAL)])
    executed: list[str] = []

    async with open_model(profile, transport=endpoint.transport) as opened:
        m = opened.model
        result = await Runner.run(_agent(m, profile, executed), "东区订单总数？", max_turns=4)

    assert executed == ["east"]
    assert isinstance(result.final_output, TotalAnswer)
    assert result.final_output.total == 100

    # 工具执行后确有第二次模型请求，且携带了工具结果。
    assert len(endpoint.requests) == 2
    first, second = endpoint.bodies()
    for request in endpoint.requests:
        assert str(request.url).startswith(profile.base_url + "/")
        assert request.headers["authorization"] == f"Bearer {_KEYS[profile.profile_id]}"
        # 期限来自 Profile，而不是 SDK / 客户端默认值。
        assert request.extensions["timeout"]["read"] == profile.request_timeout_seconds

    if profile.api_mode == "responses":
        assert endpoint.requests[0].url.path.endswith("/responses")
        outputs = [i for i in second["input"] if i.get("type") == "function_call_output"]
        assert [o["call_id"] for o in outputs] == ["call-1"]
        assert first["max_output_tokens"] == profile.max_output_tokens
        assert first["store"] is False  # 模型服务端不保存本轮对话
        assert first["text"]["format"]["type"] == "json_schema"
    else:
        assert endpoint.requests[0].url.path.endswith("/chat/completions")
        tool_messages = [m for m in second["messages"] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_messages] == ["call-1"]
        assert first["max_tokens"] == profile.max_output_tokens
        assert first["response_format"]["type"] == "json_schema"
        assert "store" not in first

    # 只发送经过映射的参数，不出现未配置的推理/采样字段。
    assert "reasoning" not in first
    assert "reasoning_effort" not in first
    assert "temperature" not in first


@pytest.mark.parametrize(
    "profile", [OPENAI, GEMINI, DEEPSEEK], ids=["openai", "gemini", "deepseek"]
)
async def test_parallel_tool_calls_are_disabled(
    profile: ModelProfile, no_ambient_openai_env: None
) -> None:
    endpoint = Endpoint([_text(profile, FINAL)])
    async with open_model(profile, transport=endpoint.transport) as opened:
        m = opened.model
        await Runner.run(_agent(m, profile, []), "hi")
    assert endpoint.bodies()[0]["parallel_tool_calls"] is False


@pytest.mark.parametrize(
    ("profile", "effort", "field_path"),
    [
        (OPENAI, "low", ("reasoning", "effort")),
        (GEMINI, "high", ("reasoning_effort",)),
    ],
    ids=["responses", "chat_completions"],
)
async def test_reasoning_effort_maps_to_the_selected_protocol(
    profile: ModelProfile, effort: str, field_path: tuple[str, ...], no_ambient_openai_env: None
) -> None:
    profile = profile.model_copy(update={"reasoning_effort": effort})
    endpoint = Endpoint([_text(profile, FINAL)])
    async with open_model(profile, transport=endpoint.transport) as opened:
        m = opened.model
        await Runner.run(_agent(m, profile, []), "hi")

    value: Any = endpoint.bodies()[0]
    for key in field_path:
        value = value[key]
    assert value == effort


@pytest.mark.parametrize(
    ("usage", "expected_raw"),
    [
        (None, None),
        (
            {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        ),
    ],
    ids=["missing", "reported"],
)
async def test_missing_usage_is_not_recorded_as_zero(
    usage: dict[str, Any] | None, expected_raw: dict[str, Any] | None, no_ambient_openai_env: None
) -> None:
    endpoint = Endpoint([_chat_text(FINAL, usage)])
    async with open_model(GEMINI, transport=endpoint.transport) as opened:
        m = opened.model
        result = await Runner.run(_agent(m, GEMINI, []), "hi")

    (response,) = result.raw_responses
    # SDK 会把缺失的 usage 规范化为 0；原始 usage 保留“是否上报”的事实。
    raw = response.raw_usage
    if expected_raw is None:
        assert raw is None
    else:
        assert raw is not None
        assert {k: raw[k] for k in expected_raw} == expected_raw


# ---- JSON mode ------------------------------------------------------------------------


async def test_json_object_still_validates_output_type(no_ambient_openai_env: None) -> None:
    endpoint = Endpoint([_tool_call(DEEPSEEK), _chat_text(FINAL)])
    executed: list[str] = []
    async with open_model(DEEPSEEK, transport=endpoint.transport) as opened:
        m = opened.model
        result = await Runner.run(_agent(m, DEEPSEEK, executed), "东区订单总数？")

    assert executed == ["east"]
    assert result.final_output == TotalAnswer(total=100, note="ok")
    for body in endpoint.bodies():
        assert body["response_format"] == {"type": "json_object"}
        system = body["messages"][0]
        assert system["role"] == "system"
        # JSON mode 本身不携带 schema：由请求说明提供，外层 output_type 仍做最终校验。
        assert "JSON" in system["content"]
        assert '"total"' in system["content"]
        assert system["content"].startswith("使用工具回答。")


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(json.dumps({"total": 100}), id="missing-field"),
        pytest.param(json.dumps({"total": "100", "note": "ok"}), id="wrong-type"),
        pytest.param("", id="empty"),
        pytest.param(None, id="null"),
        pytest.param("total is 100", id="not-json"),
    ],
)
async def test_json_object_invalid_final_output_is_not_success(
    content: str | None, no_ambient_openai_env: None
) -> None:
    endpoint = Endpoint([_chat_text(content)])
    async with open_model(DEEPSEEK, transport=endpoint.transport) as opened:
        m = opened.model
        # 非法 JSON 由 output_type 校验拒绝；空内容不是最终输出，Runner 继续请求直到轮次上限。
        with pytest.raises((ModelBehaviorError, MaxTurnsExceeded)):
            await Runner.run(_agent(m, DEEPSEEK, []), "hi", max_turns=1)
    assert len(endpoint.requests) == 1


def _chat_finished(body: dict[str, Any], finish_reason: str | None) -> dict[str, Any]:
    body["choices"][0]["finish_reason"] = finish_reason
    return body


@pytest.mark.parametrize("profile", [GEMINI, DEEPSEEK], ids=["gemini", "deepseek"])
@pytest.mark.parametrize("finish_reason", ["length", "content_filter", None])
@pytest.mark.parametrize("kind", ["final-json", "tool-call"])
async def test_incomplete_chat_result_is_rejected_before_the_sdk_sees_it(
    profile: ModelProfile, finish_reason: str | None, kind: str, no_ambient_openai_env: None
) -> None:
    """内容碰巧合法也不行：截断/过滤终态不能成为成功答案，截断的工具调用不能执行。"""
    body = _chat_text(FINAL) if kind == "final-json" else _tool_call(profile)
    endpoint = Endpoint([_chat_finished(body, finish_reason)])
    executed: list[str] = []
    async with open_model(profile, transport=endpoint.transport) as opened:
        m = opened.model
        with pytest.raises(ModelResponseRejectedError):
            await Runner.run(_agent(m, profile, executed), "hi")
    assert executed == []
    assert len(endpoint.requests) == 1  # 没有续轮


def _no_choices() -> dict[str, Any]:
    body = _chat_text(FINAL)
    body["choices"] = []
    return body


def _one_truncated_choice() -> dict[str, Any]:
    body = _chat_text(FINAL)
    truncated = dict(body["choices"][0], index=1, finish_reason="length")
    body["choices"].append(truncated)
    return body


@pytest.mark.parametrize(
    "body", [_no_choices(), _one_truncated_choice()], ids=["no-choices", "one-truncated"]
)
async def test_chat_without_complete_choices_is_rejected(
    body: dict[str, Any], no_ambient_openai_env: None
) -> None:
    endpoint = Endpoint([body])
    async with open_model(GEMINI, transport=endpoint.transport) as opened:
        m = opened.model
        with pytest.raises(ModelResponseRejectedError):
            await Runner.run(_agent(m, GEMINI, []), "hi")
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize("finish_reason", ["stop", "tool_calls"])
async def test_complete_chat_tool_call_is_executed(
    finish_reason: str, no_ambient_openai_env: None
) -> None:
    endpoint = Endpoint([_chat_finished(_tool_call(GEMINI), finish_reason), _chat_text(FINAL)])
    executed: list[str] = []
    async with open_model(GEMINI, transport=endpoint.transport) as opened:
        m = opened.model
        result = await Runner.run(_agent(m, GEMINI, executed), "hi")
    assert executed == ["east"]
    assert result.final_output == TotalAnswer(total=100, note="ok")


async def test_incomplete_responses_result_is_rejected_by_the_sdk(
    no_ambient_openai_env: None,
) -> None:
    """对照：Responses 的 incomplete 终态由 SDK 自身拒绝，这里不重复校验。"""
    body = _responses_tool_call("synthetic_total", {"region": "east"}, "call-1")
    body["status"] = "incomplete"
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    endpoint = Endpoint([body])
    executed: list[str] = []
    async with open_model(OPENAI, transport=endpoint.transport) as opened:
        m = opened.model
        with pytest.raises(ModelBehaviorError):
            await Runner.run(_agent(m, OPENAI, executed), "hi")
    assert executed == []
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize("profile", [OPENAI, GEMINI], ids=["responses", "chat_completions"])
async def test_successful_response_must_be_json(
    profile: ModelProfile, no_ambient_openai_env: None
) -> None:
    """流式（SSE）与非 JSON 的成功响应未经验证，不交给 SDK 解析。"""
    sse = httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=b"data: {}\n\n"
    )
    endpoint = Endpoint([sse])
    async with open_model(profile, transport=endpoint.transport) as opened:
        m = opened.model
        with pytest.raises(ModelResponseRejectedError):
            await Runner.run(_agent(m, profile, []), "hi")


# ---- 凭据与端点隔离 ---------------------------------------------------------------------


async def test_model_client_close_timeout_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    original_close = model_api_module.AsyncOpenAI.close

    async def hanging_close(client: Any) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await original_close(client)
            raise

    monkeypatch.setattr(model_api_module, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(model_api_module.AsyncOpenAI, "close", hanging_close)
    transport = httpx2.MockTransport(lambda request: httpx2.Response(500, request=request))

    async def close_model() -> None:
        with pytest.raises(ModelAPIRejectedError, match="模型客户端关闭超时"):
            async with open_model(OPENAI, transport=transport):
                pass

    await asyncio.wait_for(close_model(), 0.2)


async def test_credential_comes_from_the_profile_reference(
    monkeypatch: pytest.MonkeyPatch, no_ambient_openai_env: None
) -> None:
    """装配层不能给 Profile 另配凭据：密钥只按 Profile 自己的引用解析。"""
    assert "api_key" not in inspect.signature(open_model).parameters
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_responses_text(FINAL))

    transport = httpx2.MockTransport(handle)
    async with open_model(OPENAI, transport=transport) as opened:
        await Runner.run(_agent(opened.model, OPENAI, []), "hi")
    assert [r.headers["authorization"] for r in seen] == [f"Bearer {OPENAI_KEY}"]

    # 引用无法解析：不创建客户端，零请求。
    monkeypatch.delenv("XIAOWEI_TEST_OPENAI_KEY")
    with pytest.raises(SecretRefError):
        async with open_model(OPENAI, transport=transport):
            pass
    assert len(seen) == 1


async def test_profiles_keep_keys_and_endpoints_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    # SDK 的 OpenAI 客户端默认读取这些环境变量；它们不能随请求发往任何 Profile 的端点。
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-api-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.test/v1")
    monkeypatch.setenv("OPENAI_ORG_ID", "ambient-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "ambient-project")
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS", "Authorization: Bearer ambient-header-key\nX-Ambient: leak"
    )

    seen: list[httpx2.Request] = []
    replies: dict[str, dict[str, Any]] = {
        "api.openai.test": _responses_text(FINAL),
        "gemini.test": _chat_text(FINAL),
        "api.deepseek.test": _chat_text(FINAL),
    }

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=replies[request.url.host])

    shared = httpx2.MockTransport(handle)
    for profile in (OPENAI, GEMINI, DEEPSEEK):
        async with open_model(profile, transport=shared) as opened:
            m = opened.model
            await Runner.run(_agent(m, profile, []), "hi")

    by_host = {r.url.host: r for r in seen}
    assert len(seen) == 3
    assert by_host["api.openai.test"].headers["authorization"] == f"Bearer {OPENAI_KEY}"
    assert by_host["gemini.test"].headers["authorization"] == f"Bearer {GEMINI_KEY}"
    assert by_host["api.deepseek.test"].headers["authorization"] == f"Bearer {DEEPSEEK_KEY}"
    for request in seen:
        sent = json.dumps(dict(request.headers)) + request.content.decode()
        for ambient in ("ambient-", "leak"):
            assert ambient not in sent
        for name in ("openai-organization", "openai-project", "x-ambient"):
            assert name not in request.headers
        others = {OPENAI_KEY, GEMINI_KEY, DEEPSEEK_KEY} - {
            request.headers["authorization"].removeprefix("Bearer ")
        }
        assert not any(k in sent for k in others)


@pytest.mark.parametrize(
    "profile", [OPENAI, GEMINI, DEEPSEEK], ids=["openai", "gemini", "deepseek"]
)
async def test_ambient_headers_cannot_replace_protocol_headers(
    profile: ModelProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单只说明“发哪些头”，值必须来自可信 URL、实际 body 与固定协议值。"""
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS",
        "\n".join(
            [
                "Host: foreign-tenant.example",
                "Accept: application/x-ambient-sentinel",
                "Content-Type: text/plain",
                "Content-Length: 1",
                "User-Agent: ambient-agent",
                "Accept-Encoding: gzip",
            ]
        ),
    )
    endpoint = Endpoint([_text(profile, FINAL)])
    async with open_model(profile, transport=endpoint.transport) as opened:
        m = opened.model
        await Runner.run(_agent(m, profile, []), "hi")

    (request,) = endpoint.requests
    assert request.headers["host"] == httpx2.URL(profile.base_url).host
    assert request.headers["content-length"] == str(len(request.content))
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/json"
    assert request.headers["accept-encoding"] == "identity"
    sent = json.dumps(dict(request.headers))
    for sentinel in ("foreign-tenant", "x-ambient-sentinel", "text/plain", "ambient-agent"):
        assert sentinel not in sent


async def test_default_transport_ignores_tls_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """默认网络 transport 不从环境读取证书或代理配置。"""
    missing = str(tmp_path / "missing-ca.pem")
    monkeypatch.setenv("SSL_CERT_FILE", missing)
    monkeypatch.setenv("SSL_CERT_DIR", missing)
    async with open_model(GEMINI) as opened:
        m = opened.model
        assert isinstance(m, Model)


def test_profile_fingerprint_is_stable_and_has_no_secret() -> None:
    assert profile_fingerprint(OPENAI) == profile_fingerprint(_profile())
    fingerprints = {profile_fingerprint(p) for p in (OPENAI, GEMINI, DEEPSEEK)}
    assert len(fingerprints) == 3
    # 端点、协议、模型任一变化都得到新的配置版本，用于要求新建会话。
    for change in (
        {"base_url": "https://other.test/v1"},
        {"model": "other-model"},
        {"api_mode": "chat_completions"},
        {"output_mode": "json_object", "api_mode": "chat_completions"},
    ):
        assert profile_fingerprint(_profile(**change)) != profile_fingerprint(OPENAI)
    assert OPENAI_KEY not in profile_fingerprint(OPENAI)


# 加入 Vertex 前（04637cf）由同一 Profile 算出的值：既有四种 Provider 的指纹必须逐字不变，
# 否则已绑定的会话会被误判为换了模型。
_PINNED_FINGERPRINTS = {
    "openai": "sha256:2a771199f07839cc024fce1311f0ff26a8b21b9f45848ce25ba5295402a1e3c3",
    "gemini": "sha256:683fed884f717313a79babd222f7da45f0265ddabd426868fe96947fa5233958",
    "deepseek": "sha256:900406a742a3d06f79af1810d320201c3144a1a22edcf20e22f7e231821a65ba",
    "openai_compatible": (
        "sha256:ecd976fc9e0b6b8ad87a41308cce96ff0ac873cac71fc9186ef4234f4f9347db"
    ),
}


def test_existing_provider_fingerprints_are_unchanged() -> None:
    compatible = _profile(provider="openai_compatible", base_url="https://compat.test/v1")
    actual = {p.provider: profile_fingerprint(p) for p in (OPENAI, GEMINI, DEEPSEEK, compatible)}
    assert actual == _PINNED_FINGERPRINTS


def _vertex(**overrides: Any) -> ModelProfile:
    values: dict[str, Any] = {
        "profile_id": "vertex-main",
        "provider": "vertex",
        "model": "gemini-3-flash-preview",
        "api_key_ref": "env:XIAOWEI_TEST_VERTEX_KEY",
        "request_timeout_seconds": 30,
        "max_output_tokens": 256,
        "max_request_bytes": 64_000,
        "max_response_bytes": 64_000,
        "data_policy_id": "synthetic-only",
        "reasoning_effort": None,
    }
    values.update(overrides)
    return ModelProfile(**values)


def test_vertex_profile_needs_only_common_fields() -> None:
    profile = _vertex()
    assert profile.provider == "vertex"
    assert (profile.base_url, profile.api_mode, profile.output_mode) == (None, None, None)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"base_url": "https://aiplatform.googleapis.com/v1beta1"}, id="base-url"),
        pytest.param({"api_mode": "chat_completions"}, id="api-mode"),
        pytest.param({"output_mode": "json_schema"}, id="output-mode"),
        pytest.param({"reasoning_effort": "low"}, id="reasoning-effort"),
        pytest.param({"model": ""}, id="empty-model"),
        pytest.param({"model": "publishers/google/models/x"}, id="slash"),
        pytest.param({"model": "gemini?alt=sse"}, id="query"),
        pytest.param({"model": "gemini#frag"}, id="fragment"),
        pytest.param({"model": ".."}, id="dot-dot"),
        pytest.param({"model": "gemini:generateContent"}, id="colon"),
        pytest.param({"model": "gemini 3"}, id="space"),
    ],
)
def test_vertex_profile_rejects_operator_protocol_fields(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _vertex(**change)


@pytest.mark.parametrize("field", ["base_url", "api_mode", "output_mode"])
def test_existing_providers_still_require_protocol_fields(field: str) -> None:
    values = OPENAI.model_dump()
    del values[field]
    with pytest.raises(ValidationError):
        ModelProfile(**values)


def test_vertex_fingerprint_names_the_fixed_endpoint_and_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _vertex()
    fingerprint = profile_fingerprint(profile)
    assert fingerprint == profile_fingerprint(_vertex())
    assert fingerprint not in {profile_fingerprint(p) for p in (OPENAI, GEMINI, DEEPSEEK)}
    for change in ({"model": "gemini-3-pro-preview"}, {"data_policy_id": "other-policy"}):
        assert profile_fingerprint(_vertex(**change)) != fingerprint
    # 固定端点与协议身份属于指纹：改变任一常量都必须得到新的配置版本。
    monkeypatch.setattr(model_api_module, "VERTEX_PROTOCOL", "v1-generateContent")
    assert profile_fingerprint(profile) != fingerprint
    monkeypatch.undo()
    monkeypatch.setattr(
        model_api_module, "VERTEX_ENDPOINT", "https://other.googleapis.com/v1beta1/{model}"
    )
    assert profile_fingerprint(profile) != fingerprint
    monkeypatch.undo()
    monkeypatch.setenv("XIAOWEI_TEST_VERTEX_KEY", "fake-vertex-key")
    assert "fake-vertex-key" not in fingerprint


async def test_model_client_is_closed_on_exit(no_ambient_openai_env: None) -> None:
    endpoint = Endpoint([_chat_text(FINAL), _chat_text(FINAL)])
    async with open_model(GEMINI, transport=endpoint.transport) as opened:
        m = opened.model
        await Runner.run(_agent(m, GEMINI, []), "hi")
    with pytest.raises(RuntimeError):
        await Runner.run(_agent(m, GEMINI, []), "hi")
    assert len(endpoint.requests) == 1


# ---- 失败：不重试、不切换、不重复执行工具 ---------------------------------------------------


def _status(code: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        code,
        headers=headers,
        json={"error": {"message": "upstream-detail", "type": "x"}},
    )


def _raise_timeout(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("simulated", request=request)


def _invalid_json(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, content=b"{not json", headers={"content-type": "application/json"})


def _redirect(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(
        307, headers={"location": "https://api.deepseek.test/v1/chat/completions"}
    )


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        pytest.param(_status(401), APIStatusError, id="401"),
        pytest.param(_status(403), APIStatusError, id="403"),
        pytest.param(_status(429, {"retry-after": "0"}), APIStatusError, id="429"),
        pytest.param(_status(503), APIStatusError, id="503"),
        pytest.param(_raise_timeout, APITimeoutError, id="timeout"),
        pytest.param(_invalid_json, ModelResponseRejectedError, id="invalid-json"),
        pytest.param(_redirect, APIStatusError, id="cross-endpoint-redirect"),
    ],
)
async def test_api_failure_has_no_retry_or_fallback(
    failure: Reply, error: type[Exception], no_ambient_openai_env: None
) -> None:
    # 第一轮正常请求工具；工具执行后的续轮失败。
    endpoint = Endpoint([_tool_call(GEMINI), failure])
    other = Endpoint([])
    executed: list[str] = []

    def route(request: httpx2.Request) -> httpx2.Response:
        target = endpoint if request.url.host == "gemini.test" else other
        return target.handle(request)

    async with open_model(GEMINI, transport=httpx2.MockTransport(route)) as opened:
        m = opened.model
        with pytest.raises(error) as excinfo:
            await Runner.run(_agent(m, GEMINI, executed), "hi")

    assert len(endpoint.requests) == 2  # 失败请求只发出一次
    assert other.requests == []  # 不跟随跨端点重定向，也不切换其他端点
    assert executed == ["east"]  # 工具只执行一次
    assert GEMINI_KEY not in repr(excinfo.value)


async def test_upstream_error_body_reaches_the_raw_exception(no_ambient_openai_env: None) -> None:
    """现状记录：SDK 客户端把上游错误体放进异常消息。

    因此原始异常不能直接展示或记录；应用出口（Task 5）必须映射为固定的受控失败。
    """
    endpoint = Endpoint([_status(400)])
    async with open_model(GEMINI, transport=endpoint.transport) as opened:
        m = opened.model
        with pytest.raises(APIStatusError) as excinfo:
            await Runner.run(_agent(m, GEMINI, []), "hi")
    assert "upstream-detail" in str(excinfo.value)
    assert GEMINI_KEY not in repr(excinfo.value)


# ---- 请求与响应限额 ----------------------------------------------------------------------


async def test_request_over_limit_is_rejected_before_sending(no_ambient_openai_env: None) -> None:
    profile = GEMINI.model_copy(update={"max_request_bytes": 2_000})
    endpoint = Endpoint([_chat_text(FINAL)])
    async with open_model(profile, transport=endpoint.transport) as opened:
        m = opened.model
        with pytest.raises(ModelRequestRejectedError) as excinfo:
            await Runner.run(_agent(m, profile, []), "x" * 5_000)
    assert endpoint.requests == []
    assert "xxxx" not in str(excinfo.value)
    assert GEMINI_KEY not in str(excinfo.value)


@dataclass
class _ChunkStream(httpx2.AsyncByteStream):
    """按块产生响应体，并记录被读取了多少块。"""

    chunks: list[bytes]
    pulled: int = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk


async def test_response_over_limit_stops_reading_early(no_ambient_openai_env: None) -> None:
    profile = GEMINI.model_copy(update={"max_response_bytes": 4_096})
    stream = _ChunkStream([b" " * 1_024] * 64)  # 64 KiB，无 Content-Length
    endpoint = Endpoint(
        [httpx2.Response(200, headers={"content-type": "application/json"}, stream=stream)]
    )
    async with open_model(profile, transport=endpoint.transport) as opened:
        m = opened.model
        with pytest.raises(ModelResponseRejectedError):
            await Runner.run(_agent(m, profile, []), "hi")
    assert len(endpoint.requests) == 1
    assert stream.pulled <= 5  # 超过上限即停止，不读完整个响应


@pytest.mark.parametrize("status", [200, 503])
async def test_compressed_response_is_rejected(status: int, no_ambient_openai_env: None) -> None:
    """小于上限的压缩体解压后可以任意大；错误响应同样会被客户端解压读取。"""
    body = gzip.compress(json.dumps(_chat_text(FINAL)).encode())
    profile = GEMINI.model_copy(update={"max_response_bytes": 4_096})
    endpoint = Endpoint(
        [httpx2.Response(status, headers={"content-encoding": "gzip"}, stream=_ChunkStream([body]))]
    )
    async with open_model(profile, transport=endpoint.transport) as opened:
        m = opened.model
        with pytest.raises(ModelResponseRejectedError):
            await Runner.run(_agent(m, profile, []), "hi")
    # 请求声明只接受未压缩内容。
    assert endpoint.requests[0].headers["accept-encoding"] == "identity"


# ---- Profile 校验 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"base_url": "http://api.openai.test/v1"}, id="plain-http"),
        pytest.param({"base_url": "https://user:pw@api.openai.test/v1"}, id="userinfo"),
        pytest.param({"base_url": "https://api.openai.test/v1?key=x"}, id="query"),
        pytest.param({"base_url": "https:///v1"}, id="no-host"),
        pytest.param({"model": ""}, id="empty-model"),
        pytest.param({"api_key_ref": "sk-raw-key-pasted"}, id="raw-key-as-ref"),
        pytest.param({"request_timeout_seconds": 0}, id="zero-timeout"),
        pytest.param({"max_output_tokens": 0}, id="zero-tokens"),
        pytest.param({"max_request_bytes": -1}, id="negative-request-bytes"),
        pytest.param({"max_response_bytes": 0}, id="zero-response-bytes"),
        pytest.param({"provider": "deepseek"}, id="responses-for-deepseek"),
        pytest.param({"output_mode": "json_object"}, id="json-object-on-responses"),
        pytest.param({"reasoning_effort": "extreme"}, id="unknown-effort"),
        pytest.param(
            {
                "provider": "deepseek",
                "api_mode": "chat_completions",
                "reasoning_effort": "high",
            },
            id="effort-not-verified-for-provider",
        ),
        pytest.param({"temperature": 0.2}, id="unmapped-parameter"),
    ],
)
def test_invalid_profiles_are_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _profile(**change)


def test_profile_is_immutable() -> None:
    with pytest.raises(ValidationError):
        OPENAI.model = "other"


# ---- 凭据引用 --------------------------------------------------------------------------


def test_secret_ref_resolves_only_from_named_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIAOWEI_TEST_GEMINI_KEY", GEMINI_KEY)
    assert resolve_secret_ref(GEMINI.api_key_ref).get_secret_value() == GEMINI_KEY

    monkeypatch.delenv("XIAOWEI_TEST_GEMINI_KEY")
    with pytest.raises(SecretRefError) as excinfo:
        resolve_secret_ref(GEMINI.api_key_ref)
    assert "XIAOWEI_TEST_GEMINI_KEY" in str(excinfo.value)

    monkeypatch.setenv("XIAOWEI_TEST_GEMINI_KEY", "")
    with pytest.raises(SecretRefError):
        resolve_secret_ref(GEMINI.api_key_ref)

    for bad in ("sk-raw-key", "file:/etc/passwd", "env:", "env:lower"):
        with pytest.raises(SecretRefError) as excinfo:
            resolve_secret_ref(bad)
        assert str(excinfo.value) == "凭据引用必须是 env:NAME 形式"
