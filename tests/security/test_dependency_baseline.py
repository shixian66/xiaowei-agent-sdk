"""产品依赖、正式入口与严格类型检查的公共约束。"""

import ast
import io
import tokenize
import tomllib
from importlib.util import find_spec
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"

# 依赖集合变化须随实现一起审查，不接受意外加入的运行或开发依赖。
_EXPECTED_RUNTIME_DEPENDENCIES = frozenset(
    {
        "httpx2",
        "pydantic",
        "sqlglot",
        "sqlalchemy",
        "asyncpg",
        "fastapi",
        "uvicorn",
        "openai-agents",
        "asyncmy",
        "lark-channel-sdk",
        "markdown-it-py",
        "pillow",
        "nh3",
        "tinycss2",
    }
)

_EXPECTED_DEV_DEPENDENCIES = frozenset(
    {
        "pytest",
        "pytest-asyncio",
        "pytest-socket",
        "ruff",
        "mypy",
        "pip-audit",
        "hatchling",
        "httpx",
        "pyyaml",
    }
)


def _pyproject() -> dict[str, object]:
    data: dict[str, object] = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data


def _requirement_names(specs: list[str]) -> set[str]:
    """``"sqlalchemy[asyncio]>=2.0.44,<2.1"`` → ``"sqlalchemy"``。"""
    names: set[str] = set()
    for spec in specs:
        head = spec.split(";", 1)[0].strip()
        for separator in ("[", ">", "<", "=", "!", "~", " "):
            head = head.split(separator, 1)[0]
        names.add(head.strip().lower().replace("_", "-"))
    return names


def _internal_module_imports(path: Path) -> set[str]:
    """文件中出现的所有顶层第三方/标准库模块名。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_runtime_dependency_set_is_exactly_the_approved_set() -> None:
    """依赖面用集合相等钉死，不用禁用清单。

    禁用清单只挡得住已经想到的那些；集合相等连"想不到的"一起挡住。
    """
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    declared = project["dependencies"]
    assert isinstance(declared, list)
    assert _requirement_names(declared) == set(_EXPECTED_RUNTIME_DEPENDENCIES)


def test_dev_dependency_set_is_exactly_the_approved_tools() -> None:
    """开发依赖只包含当前产品测试与检查所需工具。"""
    optional = _pyproject()["project"]
    assert isinstance(optional, dict)
    dev = optional["optional-dependencies"]
    assert isinstance(dev, dict)
    dev_specs = dev["dev"]
    assert isinstance(dev_specs, list)
    assert _requirement_names(dev_specs) == set(_EXPECTED_DEV_DEPENDENCIES)
    assert not ({"fastapi", "uvicorn"} & _requirement_names(dev_specs))


def test_only_the_dev_extra_is_declared() -> None:
    """旧工程已移除，不保留其专属依赖入口。"""
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    extras = project["optional-dependencies"]
    assert isinstance(extras, dict)
    assert set(extras) == {"dev"}


def test_wheel_and_command_only_point_at_the_new_package() -> None:
    """正式命令与 wheel 只指向新包 ``xiaowei``。"""
    data = _pyproject()
    project = data["project"]
    assert isinstance(project, dict)
    assert project["scripts"] == {"xiaowei": "xiaowei.cli:main"}
    tool = data["tool"]
    assert isinstance(tool, dict)
    assert tool["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/xiaowei"]


def test_new_package_never_imports_the_old_package() -> None:
    offenders = [
        str(path.relative_to(_ROOT))
        for path in (_SRC / "xiaowei").rglob("*.py")
        if "xiaowei_agent" in _internal_module_imports(path)
    ]
    assert not offenders


def test_sqlalchemy_ships_py_typed() -> None:
    """SQLAlchemy 自带类型声明，strict 检查不需要放宽。"""
    spec = find_spec("sqlalchemy")
    assert spec is not None and spec.origin is not None
    assert (Path(spec.origin).parent / "py.typed").is_file()


def test_asyncpg_still_lacks_py_typed() -> None:
    """反向 tripwire：``asyncpg`` 目前**没有** ``py.typed``。

    这条断言故意钉住上游的现状。一旦 asyncpg 发布 ``py.typed``，它会转红——那正是
    重新决定"是否允许 ``src/`` 直接 import asyncpg"的时刻。没有这条，下面那条禁令
    会变成一条无人知道何时可以撤销的永久规则。
    """
    spec = find_spec("asyncpg")
    assert spec is not None and spec.origin is not None
    marker = Path(spec.origin).parent / "py.typed"
    assert not marker.is_file(), (
        "asyncpg 现已自带 py.typed：可重新评估 test_src_never_imports_asyncpg_directly 的禁令"
    )


def test_src_never_imports_asyncpg_directly() -> None:
    """驱动只经 DSN 方言字符串加载，业务代码不得 import 它的符号。

    直接 import 会在 ``mypy --strict`` 下产生 ``import-untyped``，唯一的消解办法是
    放宽类型检查。禁止 import 让这个取舍根本不出现。
    """
    offenders = [
        str(path.relative_to(_ROOT))
        for path in _SRC.rglob("*.py")
        if "asyncpg" in _internal_module_imports(path)
    ]
    assert not offenders, f"src/ 不得直接 import asyncpg（无 py.typed）：{offenders}"


def test_mypy_stays_globally_strict() -> None:
    """全局 strict 不得被放宽，也不得出现全局 ``ignore_missing_imports``。

    §5.3 只允许**逐模块** override。全局开关会让所有第三方缺失存根一起静默，
    包括将来引入的、我们还没看过的那些。
    """
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    mypy_config = tool["mypy"]
    assert isinstance(mypy_config, dict)
    assert mypy_config["strict"] is True
    assert "ignore_missing_imports" not in mypy_config
    assert "follow_imports" not in mypy_config


def _type_ignore_comment_lines(source: str) -> list[int]:
    lines: list[int] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type is tokenize.COMMENT and "type: ignore" in token.string:
            lines.append(token.start[0])
    return lines


def test_no_module_silences_a_type_error() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        for line in _type_ignore_comment_lines(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(_SRC)}:{line}")
    assert not offenders, offenders


def test_the_type_ignore_detector_ignores_docstrings_and_catches_comments() -> None:
    assert _type_ignore_comment_lines('"""说明：不许写 type: ignore。"""\n') == []
    assert _type_ignore_comment_lines("x = 1  # type: ignore[arg-type]\n") == [1]


def test_mypy_relaxation_is_limited_to_approved_untyped_dependencies() -> None:
    """飞书 SDK 与报告 CSS 解析器没有 ``py.typed``，仅允许这两个依赖的缺失存根。

    全局仍是 strict，新增任何放宽都要改这里；依赖增加类型声明后应删除对应 override。
    """
    mypy = _pyproject()["tool"]
    assert isinstance(mypy, dict)
    config = mypy["mypy"]
    assert isinstance(config, dict)
    assert config["strict"] is True
    assert config["overrides"] == [
        {"module": ["lark_channel.*"], "ignore_missing_imports": True},
        {"module": ["tinycss2.*"], "ignore_missing_imports": True},
    ]
