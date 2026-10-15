"""飞书单消息展示：只读已验证的当前渠道投影；模型说明可用受限格式，不执行 I/O。

JSON 2.0 要求客户端 >=7.20。纯文本表格最多五个，额外/宽表用纯文本字段；所有组件和
折叠文字均计入预算。完整 HTTP 请求预算包括 SDK 的内层 JSON 字符串与外层再次转义。
"""

import json
import re
import unicodedata
from collections.abc import Sequence
from typing import Any, Final

from xiaowei.feishu_markdown import format_prose, prose_post
from xiaowei.models import AnswerInference, Delivery, DeliveryFact, JsonScalar

MAX_REQUEST_BYTES: Final = 30_000
CAPACITY_NOTICE: Final = "结果未展示：单消息容量不足。可缩小问题范围后重新提问；本次仅发送此说明。"
FACTS_CLIPPED: Final = "事实展示已截断，未展示全部结果。"
ANALYSIS_CLIPPED: Final = "分析展示已截断，未展示全部模型分析。"
TEXT_CLIPPED: Final = "（内容超过单消息上限，已截断）"
BRIEF_ORIGINAL_OMITTED: Final = "获准原文未展示（单消息容量限制）；如需原文请明确提出。"
_TECHNICAL = frozenset({"sql", "row_count", "elapsed_ms", "next_cursor"})
_PAGED = frozenset({"local/list_databases", "local/list_tables", "local/describe_table"})
_ACTION_LABELS = {
    "action_id": "Action ID",
    "action": "动作",
    "parameters": "实际参数",
    "revision": "当前版本",
    "change": "差异",
    "impact": "影响",
    "rollback": "恢复办法",
    "approve_before": "批准截止时间",
    "approval_command": "批准命令",
    "approval_status": "审批状态",
}

Message = dict[str, Any]
_AT_TAG = re.compile(r"<(?=\s*at\b)", re.IGNORECASE)


def mention_safe(text: str) -> str:
    """普通文本消息中阻止 @；卡片的 plain_text 无需改写原值。"""
    return _AT_TAG.sub("＜", text)


def display_text(text: str) -> str:
    """保留真实换行/缩进；将双向、零宽等不可见控制符显示为转义，不改证据原值。"""
    return "".join(
        json.dumps(c)[1:-1]
        if c not in ("\n", "\t") and unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"}
        else c
        for c in text
    )


def fact_limit_notices(fact: DeliveryFact) -> tuple[str, ...]:
    """区分分页未完与真实截断，供卡片和报告沿用同一事实口径。"""
    cursor = fact.metadata.get("next_cursor")
    notices = []
    if cursor is not None:
        notices.append("还有后续页，本页不是全部结果。")
    if fact.truncated and not (fact.tool_id in _PAGED and cursor is not None):
        notices.append("工具结果已截断，不能据此声称完整。")
    return tuple(notices)


def capacity_notice(session_notice: str | None = None) -> Message:
    """容量回执同条保留必要系统提醒；也供发送端识别省略结果的消息。"""
    text = CAPACITY_NOTICE + ("\n" + session_notice if session_notice is not None else "")
    return {"text": mention_safe(display_text(text))}


def _plain(text: str) -> Message:
    return {"tag": "plain_text", "content": display_text(text)}


def _div(text: str) -> Message:
    return {"tag": "div", "text": _plain(text)}


def _cell(row: dict[str, JsonScalar], name: str) -> str:
    if name not in row:
        return "（未提供）"
    value = row[name]
    if value is None:
        return "NULL（空值）"
    if isinstance(value, str):
        # 字符串加引号，保留类型区别；先转义字面反斜杠/引号，再显示不可见控制符。
        return '"' + display_text(value.replace("\\", "\\\\").replace('"', '\\"')) + '"'
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _data(fact: DeliveryFact) -> tuple[tuple[str, ...], tuple[dict[str, JsonScalar], ...]]:
    if fact.view == "brief":
        return (), ()
    if fact.columns:
        return fact.columns, fact.rows
    # 非表格 MCP/标量结果也展示获准事实，技术字段留在折叠区。
    rows = tuple({"字段": k, "值": v} for k, v in fact.metadata.items() if k not in _TECHNICAL)
    return ("字段", "值"), rows


def request_bytes(message: Message, chat_id: str, *, reply_to: str | None = None) -> int:
    """锁版 SDK 的公开 create/reply 请求体大小；UUID 为 36 个 ASCII 字符。

    card 先按 SDK json.dumps 默认分隔符序列化为字符串，httpx 外层使用紧凑 JSON。
    reply 的 message_id 在路径中，不在请求体中。不调用 SDK 私有编码器。
    """
    key = "card" if "card" in message else "post" if "post" in message else None
    body = {
        "content": json.dumps(
            message[key] if key else message, ensure_ascii=False, allow_nan=False
        ),
        "msg_type": "interactive" if key == "card" else "post" if key == "post" else "text",
        "uuid": "0" * 36,
    }
    if reply_to is None:
        body["receive_id"] = chat_id
    return len(
        json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    )


def _chars(value: object) -> int:
    if isinstance(value, list):
        return sum(_chars(v) for v in value)
    if not isinstance(value, dict):
        return 0
    if value.get("tag") in {"plain_text", "markdown"}:
        return len(value["content"])
    if value.get("tag") == "table":
        return sum(len(c["display_name"]) for c in value["columns"]) + sum(
            len(cell) for row in value["rows"] for cell in row.values()
        )
    if value.get("tag") == "text":
        return len(value["text"])
    if set(value) == {"text"}:
        return len(value["text"])
    return sum(_chars(v) for v in value.values())


def _elements(value: object) -> int:
    if isinstance(value, list):
        return sum(_elements(v) for v in value)
    if not isinstance(value, dict):
        return 0
    return int("tag" in value) + sum(_elements(v) for v in value.values())


def _card(
    delivery: Delivery,
    counts: Sequence[int],
    sqls: set[int],
    analysis: Sequence[AnswerInference],
    technical: set[int],
    *,
    omissions: bool = True,
    formatted: bool = False,
) -> Message:
    elements: list[Message] = []
    if delivery.report_notice:
        elements.append(_div(delivery.report_notice))
    if delivery.monitoring_notice:
        elements.append(_div(delivery.monitoring_notice))
    if delivery.session_notice:
        elements.append(_div(delivery.session_notice))
    if analysis:
        elements.append(_div("分析与建议（模型推断）"))
        for item in analysis:
            prose = format_prose(display_text(item.text)) if formatted else [_div(item.text)]
            if prose == [_div(item.text)]:
                elements.append(_div(item.text))
            else:
                elements.extend(prose)
    if omissions and tuple(analysis) != delivery.analysis:
        elements.append(_div(ANALYSIS_CLIPPED))
    tables = 0
    for index, fact in enumerate(delivery.facts):
        columns, all_rows = _data(fact)
        rows = all_rows[: counts[index]]
        if fact.requires_complete:
            # 独立审批材料保留原来的完整来源、说明和正文。
            elements.append(
                _div(
                    f"[{fact.evidence_id}] 来源 {fact.tool_id} · 目标 {fact.target_id}\n"
                    f"采集于 {fact.captured_at.isoformat()}"
                )
            )
        else:
            elements.append(_div(f"结果 · {fact.target_id}"))
        if fact.note is not None:
            elements.append(
                _div(
                    f"说明：{fact.note}"
                    if fact.requires_complete
                    else fact.note_summary or fact.note
                )
            )
        if fact.requires_complete:
            elements.append(
                _div(
                    "动作执行事实"
                    if "state" in fact.metadata
                    else (
                        "动作提案（当前不可批准）"
                        if "approval_status" in fact.metadata
                        else "待批准动作（尚未执行）"
                    )
                )
            )
            # 完整动作材料是可信代码生成的文本；避免把整份 JSON 再转义为标量。
            for key, value in fact.metadata.items():
                text = display_text(value) if isinstance(value, str) else _cell({key: value}, key)
                elements.append(_div(f"{_ACTION_LABELS.get(key, key)}：{text}"))
            continue
        if fact.columns:
            elements.extend(
                _div(f"{key}: {_cell({key: value}, key)}")
                for key, value in fact.metadata.items()
                if key not in _TECHNICAL and value is not None
            )
        elements.extend(_div(notice) for notice in fact_limit_notices(fact))
        if fact.tool_id == "local/run_readonly_query" and index not in sqls:
            elements.append(_div("SQL 未展示。"))
        if fact.view == "brief":
            elements.append(_div(fact.summary or "摘要不可用。"))
            if omissions and fact.result_json is not None and index not in technical:
                elements.append(_div(BRIEF_ORIGINAL_OMITTED))
            continue
        if not rows:
            if not all_rows:
                absent = not fact.columns and not fact.metadata and fact.result_json is None
                elements.append(_div("（无结果）" if absent else "（无数据行）"))
            elif omissions:
                elements.append(_div("结果行未展示。"))
        elif fact.tool_id == "local/show_create_table":
            for row in rows:
                for name in columns:
                    # DDL 原文是默认可见结果，用纯文本而不是代码围栏或表格单元格。
                    elements.append(
                        _div(str(row[name]) if name == "ddl" else f"{name}: {_cell(row, name)}")
                    )
        elif len(columns) <= 50 and tables < 5:
            elements.append(
                {
                    "tag": "table",
                    "page_size": 10,
                    "row_height": "auto",
                    "columns": [
                        {"name": f"c{i}", "display_name": display_text(name), "data_type": "text"}
                        for i, name in enumerate(columns)
                    ],
                    "rows": [
                        {f"c{i}": _cell(row, name) for i, name in enumerate(columns)}
                        for row in rows
                    ],
                }
            )
            tables += 1
        else:
            elements.extend(
                _div("\n".join(f"{name}: {_cell(row, name)}" for name in columns)) for row in rows
            )
    if omissions and any(
        not f.requires_complete and n < len(_data(f)[1])
        for f, n in zip(delivery.facts, counts, strict=True)
    ):
        elements.append(_div(FACTS_CLIPPED))
    for index, fact in enumerate(delivery.facts):
        if fact.requires_complete:
            continue
        details: list[Message] = [
            _div(
                f"依据：{fact.evidence_id} · 来源 {fact.tool_id} · 目标 {fact.target_id}\n"
                f"采集于 {fact.captured_at.isoformat()}"
            )
        ]
        if fact.note_summary and fact.note:
            details.append(_div(fact.note))
        if index in sqls:
            details.append(_div(f"实际 SQL:\n{fact.metadata['sql']}"))
        if index in technical:
            for key, value in fact.metadata.items():
                if key in _TECHNICAL and not (
                    key == "sql" and fact.tool_id == "local/run_readonly_query"
                ):
                    label = "结构采集 SQL 模板" if key == "sql" and fact.tool_id in _PAGED else key
                    details.append(_div(f"{label}: {_cell({key: value}, key)}"))
            if fact.result_json is not None:
                details.append(_div(f"原文 JSON:\n{fact.result_json}"))
        if details:
            elements.append(
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": _plain(
                            "查看依据 · 获准原文与查询信息" if fact.view == "brief" else "查看依据"
                        )
                    },
                    "elements": details,
                }
            )
    elements.extend(_basis_panels(delivery.basis))
    return _static_card(
        elements, "小维 · 工具结果" if any(f.requires_complete for f in delivery.facts) else "小维"
    )


def _static_card(elements: list[Message], title: str) -> Message:
    return {
        "card": {
            "schema": "2.0",
            "header": {"template": "blue", "title": _plain(title)},
            "body": {"elements": elements},
        }
    }


def _basis_panels(basis: tuple[tuple[str, str], ...]) -> list[Message]:
    return (
        [
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {"title": _plain("查看依据 · 其他调查")},
                "elements": [_div(f"{tool} · {obj}") for tool, obj in basis],
            }
        ]
        if basis
        else []
    )


def _prose_card(
    prefix: str,
    raw: str,
    *,
    notice: str | None = None,
    clipped: bool = False,
    formatted: bool = False,
    basis: tuple[tuple[str, str], ...] = (),
) -> Message:
    text = display_text(raw)
    elements = [_div(prefix)] if prefix else []
    if notice:
        elements.append(_div(notice))
    elements.extend(format_prose(text) if formatted else [_div(text)])
    if clipped:
        elements.append(_div(TEXT_CLIPPED))
    elements.extend(_basis_panels(basis))
    return _static_card(elements, "小维 · 回复")


class MessageCapacityError(ValueError):
    """完整单消息不合容量；调用方应换载体。"""


def build_feishu_message(
    delivery: Delivery,
    max_chars: int,
    chat_id: str,
    *,
    reply_to: str | None = None,
    max_bytes: int = MAX_REQUEST_BYTES,
    allow_preview: bool = True,
) -> Message:
    """生成受控 post 或结果卡片：必要提醒、回答、完整事实与折叠依据。

    来源/限制放不下时只发固定容量说明；本地异常由调用方记 failed。这里不读另一渠道的投影，
    不靠 SDK 分片，不在发送开始后改发另一消息。
    """
    if delivery.channel != "feishu":
        raise ValueError("飞书展示仅接受飞书渠道")

    def fits(message: Message) -> bool:
        return (
            _chars(message) <= max_chars
            and _elements(message) <= 200
            and request_bytes(message, chat_id, reply_to=reply_to) <= max_bytes
        )

    notice = capacity_notice(delivery.session_notice)

    def omitted_message() -> Message:
        # 完整消息可能比容量回执更短；只在确实需要回执时检查它是否可发送。
        if not fits(notice):
            raise ValueError("单消息预算无法保留容量说明")
        return notice

    if not delivery.facts:
        if delivery.session_notice is not None and delivery.feishu_text is None:
            # 固定会话回执必须完整保留原因和操作方法；预算不足记明确发送失败，不裁掉限制。
            receipt = {
                "text": mention_safe(
                    display_text(delivery.content + "\n" + delivery.session_notice)
                )
            }
            if not fits(receipt):
                raise ValueError("单消息预算无法保留会话回执")
            return receipt
        if delivery.feishu_text is None:
            raw, prefix = delivery.content, ""
        else:
            raw = delivery.feishu_text
            if delivery.monitoring_notice:
                # Evidence 的原文字段包含这条系统说明；独立保留，不把限制送进模型格式器。
                raw = raw.removeprefix(delivery.monitoring_notice + "\n")
            title = (
                delivery.content.partition("\n")[0]
                if delivery.monitoring_notice
                else (
                    "需要澄清"
                    if delivery.content.startswith("需要澄清")
                    else "建议（未执行查询，模型生成，未经系统核实）"
                )
            )
            prefix = title

        def prose_message(text: str, *, clipped: bool = False, formatted: bool = False) -> Message:
            if delivery.feishu_text is None:
                # 固定回执没有模型原文字段；保持原来的文本消息，不按关键词猜业务类型。
                shown = mention_safe(display_text(text))
                return {"text": shown + ("\n" + TEXT_CLIPPED if clipped else "")}
            return _prose_card(
                prefix,
                text,
                notice="\n".join(
                    n
                    for n in (delivery.monitoring_notice, delivery.session_notice)
                    if n is not None
                )
                or None,
                clipped=clipped,
                formatted=formatted,
                basis=delivery.basis,
            )

        shown = display_text(raw)
        # SDK 内部 post→text 降级不会经过 Transport 的 mention_safe；含 @ 标签留在卡片。
        if delivery.feishu_text is not None and not delivery.basis and mention_safe(shown) == shown:
            post = prose_post(shown)
            if post is not None:
                leading = [
                    n for n in (prefix, delivery.monitoring_notice, delivery.session_notice) if n
                ]
                post["post"]["zh_cn"]["content"][:0] = [
                    [{"tag": "text", "text": display_text(n)}] for n in leading
                ]
                if fits(post):
                    return post
        full = prose_message(raw)
        if fits(full):
            formatted = prose_message(raw, formatted=True)
            return formatted if fits(formatted) else full
        if not allow_preview:
            raise MessageCapacityError("完整正文超过单消息容量")
        low, high = 0, len(raw)
        while low < high:
            mid = (low + high + 1) // 2
            candidate = prose_message(raw[:mid], clipped=True)
            if fits(candidate):
                low = mid
            else:
                high = mid - 1
        clipped = prose_message(raw[:low], clipped=True)
        if not fits(clipped):
            return omitted_message()
        formatted = prose_message(raw[:low], clipped=True, formatted=True)
        return formatted if fits(formatted) else clipped

    totals = [len(_data(f)[1]) for f in delivery.facts]
    all_sql = {
        i
        for i, f in enumerate(delivery.facts)
        if f.tool_id == "local/run_readonly_query" and isinstance(f.metadata.get("sql"), str)
    }
    all_technical = set(range(len(delivery.facts)))
    full = _card(delivery, totals, all_sql, delivery.analysis, all_technical)
    if fits(full):
        formatted = _card(
            delivery, totals, all_sql, delivery.analysis, all_technical, formatted=True
        )
        return formatted if fits(formatted) else full
    if not allow_preview:
        # 历史brief的小原文可折叠展示；放不下则只交付已选择的摘要，不因此自动导出原值。
        if any(f.view == "brief" for f in delivery.facts):
            brief_technical = {i for i, f in enumerate(delivery.facts) if f.view != "brief"}
            brief_sql = all_sql & brief_technical
            brief = _card(delivery, totals, brief_sql, delivery.analysis, brief_technical)
            if fits(brief):
                return brief
        raise MessageCapacityError("完整结果超过单消息容量")
    analysis: list[AnswerInference] = []
    technical: set[int] = set()

    def fitting_prefix(sqls: set[int]) -> list[int] | None:
        counts = [0] * len(totals)
        best = counts.copy() if fits(_card(delivery, counts, sqls, analysis, technical)) else None
        # 零行占位与截断提示可能比完整短结果更大；只用不含这些提示的下界提前结束搜索。
        # 下界永不交付，实际候选必须包含所有必要的未展示/截断说明。
        if not fits(_card(delivery, counts, sqls, analysis, technical, omissions=False)):
            return best
        for index, total in enumerate(totals):
            if total == 0:
                continue
            # 完整端点可能移除截断提示，零行端点可能移除/增加占位，不能混进二分区间。
            counts[index] = total
            if fits(_card(delivery, counts, sqls, analysis, technical)):
                best = counts.copy()
                continue
            low, high = 0, total - 1
            while low < high:
                mid = (low + high + 1) // 2
                counts[index] = mid
                if fits(_card(delivery, counts, sqls, analysis, technical)):
                    low = mid
                    best = counts.copy()
                else:
                    high = mid - 1
            counts[index] = total
            if not fits(_card(delivery, counts, sqls, analysis, technical, omissions=False)):
                return best
            # 此端点不合容量但下界合容量：后续短事实可能移除更大的占位，继续检查。
        return best

    sqls: set[int] = set()
    counts = fitting_prefix(sqls)
    if counts is None:
        return omitted_message()
    # SQL 每条完整保留或明示未展示；放不下不阻止展示已有结果行。
    for index in sorted(all_sql):
        selected_counts = fitting_prefix(sqls | {index})
        if selected_counts is not None:
            sqls.add(index)
            counts = selected_counts
    for item in delivery.analysis:
        candidate = _card(delivery, counts, sqls, [*analysis, item], technical)
        if fits(candidate):
            analysis.append(item)
            continue
        low, high = 0, len(item.text)
        while low < high:
            mid = (low + high + 1) // 2
            short = item.model_copy(update={"text": item.text[:mid]})
            if fits(_card(delivery, counts, sqls, [*analysis, short], technical)):
                low = mid
            else:
                high = mid - 1
        if low:
            analysis.append(item.model_copy(update={"text": item.text[:low]}))
        break
    for index in range(len(totals)):
        candidate = _card(delivery, counts, sqls, analysis, technical | {index})
        if fits(candidate):
            technical.add(index)
    plain = _card(delivery, counts, sqls, analysis, technical)
    formatted = _card(delivery, counts, sqls, analysis, technical, formatted=True)
    return formatted if fits(formatted) else plain
