"""群 post 的有界白名单解析；只返回文字与一张原消息图片的资源键，不进行 I/O。"""

import re
from dataclasses import dataclass
from typing import Any, Literal

from markdown_it import MarkdownIt

_NOTICES = {
    "invalid": "这条富文本未处理：结构或文字不合法，请改为文字或单张 PNG/JPEG 图文。",
    "unsupported": (
        "这条富文本未处理：包含不支持的媒体、附件或节点，请改为文字或单张 PNG/JPEG 图文。"
    ),
    "images": "这条图文未处理：只支持一张图片，请分次发送。",
    "image_key": "这条图文未处理：图片资源标识不合法，请重新发送原图。",
    "too_long": "这条富文本未处理：内容超过现有输入上限，请缩短后重新发送。",
}


class PostInputError(Exception):
    """仅携带固定说明；不回显正文、标签值或资源键。"""

    def __init__(
        self, code: Literal["invalid", "unsupported", "images", "image_key", "too_long"]
    ) -> None:
        super().__init__(_NOTICES[code])
        self.notice = _NOTICES[code]


@dataclass(frozen=True)
class PostInput:
    text: str
    image_key: str | None


def _string(node: dict[str, Any], key: str, default: str = "") -> str:
    value = node.get(key, default)
    if not isinstance(value, str):
        raise PostInputError("invalid")
    return value


def _text_node(node: dict[str, Any], bot_mentions: frozenset[str]) -> str:
    tag = node.get("tag")
    if tag == "text":
        return _string(node, "text")
    if tag == "a":
        return " ".join(value for value in (_string(node, "text"), _string(node, "href")) if value)
    if tag == "at":
        user = _string(node, "user_id")
        if not user:
            raise PostInputError("invalid")
        if user in bot_mentions:
            return ""
        name = _string(node, "user_name")
        return "@" + name if name else user
    if tag == "code_block":
        return f"```{_string(node, 'language')}\n{_string(node, 'text')}\n```"
    if tag == "hr":
        return "---"
    if tag == "emotion":
        return f":{_string(node, 'emoji_type')}:"
    if tag == "md":
        text = _string(node, "text")
        tokens = list(MarkdownIt().parse(text))
        while tokens:
            token = tokens.pop()
            if token.type in {"image", "html_inline", "html_block"}:
                raise PostInputError("unsupported")
            tokens.extend(token.children or [])
        return text
    raise PostInputError("unsupported")


def parse_post(
    content: dict[str, Any], *, bot_mentions: frozenset[str], max_chars: int
) -> PostInput:
    """选首个文档；有 v2 即选 v2，同组段落同时取得正文和结构化图片。"""
    if "files" in content:
        raise PostInputError("unsupported")
    if "content" in content or "content_v2" in content:
        doc = content
    else:
        doc = next((value for value in content.values() if isinstance(value, dict)), {})
    if "files" in doc:
        raise PostInputError("unsupported")
    paragraphs = doc.get("content_v2") if "content_v2" in doc else doc.get("content")
    if not isinstance(paragraphs, list):
        raise PostInputError("invalid")
    lines = [_string(doc, "title")]
    image_key = None
    for paragraph in paragraphs:
        if not isinstance(paragraph, list):
            raise PostInputError("invalid")
        chunks = []
        for node in paragraph:
            if not isinstance(node, dict):
                raise PostInputError("invalid")
            if node.get("tag") == "img":
                key = node.get("image_key")
                if not isinstance(key, str) or re.fullmatch(r"[0-9A-Za-z_-]{1,200}", key) is None:
                    raise PostInputError("image_key")
                if image_key is not None:
                    raise PostInputError("images")
                image_key = key
            else:
                chunks.append(_text_node(node, bot_mentions))
        lines.append("".join(chunks))
    text = "\n".join(line for line in lines if line).strip()
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise PostInputError("invalid") from None
    if len(text) > max_chars:
        raise PostInputError("too_long")
    if not text and image_key is None:
        raise PostInputError("invalid")
    return PostInput(text=text, image_key=image_key)
