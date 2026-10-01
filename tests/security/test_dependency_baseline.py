"""生产依赖面与类型检查严格度是同一条边界。

M4 引入 SQLAlchemy / Alembic / asyncpg，M5 引入 FastAPI / uvicorn。
两件事必须被机制钉住，而不是靠计划里的一句话：

1. **依赖面**。M5 只新增 HTTP gateway 所需的 FastAPI / uvicorn，以及开发侧的
   httpx / PyYAML；仍不引入 Redis、队列、Worker 框架、模型 SDK 或 LangGraph。
   用集合相等表达比用禁用清单强：禁用清单挡不住清单外的新依赖。

2. **``asyncpg`` 不得被 ``src/`` 直接 import**。T0 实测：``asyncpg`` 0.30.0 **不带
   ``py.typed``**，直接 import 会让 ``mypy --strict`` 报 ``import-untyped``。M4 计划
   §5.3 给的退路是"对单个模块加 ``ignore_missing_imports``"，但那条退路不必走：
   驱动只经 ``postgresql+asyncpg://`` 的 DSN 方言字符串被 SQLAlchemy 内部加载，
   业务代码从不需要它的符号。**不 import 就不需要任何放宽**，全局 strict 完好。
"""

import ast
import tomllib
from importlib.util import find_spec
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
# 扫描整个 src/：新包 xiaowei 与过渡期旧包 xiaowei_agent 都受同一条导入禁令约束。
_SRC = _ROOT / "src"

# ``[project].dependencies`` 的包名集合——新增任何一项都必须先改这里，
# 从而必须在 review 里被看见。P1-A Task 1 按新产品方向加入 OpenAI Agents SDK
# （``openai-agents[sqlalchemy]``）；P1-B Task 2 按计划加入 StarRocks 驱动 ``asyncmy``（精确钉版）；
# P1-B Task 7 加入飞书 ``lark-channel-sdk``（精确钉版）。
# 旧包专用的依赖在 P1-B 清理时重新取舍。
_EXPECTED_RUNTIME_DEPENDENCIES = frozenset(
    {
        "pydantic",
        "sqlglot",
        "sqlalchemy",
        "alembic",
        "asyncpg",
        "fastapi",
        "uvicorn",
        "openai-agents",
        "asyncmy",
        "lark-channel-sdk",
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
    """M5+ 的基础设施依赖也不得从 ``dev`` 这条侧门进来。

    ``dev`` 不是"随便放"的口袋：一个只在 dev 出现的 FastAPI 同样会让人开始写
    interfaces 层，而它的准入门在 M5。
    """
    optional = _pyproject()["project"]
    assert isinstance(optional, dict)
    dev = optional["optional-dependencies"]
    assert isinstance(dev, dict)
    dev_specs = dev["dev"]
    assert isinstance(dev_specs, list)
    assert _requirement_names(dev_specs) == set(_EXPECTED_DEV_DEPENDENCIES)
    assert not ({"fastapi", "uvicorn"} & _requirement_names(dev_specs))


@pytest.mark.parametrize("package", ["sqlalchemy", "alembic"])
def test_typed_dependency_ships_py_typed(package: str) -> None:
    """SQLAlchemy 与 Alembic 自带 ``py.typed``，因此 strict 下无需任何放宽。

    这条转红意味着上游撤回了类型标注，届时必须重新评估 §5.3.1 的结论，
    而不是顺手加 ``ignore_missing_imports``。
    """
    spec = find_spec(package)
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


def test_mypy_relaxation_is_limited_to_the_untyped_feishu_sdk() -> None:
    """唯一的 mypy 放宽：``lark-channel-sdk`` 1.4.0 没有 ``py.typed``，而飞书入口必须导入它。

    只允许对 ``lark_channel.*`` 关闭缺少类型信息的报错；全局仍是 strict，新增任何放宽都要改这里。
    """
    mypy = _pyproject()["tool"]
    assert isinstance(mypy, dict)
    config = mypy["mypy"]
    assert isinstance(config, dict)
    assert config["strict"] is True
    assert config["overrides"] == [{"module": ["lark_channel.*"], "ignore_missing_imports": True}]
