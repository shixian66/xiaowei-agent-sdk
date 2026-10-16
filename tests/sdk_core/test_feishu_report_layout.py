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
    # 新契约：DDL 原文仍在表下围栏中，表格只放指引。
    assert "| ddl |" in body and "| 见下方 第 1 行 |" in body


def test_md_report_keeps_source_links_and_markup_inside_fences() -> None:
    delivery = _delivery("md")
    for value in ("https://invalid.example/path", "__strong__"):
        result = {"columns": ["value"], "rows": [{"value": value}]}
        fact = delivery.facts[0].model_copy(update={"result_json": json.dumps(result)})
        body = build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()
        # 新契约：不安全值进入表格，但处于代码片段内，不解析为链接或强调。
        assert "| value |" in body and f'`"{value}"`' in body
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


def _md_body(result: dict) -> str:
    delivery = _delivery("md")
    fact = delivery.facts[0].model_copy(
        update={"result_json": json.dumps(result, ensure_ascii=False)}
    )
    return build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()


def _md_html(body: str) -> str:
    return MarkdownIt("default").enable("table").render(body)


def test_md_table_for_timestamps_ips_and_multiline_sql() -> None:
    body = _md_body(
        {
            "columns": ["ts", "host", "sql_text", "n"],
            "rows": [
                {"ts": "2026-10-15 14:00:00", "host": "10.0.0.1", "sql_text": "SELECT 1\nFROM t"},
                {"ts": "2026-10-15 14:01:00", "host": "10.0.0.2", "sql_text": "SELECT 2", "n": 2},
            ],
        }
    )
    assert "| ts | host | sql_text | n |" in body
    assert "| 见下方 第 1 行 | （未提供） |" in body
    assert "SELECT 1\nFROM t" in body
    html = _md_html(body)
    assert html.count("<tr>") == 3  # 表头 + 两行，没有跳出表格
    assert "<td><code>&quot;10.0.0.1&quot;</code></td>" in html
    assert "<td>2</td>" in html


def test_md_table_cells_are_inert_and_reversible() -> None:
    values = [
        "a|b",
        "a\\|b",
        "x`y",
        "<at id=all>",
        "https://invalid.example/path",
        "__strong__",
        {"k": [1, "|", None]},
        2**60,
        "line\u2028sep",
    ]
    body = _md_body({"columns": ["v"], "rows": [{"v": v} for v in values]})
    html = _md_html(body)
    assert html.count("<tr>") == len(values) + 1
    assert "<a href=" not in html and "<strong>" not in html and "<at" not in html
    cells = re.findall(r"<td>(.*?)</td>", html)
    decoded = [
        json.loads(unescape(c.removeprefix("<code>").removesuffix("</code>"))) for c in cells
    ]
    assert decoded == values


def test_md_table_headers_and_detail_titles_are_inert_and_reversible() -> None:
    # SQLGuard 接受带引号的别名，列名可含换行、链接与 Markdown 结构，不能指望上游拦截。
    names = [
        "x\n\n[click](https://example.invalid)\n\ny",
        " lead ",
        "a`b",
        "a|b",
        "z\n\n- injected",
        "ok",
    ]
    row = {name: "1" for name in names}
    row["z\n\n- injected"] = "multi\nline"  # 触发表下明细标题
    body = _md_body({"columns": names, "rows": [row]})
    html = _md_html(body)
    assert html.count("<tr>") == 2
    assert "<a href=" not in html and "<li>" not in html
    headers = re.findall(r"<th>(.*?)</th>", html)
    decoded = [
        json.loads(unescape(h.removeprefix("<code>").removesuffix("</code>")))
        if h.startswith("<code>")
        else h
        for h in headers
    ]
    assert decoded == names
    assert "见下方 第 1 行" in body and "multi\nline" in body


def test_md_detail_keeps_strings_that_display_would_rewrite_lossless() -> None:
    # \r 与控制符经显示转义后会与字面反斜杠文本撞形，改用 JSON 围栏保持可逆。
    values = ["x\r\ny", "x\\r\ny", "s" * 130 + "\x00", "s" * 130 + "\\u0000"]
    body = _md_body({"columns": ["v"], "rows": [{"v": v} for v in values]})
    fences = [t for t in MarkdownIt("default").enable("table").parse(body) if t.type == "fence"]
    # 表下明细按行顺序排在分析章节之前。
    decoded = [
        json.loads(t.content) if t.info == "json" else t.content.removesuffix("\n")
        for t in fences[: len(values)]
    ]
    assert decoded == values


def test_md_pipe_names_stay_reversible_in_headers_and_outside_tables() -> None:
    # 独立审查（5fb2b30）：竖线转义只属于表格；表外字段标题与明细标题不能多出反斜杠。
    body = _md_body({"columns": ["a|b"], "rows": [{"a|b": "multi\nline"}], "c|d": 3})
    html = _md_html(body)
    (header,) = re.findall(r"<th>(.*?)</th>", html)
    assert json.loads(unescape(header.removeprefix("<code>").removesuffix("</code>"))) == "a|b"
    paragraphs = re.findall(r"<p>(.*?)</p>", html, re.S)
    labels = [unescape(c) for p in paragraphs for c in re.findall(r"<code>(.*?)</code>", p)]
    assert [json.loads(label) for label in labels if label.startswith('"')] == ["c|d", "a|b"]
