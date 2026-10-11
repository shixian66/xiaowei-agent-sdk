"""Loopback-only recording relay to the isolated official Grafana D7 container."""

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2


@dataclass
class DashboardTraffic:
    requests: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)
    behavior: str = ""
    written: bool = False

    @property
    def writes(self) -> list[dict[str, Any]]:
        return [
            body
            for method, path, body in self.requests
            if method == "POST" and path == "/api/dashboards/db" and body is not None
        ]


@contextmanager
def dashboard_relay() -> Iterator[tuple[int, DashboardTraffic]]:
    state = DashboardTraffic()
    with httpx2.Client(base_url="http://127.0.0.1:3307", trust_env=False, timeout=5) as upstream:

        class Handler(BaseHTTPRequestHandler):
            def forward(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(body) if body else None
                state.requests.append((self.command, self.path, data))
                headers = {"Authorization": self.headers.get("Authorization", "")}
                writing = self.command == "POST" and self.path == "/api/dashboards/db"
                if writing and state.behavior == "race":
                    uid = data["dashboard"]["uid"]
                    current = upstream.get(f"/api/dashboards/uid/{uid}", headers=headers).json()[
                        "dashboard"
                    ]
                    changed = upstream.post(
                        "/api/dashboards/db",
                        headers=headers,
                        json={
                            "dashboard": {**current, "title": "External racing edit"},
                            "folderUid": "",
                            "overwrite": False,
                        },
                    )
                    assert changed.status_code == 200
                if writing and state.behavior == "500":
                    status, encoded = 500, b'{"message":"synthetic server failure"}'
                else:
                    response = upstream.request(
                        self.command,
                        self.path,
                        content=body,
                        headers={
                            **headers,
                            "Content-Type": self.headers.get("Content-Type", "application/json"),
                        },
                    )
                    status, encoded = response.status_code, response.content
                    if writing and status == 200:
                        state.written = True
                        if state.behavior == "save_mismatch":
                            value = response.json()
                            value["uid"] = "wrong-uid"
                            encoded = json.dumps(value).encode()
                        elif state.behavior == "timeout":
                            time.sleep(
                                1.4
                            )  # locked MCP Grafana timeout is 1s; write has already happened
                    elif (
                        state.written
                        and state.behavior in {"read_mismatch", "read_type_mismatch"}
                        and "/versions/" in self.path
                    ):
                        value = response.json()
                        if state.behavior == "read_type_mismatch":
                            value["data"]["panels"][0]["id"] = True
                        else:
                            value["data"]["title"] = "wrong-readback"
                        encoded = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # caller's intentional timeout; recorded request remains visible

            do_GET = forward  # noqa: N815 - BaseHTTPRequestHandler hook
            do_POST = forward  # noqa: N815 - BaseHTTPRequestHandler hook
            do_PUT = forward  # noqa: N815 - BaseHTTPRequestHandler hook
            do_DELETE = forward  # noqa: N815 - BaseHTTPRequestHandler hook
            do_PATCH = forward  # noqa: N815 - BaseHTTPRequestHandler hook

            def log_message(self, *_: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server.server_port, state
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
