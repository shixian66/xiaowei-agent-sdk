"""由已验证的飞书投影生成静态报告；不读取模型原始输出或外部资源。"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import Literal

from xiaowei.feishu_render import display_text, fact_limit_notices
from xiaowei.models import Delivery, DeliveryFact

ReportFormat = Literal["md", "html"]
MAX_REPORT_BYTES = 400_000


class ReportCapacityError(ValueError):
    """报告不能在固定容量内完整交付。"""


@dataclass(frozen=True)
class ReportFile:
    format: ReportFormat
    filename: str
    content: bytes
    digest: str


def _fact_json(fact: DeliveryFact) -> str:
    if fact.result_json is not None:
        return fact.result_json
    return json.dumps(
        {"columns": fact.columns, "rows": fact.rows, "metadata": fact.metadata},
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    )


def _fence(value: str) -> str:
    """动态围栏只能包裹数据，长度严格大于数据中的任意波浪号序列。"""
    width = max([3, *(len(m.group()) + 1 for m in re.finditer(r"~+", value))])
    marker = "~" * width
    return f"{marker}text\n{value}\n{marker}"


def _sections(delivery: Delivery) -> list[tuple[str, str, str]]:
    parts: list[tuple[str, str, str]] = []
    if not delivery.facts:
        parts.append(
            (
                "回答",
                "回答（模型生成，未经系统核实）",
                delivery.feishu_text if delivery.feishu_text is not None else delivery.content,
            )
        )
    for fact in delivery.facts:
        source = (
            f"证据 {fact.evidence_id} · 来源 {fact.tool_id} · 目标 {fact.target_id} · "
            f"采集于 {fact.captured_at.isoformat()}"
        )
        details = []
        if fact.note:
            details.append(f"说明：{fact.note}")
        details.extend(fact_limit_notices(fact))
        # 显式文件是当前权限下的原值交付，不能从历史摘要反向拼接。
        details.append(_fact_json(fact))
        parts.append(("证据", source, "\n".join(details)))
    if delivery.monitoring_notice:
        parts.append(("未核实来源", "未核实来源", delivery.monitoring_notice))
    for inference in delivery.analysis:
        parts.append(
            (
                "分析与建议",
                "分析与建议（模型推断，未经系统核实；依据："
                + "、".join(inference.evidence_ids)
                + "）",
                inference.text,
            )
        )
    if delivery.session_notice:
        parts.append(("会话提示", "会话提示", delivery.session_notice))
    if delivery.basis:
        parts.append(("查看依据", "其他调查", "\n".join(f"{t} · {o}" for t, o in delivery.basis)))
    return parts


def build_report(delivery: Delivery) -> ReportFile:
    """全量构造一个有界 MD 或 HTML 文件；放不下时整体失败，不裁掉事实或限制。"""
    format_: ReportFormat | None = delivery.report_format
    if delivery.channel != "feishu" or format_ not in ("md", "html"):
        raise ValueError("当前交付没有获准的报告格式")
    if delivery.requires_complete:
        raise ValueError("当前回答不能作为只读事实报告交付")
    sections = _sections(delivery)
    if format_ == "md":
        blocks = []
        for index, (heading, title, value) in enumerate(sections, start=1):
            details = display_text(f"{title}\n{value}")
            blocks.append(f"## {heading} {index}\n\n{_fence(details)}")
        body = "# 小维巡检报告\n\n" + "\n\n".join(blocks) + "\n"
    else:
        html_blocks = "".join(
            f"<section><h2>{html.escape(display_text(title), quote=True)}</h2>"
            f"<pre>{html.escape(display_text(value), quote=True)}</pre></section>"
            for _, title, value in sections
        )
        body = (
            '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" '
            "content=\"default-src 'none'; base-uri 'none'; form-action 'none'\">"
            "<title>小维巡检报告</title></head><body><h1>小维巡检报告</h1>"
            + html_blocks
            + "</body></html>"
        )
    content = body.encode("utf-8")
    if len(content) > MAX_REPORT_BYTES:
        raise ReportCapacityError("报告超过当前文件容量")
    return ReportFile(
        format=format_,
        filename=f"xiaowei-report.{format_}",
        content=content,
        digest=hashlib.sha256(content).hexdigest(),
    )
