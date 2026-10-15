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


def _fact_section(fact: DeliveryFact, *, expand_brief: bool) -> tuple[str, str, str]:
    source = (
        f"证据 {fact.evidence_id} · 来源 {fact.tool_id} · 目标 {fact.target_id} · "
        f"采集于 {fact.captured_at.isoformat()}"
    )
    details = []
    if fact.note:
        details.append(f"说明：{fact.note}")
    details.extend(fact_limit_notices(fact))
    if fact.view == "brief" and not expand_brief:
        if fact.summary is None:
            raise ValueError("历史摘要缺失")
        details.append(fact.summary)
    else:
        # 显式文件可在当前权限下展开原值；自动换载体保持所选摘要语义。
        details.append(_fact_json(fact))
    return "证据", source, "\n".join(details)


def _sections(delivery: Delivery, *, expand_brief: bool) -> list[tuple[str, str, str]]:
    parts: list[tuple[str, str, str]] = []
    if not delivery.facts:
        parts.append(
            (
                "回答",
                "回答（模型生成，未经系统核实）",
                delivery.feishu_text if delivery.feishu_text is not None else delivery.content,
            )
        )
    parts.extend(_fact_section(fact, expand_brief=expand_brief) for fact in delivery.facts)
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


def _render(
    format_: ReportFormat,
    sections: list[tuple[str, str, str]],
    *,
    label: str | None = None,
    notice: str | None = None,
) -> bytes:
    document_title = "小维巡检报告" + (f" {label}" if label else "")
    if format_ == "md":
        blocks = []
        for index, (heading, title, value) in enumerate(sections, start=1):
            details = display_text(f"{title}\n{value}")
            blocks.append(f"## {heading} {index}\n\n{_fence(details)}")
        prefix = "# 小维巡检报告" + (f" {label}" if label else "") + "\n\n"
        if notice:
            prefix += f"范围说明\n\n{_fence(display_text(notice))}\n\n"
        body = prefix + "\n\n".join(blocks) + "\n"
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
            f"<title>{html.escape(document_title, quote=True)}</title></head><body>"
            f"<h1>{html.escape(document_title, quote=True)}</h1>"
            + (
                f"<p>范围说明：{html.escape(display_text(notice), quote=True)}</p>"
                if notice
                else ""
            )
            + html_blocks
            + "</body></html>"
        )
    return body.encode("utf-8")


def build_report(delivery: Delivery, *, expand_brief: bool = True) -> ReportFile:
    """全量构造一个有界 MD 或 HTML 文件；放不下时整体失败，不裁掉事实或限制。"""
    format_: ReportFormat | None = delivery.report_format
    if delivery.channel != "feishu" or format_ not in ("md", "html"):
        raise ValueError("当前交付没有获准的报告格式")
    if delivery.requires_complete:
        raise ValueError("当前回答不能作为只读事实报告交付")
    content = _render(format_, _sections(delivery, expand_brief=expand_brief))
    if len(content) > MAX_REPORT_BYTES:
        raise ReportCapacityError("报告超过当前文件容量")
    return ReportFile(
        format=format_,
        filename=f"xiaowei-report.{format_}",
        content=content,
        digest=hashlib.sha256(content).hexdigest(),
    )


def build_reports(delivery: Delivery, *, expand_brief: bool = True) -> tuple[ReportFile, ...]:
    """在编码后容量内，按整章节或整行交付本次获准内容。"""
    try:
        return (build_report(delivery, expand_brief=expand_brief),)
    except ReportCapacityError:
        pass
    format_ = delivery.report_format
    if format_ not in ("md", "html"):
        raise ValueError("当前交付没有获准的报告格式")
    sections = _sections(delivery, expand_brief=expand_brief)
    shared_notice = (
        "\n".join(
            dict.fromkeys(
                [notice for fact in delivery.facts for notice in fact_limit_notices(fact)]
                + ([delivery.monitoring_notice] if delivery.monitoring_notice else [])
            )
        )
        or None
    )

    def pack(estimated_total: int) -> list[list[tuple[str, str, str]]]:
        files: list[list[tuple[str, str, str]]] = []
        current: list[tuple[str, str, str]] = []

        def fits(candidate: list[tuple[str, str, str]]) -> bool:
            return (
                len(
                    _render(
                        format_,
                        candidate,
                        label=f"{len(files) + 1}/{estimated_total}",
                        notice=shared_notice,
                    )
                )
                <= MAX_REPORT_BYTES
            )

        def add(section: tuple[str, str, str]) -> bool:
            nonlocal current
            if fits([*current, section]):
                current.append(section)
                return True
            if current:
                files.append(current)
                current = []
            if fits([section]):
                current.append(section)
                return True
            return False

        for index, section in enumerate(sections):
            if add(section):
                continue
            fact = delivery.facts[index] if index < len(delivery.facts) else None
            if fact is None or (fact.view == "brief" and not expand_brief):
                raise ReportCapacityError("完整章节超过当前文件容量")
            try:
                original = json.loads(_fact_json(fact))
            except (ValueError, TypeError) as exc:
                raise ReportCapacityError("证据无法按整行拆分") from exc
            if not isinstance(original, dict) or not isinstance(original.get("rows"), list):
                raise ReportCapacityError("证据无法按整行拆分")
            rows = original["rows"]
            if not rows:
                raise ReportCapacityError("完整章节超过当前文件容量")
            start = 0
            while start < len(rows):
                low, high, best = start + 1, len(rows), None
                while low <= high:
                    end = (low + high) // 2
                    chunk = {**original, "rows": rows[start:end]}
                    try:
                        value = json.dumps(
                            chunk, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                        )
                    except (TypeError, ValueError) as exc:
                        raise ReportCapacityError("证据无法按整行拆分") from exc
                    fragment = _fact_section(
                        fact.model_copy(update={"result_json": value}), expand_brief=expand_brief
                    )
                    fragment = (
                        fragment[0],
                        fragment[1] + f" · 行 {start + 1}–{end}/{len(rows)}",
                        fragment[2],
                    )
                    if fits([*current, fragment]):
                        best = end, fragment
                        low = end + 1
                    else:
                        high = end - 1
                if best is None:
                    if current:
                        files.append(current)
                        current = []
                        continue
                    raise ReportCapacityError("单行或必要字段超过当前文件容量")
                end, fragment = best
                current.append(fragment)
                start = end
        if current:
            files.append(current)
        return files

    estimate = 2
    while True:
        grouped = pack(estimate)
        if len(grouped) <= estimate:
            break
        estimate = len(grouped)
    total = len(grouped)
    reports = []
    for index, group in enumerate(grouped, 1):
        content = _render(
            format_,
            group,
            label=f"{index}/{total}" if total > 1 else None,
            notice=shared_notice if total > 1 else None,
        )
        if len(content) > MAX_REPORT_BYTES:
            raise ReportCapacityError("报告超过当前文件容量")
        reports.append(
            ReportFile(
                format_,
                (
                    f"xiaowei-report-{index}-of-{total}.{format_}"
                    if total > 1
                    else f"xiaowei-report.{format_}"
                ),
                content,
                hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(reports)
