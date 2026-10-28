"""DS 临时日志的有界字面检索和原文定位；不做模型推理或长期索引。"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LogSample:
    raw: bytes
    task: dict[str, object]
    captured_at: datetime
    received_bytes: int
    reached_end: bool
    truncation_reason: str | None


_SECRET_BYTES = re.compile(
    rb"(?i)(\b(?:password|passwd|pwd|token|secret|api[_-]?key|authorization)\s*[:=]\s*)"
    rb"(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_AUTH_BYTES = re.compile(rb"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/=_\-.]+")
_URL_BYTES = re.compile(rb"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+:[^\s/@]+@")


def redact_bytes(raw: bytes, token: str) -> bytes:
    """采集时遮蔽完整值并保留原字节长度；片段从值中间开始也不会漏出后半段。"""
    if token:
        encoded = token.encode()
        raw = raw.replace(encoded, b"*" * len(encoded))
    for pattern in (_AUTH_BYTES, _URL_BYTES, _SECRET_BYTES):
        raw = pattern.sub(
            lambda m: m.group(1) + b"*" * (m.end() - m.start() - len(m.group(1))), raw
        )
    return raw


def fragment(raw: bytes, start: int, end: int, token: str) -> dict[str, object]:
    line_start = raw.rfind(b"\n", 0, start) + 1
    text = raw[start:end].decode("utf-8", errors="replace")
    return {
        "line": raw.count(b"\n", 0, start) + 1,
        "byte_offset": start - line_start,
        "byte_start": start,
        "byte_end": end,
        "text": text,
        "utf8_replacements": "\ufffd" in text,
    }


async def search_log(
    raw: bytes, args: dict[str, object], token: str
) -> tuple[list[dict[str, object]], dict[str, int]]:
    terms = args["terms"]
    assert isinstance(terms, list)  # noqa: S101 - normalized arguments
    maximum, context = int(str(args["max_snippets"])), int(str(args["context_lines"]))
    counts, representatives = {}, []
    for term in terms:
        text = str(term)
        pattern = re.compile(
            re.escape(text.encode()), 0 if args["case_sensitive"] else re.IGNORECASE
        )
        positions: list[tuple[int, int]] = []
        count = 0
        for match in pattern.finditer(raw):
            count += 1
            if len(positions) < maximum:
                positions.append((match.start(), match.end()))
            if count % 4096 == 0:
                await asyncio.sleep(0)
        counts[text] = count
        representatives.append(positions)
        await asyncio.sleep(0)
    # 每词轮流取代表，通用 ERROR 噪声不能挤掉另一个词的数据库异常。
    hits = [
        positions[n] for n in range(maximum) for positions in representatives if n < len(positions)
    ]
    spans: list[tuple[int, int]] = []
    for position, match_end in hits:
        left = raw.rfind(b"\n", 0, position) + 1
        right = raw.find(b"\n", match_end)
        right = len(raw) if right < 0 else right + 1
        for _ in range(context):
            left = raw.rfind(b"\n", 0, max(0, left - 1)) + 1
            found = raw.find(b"\n", right)
            right = len(raw) if found < 0 else found + 1
        left = max(left, position - 512)
        right = min(right, max(match_end, left + 2048))
        if any(a <= position and match_end <= b for a, b in spans):
            continue
        spans.append((left, right))
        if len(spans) >= maximum:
            break
    return [fragment(raw, a, b, token) for a, b in spans], counts


async def read_log(raw: bytes, args: dict[str, object], token: str) -> list[dict[str, object]]:
    start = 0
    for n in range(1, int(str(args["line"]))):
        found = raw.find(b"\n", start)
        if found < 0:
            return []
        start = found + 1
        if n % 4096 == 0:
            await asyncio.sleep(0)
    line_end = raw.find(b"\n", start)
    line_end = len(raw) if line_end < 0 else line_end
    start = min(start + int(str(args["byte_offset"])), line_end)
    end = start
    for _ in range(int(str(args["line_count"]))):
        found = raw.find(b"\n", end)
        end = len(raw) if found < 0 else found + 1
    end = min(end, start + 8192)
    return [fragment(raw, start, end, token)] if start < len(raw) else []
