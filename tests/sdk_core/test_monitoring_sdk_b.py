"""SDK-B：连接代际由 SDK Manager 管理，跨轮次关闭保持任务归属。"""

import asyncio
import time

import pytest
from agents.mcp import MCPServerStreamableHttp
from sqlalchemy.engine import URL
from tests.sdk_core.mcp_fixture import mcp_app, serve
from tests.sdk_core.test_mcp_integration import config, core

from xiaowei import mcp

pytestmark = pytest.mark.loopback


async def test_integration_connect_and_cross_task_cleanup_share_sdk_worker(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks: list[tuple[str, asyncio.Task[object] | None]] = []

    class ObservedServer(MCPServerStreamableHttp):
        async def connect(self) -> None:
            tasks.append(("connect", asyncio.current_task()))
            await super().connect()

        async def cleanup(self) -> None:
            tasks.append(("cleanup", asyncio.current_task()))
            await super().cleanup()

    monkeypatch.setattr(mcp, "MCPServerStreamableHttp", ObservedServer)
    with serve(mcp_app()) as remote:
        async with core(postgres_url) as c:
            integration = c.integration(config(remote.url, tools=("lookup",)))
            await integration.__aenter__()
            assert integration.source_status == {"fixture": "available"}
            await asyncio.create_task(integration.__aexit__(None, None, None))
        assert remote.open_connections() == 0

    assert [action for action, _ in tasks] == ["connect", "cleanup"]
    assert tasks[0][1] is tasks[1][1]
    assert tasks[0][1] is not None and tasks[0][1].done()


@pytest.mark.parametrize("failure_point", ["connect", "list_tools"])
async def test_failed_candidate_is_closed_by_manager(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    servers: list[MCPServerStreamableHttp] = []
    connect_tasks: list[asyncio.Task[object] | None] = []
    cleanup_tasks: list[asyncio.Task[object] | None] = []

    class FailingServer(MCPServerStreamableHttp):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            servers.append(self)

        async def connect(self) -> None:
            connect_tasks.append(asyncio.current_task())
            await super().connect()
            if failure_point == "connect":
                raise RuntimeError("synthetic failure after connect")

        async def list_tools(self, *args: object, **kwargs: object) -> object:
            if failure_point == "list_tools":
                raise RuntimeError("synthetic discovery failure")
            return await super().list_tools(*args, **kwargs)

        async def cleanup(self) -> None:
            cleanup_tasks.append(asyncio.current_task())
            await super().cleanup()

    monkeypatch.setattr(mcp, "MCPServerStreamableHttp", FailingServer)
    with serve(mcp_app()) as remote:
        async with core(postgres_url) as c:
            async with c.integration(config(remote.url, tools=("lookup",))) as integration:
                assert integration.available_tool_ids == frozenset()
                assert integration.source_status == {"fixture": "unavailable"}
            assert servers and connect_tasks and cleanup_tasks
        assert remote.open_connections() == 0
    assert all(task is connect_tasks[0] for task in cleanup_tasks)
    assert all(task is not None and task.done() for task in cleanup_tasks)


async def test_failed_candidate_cleanup_stays_within_reconnect_limit(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowCleanupServer(MCPServerStreamableHttp):
        async def list_tools(self, run_context=None, agent=None):
            raise RuntimeError("synthetic discovery failure")

        async def cleanup(self) -> None:
            try:
                await asyncio.sleep(10)
            finally:
                await super().cleanup()

    monkeypatch.setattr(mcp, "MCPServerStreamableHttp", SlowCleanupServer)
    with serve(mcp_app()) as remote:
        async with core(postgres_url) as c:
            integration = c.integration(config(remote.url, tools=("lookup",), timeout=5))
            source = integration._sources["fixture"]
            started = time.monotonic()
            with pytest.raises(RuntimeError, match="synthetic discovery failure"):
                await integration._connect(source, 0.2)
            assert time.monotonic() - started < 1
        assert remote.open_connections() == 0
