"""共享测试环境隔离；网络默认关闭，所需 loopback 放行由各产品测试目录声明。"""

import os
from collections.abc import Iterator

import pytest

_PREFIX = "XIAOWEI_"


@pytest.fixture(autouse=True)
def clean_xiaowei_env() -> Iterator[None]:
    """每个用例前清除全部 XIAOWEI_* 变量，结束后原样恢复。"""
    saved = {k: v for k, v in os.environ.items() if k.upper().startswith(_PREFIX)}
    for k in saved:
        del os.environ[k]
    try:
        yield
    finally:
        for k in [k for k in os.environ if k.upper().startswith(_PREFIX)]:
            del os.environ[k]
        os.environ.update(saved)
