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
        "font-size:0",
        "font-size:0px",
        "font-size:0.001rem",
        "color:transparent",
        "color:rgba(0,0,0,0)",
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

    body = markdown_content("~~~").body + "\n\n## 系统事实\n\n原值\n"
    tokens = MarkdownIt().parse(body)
    assert any(t.type == "inline" and t.content == "系统事实" for t in tokens)


def test_unclosed_nested_markdown_fence_is_not_rewritten_into_a_new_fence() -> None:
    from xiaowei.report_content import markdown_content

    with pytest.raises(ReportContentError):
        markdown_content("> ~~~sql\n> SELECT 1")
    source = "> ~~~sql\n> SELECT 1\n> ~~~"
    assert markdown_content(source).body == source


def test_markdown_reference_definitions_in_code_remain_literal() -> None:
    from xiaowei.report_content import markdown_content

    source = "~~~md\n[asset]: https://invalid.example/example\n~~~"
    assert markdown_content(source).body == source


@pytest.mark.parametrize("source", ["~~~\r\n正文\r~~~", "- 项目\n    ~~~\n    正文\n    ~~~"])
def test_closed_markdown_fences_keep_their_original_content(source: str) -> None:
    from xiaowei.report_content import markdown_content

    assert markdown_content(source).body == source


@pytest.mark.parametrize(
    "source",
    [
        "# 标题\r\n\r\nARRAY<INT>\r\n\r\n## 结论\r\n\r\n继续观察",
        "> [a]: https://invalid.example/a\n> [a]: https://invalid.example/b",
        "- 注意\n  - ![图](https://invalid.example/a)\n\n继续观察",
        "<script>~~~\nalert(1)\n~~~</script>",
        "<script>\n~~~\n![图](https://invalid.example/image)\n~~~\n</script>",
        "前言\u2028ARRAY<INT>\x0b正文\n\n结论",
    ],
)
def test_markdown_literal_blocks_keep_source_and_cannot_capture_following_fact(source: str) -> None:
    from markdown_it import MarkdownIt

    from xiaowei.report_content import prepare_content

    prepared = prepare_content(source, "md")
    final = prepared.body + "\n\n## 系统事实\n\n获准事实\n"
    env_: dict = {}
    tokens = MarkdownIt("commonmark", {"html": True}).parse(final, env_)
    assert prepared.changed and prepared.text == source
    assert not env_.get("references")
    assert any(t.type == "inline" and t.content == "系统事实" for t in tokens)
    assert not any(
        t.type in {"html_block", "html_inline", "link_open", "image"}
        or any(c.type in {"html_inline", "link_open", "image"} for c in t.children or ())
        for t in tokens
    )


@pytest.mark.parametrize("type_name", ["ARRAY<INT>", "MAP<STRING,INT>", "List<String>"])
def test_escaped_html_type_names_keep_all_text_even_inside_code(type_name: str) -> None:
    from html import escape

    prepared = prepare(f"<p>列类型 <code>{escape(type_name)}</code></p>")
    assert prepared.text == "列类型 " + type_name and not prepared.changed
