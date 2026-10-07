"""V1-A：Vertex Express Mode ``generateContent`` 与 SDK 公开 ``Model`` 接口的协议契约。

全部经过真实 ``Runner`` 与 ``open_model`` 装配的受控 transport；Vertex 端点由
``httpx2.MockTransport`` 模拟，不建立任何网络连接。协议替身通过不等于真实 Vertex 已接受
这些请求（I-V 真实 Gate 另行验证）。
"""

import asyncio
import gzip
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from agents import (
    Agent,
    AgentOutputSchema,
    ModelSettings,
    ModelTracing,
    Runner,
    WebSearchTool,
    function_tool,
    handoff,
)
from agents.items import ToolCallItem
from pydantic import BaseModel

from xiaowei.model_api import (
    VERTEX_ENDPOINT,
    ModelAPIRejectedError,
    ModelBinding,
    ModelProfile,
    ModelRequestRejectedError,
    ModelResponseRejectedError,
    open_model,
    settings_for,
)

KEY = "fake-vertex-key"
VERTEX = ModelProfile(
    profile_id="vertex-main",
    provider="vertex",
    model="gemini-3-flash-preview",
    api_key_ref="env:XIAOWEI_TEST_VERTEX_KEY",
    request_timeout_seconds=7.5,
    max_output_tokens=256,
    max_request_bytes=64_000,
    max_response_bytes=64_000,
    data_policy_id="synthetic-only",
    reasoning_effort=None,
)
URL = VERTEX_ENDPOINT.format(model="gemini-3-flash-preview")
SIG_A = "c2lnbmF0dXJlLUE="
SIG_B = "c2lnbmF0dXJlLUI="
INSTRUCTIONS = "使用工具回答。"


class TotalAnswer(BaseModel):
    total: int
    note: str


FINAL = json.dumps({"total": 100, "note": "合成数据"}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def vertex_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIAOWEI_TEST_VERTEX_KEY", KEY)


# ---- Vertex 协议的最小响应 ------------------------------------------------------------------


def _body(
    parts: list[dict[str, Any]],
    *,
    finish: str | None = "STOP",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate: dict[str, Any] = {"index": 0, "content": {"role": "model", "parts": parts}}
    if finish is not None:
        candidate["finishReason"] = finish
    body: dict[str, Any] = {
        "candidates": [candidate],
        "modelVersion": "gemini-3-flash-preview",
        "responseId": "vertex-response-1",
    }
    if usage is not None:
        body["usageMetadata"] = usage
    return body


def _call_part(
    name: str = "synthetic_total",
    args: object = None,
    *,
    signature: object = SIG_A,
    call_id: str | None = None,
) -> dict[str, Any]:
    call: dict[str, Any] = {"name": name, "args": {"region": "east"} if args is None else args}
    if call_id is not None:
        call["id"] = call_id
    part: dict[str, Any] = {"functionCall": call}
    if signature is not None:
        part["thoughtSignature"] = signature
    return part


def _call(**kwargs: Any) -> dict[str, Any]:
    return _body([_call_part(**kwargs)])


def _answer(text: str = FINAL, **kwargs: Any) -> dict[str, Any]:
    return _body([{"text": text}], **kwargs)


# ---- 记录请求的 mock 端点 -------------------------------------------------------------------

Reply = dict[str, Any] | httpx2.Response | Callable[[httpx2.Request], Awaitable[httpx2.Response]]


@dataclass
class Endpoint:
    """按顺序回放脚本化响应，并记录收到的每个请求。"""

    replies: list[Reply]
    requests: list[httpx2.Request] = field(default_factory=list)

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("未预期的额外请求")
        reply = self.replies.pop(0)
        if callable(reply):
            return await reply(request)
        if isinstance(reply, httpx2.Response):
            return reply
        return httpx2.Response(200, json=reply)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)


def _agent(binding: ModelBinding, executed: list[str], *, tag: str = "") -> Agent[None]:
    @function_tool
    def synthetic_total(region: str) -> str:
        """返回合成地区的订单总数。"""
        executed.append(region)
        return json.dumps({"region": region, "total": 100, "tag": tag})

    return Agent(
        name="vertex-contract",
        instructions=INSTRUCTIONS,
        model=binding.model,
        model_settings=binding.settings,
        tools=[synthetic_total],
        output_type=TotalAnswer,
    )


async def _run(endpoint: Endpoint, executed: list[str], text: str = "东区订单总数？") -> Any:
    async with open_model(VERTEX, transport=endpoint.transport) as binding:
        return await Runner.run(_agent(binding, executed), text)


# ---- 成功：工具往返与强制结构化最终回答 -------------------------------------------------------


async def test_runner_tool_roundtrip_with_forced_structured_answer() -> None:
    endpoint = Endpoint([_call(), _answer()])
    executed: list[str] = []

    result = await _run(endpoint, executed)

    assert result.final_output == TotalAnswer(total=100, note="合成数据")
    assert executed == ["east"]
    assert len(endpoint.requests) == 2
    tool = _agent_tool()
    schema = AgentOutputSchema(TotalAnswer).json_schema()
    for request, body in zip(endpoint.requests, endpoint.bodies(), strict=True):
        assert request.method == "POST"
        assert str(request.url) == URL
        # 每一步都同时携带函数声明与供应商原生的强制结构化输出约束。
        assert body["tools"] == [
            {
                "functionDeclarations": [
                    {
                        "name": "synthetic_total",
                        "description": tool.description,
                        "parametersJsonSchema": tool.params_json_schema,
                    }
                ]
            }
        ]
        assert body["generationConfig"] == {
            "candidateCount": 1,
            "maxOutputTokens": 256,
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
        }
        assert body["systemInstruction"] == {"parts": [{"text": INSTRUCTIONS}]}
    first, second = endpoint.bodies()
    assert first["contents"] == [{"role": "user", "parts": [{"text": "东区订单总数？"}]}]
    # 原 functionCall Part 与 signature 原样回传，紧跟同名 functionResponse；
    # 没有供应商 id 时不发 id。
    assert second["contents"] == [
        {"role": "user", "parts": [{"text": "东区订单总数？"}]},
        {"role": "model", "parts": [_call_part()]},
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": "synthetic_total",
                        "response": {
                            "output": json.dumps({"region": "east", "total": 100, "tag": ""})
                        },
                    }
                }
            ],
        },
    ]


def _agent_tool() -> Any:
    """与 ``_agent`` 中同名同签名的工具，供直接调用 Model 与核对声明使用。"""
    (tool,) = _agent_tools()
    return tool


def _agent_tools() -> list[Any]:
    return list(Agent(name="x", tools=_tool_list()).tools)


def _tool_list() -> list[Any]:
    @function_tool
    def synthetic_total(region: str) -> str:
        """返回合成地区的订单总数。"""
        return region

    return [synthetic_total]


async def test_key_is_only_in_the_vertex_header(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    endpoint = Endpoint([_call(), _answer()])
    await _run(endpoint, [])
    for request in endpoint.requests:
        assert request.headers["x-goog-api-key"] == KEY
        assert "authorization" not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["content-type"] == "application/json"
        assert KEY not in str(request.url)
        assert KEY.encode() not in request.content
    assert KEY not in caplog.text


async def test_function_call_carries_signature_and_a_fresh_call_id() -> None:
    endpoint = Endpoint([_call(), _call(args={"region": "west"}, signature=SIG_B), _answer()])
    executed: list[str] = []

    result = await _run(endpoint, executed)

    calls = [item.raw_item for item in result.new_items if isinstance(item, ToolCallItem)]
    assert [c.name for c in calls] == ["synthetic_total", "synthetic_total"]
    call_ids = [c.call_id for c in calls]
    assert all(call_ids) and len(set(call_ids)) == 2
    assert [c.provider_data for c in calls] == [
        {"thought_signature": SIG_A},
        {"thought_signature": SIG_B},
    ]
    assert executed == ["east", "west"]
    # 第三步按顺序回放两组调用与结果，签名各归其位；生成的调用 ID 不发给 Vertex。
    third = endpoint.bodies()[2]["contents"]
    assert [c["role"] for c in third] == ["user", "model", "user", "model", "user"]
    assert third[1]["parts"] == [_call_part()]
    assert third[3]["parts"] == [_call_part(args={"region": "west"}, signature=SIG_B)]
    for content in (third[2], third[4]):
        (part,) = content["parts"]
        assert set(part["functionResponse"]) == {"name", "response"}


async def test_vertex_supplied_call_id_is_used_and_echoed() -> None:
    endpoint = Endpoint([_call(call_id="vertex-call-7"), _answer()])

    result = await _run(endpoint, [])

    (call,) = [item.raw_item for item in result.new_items if isinstance(item, ToolCallItem)]
    assert call.call_id == "vertex-call-7"
    contents = endpoint.bodies()[1]["contents"]
    assert contents[1]["parts"] == [_call_part(call_id="vertex-call-7")]
    assert contents[2]["parts"][0]["functionResponse"]["id"] == "vertex-call-7"


async def test_concurrent_runs_keep_their_own_call_mapping() -> None:
    """同一 Model 实例上的两次并发运行使用同名工具，各自的结果只回到自己的请求中。"""

    async def reply(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        region = body["contents"][0]["parts"][0]["text"]
        await asyncio.sleep(0)
        if len(body["contents"]) == 1:
            return httpx2.Response(200, json=_call(args={"region": region}))
        return httpx2.Response(200, json=_answer())

    seen: list[dict[str, Any]] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return await reply(request)

    executed: dict[str, list[str]] = {"east": [], "west": []}
    async with open_model(VERTEX, transport=httpx2.MockTransport(handle)) as binding:
        await asyncio.gather(
            *(Runner.run(_agent(binding, executed[r], tag=r), r) for r in ("east", "west"))
        )

    assert executed == {"east": ["east"], "west": ["west"]}
    finals = [body for body in seen if len(body["contents"]) == 3]
    assert len(finals) == 2
    for body in finals:
        region = body["contents"][0]["parts"][0]["text"]
        output = body["contents"][2]["parts"][0]["functionResponse"]["response"]["output"]
        assert json.loads(output) == {"region": region, "total": 100, "tag": region}


async def test_text_part_signature_is_ignored() -> None:
    endpoint = Endpoint([_body([{"text": FINAL, "thoughtSignature": SIG_A}])])

    result = await _run(endpoint, [])

    assert result.final_output == TotalAnswer(total=100, note="合成数据")
    (response,) = result.raw_responses
    (message,) = response.output
    assert getattr(message, "provider_data", None) is None


async def test_text_split_across_parts_is_joined() -> None:
    half = len(FINAL) // 2
    endpoint = Endpoint([_body([{"text": FINAL[:half]}, {"text": FINAL[half:]}])])
    result = await _run(endpoint, [])
    assert result.final_output == TotalAnswer(total=100, note="合成数据")


async def test_usage_keeps_only_known_integer_counts() -> None:
    usage = {
        "promptTokenCount": 11,
        "candidatesTokenCount": 7,
        "thoughtsTokenCount": 5,
        "totalTokenCount": 23,
        "trafficType": "ON_DEMAND",
        "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 11}],
    }
    endpoint = Endpoint([_answer(usage=usage)])

    result = await _run(endpoint, [])

    (response,) = result.raw_responses
    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 12
    assert response.usage.output_tokens_details.reasoning_tokens == 5
    assert response.usage.total_tokens == 23
    assert response.raw_usage == {
        "promptTokenCount": 11,
        "candidatesTokenCount": 7,
        "thoughtsTokenCount": 5,
        "totalTokenCount": 23,
    }


async def test_missing_usage_is_not_recorded_as_zero() -> None:
    endpoint = Endpoint([_answer()])
    result = await _run(endpoint, [])
    (response,) = result.raw_responses
    assert response.raw_usage is None


# ---- 请求在发出前拒绝 -----------------------------------------------------------------------


async def _direct(
    binding: ModelBinding,
    *,
    input: Any = "hi",
    tools: list[Any] | None = None,
    output_schema: Any = "default",
    handoffs: list[Any] | None = None,
    settings: ModelSettings | None = None,
    **kwargs: Any,
) -> Any:
    return await binding.model.get_response(
        INSTRUCTIONS,
        input,
        settings or binding.settings,
        tools if tools is not None else [_agent_tool()],
        AgentOutputSchema(TotalAnswer) if output_schema == "default" else output_schema,
        handoffs or [],
        ModelTracing.DISABLED,
        previous_response_id=kwargs.get("previous_response_id"),
        conversation_id=kwargs.get("conversation_id"),
        prompt=kwargs.get("prompt"),
    )


_OTHER_AGENT: Agent[None] = Agent(name="other")


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"previous_response_id": "resp-1"}, id="previous-response-id"),
        pytest.param({"conversation_id": "conv-1"}, id="conversation-id"),
        pytest.param({"prompt": {"id": "pmpt_1"}}, id="prompt"),
        pytest.param({"handoffs": [handoff(_OTHER_AGENT)]}, id="handoff"),
        pytest.param({"tools": [WebSearchTool()]}, id="hosted-tool"),
        pytest.param({"output_schema": None}, id="plain-text-output"),
        pytest.param({"settings": ModelSettings(temperature=0.2)}, id="unmapped-setting"),
        pytest.param({"settings": ModelSettings(tool_choice="required")}, id="tool-choice"),
        pytest.param({"settings": ModelSettings(parallel_tool_calls=True)}, id="parallel-calls"),
        pytest.param({"input": [{"role": "system", "content": "x"}]}, id="system-item"),
        pytest.param({"input": [{"role": "developer", "content": "x"}]}, id="developer-item"),
        pytest.param({"input": [{"type": "reasoning", "id": "r", "summary": []}]}, id="reasoning"),
        pytest.param(
            {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]},
            id="image-part",
        ),
        pytest.param(
            {"input": [{"type": "function_call_output", "call_id": "nope", "output": "{}"}]},
            id="orphan-tool-output",
        ),
        pytest.param(
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "synthetic_total",
                        "arguments": "[1]",
                    }
                ]
            },
            id="non-object-arguments",
        ),
        pytest.param(
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "synthetic_total",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "c1",
                        "output": [{"type": "input_text", "text": "x"}],
                    },
                ]
            },
            id="non-text-tool-output",
        ),
        pytest.param(
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "synthetic_total",
                        "arguments": "{}",
                        "provider_data": {"thought_signature": ""},
                    }
                ]
            },
            id="empty-input-signature",
        ),
    ],
)
async def test_unsupported_requests_are_rejected_before_sending(change: dict[str, Any]) -> None:
    endpoint = Endpoint([_answer()])
    async with open_model(VERTEX, transport=endpoint.transport) as binding:
        with pytest.raises(ModelRequestRejectedError):
            await _direct(binding, **change)
    assert endpoint.requests == []


async def test_tool_outputs_keep_the_name_of_their_own_call() -> None:
    """结果按 ``call_id`` 找回各自的函数名，不按位置或最近一次调用猜测。"""

    def call(call_id: str, name: str) -> dict[str, Any]:
        return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}

    def output(call_id: str) -> dict[str, Any]:
        return {"type": "function_call_output", "call_id": call_id, "output": call_id}

    endpoint = Endpoint([_answer()])
    async with open_model(VERTEX, transport=endpoint.transport) as binding:
        await _direct(
            binding,
            input=[
                call("xw-vertex-a", "first_tool"),
                call("vertex-b", "second_tool"),
                output("xw-vertex-a"),
                output("vertex-b"),
            ],
        )
    contents = endpoint.bodies()[0]["contents"]
    responses = [c["parts"][0]["functionResponse"] for c in contents[2:]]
    assert responses == [
        {"name": "first_tool", "response": {"output": "xw-vertex-a"}},
        {"id": "vertex-b", "name": "second_tool", "response": {"output": "vertex-b"}},
    ]


async def test_streaming_is_not_offered() -> None:
    endpoint = Endpoint([])
    async with open_model(VERTEX, transport=endpoint.transport) as binding:
        stream = binding.model.stream_response(
            INSTRUCTIONS,
            "hi",
            binding.settings,
            [],
            AgentOutputSchema(TotalAnswer),
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
        with pytest.raises(ModelRequestRejectedError):
            await anext(aiter(stream))
    assert endpoint.requests == []


async def test_request_over_limit_is_rejected_before_sending() -> None:
    profile = VERTEX.model_copy(update={"max_request_bytes": 2_000})
    endpoint = Endpoint([_answer()])
    async with open_model(profile, transport=endpoint.transport) as binding:
        with pytest.raises(ModelRequestRejectedError) as excinfo:
            await Runner.run(_agent(binding, []), "x" * 5_000)
    assert endpoint.requests == []
    assert "xxxx" not in str(excinfo.value)


# ---- 响应在交给 Runner 和工具前拒绝 -----------------------------------------------------------


def _two_candidates() -> dict[str, Any]:
    body = _call()
    body["candidates"].append(dict(body["candidates"][0], index=1))
    return body


def _drop(key: str) -> dict[str, Any]:
    body = _call()
    del body["candidates"][0][key]
    return body


def _role(role: str) -> dict[str, Any]:
    body = _call()
    body["candidates"][0]["content"]["role"] = role
    return body


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(_call(signature=None), id="missing-signature"),
        pytest.param(_call(signature=""), id="empty-signature"),
        pytest.param(_call(signature=123), id="non-string-signature"),
        pytest.param(_call(signature="not base64!"), id="malformed-signature"),
        pytest.param(_call(signature="A" * 70_000), id="oversized-signature"),
        pytest.param(_two_candidates(), id="two-candidates"),
        pytest.param({"candidates": []}, id="no-candidates"),
        pytest.param({"promptFeedback": {"blockReason": "SAFETY"}}, id="blocked-prompt"),
        pytest.param(_body([_call_part(), _call_part(signature=None)]), id="two-calls-same-name"),
        pytest.param(_body([{"text": "先查一下"}, _call_part()]), id="text-and-call"),
        pytest.param(_call(name="unknown_tool"), id="unknown-tool"),
        pytest.param(_call(args=["east"]), id="non-object-args"),
        pytest.param(_call(call_id=""), id="empty-call-id"),
        pytest.param(_body([{"thoughtSignature": SIG_A}]), id="signature-only-part"),
        pytest.param(_body([{"inlineData": {"mimeType": "x", "data": "AA=="}}]), id="unknown-part"),
        pytest.param(_body([]), id="no-parts"),
        pytest.param(_body([{"text": FINAL, "thought": True}]), id="thought-part"),
        pytest.param(_drop("content"), id="no-content"),
        pytest.param(_role("user"), id="wrong-role"),
        pytest.param(_body([_call_part()], finish=None), id="missing-finish"),
        pytest.param(_body([_call_part()], finish="MAX_TOKENS"), id="max-tokens"),
        pytest.param(_body([_call_part()], finish="SAFETY"), id="safety"),
        pytest.param(_body([_call_part()], finish="RECITATION"), id="recitation"),
        pytest.param(_body([_call_part()], finish="MALFORMED_FUNCTION_CALL"), id="malformed-call"),
        pytest.param(_body([_call_part()], finish="FINISH_REASON_UNSPECIFIED"), id="unspecified"),
        pytest.param(_body([_call_part()], finish="SOMETHING_NEW"), id="unknown-finish"),
    ],
)
async def test_protocol_violations_fail_before_any_tool_runs(reply: dict[str, Any]) -> None:
    endpoint = Endpoint([reply])
    executed: list[str] = []
    with pytest.raises(ModelResponseRejectedError) as excinfo:
        await _run(endpoint, executed)
    assert executed == []
    assert len(endpoint.requests) == 1
    assert SIG_A not in str(excinfo.value)
    assert "unknown_tool" not in str(excinfo.value)


# ---- 失败：不重试、不 fallback、已执行的工具不重放 ---------------------------------------------


def _http(code: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        code, headers=headers, json={"error": {"message": "upstream-detail", "status": "X"}}
    )


async def _timeout(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("simulated upstream-detail", request=request)


async def _connect_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("simulated upstream-detail", request=request)


async def _invalid_json(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, content=b"{upstream-detail", headers={"content-type": _JSON})


async def _gzip(request: httpx2.Request) -> httpx2.Response:
    body = gzip.compress(json.dumps(_answer()).encode())
    return httpx2.Response(200, headers={"content-encoding": "gzip"}, content=body)


async def _oversized(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, headers={"content-type": _JSON}, content=b" " * 70_000)


_JSON = "application/json"


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(_http(400), id="400"),
        pytest.param(_http(401), id="401"),
        pytest.param(_http(403), id="403"),
        pytest.param(_http(429, {"retry-after": "0"}), id="429"),
        pytest.param(_http(500), id="500"),
        pytest.param(_http(503), id="503"),
        pytest.param(_http(307, {"location": "https://other.test/x"}), id="redirect"),
        pytest.param(_timeout, id="timeout"),
        pytest.param(_connect_error, id="connect-error"),
        pytest.param(_invalid_json, id="invalid-json"),
        pytest.param(_gzip, id="compressed"),
        pytest.param(_oversized, id="oversized"),
    ],
)
async def test_failure_after_a_tool_ran_is_fixed_and_not_retried(failure: Reply) -> None:
    endpoint = Endpoint([_call(), failure])
    executed: list[str] = []

    with pytest.raises(ModelAPIRejectedError) as excinfo:
        await _run(endpoint, executed)

    assert len(endpoint.requests) == 2  # 失败请求只发出一次
    assert executed == ["east"]  # 已执行的工具不重放
    error = excinfo.value
    for text in (str(error), repr(error)):
        assert "upstream-detail" not in text
        assert KEY not in text
    # 没有可展开出上游异常的原因链。
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


async def test_cancellation_propagates_without_retry() -> None:
    started = asyncio.Event()

    async def hang(request: httpx2.Request) -> httpx2.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    endpoint = Endpoint([hang])
    executed: list[str] = []
    task = asyncio.create_task(_run(endpoint, executed))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(endpoint.requests) == 1
    assert executed == []


async def test_model_client_is_closed_on_exit() -> None:
    endpoint = Endpoint([_answer(), _answer()])
    async with open_model(VERTEX, transport=endpoint.transport) as binding:
        await Runner.run(_agent(binding, []), "hi")
    with pytest.raises(RuntimeError):
        await Runner.run(_agent(binding, []), "hi")
    assert len(endpoint.requests) == 1


def test_settings_use_the_profile_limits() -> None:
    settings = settings_for(VERTEX)
    assert settings.max_tokens == 256
    assert settings.store is None
    assert settings.reasoning is None


# ---- 协议边界：严格 JSON、Base64 结构与字段运行时类型（PR #53 审查） ----------------------------


def _raw(body: dict[str, Any], literal: str) -> httpx2.Response:
    """把占位值替换为 JSON 字面量，模拟供应商返回的原始字节。"""
    text = json.dumps(body).replace('"__NONFINITE__"', literal)
    assert literal in text
    return httpx2.Response(200, headers={"content-type": _JSON}, content=text.encode())


_NONFINITE = [
    pytest.param("NaN", id="nan"),
    pytest.param("Infinity", id="infinity"),
    pytest.param("-Infinity", id="negative-infinity"),
    pytest.param("1e999", id="overflowing-number"),
]


@pytest.mark.parametrize("literal", _NONFINITE)
async def test_nonfinite_number_in_response_args_is_rejected_before_any_tool_runs(
    literal: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    args = {"region": "east", "nested": {"values": [1, "__NONFINITE__"]}}
    endpoint = Endpoint([_raw(_call(args=args), literal)])
    executed: list[str] = []

    with pytest.raises(ModelResponseRejectedError) as excinfo:
        await _run(endpoint, executed)

    assert executed == []
    assert len(endpoint.requests) == 1
    error = excinfo.value
    for text in (str(error), repr(error), caplog.text):
        for secret in (literal, SIG_A, KEY, "nested"):
            assert secret not in text
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


def _history(arguments: str, **extra: Any) -> list[dict[str, Any]]:
    call = {
        "type": "function_call",
        "call_id": "vertex-call-1",
        "name": "synthetic_total",
        "arguments": arguments,
        **extra,
    }
    return [
        {"role": "user", "content": "东区订单总数？"},
        call,
        {"type": "function_call_output", "call_id": "vertex-call-1", "output": "{}"},
    ]


async def _run_input(endpoint: Endpoint, items: list[Any]) -> Any:
    async with open_model(VERTEX, transport=endpoint.transport) as binding:
        return await Runner.run(_agent(binding, []), items)


@pytest.mark.parametrize("literal", _NONFINITE)
async def test_nonfinite_number_in_history_args_is_rejected_before_sending(literal: str) -> None:
    endpoint = Endpoint([_answer()])
    arguments = f'{{"region": "east", "nested": [{literal}]}}'

    with pytest.raises(ModelRequestRejectedError) as excinfo:
        await _run_input(endpoint, _history(arguments, provider_data={"thought_signature": SIG_A}))

    assert endpoint.requests == []
    assert literal not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None or excinfo.value.__suppress_context__


_BAD_SIGNATURES = [
    pytest.param("A", id="one-char"),
    pytest.param("A=", id="one-char-padded"),
    pytest.param("AA", id="missing-padding"),
    pytest.param("AA=A", id="padding-in-the-middle"),
    pytest.param("AAAA====", id="excess-padding"),
    pytest.param("AB+_", id="mixed-alphabets"),
]


@pytest.mark.parametrize("signature", _BAD_SIGNATURES)
async def test_structurally_invalid_response_signature_is_rejected(signature: str) -> None:
    endpoint = Endpoint([_call(signature=signature)])
    executed: list[str] = []
    with pytest.raises(ModelResponseRejectedError) as excinfo:
        await _run(endpoint, executed)
    assert executed == []
    assert len(endpoint.requests) == 1
    assert signature not in str(excinfo.value)


@pytest.mark.parametrize("signature", _BAD_SIGNATURES)
async def test_structurally_invalid_history_signature_is_rejected_before_sending(
    signature: str,
) -> None:
    endpoint = Endpoint([_answer()])
    with pytest.raises(ModelRequestRejectedError):
        await _run_input(
            endpoint, _history('{"region": "east"}', provider_data={"thought_signature": signature})
        )
    assert endpoint.requests == []


@pytest.mark.parametrize(
    "signature",
    [
        pytest.param(SIG_A, id="standard"),
        # 末位填充比特非零：解码后再编码会变成 "QQ=="，必须原样回传。
        pytest.param("QR==", id="non-canonical-trailing-bits"),
        pytest.param("-_-_", id="url-safe"),
    ],
)
async def test_valid_signature_is_echoed_byte_for_byte(signature: str) -> None:
    endpoint = Endpoint([_call(signature=signature), _answer()])
    executed: list[str] = []

    await _run(endpoint, executed)

    assert executed == ["east"]
    assert endpoint.bodies()[1]["contents"][1]["parts"] == [_call_part(signature=signature)]


@pytest.mark.parametrize("name", [pytest.param([], id="list"), pytest.param({}, id="dict")])
async def test_unhashable_function_name_is_a_fixed_rejection(name: Any) -> None:
    endpoint = Endpoint([_call(name=name)])
    executed: list[str] = []
    with pytest.raises(ModelResponseRejectedError):
        await _run(endpoint, executed)
    assert executed == []
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize("role", [pytest.param([], id="list"), pytest.param({}, id="dict")])
async def test_unhashable_input_role_is_rejected_before_sending(role: object) -> None:
    endpoint = Endpoint([_answer()])
    with pytest.raises(ModelRequestRejectedError):
        await _run_input(endpoint, [{"role": role, "content": "hi"}])
    assert endpoint.requests == []


async def test_response_without_role_is_accepted() -> None:
    """官方契约中 ``Content.role`` 可省略；只拒绝显式写错的 role。"""
    body = _call()
    del body["candidates"][0]["content"]["role"]
    endpoint = Endpoint([body, _answer()])
    executed: list[str] = []

    result = await _run(endpoint, executed)

    assert result.final_output == TotalAnswer(total=100, note="合成数据")
    assert executed == ["east"]
