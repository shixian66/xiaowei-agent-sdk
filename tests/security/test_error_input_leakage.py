"""拒绝路径不得把原始输入带出去。

契约层校验的正是外部文本、槽位、typed_args、trace detail 这类可能携带 secret
的值。默认的 ``ValidationError`` 会把被拒绝的原始输入回填进错误里,于是"被拒绝"
反而成了原文外泄的通道——校验越严,泄漏面越大。

用合成 canary 断言,不用任何真实凭证。
"""

import ast
import datetime as _dt
import pathlib

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    Contract,
    ExternalContent,
    ExternalSource,
    IntentDraft,
    IntentSource,
    PipelineStage,
    StageOutcome,
    TraceEvent,
)
from xiaowei_agent.redaction import safe_error_details

pytestmark = pytest.mark.security

# 合成 canary:形似 secret 但不是任何真实凭证。
CANARY = "synthetic-canary-AKIA0000EXAMPLE"
NOW = _dt.datetime(2026, 9, 2, tzinfo=_dt.UTC)
SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"


def _event(detail: dict[str, str]) -> TraceEvent:
    return TraceEvent(
        event_id="e1",
        trace_id="0" * 32,
        task_id=None,
        stage=PipelineStage.GATEWAY,
        outcome=StageOutcome.OK,
        occurred_at=NOW,
        capability_id=None,
        step_id=None,
        policy_revision=None,
        error=None,
        detail=detail,
    )


def _log_surfaces(exc: ValidationError) -> str:
    """会自动流向 traceback、日志与错误响应的那些表面。

    这三个是**被动**表面:不需要任何人主动调用就会被打印出来。``errors()`` 与
    ``json()`` 不在其中——它们必须被显式调用,由 egress 禁令单独管住
    (见 ``test_no_raw_validation_error_egress_outside_the_helper``)。
    """
    return str(exc) + repr(exc) + repr(exc.args)


@pytest.mark.parametrize(
    "construct",
    [
        pytest.param(
            lambda: ExternalContent(
                source=ExternalSource.TOOL, content="x", digest=CANARY, captured_at=NOW
            ),
            id="digest_shape",
        ),
        pytest.param(
            lambda: IntentDraft.model_validate(
                {
                    "intent": "q",
                    "slots": {"k": f"  {CANARY}  "},
                    "missing": (),
                    "confidence": 0.9,
                    "source": "llm",
                }
            ),
            id="slot_padding",
        ),
        pytest.param(lambda: _event({f"  {CANARY}  ": "v"}), id="detail_key_padded"),
    ],
)
def test_validation_error_never_renders_the_rejected_input(construct: object) -> None:
    with pytest.raises(ValidationError) as caught:
        construct()  # type: ignore[operator]
    surface = _log_surfaces(caught.value)
    assert CANARY not in surface
    # 前缀同样算泄漏:pydantic 对超长输入做截断,截断不是脱敏。
    assert "synthetic-canary" not in surface


def test_hide_input_does_not_cover_errors_or_json() -> None:
    """把 ``hide_input_in_errors`` 的**真实边界**钉死。

    这条配置只作用于 ``str(exc)``;``exc.errors()`` 与 ``exc.json()`` 仍然回填
    完整原始输入,没有任何模型配置能把它从异常对象上抹掉。因此"在基类开启隐藏"
    只是这条 P0 的一半,另一半必须是 egress 禁令 + ``safe_error_details``。

    如果这条转红,说明 pydantic 改变了行为,可以重新评估禁令是否还需要——而不是
    直接删掉断言。
    """
    with pytest.raises(ValidationError) as caught:
        ExternalContent(source=ExternalSource.TOOL, content="x", digest=CANARY, captured_at=NOW)
    exc = caught.value
    assert CANARY in repr(exc.errors()), "pydantic 行为已变,请重新评估 egress 禁令"
    assert CANARY in exc.json(), "pydantic 行为已变,请重新评估 egress 禁令"
    # 显式关掉才干净——这正是 safe_error_details 必须存在的原因
    assert CANARY not in repr(exc.errors(include_input=False))


def test_base_class_hides_input_so_new_contracts_inherit_it() -> None:
    """必须在基类上,否则下一个新增契约会以同样方式漏掉。"""
    assert Contract.model_config["hide_input_in_errors"] is True


def test_trace_detail_is_redacted_before_the_length_limit() -> None:
    """顺序反了,超长值会在脱敏前被拒绝,错误里带原文。"""
    long_secret = f"token={CANARY} " + "x" * 400
    # 脱敏后长度合法,因此应当被接受,且存下的是脱敏值
    assert _event({"k": long_secret}).detail["k"] == "token=***"


def test_length_limit_still_rejects_genuinely_long_values() -> None:
    """反例:先脱敏不等于放弃长度限制。"""
    with pytest.raises(ValidationError) as caught:
        _event({"k": "x" * 400})
    assert "256" in str(caught.value)


def test_safe_error_details_carries_no_values() -> None:
    with pytest.raises(ValidationError) as caught:
        ExternalContent(source=ExternalSource.TOOL, content="x", digest=CANARY, captured_at=NOW)
    details = safe_error_details(caught.value)
    assert details == ("digest: value_error",)
    assert CANARY not in repr(details)


def test_no_raw_validation_error_egress_outside_the_helper() -> None:
    """``.errors()`` 只允许出现在 ``safe_error_details`` 内部。

    基类的 ``hide_input_in_errors`` 关不住 ``errors()``,所以这条禁令是该 P0 的
    另一半。用 AST 而非文本扫描:文档里提到 ``errors()`` 不该触发断言。
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = {
            node
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef) and fn.name == "safe_error_details"
            for node in ast.walk(fn)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"errors", "json"}
                and node not in allowed
            ):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == [], f"这些位置直接调用了 .errors()/.json(): {offenders}"


# --- loc 泄漏：本轮新发现的第三条通道 ---------------------------------------


@pytest.mark.parametrize(
    "construct",
    [
        pytest.param(
            lambda: IntentDraft.model_validate(
                {
                    "intent": "q",
                    "slots": {f"  {CANARY}  ": "v"},
                    "missing": (),
                    "confidence": 0.9,
                    "source": "llm",
                }
            ),
            id="slots_key",
        ),
        pytest.param(lambda: _event({f"  {CANARY}  ": "v"}), id="detail_key"),
        pytest.param(
            lambda: IntentDraft.model_validate(
                {
                    "intent": "q",
                    "slots": {CANARY: b"bytes"},
                    "missing": (),
                    "confidence": 0.9,
                    "source": "llm",
                }
            ),
            id="slots_value_error_leaks_key_through_loc",
        ),
    ],
)
def test_map_keys_never_reach_the_error_location(construct: object) -> None:
    """映射字段的**键**不得经 ``loc`` 泄漏。

    ``hide_input_in_errors`` 隐藏的是 ``input``，对 ``loc`` 无效。写成
    ``Mapping[StrictStr, JsonScalar]`` 让 Pydantic 自己校验时，出错的那个键会被
    原样嵌进 ``loc``——``detail.  token=secret  .[key]``——于是它出现在
    ``str(exc)``、traceback，以及任何基于 ``loc`` 的"安全"投影里，
    ``safe_error_details`` 也不例外。

    映射的键恰恰全是外部文本：trace detail 的键由调用方拼装、slots 的键来自
    模型输出、typed_args 的键来自计划编译。

    第三个用例是**值**出错但键泄漏：证明修复覆盖的是整个映射校验路径，
    不是只把键的类型检查挪了个位置。
    """
    with pytest.raises(ValidationError) as caught:
        construct()  # type: ignore[operator]
    exc = caught.value
    assert CANARY not in str(exc)
    assert CANARY not in repr([item["loc"] for item in exc.errors(include_input=False)])
    assert CANARY not in repr(safe_error_details(exc))


def test_mapping_fields_stay_deeply_immutable_after_the_fix() -> None:
    """回归护栏：前置校验返回的 MappingProxyType 会被内层 Mapping 校验重建成
    普通 dict。只做前置校验时深不可变性会**静默**失效——没有任何报错，只是
    映射又变回可变的。必须在内层校验之后再冻结一次。
    """
    draft = IntentDraft(
        intent="q", slots={"k": "v"}, missing=(), confidence=0.9, source=IntentSource.MODEL
    )
    with pytest.raises(TypeError):
        draft.slots["k"] = "tampered"  # type: ignore[index]
    with pytest.raises(TypeError):
        _event({"k": "v"}).detail["k"] = "tampered"  # type: ignore[index]
