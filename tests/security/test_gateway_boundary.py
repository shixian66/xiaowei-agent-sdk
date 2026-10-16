"""ToolGateway 是数据面唯一工具入口，且 M0-M7 的 E1 调用次数恒为 0。"""

import ast
import asyncio
import datetime as dt
from pathlib import Path

import pytest
from tests.conftest import make_certificate

from xiaowei_agent.contracts import (
    AdapterStatus,
    AdmissionCertificate,
    EffectClass,
    ErrorCategory,
    ExternalContent,
    ExternalSource,
    PolicyDecision,
    RequestContext,
    RiskLevel,
    ToolCall,
    ToolCallStatus,
)
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.fake import RecordingToolAdapter
from xiaowei_agent.tools.gateway import _E1_EXECUTION_ENABLED, DeterministicToolGateway

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_AT = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)


def test_e1_execution_is_disabled() -> None:
    """ADR-007 D7：M0-M7 全程禁止 E1，含非生产环境。开闸必须改这一行并过评审。"""
    assert _E1_EXECUTION_ENABLED is False


async def test_side_effect_call_is_refused_and_adapter_is_never_called(
    gateway, ok_call, context, recording_adapter
) -> None:
    """ADR-007 D7 承重断言 1 与 4。"""
    cert = make_certificate(ok_call, effect_class=EffectClass.MUTATE_TARGET)
    with pytest.raises(PermissionError):
        await gateway.invoke(ok_call, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_admission_for_another_step_is_refused(
    gateway, ok_call, context, recording_adapter
) -> None:
    cert = make_certificate(ok_call, step_id="s99")
    with pytest.raises(PermissionError):
        await gateway.invoke(ok_call, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_admission_with_tampered_args_is_refused(
    gateway, ok_call, context, recording_adapter
) -> None:
    """拿合法凭证替换参数执行同一步骤，必须拒绝。

    凭证是对**已准入的那个 ToolCall** 的证明，不是对步骤身份的证明。
    """
    cert = make_certificate(ok_call)
    tampered = ok_call.model_copy(update={"typed_args": {"window_minutes": 1440}})
    with pytest.raises(PermissionError):
        await gateway.invoke(tampered, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_admission_with_tampered_timeout_is_refused(
    gateway, ok_call, context, recording_adapter
) -> None:
    cert = make_certificate(ok_call)
    tampered = ok_call.model_copy(update={"timeout_seconds": 300.0})
    with pytest.raises(PermissionError):
        await gateway.invoke(tampered, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_denied_policy_decision_is_refused(
    gateway, ok_call, context, recording_adapter
) -> None:
    denied = PolicyDecision(
        allow=False,
        reason_code="policy.denied",
        risk=RiskLevel.HIGH,
        policy_revision="policy-2026-09-01",
        obligations=(),
    )
    cert = make_certificate(ok_call, policy_decision=denied)
    with pytest.raises(PermissionError):
        await gateway.invoke(ok_call, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_unregistered_adapter_fails_closed(
    gateway, ok_call, context, recording_adapter
) -> None:
    call = ok_call.model_copy(update={"gateway": "never_registered"})
    cert = make_certificate(call)
    with pytest.raises(LookupError):
        await gateway.invoke(call, context=context, admission=cert)
    assert recording_adapter.call_count == 0


def test_gateway_requires_at_least_one_adapter() -> None:
    with pytest.raises(ValueError):
        DeterministicToolGateway(adapters={})


async def test_non_conforming_adapter_return_is_refused(ok_call, context, admission) -> None:
    """Protocol 是静态的；运行时必须自己 fail-closed。

    否则一个返回 dict 或鸭子类型对象的 adapter 能把未经契约约束的数据带进
    ToolResult——领域层拿到的"结果"再也不是 Gateway 归一化过的结果。
    """

    class _RogueAdapter:
        async def execute(self, call: object, *, context: object) -> object:
            return {"status": "ok", "payload": ({"injected": "row"},)}

    rogue = DeterministicToolGateway(adapters={"starrocks": _RogueAdapter()})
    from xiaowei_agent.tools.gateway import MalformedAdapterResponseError

    with pytest.raises(MalformedAdapterResponseError) as error:
        await rogue.invoke(ok_call, context=context, admission=admission)
    assert "Rogue" not in str(error.value)
    assert "Rogue" not in repr(error.value)


def test_adapter_response_cannot_carry_effect_classification() -> None:
    """adapter 在类型层面就无法降级分类（ADR-007 D7 承重断言 2）。"""
    assert not ({"side_effect", "effect_class"} & set(AdapterResponse.model_fields))


async def test_adapter_error_becomes_a_structured_agent_error(ok_call, context, admission) -> None:
    """丢弃 AdapterResponse.error 会让上游失败在结果里看起来像一次空成功。"""
    cause = ExternalContent.capture(
        source=ExternalSource.TOOL,
        content="ERROR 1045 (28000): Access denied for user 'x'@'h'",
        captured_at=_AT,
    )
    adapter = RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.ERROR,
                payload=(),
                source="starrocks-fake",
                error=cause,
                elapsed_ms=7,
            ),
        )
    )
    failing = DeterministicToolGateway(adapters={"starrocks": adapter})
    result = await failing.invoke(ok_call, context=context, admission=admission)

    assert result.status is ToolCallStatus.ERROR
    assert result.error is not None
    assert result.error.category is ErrorCategory.UPSTREAM
    assert result.error.cause_ref == cause.digest


async def test_adapter_error_text_never_reaches_the_tool_result(
    ok_call, context, admission
) -> None:
    """只有摘要引用出去，原文留在 ExternalContent 里。"""
    # S105 误报：这是被测的上游错误原文，不是凭证。
    upstream_text = "Access denied for user 'admin'"
    cause = ExternalContent.capture(
        source=ExternalSource.TOOL, content=upstream_text, captured_at=_AT
    )
    adapter = RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.ERROR,
                payload=(),
                source="starrocks-fake",
                error=cause,
                elapsed_ms=7,
            ),
        )
    )
    failing = DeterministicToolGateway(adapters={"starrocks": adapter})
    result = await failing.invoke(ok_call, context=context, admission=admission)
    assert upstream_text not in result.model_dump_json()


def test_adapter_response_rejects_self_contradictory_combinations() -> None:
    """矛盾组合在契约层拒绝，不让 Gateway 去猜。

    status=OK 却带 error 会让 Gateway 返回成功并静默丢掉错误；status 非 OK 却带
    payload 会让半截数据被当成证据。
    """
    from pydantic import ValidationError

    cause = ExternalContent.capture(source=ExternalSource.TOOL, content="boom", captured_at=_AT)
    with pytest.raises(ValidationError, match="must not carry an error"):
        AdapterResponse(
            status=AdapterStatus.OK,
            payload=(),
            source="s",
            error=cause,
            elapsed_ms=1,
        )
    with pytest.raises(ValidationError, match="must not carry a payload"):
        AdapterResponse(
            status=AdapterStatus.ERROR,
            payload=({"partial": "row"},),
            source="s",
            error=cause,
            elapsed_ms=1,
        )


async def test_failed_call_carries_no_data_view(ok_call, context, admission) -> None:
    """失败时结果里不得出现任何数据视图。"""
    adapter = RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.ERROR,
                payload=(),
                source="starrocks-fake",
                error=None,
                elapsed_ms=7,
            ),
        )
    )
    failing = DeterministicToolGateway(adapters={"starrocks": adapter})
    result = await failing.invoke(ok_call, context=context, admission=admission)
    assert result.data_view == ()
    assert result.error is not None
    assert result.error.cause_ref is None  # 明确的"无原文"，不是丢失


async def test_adapter_exception_is_structured_not_propagated(ok_call, context, admission) -> None:
    """adapter 的任意异常不得原样冒泡。

    异常文本是外部内容；直接抛出会绕过 ExternalContent、AgentError 与脱敏边界，
    把上游的密码或连接串带进调用方的 traceback。
    """

    class _ExplodingAdapter:
        async def execute(self, call: object, *, context: object) -> object:
            raise RuntimeError("connect failed: password=hunter2")

    gw = DeterministicToolGateway(adapters={"starrocks": _ExplodingAdapter()})
    result = await gw.invoke(ok_call, context=context, admission=admission)

    assert result.status is ToolCallStatus.ERROR
    assert result.error is not None
    assert result.error.category is ErrorCategory.UPSTREAM
    assert result.error.cause_ref is not None
    assert result.data_view == ()


async def test_adapter_exception_text_never_reaches_the_result(ok_call, context, admission) -> None:
    """异常原文只留摘要引用，不进入对外 JSON。"""

    class _ExplodingAdapter:
        async def execute(self, call: object, *, context: object) -> object:
            raise RuntimeError("connect failed: password=hunter2")

    gw = DeterministicToolGateway(adapters={"starrocks": _ExplodingAdapter()})
    result = await gw.invoke(ok_call, context=context, admission=admission)
    assert "hunter2" not in result.model_dump_json()


async def test_cancellation_is_not_swallowed(ok_call, context, admission) -> None:
    """协作式取消必须照常传播——CancelledError 继承自 BaseException。

    吞掉取消会让 worker 在关停时挂住，是比泄漏更隐蔽的故障。
    """
    import asyncio

    class _CancellingAdapter:
        async def execute(self, call: object, *, context: object) -> object:
            raise asyncio.CancelledError

    gw = DeterministicToolGateway(adapters={"starrocks": _CancellingAdapter()})
    with pytest.raises(asyncio.CancelledError):
        await gw.invoke(ok_call, context=context, admission=admission)


def _referenced(path: Path) -> set[str]:
    """模块里出现过的所有标识符：裸名、from-import 名，以及**属性名**。

    漏掉 ``ast.Attribute.attr`` 会留下一条完整的绕过路径::

        import xiaowei_agent.contracts.approval as approval
        approval._ADMISSION_WITNESS      # 只看 Name 时，扫描器只看到 ``approval``

    签发凭据的整个价值就在于"除签发者外没人拿得到"，一个只盖住 import 形式的
    扫描器给出的是虚假保证。
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.Import):
            # ``import a.b.c`` 与 ``import a.b as c``：点分路径的每一段都算引用。
            for alias in node.names:
                names |= set(alias.name.split("."))
                if alias.asname:
                    names.add(alias.asname)
    return names


def test_referenced_detects_aliased_attribute_access(tmp_path: Path) -> None:
    """检测器自测：三种引用形式都必须被看到。

    只测 from-import 形式会让别名属性访问这条路径长期隐形——护栏本身必须有反例。
    """
    nl = chr(10)
    from_import = tmp_path / "a.py"
    from_import.write_text(
        "from xiaowei_agent.contracts.approval import _ADMISSION_WITNESS" + nl,
        encoding="utf-8",
    )
    assert "_ADMISSION_WITNESS" in _referenced(from_import)

    aliased = tmp_path / "b.py"
    aliased.write_text(
        "import xiaowei_agent.contracts.approval as approval"
        + nl
        + "x = approval._ADMISSION_WITNESS"
        + nl,
        encoding="utf-8",
    )
    assert "_ADMISSION_WITNESS" in _referenced(aliased)

    dotted = tmp_path / "c.py"
    dotted.write_text(
        "import xiaowei_agent.contracts.approval"
        + nl
        + "x = xiaowei_agent.contracts.approval._ADMISSION_WITNESS"
        + nl,
        encoding="utf-8",
    )
    assert "_ADMISSION_WITNESS" in _referenced(dotted)

    clean = tmp_path / "d.py"
    clean.write_text("x = 1" + nl, encoding="utf-8")
    assert "_ADMISSION_WITNESS" not in _referenced(clean)


@pytest.mark.parametrize(
    ("witness", "allowed"),
    [
        (
            {"_TOOL_RESULT_WITNESS", "_TOOL_RESULT_WITNESS_KEY"},
            {("tools", "gateway.py"), ("contracts", "tool.py")},
        ),
        (
            {"_ADMISSION_WITNESS", "_ADMISSION_WITNESS_KEY"},
            {("governance", "admission.py"), ("contracts", "approval.py")},
        ),
    ],
    ids=["tool_result", "admission_certificate"],
)
def test_only_the_issuing_module_references_a_construction_witness(
    witness: set[str], allowed: set[tuple[str, ...]]
) -> None:
    """每个签发凭据只有"定义它的契约"和"唯一签发者"两个模块可以引用。

    两组分开参数化而不是并成一个集合:并起来会让 gateway.py 也"合法地"引用
    准入凭据,而 Gateway 是凭证的**消费者**,一旦它能拿到签发凭据就能自签自用,
    整个模式失效。
    """
    allowed_paths = {_SRC.joinpath(*parts) for parts in allowed}
    offenders = [
        path.relative_to(_SRC)
        for path in _SRC.rglob("*.py")
        if path not in allowed_paths and witness & _referenced(path)
    ]
    assert not offenders, f"以下模块不得引用构造凭证: {offenders}"


# --- 领域层禁令的正确作用域 -----------------------------------------------
_DOMAIN_PACKAGES = (
    "contracts",
    "capabilities",
    "planning",
    "governance",
    "runners",
    "observability",
    "evidence",
    "reflection",
    "rendering",
    "application",
)
"""只扫**领域层**。

刻意排除：
- ``tools/``：持有外部客户端正是 adapter 的本职（AGENTS.md 落点表）。
- ``persistence/``：M4 起必须引入 SQLAlchemy / 数据库驱动（DEVELOPMENT_PLAN §7 M4）。

扫描整个 ``src`` 会在 M4 与 M6b 误杀合法实现。
"""

_BANNED_ROOTS = {
    "pymysql",
    "mysql",
    "psycopg",
    "psycopg2",
    "sqlalchemy",
    "alembic",
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "kafka",
    "kubernetes",
    "redis",
}


def test_domain_layer_has_no_third_party_client_import() -> None:
    offenders: list[tuple[str, str]] = []
    for package in _DOMAIN_PACKAGES:
        root = _SRC / package
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if isinstance(node, ast.Import):
                    module = node.names[0].name
                if (module or "").split(".")[0] in _BANNED_ROOTS:
                    offenders.append((str(path.relative_to(_SRC)), module or ""))
    assert not offenders, f"领域层不得导入外部客户端: {offenders}"


# --- 异常处理分支自身的健壮性 -----------------------------------------------

_HOSTILE_CANARY = "synthetic-canary-password=hunter2"


class _HostileStrError(RuntimeError):
    """``__str__`` 自身抛异常的错误对象。

    现实里这来自故障的第三方 driver（例如格式化错误详情时又访问了已关闭的连接），
    不必假设恶意也成立。
    """

    def __str__(self) -> str:
        raise RuntimeError(f"str failed: {_HOSTILE_CANARY}")


class _HostileReprError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("str failed")

    def __repr__(self) -> str:
        raise RuntimeError("repr failed")


class _HostileAdapter:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls: list[object] = []

    async def execute(self, call: object, *, context: object) -> object:
        self.calls.append(call)
        raise self._exc


@pytest.mark.parametrize(
    "exc",
    [_HostileStrError(), _HostileReprError()],
    ids=["hostile_str", "hostile_str_and_repr"],
)
@pytest.mark.asyncio
async def test_adapter_exception_with_failing_str_is_still_absorbed(
    exc: BaseException,
    ok_call: ToolCall,
    context: RequestContext,
    admission: AdmissionCertificate,
) -> None:
    """``str(exc)`` 会调用异常自己的 ``__str__``。

    直接写 ``f"{type(exc).__name__}: {exc}"`` 时，一个 ``__str__`` 抛异常的错误
    对象会让**异常处理分支本身再抛异常**：Gateway 不返回结构化 ERROR，原始异常
    连同它携带的内容一路逃逸到调用方。"任何 adapter 异常都被结构化吸收"这条承诺
    在最需要它的时候失效。

    根因与"先派生后校验"相同：处理器假定某个操作是全函数，而它不是。
    """
    gateway = DeterministicToolGateway(adapters={ok_call.gateway: _HostileAdapter(exc)})
    result = await gateway.invoke(ok_call, context=context, admission=admission)
    assert result.status is ToolCallStatus.ERROR
    assert result.data_view == ()
    assert _HOSTILE_CANARY not in result.model_dump_json()


@pytest.mark.asyncio
async def test_renderable_exception_text_never_reaches_the_result(
    ok_call: ToolCall, context: RequestContext, admission: AdmissionCertificate
) -> None:
    """反例配对：能被渲染的异常同样不得把原文带进结果。

    只测"不崩溃"是不够的——降级路径可能把原文写进 limitations 或 error。
    """
    gateway = DeterministicToolGateway(
        adapters={ok_call.gateway: _HostileAdapter(RuntimeError(_HOSTILE_CANARY))}
    )
    result = await gateway.invoke(ok_call, context=context, admission=admission)
    assert result.status is ToolCallStatus.ERROR
    assert _HOSTILE_CANARY not in result.model_dump_json()


class _BaseExceptionStrError(RuntimeError):
    """``__str__`` 抛 ``BaseException``。

    渲染 helper 只捕 ``Exception`` 时，这条路径让 Gateway 的异常分支再次失效：
    不返回结构化 ERROR，原文随 ``KeyboardInterrupt`` 一起逃逸。这与 adapter
    **自己**抛取消/中断是两回事——那种情况下真实语义就该向上传播。
    """

    def __str__(self) -> str:
        raise KeyboardInterrupt(f"str failed: {_HOSTILE_CANARY}")


@pytest.mark.asyncio
async def test_exception_whose_str_raises_baseexception_is_absorbed(
    ok_call: ToolCall, context: RequestContext, admission: AdmissionCertificate
) -> None:
    gateway = DeterministicToolGateway(
        adapters={ok_call.gateway: _HostileAdapter(_BaseExceptionStrError())}
    )
    result = await gateway.invoke(ok_call, context=context, admission=admission)
    assert result.status is ToolCallStatus.ERROR
    assert _HOSTILE_CANARY not in result.model_dump_json()


class _CustomBaseError(BaseException):
    """任意 ``BaseException`` 子类。

    不用 ``KeyboardInterrupt`` / ``SystemExit`` 作参数：它们从 asyncio task 里
    逃出时会绕过测试内的 try/except、直接打到 pytest 的 session 层，把整轮测试
    截断。Gateway 的 ``except Exception`` 对任意 ``BaseException`` 子类行为完全
    一致，因此用自定义子类覆盖的是同一条语义，没有损失。
    """


@pytest.mark.parametrize(
    "exc",
    [_CustomBaseError("interrupted"), asyncio.CancelledError()],
    ids=["custom_baseexception", "cancelled"],
)
@pytest.mark.asyncio
async def test_adapter_raising_baseexception_itself_still_propagates(
    exc: BaseException,
    ok_call: ToolCall,
    context: RequestContext,
    admission: AdmissionCertificate,
) -> None:
    """反例配对：加固的是**渲染**，不是吞掉真实的中断。

    adapter 自身抛出的 KeyboardInterrupt / SystemExit / CancelledError 必须照常
    向上传播；把它们一起吞掉会让进程无法被中断，那是比泄漏更糟的故障。

    手工 try/except 而不是 ``pytest.raises``：后者对 ``BaseException`` 的处理与
    pytest 的 session 级中断语义纠缠，断言"确实传播"更直接。
    """
    gateway = DeterministicToolGateway(adapters={ok_call.gateway: _HostileAdapter(exc)})
    raised: BaseException | None = None
    try:
        await gateway.invoke(ok_call, context=context, admission=admission)
    except BaseException as caught:
        raised = caught
    assert type(raised) is type(exc), "adapter 自身抛出的 BaseException 必须传播"
