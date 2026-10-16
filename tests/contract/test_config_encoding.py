"""按**进程 locale** 读取的配置文件必须是 ASCII —— 否则换台机器就读不出来。

Alembic 用 ``ConfigParser.read(..., encoding="locale")`` 读 ``alembic.ini``
（``alembic/util/compat.py``）。``"locale"`` 由进程环境决定，不由仓库决定，Alembic
也没有任何选项能覆盖它。于是 ini 里的一个中文注释在 ``LANG`` 为 UTF-8 的机器上完全
正常，在 ``LC_ALL=C``（macOS 上解析为 US-ASCII）的机器上直接 ``UnicodeDecodeError``。

这不只是"测试在某些机器上红"。同一条读取路径也是 ``alembic upgrade head`` 的路径，
所以受影响的是**迁移本身**：一个 locale 不是 UTF-8 的运维环境根本跑不了迁移。

**范围为什么只有 ``*.ini`` / ``*.cfg``**：仓库里其余配置的读取方都规定了编码，与
locale 无关——``pyproject.toml`` 由 tomllib 按 TOML 规范读 UTF-8，``.gitleaks.toml``
由 gitleaks（Go，UTF-8）读，工作流 YAML 由 GitHub 读，``env.py`` 与迁移脚本是 ``.py``
（PEP 263 默认 UTF-8），``script.py.mako`` 由 Mako 读且其 lexer 在未指定编码时默认
UTF-8。这些都可以放心写中文；configparser 那条路不行。
"""

import re
from configparser import ConfigParser
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# 不下探的目录：虚拟环境与各类缓存里全是第三方文件，它们的编码不归本仓库管。
_PRUNED = {".git", ".venv", "__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache"}

# 形如 ``postgresql+asyncpg://`` / ``redis://`` 的连接串前缀。
_DSN_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9.+-]*://")


def _locale_read_configs() -> list[Path]:
    """仓库里所有会被 configparser 按 locale 编码读取的文件。"""
    found: list[Path] = []
    stack = [_ROOT]
    while stack:
        current = stack.pop()
        for child in current.iterdir():
            if child.is_dir():
                if child.name not in _PRUNED:
                    stack.append(child)
            elif child.suffix in {".ini", ".cfg"}:
                found.append(child)
    return sorted(found)


def test_the_scan_actually_finds_the_known_locale_read_config() -> None:
    """反空洞：扫不到文件时下面两条会平凡通过，而"没有文件"与"全部合规"无法区分。"""
    found = _locale_read_configs()
    assert found, "没有扫到任何 configparser 配置——扫描逻辑坏了，不是仓库干净了"
    assert _ROOT / "alembic.ini" in found


def test_locale_read_configs_are_ascii_only() -> None:
    """逐字节判定，与 configparser 在 ASCII locale 下做的解码是同一件事。"""
    for path in _locale_read_configs():
        raw = path.read_bytes()
        offenders = [index for index, byte in enumerate(raw) if byte > 0x7F]
        assert not offenders, (
            f"{path.relative_to(_ROOT)} 第 {offenders[0]} 字节非 ASCII："
            "该文件按进程 locale 读取，非 ASCII 会让它在非 UTF-8 环境下无法解码"
        )


def test_alembic_ini_parses_under_an_ascii_decoder() -> None:
    """走 Alembic 的真实读取方式，只是把 ``"locale"`` 钉成 ``"ascii"``。

    不去改真实 locale：``encoding="locale"`` 由 CPython 在 C 层解析
    （``_Py_GetLocaleEncoding``），monkeypatch ``locale.getencoding`` 不生效；而靠
    ``LC_ALL=C`` 起子进程在 Linux 上会被 PEP 538 强制转成 C.UTF-8，那样这条检查会
    随平台变成"有时才真的在测东西"。直接指定 ``ascii`` 是确定的同一件事。
    """
    parser = ConfigParser()
    read = parser.read([_ROOT / "alembic.ini"], encoding="ascii")
    assert read, "alembic.ini 未被读到"
    assert parser.get("alembic", "script_location") == ("src/xiaowei_agent/persistence/migrations")
    assert parser.get("alembic", "path_separator") == "os"


def test_no_locale_read_config_carries_a_connection_string() -> None:
    """任何选项值都不得是连接串。

    此前这条写成"遍历以 ``sqlalchemy.url`` 开头的行，若有则断言其为空"。但 ini 里
    根本没有那个键，循环体一次都不执行——它恒绿，把 DSN 写进**别的**键也照样通过。
    改成扫描全部选项值，并断言确实扫过东西。
    """
    inspected = 0
    for path in _locale_read_configs():
        parser = ConfigParser()
        parser.read([path], encoding="ascii")
        for section in parser.sections():
            for option in parser.options(section):
                # raw=True：不做插值，否则 %(levelname)s 之类的日志格式会抛
                # InterpolationSyntaxError，而我们要看的就是字面值。
                value = parser.get(section, option, raw=True)
                inspected += 1
                assert not _DSN_SCHEME.match(value.strip()), (
                    f"{path.relative_to(_ROOT)} 的 [{section}]{option} 是一个连接串"
                )
    assert inspected, "没有检查到任何选项值"
