"""契约枚举的成员引用必须真实存在 —— 在**默认路径**上就要红。

`StageOutcome.DENIED` 与 `StageOutcome.ERROR` 这两个成员从来不存在（闭集是
``OK / REJECTED / FAILED / SKIPPED``），却在 `tests/integration/` 里躺了整整一轮验收。
两层原因叠加才让它活下来：

1. ADR-008 的 ``mypy src`` **不覆盖 ``tests``**——那是刻意的，测试里大量使用 ``Any``
   与动态 fixture，把它们纳入类型检查会淹没在噪声里；
2. integration 用例在没有 PostgreSQL 的机器上全部 skipped，**运行时也碰不到那一行**。

于是这类错误既没有静态检查也没有运行时检查。它不是"某个用例写错了"，是**一整类
只在 integration 里出现的代码没有任何验证**：属性名拼错、成员被删除后引用没跟着改、
把别的枚举的成员写到这个枚举上，全都长这样。

因此这里按 AST 扫源码：枚举清单**从 ``xiaowei_agent.contracts`` 派生**，不硬编码——
新增枚举自动纳入，理由与 ``test_row_mapping.py`` 从 ``ALL_TABLES`` 扫 JSONB 列相同。

判定写成纯函数，因此可以在**不真的往仓库里放一处错误引用**的前提下测试它自己。
"""

import ast
from collections.abc import Mapping
from enum import Enum
from pathlib import Path

import xiaowei_agent.contracts as contracts

_ROOT = Path(__file__).resolve().parents[2]
_TREES = (_ROOT / "src", _ROOT / "tests")
_PRUNED = {"__pycache__"}


def contract_enums() -> Mapping[str, type[Enum]]:
    """``xiaowei_agent.contracts`` 导出的全部枚举，按名字索引。"""
    return {
        name: obj
        for name, obj in vars(contracts).items()
        if isinstance(obj, type) and issubclass(obj, Enum)
    }


def unknown_enum_members(source: str, *, enums: Mapping[str, type[Enum]]) -> list[str]:
    """``<Enum>.<NAME>`` 形态的引用里，哪些 ``NAME`` 在该枚举上不存在。

    只看 ``value`` 是裸名字的属性访问：``payload.outcome.value`` 的外层 ``.value``
    挂在一个 ``Attribute`` 上而不是 ``Name`` 上，因此天然不参与判定。
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        enum_cls = enums.get(node.value.id)
        if enum_cls is not None and not hasattr(enum_cls, node.attr):
            offenders.append(f"{node.value.id}.{node.attr}")
    return offenders


def _python_files() -> list[Path]:
    found: list[Path] = []
    for tree in _TREES:
        stack = [tree]
        while stack:
            for child in stack.pop().iterdir():
                if child.is_dir():
                    if child.name not in _PRUNED:
                        stack.append(child)
                elif child.suffix == ".py":
                    found.append(child)
    return sorted(found)


def _references(source: str, *, enums: Mapping[str, type[Enum]]) -> int:
    return sum(
        1
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in enums
    )


def test_no_source_file_references_a_nonexistent_enum_member() -> None:
    enums = contract_enums()
    offenders: dict[str, list[str]] = {}
    for path in _python_files():
        bad = unknown_enum_members(path.read_text(encoding="utf-8"), enums=enums)
        if bad:
            offenders[str(path.relative_to(_ROOT))] = bad
    assert not offenders, f"引用了不存在的枚举成员：{offenders}"


def test_the_scan_covers_both_trees_and_actually_sees_references() -> None:
    """反空洞：扫不到文件或扫不到引用时，上一条平凡通过而什么也没证明。

    分树断言是必要的：只扫到 ``src`` 同样能让上一条全绿，而**出问题的那一类文件全在
    ``tests`` 里**——那正是没有类型检查覆盖的那一半。
    """
    enums = contract_enums()
    assert len(enums) >= 5, sorted(enums)
    per_tree = {
        tree.name: sum(
            _references(path.read_text(encoding="utf-8"), enums=enums)
            for path in _python_files()
            if tree in path.parents
        )
        for tree in _TREES
    }
    assert per_tree["src"] > 0, per_tree
    assert per_tree["tests"] > 0, per_tree


def test_the_check_catches_the_exact_members_that_shipped() -> None:
    """反例：用真实出过事的那两个名字确认这条检查有分辨力。"""
    enums = contract_enums()
    assert unknown_enum_members(
        "x = StageOutcome.DENIED\ny = StageOutcome.ERROR\n", enums=enums
    ) == ["StageOutcome.DENIED", "StageOutcome.ERROR"]


def test_the_check_does_not_flag_real_members_or_unrelated_names() -> None:
    """误报会让人把整条检查关掉，因此正例同样要钉。"""
    enums = contract_enums()
    assert (
        unknown_enum_members(
            "a = StageOutcome.REJECTED\n"
            "b = StageOutcome.FAILED.value\n"
            "c = payload.outcome.value\n"
            "d = SomeOtherClass.WHATEVER\n",
            enums=enums,
        )
        == []
    )
