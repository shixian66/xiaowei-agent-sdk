"""P1-B 测试的 StarRocks 真实环境开关。

``starrocks_real`` 用例默认不收集；只有显式 ``-m starrocks_real`` 时运行，且此时
``SDK_TEST_STARROCKS_ADMIN_URL`` 缺失或不合法即失败，不跳过。该地址只能是 loopback 上一个
可丢弃的测试实例（例如 ``mysql://root@127.0.0.1:59030``）：fixture 用它建立合成数据库和只读
账号，Adapter 始终以只读账号连接。pytest-socket 只对这些用例放行该主机。

``starrocks_audit_real`` 用例同样默认不收集，只在显式 ``-m starrocks_audit_real`` 时运行；
它另外要求该实例已安装官方 AuditLoader（审计表为默认库表名），缺失即失败。
"""

import os
from urllib.parse import urlsplit

import pytest

ADMIN_URL_ENV = "SDK_TEST_STARROCKS_ADMIN_URL"
MARKER = "starrocks_real"
AUDIT_MARKER = "starrocks_audit_real"
LOOPBACK = "127.0.0.1"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", f"{MARKER}: 需要可丢弃的真实 StarRocks 测试实例")
    config.addinivalue_line(
        "markers", f"{AUDIT_MARKER}: 需要已安装 AuditLoader 的可丢弃 StarRocks 测试实例"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    expression = config.option.markexpr or ""
    keep, drop = [], []
    for item in items:
        marker = next(
            (m for m in (AUDIT_MARKER, MARKER) if item.get_closest_marker(m) is not None), None
        )
        # 按词元匹配：-m starrocks_real 不会选中 starrocks_audit_real。
        selected = (
            marker is not None and marker in expression.replace("(", " ").replace(")", " ").split()
        )
        if marker is None:
            keep.append(item)
        elif selected:
            item.add_marker(pytest.mark.allow_hosts([LOOPBACK]))
            keep.append(item)
        else:
            drop.append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def admin_address() -> tuple[str, int, str]:
    """返回 (host, port, user)；缺失或不是 loopback 的 ``mysql://user@127.0.0.1:port`` 即失败。"""
    raw = os.environ.get(ADMIN_URL_ENV)
    if not raw:
        pytest.fail(f"{ADMIN_URL_ENV} 未设置：需要可丢弃的 StarRocks 测试实例")
    parts = urlsplit(raw)
    if (
        parts.scheme != "mysql"
        or parts.hostname != LOOPBACK
        or parts.port is None
        or not parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
    ):
        pytest.fail(f"{ADMIN_URL_ENV} 必须是 mysql://user@127.0.0.1:port 形式的无密码测试实例")
    return LOOPBACK, parts.port, parts.username
