"""模块依赖必须单向。

分层自下而上是：标准库 → redaction.py（无对内依赖的叶子）→ contracts/ → 其余业务
包。依赖方向一旦反转，"契约不随框架变化"就失效。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"

# ``redaction`` 是**无对内依赖的叶子**：它只含脱敏正则与安全投影原语，不 import
# 任何 xiaowei_agent 模块。因此任何层依赖它都不构成分层违规，写成全局允许而不是
# 逐包加白名单——后者每新增一个使用方就要改一次，必然漂移。
#
# 这条允许是有代价的双向约束：``redaction`` 一旦获得对内依赖，就会从叶子变成
# 环的一部分，故由 ``test_redaction_stays_a_leaf`` 单独钉死。
_UNIVERSAL_LEAF = {"xiaowei_agent.redaction"}

_FILE_SCOPED_PACKAGES = {"interfaces"}

_ALLOWED_INTERNAL = {
    # contracts 除叶子外不得依赖任何内部模块；log / config / trace 一律不可。
    "contracts": {"xiaowei_agent.contracts"},
    "capabilities": {"xiaowei_agent.contracts", "xiaowei_agent.capabilities"},
    # planning 需要 capabilities：PlanCompiler 必须经 build_plan_step() 构造步骤
    # （test_only_effect_module_constructs_plan_step 禁止直接构造 PlanStep），而分类
    # 派生的唯一来源在 capabilities/effect.py。方向是 planning → capabilities，
    # 而 capabilities 只依赖 contracts，因此无环。
    "planning": {
        "xiaowei_agent.contracts",
        "xiaowei_agent.capabilities",
        "xiaowei_agent.planning",
    },
    # governance 需要 capabilities：分类必须在准入边界重算，不能依赖 Runner 记得调。
    "governance": {
        "xiaowei_agent.contracts",
        "xiaowei_agent.planning",
        "xiaowei_agent.capabilities",
        "xiaowei_agent.governance",
    },
    "tools": {"xiaowei_agent.contracts", "xiaowei_agent.planning", "xiaowei_agent.tools"},
    "persistence": {
        "xiaowei_agent.contracts",
        "xiaowei_agent.planning",
        "xiaowei_agent.persistence",
    },
    # runners 是生命周期宿主（ARCHITECTURE §5.6），必须驱动准入与工具调用。
    # 对 evidence 的依赖以 test_evidence_package_is_a_pure_builder 为对价。
    "runners": {
        "xiaowei_agent.contracts",
        "xiaowei_agent.planning",
        "xiaowei_agent.capabilities",
        "xiaowei_agent.governance",
        "xiaowei_agent.persistence",
        "xiaowei_agent.tools",
        "xiaowei_agent.evidence",
        "xiaowei_agent.observability",
        "xiaowei_agent.runners",
    },
    # evidence 是纯构造器：只依赖 contracts（+ 通用叶子 redaction）。这条窄依赖是
    # runners → evidence 那条边的对价，由 test_evidence_layer_purity.py 承重。
    "evidence": {"xiaowei_agent.contracts", "xiaowei_agent.evidence"},
    # application 是编排层：除 interfaces 外的全部业务包 + trace。它是依赖面最宽的
    # 一层，代价是 test_runtime_bypass.py 用 AST 封死"绕过 port 直接摸 Runner 内部"、
    # "自签凭证"与"直接调 Gateway"三条路径。
    "application": {
        "xiaowei_agent.contracts",
        "xiaowei_agent.capabilities",
        "xiaowei_agent.planning",
        "xiaowei_agent.governance",
        "xiaowei_agent.persistence",
        "xiaowei_agent.tools",
        "xiaowei_agent.evidence",
        "xiaowei_agent.reflection",
        "xiaowei_agent.rendering",
        "xiaowei_agent.runners",
        "xiaowei_agent.observability",
        "xiaowei_agent.trace",
        "xiaowei_agent.application",
    },
    # reflection 只消费结构化证据，rendering 只做投影：两者都不得触达工具、存储或
    # 治理组件（test_reflection_has_no_authority.py 另有更严的同向断言）。
    "reflection": {"xiaowei_agent.contracts", "xiaowei_agent.reflection"},
    "rendering": {"xiaowei_agent.contracts", "xiaowei_agent.rendering"},
    "observability": {"xiaowei_agent.contracts", "xiaowei_agent.observability"},
    # 入口层不能使用包级并集；下面的逐文件穷尽表才是它的承重规则。
    "interfaces": {"xiaowei_agent.interfaces"},
}

_ALLOWED_INTERNAL_BY_FILE = {
    "interfaces/__init__.py": {"xiaowei_agent.interfaces"},
    "interfaces/api.py": {
        "xiaowei_agent.application",
        "xiaowei_agent.contracts",
        "xiaowei_agent.interfaces",
        "xiaowei_agent.config",
        "xiaowei_agent.log",
        "xiaowei_agent.trace",
    },
    "interfaces/auth.py": {
        "xiaowei_agent.config",
        "xiaowei_agent.contracts",
        "xiaowei_agent.interfaces",
        "xiaowei_agent.trace",
    },
    "interfaces/body_limit.py": {"xiaowei_agent.interfaces"},
    "interfaces/cli.py": {
        "xiaowei_agent.contracts",
        "xiaowei_agent.interfaces",
    },
    "interfaces/http_models.py": {
        "xiaowei_agent.contracts",
        "xiaowei_agent.interfaces",
    },
    "interfaces/local_stack.py": {
        "xiaowei_agent.application",
        "xiaowei_agent.capabilities",
        "xiaowei_agent.contracts",
        "xiaowei_agent.evidence",
        "xiaowei_agent.governance",
        "xiaowei_agent.interfaces",
        "xiaowei_agent.observability",
        "xiaowei_agent.persistence",
        "xiaowei_agent.planning",
        "xiaowei_agent.reflection",
        "xiaowei_agent.rendering",
        "xiaowei_agent.runners",
        "xiaowei_agent.tools",
        "xiaowei_agent.config",
        "xiaowei_agent.log",
        "xiaowei_agent.trace",
    },
    "interfaces/migrate.py": {
        "xiaowei_agent.config",
        "xiaowei_agent.interfaces",
        "xiaowei_agent.persistence",
    },
    "interfaces/worker.py": {
        "xiaowei_agent.application",
        "xiaowei_agent.config",
        "xiaowei_agent.interfaces",
        "xiaowei_agent.log",
    },
}


def _existing_packages() -> set[str]:
    """``src/xiaowei_agent`` 下磁盘上真实存在的包（含 ``__init__.py`` 的目录）。"""
    return {child.name for child in _SRC.iterdir() if (child / "__init__.py").is_file()}


def _internal_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        if (module or "").startswith("xiaowei_agent"):
            found.add(".".join((module or "").split(".")[:2]))
    return found


@pytest.mark.parametrize("package", sorted(set(_ALLOWED_INTERNAL) - _FILE_SCOPED_PACKAGES))
def test_package_only_imports_allowed_internal_modules(package: str) -> None:
    allowed = _ALLOWED_INTERNAL[package] | _UNIVERSAL_LEAF
    offenders: list[tuple[str, str]] = []
    for path in (_SRC / package).rglob("*.py"):
        for module in _internal_imports(path):
            if module not in allowed:
                offenders.append((str(path.relative_to(_SRC)), module))
    assert not offenders, f"{package} 出现非法内部依赖: {offenders}"


@pytest.mark.parametrize("relative", sorted(_ALLOWED_INTERNAL_BY_FILE))
def test_interface_file_only_imports_its_explicit_allowlist(relative: str) -> None:
    path = _SRC / relative
    allowed = _ALLOWED_INTERNAL_BY_FILE[relative] | _UNIVERSAL_LEAF
    offenders = sorted(_internal_imports(path) - allowed)
    assert not offenders, f"{relative} 出现非法内部依赖: {offenders}"


def test_every_interface_file_is_registered_exactly_once() -> None:
    actual = {path.relative_to(_SRC).as_posix() for path in (_SRC / "interfaces").rglob("*.py")}
    assert set(_ALLOWED_INTERNAL_BY_FILE) == actual


def test_only_local_stack_can_import_the_tools_layer() -> None:
    broad = {
        relative
        for relative, allowed in _ALLOWED_INTERNAL_BY_FILE.items()
        if "xiaowei_agent.tools" in allowed
    }
    assert broad == {"interfaces/local_stack.py"}


def test_tools_recording_cannot_reverse_import_capabilities() -> None:
    path = _SRC / "tools/starrocks_recording.py"
    assert "xiaowei_agent.capabilities" not in _internal_imports(path)


def test_every_existing_package_is_registered() -> None:
    """磁盘上存在的包必须全部登记——这是不变量，任何时刻都成立。

    不在表里的包完全不被扫描：它会静默失去全部分层护栏，而所有现有测试依然全绿。
    这比"依赖写错"更隐蔽，因此覆盖完整性必须由机制保证。
    """
    missing = _existing_packages() - set(_ALLOWED_INTERNAL)
    assert not missing, f"以下包未登记进分层白名单，因此完全未被扫描：{sorted(missing)}"


def test_every_registered_package_exists() -> None:
    """反向：不允许预登记尚不存在的包。

    预登记会让"包建好了但没登记"在很长一段时间里无法被这条测试区分出来——
    那会把一条护栏削弱成一句注释。新包必须在**创建它的那个提交**里登记。
    """
    absent = set(_ALLOWED_INTERNAL) - _existing_packages()
    assert not absent, f"以下包已登记但不存在：{sorted(absent)}"


def test_contracts_never_depend_on_implementations() -> None:
    """契约反向依赖实现，会让替换实现牵动契约。"""
    banned = {
        "xiaowei_agent.runners",
        "xiaowei_agent.tools",
        "xiaowei_agent.persistence",
        "xiaowei_agent.capabilities",
        "xiaowei_agent.planning",
        "xiaowei_agent.governance",
        "xiaowei_agent.log",
        "xiaowei_agent.config",
        "xiaowei_agent.trace",
    }
    for path in (_SRC / "contracts").rglob("*.py"):
        assert not (_internal_imports(path) & banned), path


def test_redaction_is_a_leaf() -> None:
    assert not _internal_imports(_SRC / "redaction.py")


def test_redaction_stays_a_leaf() -> None:
    """``redaction`` 对所有层开放，代价是它自己不得有任何对内依赖。

    一旦它 import 了别的 xiaowei_agent 模块，上面的全局允许就会把环引进来。
    """
    offenders = [
        (str(path.relative_to(_SRC)), module)
        for path in [_SRC / "redaction.py"]
        for module in _internal_imports(path)
    ]
    assert not offenders, f"redaction 必须保持无对内依赖: {offenders}"
