"""API 在 Pydantic 前限制 body，并让拒绝响应不回显输入。"""

import datetime as dt
import json
from typing import Any

import httpx
import pytest

from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    ReadinessReport,
    RenderPayload,
    RenderSection,
    TaskStatus,
    TaskView,
    task_query_path,
)
from xiaowei_agent.interfaces.api import create_app

pytestmark = pytest.mark.security


class _Service:
    def __init__(self) -> None:
        self.calls = 0

    async def submit_task(self, *, submission: Any) -> TaskView:
        self.calls += 1
        return TaskView(
            task_id="task-1",
            status=TaskStatus.CREATED,
            render=None,
            query_path=task_query_path("task-1"),
        )

    async def query_task(self, *, lookup: Any) -> TaskView:
        raise AssertionError("not used")


class _Probe:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(database_ok=True, revision_matches_head=True, assembled=True)


def _app(service: _Service) -> Any:
    return create_app(
        runtime=service,
        settings=Settings(environment_id="dev"),
        readiness=_Probe(),
        clock=lambda: dt.datetime(2026, 9, 5, tzinfo=dt.UTC),
        policy_revision="policy-2026-09-01",
    )


async def _post(app: Any, content: bytes | Any, *, content_type: str | None) -> httpx.Response:
    headers = {} if content_type is None else {"content-type": content_type}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        return await client.post("/v1/tasks", content=content, headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    [None, "", "text/json", "application/jsonx", "application/json-patch+json"],
)
async def test_non_json_media_types_are_rejected_before_runtime(
    content_type: str | None,
) -> None:
    service = _Service()
    response = await _post(_app(service), b"{}", content_type=content_type)
    assert response.status_code == 415
    assert response.json() == {"error": {"code": "unsupported_media_type"}}
    assert service.calls == 0


@pytest.mark.asyncio
async def test_json_charset_is_accepted() -> None:
    service = _Service()
    body = json.dumps({"text": "检查慢查询", "idempotency_key": "idem-1"}).encode()
    response = await _post(_app(service), body, content_type="application/json; charset=utf-8")
    assert response.status_code == 202
    assert service.calls == 1


@pytest.mark.asyncio
async def test_largest_legal_escaped_unicode_request_fits_the_body_limit() -> None:
    service = _Service()
    body = json.dumps({"text": "😀" * 8192, "idempotency_key": "idem-1"}).encode()
    assert len(body) > 64 * 1024
    response = await _post(_app(service), body, content_type="application/json")
    assert response.status_code == 202


@pytest.mark.asyncio
async def test_streamed_body_over_limit_is_rejected_before_runtime() -> None:
    service = _Service()

    async def chunks() -> Any:
        yield b"{" + b'"text":"'
        yield b"x" * 131_072
        yield b'","idempotency_key":"idem-1"}'

    response = await _post(_app(service), chunks(), content_type="application/json")
    assert response.status_code == 413
    assert response.json() == {"error": {"code": "payload_too_large"}}
    assert service.calls == 0


@pytest.mark.asyncio
async def test_invalid_json_does_not_echo_secret_shaped_input() -> None:
    service = _Service()
    secret = "token=" + "fake-private-value"
    response = await _post(
        _app(service),
        ("{" + secret).encode(),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid_request"}}
    assert secret not in response.text
    assert service.calls == 0


@pytest.mark.asyncio
async def test_duplicate_media_type_is_rejected_fail_closed() -> None:
    service = _Service()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/v1/tasks",
            content=b"{}",
            headers=[
                ("content-type", "application/json"),
                ("content-type", "text/plain"),
            ],
        )
    assert response.status_code == 415
    assert service.calls == 0


@pytest.mark.asyncio
async def test_external_control_characters_remain_inside_json_strings() -> None:
    class ExternalService(_Service):
        async def query_task(self, *, lookup: Any) -> TaskView:
            body = "line one\n\x1b[31mexternal\x1b[0m"
            render = RenderPayload(
                answer="answer",
                sections=(RenderSection(title="evidence", body=body, refs=("e1",)),),
                next_steps=(),
                status=TaskStatus.SUCCEEDED,
                refs=("e1",),
            )
            return TaskView(
                task_id=lookup.task_id,
                status=TaskStatus.SUCCEEDED,
                render=render,
                query_path=task_query_path(lookup.task_id),
            )

    app = _app(ExternalService())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/v1/tasks/task-1")

    assert response.status_code == 200
    assert "\x1b" not in response.text
    assert "\n" not in response.text
    assert response.json()["render"]["sections"][0]["body"].endswith("external\x1b[0m")
