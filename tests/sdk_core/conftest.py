"""SDK 核心测试的真实 PostgreSQL、loopback MCP fixture 与网络放行。

默认无网络。只有标记 ``loopback`` 的用例获得 pytest-socket 的 ``allow_hosts`` marker，
且只放行 ``127.0.0.1``。PostgreSQL 地址来自 ``SDK_TEST_POSTGRES_URL``（不带 ``XIAOWEI_``
前缀，避免被根 conftest 清理）；未设置时相关用例失败，不跳过。
"""

import os
import socket
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer as FixtureMCPServer
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from tests.sdk_core.postgres_harness import (
    LOOPBACK,
    HarnessMisconfiguredError,
    parse_admin_url,
    verify_test_instance,
)

from xiaowei.config import configure_runtime

POSTGRES_URL_ENV = "SDK_TEST_POSTGRES_URL"
_TEST_DB_PREFIX = "xw_sdk_test_"
_POSTGRES_URL_STASH: pytest.StashKey[str | None] = pytest.StashKey()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "loopback: 仅允许连接 127.0.0.1 的用例")
    config.stash[_POSTGRES_URL_STASH] = os.environ.get(POSTGRES_URL_ENV) or None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if item.get_closest_marker("loopback") is not None:
            item.add_marker(pytest.mark.allow_hosts([LOOPBACK]))


@pytest.fixture(autouse=True)
def _product_runtime() -> None:
    """每个用例都在产品运行配置下执行：tracing 与默认导出关闭。"""
    configure_runtime()


def _admin_url(config: pytest.Config) -> URL:
    raw = config.stash.get(_POSTGRES_URL_STASH, None)
    if raw is None:
        pytest.fail(
            f"{POSTGRES_URL_ENV} 未设置：需要隔离的真实 PostgreSQL，见 compose.sdk-test.yml"
        )
    try:
        return parse_admin_url(raw)
    except HarnessMisconfiguredError as exc:
        pytest.fail(f"{POSTGRES_URL_ENV} {exc}")


@pytest.fixture
async def postgres_url(request: pytest.FixtureRequest) -> AsyncIterator[URL]:
    """在测试实例中新建一个独立数据库，用例结束后只删除这个数据库。"""
    admin = _admin_url(request.config)
    name = f"{_TEST_DB_PREFIX}{uuid.uuid4().hex[:12]}"
    engine = create_async_engine(admin, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            try:
                await verify_test_instance(conn)
            except HarnessMisconfiguredError as exc:
                pytest.fail(f"{POSTGRES_URL_ENV} {exc}")
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield admin.set(database=name)
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await engine.dispose()


@dataclass
class MCPFixture:
    url: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def mcp_fixture() -> Iterator[MCPFixture]:
    """仅服务合成数据的 loopback Streamable HTTP MCP 服务，不属于产品部署。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((LOOPBACK, 0))
    port = sock.getsockname()[1]
    fixture = MCPFixture(url=f"http://{LOOPBACK}:{port}/mcp")

    app = FixtureMCPServer("xiaowei-sdk-test-fixture")

    @app.tool()
    def lookup(key: str) -> dict[str, Any]:
        """返回合成记录。"""
        fixture.tool_calls.append({"key": key})
        return {"key": key, "value": 7, "private_note": "fixture-private-note"}

    server = uvicorn.Server(
        uvicorn.Config(
            app.streamable_http_app(host=LOOPBACK),
            log_level="warning",
            lifespan="on",
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if server.started:
                break
            time.sleep(0.01)
        else:
            pytest.fail("MCP fixture 未能启动")
        yield fixture
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
