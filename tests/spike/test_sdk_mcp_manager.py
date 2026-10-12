"""MCPServerManager 的公开生命周期实验，不实现正式重连。"""

import asyncio
import time

import pytest
from agents import UserError
from agents.mcp import MCPServer, MCPServerManager, MCPServerStreamableHttp
from mcp.server.mcpserver import MCPServer as FixtureMCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent
from tests.sdk_core.mcp_fixture import Recorder, mcp_app, recording
from tests.sdk_core.test_monitoring_r1b import Restartable

pytestmark = pytest.mark.loopback


class ProbeServer(MCPServer):
    """SDK 生命周期替身；只观测公开方法、任务归属和资源计数。"""

    def __init__(self, name, *, fail=False, hang=False):
        super().__init__()
        self.label, self.fail, self.hang = name, fail, hang
        self.connects = self.cleanups = 0
        self.live = False
        self.tasks = []
        self.entered = asyncio.Event()

    @property
    def name(self):
        return self.label

    async def connect(self):
        self.connects += 1
        self.live = True
        self.tasks.append(("connect", asyncio.current_task()))
        self.entered.set()
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise UserError("synthetic connect failure")

    async def cleanup(self):
        self.cleanups += 1
        self.tasks.append(("cleanup", asyncio.current_task()))
        self.live = False

    async def list_tools(self, run_context=None, agent=None):
        return []

    async def call_tool(self, tool_name, arguments=None, meta=None):
        return CallToolResult(content=[TextContent(type="text", text=self.label)])

    async def list_prompts(self):
        return []

    async def get_prompt(self, name, arguments=None):
        raise NotImplementedError


class CountedHTTP(MCPServerStreamableHttp):
    def __init__(self, url):
        super().__init__(
            name="loopback",
            params={"url": url, "timeout": 1, "sse_read_timeout": 1},
            cache_tools_list=True,
            client_session_timeout_seconds=1,
            max_retry_attempts=0,
        )
        self.connects = 0

    async def connect(self):
        self.connects += 1
        await super().connect()


def manager(server, **options):
    return MCPServerManager(
        [server],
        connect_in_parallel=True,
        suppress_cancelled_error=False,
        connect_timeout_seconds=0.2,
        cleanup_timeout_seconds=1,
        **options,
    )


async def test_failed_start_subset_and_cross_task_cleanup_have_affinity():
    healthy, failed = ProbeServer("healthy"), ProbeServer("failed", fail=True)
    lifecycle = MCPServerManager(
        [healthy, failed, healthy],
        connect_in_parallel=True,
        suppress_cancelled_error=False,
        connect_timeout_seconds=0.2,
    )
    await lifecycle.__aenter__()
    assert lifecycle.all_servers == [healthy, failed]
    assert lifecycle.active_servers == [healthy]
    assert lifecycle.failed_servers == [failed]
    assert isinstance(lifecycle.errors[failed], UserError)
    assert healthy.connects == failed.connects == 1
    # 从 connect 的调用者之外另一个 Task 关闭；SDK 的 worker 保持每个 Server 的任务归属。
    await asyncio.create_task(lifecycle.cleanup_all())
    await asyncio.sleep(0)
    for server in (healthy, failed):
        assert not server.live and server.cleanups == 1
        assert server.tasks[0][1] is server.tasks[1][1]
        assert server.tasks[0][1].done()
    print("manager_affinity", "two_closed", "same_worker")


async def test_connect_timeout_and_cancelled_candidate_are_cleaned():
    stalled = ProbeServer("stalled", hang=True)
    lifecycle = manager(stalled)
    started = time.monotonic()
    try:
        await lifecycle.connect_all()
        assert 0.15 <= time.monotonic() - started < 0.8
        assert lifecycle.active_servers == [] and lifecycle.failed_servers == [stalled]
        assert isinstance(lifecycle.errors[stalled], TimeoutError)
    finally:
        await lifecycle.cleanup_all()
    assert not stalled.live

    cancelled = ProbeServer("cancelled", hang=True)
    candidate = manager(cancelled)
    task = asyncio.create_task(candidate.__aenter__())
    try:
        await asyncio.wait_for(cancelled.entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # 只收集本用例主动取消的任务；其他异常继续抛出。
        finally:
            await candidate.cleanup_all()
    await asyncio.sleep(0)
    assert cancelled.cleanups >= 1 and not cancelled.live
    assert all(worker.done() for _, worker in cancelled.tasks)
    print("manager_cancel", "candidate_closed")


async def test_per_source_managers_do_not_delay_a_healthy_source():
    stalled, healthy = ProbeServer("stalled", hang=True), ProbeServer("healthy")
    first, second = manager(stalled), manager(healthy)
    pending = asyncio.create_task(first.connect_all())
    await stalled.entered.wait()
    started = time.monotonic()
    await second.connect_all()
    assert time.monotonic() - started < 0.1
    assert second.active_servers == [healthy]
    await pending
    await first.cleanup_all()
    await second.cleanup_all()
    assert not stalled.live and not healthy.live


async def test_manager_serializes_concurrent_connects():
    server = ProbeServer("healthy")
    lifecycle = manager(server)
    try:
        results = await asyncio.gather(*(lifecycle.connect_all() for _ in range(12)))
        assert all(result == [server] for result in results)
        assert server.connects == 1
    finally:
        await lifecycle.cleanup_all()
    assert not server.live


def drifted(recorder: Recorder):
    app = FixtureMCPServer("spike-drift")

    @app.tool()
    def lookup(key: int) -> dict:
        recorder.tool_calls.append(("lookup", {"key": key}))
        return {"generation": 2, "key": key}

    return recording(app.streamable_http_app(host="127.0.0.1"), recorder, token=None)


async def test_manager_does_not_track_runtime_failure_or_freeze_server_generation():
    remote = Restartable()
    live = remote.start(mcp_app())
    server = CountedHTTP(live.recorder.url)
    lifecycle = manager(server)
    try:
        await lifecycle.__aenter__()
        assert lifecycle.active_servers == [server]
        original = await server.list_tools()
        assert (
            next(t for t in original if t.name == "lookup").input_schema["properties"]["key"][
                "type"
            ]
            == "string"
        )
        result = await server.call_tool("lookup", {"key": "first"})
        assert not result.is_error and len(live.recorder.tool_calls) == 1
        remote.stop()
        with pytest.raises(MCPError) as caught:
            await server.call_tool("lookup", {"key": "second"})
        assert caught.value.error.code == -32000
        assert lifecycle.failed_servers == [] and lifecycle.active_servers == [server]
        await lifecycle.reconnect(failed_only=True)
        assert server.connects == 1
        # Runtime 错误并不自动进入 failed_servers；all reconnect 会复用同一个对象。
        new_remote = remote.start(drifted)
        old_closure = server.call_tool
        await lifecycle.reconnect(failed_only=False)
        assert server.connects == 2
        stale = await server.list_tools()
        assert (
            next(t for t in stale if t.name == "lookup").input_schema["properties"]["key"]["type"]
            == "string"
        )
        result = await old_closure("lookup", {"key": "7"})
        assert not result.is_error
        assert new_remote.recorder.tool_calls == [("lookup", {"key": 7})]
        server.invalidate_tools_cache()
        changed = await server.list_tools()
        assert changed[0].input_schema["properties"]["key"]["type"] == "integer"
        print("manager_runtime_failure", "not_recorded", "old_closure_reaches_new_connection")
    finally:
        await lifecycle.cleanup_all()
        remote.stop()


async def test_new_generation_manager_preserves_the_old_connection_snapshot():
    remote = Restartable()
    live = remote.start(mcp_app())
    old_server = CountedHTTP(live.recorder.url)
    old = manager(old_server)
    candidate = None
    try:
        await old.__aenter__()
        await old_server.list_tools()
        bound_old = old_server.call_tool
        remote.stop()
        with pytest.raises(MCPError):
            await bound_old("lookup", {"key": "old"})
        live2 = remote.start(drifted)
        new_server = CountedHTTP(live2.recorder.url)
        candidate = manager(new_server)
        await candidate.__aenter__()
        assert (await new_server.list_tools())[0].input_schema["properties"]["key"][
            "type"
        ] == "integer"
        # A 已捕获旧 Server；新 generation 的 schema 漂移不能让 A 串入新 Server。
        with pytest.raises(MCPError):
            await bound_old("lookup", {"key": "7"})
        assert live2.recorder.tool_calls == []
        await new_server.call_tool("lookup", {"key": 7})
        assert live2.recorder.tool_calls == [("lookup", {"key": 7})]
    finally:
        if candidate is not None:
            await candidate.cleanup_all()
        await old.cleanup_all()
        remote.stop()
