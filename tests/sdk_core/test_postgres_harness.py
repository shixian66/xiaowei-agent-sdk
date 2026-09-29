"""测试 PostgreSQL 的隔离护栏：误配的地址在任何 DDL 之前被拒绝。

fixture 会在测试实例上执行 ``CREATE DATABASE`` / ``DROP DATABASE ... WITH (FORCE)``，
因此“连接的就是 compose.sdk-test.yml 启动的那台一次性实例”必须由代码核对，而不是约定。
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import event
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.ext.asyncio import create_async_engine
from tests.sdk_core.postgres_harness import (
    ADMIN_DATABASE,
    ADMIN_USER,
    INSTANCE_MARKER,
    LOOPBACK,
    PORT,
    HarnessMisconfiguredError,
    isolated_database,
    parse_admin_url,
    verify_test_instance,
)

_COMPOSE = Path(__file__).resolve().parents[2] / "compose.sdk-test.yml"
_DOCUMENTED_URL = "postgresql+asyncpg://postgres@127.0.0.1:55432/postgres"


def test_documented_url_is_accepted() -> None:
    url = parse_admin_url(_DOCUMENTED_URL)
    assert (url.host, url.port, url.username, url.database) == (
        LOOPBACK,
        PORT,
        ADMIN_USER,
        ADMIN_DATABASE,
    )


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("postgresql+asyncpg://admin@127.0.0.1:5432/production", id="review-case"),
        pytest.param("postgresql+asyncpg://postgres@127.0.0.1:5432/postgres", id="default-port"),
        pytest.param("postgresql+asyncpg://postgres@127.0.0.1/postgres", id="no-port"),
        pytest.param("postgresql+asyncpg://admin@127.0.0.1:55432/postgres", id="other-user"),
        pytest.param("postgresql+asyncpg://postgres@127.0.0.1:55432/app", id="other-database"),
        pytest.param("postgresql+asyncpg://postgres@localhost:55432/postgres", id="hostname"),
        pytest.param("postgresql+asyncpg://postgres@10.0.0.5:55432/postgres", id="remote"),
        pytest.param("postgresql://postgres@127.0.0.1:55432/postgres", id="sync-driver"),
        pytest.param("not a url pw=pw-must-not-leak", id="malformed"),
        # SQLAlchemy 会用 query 覆盖主字段，决定实际连接的是完整输入，而不是解析后的主字段。
        pytest.param(_DOCUMENTED_URL + "?port=55433", id="query-port"),
        pytest.param(_DOCUMENTED_URL + "?host=10.0.0.5", id="query-host"),
        pytest.param(_DOCUMENTED_URL + "?user=admin", id="query-user"),
        pytest.param(_DOCUMENTED_URL + "?database=app", id="query-database"),
        pytest.param(
            "postgresql+asyncpg://postgres:pw-must-not-leak@127.0.0.1:55432/postgres",
            id="password",
        ),
        pytest.param(
            "postgresql+asyncpg://postgres@127.0.0.1:pw-must-not-leak/postgres", id="bad-port"
        ),
        pytest.param(_DOCUMENTED_URL + " ", id="trailing-space"),
    ],
)
def test_other_instances_are_rejected_before_any_connection(raw: str) -> None:
    with pytest.raises(HarnessMisconfiguredError) as excinfo:
        parse_admin_url(raw)
    assert raw not in str(excinfo.value)
    assert "pw-must-not-leak" not in str(excinfo.value)


def test_accepted_url_drives_the_actual_connect_arguments() -> None:
    """断言驱动实际拿到的连接参数，而不只是 URL 对象的字段。"""
    url = parse_admin_url(_DOCUMENTED_URL)
    _, kwargs = create_async_engine(url).dialect.create_connect_args(url)
    assert kwargs == {
        "host": LOOPBACK,
        "port": PORT,
        "user": ADMIN_USER,
        "database": ADMIN_DATABASE,
    }


def test_compose_declares_the_same_pinned_instance() -> None:
    service = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]["postgres"]
    # 与 CI 的镜像规则一致：版本标签 + 内容 digest。
    assert re.fullmatch(r"[^\s@:]+:[^\s@]+@sha256:[0-9a-f]{64}", service["image"])
    assert service["ports"] == [f"{LOOPBACK}:{PORT}:5432"]
    assert f"cluster_name={INSTANCE_MARKER}" in service["command"]
    assert service["tmpfs"] == ["/var/lib/postgresql/data"]


@pytest.mark.loopback
async def test_server_marker_is_verified(postgres_url: URL) -> None:
    admin = postgres_url.set(database=ADMIN_DATABASE)
    engine = create_async_engine(admin)
    try:
        async with engine.connect() as conn:
            await verify_test_instance(conn)
            # 同一端口上若是另一台没有该标识的 PostgreSQL，必须拒绝。
            with pytest.raises(HarnessMisconfiguredError):
                await verify_test_instance(conn, expected_marker="some-other-cluster")
    finally:
        await engine.dispose()


@pytest.mark.loopback
async def test_every_ddl_connection_verifies_the_instance_first(test_postgres: None) -> None:
    """建库与删库各自在执行前、在同一连接上核对实例，不依赖连接池复用已验证的连接。"""
    executed: list[tuple[int, str]] = []

    def record(conn: Connection, cursor: Any, statement: str, *args: Any) -> None:
        executed.append((id(conn), statement))

    event.listen(Engine, "before_cursor_execute", record)
    try:
        async with isolated_database():
            pass
    finally:
        event.remove(Engine, "before_cursor_execute", record)

    ddl = [(i, conn) for i, (conn, sql) in enumerate(executed) if " DATABASE " in sql]
    assert [executed[i][1].split()[0] for i, _ in ddl] == ["CREATE", "DROP"]
    for index, conn in ddl:
        earlier = [sql for c, sql in executed[:index] if c == conn]
        assert "SHOW cluster_name" in earlier, executed[index][1]
