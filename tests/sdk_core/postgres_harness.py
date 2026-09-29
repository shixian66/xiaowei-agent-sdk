"""测试 PostgreSQL 实例的身份校验：在执行任何 DDL 之前确认连接的是隔离测试实例。"""

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncConnection

LOOPBACK = "127.0.0.1"
PORT = 55432
ADMIN_USER = "postgres"
ADMIN_DATABASE = "postgres"
INSTANCE_MARKER = "xiaowei-sdk-test"


class HarnessMisconfiguredError(Exception):
    """测试数据库地址不是隔离测试实例；消息不回显 URL。"""


def parse_admin_url(raw: str) -> URL:
    """只接受 compose.sdk-test.yml 声明的管理连接；不连接、不回显原始地址。"""
    try:
        url = make_url(raw)
    except ArgumentError:
        raise HarnessMisconfiguredError("地址格式无效") from None
    expected = ("postgresql+asyncpg", LOOPBACK, PORT, ADMIN_USER, ADMIN_DATABASE)
    if (url.drivername, url.host, url.port, url.username, url.database) != expected:
        raise HarnessMisconfiguredError(
            f"必须是 postgresql+asyncpg://{ADMIN_USER}@{LOOPBACK}:{PORT}/{ADMIN_DATABASE}"
        )
    return url


async def verify_test_instance(
    conn: AsyncConnection, *, expected_marker: str = INSTANCE_MARKER
) -> None:
    """读取服务器的 ``cluster_name``；同端口上的其他 PostgreSQL 没有该标识。"""
    marker = (await conn.execute(text("SHOW cluster_name"))).scalar_one()
    if marker != expected_marker:
        raise HarnessMisconfiguredError("连接的 PostgreSQL 不是隔离测试实例")
