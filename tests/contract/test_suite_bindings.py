"""共享套件的绑定完整性。

抽出共享套件解决了"两个实现各自跑偏"，但换来四条**新的**静默失效路径。它们的共同
形态是"看起来写了测试，实际一次都没跑"，而全部现有用例照样全绿：

1. **用例漏进分组**：函数写在套件里，但没加进任何 ``*_CASES`` 元组。
2. **绑定漏挂分组 / 局部覆盖**：某一组只接了内存实现，忘了接 PostgreSQL。
3. **元测试不知道新绑定**：加了绑定模块却没登记，护栏本身被绕过。
4. **绑定挂错分组**：挂了个截断的元组，少几条谁也看不出来。

第 3 条用 AST 扫描而不是靠人记得登记，理由与
``test_module_layering.py::test_every_existing_package_is_registered`` 相同：覆盖
完整性必须由机制保证，否则漏登记与"检查通过"无法区分。

三个套件（TaskStore / PlanStore / EvidenceLedger）共用这一套检查。**登记表是按套件
分层的**，因为"每一组必须被同一批实现绑定"只在**同一个套件内部**成立：TaskStore 有
memory 与 postgres 两种，将来若某个套件只有一种实现，跨套件比较会误报。
"""

import ast
import importlib
from pathlib import Path
from types import ModuleType

from tests.suites import evidence_ledger as evidence_suite
from tests.suites import plan_store as plan_suite
from tests.suites import task_store as task_suite

_TESTS_ROOT = Path(__file__).resolve().parents[1]

_SUITES: dict[str, ModuleType] = {
    "task_store": task_suite,
    "plan_store": plan_suite,
    "evidence_ledger": evidence_suite,
}

# 绑定登记表：模块路径 → (套件, 分组, 实现种类)。
#
# T5 为 TaskStore 的三组接上 postgres 绑定，T7 为另外两个套件接上。自此
# ``test_every_group_is_bound_by_the_same_kinds`` 与
# ``test_binding_exposes_exactly_its_group`` 不再是平凡真，两条都在 T5 复查过确实
# 会因缺绑定而转红。
_BINDINGS: dict[str, tuple[str, str, str]] = {
    "tests.contract.test_task_store_contract": ("task_store", "contract", "memory"),
    "tests.security.test_lease_fencing": ("task_store", "lease_fencing", "memory"),
    "tests.security.test_terminal_protection": (
        "task_store",
        "terminal_protection",
        "memory",
    ),
    "tests.integration.test_task_store_contract_postgres": (
        "task_store",
        "contract",
        "postgres",
    ),
    "tests.integration.test_lease_fencing_postgres": (
        "task_store",
        "lease_fencing",
        "postgres",
    ),
    "tests.integration.test_terminal_protection_postgres": (
        "task_store",
        "terminal_protection",
        "postgres",
    ),
    "tests.contract.test_dispatch_and_attempts": (
        "task_store",
        "dispatch_attempt",
        "memory",
    ),
    "tests.integration.test_dispatch_and_attempts_postgres": (
        "task_store",
        "dispatch_attempt",
        "postgres",
    ),
    "tests.contract.test_step_execution_store": (
        "task_store",
        "step_execution",
        "memory",
    ),
    "tests.integration.test_step_execution_postgres": (
        "task_store",
        "step_execution",
        "postgres",
    ),
    "tests.contract.test_plan_store": ("plan_store", "plan_store", "memory"),
    "tests.integration.test_plan_store_postgres": ("plan_store", "plan_store", "postgres"),
    "tests.contract.test_evidence_ledger": (
        "evidence_ledger",
        "evidence_ledger",
        "memory",
    ),
    "tests.integration.test_evidence_ledger_postgres": (
        "evidence_ledger",
        "evidence_ledger",
        "postgres",
    ),
}


def _suite_case_names(suite: ModuleType) -> set[str]:
    return {
        name
        for name, value in vars(suite).items()
        if name.startswith("test_") and callable(value) and value.__module__ == suite.__name__
    }


def _modules_calling_bind() -> set[str]:
    """AST 扫描：``tests/`` 下所有调用了 ``bind(...)`` 的模块，返回 import 路径。"""
    found: set[str] = set()
    for path in _TESTS_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "bind"
            ):
                relative = path.relative_to(_TESTS_ROOT.parent).with_suffix("")
                found.add(".".join(relative.parts))
    return found


def test_every_suite_case_belongs_to_exactly_one_group() -> None:
    """漏进分组的用例在**所有**绑定里都收不到，且不会有任何测试失败。"""
    for name, suite in _SUITES.items():
        grouped = [case.__name__ for group in suite.ALL_GROUPS.values() for case in group]
        assert len(grouped) == len(set(grouped)), f"{name}: 同一用例被登记进多个分组"
        assert set(grouped) == _suite_case_names(suite), name


def test_every_module_that_binds_is_registered() -> None:
    """漏登记的绑定完全不被本文件检查——护栏被绕过，而一切照常全绿。

    套件模块自己也 import 了 ``bind``（``plan_store`` / ``evidence_ledger`` 复用
    ``task_store`` 的那个），但它们不**调用**它，因此不会出现在扫描结果里。
    """
    assert _modules_calling_bind() == set(_BINDINGS)


def test_every_group_has_at_least_one_binding() -> None:
    bound = {(suite, group) for suite, group, _ in _BINDINGS.values()}
    declared = {(name, group) for name, suite in _SUITES.items() for group in suite.ALL_GROUPS}
    assert bound == declared


def test_every_group_is_bound_by_the_same_kinds() -> None:
    """**同一套件内**每一组必须被同一批实现绑定。

    否则会出现"两组绑了 PostgreSQL、第三组忘了"这种局部覆盖：判定标准 1 说的是
    *同一套*用例在两个实现上逐条通过，少一组就不成立，而没有任何用例会失败。
    """
    for name, suite in _SUITES.items():
        kinds: dict[str, set[str]] = {group: set() for group in suite.ALL_GROUPS}
        for suite_name, group, kind in _BINDINGS.values():
            if suite_name == name:
                kinds[group].add(kind)
        assert len(set(map(frozenset, kinds.values()))) == 1, f"{name}: {kinds}"


def test_binding_exposes_exactly_its_group() -> None:
    """每个绑定模块暴露的套件用例，恰为它登记的那一组。

    比较范围限定在该套件的用例名内，因此绑定模块自己的本地用例（如
    ``test_plan_store.py`` 里那条与存储无关的契约断言）不受影响。

    由此推出判定标准 1 要的东西：同一组的任意两个绑定都等于该组，因而彼此相等。
    写成"逐个绑定对齐分组"而不是"两两比较"，是为了让失败信息直接指出**哪个绑定少了
    哪条**，而不是只说"两边不一样"。
    """
    for module_path, (suite_name, group, _) in _BINDINGS.items():
        suite = _SUITES[suite_name]
        universe = _suite_case_names(suite)
        module = importlib.import_module(module_path)
        exposed = {name for name in vars(module) if name in universe}
        expected = {case.__name__ for case in suite.ALL_GROUPS[group]}
        assert exposed == expected, f"{module_path} 暴露的套件用例与分组 {group} 不符"
