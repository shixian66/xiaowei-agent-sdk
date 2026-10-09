"""R1b throwaway probe for the public SDK MCP lifecycle.

Run: PYTHONPATH=src python -m pytest tests/spike/test_mcp_reconnect.py -s -q
The optional official v0.18.0 test uses a loopback synthetic Prometheus API.
No company service or real model is used. This is not product code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
import pytest
import uvicorn
from agents import Agent, FunctionTool, Runner
from agents.mcp import MCPServerStreamableHttp
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict
from tests.sdk_core.mcp_fixture import FAKE_TOKEN, LOOPBACK, ASGIApp, Recorder, mcp_app, recording

from xiaowei.config import MCPServerConfig, configure_runtime
from xiaowei.mcp import MCPTransportError, _Binding, _client_factory, _verified
from xiaowei.models import ToolContract

pytestmark = pytest.mark.allow_hosts([LOOPBACK])


@pytest.fixture(autouse=True)
def _disable_sdk_tracing() -> None:
    configure_runtime()


class RestartableMCP:
    """Rebind one loopback port with a fresh MCP server and session store."""

    def __init__(self) -> None:
        self.port: int | None = None
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.recorders: list[Recorder] = []

    @property
    def url(self) -> str:
        assert self.port is not None
        return f"http://{LOOPBACK}:{self.port}/mcp"

    def start(self, build: Callable[[Recorder], ASGIApp] | None = None) -> Recorder:
        assert self.thread is None
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LOOPBACK, self.port or 0))
        self.port = sock.getsockname()[1]
        recorder = Recorder(url=self.url)
        server = uvicorn.Server(
            uvicorn.Config(
                (build or mcp_app())(recorder),
                log_level="error",
                lifespan="on",
                timeout_graceful_shutdown=1,
            )
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        for _ in range(500):
            if server.started:
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("loopback MCP failed to start")
        self.server, self.thread = server, thread
        self.recorders.append(recorder)
        return recorder

    def stop(self) -> None:
        if self.thread is None:
            return
        assert self.server is not None
        self.server.should_exit = True
        self.thread.join(timeout=5)
        assert not self.thread.is_alive(), "loopback MCP did not stop"
        self.server = None
        self.thread = None


@contextmanager
def official_server(binary: Path, backend_port: int, *, port: int | None = None) -> Iterator[str]:
    """Start the verified release binary against a disposable local Prometheus API."""
    if port is None:
        with socket.socket() as probe:
            probe.bind((LOOPBACK, 0))
            port = probe.getsockname()[1]
    process = subprocess.Popen(  # noqa: S603 - explicit opt-in release binary
        [
            str(binary),
            "--mcp.transport=http",
            f"--prometheus.url=http://{LOOPBACK}:{backend_port}",
            f"--web.listen-address={LOOPBACK}:{port}",
            "--mcp.tools=core",
            "--log.level=error",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Prometheus MCP exited during startup")
            try:
                with socket.create_connection((LOOPBACK, port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Prometheus MCP did not listen")
        yield f"http://{LOOPBACK}:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def client(url: str, *, timeout: float = 0.5) -> MCPServerStreamableHttp:
    return MCPServerStreamableHttp(
        params={"url": url, "timeout": timeout, "sse_read_timeout": timeout * 2},
        name="r1b-fixture",
        cache_tools_list=True,
        client_session_timeout_seconds=timeout,
        max_retry_attempts=0,
    )


async def observed(awaitable: Any) -> dict[str, Any]:
    try:
        result = await awaitable
    except BaseException as exc:
        return {
            "kind": "exception",
            "type": type(exc).__name__,
            "code": getattr(exc, "code", None),
            "message": str(exc)[:180],
        }
    return {
        "kind": "result",
        "type": type(result).__name__,
        "is_error": getattr(result, "is_error", None),
        "tool_names": [tool.name for tool in result] if isinstance(result, list) else None,
    }


@pytest.mark.asyncio
async def test_restart_failure_and_public_reconnect() -> None:
    node = RestartableMCP()
    first = node.start()
    old = client(node.url)
    try:
        await old.connect()
        initial = await observed(old.list_tools())
        remote_error = await observed(old.call_tool("failing", {"key": "x"}))
        node.stop()
        requests_at_stop = len(first.requests)
        cached_while_down = await observed(old.list_tools())
        down_call = await observed(old.call_tool("lookup", {"key": "x"}))
        old.invalidate_tools_cache()
        down_list = await observed(old.list_tools())
        second = node.start()
        stale_call = await observed(old.call_tool("lookup", {"key": "x"}))
        stale_list = await observed(old.list_tools())
        requests_before_reconnect = len(second.requests)
        await old.cleanup()
        await old.connect()
        old.invalidate_tools_cache()
        same_object_list = await observed(old.list_tools())
        same_object_call = await observed(old.call_tool("lookup", {"key": "x"}))
        await old.cleanup()
        replacement = client(node.url)
        await replacement.connect()
        try:
            new_object_list = await observed(replacement.list_tools())
            new_object_call = await observed(replacement.call_tool("lookup", {"key": "x"}))
        finally:
            await replacement.cleanup()
        print(
            "restart/public-reconnect:",
            {
                "initial": initial,
                "remote_error": remote_error,
                "cached_while_down": cached_while_down,
                "down_call": down_call,
                "down_list": down_list,
                "stale_call": stale_call,
                "stale_list": stale_list,
                "same_object_list": same_object_list,
                "same_object_call": same_object_call,
                "new_object_list": new_object_list,
                "new_object_call": new_object_call,
                "first_calls": first.tool_calls,
                "second_calls": second.tool_calls,
                "requests_after_stop": len(first.requests) - requests_at_stop,
                "requests_to_restarted_before_reconnect": requests_before_reconnect,
            },
        )
        assert initial["kind"] == "result"
        assert remote_error == {
            "kind": "result",
            "type": "CallToolResult",
            "is_error": True,
            "tool_names": None,
        }
        assert cached_while_down["kind"] == "result"
        assert down_call["kind"] == "exception"
        assert down_list["kind"] == "exception"
        assert len(first.requests) == requests_at_stop
        assert requests_before_reconnect == 0
        assert same_object_call["is_error"] is False
        assert new_object_call["is_error"] is False
        assert len(first.tool_calls) == 1
        assert len(second.tool_calls) == 2
    finally:
        await old.cleanup()
        node.stop()


@pytest.mark.asyncio
async def test_failed_initial_connect_can_reuse_public_object() -> None:
    node = RestartableMCP()
    node.start()
    url = node.url
    node.stop()
    source = client(url)
    try:
        failed = await observed(source.connect())
        assert failed["kind"] == "exception"
        node.start()
        await source.connect()
        source.invalidate_tools_cache()
        names = {tool.name for tool in await source.list_tools()}
        print("startup unavailable then recovered:", failed, "lookup visible", "lookup" in names)
        assert "lookup" in names
    finally:
        await source.cleanup()
        node.stop()


@pytest.mark.asyncio
async def test_cross_task_cleanup_vs_owner_task_cleanup(caplog: pytest.LogCaptureFixture) -> None:
    node = RestartableMCP()
    node.start()
    wrong_task = client(node.url)
    ready = asyncio.Event()
    finish = asyncio.Event()

    async def owner() -> None:
        await wrong_task.connect()
        ready.set()
        await finish.wait()
        await wrong_task.cleanup()

    owner_task = asyncio.create_task(owner())
    try:
        await ready.wait()
        with caplog.at_level(logging.DEBUG):
            cross = await observed(wrong_task.cleanup())
        finish.set()
        owner_result = await observed(owner_task)
        await asyncio.sleep(0.05)
        connections_after_cross = len(node.server.server_state.connections) if node.server else -1
        print("cross-task cleanup:", cross, owner_result, "session", wrong_task.session)
        print("cross-task connections after:", connections_after_cross)
        cancel_scope_logs = [
            record.getMessage()
            for record in caplog.records
            if "cancel scope" in record.getMessage().lower()
        ]
        print("cross-task cancel-scope logs:", cancel_scope_logs)
        assert cross["kind"] in {"result", "exception"}
        assert cancel_scope_logs == []
        same_task = client(node.url)

        async def owned() -> None:
            await same_task.connect()
            await same_task.cleanup()

        same_task_result = await observed(asyncio.create_task(owned()))
        print("same-task cleanup:", same_task_result, "session", same_task.session)
        assert same_task_result["kind"] == "result"
        assert connections_after_cross == 0
    finally:
        finish.set()
        if not owner_task.done():
            await owner_task
        await wrong_task.cleanup()
        node.stop()


@pytest.mark.asyncio
async def test_async_exit_stack_can_close_from_another_task() -> None:
    node = RestartableMCP()
    node.start()
    source = client(node.url)
    stack = AsyncExitStack()
    ready = asyncio.Event()
    release = asyncio.Event()

    async def enter_owner() -> None:
        await stack.enter_async_context(source)
        ready.set()
        await release.wait()

    task = asyncio.create_task(enter_owner())
    try:
        await ready.wait()
        closed = await observed(asyncio.wait_for(stack.aclose(), timeout=2))
        release.set()
        await task
        await asyncio.sleep(0.05)
        connections = len(node.server.server_state.connections) if node.server else -1
        print(
            "AsyncExitStack cross-task:",
            closed,
            "session",
            source.session,
            "connections",
            connections,
        )
        assert closed["kind"] == "result"
        assert source.session is None
        assert connections == 0
    finally:
        release.set()
        await task
        await stack.aclose()
        node.stop()


@pytest.mark.asyncio
async def test_owner_cancellation_closes_connection() -> None:
    node = RestartableMCP()
    node.start()
    owned = client(node.url)
    ready = asyncio.Event()

    async def owner() -> None:
        await owned.connect()
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            await owned.cleanup()

    task = asyncio.create_task(owner())
    try:
        await ready.wait()
        task.cancel()
        result = await observed(task)
        await asyncio.sleep(0.05)
        connections = len(node.server.server_state.connections) if node.server else -1
        print("owner cancellation:", result, "session", owned.session, "connections", connections)
        assert result["type"] == "CancelledError"
        assert owned.session is None
        assert connections == 0
    finally:
        await owned.cleanup()
        node.stop()


@pytest.mark.asyncio
async def test_inflight_runner_call_keeps_old_connection_and_tool_list() -> None:
    node = RestartableMCP()
    first = node.start(mcp_app(slow_seconds=0.6))
    old = client(node.url, timeout=2)
    replacement = client(node.url, timeout=2)
    await old.connect()
    try:

        async def invoke(_context: Any, raw: str) -> str:
            result = await old.call_tool("slow", json.loads(raw))
            assert result.is_error is False
            return "old-generation-result"

        tool = FunctionTool(
            name="probe",
            description="Call the old synthetic connection",
            params_json_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
            on_invoke_tool=invoke,
        )
        model = ScriptedModel(
            [
                ModelStep(output=[function_call("probe", {"key": "A"}, call_id="old-A")]),
                ModelStep(output=[assistant_message("done")]),
            ]
        )
        agent = Agent(name="old-round", model=model, tools=[tool])
        round_a = asyncio.create_task(Runner.run(agent, "probe A"))
        for _ in range(100):
            if first.tool_calls:
                break
            await asyncio.sleep(0.01)
        assert first.tool_calls == [("slow", {"key": "A"})]
        await replacement.connect()
        replacement_list = await replacement.list_tools()
        result = await round_a
        print(
            "overlapping rounds:",
            {
                "A": result.final_output,
                "A_model_calls": len(model.calls),
                "A_tool_list_unchanged": agent.tools == [tool],
                "B_has_slow": "slow" in {x.name for x in replacement_list},
                "server_calls": first.tool_calls,
            },
        )
        assert result.final_output == "done"
        assert len(model.calls) == 2
        assert agent.tools == [tool]
        assert "slow" in {x.name for x in replacement_list}
        assert first.tool_calls == [("slow", {"key": "A"})]
    finally:
        await replacement.cleanup()
        await old.cleanup()
        node.stop()


class KeyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str


class KeyResult(BaseModel):
    key: str


def drift_app(recorder: Recorder) -> ASGIApp:
    app = MCPServer("r1b-drift")

    @app.tool()
    def lookup(key: int) -> dict[str, str]:
        recorder.tool_calls.append(("lookup", {"key": key}))
        return {"key": str(key)}

    return recording(app.streamable_http_app(host=LOOPBACK), recorder, token=None)


@pytest.mark.asyncio
async def test_reconnect_rechecks_schema_and_fixed_transport() -> None:
    node = RestartableMCP()
    first = node.start()
    config = MCPServerConfig(
        server_id="fixture",
        url=node.url,
        auth_ref=None,
        timeout_seconds=1,
        max_response_bytes=64_000,
        allowed_tools={"lookup": "fixture.key"},
    )
    contract = ToolContract(
        tool_id="fixture/lookup",
        target_id="fixture-target",
        input_schema=KeyArgs.model_json_schema(),
        policy_id="fixture.key",
        description="synthetic lookup",
    )
    planned = {"fixture__lookup": _Binding(config, "lookup", contract, KeyResult)}
    source = client(node.url)
    try:
        await source.connect()
        before = _verified(await source.list_tools(), planned, config)
        await source.cleanup()
        node.stop()
        second = node.start(drift_app)
        await source.connect()
        source.invalidate_tools_cache()
        after = _verified(await source.list_tools(), planned, config)
        print("schema drift:", before, after, "new requests", len(second.requests))
        assert before == ["fixture__lookup"]
        assert after == []
        assert first.tool_calls == second.tool_calls == []
    finally:
        await source.cleanup()
        node.stop()

    secure = RestartableMCP()
    good = secure.start(mcp_app(token=FAKE_TOKEN))
    secure_config = config.model_copy(update={"url": secure.url})
    guarded = MCPServerStreamableHttp(
        params={
            "url": secure.url,
            "headers": {"authorization": "Bearer attacker"},
            "timeout": 1,
            "sse_read_timeout": 1,
            "httpx_client_factory": _client_factory(secure_config, f"Bearer {FAKE_TOKEN}"),
        },
        name="guarded",
        max_retry_attempts=0,
    )
    other = RestartableMCP()
    other_recorder = other.start()
    try:
        await guarded.connect()
        assert (await guarded.call_tool("lookup", {"key": "x"})).is_error is False
        assert {authorization for _, _, authorization in good.requests} == {f"Bearer {FAKE_TOKEN}"}
        await guarded.cleanup()
        guarded.params["url"] = other.url
        endpoint_change = await observed(guarded.connect())
        async with _client_factory(secure_config, f"Bearer {FAKE_TOKEN}")() as direct:
            with pytest.raises(MCPTransportError):
                await direct.get(other.url)
        print("fixed endpoint:", endpoint_change, "other requests", other_recorder.requests)
        assert endpoint_change["kind"] == "exception"
        assert other_recorder.requests == []
    finally:
        await guarded.cleanup()
        secure.stop()
        other.stop()


@pytest.mark.asyncio
async def test_cleanup_from_other_task_during_call_has_one_outcome() -> None:
    node = RestartableMCP()
    recorder = node.start(mcp_app(slow_seconds=0.8))
    source = client(node.url, timeout=2)
    await source.connect()
    try:
        call = asyncio.create_task(source.call_tool("slow", {"key": "A"}))
        for _ in range(100):
            if recorder.tool_calls:
                break
            await asyncio.sleep(0.01)
        assert recorder.tool_calls == [("slow", {"key": "A"})]
        cleanup = await observed(asyncio.wait_for(source.cleanup(), timeout=2))
        outcome = await observed(asyncio.wait_for(call, timeout=2))
        await asyncio.sleep(0.05)
        connections = len(node.server.server_state.connections) if node.server else -1
        print("cross-task cleanup during call:", cleanup, outcome, "connections", connections)
        assert cleanup["kind"] == "result"
        assert outcome["kind"] in {"result", "exception"}
        assert recorder.tool_calls == [("slow", {"key": "A"})]
        assert source.session is None
        assert connections == 0
    finally:
        await source.cleanup()
        node.stop()


class ReconnectGate:
    """Test-only single-flight, cooldown and deadline around a public connect call."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.lock = asyncio.Lock()
        self.ready = False
        self.cooldown_until = 0.0
        self.attempts = 0
        self.active: MCPServerStreamableHttp | None = None

    async def ensure(self) -> bool:
        if self.ready:
            return True
        async with self.lock:
            if self.ready:
                return True
            if time.monotonic() < self.cooldown_until:
                return False
            self.attempts += 1
            candidate = client(self.url, timeout=1)
            try:
                await asyncio.wait_for(candidate.connect(), timeout=0.25)
                candidate.invalidate_tools_cache()
                await asyncio.wait_for(candidate.list_tools(), timeout=0.25)
            except Exception:
                await candidate.cleanup()
                self.cooldown_until = time.monotonic() + 0.3
                return False
            self.active = candidate
            self.ready = True
            return True


def hanging_app(recorder: Recorder) -> ASGIApp:
    normal = mcp_app()(recorder)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            await asyncio.sleep(0.55)
            return
        await normal(scope, receive, send)

    return app


@pytest.mark.asyncio
async def test_single_flight_cooldown_deadline_and_other_source() -> None:
    bad = RestartableMCP()
    bad.start(hanging_app)
    good = RestartableMCP()
    good.start()
    other = client(good.url)
    gate = ReconnectGate(bad.url)
    try:
        await other.connect()
        pending = [asyncio.create_task(gate.ensure()) for _ in range(12)]
        await asyncio.sleep(0.05)
        begin = time.monotonic()
        other_result = await other.call_tool("lookup", {"key": "other"})
        other_elapsed = time.monotonic() - begin
        first = await asyncio.gather(*pending)
        during_cooldown = await gate.ensure()
        print(
            "single-flight/cooldown:",
            {"first": first, "attempts": gate.attempts, "other_elapsed": round(other_elapsed, 3)},
        )
        assert first == [False] * 12
        assert gate.attempts == 1
        assert during_cooldown is False
        assert other_result.is_error is False
        assert other_elapsed < 0.25
        await asyncio.sleep(0.4)
        assert bad.server is not None
        assert len(bad.server.server_state.connections) == 0
        bad.stop()
        bad.start()
        await asyncio.sleep(0.31)
        second = await asyncio.gather(*(gate.ensure() for _ in range(12)))
        assert second == [True] * 12
        assert gate.attempts == 2
        assert gate.active is not None
        assert (await gate.active.call_tool("lookup", {"key": "recovered"})).is_error is False
        print("recovered after cooldown:", {"attempts": gate.attempts, "ready": gate.ready})
    finally:
        if gate.active is not None:
            await gate.active.cleanup()
        await other.cleanup()
        bad.stop()
        good.stop()


@pytest.mark.asyncio
async def test_pre_round_probe_vs_failure_marked_next_round() -> None:
    node = RestartableMCP()
    node.start()
    proactive = client(node.url)
    lazy = client(node.url)
    await proactive.connect()
    await lazy.connect()
    await proactive.list_tools()
    await lazy.list_tools()
    try:
        node.stop()
        await asyncio.sleep(0.05)  # let the existing SDK streams observe process exit
        lazy_first_call = await observed(lazy.call_tool("lookup", {"key": "lazy-first"}))
        assert lazy_first_call["kind"] == "exception"
        recovered = node.start()
        assert recovered.requests == []  # no background probe

        # Pre-round probe: invalidate the cache and perform network I/O before exposing tools.
        proactive.invalidate_tools_cache()
        probe = await observed(proactive.list_tools())
        if probe["kind"] == "exception":
            await proactive.cleanup()
            await proactive.connect()
            proactive.invalidate_tools_cache()
        assert "lookup" in {tool.name for tool in await proactive.list_tools()}
        proactive_call = await observed(proactive.call_tool("lookup", {"key": "proactive"}))

        # Failure-marked recovery: the first call fails; only a later round reconnects.
        await lazy.cleanup()
        await lazy.connect()
        lazy.invalidate_tools_cache()
        assert "lookup" in {tool.name for tool in await lazy.list_tools()}
        lazy_later_call = await observed(lazy.call_tool("lookup", {"key": "lazy-next"}))
        print(
            "trigger timing:",
            {
                "pre_round_probe": probe,
                "pre_round_query": proactive_call,
                "failure_marked_first_query": lazy_first_call,
                "failure_marked_next_query": lazy_later_call,
                "upstream_calls": recovered.tool_calls,
            },
        )
        assert probe["kind"] in {"result", "exception"}
        assert proactive_call["is_error"] is False
        assert lazy_first_call["kind"] == "exception"
        assert lazy_later_call["is_error"] is False
        assert recovered.tool_calls == [
            ("lookup", {"key": "proactive"}),
            ("lookup", {"key": "lazy-next"}),
        ]
    finally:
        await proactive.cleanup()
        await lazy.cleanup()
        node.stop()


async def _process_child(url: str) -> None:
    configure_runtime()
    source = client(url, timeout=1)
    stopped = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopped.set)
    await source.connect()
    print("READY", flush=True)
    try:
        await stopped.wait()
    finally:
        await source.cleanup()
        print("CLEAN", flush=True)


@pytest.mark.asyncio
async def test_graceful_and_abrupt_process_exit() -> None:
    node = RestartableMCP()
    node.start()

    def launch() -> subprocess.Popen[str]:
        return subprocess.Popen(  # noqa: S603 - fixed local test module, no shell
            [sys.executable, "-m", "tests.spike.test_mcp_reconnect", "--child", node.url],
            cwd=os.getcwd(),
            env={**os.environ, "PYTHONPATH": "src:."},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    try:
        graceful = launch()
        assert graceful.stdout is not None
        assert graceful.stdout.readline().strip() == "READY"
        graceful.send_signal(signal.SIGTERM)
        out, err = graceful.communicate(timeout=5)
        print("graceful exit:", graceful.returncode, out.strip(), err.strip()[:100])
        assert graceful.returncode == 0
        assert "CLEAN" in out

        abrupt = launch()
        assert abrupt.stdout is not None
        assert abrupt.stdout.readline().strip() == "READY"
        abrupt.kill()
        out, err = abrupt.communicate(timeout=5)
        print("abrupt exit:", abrupt.returncode, out.strip(), err.strip()[:100])
        assert abrupt.returncode != 0

        after = client(node.url)
        await after.connect()
        try:
            assert (await after.call_tool("lookup", {"key": "after"})).is_error is False
        finally:
            await after.cleanup()
    finally:
        node.stop()


@pytest.mark.asyncio
async def test_timeout_is_not_a_remote_is_error_or_an_automatic_retry() -> None:
    node = RestartableMCP()
    recorder = node.start(mcp_app(slow_seconds=0.6))
    source = client(node.url, timeout=0.2)
    try:
        await source.connect()
        timeout = await observed(source.call_tool("slow", {"key": "one"}))
        await asyncio.sleep(0.5)
        later = await observed(source.call_tool("lookup", {"key": "two"}))
        print("timeout:", timeout, "later:", later, "calls:", recorder.tool_calls)
        assert timeout["kind"] == "exception"
        assert timeout["type"] == "MCPError"
        assert recorder.tool_calls.count(("slow", {"key": "one"})) == 1
        assert later["is_error"] is False
    finally:
        await source.cleanup()
        node.stop()


@pytest.mark.asyncio
async def test_synthetic_server_does_not_issue_session_id() -> None:
    seen_session_ids: list[str] = []
    request_headers: list[list[str]] = []
    response_headers: list[list[str]] = []

    def build(recorder: Recorder) -> ASGIApp:
        inner = mcp_app()(recorder)

        async def app(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
                request_headers.append(sorted(headers))
                if session_id := headers.get("mcp-session-id"):
                    seen_session_ids.append(session_id)

                async def capture(message: Any) -> None:
                    if message["type"] == "http.response.start":
                        response_headers.append([k.decode().lower() for k, _ in message["headers"]])
                    await send(message)

                await inner(scope, receive, capture)
                return
            await inner(scope, receive, send)

        return app

    node = RestartableMCP()
    node.start(build)
    source = client(node.url)
    try:
        await source.connect()
        await source.list_tools()
        print("session headers:", request_headers, response_headers)
        assert request_headers
        assert response_headers
        assert seen_session_ids == []
        assert all("mcp-session-id" not in headers for headers in response_headers)
    finally:
        await source.cleanup()
        node.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("XW_TEST_PROMETHEUS_MCP_BIN"),
    reason="opt-in verified Prometheus MCP v0.18.0 binary",
)
async def test_official_server_session_headers() -> None:
    queries: list[str] = []

    class Backend(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            queries.append(self.path)
            body = b'{"status":"success","data":{"resultType":"vector","result":[]}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    backend = ThreadingHTTPServer((LOOPBACK, 0), Backend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    seen: list[tuple[str, int, str | None]] = []

    def factory(headers: Any = None, timeout: Any = None, auth: Any = None) -> httpx2.AsyncClient:
        async def capture(response: httpx2.Response) -> None:
            seen.append(
                (
                    response.request.headers.get("mcp-method", "?"),
                    response.status_code,
                    response.headers.get("mcp-session-id"),
                )
            )

        return httpx2.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            event_hooks={"response": [capture]},
            trust_env=False,
            follow_redirects=False,
        )

    try:
        binary = Path(os.environ["XW_TEST_PROMETHEUS_MCP_BIN"])
        version = subprocess.run(  # noqa: S603 - explicit local release binary
            [str(binary), "--version"], capture_output=True, text=True, check=True
        )
        assert "version: 0.18.0" in version.stdout
        source: MCPServerStreamableHttp | None = None
        with official_server(binary, backend.server_port) as url:
            source = MCPServerStreamableHttp(
                params={"url": url, "timeout": 1, "httpx_client_factory": factory},
                cache_tools_list=True,
                client_session_timeout_seconds=1,
                max_retry_attempts=0,
            )
            await source.connect()
            names = {tool.name for tool in await source.list_tools()}
            assert "query" in names
            print("official v0.18.0 session headers:", seen)
            session_ids = [session_id for _, _, session_id in seen if session_id]
            assert len(session_ids) == 1
            async with httpx2.AsyncClient(trust_env=False) as http:
                termination = await http.delete(
                    url,
                    headers={"mcp-session-id": session_ids[0]},
                    timeout=1,
                )
            after_termination = await observed(source.call_tool("query", {"query": "up"}))
            source.invalidate_tools_cache()
            list_after_termination = await observed(source.list_tools())
            print(
                "official session invalidation:",
                {
                    "delete_status": termination.status_code,
                    "call": after_termination,
                    "list": list_after_termination,
                    "upstream_queries_before_reconnect": len(queries),
                },
            )
            assert termination.status_code == 204
            assert after_termination["kind"] == "exception"
            assert list_after_termination["kind"] == "exception"
            assert queries == []
            await source.cleanup()
            await source.connect()
            source.invalidate_tools_cache()
            assert "query" in {tool.name for tool in await source.list_tools()}
            recovered = await observed(source.call_tool("query", {"query": "up"}))
            print("official reconnect:", recovered, "upstream query count:", len(queries))
            assert recovered["is_error"] is False
            assert len(queries) == 1

        try:
            # Keep the old SDK object alive while restarting the external process on its port.
            down = await observed(source.call_tool("query", {"query": "down"}))
            source.invalidate_tools_cache()
            down_list = await observed(source.list_tools())
            port = urlsplit(url).port
            assert port is not None
            with official_server(binary, backend.server_port, port=port):
                stale = await observed(source.call_tool("query", {"query": "stale"}))
                source.invalidate_tools_cache()
                stale_list = await observed(source.list_tools())
                print(
                    "official process restart:",
                    {
                        "down": down,
                        "down_list": down_list,
                        "stale": stale,
                        "stale_list": stale_list,
                        "upstream_queries": len(queries),
                    },
                )
                assert down["kind"] == "exception"
                assert down_list["kind"] == "exception"
                assert stale["kind"] == "exception"
                assert stale_list["kind"] == "exception"
                assert len(queries) == 1
                await source.cleanup()
                await source.connect()
                source.invalidate_tools_cache()
                assert "query" in {tool.name for tool in await source.list_tools()}
                assert (await source.call_tool("query", {"query": "after"})).is_error is False
                assert len(queries) == 2
        finally:
            await source.cleanup()
    finally:
        backend.shutdown()
        thread.join(timeout=5)
        backend.server_close()


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--child":
    asyncio.run(_process_child(sys.argv[2]))
