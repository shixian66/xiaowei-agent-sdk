"""仅 loopback 的 API v2 替身与官方可丢弃 Alertmanager；无外部通知接收器。"""

import base64
import copy
import json
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from tests.sdk_core.test_runtime import free_port

SELECTED = (
    "get_alerts",
    "get_alert_groups",
    "get_silences",
    "get_silence",
    "get_receivers",
    "get_status",
)
SILENCE_ID = "11111111-1111-4111-8111-111111111111"
PASSWORD = "synthetic-password"  # noqa: S105 - 仅 loopback 合成凭据
AUTHORIZATION = "Basic " + base64.b64encode(f"reader:{PASSWORD}".encode()).decode()


def alert() -> dict[str, Any]:
    return {
        "labels": {"alertname": "HostCPU", "instance": "host1"},
        "annotations": {"summary": "Synthetic CPU alert"},
        "fingerprint": "abcdef0123456789",
        "startsAt": "2026-10-09T00:00:00Z",
        "endsAt": "2026-10-12T00:00:00Z",
        "updatedAt": "2026-10-10T00:00:00Z",
        "status": {
            "state": "suppressed",
            "silencedBy": [SILENCE_ID],
            "inhibitedBy": [],
            "mutedBy": [],
        },
        "receivers": [{"name": "ops"}],
        "generatorURL": "http://private-upstream/token=secret-canary",
    }


def silence() -> dict[str, Any]:
    return {
        "id": SILENCE_ID,
        "matchers": [{"name": "instance", "value": "host1", "isRegex": False, "isEqual": True}],
        "startsAt": "2026-10-09T00:00:00Z",
        "endsAt": "2029-10-09T00:00:00Z",
        "updatedAt": "2026-10-09T00:00:00Z",
        "status": {"state": "active"},
        "comment": "Synthetic maintenance",
        "createdBy": "private-author",
        "annotations": {"private": "secret-canary"},
    }


def responses() -> dict[str, Any]:
    return {
        "/api/v2/alerts": [alert()],
        "/api/v2/alerts/groups": [
            {
                "labels": {"alertname": "HostCPU"},
                "routeLabels": {},
                "receiver": {"name": "ops"},
                "alerts": [alert()],
            }
        ],
        "/api/v2/silences": [silence()],
        f"/api/v2/silence/{SILENCE_ID}": silence(),
        "/api/v2/receivers": [{"name": "ops"}],
        "/api/v2/status": {
            "cluster": {"status": "disabled", "peers": [{"address": "private-peer"}]},
            "versionInfo": {"version": "0.34.1", "buildUser": "private-author"},
            "uptime": "2026-10-09T00:00:00Z",
            "config": {"original": "secret-canary"},
        },
    }


@dataclass
class State:
    payloads: dict[str, Any] = field(default_factory=responses)
    requests: list[tuple[str, str, str | None]] = field(default_factory=list)
    status: int = 200
    delay: float = 0
    body_delay: float = 0
    raw: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    require_auth: bool = True


@contextmanager
def backend() -> Iterator[tuple[str, State]]:
    state = State()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append(("GET", self.path, self.headers.get("authorization")))
            time.sleep(state.delay)
            code = state.status
            if state.require_auth and self.headers.get("authorization") != AUTHORIZATION:
                code = 401
            body = (
                state.raw
                if state.raw is not None
                else json.dumps(
                    copy.deepcopy(state.payloads.get(urlsplit(self.path).path, {}))
                ).encode()
            )
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in state.headers.items():
                self.send_header(name, value)
            self.end_headers()
            time.sleep(state.body_delay)
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 客户端取消/有界超时后的隔离端点。

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def wait_ready(
    url: str, process: subprocess.Popen[bytes], *, auth: tuple[str, str] | None = None
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"隔离服务提前退出：{process.returncode}")
        try:
            with httpx.Client(timeout=0.2, trust_env=False) as client:
                if client.get(url, auth=auth).status_code < 500:
                    return
        except httpx.TransportError:
            pass
        time.sleep(0.02)
    raise AssertionError("隔离服务启动超时")


@contextmanager
def official(binary: Path) -> Iterator[str]:
    """创建/注入仅本机测试数据；这些 POST 不属于产品能力。"""
    with tempfile.TemporaryDirectory(prefix="xw-a4-") as directory:
        root = Path(directory)
        (root / "alertmanager.yml").write_text(
            "route:\n  receiver: ops\nreceivers:\n  - name: ops\n"
        )
        (root / "web.yml").write_text(
            "basic_auth_users:\n  reader: "
            "'$2y$04$.6Ogc8zIUCkRk2/vPiP1f..Z3vcmG5t6FJQZ/g/hx/.uXzc7vWXD6'\n"
        )
        url = f"http://127.0.0.1:{free_port()}"
        process = subprocess.Popen(  # noqa: S603 - 显式提供的官方测试二进制
            [
                str(binary),
                f"--config.file={root / 'alertmanager.yml'}",
                f"--web.config.file={root / 'web.yml'}",
                f"--storage.path={root / 'data'}",
                f"--web.listen-address={url.removeprefix('http://')}",
                "--cluster.listen-address=",
                "--log.level=error",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_ready(f"{url}/-/ready", process, auth=("reader", PASSWORD))
            yield url
        finally:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)


@pytest.fixture
def alertmanager_binary() -> Path:
    value = os.environ.get("XW_TEST_ALERTMANAGER_BIN")
    if not value:
        pytest.skip("需要计划锁定的官方 Alertmanager 二进制")
    path = Path(value)
    assert path.is_file()
    return path


def seed(client: httpx.Client) -> str:
    current = datetime.now(UTC)
    value = silence()
    for key in ("id", "status", "updatedAt"):
        value.pop(key)
    value.update(
        startsAt=(current - timedelta(minutes=1)).isoformat(),
        endsAt=(current + timedelta(days=365 * 3)).isoformat(),
    )
    response = client.post("/api/v2/silences", json=value)
    assert response.status_code == 200
    silence_id: str = response.json()["silenceID"]
    value = alert()
    for key in ("status", "fingerprint", "receivers", "updatedAt"):
        value.pop(key)
    value.update(
        startsAt=(current - timedelta(minutes=2)).isoformat(),
        endsAt=(current + timedelta(hours=1)).isoformat(),
    )
    assert client.post("/api/v2/alerts", json=[value]).status_code == 200
    return silence_id
