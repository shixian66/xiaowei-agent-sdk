"""每个契约字段的字符串类型必须显式声明它属于哪一档。

**这条测试针对的是根因,不是某几个字段。** 四轮复审都没抓到 ``approval_ref:
str | None``,因为它在类型上"看起来对";真正的成因是:必填字段写 ``StrictStr``、
可选字段要写 ``StrictStr | None`` 这个联合,写的时候把别名丢掉不会有任何反馈。
逐个补字段治不了这个——下一个可选字段会以同样方式漏掉。

因此这里用 AST 扫描全部 ``Contract`` 子类,禁止裸 ``str`` 出现在字段标注里。
作者必须在三档里挑一个:``StrictStr``(标识符/引用)、``NonEmptyText``(本系统
生成的文本)、``FreeText``(外部不可信文本逐字捕获)。挑哪个是设计决定,而"没挑"
不再是一个可能的状态。

用 AST 而非文本扫描:文档字符串里出现 ``str`` 不该触发断言(参见
``test_runner_never_touches_a_gateway_or_adapter`` 的同类教训)。
"""

import ast
import pathlib

import pytest

pytestmark = pytest.mark.security

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"

# 三档别名 + 已有的窄类型。裸 ``str`` 不在其中,这正是本测试的要点。
APPROVED = frozenset({"StrictStr", "NonEmptyText", "FreeText", "TraceId", "Sha256Hex"})


def _contract_fields(source: str) -> list[tuple[str, str, set[str]]]:
    """返回 (类名, 字段名, 标注里出现的名字集合)。

    只收 ``Contract`` 子类;子类关系在同一文件内传递求闭包。
    """
    tree = ast.parse(source)
    kinds = {"Contract"}
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    for _ in range(len(classes) + 1):  # 传递闭包,继承链最长不超过类数
        for cls in classes:
            names = {b.id for b in cls.bases if isinstance(b, ast.Name)}
            if names & kinds:
                kinds.add(cls.name)

    out: list[tuple[str, str, set[str]]] = []
    for cls in classes:
        if not ({b.id for b in cls.bases if isinstance(b, ast.Name)} & kinds):
            continue
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                used = {n.id for n in ast.walk(stmt.annotation) if isinstance(n, ast.Name)}
                out.append((cls.name, stmt.target.id, used))
    return out


def _type_aliases() -> dict[str, set[str]]:
    """收集全部模块级类型别名 -> 它引用的名字。

    **必须跨模块收集**:``FrozenStrMap`` 定义在 base.py、用在 intent.py。只看
    字段标注会漏掉别名内部的裸 ``str`` —— ``FrozenStrMap = Mapping[StrictStr, str]``
    与 ``DetailValue = Annotated[str, Field(max_length=256)]`` 两处都是这样藏住的。
    """
    aliases: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for stmt in tree.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                target, value = stmt.target.id, stmt.value
            elif (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                target, value = stmt.targets[0].id, stmt.value
            else:
                continue
            # 只把"看起来是类型表达式"的赋值当别名:Subscript(Mapping[...]、
            # Annotated[...])、BinOp(联合)、裸 Name。object()、常量等排除。
            if not isinstance(value, ast.Subscript | ast.BinOp | ast.Name):
                continue
            aliases[target] = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
    return aliases


def _expand(names: set[str], aliases: dict[str, set[str]]) -> set[str]:
    """把标注里的名字沿别名表展开。

    **在已批准的别名处停止展开**:``StrictStr = Annotated[str, ...]`` 本身就以
    ``str`` 收尾,继续展开会让每个字段都"含裸 str"。已批准别名正是合法包装
    ``str`` 的叶子。
    """
    seen: set[str] = set()
    queue = list(names)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in APPROVED:
            continue
        queue.extend(aliases.get(name, ()))
    return seen


def test_no_contract_field_resolves_to_bare_str() -> None:
    aliases = _type_aliases()
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for cls, field, used in _contract_fields(path.read_text(encoding="utf-8")):
            if "str" in _expand(used, aliases):
                offenders.append(f"{path.relative_to(SRC)}::{cls}.{field}")
    assert offenders == [], (
        "以下字段(含经别名间接引用)用了裸 str,必须显式选择 "
        "StrictStr / NonEmptyText / FreeText:\n" + "\n".join(offenders)
    )


def test_alias_expansion_is_load_bearing() -> None:
    """检测器自测:别名内部的裸 str 必须被展开抓到,已批准别名不得被误报。"""
    aliases = {
        "Leaky": {"Mapping", "StrictStr", "str"},
        "Nested": {"Leaky"},
        "Clean": {"Mapping", "StrictStr"},
    }
    assert "str" in _expand({"Leaky"}, aliases)
    assert "str" in _expand({"Nested"}, aliases)  # 传递展开
    assert "str" not in _expand({"Clean"}, aliases)
    assert "str" not in _expand({"StrictStr"}, aliases)  # 已批准别名处停止


def test_every_contract_string_field_uses_an_approved_alias() -> None:
    """反面覆盖:不只是"没有裸 str",还必须确实用了三档之一。

    单测"没有裸 str"会被 ``Any``、``object`` 之类绕过。
    """
    seen: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        for _cls, _field, used in _contract_fields(path.read_text(encoding="utf-8")):
            seen |= used & APPROVED
    # 三档都必须真的有人在用,否则说明分档只是摆设
    assert {"StrictStr", "NonEmptyText", "FreeText"} <= seen


@pytest.mark.parametrize(
    ("annotation", "flagged"),
    [
        ("str", True),
        ("str | None", True),
        ("tuple[str, ...]", True),
        ("StrictStr", False),
        ("StrictStr | None", False),
        ("tuple[NonEmptyText, ...]", False),
        ("FreeText", False),
    ],
)
def test_detector_itself_catches_bare_str(annotation: str, flagged: bool) -> None:
    """检测器自测。

    一个恒为空的 offenders 列表同样能让上面两条断言通过;必须先证明检测器真的会
    对裸 str 报警,前面的绿色才有意义。
    """
    source = f"class Probe(Contract):\n    field: {annotation}\n"
    fields = _contract_fields(source)
    assert len(fields) == 1
    assert ("str" in fields[0][2]) is flagged


def test_detector_ignores_str_in_docstrings_and_non_contract_classes() -> None:
    """两条反例:文本扫描会误报的位置,AST 扫描必须放行。"""
    with_docstring = (
        'class Probe(Contract):\n    """这里提到 str 只是散文,不是标注。"""\n    field: StrictStr\n'
    )
    assert _contract_fields(with_docstring) == [("Probe", "field", {"StrictStr"})]

    not_a_contract = "class Helper:\n    field: str\n"
    assert _contract_fields(not_a_contract) == []


def test_every_module_under_tests_security_declares_the_marker() -> None:
    """``tests/security/`` 下漏掉 ``pytestmark`` 的模块不会进 ``-m security`` gate。

    这是无声失败:文件放在安全目录里、``python -m pytest -q`` 也跑,唯独独立的
    安全 CI gate 不覆盖它——本轮新增的两个文件就是这样漏的(``-m security``
    仍停在 390,只有 deselected 数变了)。
    """
    root = pathlib.Path(__file__).resolve().parent
    missing = [p.name for p in sorted(root.glob("test_*.py")) if not _declares_marker(p)]
    assert missing == [], f"以下安全测试模块未声明 security marker: {missing}"


def _declares_marker(path: pathlib.Path) -> bool:
    """AST 判定模块是否声明了 ``pytestmark = pytest.mark.security``。

    **不能用文本扫描**:docstring 或注释里出现同一句话就能骗过它——本轮我已经
    因为文本扫描栽过两次(``test_runner_never_touches_a_gateway_or_adapter``、
    ``test_fake_is_not_exported_from_its_package_init``),这里是第三次,同一个根因。

    同时接受 ``pytestmark = pytest.mark.security`` 与列表形式
    ``pytestmark = [pytest.mark.security, ...]``。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets):
            continue
        candidates = (
            stmt.value.elts if isinstance(stmt.value, ast.List | ast.Tuple) else [stmt.value]
        )
        for item in candidates:
            if (
                isinstance(item, ast.Attribute)
                and item.attr == "security"
                and isinstance(item.value, ast.Attribute)
                and item.value.attr == "mark"
            ):
                return True
    return False


def test_marker_detector_is_not_fooled_by_text(tmp_path: pathlib.Path) -> None:
    """反例:只在 docstring 里写下同一句话,不得算作声明。"""
    faker = tmp_path / "test_faker.py"
    faker.write_text('"""pytestmark = pytest.mark.security"""\n', encoding="utf-8")
    assert not _declares_marker(faker)

    real = tmp_path / "test_real.py"
    real.write_text("import pytest\n\npytestmark = pytest.mark.security\n", encoding="utf-8")
    assert _declares_marker(real)

    listed = tmp_path / "test_listed.py"
    listed.write_text(
        "import pytest\n\npytestmark = [pytest.mark.security, pytest.mark.slow]\n",
        encoding="utf-8",
    )
    assert _declares_marker(listed)
