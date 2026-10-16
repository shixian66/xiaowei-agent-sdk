"""请求体不能伪造服务端身份、scope 或 trace 上下文。"""

import datetime as dt
from typing import Any

import httpx
import pytest

from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import ReadinessReport, TaskStatus, TaskView, task_query_path
from xiaowei_agent.interfaces.api import create_app
from xiaowei_agent.trace import get_trace_id

pytestmark = pytest.mark.security


class _Service:
    def __init__(self) -> None:
        self.submissions: list[Any] = []

    async def submit_task(self, *, submission: Any) -> TaskView:
        self.submissions.append(submission)
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
        settings=Settings(environment_id="dev", actor="trusted-actor"),
        readiness=_Probe(),
        clock=lambda: dt.datetime(2026, 9, 5, tzinfo=dt.UTC),
        policy_revision="policy-2026-09-01",
    )


async def _post(app: Any, body: dict[str, object]) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        return await client.post("/v1/tasks", json=body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "environment_id",
        "actor",
        "trace_id",
        "request_id",
        "channel",
        "policy_revision",
    ],
)
async def test_every_server_owned_field_is_rejected_as_extra_input(field: str) -> None:
    service = _Service()
    response = await _post(
        _app(service),
        {"text": "检查慢查询", "idempotency_key": "idem-1", field: "forged"},
    )
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid_request"}}
    assert service.submissions == []


@pytest.mark.asyncio
async def test_concurrent_requests_receive_distinct_server_trace_ids() -> None:
    import asyncio

    service = _Service()
    app = _app(service)
    await asyncio.gather(
        _post(app, {"text": "检查慢查询", "idempotency_key": "idem-1"}),
        _post(app, {"text": "检查慢查询", "idempotency_key": "idem-2"}),
    )
    trace_ids = {item.context.trace_id for item in service.submissions}
    assert len(trace_ids) == 2
    assert all(item.envelope.request_id not in trace_ids for item in service.submissions)


@pytest.mark.asyncio
async def test_trace_binding_matches_submission_and_resets_after_failure() -> None:
    class TraceService(_Service):
        def __init__(self) -> None:
            super().__init__()
            self.bound: list[str | None] = []

        async def submit_task(self, *, submission: Any) -> TaskView:
            self.bound.append(get_trace_id())
            self.submissions.append(submission)
            if len(self.bound) == 1:
                raise RuntimeError("private failure")
            return TaskView(
                task_id="task-1",
                status=TaskStatus.CREATED,
                render=None,
                query_path=task_query_path("task-1"),
            )

    service = TraceService()
    app = _app(service)
    first = await _post(app, {"text": "检查慢查询", "idempotency_key": "idem-1"})
    assert first.status_code == 500
    assert get_trace_id() is None
    second = await _post(app, {"text": "检查慢查询", "idempotency_key": "idem-2"})
    assert second.status_code == 202
    assert service.bound == [
        service.submissions[0].context.trace_id,
        service.submissions[1].context.trace_id,
    ]
    assert service.bound[0] != service.bound[1]
    assert get_trace_id() is None
