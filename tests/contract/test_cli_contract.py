"""标准库 CLI 与 HTTP API 的请求/响应契约。"""

import io
import json
import urllib.error
from email.message import Message
from typing import Any

import pytest

from xiaowei_agent.contracts import TaskStatus, TaskView, task_query_path
from xiaowei_agent.interfaces.cli import CLI_RESPONSE_LIMIT, run_cli


def _view(task_id: str = "task-1") -> bytes:
    return (
        TaskView(
            task_id=task_id,
            status=TaskStatus.CREATED,
            render=None,
            query_path=task_query_path(task_id),
        )
        .model_dump_json()
        .encode()
    )


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self._body = body
        self.status = status
        self.headers = Message()
        self.headers["content-type"] = content_type
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._body if size < 0 else self._body[:size]

    def close(self) -> None:
        self.closed = True


class _Opener:
    def __init__(self, response: _Response | None = None, failure: Exception | None = None) -> None:
        self.response = response or _Response(_view())
        self.failure = failure
        self.requests: list[Any] = []
        self.timeouts: list[float] = []

    def __call__(self, request: Any, *, timeout: float) -> _Response:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.failure is not None:
            raise self.failure
        return self.response


def _run(argv: list[str], opener: _Opener) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_cli(argv, opener=opener, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def test_submit_request_contains_only_the_api_body_fields_and_utf8() -> None:
    opener = _Opener()
    code, _, _ = _run(
        [
            "--base-url",
            "http://127.0.0.1:8000",
            "task",
            "submit",
            "--text",
            "😀" * 8192,
            "--idempotency-key",
            "idem-1",
        ],
        opener,
    )
    request = opener.requests[0]
    body = json.loads(request.data)
    assert code == 0
    assert request.method == "POST"
    assert request.full_url == "http://127.0.0.1:8000/v1/tasks"
    assert body == {"text": "😀" * 8192, "idempotency_key": "idem-1"}
    assert len(request.data) < 64 * 1024
    assert request.headers["Content-type"] == "application/json"


def test_get_percent_encodes_the_complete_task_id_and_checks_the_response_id() -> None:
    task_id = "task/with?reserved#chars%"
    opener = _Opener(_Response(_view(task_id)))
    code, _, _ = _run(
        ["--base-url", "https://localhost:8443", "task", "get", task_id],
        opener,
    )
    assert code == 0
    assert opener.requests[0].full_url.endswith("/v1/tasks/task%2Fwith%3Freserved%23chars%25")

    mismatch = _Opener(_Response(_view("another-task")))
    code, _, _ = _run(["task", "get", task_id], mismatch)
    assert code == 2


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, 0),
        (302, 2),
        (400, 2),
        (404, 3),
        (409, 4),
        (413, 5),
        (418, 2),
        (503, 6),
        (504, 7),
        (500, 8),
        (502, 8),
        (599, 8),
    ],
)
def test_http_status_to_exit_code_is_a_total_mapping(status: int, expected: int) -> None:
    opener = _Opener(_Response(_view(), status=status))
    code, _, _ = _run(["task", "submit", "--text", "x", "--idempotency-key", "i"], opener)
    assert code == expected


def test_connection_and_timeout_have_distinct_exit_codes() -> None:
    refused = _Opener(failure=urllib.error.URLError(OSError("private network detail")))
    timeout = _Opener(failure=TimeoutError("private timeout detail"))
    assert _run(["task", "get", "task-1"], refused)[0] == 6
    assert _run(["task", "get", "task-1"], timeout)[0] == 7


def test_response_is_read_with_a_hard_one_megabyte_limit() -> None:
    base = _view()
    exact = _Response(base + b" " * (CLI_RESPONSE_LIMIT - len(base)))
    assert _run(["task", "get", "task-1"], _Opener(exact))[0] == 0
    assert exact.read_sizes == [CLI_RESPONSE_LIMIT + 1]

    too_large = _Response(base + b" " * (CLI_RESPONSE_LIMIT + 1 - len(base)))
    assert _run(["task", "get", "task-1"], _Opener(too_large))[0] == 2
    assert too_large.read_sizes == [CLI_RESPONSE_LIMIT + 1]


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"not-json", "application/json"),
        (_view(), "text/plain"),
        (b"{}", "application/json"),
        (_view()[:-1] + b',"extra":1}', "application/json"),
    ],
)
def test_invalid_success_response_is_a_protocol_error(body: bytes, content_type: str) -> None:
    assert (
        _run(
            ["task", "get", "task-1"],
            _Opener(_Response(body, content_type=content_type)),
        )[0]
        == 2
    )


@pytest.mark.parametrize(
    "base_url",
    ["file:///etc/passwd", "ftp://localhost/data", "http://user:pass@localhost"],
)
def test_invalid_base_url_is_rejected_without_calling_the_opener(base_url: str) -> None:
    opener = _Opener()
    code, _, _ = _run(["--base-url", base_url, "task", "get", "task-1"], opener)
    assert code == 2
    assert opener.requests == []


def test_identity_and_trace_override_flags_do_not_exist() -> None:
    for flag in ("--tenant-id", "--environment-id", "--actor", "--trace-id"):
        opener = _Opener()
        code, _, _ = _run([flag, "forged", "task", "get", "task-1"], opener)
        assert code == 2
        assert opener.requests == []
