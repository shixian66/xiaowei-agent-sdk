"""薄 FastAPI gateway 的状态码、投影与健康端点契约。"""

import datetime as dt
from typing import Any

import httpx
import pytest

from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import ReadinessReport, TaskStatus, TaskView, task_query_path
from xiaowei_agent.interfaces.api import create_app
from xiaowei_agent.persistence import (
    IdempotencyConflictError,
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
    TaskNotFoundError,
)

_NOW = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)


def _view(task_id: str = "task-1") -> TaskView:
    return TaskView(
        task_id=task_id,
        status=TaskStatus.CREATED,
        render=None,
        query_path=task_query_path(task_id),
    )


class _Service:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.submissions: list[Any] = []
        self.lookups: list[Any] = []

    async def submit_task(self, *, submission: Any) -> TaskView:
        self.submissions.append(submission)
        if self.failure is not None:
            raise self.failure
        return _view()

    async def query_task(self, *, lookup: Any) -> TaskView:
        self.lookups.append(lookup)
        if self.failure is not None:
            raise self.failure
        return _view(lookup.task_id)


class _Probe:
    def __init__(self, report: ReadinessReport) -> None:
        self.report = report

    async def check(self) -> ReadinessReport:
        return self.report


def _app(service: Any, *, ready: bool = True) -> Any:
    return create_app(
        runtime=service,
        settings=Settings(environment_id="dev", actor="local-developer"),
        readiness=_Probe(
            ReadinessReport(
                database_ok=ready,
                revision_matches_head=ready,
                assembled=ready,
            )
        ),
        clock=lambda: _NOW,
        policy_revision="policy-2026-09-01",
    )


async def _request(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_post_submits_once_and_returns_runtime_projection_verbatim() -> None:
    service = _Service()
    response = await _request(
        _app(service),
        "POST",
        "/v1/tasks",
        json={"text": "检查慢查询", "idempotency_key": "idem-1"},
    )

    assert response.status_code == 202
    assert response.json() == _view().model_dump(mode="json")
    assert len(service.submissions) == 1
    submission = service.submissions[0]
    assert submission.context.actor == "local-developer"
    assert submission.context.tenant_id == "dev-local"
    assert submission.context.environment_id == "dev"
    assert submission.envelope.channel.value == "api"
    assert submission.as_of == _NOW


@pytest.mark.asyncio
async def test_get_uses_trusted_scope_and_returns_runtime_projection() -> None:
    service = _Service()
    response = await _request(_app(service), "GET", "/v1/tasks/task-1")

    assert response.status_code == 200
    assert response.json() == _view().model_dump(mode="json")
    assert service.lookups[0].tenant_id == "dev-local"
    assert service.lookups[0].environment_id == "dev"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (IdempotencyConflictError("conflict"), 409, "idempotency_conflict"),
        (TaskNotFoundError(task_id="private-task"), 404, "not_found"),
        (
            PersistenceUnavailableError(category=PersistenceUnavailableCategory.CONNECT),
            503,
            "unavailable",
        ),
        (RuntimeError("private internal text"), 500, "internal_error"),
    ],
)
async def test_application_failures_use_the_closed_error_body(
    failure: Exception, status: int, code: str
) -> None:
    response = await _request(
        _app(_Service(failure)),
        "POST",
        "/v1/tasks",
        json={"text": "检查慢查询", "idempotency_key": "idem-1"},
    )
    assert response.status_code == status
    assert response.json() == {"error": {"code": code}}
    assert "private" not in response.text


@pytest.mark.asyncio
async def test_framework_404_405_and_validation_are_in_the_closed_protocol() -> None:
    app = _app(_Service())
    cases = [
        ("GET", "/missing", None, 404, "not_found"),
        ("DELETE", "/v1/tasks/task-1", None, 405, "method_not_allowed"),
        ("POST", "/v1/tasks", {"text": "x", "idempotency_key": ""}, 400, "invalid_request"),
    ]
    for method, path, body, status, code in cases:
        response = await _request(app, method, path, json=body)
        assert response.status_code == status
        assert response.json() == {"error": {"code": code}}


@pytest.mark.asyncio
async def test_health_readiness_and_slash_redirect_behavior() -> None:
    app = _app(_Service())
    assert (await _request(app, "GET", "/healthz")).json() == {"status": "ok"}
    ready = await _request(app, "GET", "/readyz")
    assert ready.status_code == 200
    assert ready.json() == {
        "database_ok": True,
        "revision_matches_head": True,
        "assembled": True,
    }
    unavailable = await _request(_app(_Service(), ready=False), "GET", "/readyz")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"error": {"code": "unavailable"}}

    for path in ("/v1/tasks/", "/healthz/", "/readyz/"):
        response = await _request(app, "GET", path)
        assert response.status_code == 404
        assert response.json() == {"error": {"code": "not_found"}}

    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await _request(app, "GET", path)).status_code == 404


@pytest.mark.asyncio
async def test_api_get_preserves_runtime_query_purity() -> None:
    from tests.fakes.recordings import GOLDEN
    from tests.fakes.runtime import RuntimeHarness

    harness = RuntimeHarness(GOLDEN)
    await harness.handle("最近30分钟有哪些慢查询")
    before = await harness.store.get(lookup=harness.lookup)
    audit_count = len(harness.state.audit_events[harness.task_id])
    evidence_count = len(await harness.ledger.load(task_id=harness.task_id))
    gateway_count = harness.gateway.invocations
    response = await _request(_app(harness.runtime), "GET", f"/v1/tasks/{harness.task_id}")

    after = await harness.store.get(lookup=harness.lookup)
    assert response.status_code == 200
    assert after.version == before.version
    assert len(harness.state.audit_events[harness.task_id]) == audit_count
    assert len(await harness.ledger.load(task_id=harness.task_id)) == evidence_count
    assert harness.gateway.invocations == gateway_count


def test_api_never_projects_internal_task_record_fields() -> None:
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "src/xiaowei_agent/interfaces/api.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {
        "status",
        "version",
        "terminal_reason",
        "lease_owner",
        "lease_expires_at",
        "fencing_token",
    }
    assert not {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    }
