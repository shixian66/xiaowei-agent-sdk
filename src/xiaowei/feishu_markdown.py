"""只格式化模型说明；事实、来源、SQL/DDL 原值不进入 Markdown 解析器。

Lark Markdown 特殊字符按官方参考编码为 HTML 数字实体；仅代码生成的加粗、列表和换行
保留语法。代码、引用或反斜杠所在的局部保留原文 plain_text，避免规范化或围栏逃逸。
解析输入沿既有展示上限有界；先检查完整 post，再退回完整卡片或既有预览路径。
"""

import html
import string
from typing import Any, Final

from lark_channel.channel import markdown_to_post_ast
from markdown_it import MarkdownIt
from markdown_it.token import Token

# 沿用 FeishuConfig 的 3500 字符展示上限；超长输入只退回纯文字，不减少事实或改执行预算。
MAX_FORMAT_CHARS: Final = 3500
MAX_FORMAT_TOKENS: Final = 4 * MAX_FORMAT_CHARS
_LITERAL = str.maketrans({c: f"&#{ord(c)};" for c in string.punctuation})
_PARSER = MarkdownIt("zero", {"maxNesting": 16}).enable(
    [
        "normalize",
        "block",
        "inline",
        "paragraph",
        "heading",
        "list",
        "blockquote",
        "fence",
        "code",
        "text",
        "escape",
        "newline",
        "backticks",
        "emphasis",
    ]
)


def _inline(tokens: list[Token]) -> str:
    parts: list[str] = []
    for token in tokens:
        if token.type in {"strong_open", "strong_close"} and token.markup == "**":
            parts.append("**")
        elif token.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        else:
            parts.append((token.content or token.markup).translate(_LITERAL))
    return "".join(parts)


def _literal_block(text: str, tokens: list[Token]) -> bool:
    return "\\" in text or any(
        t.type in {"fence", "code_block", "blockquote_open"}
        or any(c.type == "code_inline" for c in t.children or ())
        for t in tokens
    )


def _format_tokens(text: str, tokens: list[Token]) -> list[dict[str, Any]]:
    plain = [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]
    # Markdown 会规范化代码的换行、首尾空格和缩进；保留原输入，不能由 token 重建。
    if _literal_block(text, tokens):
        return plain
    if not any(
        t.type in {"heading_open", "bullet_list_open", "ordered_list_open"}
        or any(c.type == "strong_open" and c.markup == "**" for c in t.children or ())
        for t in tokens
    ):
        return plain
    elements: list[dict[str, Any]] = []
    parts: list[str] = []
    lists: list[int | None] = []

    def flush() -> None:
        content = "".join(parts).rstrip("\n")
        if content:
            elements.append({"tag": "markdown", "content": content})
        parts.clear()

    for token in tokens:
        kind = token.type
        if kind == "heading_open" or kind == "heading_close":
            parts.append("**" if kind == "heading_open" else "**\n\n")
        elif kind == "bullet_list_open":
            lists.append(None)
        elif kind == "ordered_list_open":
            lists.append(int(token.attrGet("start") or 1))
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
            if not lists:
                parts.append("\n")
        elif kind == "list_item_open":
            parts.append("  " * (len(lists) - 1))
            number = lists[-1]
            parts.append("- " if number is None else f"{number}. ")
            if number is not None:
                lists[-1] = number + 1
        elif kind == "inline":
            parts.append(_inline(token.children or []))
        elif kind == "paragraph_close":
            parts.append("\n" if lists else "\n\n")
    flush()
    return elements or plain


def _prose_elements(text: str, *, local: bool = True) -> list[dict[str, Any]] | None:
    """只解析一次；以顶层语法块的原始行范围隔离不支持的局部，不跨事实块解析。"""
    if len(text) > MAX_FORMAT_CHARS:
        return None
    tokens = _PARSER.parse(text)
    if sum(1 + len(t.children or ()) for t in tokens) > MAX_FORMAT_TOKENS:
        return None
    if not _literal_block(text, tokens):
        return _format_tokens(text, tokens)
    if not local:
        return None  # SDK post 的文本降级会改变代码空白，继续使用卡片。
    lines = text.splitlines(keepends=True)
    starts = [(i, t.map[0]) for i, t in enumerate(tokens) if t.level == 0 and t.map]
    if not starts:
        return _format_tokens(text, tokens)
    elements: list[dict[str, Any]] = []
    for position, (index, line) in enumerate(starts):
        end, end_line = (
            starts[position + 1] if position + 1 < len(starts) else (len(tokens), len(lines))
        )
        raw = "".join(lines[0 if position == 0 else line : end_line])
        elements.extend(_format_tokens(raw, tokens[index:end]))
    return elements


def format_prose(text: str) -> list[dict[str, Any]]:
    """卡片只格式化白名单说明，代码/超长输入仍保留原文。"""
    return _prose_elements(text) or [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]


def prose_post(text: str) -> dict[str, Any] | None:
    """复用官方 structured 转换；原生 md 在 1.4.0 的文本降级中会被丢弃。

    仅把白名单生成的 Markdown 交给 SDK，转换后逐 text 值解码一次实体。链接、@、
    HTML 和标识符始终是字面文字。代码、反斜杠或末尾空白继续用原文卡片，避免降级
    的 strip/Markdown 规范化改变可复制内容。预建 AST 不进入 SDK 自动分段。
    """
    elements = _prose_elements(text, local=False)
    if elements is None or text != text.rstrip():
        return None
    rows: list[list[dict[str, Any]]] = []
    for element in elements:
        if element["tag"] == "div":
            rows.append([{"tag": "text", "text": element["text"]["content"]}])
            continue
        # SDK structured 的列表转换会剥掉缩进；层级承载约束时沿原卡片展示。
        if any(line.startswith((" ", "\t")) for line in element["content"].splitlines()):
            return None
        converted = markdown_to_post_ast(element["content"], tag_md_mode="structured")
        for row in converted["zh_cn"]["content"]:
            for node in row:
                if node["tag"] != "text" or any(s != "bold" for s in node.get("style", ())):
                    return None
                node["text"] = html.unescape(node["text"])
            rows.append(row)
    return {"post": {"zh_cn": {"title": "", "content": rows}}}
