"""模块边界不得被"类型擦除 + 静音"绕过。

M3 深档验收发现的阻断项：``XiaoweiRuntime`` 把 runner 标成 ``object``，再用
``type: ignore[attr-defined]`` 调它的真实方法。后果不是"类型标注不好看"——是
**Runtime → Runner 这条边在类型层完全没有契约**，而 ``mypy src`` 依然全绿：
契约与实现的矛盾被静音掉了，没有任何 gate 会响。

同一根因在应用层还有两种表现形式，一并封死：

1. ``type: ignore`` —— 把已知的类型矛盾按下不表。
2. ``getattr(obj, "attr", default)`` —— 属性不存在时不报错，而是取一个默认值。
   ``_terminal_status`` 曾写成 ``getattr(outcome, "status", INDETERMINATE)``：
   一旦 outcome 形状变了，任务会静默变成 indeterminate 而不是失败。这是 fail-soft，
   与"任何无法确认的外部结果都进入 indeterminate、不能伪装成功"是两回事——后者是
   对**外部结果**的判定，前者是对**自己人契约**的猜测。
"""

import ast
import io
import tokenize
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
_SRC = _SRC_ROOT / "xiaowei_agent"


def _type_ignore_comment_lines(source: str) -> list[int]:
    """真实注释里的 ``type: ignore``。

    **必须走 tokenize 而不是文本扫描**：``runners/runner.py`` 的 docstring 里就写着
    这个字符串（用来说明为什么不许这么写），文本扫描会被那段说明自己触发，断言的
    就不再是代码行为。
    """
    lines: list[int] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type is tokenize.COMMENT and "type: ignore" in token.string:
            lines.append(token.start[0])
    return lines


def test_no_module_silences_a_type_error() -> None:
    offenders: list[str] = []
    # 与架构无关的类型纪律：扫描 src/ 下的全部包，包括新包 xiaowei。
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        for line in _type_ignore_comment_lines(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(_SRC_ROOT)}:{line}")
    assert not offenders, offenders


def test_the_type_ignore_detector_ignores_docstrings_and_catches_comments() -> None:
    """检测器自身必须先被证明有效。"""
    assert _type_ignore_comment_lines('"""说明：不许写 type: ignore。"""\n') == []
    assert _type_ignore_comment_lines("x = 1  # type: ignore[arg-type]\n") == [1]


def _getattr_with_default(tree: ast.Module) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) == 3
    ]


def test_the_application_layer_does_not_guess_collaborator_shapes() -> None:
    """应用层不得用 ``getattr`` 兜底读协作者属性。

    协作者的形状由 Protocol 与 Contract 规定；读不到就是契约破了，必须炸，不能
    悄悄取一个默认值继续往下走。
    """
    offenders: list[str] = []
    for path in sorted((_SRC / "application").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [f"{path.name}:{line}" for line in _getattr_with_default(tree)]
    assert not offenders, offenders


def test_the_getattr_detector_catches_the_pattern_it_bans() -> None:
    tree = ast.parse('s = getattr(outcome, "status", None)\n')
    assert _getattr_with_default(tree) == [1]
    assert _getattr_with_default(ast.parse('s = getattr(outcome, "status")\n')) == []


def _init_annotation(tree: ast.Module, cls: str, param: str) -> str | None:
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == cls):
            continue
        for item in node.body:
            if not (
                isinstance(item, ast.FunctionDef) and item.name == "__init__"
            ):
                continue
            args = item.args
            for arg in (*args.args, *args.kwonlyargs):
                if arg.arg == param and arg.annotation is not None:
                    return ast.unparse(arg.annotation)
    return None


def test_the_runtime_declares_its_runner_by_protocol() -> None:
    """Runtime 持有的 runner 必须是 ``WorkflowRunner``，不是 ``object`` / ``Any``。

    这是上面那条阻断项的正面断言：禁掉 ``type: ignore`` 只堵住了静音手段，还要钉住
    这条边**声明了契约**——否则把参数标成 ``Any`` 同样能全绿通过。
    """
    tree = ast.parse((_SRC / "application" / "runtime.py").read_text(encoding="utf-8"))
    assert _init_annotation(tree, "XiaoweiRuntime", "runner") == "WorkflowRunner"


def test_the_annotation_reader_distinguishes_object_from_a_protocol() -> None:
    source = (
        "class C:\n"
        "    def __init__(self, *, runner: object, other: WorkflowRunner) -> None: ...\n"
    )
    tree = ast.parse(source)
    assert _init_annotation(tree, "C", "runner") == "object"
    assert _init_annotation(tree, "C", "other") == "WorkflowRunner"
