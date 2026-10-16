"""恶意对象跨越每一条"必须吸收"的边界时，都不得让异常或原文逃逸。

**按边界枚举，不按函数枚举。** 上一轮修 ``redaction._safe_str`` 时我是按函数找
同类问题的，于是漏掉了 ``log.RedactingFilter``——它是另一个函数，却是同一条边界。
这里把边界列成参数表：新增一条边界就要在表里加一行，漏掉会很显眼。

三种攻击面各有独立成因：

* ``__str__`` 抛 ``BaseException``——只捕 ``Exception`` 的处理器会被打穿。
* 运行时构造的类名——``type(name, bases, dict)`` 让类名成为调用方可控数据。
  上一轮把 ``type(x).__name__`` 当作"类的身份、不是数据"放进了白名单，那是错的。
* 校验路径绕过 ``Contract`` 基类——``TypeAdapter`` 不经过基类的 model_config，
  ``hide_input_in_errors`` 因此不生效。
"""

import io
import logging

import pytest
from pydantic import ValidationError
from tests.conftest import make_certificate
from tests.fakes.fixtures import FIXTURE_TOOL_CALL

from xiaowei_agent.config import load_settings
from xiaowei_agent.contracts import RequestContext
from xiaowei_agent.contracts.external import content_digest
from xiaowei_agent.log import LOGGER_NAME, configure_logging
from xiaowei_agent.planning import canonical_json
from xiaowei_agent.redaction import safe_error_details
from xiaowei_agent.tools.gateway import DeterministicToolGateway

pytestmark = pytest.mark.security

CANARY = "synthetic-canary-password=hunter2"


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="0" * 32,
        policy_revision="policy-2026-09-01",
    )


def _hostile_type() -> type:
    """类名由 canary 构成的运行时类型。

    ``type(name, bases, dict)`` 是标准库能力，不需要任何特殊权限——这正是
    "类名是常量"这个假设不成立的原因。
    """
    return type(f"Evil_{CANARY}", (), {})


class _RaisingStr:
    def __str__(self) -> str:
        raise KeyboardInterrupt(CANARY)


# --- 边界 1：日志过滤器 -----------------------------------------------------


def test_log_filter_absorbs_a_message_object_whose_str_raises() -> None:
    """``record.getMessage()`` 会调用消息对象自己的 ``__str__``。

    只捕 ``Exception`` 时，一个 ``__str__`` 抛 ``KeyboardInterrupt`` 的消息对象
    会连同它携带的文本一起逃出日志边界——日志调用点通常没有 try，直接打到调用方。

    注意必须把恶意对象作为 **msg** 传入：作为 ``args`` 传入时 ``redact()`` 会先
    把它吃掉，探针打不到这条路径（我第一次复现就是这样打偏的）。
    """
    buffer = io.StringIO()
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=buffer)
    logging.getLogger(LOGGER_NAME).info(_RaisingStr())
    assert CANARY not in buffer.getvalue()
    assert "unrenderable" in buffer.getvalue()


def test_log_filter_still_records_hostile_args() -> None:
    """反例配对：降级不等于丢记录。"""
    buffer = io.StringIO()
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=buffer)
    logging.getLogger(LOGGER_NAME).info("%s", _RaisingStr())
    assert buffer.getvalue().strip() != ""
    assert CANARY not in buffer.getvalue()


# --- 边界 2：TypeAdapter 校验路径 -------------------------------------------


def test_content_digest_rejection_hides_the_rejected_input() -> None:
    """``hide_input_in_errors`` 挂在 ``Contract`` 的 model_config 上。

    ``content_digest`` 用 ``TypeAdapter`` 校验，**不经过基类**，因此必须自带该
    配置。这是"安全属性挂在基类上、但某条校验路径根本不走基类"的漏法。
    """
    with pytest.raises(ValidationError) as caught:
        content_digest(CANARY.encode())
    assert CANARY not in str(caught.value)
    assert CANARY not in repr(safe_error_details(caught.value))


def test_content_digest_still_accepts_text() -> None:
    """反例配对：隐藏输入不等于拒绝合法输入。"""
    assert len(content_digest("hello")) == 64


# --- 边界 3：运行时构造的类名 -----------------------------------------------


def test_canonical_json_rejection_carries_no_type_name() -> None:
    with pytest.raises(TypeError) as caught:
        canonical_json({"k": _hostile_type()()})
    assert CANARY not in str(caught.value)


@pytest.mark.asyncio
async def test_gateway_non_conforming_return_carries_no_type_name() -> None:
    """adapter 返回值的类可以是运行时构造的，类名因此是外部数据。"""

    from xiaowei_agent.tools.gateway import MalformedAdapterResponseError

    class _Adapter:
        async def execute(self, call: object, *, context: object) -> object:
            return _hostile_type()()

    gateway = DeterministicToolGateway(adapters={FIXTURE_TOOL_CALL.gateway: _Adapter()})
    with pytest.raises(MalformedAdapterResponseError) as caught:
        await gateway.invoke(
            FIXTURE_TOOL_CALL,
            context=_context(),
            admission=make_certificate(FIXTURE_TOOL_CALL),
        )
    assert CANARY not in str(caught.value)


@pytest.mark.asyncio
async def test_hostile_exception_type_name_never_reaches_the_result() -> None:
    """``safe_exception_text`` 仍保留类型名——这是**有意**的，不是遗漏。

    它的输出只进 ``ExternalContent.content``，即本项目指定的不可信文本容器，
    并且已经过 ``scrub_text``；而它的另一半 ``_safe_str(exc)`` 本来就是任意
    不可信文本。在一个"按设计装不可信文本"的容器里单独抹掉类型名并不自洽。

    真正要守的是**它不进结果**——这里直接断言这一点。
    """

    class _Adapter:
        async def execute(self, call: object, *, context: object) -> object:
            raise type(f"Boom_{CANARY}", (RuntimeError,), {})("x")

    gateway = DeterministicToolGateway(adapters={FIXTURE_TOOL_CALL.gateway: _Adapter()})
    result = await gateway.invoke(
        FIXTURE_TOOL_CALL,
        context=_context(),
        admission=make_certificate(FIXTURE_TOOL_CALL),
    )
    assert result.status.value == "error"
    assert CANARY not in result.model_dump_json()
