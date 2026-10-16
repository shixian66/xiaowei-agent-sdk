"""拒绝路径不得把外部/调用输入拼进异常消息。

这是**全项目不变量**，不是某一层的实现细节。上一轮我在契约层把它当作 pydantic
的问题解决了（``hide_input_in_errors`` + 映射键改序号定位），却没有把它提升为
不变量——于是 Gateway、canonical、plan 里手写的 ``raise`` 全部漏网：

    adapter not registered: password=hunter2
    canonical key collision after NFC: 'é-password=hunter2'

逐个改这些消息治不了根：下一个新增的 ``raise`` 会以同样方式回显。因此这里把
"``raise`` 里允许插入什么"变成一张显式白名单，新增任何其他插值都必须报红，
作者被迫做一次决定（改成序号/错误码，或把理由写进白名单）。

与 ``PLAN_FIELD_TO_HASH_KEY`` 之类的映射表同一个模式：用机制保证覆盖完整，
而不是靠每次记得。
"""

import ast
import pathlib

import pytest

pytestmark = pytest.mark.security

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"

SAFE_INTERPOLATIONS: frozenset[str] = frozenset(
    {
        # 这里曾经放着 ``type(value).__name__`` 与 ``type(response).__name__``，
        # 理由写的是"类型名是类的身份，不是数据"。**那个判断是错的**：
        # ``type(name, bases, dict)`` 能在运行时造出任意类名，所以跨信任边界对象
        # 的类名是调用方可控数据。canonical_json 的输入来自计划与外部载荷，
        # adapter 的返回值来自 adapter——两处都已实测泄漏 canary。
        #
        # 两个条目已删除，对应站点改为不回显类型名。留下这段注释而不是静默删掉，
        # 是因为"看起来像常量的东西其实是数据"这类判断错误会重复发生。
        # --- 序号与模块常量：不含任何调用方取值 ---
        "index",
        "label",
        "_DETAIL_VALUE_MAX",
        # --- 枚举成员：取值域是代码里的闭集 ---
        "self.kind.value",
        "self.status",
        "approval.state.value",
        "result.rejection",
        # --- 闭集：来自代码定义的必填操作数名，不是外部输入 ---
        "sorted(required)",
        # --- 已经过安全投影 ---
        # config.py 的 detail 来自 redaction.safe_error_details，只含 loc 与 type。
        "detail",
        # --- SqlGuardRejection 枚举成员：取值域是代码里的闭集 ---
        # 被检 SQL 是最可能携带外部数据的东西，一律不进消息；由
        # test_rejection_message_never_echoes_the_sql 与
        # test_unparsable_sql_is_structured_not_a_raw_parse_error 反向承重。
        "SqlGuardRejection.AMBIGUOUS_CHARACTER",
        "SqlGuardRejection.COLUMN_NOT_ALLOWED",
        "SqlGuardRejection.COMMENT_PRESENT",
        "SqlGuardRejection.FORBIDDEN_NODE",
        "SqlGuardRejection.LIMIT_EXCEEDED",
        "SqlGuardRejection.LIMIT_MISSING",
        "SqlGuardRejection.MULTIPLE_STATEMENTS",
        "SqlGuardRejection.NON_SELECT",
        "SqlGuardRejection.RECOMPILE_MISMATCH",
        "SqlGuardRejection.STAR_NOT_ALLOWED",
        "SqlGuardRejection.TABLE_NOT_ALLOWED",
        "SqlGuardRejection.UNKNOWN_DIALECT",
        "SqlGuardRejection.UNKNOWN_TEMPLATE",
        "SqlGuardRejection.UNPARSABLE",
        "SqlGuardRejection.WINDOW_MISMATCH",
        "SqlGuardRejection.WINDOW_TOO_WIDE",
        "SqlGuardRejection.WINDOW_UNBOUNDED",
        # --- TargetRejection 枚举成员：取值域是代码里的闭集 ---
        # 被拒的环境标识来自调用方，不进消息；由
        # test_rejection_message_never_echoes_the_environment_id 反向承重。
        "TargetRejection.UNKNOWN_ENVIRONMENT",
        "TargetRejection.EMPTY_ENVIRONMENT_DIRECTORY",
        # --- BindingRejection 枚举成员：取值域是代码里的闭集 ---
        "BindingRejection.POLICY_REVISION_DRIFT",
        "BindingRejection.APPROVAL_EXPIRED",
        "BindingRejection.APPROVAL_NOT_GRANTED",
        "BindingRejection.PLAN_DRIFT",
        "BindingRejection.TARGET_DRIFT",
        # --- 显式豁免（环境变量名，非取值）---
        # 未知配置变量的**名字**是 fail-fast 诊断的全部价值所在，而 secret 存在
        # 取值里、不在名字里；环境变量名也不可能包含 "="。这是一次有意识的决定，
        # 不是遗漏——因此写在这里而不是被静默放过。
        "unknown",
    }
)


def _raise_dynamic_values() -> list[tuple[str, str]]:
    """``raise`` 语句里出现的**全部**动态取值 (位置, 源文本)。

    早先版本只扫 f-string / ``%`` / ``.format()``，于是漏掉了没有插值语法的形式::

        raise TaskNotFoundError(task_id)      # task_id 成为 str(exc) 的全部内容

    这与 f-string 回显是同一条缺陷，只是没有拼接语法。**两个覆盖面不同的扫描器
    本身就是缝隙**，所以这里合并成一个：既看字符串拼接，也看传给异常构造器的
    每一个非常量实参。
    """
    found: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            loc = f"{path.relative_to(SRC)}:{node.lineno}"
            for sub in ast.walk(node):
                if isinstance(sub, ast.JoinedStr):
                    found.extend(
                        (loc, ast.unparse(part.value))
                        for part in sub.values
                        if isinstance(part, ast.FormattedValue)
                    )
                elif isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mod):
                    found.append((loc, "%-format"))
                elif (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "format"
                ):
                    found.append((loc, ".format()"))
            if isinstance(node.exc, ast.Call):
                # 只查**位置**参数。Python 默认的 ``BaseException.__str__`` 渲染的是
                # ``args``，而 ``args`` 只收位置参数——``raise X(task_id)`` 因此让
                # task_id 成为错误文本的全部内容。关键字参数进不了 ``args``，除非
                # 异常类自己把它塞进去；那属于运行时性质，由
                # ``test_structured_errors_keep_a_constant_message`` 断言。
                found.extend(
                    (loc, ast.unparse(arg))
                    for arg in node.exc.args
                    # 常量与 f-string 已由上面的循环处理，这里只补"裸传一个值"。
                    if not isinstance(arg, ast.Constant | ast.JoinedStr)
                )
    return found


def test_no_raise_carries_unvetted_values() -> None:
    offenders = [
        f"{loc}  ->  {expr}"
        for loc, expr in _raise_dynamic_values()
        if expr not in SAFE_INTERPOLATIONS
    ]
    assert offenders == [], (
        "以下 raise 消息插入了未经审阅的表达式。拒绝路径不得回显外部/调用输入：\n"
        + "\n".join(offenders)
        + "\n改用序号或错误码定位；确属安全时把表达式加进 SAFE_INTERPOLATIONS 并写明理由。"
    )


def test_allowlist_has_no_dead_entries() -> None:
    """白名单不得留下已不存在的条目。

    死条目会悄悄放宽护栏：一个被删掉的表达式留在名单里，下次有人写出同名表达式
    时就会被静默放行。与 hash 映射表必须与 model_fields 相等是同一条要求。
    """
    live = {expr for _loc, expr in _raise_dynamic_values()}
    assert SAFE_INTERPOLATIONS - live == set()


def test_detector_catches_each_interpolation_form() -> None:
    """检测器自测：三种字符串拼接形式都要抓到，普通字符串不得误报。"""
    import tempfile

    cases = {
        'raise ValueError(f"bad: {secret}")': "secret",
        'raise ValueError("bad: %s" % secret)': "%-format",
        'raise ValueError("bad: {}".format(secret))': ".format()",
    }
    for source, expected in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            probe = pathlib.Path(tmp) / "probe.py"
            probe.write_text(source + chr(10), encoding="utf-8")
            tree = ast.parse(probe.read_text(encoding="utf-8"))
            exprs: list[str] = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Raise) or node.exc is None:
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.JoinedStr):
                        exprs += [
                            ast.unparse(part.value)
                            for part in sub.values
                            if isinstance(part, ast.FormattedValue)
                        ]
                    elif isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mod):
                        exprs.append("%-format")
                    elif (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "format"
                    ):
                        exprs.append(".format()")
            assert expected in exprs, source


def test_constant_messages_are_not_flagged() -> None:
    """反例：纯常量消息不得被算作插值，否则护栏会逼人放弃可读的错误文本。"""
    tree = ast.parse('raise ValueError("plan exceeds max_steps budget")' + chr(10))
    joined = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]
    assert joined == []


# --- 运行时 canary：静态扫描保证不新增，这里保证确实不泄漏 -------------------

import asyncio  # noqa: E402
import unicodedata  # noqa: E402

from tests.conftest import make_certificate  # noqa: E402
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TOOL_CALL  # noqa: E402

from xiaowei_agent.contracts import RequestContext  # noqa: E402
from xiaowei_agent.planning import canonical_json  # noqa: E402
from xiaowei_agent.tools.gateway import DeterministicToolGateway  # noqa: E402

CANARY = "synthetic-canary-password=hunter2"


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="0" * 32,
        policy_revision="policy-2026-09-01",
    )


def test_unregistered_adapter_message_carries_no_gateway_name() -> None:
    call = FIXTURE_TOOL_CALL.model_copy(update={"gateway": CANARY})
    gateway = DeterministicToolGateway(adapters={"registered": object()})
    with pytest.raises(LookupError) as caught:
        asyncio.run(gateway.invoke(call, context=_context(), admission=make_certificate(call)))
    assert CANARY not in str(caught.value)


def test_canonical_key_collision_message_carries_no_key() -> None:
    composed = unicodedata.normalize("NFC", f"é-{CANARY}")
    decomposed = unicodedata.normalize("NFD", f"é-{CANARY}")
    assert composed != decomposed  # 前提：两者确实是不同的码点序列
    with pytest.raises(ValueError) as caught:
        canonical_json({composed: 1, decomposed: 2})
    assert CANARY not in str(caught.value)


def test_plan_validator_messages_carry_no_step_id() -> None:
    """重复 step_id 与未知依赖两条路径都不得回显 id。"""
    from pydantic import ValidationError

    poisoned = FIXTURE_PLAN.steps[0].model_copy(update={"step_id": CANARY})
    with pytest.raises(ValidationError) as caught:
        FIXTURE_PLAN.model_copy(update={"steps": (poisoned, poisoned)})
    assert CANARY not in str(caught.value)

    dangling = FIXTURE_PLAN.steps[0].model_copy(update={"depends_on": (CANARY,)})
    with pytest.raises(ValidationError) as caught:
        FIXTURE_PLAN.model_copy(update={"steps": (dangling,)})
    assert CANARY not in str(caught.value)


def test_structured_errors_keep_a_constant_message() -> None:
    """携带 ``task_id`` 的异常，其 ``str``/``repr`` 必须与 task_id 无关。

    静态扫描只能保证 task_id 不是位置参数；"关键字参数不会进 ``args``"是运行时
    性质，必须在这里直接断言——否则一个把 task_id 转存进 ``args`` 的
    ``__init__`` 会让静态规则给出虚假保证。
    """
    from xiaowei_agent.persistence.store import TaskNotFoundError
    from xiaowei_agent.runners.fake import TerminalOrLeasedTaskError

    for factory in (TaskNotFoundError, TerminalOrLeasedTaskError):
        exc = factory(task_id=CANARY)
        assert CANARY not in str(exc)
        assert CANARY not in repr(exc)
        assert CANARY not in repr(exc.args)
        # 反例配对：结构化字段仍然可读，否则这个改动等于丢掉诊断能力
        assert exc.task_id == CANARY


def test_detector_catches_a_bare_positional_value() -> None:
    """检测器自测：``raise X(value)`` 这种无插值语法的形式必须被抓到。

    早先只扫 f-string / % / .format() 的版本对它完全无感——两个覆盖面不同的
    扫描器本身就是缝隙。
    """
    tree = ast.parse("raise TaskNotFoundError(task_id)" + chr(10))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Raise))
    assert isinstance(node.exc, ast.Call)
    dynamic = [ast.unparse(a) for a in node.exc.args if not isinstance(a, ast.Constant)]
    assert dynamic == ["task_id"]


def test_detector_allows_keyword_carried_values() -> None:
    """反例：关键字参数不进 ``args``，不得被误报——否则规则会逼人把诊断值删掉。"""
    tree = ast.parse("raise TaskNotFoundError(task_id=task_id)" + chr(10))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Raise))
    assert isinstance(node.exc, ast.Call)
    assert [a for a in node.exc.args if not isinstance(a, ast.Constant)] == []
