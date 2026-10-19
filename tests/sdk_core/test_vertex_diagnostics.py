"""Vertex 429 诊断：只使用合成 HTTP 响应。"""

import asyncio
import logging
from dataclasses import replace

import httpx2
import pytest
from agents import RetryDecision, RetryPolicyContext
from tests.sdk_core.test_vertex_model import KEY, Endpoint, _answer, _body, _call_part, _run

from xiaowei import model_api
from xiaowei.vertex_model import ModelAPIStatusError, VertexTurnMetrics, active_vertex_metrics


@pytest.fixture(autouse=True)
def vertex_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIAOWEI_TEST_VERTEX_KEY", KEY)
    policy = model_api._retry_rejected_load

    def no_wait(context: RetryPolicyContext) -> RetryDecision | bool:
        decision = policy(context)
        return replace(decision, delay=0) if isinstance(decision, RetryDecision) else decision

    monkeypatch.setattr(model_api, "_retry_rejected_load", no_wait)


def _error(status: str = "RESOURCE_EXHAUSTED", details: object = None) -> httpx2.Response:
    return httpx2.Response(
        429,
        headers={"Retry-After": "7"},
        json={
            "error": {
                "status": status,
                "message": "secret-message SELECT private_column FROM private_table",
                "details": [] if details is None else details,
            }
        },
    )


async def _observed(endpoint: Endpoint) -> VertexTurnMetrics:
    metrics = VertexTurnMetrics()
    token = active_vertex_metrics.set(metrics)
    try:
        await _run(endpoint, [])
    finally:
        active_vertex_metrics.reset(token)
    return metrics


async def test_429_quota_details_are_counted_and_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = _error(
        details=[
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "RATE_LIMIT_EXCEEDED",
                "domain": "googleapis.com",
                "metadata": {"consumer": "projects/123456", "api_key": "sk-test-secret"},
            },
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "aiplatform.googleapis.com/predict_requests",
                        "quotaId": "PredictRequestsPerMinute",
                        "quotaDimensions": {"project": "123456"},
                    }
                ],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1.5s"},
        ]
    )
    with caplog.at_level(logging.INFO):
        with pytest.raises(ModelAPIStatusError) as failed:
            await _observed(Endpoint([response] * 4))
    assert failed.value.status_code == 429
    output = "\n".join(record.getMessage() for record in caplog.records)
    for field in (
        "rate_kind=quota",
        "status=RESOURCE_EXHAUSTED",
        "reason=RATE_LIMIT_EXCEEDED",
        "domain=googleapis.com",
        "quota_metric=aiplatform.googleapis.com/predict_requests",
        "quota_id=PredictRequestsPerMinute",
        "retry_delay_s=1.5",
        "retry_after=7",
    ):
        assert field in output
    for forbidden in ("secret-message", "private_column", "123456", "sk-test-secret"):
        assert forbidden not in output


async def test_429_without_quota_detail_is_not_misclassified_as_quota(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        with pytest.raises(ModelAPIStatusError):
            await _observed(Endpoint([_error()] * 4))
    assert "rate_kind=no_quota_detail" in caplog.text


async def test_error_info_quota_reason_is_quota_without_violation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = _error(
        details=[
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "QUOTA_EXCEEDED",
                "domain": "googleapis.com",
                "metadata": {"consumer": "projects/123456"},
            }
        ]
    )
    with caplog.at_level(logging.INFO):
        with pytest.raises(ModelAPIStatusError):
            await _observed(Endpoint([response] * 4))
    assert "rate_kind=quota" in caplog.text
    assert "projects/123456" not in caplog.text


async def test_retry_after_cannot_log_a_secret_shaped_header(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = httpx2.Response(
        429,
        headers={"Retry-After": "sk-test-secret"},
        json={"error": {"status": "RESOURCE_EXHAUSTED"}},
    )
    with caplog.at_level(logging.INFO):
        with pytest.raises(ModelAPIStatusError):
            await _observed(Endpoint([response] * 4))
    assert "retry_after=other" in caplog.text
    assert "sk-test-secret" not in caplog.text


@pytest.mark.parametrize(
    "response,kind,reason",
    [
        (httpx2.Response(429, text="secret-message"), "unknown", "unknown"),
        (httpx2.Response(429, content=b""), "unknown", "unknown"),
        (_error(details={"bad": "shape"}), "unknown", "unknown"),
        (
            _error(
                details=[
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": {"seconds": "5"},
                    }
                ]
            ),
            "unknown",
            "unknown",
        ),
        (
            _error(
                details=[{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "A" * 129}]
            ),
            "unknown",
            "other",
        ),
        (
            _error(
                details=[
                    {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "bad value"}
                ]
            ),
            "unknown",
            "other",
        ),
    ],
)
async def test_bad_429_body_keeps_status_exception_and_safe_values(
    response: httpx2.Response, kind: str, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        with pytest.raises(ModelAPIStatusError) as failed:
            await _observed(Endpoint([response] * 4))
    assert failed.value.status_code == 429
    assert "rate_kind=" + kind in caplog.text
    assert "reason=" + reason in caplog.text
    assert "secret-message" not in caplog.text and "bad value" not in caplog.text


async def test_retry_success_counts_each_http_attempt_and_unknown_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    endpoint = Endpoint(
        [
            _body(
                [_call_part()],
                usage={"promptTokenCount": 11, "candidatesTokenCount": 7, "thoughtsTokenCount": 2},
            ),
            _error(),
            _answer(
                usage={"promptTokenCount": 13, "candidatesTokenCount": 5, "thoughtsTokenCount": 3}
            ),
        ]
    )
    with caplog.at_level(logging.INFO):
        metrics = await _observed(endpoint)
    assert metrics.model_requests == 3
    assert metrics.http_429 == 1
    assert metrics.retries == 1
    assert metrics.input_tokens == 24
    assert metrics.output_tokens == 17
    assert metrics.request_bytes_max == max(len(request.content) for request in endpoint.requests)
    attempts = [r.getMessage() for r in caplog.records if r.name == "xiaowei.vertex_model"]
    assert len(attempts) == 3
    assert "http_status=429" in attempts[1]
    assert "prompt_tokens=13 candidates_tokens=5 thoughts_tokens=3" in attempts[2]


async def test_exhausted_retry_keeps_usage_from_earlier_success() -> None:
    endpoint = Endpoint(
        [
            _body(
                [_call_part()],
                usage={"promptTokenCount": 11, "candidatesTokenCount": 7, "thoughtsTokenCount": 2},
            ),
            *[_error() for _ in range(4)],
        ]
    )
    metrics = VertexTurnMetrics()
    token = active_vertex_metrics.set(metrics)
    try:
        with pytest.raises(ModelAPIStatusError):
            await _run(endpoint, [])
    finally:
        active_vertex_metrics.reset(token)
    assert (metrics.model_requests, metrics.http_429, metrics.retries) == (5, 4, 3)
    assert (metrics.input_tokens, metrics.output_tokens) == (11, 9)


async def test_missing_success_usage_stays_unknown_after_later_known_usage() -> None:
    endpoint = Endpoint(
        [
            _body([_call_part()]),
            _answer(
                usage={"promptTokenCount": 13, "candidatesTokenCount": 5, "thoughtsTokenCount": 3}
            ),
        ]
    )
    metrics = await _observed(endpoint)
    assert metrics.model_requests == 2
    assert metrics.input_tokens is None and metrics.output_tokens is None


async def test_concurrent_runs_keep_separate_counts() -> None:
    async def one(replies: list[object]) -> VertexTurnMetrics:
        endpoint = Endpoint(replies)
        metrics = VertexTurnMetrics()
        token = active_vertex_metrics.set(metrics)
        try:
            await _run(endpoint, [])
        finally:
            active_vertex_metrics.reset(token)
        return metrics

    first, second = await asyncio.gather(
        one(
            [
                _error(),
                _answer(
                    usage={
                        "promptTokenCount": 3,
                        "candidatesTokenCount": 2,
                        "thoughtsTokenCount": 1,
                    }
                ),
            ]
        ),
        one(
            [
                _answer(
                    usage={
                        "promptTokenCount": 5,
                        "candidatesTokenCount": 4,
                        "thoughtsTokenCount": 0,
                    }
                )
            ]
        ),
    )
    assert (first.model_requests, first.http_429, first.retries, first.input_tokens) == (2, 1, 1, 3)
    assert (second.model_requests, second.http_429, second.retries, second.input_tokens) == (
        1,
        0,
        0,
        5,
    )
