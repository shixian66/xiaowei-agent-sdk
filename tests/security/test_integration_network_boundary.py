"""默认阻断网络；产品协议测试仅按用例放行 loopback。

pytest-socket 的 socket_enabled 会跳过 allow_hosts 约束，因此禁止使用；
全局 addopts 同样不得放行网络。
"""

import ast
import socket
import tomllib
from pathlib import Path

import pytest
from pytest_socket import SocketBlockedError

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]


def test_sockets_outside_the_integration_directory_are_still_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_name_resolution_outside_the_integration_directory_is_still_blocked() -> None:
    """``getaddrinfo`` 也必须被拦：只拦 ``socket()`` 挡不住 DNS 侧信道。"""
    with pytest.raises(SocketBlockedError):
        socket.getaddrinfo("example.invalid", 80)


def test_addopts_never_carries_a_global_allow_hosts() -> None:
    """全局 ``--allow-hosts`` 会让 ``disable_socket()`` 对**整个套件**永不执行。

    pytest-socket 只在 ``_resolve_allow_hosts`` 返回空时才 ``disable_socket``；命令行
    或 addopts 里的 ``--allow-hosts`` 对每个 item 都返回非空。于是一个"只是为了让
    integration 能连库"的开关，会静默解除全部用例的网络封锁。
    """
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    assert "--allow-hosts" not in addopts
    assert "--disable-socket" in addopts


def test_no_test_uses_the_bare_socket_enabled_fixture() -> None:
    """``socket_enabled`` 会让 ``allow_hosts`` 完全失效——它不是"放行"，是全开。"""
    offenders: list[str] = []
    for path in (_ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names = {argument.arg for argument in node.args.args}
                if "socket_enabled" in names:
                    offenders.append(f"{path.relative_to(_ROOT)}::{node.name}")
            if isinstance(node, ast.Attribute) and node.attr in {
                "enable_socket",
                "socket_enabled",
            }:
                offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, f"裸用 socket_enabled 会让 allow_hosts 失效：{offenders}"


_BAN_EXPLAINERS = frozenset(
    {
        "tests/security/test_integration_network_boundary.py",
    }
)
"""只有本文件解释被禁的 ``socket_enabled`` 用法。"""


def test_no_file_under_tests_even_recommends_the_banned_fixture() -> None:
    """禁令此前只扫代码，散文不受约束——于是散文漂了。

    ``tests/conftest.py`` 的模块 docstring 一度写着"未来需要放行时使用官方提供的
    ``socket_enabled``"，而同一仓库的这个文件正禁止它。照做的人会写出一条被本文件
    判红的用例，然后以为是护栏坏了。

    AST 扫描看不到这种漂移：docstring 里没有 ``ast.Name`` 也没有 ``ast.Attribute``。
    因此这里按**原文**扫，只豁免解释禁令本身的文件。
    """
    offenders = [
        str(path.relative_to(_ROOT))
        for path in (_ROOT / "tests").rglob("*.py")
        if str(path.relative_to(_ROOT)) not in _BAN_EXPLAINERS
        and "socket_enabled" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"这些文件提到了被禁的 socket_enabled：{offenders}"


def test_the_ban_explainers_still_exist_and_still_explain() -> None:
    """反空洞：豁免名单里的文件若被删或改名，上一条会平凡通过而禁令悄悄失去解释。"""
    for relative in _BAN_EXPLAINERS:
        path = _ROOT / relative
        assert path.exists(), relative
        assert "socket_enabled" in path.read_text(encoding="utf-8"), relative


def test_sdk_allowlist_is_only_loopback() -> None:
    """SDK 集成用例只放行隔离测试实例所在的 loopback。"""
    from tests.sdk_core.postgres_harness import LOOPBACK

    assert LOOPBACK == "127.0.0.1"
    source = (_ROOT / "tests/sdk_core/conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    marker_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "allow_hosts"
    ]
    assert len(marker_calls) == 1, "allow_hosts 只应在一处构造"
    (argument,) = marker_calls[0].args
    assert isinstance(argument, ast.List), "放行清单必须是显式列表"
    assert len(argument.elts) == 1, "只放行 loopback"
    assert isinstance(argument.elts[0], ast.Name) and argument.elts[0].id == "LOOPBACK"
