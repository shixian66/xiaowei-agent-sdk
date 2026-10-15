"""D2：文件里的结果可读，同时保留可信投影的值和类型。"""

import json
import re
from html import unescape

import pytest
from markdown_it import MarkdownIt
from tests.sdk_core.test_feishu_reports import _delivery

from xiaowei.feishu_report import build_report
from xiaowei.models import AnswerInference, Delivery


def test_html_report_uses_real_table_and_preserves_values_without_duplicate_json() -> None:
    delivery = _delivery("html")
    result = {
        "sql": "SELECT '<x>' AS label",
        "columns": ["id", "nullable", "literal", "nested"],
        "metadata": {"scope": "昨天", "limit": 1000},
        "rows": [
            {"id": 2**60, "nullable": None, "literal": "NULL", "nested": {"a": [1, True]}},
            {"id": 2**60 + 1, "nullable": 0, "literal": "<img src=x>", "nested": []},
        ],
    }
    fact = delivery.facts[0].model_copy(
        update={"result_json": json.dumps(result, ensure_ascii=False)}
    )
    body = build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()
    assert "小维巡检报告" not in body
    assert "<style>" in body and "<table" in body
    assert '<th scope="col">nullable</th>' in body
    assert 'class="sql-code"' in body and "SELECT &#x27;&lt;x&gt;&#x27; AS label" in body
    assert "1152921504606846976" in body and "1152921504606846977" in body
    assert ">null</code>" in body and ">&quot;NULL&quot;</code>" in body
    assert "&quot;a&quot;: [1, true]" in body
    assert "&lt;img src=x&gt;" in body and "<img src=x>" not in body
    assert "scope" in body and "昨天" in body and "limit" in body
    assert body.count("1152921504606846976") == 1


def test_md_report_uses_table_for_simple_rows_and_fences_complex_originals() -> None:
    delivery = _delivery("md")
    result = {
        "columns": ["name", "count"],
        "rows": [{"name": "east", "count": 2}, {"name": "west", "count": 3}],
    }
    fact = delivery.facts[0].model_copy(update={"result_json": json.dumps(result)})
    body = build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()
    assert "小维巡检报告" not in body
    assert "| name | count |" in body
    assert '| "east" | 2 |' in body and '| "west" | 3 |' in body

    hostile = {
        "columns": ["ddl"],
        "rows": [{"ddl": "CREATE TABLE t (v STRING);\n-- [x](https://evil.invalid) ~~~"}],
    }
    changed = fact.model_copy(update={"result_json": json.dumps(hostile)})
    body = build_report(delivery.model_copy(update={"facts": (changed,)})).content.decode()
    assert "CREATE TABLE t (v STRING);\n-- [x](https://evil.invalid) ~~~" in body
    assert "~~~~sql\n" in body
    assert "| ddl |" not in body


def test_md_report_keeps_source_links_and_markup_inside_fences() -> None:
    delivery = _delivery("md")
    for value in ("https://invalid.example/path", "__strong__"):
        result = {"columns": ["value"], "rows": [{"value": value}]}
        fact = delivery.facts[0].model_copy(update={"result_json": json.dumps(result)})
        body = build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()
        assert "| value |" not in body
        assert f'~~~json\n{{"value": "{value}"}}' in body
        rendered = MarkdownIt("default").enable("table").render(body)
        assert "<a href=" not in rendered and "<strong>strong</strong>" not in rendered


def test_md_report_keeps_word_internal_underscores_in_a_readable_table() -> None:
    delivery = _delivery("md")
    result = {"columns": ["order_id"], "rows": [{"order_id": "east_1"}]}
    fact = delivery.facts[0].model_copy(update={"result_json": json.dumps(result)})
    body = build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()
    assert "| order_id |" in body
    assert '| "east_1" |' in body
    rendered = MarkdownIt("default").enable("table").render(body)
    assert "<th>order_id</th>" in rendered
    assert "<td>&quot;east_1&quot;</td>" in rendered


def test_no_fact_advice_preserves_lines_and_invalid_projection_stays_literal() -> None:
    advice = Delivery(
        content="解释第一段。\n第二段。",
        channel="feishu",
        evidence_ids=(),
        feishu_text="解释第一段。\n第二段。",
    )
    html = build_report(advice.model_copy(update={"report_format": "html"})).content.decode()
    md = build_report(advice.model_copy(update={"report_format": "md"})).content.decode()
    assert '<p class="prose">' in html and "解释第一段。" in html
    assert "本文件包含 0 项结果" not in html
    rendered = MarkdownIt("default").render(md)
    assert "解释第一段。\n第二段。" in unescape(rendered)

    delivery = _delivery("html")
    fact = delivery.facts[0].model_copy(update={"result_json": "not valid JSON <x>"})
    body = build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()
    assert "not valid JSON &lt;x&gt;" in body


def test_empty_and_sparse_results_keep_fields_and_distinguish_missing_from_null() -> None:
    delivery = _delivery("html")
    result = {
        "columns": ["value", "value", "nullable"],
        "rows": [{"value": 0, "nullable": None, "extra": False}, {"value": ""}],
        "next_cursor": "page-2",
    }
    fact = delivery.facts[0].model_copy(
        update={"result_json": json.dumps(result, ensure_ascii=False)}
    )
    html = build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()
    assert html.count('<th scope="col">value</th>') == 2
    assert '<th scope="col">extra</th>' in html
    assert ">null</code>" in html and ">false</code>" in html
    assert 'class="missing">未提供</span>' in html
    assert "page-2" in html

    empty = fact.model_copy(update={"result_json": "{}"})
    for format_ in ("html", "md"):
        body = build_report(
            delivery.model_copy(update={"facts": (empty,), "report_format": format_})
        ).content.decode()
        assert "{}" in body

    no_rows = fact.model_copy(update={"result_json": '{"columns":[],"rows":[]}'})
    for format_ in ("html", "md"):
        body = build_report(
            delivery.model_copy(update={"facts": (no_rows,), "report_format": format_})
        ).content.decode()
        assert "列定义" in body and "0 行" in body


def test_dashboard_summary_and_diagnostic_plan_keep_readable_shapes() -> None:
    delivery = _delivery("html")
    dashboard = {
        "uid": "StarsL-JOB-node",
        "title": "Node Exporter 基础监控",
        "panelCount": 24,
        "timeRange": {"start": "now-30m", "end": "now"},
        "description": "看板定义，不是已经执行的实时采样。",
    }
    fact = delivery.facts[0].model_copy(
        update={
            "tool_id": "grafana-prod/get_dashboard_summary",
            "result_json": json.dumps(dashboard, ensure_ascii=False),
        }
    )
    report = build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()
    for value in ("StarsL-JOB-node", "Node Exporter 基础监控", "24", "now-30m"):
        assert value in report
    assert "<table" not in report

    plan = "SCAN orders\n  predicates: dt = 'yesterday'\n  rows: 1000"
    diagnostic = {
        "sql": "EXPLAIN LOGICAL SELECT * FROM orders",
        "columns": ["plan"],
        "rows": [{"plan": plan}],
    }
    changed = fact.model_copy(
        update={
            "tool_id": "local/explain_query",
            "result_json": json.dumps(diagnostic),
        }
    )
    for format_ in ("html", "md"):
        body = build_report(
            delivery.model_copy(update={"facts": (changed,), "report_format": format_})
        ).content.decode()
        assert plan in unescape(body)
        if format_ == "html":
            assert 'class="sql-code"' in body
        else:
            assert "~~~text\n" in body


@pytest.mark.parametrize("section", ["answer", "analysis", "basis"])
def test_md_prose_preserves_visible_lines_indentation_and_literal_markup(section: str) -> None:
    original = (
        "SELECT region, -- 区域\n"
        "    SUM(total) AS s\n"
        "FROM shop.sales\n"
        "GROUP BY region;\n\n"
        "_强调_ ~~旧值~~ &lt; [链接](https://invalid.example)\n"
        "~~~text\n围栏里的文字\n~~~"
    )
    if section == "answer":
        delivery = Delivery(
            channel="feishu",
            content=original,
            feishu_text=original,
            evidence_ids=(),
            report_format="md",
        )
    else:
        delivery = _delivery("md")
        if section == "analysis":
            delivery = delivery.model_copy(
                update={
                    "analysis": (
                        AnswerInference(text=original, evidence_ids=delivery.evidence_ids),
                    )
                }
            )
        else:
            delivery = delivery.model_copy(
                update={
                    "basis": (
                        ("local/show_create_table", "shop.sales"),
                        ("local/describe_table", "shop.orders"),
                    )
                }
            )
            original = "local/show_create_table · shop.sales\nlocal/describe_table · shop.orders"
    rendered = MarkdownIt("default").render(build_report(delivery).content.decode())
    visible_blocks = [
        unescape(value)
        for value in re.findall(r"<pre><code[^>]*>(.*?)</code></pre>", rendered, re.DOTALL)
    ]
    assert original.splitlines() in [value.splitlines() for value in visible_blocks]
    assert "<del>旧值</del>" not in rendered and "<em>强调</em>" not in rendered
    assert "<a href=" not in rendered
