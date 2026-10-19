"""Static prose safety; business facts bypass this processor."""

import pytest

from xiaowei.report_content import ReportContentError


def prepare(source: str):
    from xiaowei.report_content import prepare_content

    return prepare_content(source, "html")


def test_html_keeps_design_and_literal_sql_without_layout_source_preview() -> None:
    content = prepare(
        '<section style="background-color:#14243a;color:white;display:grid;gap:12px">'
        "<h1>巡检</h1><pre><code>SELECT a&lt;b &amp; 1; &lt;table&gt;</code></pre></section>"
    )
    assert "display:grid" in content.body and "<h1>巡检</h1>" in content.body
    assert "SELECT a<b & 1; <table>" in content.text
    assert "<section" not in content.text and "style=" not in content.text
    assert not content.changed


@pytest.mark.parametrize(
    "style",
    [
        "position:fixed;inset:0",
        "margin:-999999px",
        "width:999999999px",
        "color:expression(alert(1))",
        "background-image:url(https://invalid.example/x)",
        "color:var(--forged)",
        "font-size:999999px",
        "display:none",
        "opacity:0",
        "transform:scale(9999)",
        "padding:999999rem",
        "color:red!important",
    ],
)
def test_html_removes_unbounded_or_active_css_values(style: str) -> None:
    content = prepare(f'<div style="{style}">保留正文</div>')
    assert content.text == "保留正文"
    assert style not in content.body
    assert content.changed


def test_html_closes_fragment_and_strips_resources_and_identity_attributes() -> None:
    content = prepare(
        '<div id="facts" class="card" onclick="bad()"><b>正文<img src="file:///private/nope">'
        '<iframe src="https://invalid.example">hidden</iframe><script>bad()</script>'
    )
    assert content.text == "正文"
    for forbidden in ("id=", "class=", "onclick", "<img", "<iframe", "<script", "bad()", "file:"):
        assert forbidden not in content.body
    assert content.body.endswith("</b></div>") and content.changed


def test_html_refuses_an_empty_readable_body() -> None:
    with pytest.raises(ReportContentError):
        prepare('<script>bad()</script><img src="https://invalid.example/x">')


def test_bogus_declaration_is_inert_and_keeps_readable_content() -> None:
    content = prepare("<![BROKEN]>正文")
    assert content.body == content.text == "正文"
    assert "BROKEN" not in content.body


def test_canonicalization_preserves_content_without_spurious_warning() -> None:
    content = prepare('<P style="color: red"><b>正文')
    assert content.text == "正文" and content.body.endswith("</b></p>")
    assert not content.changed


def test_normal_shadow_color_keeps_its_design() -> None:
    content = prepare('<p style="box-shadow:0 8px 24px rgba(20,36,58,0.2)">正文</p>')
    assert "box-shadow" in content.body and not content.changed


def test_opening_only_markdown_fence_cannot_capture_next_fact() -> None:
    from markdown_it import MarkdownIt

    from xiaowei.report_content import markdown_content

    body = markdown_content("~~~") + "\n\n## 系统事实\n\n原值\n"
    tokens = MarkdownIt().parse(body)
    assert any(t.type == "inline" and t.content == "系统事实" for t in tokens)


def test_unclosed_nested_markdown_fence_is_not_rewritten_into_a_new_fence() -> None:
    from xiaowei.report_content import markdown_content

    with pytest.raises(ReportContentError):
        markdown_content("> ~~~sql\n> SELECT 1")
    assert markdown_content("> ~~~sql\n> SELECT 1\n> ~~~")
