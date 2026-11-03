"""普通事实的可见文字；审批和控制事实继续使用原格式。"""

import json
import re
from html.parser import HTMLParser

import pytest
from markdown_it import MarkdownIt
from tests.sdk_core.test_business_result_tables import rendered, visible_text
from tests.sdk_core.test_feishu_render import tables, text_parts

from xiaowei.feishu_render import build_feishu_message, business_text
from xiaowei.feishu_report import build_report
from xiaowei.models import Delivery


@pytest.mark.parametrize("value,expected", [
    ("plain", "plain"),
    ('{"business":"json"}', '{"business":"json"}'),
    ('up{instance="host"} => 1', 'up{instance="host"} => 1'),
    ("", "（空字符串）"),
    (None, "NULL（空值）"),
    (False, "false（布尔值）"),
    (0, "0"),
    (2**60 + 1, "1152921504606846977"),
    ([], "（无）"),
    ({}, "（无）"),
    (["ops", "prod"], "ops、prod"),
    ({"state": "active", "silencedBy": [], "inhibitedBy": [], "mutedBy": None},
     "state=active；silencedBy=（无）；inhibitedBy=（无）；mutedBy=NULL（空值）"),
    ([{"name": "one"}, {"name": "two"}], "name=one\nname=two"),
    ({"outer": {"inner": {"value": "deep"}}}, "outer=\n  inner=\n    value=deep"),
])
def test_plain_fact_value_preserves_text_without_json_dump(value, expected):
    _, result = rendered("local/example", {"value": value})
    assert business_text(result, plain_transport=True).endswith("value: " + expected)
    assert business_text(result).endswith("value: " + expected.replace("\n", r"\n"))


def test_plain_fact_distinguishes_missing_from_null_and_empty():
    _, result = rendered("local/run_readonly_query", {
        "columns": ["missing", "null", "empty"], "rows": [{"null": None, "empty": ""}],
    })
    assert business_text(result, plain_transport=True).endswith(
        "missing: （未提供）\nnull: NULL（空值）\nempty: （空字符串）"
    )


@pytest.mark.parametrize("channel", ["feishu", "web"])
def test_alerts_are_rows_with_readable_labels_and_status(channel):
    _, result = rendered("alertmanager/get_alerts", {"data": [{
        "labels": {"alertname": "HighCPU", "instance": "host-a", "severity": "warning"},
        "status": {"state": "active", "silencedBy": [], "inhibitedBy": [], "mutedBy": None},
    }]}, channel=channel)
    shown = visible_text(result, channel)
    assert "labels" in shown and "data" not in shown
    assert "alertname=HighCPU；instance=host-a；severity=warning" in shown
    assert "state=active；silencedBy=（无）；inhibitedBy=（无）；mutedBy=NULL（空值）" in shown


def test_order_table_retains_sql_and_raw_in_non_attachment_card():
    payload = {"columns": ["channel", "orders"], "rows": [{"channel": "app", "orders": 7}],
               "sql": "SELECT channel, count(*) AS orders FROM orders GROUP BY channel"}
    source, result = rendered("local/run_readonly_query", payload)
    message = build_feishu_message(result, 3500, "oc_test")
    assert source.result_json in text_parts(message)
    assert payload["sql"] in "\n".join(text_parts(message))
    assert tables(message)[0]["rows"] == [{"c0": "app", "c1": "7"}]


def test_brief_non_attachment_row_details_use_readable_cells():
    _, result = rendered("prometheus/query", {
        "columns": ["metric"], "rows": [{"metric": 'up{instance="host"} => 1'}],
    }, view="brief")
    message = build_feishu_message(result, 3500, "oc_test")
    assert not tables(message)
    assert '第 1 行\nmetric: up{instance="host"} => 1' in text_parts(message)


def test_attachment_preview_retains_baseline_rows_without_raw():
    payload = {"columns": ["name", "value"],
               "rows": [{"name": f"row-{i}", "value": "x" * 40} for i in range(200)],
               "sql": "SELECT name, value FROM sample"}
    source, result = rendered("local/run_readonly_query", payload)
    message = build_feishu_message(result, 3500, "oc_test", attachment_preview=True,
                                   platform_limits=True)
    # 同一样例在 a98fb89 上容纳 200 行，防止技术原文挤掉正文预览。
    assert sum(len(t["rows"]) for t in tables(message)) >= 200
    assert source.result_json not in text_parts(message)
    assert payload["sql"] in "\n".join(text_parts(message))


def test_failure_receipt_keeps_code_text():
    message = build_feishu_message(Delivery(content="本轮未完成，请稍后再试。", channel="feishu",
                                           evidence_ids=()),
                                   3500, "oc_test")
    assert message == {"text": "本轮未完成，请稍后再试。"}


@pytest.mark.parametrize("channel", ["feishu", "web"])
@pytest.mark.parametrize("name", ["get_dashboard_by_uid", "list_dashboard_versions",
                                 "update_dashboard", "create_annotation", "get_datasource",
                                 "create_datasource", "update_datasource"])
def test_grafana_contract_json_is_decoded_only_for_registered_result_tools(channel, name):
    _, result = rendered("custom-grafana/" + name, {
        "result_json": json.dumps({"title": "主机监控", "panels": [{"title": "CPU"}],
                                   "tags": ["ops", "prod"]}, ensure_ascii=False),
    }, channel=channel)
    shown = visible_text(result, channel)
    assert "title=CPU" in shown
    assert "ops、prod" in shown
    assert '"panels"' not in shown


@pytest.mark.parametrize("channel", ["feishu", "web"])
@pytest.mark.parametrize("payload,expected", [
    ({"title": "普通看板事实"}, "普通看板事实"),
    ({"result_json": {"title": "普通看板事实"}}, "title=普通看板事实"),
])
def test_grafana_named_tool_without_serialized_result_keeps_ordinary_fields(
    channel, payload, expected,
):
    _, result = rendered("other-contract/get_dashboard_by_uid", payload, channel=channel)
    assert expected in visible_text(result, channel)


@pytest.mark.parametrize("tool", ["grafana/get_dashboard_summary", "local/example"])
def test_other_json_looking_business_strings_are_not_decoded(tool):
    value = '{"panels":[{"title":"business JSON"}]}'
    _, result = rendered(tool, {"result_json": value})
    assert business_text(result, plain_transport=True).endswith("result_json: " + value)


def test_annotation_rows_preserve_window_and_readable_tags():
    _, result = rendered("grafana/get_annotations", {
        "from": 1792673600000, "to": 1792673700000, "dashboardUid": "hosts",
        "annotations": [{"id": 1, "dashboardUID": "hosts", "panelId": None,
                         "time": 1792673650000, "timeEnd": None,
                         "text": "deploy", "tags": ["ops", "prod"]}],
    })
    message = build_feishu_message(result, 3500, "oc_test")
    rows = tables(message)[0]["rows"]
    assert rows == [{"c0": "1", "c1": "hosts", "c2": "NULL（空值）", "c3": "1792673650000",
                     "c4": "NULL（空值）", "c5": "deploy", "c6": "ops、prod"}]
    assert "查询窗口（北京时间）" in "\n".join(text_parts(message, folded=False))
    assert "dashboardUid: hosts" in text_parts(message, folded=False)


def test_alert_group_rows_keep_group_alerts_and_pagination_in_card_body():
    _, result = rendered("alertmanager/get_alert_groups", {
        "data": [{"group_index": 0, "labels": {"alertname": "HighCPU"},
                  "receiver": {"name": "ops"},
                  "alert_count": 1, "alerts_truncated": False}],
        "alerts": [{"group_index": 0, "alerts": [{"labels": {"instance": "host-a"}}]}],
        "total": 1, "offset": 0, "limit": 20,
    })
    message = build_feishu_message(result, 3500, "oc_test")
    shown = text_parts(message, folded=False)
    assert any("instance=host-a" in value for value in shown)
    assert {"total: 1", "offset: 0", "limit: 20"} <= set(shown)
    assert tables(message)[0]["rows"][0]["c1"] == "alertname=HighCPU"


def test_annotation_attachment_preview_counts_events_as_rows():
    _, result = rendered("grafana/get_annotations", {
        "annotations": [{"id": i, "text": "x" * 40} for i in range(200)],
    })
    message = build_feishu_message(result, 3500, "oc_test", attachment_preview=True)
    shown = "\n".join(text_parts(message, folded=False))
    assert 0 < sum(len(t["rows"]) for t in tables(message)) < 200
    assert re.search(r"展示 \d+／本次返回 200 行", shown)


def test_annotation_null_does_not_claim_an_empty_list():
    _, result = rendered("grafana/get_annotations", {"annotations": None})
    assert "annotations: NULL（空值）" in business_text(result, plain_transport=True)


class ReportText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_data(self, data):
        self.values.append(data)


@pytest.mark.parametrize("format_", ["md", "html"])
def test_v4_reports_share_readable_fields_cells_and_long_details(format_):
    literal = '{"business":"json"}'
    prom = 'up{instance="host-a"} => 1'
    _, result = rendered("local/run_readonly_query", {
        "columns": ["channel", "orders", "labels", "literal", "prom", "empty", "missing"],
        "rows": [{"channel": "app", "orders": 7, "labels": {"instance": "host-a"},
                  "literal": literal, "prom": prom, "empty": ""}],
        "status": {"state": "active", "silencedBy": [], "mutedBy": None},
        "long": {"outer": {"value": "x" * 130}},
    })
    raw = result.facts[0].result_json
    body = build_report(result.model_copy(update={"report_format": format_})).content.decode()
    visible = ReportText()
    visible.feed(MarkdownIt("default").enable("table").render(body) if format_ == "md" else body)
    assert "app" in visible.values
    assert "instance=host-a" in visible.values
    assert literal in visible.values and prom in visible.values
    assert "（空字符串）" in visible.values and "（未提供）" in visible.values
    assert any("state=active；silencedBy=（无）；mutedBy=NULL（空值）" in v for v in visible.values)
    assert any("outer=\n  value=" + "x" * 130 in v for v in visible.values)
    assert 'class="language-json"' not in body
    assert "```json" not in body and "~~~json" not in body
    assert result.facts[0].result_json == raw


@pytest.mark.parametrize("format_", ["md", "html"])
def test_v4_report_business_strings_preserve_backslashes_and_markup_as_text(format_):
    values = [r"a\|b", "<img src=x>", "https://invalid.example/x", "__strong__", "a\nb",
              "  padded  ", "   ", " x|y ", "` hi `", "`value", "value`"]
    _, result = rendered("local/run_readonly_query", {
        "columns": ["value"], "rows": [{"value": v} for v in values],
    })
    body = build_report(result.model_copy(update={"report_format": format_})).content.decode()
    html = MarkdownIt("default").enable("table").render(body) if format_ == "md" else body
    visible = ReportText()
    visible.feed(html)
    for value in values:
        assert value in [part.rstrip("\n") for part in visible.values]
    assert "<img src=x>" not in html
    assert '<a href="https://invalid.example/x"' not in html
    assert "<strong>strong</strong>" not in html
