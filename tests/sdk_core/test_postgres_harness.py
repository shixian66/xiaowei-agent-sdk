"""测试 PostgreSQL 的隔离护栏：误配的地址在任何 DDL 之前被拒绝。

fixture 会在测试实例上执行 ``CREATE DATABASE`` / ``DROP DATABASE ... WITH (FORCE)``，
因此“连接的就是 compose.sdk-test.yml 启动的那台一次性实例”必须由代码核对，而不是约定。
"""

import re
from pathlib import Path

import pytest
import yaml
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from tests.sdk_core.postgres_harness import (
    ADMIN_DATABASE,
    ADMIN_USER,
    INSTANCE_MARKER,
    LOOPBACK,
    PORT,
    HarnessMisconfiguredError,
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
    ],
)
def test_other_instances_are_rejected_before_any_connection(raw: str) -> None:
    with pytest.raises(HarnessMisconfiguredError) as excinfo:
        parse_admin_url(raw)
    assert raw not in str(excinfo.value)


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
