"""实验沿用 SDK 测试的隔离数据库和逐用例 loopback 放行，不属于产品。"""

import pytest
from tests.sdk_core.conftest import postgres_url as postgres_url
from tests.sdk_core.conftest import pytest_configure as pytest_configure
from tests.sdk_core.conftest import test_postgres as test_postgres

from xiaowei.config import configure_runtime


@pytest.fixture(autouse=True)
def _runtime() -> None:
    configure_runtime()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.get_closest_marker("loopback") is not None:
            item.add_marker(pytest.mark.allow_hosts(["127.0.0.1"]))
