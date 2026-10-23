"""由已验证的飞书投影生成静态报告；不读取模型原始输出或外部资源。"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, replace
from typing import Literal

from xiaowei.feishu_render import (
    business_fact,
    display_text,
    fact_limit_notices,
    fact_scope,
    fold_details,
)
from xiaowei.models import Delivery, DeliveryFact, DeliveryFactRef
from xiaowei.report_content import FORMAT_NOTICE, ReportContent, prepare_content

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


@dataclass(frozen=True)
class _Section:
    kind: Literal["fact", "answer", "analysis", "notice", "session", "basis"]
    title: str
    value: str
    meta: str | None = None
    notices: tuple[str, ...] = ()
    fact: DeliveryFact | None = None
    first_row: int = 1
    native: bool = False
    business: bool = False


_STYLE = """
:root{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#172338;background:#f4f7fb}
*{box-sizing:border-box}body{margin:0;line-height:1.65}main,.hero-inner{max-width:1100px;margin:auto}
.hero{background:linear-gradient(125deg,#122c52,#17617c);color:#fff;padding:42px 24px 36px}
.eyebrow{font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;opacity:.82}
h1{font-size:clamp(28px,4vw,42px);line-height:1.2;margin:10px 0 12px}
h2{font-size:20px;line-height:1.35;margin:0}
.summary{opacity:.82;margin:0}
.part{display:inline-block;background:#ffffff25;border:1px solid #ffffff55;border-radius:99px}
.part{padding:2px 10px;margin-left:8px}
main{padding:24px}
.card{background:#fff;border:1px solid #dce5ef;border-radius:16px;margin:0 0 18px}
.card{padding:24px;box-shadow:0 8px 25px #1723380a}
.card-head{display:flex;gap:14px;align-items:flex-start}
.number{color:#106b83;background:#e7f5f6;border-radius:9px;padding:3px 9px}
.number{font-weight:700;font-size:13px}
.meta{color:#52647b;font-size:13px;overflow-wrap:anywhere;margin:9px 0 16px}
.notice{background:#fff5e7;border-left:3px solid #d98922;padding:10px 14px}
.notice{border-radius:6px;margin:12px 0;color:#5d3d13}
.prose{white-space:pre-wrap;overflow-wrap:anywhere;margin:14px 0}
.field-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.field-grid{gap:10px;margin:14px 0}
.field{background:#f5f8fc;border:1px solid #e5ebf2;border-radius:9px;padding:10px 12px;min-width:0}
.field-name{color:#52647b;font-size:12px;margin-bottom:4px;overflow-wrap:anywhere}
.field-value,.cell{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.field-value,.cell{font-size:12px;overflow-wrap:anywhere;white-space:pre-wrap}
.field.full{grid-column:1/-1}
.sql-code,.raw-code{margin:12px 0 0;background:#14243a;color:#edf6fc;border-radius:9px}
.sql-code,.raw-code{padding:14px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}
.sql-code,.raw-code{font-size:12px}
.table-wrap{overflow:auto;border:1px solid #dce5ef;border-radius:10px;margin-top:14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;vertical-align:top;border-bottom:1px solid #e7edf4}
th,td{padding:10px 12px;min-width:90px}
th{background:#eaf2f8;color:#304b64;position:sticky;top:0}
tr:last-child td{border-bottom:0}tbody tr:nth-child(even){background:#f9fbfd}
.missing{color:#7c8898}.count{color:#52647b;font-size:12px;margin:10px 0 0}
footer{max-width:1100px;margin:8px auto 32px;padding:0 24px;color:#718095;font-size:12px}
@media(max-width:650px){.hero{padding:28px 18px}main{padding:14px}}
@media(max-width:650px){.card{padding:16px;border-radius:12px}.field-grid{grid-template-columns:1fr}}
@media(max-width:650px){th,td{padding:8px}}
@media print{body{background:#fff}.hero{color:#172338;background:#fff}}
@media print{.hero{border-bottom:2px solid #17617c}.card{box-shadow:none;break-inside:avoid}}
"""

_NATIVE_STYLE = """
.model-block{margin:0 0 18px;border:1px solid #dce5ef;border-radius:12px;background:#fff}
.model-label{padding:10px 16px;margin:0;color:#52647b;font-size:12px}
.model-label{border-bottom:1px solid #dce5ef}
.model-prose{contain:layout paint;padding:16px;min-width:0;overflow-wrap:anywhere}
.model-prose *{box-sizing:border-box;max-width:100%!important;min-width:0!important}
.model-prose pre,.model-prose table{overflow:auto;white-space:pre-wrap}
.model-prose table{display:block}.report-nav{margin:12px 0;display:flex;gap:12px;flex-wrap:wrap}
.report-nav a{color:#c8f4ff;text-decoration:underline}
"""


def _fact_json(fact: DeliveryFact) -> str:
    if fact.result_json is not None:
        return fact.result_json
    return json.dumps(
        {"columns": fact.columns, "rows": fact.rows, "metadata": fact.metadata},
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    )


def _fence(value: str, language: str = "text") -> str:
    """动态围栏只能包裹数据，长度严格大于数据中的任意波浪号序列。"""
    value = display_text(value)
    width = max([3, *(len(m.group()) + 1 for m in re.finditer(r"~+", value))])
    marker = "~" * width
    return f"{marker}{language}\n{value}\n{marker}"


def _fact_section(
    fact: DeliveryFact, label: str, *, expand_brief: bool, business: bool = False
) -> _Section:
    if business:
        shown = business_fact(fact)
        values: dict[str, object] = dict(shown.metadata)
        if shown.columns:
            values.update(columns=shown.columns, rows=shown.rows)
        # 文件只保留业务字段，从获准原文恢复其值类型，不附技术元数据。
        if fact.result_json is not None:
            original = json.loads(fact.result_json)
            if isinstance(original, dict):
                values = {key: original[key] for key in values if key in original}
        value = json.dumps(values, ensure_ascii=False, allow_nan=False)
        return _Section(
            "fact",
            fact_scope(fact),
            (fact.summary or "补充原值未附；如需原文请明确提出。")
            if fact.historical_brief and not expand_brief
            else value,
            notices=tuple(
                filter(
                    None,
                    (
                        fact.note if expand_brief else fact.note_summary or fact.note,
                        *fact_limit_notices(fact),
                    ),
                )
            ),
            fact=None if fact.historical_brief and not expand_brief else shown,
            business=True,
        )

    meta = f"来源 {fact.tool_id} · 目标 {fact.target_id} · 采集于 {fact.captured_at.isoformat()}"
    notices = tuple(([f"说明：{fact.note}"] if fact.note else []) + list(fact_limit_notices(fact)))
    if fact.historical_brief and not expand_brief:
        return _Section(
            "fact",
            label,
            fact.summary or "",
            meta,
            (
                *filter(None, (fact.note_summary,)),
                *fact_limit_notices(fact),
                "获准原文未展示；如需原文请明确提出。",
            ),
        )
    # 显式文件可在当前权限下展开原值；自动换载体保持所选摘要语义。
    return _Section("fact", label, _fact_json(fact), meta, notices, fact)


def prepare_prose(delivery: Delivery) -> tuple[ReportContent, ...]:
    """One preparation per model block; files and message preview reuse the result."""
    format_ = delivery.report_format
    if not delivery.report_native or format_ is None or delivery.awaiting_user:
        return ()
    return tuple(prepare_content(value, format_) for value in delivery.texts)


def _sections(
    delivery: Delivery, *, expand_brief: bool, prose: tuple[ReportContent, ...] | None = None
) -> list[_Section]:
    """按正文块顺序填入现有模板：模型文字转义为说明段落，事实章节由代码生成并带来源；
    其余结果在后；动作区域不进入附件（只在消息中完整展示）。"""
    parts: list[_Section] = []
    content = iter(prepare_prose(delivery) if prose is None else prose)
    format_notice = (
        "部分不支持的格式已按原文展示" if delivery.report_format == "md" else FORMAT_NOTICE
    )
    facts = {fact.evidence_id: fact for fact in delivery.facts}
    for block in delivery.blocks:
        if isinstance(block, DeliveryFactRef):
            if block.evidence_id not in facts:
                continue  # 动作材料不进附件
            fact = facts[block.evidence_id]
            parts.append(
                _fact_section(
                    fact,
                    delivery.result_label(fact.evidence_id),
                    expand_brief=expand_brief,
                    business=delivery.answer_version == 4,
                )
            )
        else:
            prepared = next(content, None)
            parts.append(
                _Section(
                    "answer",
                    "模型说明",
                    prepared.body if prepared else block.text,
                    "模型生成，未经系统核实",
                    (format_notice,) if prepared and prepared.changed else (),
                    native=prepared is not None,
                )
            )
    placed = {b.evidence_id for b in delivery.blocks if isinstance(b, DeliveryFactRef)}
    parts.extend(
        _fact_section(
            fact,
            delivery.result_label(fact.evidence_id),
            expand_brief=expand_brief,
            business=delivery.answer_version == 4,
        )
        for fact in delivery.section("body")
        if fact.evidence_id not in placed
    )
    parts.extend(
        _fact_section(
            fact,
            "其余结果 · " + delivery.result_label(fact.evidence_id),
            expand_brief=expand_brief,
        )
        for fact in delivery.section("remaining")
    )
    if delivery.limit_notice:
        parts.append(_Section("notice", "完整性说明", delivery.limit_notice))
    if delivery.monitoring_notice:
        parts.append(_Section("notice", "未核实来源", delivery.monitoring_notice))
    if delivery.session_notice:
        parts.append(_Section("session", "会话提示", delivery.session_notice))
    if delivery.basis and delivery.answer_version != 4:
        parts.append(
            _Section("basis", "查看依据", "\n".join(f"{t} · {o}" for t, o in delivery.basis))
        )
    return parts


_INVALID = object()
_RAW_FIELDS = ("sql", "ddl", "plan")


def _decoded(value: str) -> object:
    def nonfinite(raw: str) -> None:
        raise ValueError(raw)

    try:
        return json.loads(value, parse_constant=nonfinite)
    except (ValueError, TypeError):
        return _INVALID


def _table(data: object) -> tuple[tuple[str, ...], list[dict[str, object]]] | None:
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return None
    rows = data["rows"]
    raw_columns = data.get("columns", [])
    if not isinstance(raw_columns, list) or any(not isinstance(c, str) for c in raw_columns):
        return None
    if any(not isinstance(row, dict) for row in rows):
        return None
    extra = dict.fromkeys(key for row in rows for key in row if key not in raw_columns)
    columns = (*raw_columns, *extra)
    return columns, rows


def _escaped(value: str) -> str:
    return html.escape(display_text(value), quote=True)


def _json_value(value: object, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=indent)


def _html_fact(value: str) -> str:
    data = _decoded(value)
    if data is _INVALID:
        return f'<pre class="raw-code"><code>{_escaped(value)}</code></pre>'
    table = _table(data)
    if not isinstance(data, dict):
        return f'<pre class="raw-code"><code>{_escaped(_json_value(data, indent=2))}</code></pre>'
    if not data:
        return '<pre class="raw-code"><code>{}</code></pre>'
    fields = []
    for key, item in data.items():
        if table is not None and key in ("columns", "rows"):
            continue
        if key in _RAW_FIELDS and isinstance(item, str):
            fields.append(
                '<div class="field full"><div class="field-name">'
                f"{_escaped(key.upper())} 原文</div>"
                f'<pre class="sql-code"><code>{_escaped(item)}</code></pre></div>'
            )
        else:
            fields.append(
                f'<div class="field"><div class="field-name">{_escaped(str(key))}</div>'
                f'<code class="field-value">{_escaped(_json_value(item))}</code></div>'
            )
    body = '<div class="field-grid">' + "".join(fields) + "</div>" if fields else ""
    if table is None:
        return body
    columns, rows = table
    if columns:
        head = "".join(f'<th scope="col">{_escaped(c)}</th>' for c in columns)
        cells = []
        for row in rows:
            values = []
            for column in columns:
                if column not in row:
                    values.append('<td><span class="missing">未提供</span></td>')
                elif column in _RAW_FIELDS and isinstance(item := row[column], str):
                    values.append(
                        f'<td><pre class="sql-code"><code>{_escaped(item)}</code></pre></td>'
                    )
                else:
                    rendered = _escaped(_json_value(row[column]))
                    values.append(f'<td><code class="cell">{rendered}</code></td>')
            cells.append("<tr>" + "".join(values) + "</tr>")
        body += (
            '<div class="table-wrap"><table><thead><tr>'
            f"{head}</tr></thead><tbody>{''.join(cells)}</tbody></table></div>"
        )
    elif "columns" in data:
        body += '<p class="count">列定义 []</p>'
    body += f'<p class="count">本文件展示 {len(rows)} 行。</p>'
    return body


_SIMPLE_MD = re.compile(r'^(?:[\w ,+"-]+|-?\d+\.\d+(?:[eE][+-]?\d+)?)$', re.UNICODE)


def _safe_md_cell(value: str) -> bool:
    return bool(_SIMPLE_MD.fullmatch(value)) and all(
        char != "_"
        or (
            0 < index < len(value) - 1 and value[index - 1].isalnum() and value[index + 1].isalnum()
        )
        for index, char in enumerate(value)
    )


_MD_CELL_MAX = 120


def _md_cell(value: object) -> str | None:
    """表格单元格：简单值直出，其余放代码片段；多行或过长值返回 None，放表下明细。"""
    text = _json_value(value)
    if (isinstance(value, str) and ("\n" in value or "\r" in value)) or len(text) > _MD_CELL_MAX:
        return None
    if _safe_md_cell(text):
        return text
    # 代码片段内不解析强调、链接与 HTML；JSON 已把字面反斜杠加倍，故每个 \| 都是竖线转义。
    return _inline_code(display_text(text).replace("|", "\\|"))


def _md_label(name: str) -> str:
    """列名/字段名：简单名直出；其余用 JSON 单行编码放进代码片段，可逆且不能跨行。

    表格竖线转义只在表头调用处添加；表外标题若也转义，代码片段会保留反斜杠而改变名称。
    """
    if _safe_md_cell(name) and name == name.strip():
        return name
    return _inline_code(display_text(_json_value(name)))


def _md_fact(value: str, first_row: int = 1) -> str:
    data = _decoded(value)
    if data is _INVALID:
        return _fence(display_text(value))
    table = _table(data)
    if not isinstance(data, dict):
        return _fence(_json_value(data, indent=2), "json")
    if not data:
        return _fence("{}", "json")
    blocks = []
    for key, item in data.items():
        if table is not None and key in ("columns", "rows"):
            continue
        if key in _RAW_FIELDS and isinstance(item, str):
            language = "text" if key == "plan" else "sql"
            blocks.append(f"**{key.upper()} 原文**\n\n{_fence(item, language)}")
        else:
            blocks.append(f"**{_md_label(key)}**\n\n{_fence(_json_value(item, indent=2), 'json')}")
    if table is not None:
        columns, rows = table
        if columns:
            details = []
            lines = [
                # JSON 已把字面反斜杠加倍，故表头中每个 \| 都是竖线转义。
                "| " + " | ".join(_md_label(c).replace("|", "\\|") for c in columns) + " |",
                "| " + " | ".join("---" for _ in columns) + " |",
            ]
            # 拆分文件中行号与章节标题“行 a–b/N”一致，按全局行号编号。
            for index, row in enumerate(rows, first_row):
                cells = []
                for column in columns:
                    if column not in row:
                        cells.append("（未提供）")
                        continue
                    item = row[column]
                    cell = None if column in _RAW_FIELDS else _md_cell(item)
                    if cell is not None:
                        cells.append(cell)
                        continue
                    cells.append(f"见下方 第 {index} 行")
                    title = f"**第 {index} 行** · {_md_label(column)}"
                    if isinstance(item, str) and display_text(item) == item:
                        language = "sql" if column in ("sql", "ddl") else "text"
                        details.append(f"{title}（字符串原文）\n\n{_fence(item, language)}")
                    else:
                        details.append(f"{title}\n\n{_fence(_json_value(item, indent=2), 'json')}")
                lines.append("| " + " | ".join(cells) + " |")
            blocks.append("\n".join(lines))
            blocks.extend(details)
        elif "columns" in data:
            blocks.append("**列定义**\n\n" + _fence(_json_value(columns), "json"))
        blocks.append(f"本文件展示 {len(rows)} 行。")
    return "\n\n".join(blocks)


def _md_escape(value: str) -> str:
    return re.sub(r"([\\`*{}\[\]()#|<>])", r"\\\1", display_text(value))


def _inline_code(value: str) -> str:
    width = max([1, *(len(m.group()) + 1 for m in re.finditer(r"`+", value))])
    marker = "`" * width
    return f"{marker}{value}{marker}"


def _html_prose(value: str) -> str:
    return "".join(
        f'<p class="prose">{_escaped(paragraph)}</p>'
        for paragraph in re.split(r"\n\s*\n", value)
        if paragraph
    )


def _render(
    format_: ReportFormat,
    sections: list[_Section],
    *,
    label: str | None = None,
    notice: str | None = None,
    purpose: str | None = None,
) -> bytes:
    natural = purpose is not None
    document_title = (
        f"小维 · {purpose}"
        if natural
        else "小维结果"
        if any(s.kind == "fact" for s in sections)
        else "小维答复"
    ) + (f" {label}" if label else "")
    if format_ == "md":
        blocks = []
        for index, section in enumerate(sections, start=1):
            content = (
                _md_fact(section.value, section.first_row)
                if section.fact
                else section.value
                if section.native
                else _md_escape(section.value)
                if natural and section.kind == "fact"
                else _fence(section.value)
            )
            meta = (
                f"\n\n{_inline_code(display_text(section.meta))}"
                if section.meta and (not natural or purpose == "证据明细")
                else ""
            )
            notes = "".join(f"\n\n> {_md_escape(note)}" for note in section.notices)
            if section.native:
                blocks.append(f"{meta.strip()}{notes}\n\n{content}")
            elif (
                natural
                and purpose == "报告正文"
                and section.kind == "fact"
                and section.fact is None
            ):
                blocks.append(f"{_md_escape(section.title)}：{content}{notes}")
            else:
                blocks.append(f"## {index}. {_md_escape(section.title)}{meta}{notes}\n\n{content}")
        prefix = "# " + document_title + "\n\n"
        if notice:
            prefix += f"> 范围说明：{_md_escape(notice)}\n\n"
        body = prefix + "\n\n".join(blocks) + "\n"
    else:
        native = any(s.native for s in sections)
        html_blocks = []
        for index, section in enumerate(sections, 1):
            if section.native:
                notes = "".join(
                    f'<p class="notice">{_escaped(item)}</p>' for item in section.notices
                )
                html_blocks.append(
                    '<section class="model-block"><p class="model-label">模型生成，未经系统核实</p>'
                    f'{notes}<div class="model-prose">{section.value}</div></section>'
                )
                continue
            detail = _html_fact(section.value) if section.fact else _html_prose(section.value)
            meta = f'<p class="meta">{_escaped(section.meta)}</p>' if section.meta else ""
            notes = "".join(f'<p class="notice">{_escaped(item)}</p>' for item in section.notices)
            if natural and section.fact is not None and not section.business:
                fact = section.fact
                limits = "".join(
                    f'<p class="notice">{_escaped(item)}</p>'
                    for item in filter(None, (fact.note_summary, *fact_limit_notices(fact)))
                )
                inline = (
                    _html_prose(fact.summary)
                    if fact.historical_brief and fact.summary is not None
                    else ""
                    if fold_details(fact)
                    else _html_fact(_table_json(fact, section.value))
                )
                html_blocks.append(
                    f'<section class="card" id="report-section-{index}">{inline}{limits}'
                    '<details class="evidence"><summary>'
                    f"{_escaped(section.title)} · 查看原值与来源</summary>"
                    f"{meta}{notes}{detail}</details></section>"
                )
                continue
            if natural and section.kind == "answer":
                meta = '<p class="model-label">AI 说明</p>'

            html_blocks.append(
                (
                    '<section class="card"'
                    + (f' id="report-section-{index}"' if native else "")
                    + '><div class="card-head">'
                )
                + f'<span class="number">{index:02}</span>'
                f"<h2>{_escaped(section.title)}</h2></div>{meta}{notes}{detail}</section>"
            )
        fact_count = sum(s.kind == "fact" for s in sections)
        summary = (
            "任务答复与可核对依据"
            if natural
            else f"本文件包含 {fact_count} 项结果"
            if fact_count
            else "本轮答复与说明"
        )
        if label:
            summary += f'<span class="part">{_escaped(label)}</span>'
        nav = ""
        if native:
            nav = (
                '<nav class="report-nav" aria-label="系统事实">'
                + "".join(
                    f'<a href="#report-section-{i}">{_escaped(s.title)}</a>'
                    for i, s in enumerate(sections, 1)
                    if s.fact
                )
                + "</nav>"
            )
        body = (
            '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" '
            "content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'none'; "
            "base-uri 'none'; form-action 'none'\">"
            f"<title>{_escaped(document_title)}</title><style>{_STYLE}"
            + (_NATIVE_STYLE if native else "")
            + "</style></head><body>"
            '<header class="hero"><div class="hero-inner">'
            '<div class="eyebrow">小维 · 结果文件</div>'
            f"<h1>{_escaped(document_title)}</h1>"
            f'<p class="summary">{summary}</p>{nav}</div></header><main>'
            + (f'<p class="notice">范围说明：{_escaped(notice)}</p>' if notice else "")
            + "".join(html_blocks)
            + "</main><footer>由小维生成 · 静态离线文件</footer></body></html>"
        )
    return body.encode("utf-8")


def _report_sections(
    delivery: Delivery, expand_brief: bool, prose: tuple[ReportContent, ...] | None
) -> list[_Section]:
    if delivery.channel != "feishu" or delivery.report_format not in ("md", "html"):
        raise ValueError("当前交付没有获准的报告格式")
    if delivery.requires_complete:
        raise ValueError("当前回答不能作为只读事实报告交付")
    return _sections(delivery, expand_brief=expand_brief, prose=prose)


def _single_report(
    format_: ReportFormat,
    sections: list[_Section],
    *,
    purpose: str | None = None,
    notice: str | None = None,
) -> ReportFile:
    content = _render(format_, sections, purpose=purpose, notice=notice)
    if len(content) > MAX_REPORT_BYTES:
        raise ReportCapacityError("报告超过当前文件容量")
    return ReportFile(
        format_,
        f"xiaowei-{'details' if purpose == '证据明细' else 'report'}.{format_}",
        content,
        hashlib.sha256(content).hexdigest(),
    )


def build_report(delivery: Delivery, *, expand_brief: bool = True) -> ReportFile:
    """旧 v0 绑定使用的单文件布局；新交付由 build_reports 完整分包。"""
    sections = _report_sections(delivery, expand_brief, None)
    if delivery.report_format is None:
        raise ValueError("当前交付没有获准的报告格式")
    return _single_report(delivery.report_format, sections)


def build_reports(
    delivery: Delivery,
    *,
    expand_brief: bool = True,
    prose: tuple[ReportContent, ...] | None = None,
) -> tuple[ReportFile, ...]:
    """在编码后容量内，按整章节或整行交付本次获准内容。"""
    sections = _report_sections(delivery, expand_brief, prose)
    format_ = delivery.report_format
    if format_ not in ("md", "html"):
        raise ValueError("当前交付没有获准的报告格式")
    shared_notice = (
        "\n".join(
            dict.fromkeys(
                [
                    notice
                    for fact in delivery.facts
                    if fact.section != "action"
                    for notice in fact_limit_notices(fact)
                ]
                + ([delivery.monitoring_notice] if delivery.monitoring_notice else [])
            )
        )
        or None
    )
    sections = [
        replace(section, title=section.title.replace("其余结果 · ", "").replace("结果 ", "依据 "))
        if section.kind == "fact"
        else section
        for section in sections
    ]
    if delivery.answer_version == 4 or format_ == "html":
        return _pack_reports(format_, sections, "报告正文", shared_notice)
    body, details = [], []
    for section in sections:
        fact = section.fact
        if fact is None:
            body.append(section)
            continue
        details.append(section)
        body.append(
            replace(
                section,
                value=(fact.summary or "完整原值见证据明细文件。")
                if fold_details(fact)
                else _table_json(fact),
                fact=None if fold_details(fact) else fact,
                meta=None,
                notices=tuple(filter(None, (fact.note_summary, *fact_limit_notices(fact)))),
            )
        )
    return (
        *_pack_reports(format_, body, "报告正文", shared_notice),
        *(_pack_reports(format_, details, "证据明细", shared_notice) if details else ()),
    )


def _table_json(fact: DeliveryFact, fragment: str | None = None) -> str:
    """正文仅排版获准列及当前分片的行，完整原值单独交付。"""
    table = _table(_decoded(fragment)) if fragment is not None else None
    rows = table[1] if table is not None else fact.rows
    return json.dumps({"columns": fact.columns, "rows": rows}, ensure_ascii=False, allow_nan=False)


def _pack_reports(
    format_: ReportFormat, sections: list[_Section], purpose: str, shared_notice: str | None
) -> tuple[ReportFile, ...]:
    try:
        return (_single_report(format_, sections, purpose=purpose, notice=shared_notice),)
    except ReportCapacityError:
        pass

    def pack(estimated_total: int) -> list[list[_Section]]:
        files: list[list[_Section]] = []
        current: list[_Section] = []

        def fits(candidate: list[_Section]) -> bool:
            return (
                len(
                    _render(
                        format_,
                        candidate,
                        label=f"{len(files) + 1}/{estimated_total}",
                        notice=shared_notice,
                        purpose=purpose,
                    )
                )
                <= MAX_REPORT_BYTES
            )

        def add(section: _Section) -> bool:
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

        for section in sections:
            if add(section):
                continue
            fact = section.fact
            if fact is None:
                raise ReportCapacityError("完整章节超过当前文件容量")
            try:
                original = json.loads(section.value)
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
                    fragment = replace(
                        section,
                        title=section.title + f" · 行 {start + 1}–{end}/{len(rows)}",
                        value=value,
                        fact=fact.model_copy(update={"result_json": value}),
                        first_row=start + 1,
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
    file_stem = "xiaowei-details" if purpose == "证据明细" else "xiaowei-report"
    for index, group in enumerate(grouped, 1):
        content = _render(
            format_,
            group,
            label=f"{index}/{total}" if total > 1 else None,
            notice=shared_notice if total > 1 else None,
            purpose=purpose,
        )
        if len(content) > MAX_REPORT_BYTES:
            raise ReportCapacityError("报告超过当前文件容量")
        reports.append(
            ReportFile(
                format_,
                (
                    f"{file_stem}-{index}-of-{total}.{format_}"
                    if total > 1
                    else f"{file_stem}.{format_}"
                ),
                content,
                hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(reports)
