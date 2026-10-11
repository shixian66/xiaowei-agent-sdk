"""只格式化模型说明；事实、来源、SQL/DDL 原值不进入 Markdown 解析器。

Lark Markdown 特殊字符按官方参考编码为 HTML 数字实体；仅代码生成的加粗、列表和换行
保留语法。含代码或反斜杠的说明整段保留原文 plain_text，避免规范化或围栏逃逸。
解析只在容量分配结束后执行一次。
"""

import string
from typing import Any, Final

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
        if token.type in {"strong_open", "strong_close"}:
            parts.append("**")
        elif token.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        else:
            parts.append((token.content or token.markup).translate(_LITERAL))
    return "".join(parts)


def format_prose(text: str) -> list[dict[str, Any]]:
    """把已经 display_text 处理过的模型文字变成受限组件；容量不足可保留调用方的纯文字卡片。"""
    plain = [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]
    if len(text) > MAX_FORMAT_CHARS or "\\" in text:
        return plain
    tokens = _PARSER.parse(text)
    if sum(1 + len(t.children or ()) for t in tokens) > MAX_FORMAT_TOKENS:
        return plain
    # Markdown 会规范化代码的换行、首尾空格和缩进；保留原输入，不能由 token 重建。
    if any(
        t.type in {"fence", "code_block"} or any(c.type == "code_inline" for c in t.children or ())
        for t in tokens
    ):
        return plain
    if not any(
        t.type in {"heading_open", "bullet_list_open", "ordered_list_open"}
        or any(c.type == "strong_open" for c in t.children or ())
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
