"""标识符槽位是模型输出进入 SQL 编译器的唯一入口。

把 ``database`` / ``user_name`` / ``query_id`` 收进朴素标识符正则，等于在**进入
编译器之前**就消灭引号、分号、空格、注释符、全角字符与隐藏字符。这比在 SQL 文本
层做归一化更早也更彻底：旧项目要守的是用户自由 SQL，我们要守的只是标识符。
"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import MAX_WINDOW_MINUTES
from xiaowei_agent.planning.starrocks.params import (
    DEFAULT_MIN_QUERY_TIME_MS,
    DEFAULT_ROW_LIMIT,
    SlowQueryParams,
)

pytestmark = pytest.mark.security

_START = dt.datetime(2026, 9, 2, 11, 30, tzinfo=dt.UTC)
_END = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)


def _params(**overrides: object) -> SlowQueryParams:
    base: dict[str, object] = {
        "window_start": _START,
        "window_end": _END,
        "min_query_time_ms": DEFAULT_MIN_QUERY_TIME_MS,
        "row_limit": DEFAULT_ROW_LIMIT,
    }
    return SlowQueryParams(**(base | overrides))


_HOSTILE_IDENTIFIERS = [
    "'; DROP TABLE t; --",
    "a OR 1=1",
    "a/*x*/",
    "a b",
    "a'b",
    "a`b",
    "a;b",
    'a"b',
    "a)b",
    "ａ",  # 全角 a
    "a​",  # 零宽空格（Cf）
    "a‘b",  # 智能引号
    "",
    " a",
    "a ",
    "1" * 65,  # 超长
    "-leading-hyphen",
    "a.b",  # 点号会把标识符变成限定名
]


@pytest.mark.parametrize("hostile", _HOSTILE_IDENTIFIERS)
@pytest.mark.parametrize("slot", ["database", "user_name", "query_id"])
def test_identifier_slots_reject_everything_but_plain_identifiers(slot: str, hostile: str) -> None:
    with pytest.raises(ValidationError):
        _params(**{slot: hostile})


@pytest.mark.parametrize("benign", ["sales", "app_user_1", "q1", "a-b", "a$b", "A_1"])
@pytest.mark.parametrize("slot", ["database", "user_name", "query_id"])
def test_plain_identifiers_are_accepted(slot: str, benign: str) -> None:
    """反例配对：闸门不能是恒拒——恒拒的闸门在攻击矩阵下也全绿。"""
    assert getattr(_params(**{slot: benign}), slot) == benign


def test_window_wider_than_the_cap_is_rejected_at_the_parameter_layer() -> None:
    """A14 的参数层：SQL 层的 WINDOW_TOO_WIDE 是第二道闸，不是唯一一道。"""
    with pytest.raises(ValidationError):
        _params(window_end=_START + dt.timedelta(minutes=MAX_WINDOW_MINUTES + 1))


def test_rejection_never_echoes_the_hostile_value() -> None:
    """被拒的槽位取值来自模型输出，不得回填进异常文本。"""
    canary = "canary" + "-slot-4417"
    with pytest.raises(ValidationError) as err:
        _params(database=f"{canary}';--")
    assert canary not in str(err.value)


@pytest.mark.parametrize("hostile", [b"sales", 1, True, None, ["sales"], {"a": 1}])
def test_identifier_slots_reject_non_string_payloads(hostile: object) -> None:
    """严格模式下标识符只接受 str：bytes 与容器都是夹带载荷的通道。

    ``None`` 是**可选过滤未设置**的合法取值，因此单独排除在断言之外。
    """
    if hostile is None:
        assert _params(database=None).database is None
        return
    with pytest.raises(ValidationError):
        _params(database=hostile)


@pytest.mark.parametrize("hostile", ["20", 20.0, True, float("nan")])
def test_row_limit_rejects_non_integer_payloads(hostile: object) -> None:
    """字符串 "20" 与 bool 在 lax 模式下都会被当成整数；严格模式必须拒绝。"""
    with pytest.raises(ValidationError):
        _params(row_limit=hostile)


def test_from_typed_arguments_rejects_a_hostile_extra_key() -> None:
    """篡改者往 typed_arguments 里加键，不得被静默忽略。"""
    args = _params().to_typed_arguments()
    with pytest.raises(ValidationError):
        SlowQueryParams.from_typed_arguments({**args, "min_query_time_ms_": 0})


def test_from_typed_arguments_rejects_a_hostile_identifier() -> None:
    """还原入口与构造入口共用同一条校验，不存在"从存储读回就不校验"的旁路。"""
    args = dict(_params().to_typed_arguments())
    args["database"] = "'; DROP TABLE t; --"
    with pytest.raises(ValidationError):
        SlowQueryParams.from_typed_arguments(args)


def test_from_typed_arguments_rejects_a_widened_window() -> None:
    """存储里的窗口被拉宽后读回，必须与首次构造一样被拒。"""
    args = dict(_params().to_typed_arguments())
    args["window_end"] = (_START + dt.timedelta(minutes=MAX_WINDOW_MINUTES + 1)).isoformat()
    with pytest.raises(ValidationError):
        SlowQueryParams.from_typed_arguments(args)


def test_from_typed_arguments_rejects_a_malformed_timestamp() -> None:
    args = dict(_params().to_typed_arguments())
    args["window_start"] = "not-a-timestamp"
    with pytest.raises(ValidationError):
        SlowQueryParams.from_typed_arguments(args)


def test_from_typed_arguments_rejects_a_naive_timestamp() -> None:
    """不带时区的字面量会让窗口比较变成崩溃而不是拒绝。"""
    args = dict(_params().to_typed_arguments())
    args["window_start"] = "2026-09-02T11:30:00"
    with pytest.raises(ValidationError):
        SlowQueryParams.from_typed_arguments(args)
