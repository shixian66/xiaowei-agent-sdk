"""Static model prose only; trusted Evidence values never pass through this module."""

import math
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Literal

import nh3
import tinycss2
from markdown_it import MarkdownIt
from tinycss2.color3 import parse_color

FORMAT_NOTICE = "部分不支持的格式已移除"


@dataclass(frozen=True)
class ReportContent:
    body: str
    text: str
    changed: bool = False


class ReportContentError(ValueError):
    """A report cannot preserve readable content within its static format contract."""


def markdown_content(value: str) -> str:
    """Keep native Markdown, rejecting active constructs outside literal code."""
    tokens = MarkdownIt("commonmark", {"html": True}).enable("table").parse(value)
    forbidden = {"html_block", "html_inline", "image", "link_open"}
    if any(
        token.type in forbidden or any(c.type in forbidden for c in token.children or ())
        for token in tokens
    ):
        raise ReportContentError("报告不支持原始 HTML、图片或链接，请使用文字或代码围栏")
    # An unterminated model fence must not capture the following code-owned fact block.
    quotes = 0
    lines = value.splitlines()
    for token in tokens:
        if token.type == "blockquote_open":
            quotes += 1
        elif token.type == "blockquote_close":
            quotes -= 1
        if token.type == "fence" and token.map:
            final = lines[token.map[1] - 1].strip()
            for _ in range(quotes):
                final = final.removeprefix(">").lstrip()
            closed = (
                token.map[1] - token.map[0] >= 2
                and len(final) >= len(token.markup)
                and set(final) == {token.markup[0]}
            )
            if not closed:
                if token.level:
                    raise ReportContentError("列表或引用中的代码围栏须完整闭合")
                value = value.rstrip() + "\n" + token.markup + "\n"
    return value


_TAGS = {
    "section",
    "article",
    "div",
    "span",
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "blockquote",
    "pre",
    "code",
    "strong",
    "b",
    "em",
    "i",
    "s",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "caption",
    "br",
    "hr",
}
_BLOCKS = _TAGS - {"span", "code", "strong", "b", "em", "i", "s"}
_REMOVED_CONTENT = {"script", "style", "iframe", "object", "embed", "svg", "math", "head"}
_KEYWORDS = {
    "display": {"block", "inline", "inline-block", "flex", "grid"},
    "flex-direction": {"row", "column", "row-reverse", "column-reverse"},
    "flex-wrap": {"wrap", "nowrap"},
    "justify-content": {
        "start",
        "end",
        "center",
        "flex-start",
        "flex-end",
        "space-between",
        "space-around",
        "space-evenly",
    },
    "align-items": {"start", "end", "center", "stretch", "baseline", "flex-start", "flex-end"},
    "text-align": {"left", "right", "center", "start", "end", "justify"},
    "font-style": {"normal", "italic"},
    "font-weight": {"normal", "bold", "bolder", "lighter"},
    "white-space": {"normal", "pre-wrap"},
    "overflow-wrap": {"normal", "anywhere", "break-word"},
    "word-break": {"normal", "break-all", "keep-all"},
    "border-style": {"solid", "dashed", "dotted", "double", "none"},
    "text-decoration": {"none", "underline", "line-through"},
}
_COLORS = {"color", "background-color", "border-color"}
_SPACING = {
    "margin",
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
    "padding",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
    "gap",
    "row-gap",
    "column-gap",
    "border-radius",
    "border-width",
    "letter-spacing",
}
_LAYOUT = {
    "width",
    "max-width",
    "min-width",
    "grid-template-columns",
    "flex",
    "flex-basis",
    "flex-grow",
    "flex-shrink",
}
_PROPERTIES = (
    _COLORS
    | _SPACING
    | _LAYOUT
    | set(_KEYWORDS)
    | {"font-size", "line-height", "font-family", "box-shadow", "border"}
)
_FONTS = {
    "system-ui",
    "sans-serif",
    "serif",
    "monospace",
    "ui-monospace",
    "ui-sans-serif",
    "arial",
    "helvetica",
    "segoe",
    "ui",
    "apple-system",
    "-apple-system",
}


def _css_tokens(tokens: list[Any], prop: str, depth: int = 0) -> bool:
    """Bound parsed values, not CSS source strings; no resource or variable functions."""
    if depth > 3 or len(tokens) > 64:
        return False
    for token in tokens:
        kind = token.type
        if kind == "whitespace":
            continue
        if kind == "ident":
            allowed = _KEYWORDS.get(prop, set()) | {
                "auto",
                "normal",
                "solid",
                "none",
                "auto-fit",
                "auto-fill",
            }
            if (
                token.lower_value not in allowed
                and not (prop == "font-family" and token.lower_value in _FONTS)
                and not (prop in {"border", "box-shadow"} and parse_color(token) is not None)
            ):
                return False
        elif kind == "string":
            if prop != "font-family" or len(token.value) > 64:
                return False
        elif kind == "hash":
            if prop not in {"border", "box-shadow"} or parse_color(token) is None:
                return False
        elif kind in {"number", "dimension", "percentage"}:
            number = token.value
            if not math.isfinite(number) or number < 0:
                return False
            if kind == "number":
                maximum = 900 if prop == "font-weight" else 3 if prop == "line-height" else 12
                if number > maximum:
                    return False
            elif kind == "percentage":
                if number > (100 if prop in _LAYOUT else 5):
                    return False
            else:
                unit = token.lower_unit
                # Root-relative units avoid unbounded compounding of nested font sizes.
                if unit == "fr" and prop == "grid-template-columns":
                    if number > 12:
                        return False
                    continue
                if unit not in {"px", "rem"}:
                    return False
                pixels = number * (16 if unit == "rem" else 1)
                bound = (
                    64 if prop in {"font-size", "box-shadow"} else 1280 if prop in _LAYOUT else 128
                )
                if pixels > bound:
                    return False
        elif kind == "literal":
            if token.value not in {",", "/"}:
                return False
        elif kind == "function":
            if token.lower_name in {"rgb", "rgba", "hsl", "hsla"}:
                if parse_color(token) is None:
                    return False
                continue
            if token.lower_name not in {
                "repeat",
                "minmax",
            } or not _css_tokens(token.arguments, prop, depth + 1):
                return False
        else:
            return False
    return True


def _css(value: str) -> tuple[str, bool]:
    safe: list[str] = []
    changed = False
    for declaration in tinycss2.parse_declaration_list(
        value, skip_comments=True, skip_whitespace=True
    ):
        if declaration.type != "declaration":
            changed = True
            continue
        name = declaration.lower_name
        valid = name in _PROPERTIES and not declaration.important
        if valid and name in _COLORS:
            valid = parse_color(tinycss2.serialize(declaration.value).strip()) is not None
        elif valid:
            valid = _css_tokens(declaration.value, name)
            if name in _KEYWORDS and name != "font-weight":
                valid = valid and all(
                    t.type == "whitespace"
                    or (t.type == "ident" and t.lower_value in _KEYWORDS[name])
                    for t in declaration.value
                )
        if valid:
            safe.append(f"{name}:{tinycss2.serialize(declaration.value).strip()}")
        else:
            changed = True
    return ";".join(safe), changed


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.removed = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCKS:
            self.parts.append("\n")
        if tag not in _TAGS or any(name != "style" for name, _ in attrs):
            self.removed = True

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def prepare_content(value: str, format_: Literal["md", "html"]) -> ReportContent:
    """Prepare a model block once for both file bytes and readable message preview."""
    if format_ == "md":
        return ReportContent(markdown_content(value), value)
    inspector = _Text()
    inspector.feed(value)
    changed = inspector.removed

    def attribute(tag: str, name: str, raw: str) -> str | None:
        nonlocal changed
        if name != "style":
            changed = True
            return None
        style, removed = _css(raw)
        changed |= removed
        return style or None

    try:
        body = nh3.clean(
            value,
            tags=_TAGS,
            clean_content_tags=_REMOVED_CONTENT,
            attributes={"*": {"style"}},
            attribute_filter=attribute,
            link_rel=None,
        )
        parser = _Text()
        parser.feed(body)
    except (ValueError, RecursionError) as exc:
        raise ReportContentError("报告正文无法安全处理，请简化格式") from exc
    plain = "".join(parser.parts).strip()
    if not plain:
        raise ReportContentError("报告清理后没有可读正文，请使用静态文字")
    return ReportContent(body, plain, changed)
