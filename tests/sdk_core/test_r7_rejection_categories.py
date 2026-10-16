"""R7-1: trusted rejection branches retain stop semantics and safe correction hints."""

import json
from typing import get_args

import pytest
from sqlalchemy.engine import URL
from tests.p1b.test_starrocks_adapter import NOW, driver, schema_results
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.test_starrocks_tools import assembled

from xiaowei.governance import ToolRejectedError
from xiaowei.mcp import _prepare_grafana, _prepare_prometheus
from xiaowei.models import ToolRequest
from xiaowei.tools import _rejection_feedback

pytestmark = pytest.mark.loopback
SENTINEL = "R7_PRIVATE_SENTINEL"


def request(name: str, **arguments: object) -> ToolRequest:
    return ToolRequest(
        tool_id=f"local/{name}",
        target_id=SR.target_id,
        call_id="r7-reject",
        tool_name=name,
        arguments=arguments,
    )


def feedback(exc: ToolRejectedError, name: str) -> dict:
    raw = _rejection_feedback(exc, name)
    assert SENTINEL not in raw and "Please try again" not in raw
    result = json.loads(raw)
    assert "evidence_id" not in result
    return result


@pytest.mark.parametrize(
    "case,code,retry,detail",
    [
        ("database", "capacity_exceeded", False, "库名超过结果容量"),
        ("column", "capacity_exceeded", False, "下一列"),
        ("search", "invalid_arguments", True, "更具体的关键词"),
    ],
)
async def test_actual_row_capacity_rejections_before_io(
    postgres_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    code: str,
    retry: bool,
    detail: str,
) -> None:
    wide = SENTINEL + "x" * 210  # legal identifier, exceeds the actual 200-byte value limit
    key = (wide, "sales") if case == "database" else ("shop", wide if case == "search" else "sales")
    column = wide if case == "column" else "region"
    drv = driver()
    make = drv.make

    def connection():
        conn = make()
        # Refresh uses multiple connections: every one must see the same synthetic metadata.
        conn.results.update(schema_results({key: ((column, "varchar", "YES", None),)}))
        return conn

    drv.make = connection
    names = {"database": "list_databases", "column": "describe_table", "search": "list_tables"}
    name = names[case]
    args = {"cluster": SR.target_id, "cursor": None}
    if case == "database":
        args.update(page_size=1)
    elif case == "column":
        args.update(database=key[0], table=key[1])
    else:
        args.update(database=None, keyword=SENTINEL, page_size=1)
    async with assembled(postgres_url, monkeypatch, drv) as env:
        with pytest.raises(ToolRejectedError) as refused:
            await env.governed.invoke(
                env.ctx(), request(name, **args), env.executes[(f"local/{name}", SR.target_id)]
            )
        result = feedback(refused.value, name)
        assert result["code"] == code and result["retry_this_turn"] is retry
        assert detail in result["reason"]
        assert env.drv.attempts == 0 and await env.evidence_rows() == 0


@pytest.mark.parametrize("cursor", [SENTINEL, "0000000000000000-1-0000000000000000"])
async def test_cursor_invalid_or_stale_keeps_restart_hint(
    postgres_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    cursor: str,
) -> None:
    async with assembled(postgres_url, monkeypatch, driver()) as env:
        name = "list_tables"
        with pytest.raises(ToolRejectedError) as refused:
            await env.governed.invoke(
                env.ctx(),
                request(
                    name,
                    cluster=SR.target_id,
                    keyword="sales",
                    database=None,
                    page_size=1,
                    cursor=cursor,
                ),
                env.executes[(f"local/{name}", SR.target_id)],
            )
        result = feedback(refused.value, name)
        assert result["code"] == "invalid_arguments" and result["retry_this_turn"] is True
        assert "从头开始" in result["reason"]
        assert "cursor" in result["reason"] and "null" in result["reason"]
        assert env.drv.attempts == 0 and await env.evidence_rows() == 0


@pytest.mark.parametrize(
    "args,needles",
    [
        ({"metric": " "}, ("metric 指标名不能为空", "label_values")),
        ({"query": SENTINEL * 820}, ("8192", "不能为空")),
        ({"matches": []}, ("选择器", "至少一个")),
        ({"label": "!" + SENTINEL}, ("标签名", "256")),
        ({"start_time": "200", "end_time": "100"}, ("有序", "31天")),
        ({"start_time": "0", "end_time": "11000", "step": "1"}, ("11000", "增大步长")),
        ({"start_time": "0", "end_time": "1", "step": "1.0001"}, ("整毫秒", "1秒")),
        ({"start_time": SENTINEL, "end_time": "1"}, ("Unix", "RFC3339")),
        ({"start_time": "0", "end_time": "1", "step": SENTINEL}, ("30s/5m", "有限秒数")),
    ],
)
def test_prometheus_parameter_rejections_keep_fixed_actionable_hints(args, needles) -> None:
    with pytest.raises(ToolRejectedError) as refused:
        _prepare_prometheus(request("series", **args), "prometheus.series", NOW)
    result = feedback(refused.value, "series")
    assert result["code"] == "invalid_arguments" and result["retry_this_turn"] is True
    assert all(needle in result["reason"] for needle in needles)


@pytest.mark.parametrize(
    "args,needles",
    [
        ({"uid": " "}, ("uid", "不能为空")),
        ({"tags": [""]}, ("标签", "32")),
        ({"limit": 101}, ("limit", "1–100")),
        ({"from": 1000, "to": 0}, ("Unix 毫秒", "31天")),
    ],
)
def test_grafana_parameter_rejections_keep_fixed_actionable_hints(args, needles) -> None:
    with pytest.raises(ToolRejectedError) as refused:
        _prepare_grafana(request("search", **args))
    result = feedback(refused.value, "search")
    assert result["code"] == "invalid_arguments" and result["retry_this_turn"] is True
    assert all(needle in result["reason"] for needle in needles)


@pytest.mark.parametrize(
    "name,args,needle",
    [
        ("list_databases", {"page_size": 0}, "页大小上限"),
        ("list_tables", {"keyword": " ", "database": None, "page_size": 1}, "keyword 不能为空白"),
        ("list_tables", {"keyword": None, "database": None, "page_size": 1}, "须指定 database"),
    ],
)
async def test_local_parameter_prechecks_keep_specific_safe_correction(
    postgres_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    name,
    args,
    needle,
) -> None:
    async with assembled(postgres_url, monkeypatch, driver()) as env:
        with pytest.raises(ToolRejectedError) as refused:
            await env.governed.invoke(
                env.ctx(),
                request(name, cluster=SR.target_id, cursor=None, **args),
                env.executes[(f"local/{name}", SR.target_id)],
            )
        result = feedback(refused.value, name)
        assert result["code"] == "invalid_arguments" and result["retry_this_turn"] is True
        assert needle in result["reason"]
        assert env.drv.attempts == 0 and await env.evidence_rows() == 0


def test_all_closed_labels_render_without_reading_exception_text() -> None:
    from xiaowei.governance import ToolRejectCode, ToolRejectHint

    class NoMessageError(ToolRejectedError):
        def __str__(self):
            raise AssertionError("Do not read exception text")

    for code in get_args(ToolRejectCode):
        result = feedback(NoMessageError(SENTINEL, code=code), "any_tool")
        assert result["code"] == code
        if code in {"unsupported", "capacity_exceeded", "precheck_failed"}:
            assert result["retry_this_turn"] is False and "停止" in result["reason"]
    for hint in get_args(ToolRejectHint):
        result = feedback(NoMessageError(SENTINEL, hint=hint), "any_tool")
        assert result["reason"]


def test_unknown_target_feedback_points_back_to_the_selected_environment() -> None:
    result = feedback(ToolRejectedError(SENTINEL, code="unknown_target"), "describe_table")
    assert result["code"] == "unknown_target" and result["retry_this_turn"] is True
    reason = result["reason"]
    assert "用户在本会话明确选定的环境" in reason
    assert "不猜选集群" in reason and "未选定先澄清" in reason
    for legacy in ("唯一可读", "完整发现", "获准发现"):
        assert legacy not in reason
