"""慢查询参数 schema、时间窗规范化与 typed_arguments 往返。"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import MAX_ROW_LIMIT, MAX_WINDOW_MINUTES
from xiaowei_agent.planning.starrocks.params import (
    DEFAULT_MIN_QUERY_TIME_MS,
    DEFAULT_ROW_LIMIT,
    DEFAULT_WINDOW_MINUTES,
    SQL_TIME_FORMAT,
    SlowQueryParams,
    normalise_window,
)

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


_PARAMS = _params()
_ARGS = _PARAMS.to_typed_arguments()


def test_defaults_are_within_the_declared_caps() -> None:
    """默认值必须落在上限内，否则默认路径自己就会被参数层拒绝。"""
    assert 0 < DEFAULT_ROW_LIMIT <= MAX_ROW_LIMIT
    assert 0 < DEFAULT_WINDOW_MINUTES <= MAX_WINDOW_MINUTES


def test_window_wider_than_the_cap_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _params(window_end=_START + dt.timedelta(minutes=MAX_WINDOW_MINUTES + 1))


def test_window_exactly_at_the_cap_is_accepted() -> None:
    """边界必须是闭区间的哪一侧要有明确答案，否则上限只是"大约"。"""
    assert _params(window_end=_START + dt.timedelta(minutes=MAX_WINDOW_MINUTES))


@pytest.mark.parametrize(
    "window_end",
    [_START, _START - dt.timedelta(minutes=1)],
    ids=["empty", "reversed"],
)
def test_reversed_or_empty_window_is_rejected(window_end: dt.datetime) -> None:
    with pytest.raises(ValidationError):
        _params(window_end=window_end)


@pytest.mark.parametrize("row_limit", [0, -1, MAX_ROW_LIMIT + 1])
def test_row_limit_outside_the_cap_is_rejected(row_limit: int) -> None:
    with pytest.raises(ValidationError):
        _params(row_limit=row_limit)


def test_naive_window_bounds_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _params(window_start=dt.datetime(2026, 9, 2, 11, 30))


def test_as_of_is_floored_to_the_minute() -> None:
    start, end = normalise_window(
        as_of=dt.datetime(2026, 9, 2, 12, 0, 37, 500000, tzinfo=dt.UTC),
        window_minutes=30,
    )
    assert end == dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)
    assert start == dt.datetime(2026, 9, 2, 11, 30, tzinfo=dt.UTC)


def test_normalised_window_is_stable_across_the_same_minute() -> None:
    """同一分钟内的任意两个时刻必须得到同一窗口——否则 plan_hash 每秒都在变。"""
    first = normalise_window(
        as_of=dt.datetime(2026, 9, 2, 12, 0, 1, tzinfo=dt.UTC), window_minutes=30
    )
    second = normalise_window(
        as_of=dt.datetime(2026, 9, 2, 12, 0, 59, 999999, tzinfo=dt.UTC),
        window_minutes=30,
    )
    assert first == second


def test_non_utc_as_of_is_normalised_to_utc() -> None:
    """带偏移的时刻必须换算到 UTC，否则同一时刻会产生两个字面量。"""
    shanghai = dt.timezone(dt.timedelta(hours=8))
    start, end = normalise_window(
        as_of=dt.datetime(2026, 9, 2, 20, 0, tzinfo=shanghai), window_minutes=30
    )
    assert end == dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)
    assert start == dt.datetime(2026, 9, 2, 11, 30, tzinfo=dt.UTC)


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        normalise_window(
            as_of=dt.datetime(2026, 9, 2, 12, 0),
            window_minutes=30,
        )


@pytest.mark.parametrize("window_minutes", [0, -1, MAX_WINDOW_MINUTES + 1])
def test_normalise_window_rejects_spans_outside_the_cap(window_minutes: int) -> None:
    """规范化不得产出一个参数层随后会拒绝的窗口——上限只有一处定义。"""
    with pytest.raises(ValueError, match="window"):
        normalise_window(as_of=_END, window_minutes=window_minutes)


def test_sql_time_format_round_trips_the_window_bounds() -> None:
    """SQLGuard 要用同一个格式把 AST 里的字面量解析回来比对。"""
    rendered = _START.strftime(SQL_TIME_FORMAT)
    assert rendered == "2026-09-02 11:30:00"
    assert dt.datetime.strptime(rendered, SQL_TIME_FORMAT).replace(tzinfo=dt.UTC) == _START


def test_from_typed_arguments_rejects_unknown_and_missing_keys() -> None:
    """A35：准入路径靠它把标量还原成已校验参数；漏校验会让规则 0 的比对失去意义。"""
    with pytest.raises(ValidationError):
        SlowQueryParams.from_typed_arguments({**_ARGS, "extra": 1})
    with pytest.raises(ValidationError):
        SlowQueryParams.from_typed_arguments({k: v for k, v in _ARGS.items() if k != "row_limit"})


def test_typed_arguments_round_trip_is_lossless() -> None:
    """参数 → typed_arguments → 参数必须完全相同，否则重编译比对会假阳性。"""
    assert SlowQueryParams.from_typed_arguments(_PARAMS.to_typed_arguments()) == _PARAMS


def test_round_trip_is_lossless_with_every_optional_filter_set() -> None:
    filtered = _params(database="sales", user_name="app_user_1", query_id="q1")
    assert SlowQueryParams.from_typed_arguments(filtered.to_typed_arguments()) == filtered


def test_typed_arguments_are_json_scalars_only() -> None:
    """typed_arguments 进 plan_hash，必须是标量；datetime 一律转 ISO 字符串。"""
    for value in _ARGS.values():
        assert value is None or isinstance(value, bool | int | float | str)


def test_from_typed_arguments_ignores_the_declared_envelope_keys() -> None:
    """步骤的 typed_arguments 还携带 sql 与 sql_template_id 两个非参数键。

    它们是**声明过的**信封键，不是"未知键"：准入路径拿到的正是整份
    typed_arguments，还原入口必须能在不放宽未知键拒绝的前提下跳过它们。
    """
    restored = SlowQueryParams.from_typed_arguments(
        {**_ARGS, "sql": "SELECT 1", "sql_template_id": "starrocks.slow_query.list.v1"}
    )
    assert restored == _PARAMS


def test_min_query_time_ms_falls_back_to_the_declared_default() -> None:
    """count 模板不消费阈值，因此其步骤的 typed_arguments 里没有这个键。

    还原时取声明默认值而不是报错；真正的防线是重编译比对——阈值若被篡改，
    重编译出的 SQL 与步骤携带的 SQL 逐字节不同，规则 0 立即拒绝。
    """
    without = {k: v for k, v in _ARGS.items() if k != "min_query_time_ms"}
    assert (
        SlowQueryParams.from_typed_arguments(without).min_query_time_ms == DEFAULT_MIN_QUERY_TIME_MS
    )


def test_params_do_not_carry_execution_context() -> None:
    """环境/租户/发起人不是 SQL 参数：它们由 target_fingerprint 绑定。

    机械复制执行上下文会让同一份 SQL 参数在两个 DTO 里各有一份真源
    （ARCHITECTURE §6）。
    """
    assert not ({"environment_id", "tenant_id", "actor"} & set(SlowQueryParams.model_fields))
