"""锁版官方 Grafana MCP 与仅 loopback 的合成 Grafana API；不属于产品部署。"""

import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

BINARY_SHA256 = "480c23f5f0c80e7819fa060e12d4faf471732b63bd05a2023e45f868a7f33baf"
SELECTED = (
    "search_dashboards",
    "get_dashboard_summary",
    "get_dashboard_panel_queries",
    "list_datasources",
    "get_annotations",
)
DASHBOARD = {
    "dashboard": {
        "uid": "host",
        "title": "Host CPU",
        "version": 7,
        "schemaVersion": 39,
        "tags": ["host"],
        "description": "Synthetic host metrics",
        "time": {"from": "now-1h", "to": "now"},
        "panels": [
            {
                "id": 1,
                "title": "CPU ratio",
                "type": "timeseries",
                "description": "CPU usage",
                "datasource": {"uid": "prom", "type": "prometheus"},
                "targets": [{"refId": "A", "expr": 'cpu_ratio{instance="$host"}'}],
            }
        ],
        "templating": {"list": [{"name": "host", "type": "custom", "current": {"value": "host1"}}]},
    },
    "meta": {"folderUid": "hosts", "version": 7, "canEdit": True, "createdBy": "private-author"},
}


@dataclass
class BackendState:
    requests: list[tuple[str, dict[str, list[str]], str | None]] = field(default_factory=list)
    status: int = 200
    dashboard: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DASHBOARD)))
    count: int = 1


@contextmanager
def grafana_backend() -> Iterator[tuple[int, BackendState]]:
    state = BackendState()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            args = parse_qs(parsed.query)
            state.requests.append((parsed.path, args, self.headers.get("Authorization")))
            status = state.status
            if parsed.path.startswith("/apis"):
                status, body = 404, {"message": "Not found"}
            elif status != 200:
                body = {"message": "synthetic failure with private detail"}
            elif parsed.path == "/api/search":
                start = (int(args.get("page", ["1"])[0]) - 1) * int(args.get("limit", ["50"])[0])
                body = [
                    {
                        "uid": f"host{i}",
                        "title": "Host CPU",
                        "type": "dash-db",
                        "url": "/d/host/cpu",
                    }
                    for i in range(
                        start, min(state.count, start + int(args.get("limit", ["50"])[0]))
                    )
                ]
            elif parsed.path.startswith("/api/dashboards/uid/"):
                body = state.dashboard
            elif parsed.path in ("/api/datasources", "/api/frontend/settings"):
                items = [
                    {
                        "id": i + 1,
                        "uid": f"prom{i}",
                        "name": "Prometheus",
                        "type": "prometheus",
                        "isDefault": i == 0,
                        "url": "http://private-upstream",
                        "secureJsonData": {"password": "secret-canary"},
                    }
                    for i in range(state.count)
                ]
                body = (
                    items
                    if parsed.path == "/api/datasources"
                    else {"datasources": {d["uid"]: d for d in items}}
                )
            elif parsed.path == "/api/annotations":
                body = [
                    {
                        "id": i + 1,
                        "dashboardUID": "host",
                        "panelId": 1,
                        "time": 1720000000000,
                        "timeEnd": 1720000060000,
                        "text": "Synthetic deploy",
                        "tags": ["deploy"],
                        "login": "private-login",
                        "email": "private-email",
                    }
                    for i in range(min(state.count, int(args.get("limit", ["100"])[0])))
                ]
            else:
                status, body = 404, {"message": "Not found"}
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args: object) -> None:
            return

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=backend.serve_forever, daemon=True)
    worker.start()
    try:
        yield backend.server_port, state
    finally:
        backend.shutdown()
        backend.server_close()
        worker.join(timeout=5)


@contextmanager
def official_grafana(
    binary: Path,
    backend_port: int,
    *,
    token: str = "synthetic-mcp-token",  # noqa: S107 - synthetic only
) -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(  # noqa: S603 - only an opt-in, hash-checked protocol binary
        [
            str(binary),
            "--transport=streamable-http",
            f"--address=127.0.0.1:{port}",
            "--enabled-tools=search,dashboard,datasource,annotations",
            "--disable-write",
            "--disable-query",
            "--usage-stats=disabled",
            "--grafana-timeout=1s",
            "--log-level=error",
            f"--server-auth-token={token}",
        ],
        env={
            "PATH": os.environ["PATH"],
            "GRAFANA_URL": f"http://127.0.0.1:{backend_port}",
            "GRAFANA_SERVICE_ACCOUNT_TOKEN": "synthetic-upstream-token",
            "DO_NOT_TRACK": "1",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Grafana MCP 提前退出")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("Grafana MCP 未开始监听")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
