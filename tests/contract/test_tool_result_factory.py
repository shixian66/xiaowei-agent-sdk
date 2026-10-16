"""Gateway 之外不能用原始 adapter 响应伪造 ToolResult。

**诚实声明**：Python 没有语言级私有性。本文件断言的是**已文档化的边界**——直接
构造、model_validate、model_construct、model_copy 四条实用路径均被封堵。
``object.__setattr__`` 与重定义模块无法在语言层封堵，属已知残余风险，由评审与源码
扫描测试覆盖（ARCHITECTURE §5.8）。
"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import ToolCallStatus, ToolResult


def _payload() -> dict[str, object]:
    return {
        "status": ToolCallStatus.OK,
        "data_view": (),
        "raw_ref": None,
        "source": "fake",
        "limitations": (),
        "trace_id": "0" * 32,
        "error": None,
    }


def test_direct_construction_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolResult(**_payload())


def test_model_validate_without_the_witness_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolResult.model_validate(_payload())


def test_model_construct_is_rejected() -> None:
    """model_construct 绕过全部校验，必须显式封堵。"""
    with pytest.raises(NotImplementedError):
        ToolResult.model_construct(**_payload())


async def test_model_copy_on_a_legitimate_result_is_rejected(
    gateway, ok_call, context, admission
) -> None:
    """否则可由一个合法结果 copy 出一个被篡改的结果。"""
    legit = await gateway.invoke(ok_call, context=context, admission=admission)
    with pytest.raises(NotImplementedError):
        legit.model_copy(update={"status": ToolCallStatus.ERROR})


async def test_gateway_can_construct(gateway, ok_call, context, admission) -> None:
    result = await gateway.invoke(ok_call, context=context, admission=admission)
    assert isinstance(result, ToolResult)
    assert result.status is ToolCallStatus.OK
    assert result.trace_id == context.trace_id


async def test_result_data_view_is_deeply_immutable(gateway, ok_call, context, admission) -> None:
    result = await gateway.invoke(ok_call, context=context, admission=admission)
    with pytest.raises(TypeError):
        result.data_view[0]["query_id"] = "tampered"  # type: ignore[index]
