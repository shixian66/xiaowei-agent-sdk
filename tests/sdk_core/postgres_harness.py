"""测试 PostgreSQL 实例的身份校验：在执行任何 DDL 之前确认连接的是隔离测试实例。"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

LOOPBACK = "127.0.0.1"
PORT = 55432
ADMIN_USER = "postgres"
ADMIN_DATABASE = "postgres"
INSTANCE_MARKER = "xiaowei-sdk-test"
TEST_DATABASE_PREFIX = "xw_sdk_test_"
ADMIN_URL = f"postgresql+asyncpg://{ADMIN_USER}@{LOOPBACK}:{PORT}/{ADMIN_DATABASE}"


class HarnessMisconfiguredError(Exception):
    """测试数据库地址不是隔离测试实例；消息不回显 URL。"""


def parse_admin_url(raw: str) -> URL:
    """只接受唯一声明的完整管理地址。

    按原始字符串整体比较：SQLAlchemy 会用 query 参数覆盖 host/port/user/database，端口等
    字段也要等到访问时才解析，因此比较解析后的字段挡不住绕过，也挡不住格式错误逃逸。
    """
    if raw != ADMIN_URL:
        raise HarnessMisconfiguredError(f"必须是 {ADMIN_URL}")
    return make_url(ADMIN_URL)


async def verify_test_instance(
    conn: AsyncConnection, *, expected_marker: str = INSTANCE_MARKER
) -> None:
    """读取服务器的 ``cluster_name``；同端口上的其他 PostgreSQL 没有该标识。"""
    marker = (await conn.execute(text("SHOW cluster_name"))).scalar_one()
    if marker != expected_marker:
        raise HarnessMisconfiguredError("连接的 PostgreSQL 不是隔离测试实例")


@asynccontextmanager
async def isolated_database() -> AsyncIterator[URL]:
    """在测试实例中新建一个独立数据库，结束后只删除这个数据库。

    目标只来自 ``ADMIN_URL`` 常量，调用方无法传入其他地址。
    """
    admin = make_url(ADMIN_URL)
    name = f"{TEST_DATABASE_PREFIX}{uuid.uuid4().hex[:12]}"
    engine = create_async_engine(admin, isolation_level="AUTOCOMMIT")
    try:
        await _verified_ddl(engine, f'CREATE DATABASE "{name}"')
        try:
            yield admin.set(database=name)
        finally:
            # 调用体失败也要删除；删除同样先在执行连接上核对实例。
            await _verified_ddl(engine, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await engine.dispose()


async def _verified_ddl(engine: AsyncEngine, statement: str) -> None:
    """每条 DDL 都在执行它的同一连接上先核对实例，不依赖连接池复用已验证的连接。"""
    async with engine.connect() as conn:
        await verify_test_instance(conn)
        await conn.execute(text(statement))
