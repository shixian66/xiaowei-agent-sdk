"""新交付默认：可见业务事实、单一技术区、无技术附件或通知包装。"""

import json

import pytest
from tests.sdk_core.test_feishu_render import delivery, fact, text_parts

from xiaowei.feishu_delivery import _data_parts
from xiaowei.feishu_render import build_feishu_message
from xiaowei.models import Delivery
from xiaowei.webhook import _notification_text


def business(*facts, **changes) -> Delivery:
    return delivery(*facts, analysis="分析与建议：继续关注。").model_copy(
        update={"answer_version": 4, **changes}
    )


def panels(message):
    return [e for e in message["card"]["body"]["elements"] if e["tag"] == "collapsible_panel"]


def test_v4_single_technical_area_and_visible_business_scope() -> None:
    value = business(fact(), fact(evidence_id="ev_other", target_id="fat", truncated=True))
    message = build_feishu_message(value, 3500, "oc", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    assert len(panels(message)) == 1
    assert panels(message)[0]["header"]["title"]["content"] == "技术详情"
    assert panels(message)[0]["expanded"] is False
    for word in (
        "查看依据",
        "结果 1",
        "工具结果（系统根据证据生成）",
        "local/",
        "ev_",
        "SELECT",
        "elapsed_ms",
    ):
        assert word not in visible
    assert "orders" in visible and "pre" in visible and "fat" in visible
    assert "2026-10-09" in visible and "已截断" in visible
    assert "SELECT name" in "\n".join(text_parts(message))


def test_v4_non_tabular_prometheus_result_is_still_a_fact() -> None:
    source = fact(
        tool_id="prometheus-pre/query",
        columns=(),
        rows=(),
        metadata={
            "query": "avg_over_time(up[1h])",
            "result": '{instance="host"} => 0.5 @[1792673695]',
            "warnings": "部分样本不可用",
        },
    )
    message = build_feishu_message(business(source), 3500, "oc", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    assert "host" in visible and "0.5" in visible
    assert "数据源返回警告" in visible
    assert "avg_over_time" not in visible


def test_unasked_large_technical_data_does_not_create_an_attachment() -> None:
    source = fact(metadata={"sql": "SELECT '" + "x" * 40_000 + "'", "elapsed_ms": 42})
    parts = _data_parts(business(source), 0, 3500, "oc", None)
    assert [p.kind for p in parts] == ["body"]
    message = json.loads(parts[0].body)
    assert "orders" in "\n".join(text_parts(message, folded=False))
    assert "本次完整结果见附件" not in str(message)
    assert "技术详情超出消息容量，未附" in str(message)


@pytest.mark.parametrize("format_", ["md", "html"])
def test_v4_report_contains_business_not_duplicate_technical_files(format_: str) -> None:
    parts = _data_parts(business(fact(), report_format=format_), 0, 3500, "oc", None)
    assert [p.kind for p in parts] == [format_, "body"]
    report = parts[0].body.decode()
    assert "orders" in report and "pre" in report and "2026-10-09" in report
    for word in ("ev_original", "查看依据", "<details", "证据明细"):
        assert word not in report


def test_v4_notification_omits_technical_metadata_and_approval_command() -> None:
    source = fact(
        metadata={"sql": "SELECT name", "approval_command": "/批准 act_x", "status": "unknown"}
    )
    text = _notification_text(business(source, notification_notice="结果未知，不会自动重发"), "")
    assert "orders" in text and "unknown" in text and "不会自动重发" in text
    assert "pre" in text and "2026-10-09" in text
    for word in ("SELECT", "/批准", "工具结果", "ev_", "local/"):
        assert word not in text


def test_v4_brief_monitoring_value_uses_the_single_technical_area() -> None:
    source = fact(
        tool_id="prometheus/query",
        view="brief",
        columns=(),
        rows=(),
        metadata={"query": "up", "result": 'node_uname_info{instance="host"} => 1'},
    )
    message = build_feishu_message(business(source), 3500, "oc", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    assert "node_uname_info" not in visible
    assert "node_uname_info" in "\n".join(text_parts(message))
    assert len(panels(message)) == 1


def test_v4_automatic_business_file_keeps_nested_value_types() -> None:
    from xiaowei.feishu_report import build_reports

    original = {
        "columns": ["value"],
        "rows": [{"value": {"nested": 1}}],
        "sql": "SELECT nested",
        "status": {"checked": True},
    }
    source = fact(
        columns=("value",),
        rows=({"value": '{"nested": 1}'},),
        metadata={"sql": "SELECT nested", "status": '{"checked": true}'},
        result_json=json.dumps(original),
    )
    (file,) = build_reports(business(source, report_format="md"), expand_brief=False)
    content = file.content.decode()
    assert '"nested": 1' in content and '"checked": true' in content
    assert '\\"nested\\"' not in content and "SELECT nested" not in content


@pytest.mark.parametrize(
    "tool,sql,label",
    [
        ("local/list_databases", "objects", "结构采集 SQL 模板"),
        ("local/list_tables", "objects", "结构采集 SQL 模板"),
        ("local/describe_table", "columns", "结构采集 SQL 模板"),
        ("local/run_readonly_query", "SELECT name\nFROM shop.orders", "实际 SQL"),
    ],
)
def test_v4_distinguishes_schema_templates_from_actual_sql(tool: str, sql: str, label: str) -> None:
    from xiaowei.starrocks import SCHEMA_COLUMNS_SQL, SCHEMA_OBJECTS_SQL

    actual = {"objects": SCHEMA_OBJECTS_SQL, "columns": SCHEMA_COLUMNS_SQL}.get(sql, sql)
    message = build_feishu_message(
        business(fact(tool_id=tool, metadata={"sql": actual})),
        3500,
        "oc",
        platform_limits=True,
    )
    assert actual not in "\n".join(text_parts(message, folded=False))
    detail = "\n".join(text_parts(message))
    assert f"{label}:\n{actual}" in detail
    if label == "结构采集 SQL 模板":
        assert "实际 SQL" not in detail
