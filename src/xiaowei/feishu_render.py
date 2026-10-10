"""飞书单消息展示：只读已验证的当前渠道投影，不解析模型 Markdown，不执行 I/O。

JSON 2.0 要求客户端 >=7.20。纯文本表格最多五个，额外/宽表用纯文本字段；所有组件和
折叠文字均计入预算。完整 HTTP 请求预算包括 SDK 的内层 JSON 字符串与外层再次转义。
"""

import json
import re
import unicodedata
from collections.abc import Sequence
from typing import Any, Final

from xiaowei.models import AnswerInference, Delivery, DeliveryFact, JsonScalar

MAX_REQUEST_BYTES: Final = 30_000
CAPACITY_NOTICE: Final = "结果未展示：单消息容量不足。可缩小问题范围后重新提问；本次仅发送此说明。"
FACTS_CLIPPED: Final = "事实展示已截断，未展示全部结果。"
ANALYSIS_CLIPPED: Final = "分析展示已截断，未展示全部模型分析。"
TEXT_CLIPPED: Final = "（内容超过单消息上限，已截断）"
_TECHNICAL = frozenset({"sql", "row_count", "elapsed_ms", "next_cursor"})
_PAGED = frozenset({"local/list_databases", "local/list_tables", "local/describe_table"})

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
    card = "card" in message
    body = {
        "content": json.dumps(
            message["card"] if card else message, ensure_ascii=False, allow_nan=False
        ),
        "msg_type": "interactive" if card else "text",
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
    if value.get("tag") == "plain_text":
        return len(value["content"])
    if value.get("tag") == "table":
        return sum(len(c["display_name"]) for c in value["columns"]) + sum(
            len(cell) for row in value["rows"] for cell in row.values()
        )
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
) -> Message:
    elements: list[Message] = []
    tables = 0
    for index, fact in enumerate(delivery.facts):
        columns, all_rows = _data(fact)
        rows = all_rows[: counts[index]]
        elements.append(
            _div(
                f"[{fact.evidence_id}] 来源 {fact.tool_id} · 目标 {fact.target_id}\n"
                f"采集于 {fact.captured_at.isoformat()}"
            )
        )
        if fact.note is not None:
            elements.append(_div(f"说明：{fact.note}"))
        if fact.columns:
            elements.extend(
                _div(f"{key}: {_cell({key: value}, key)}")
                for key, value in fact.metadata.items()
                if key not in _TECHNICAL and value is not None
            )
        cursor = fact.metadata.get("next_cursor")
        if cursor is not None:
            elements.append(_div("还有后续页，本页不是全部结果。"))
        if fact.truncated and not (fact.tool_id in _PAGED and cursor is not None):
            elements.append(_div("工具结果已截断，不能据此声称完整。"))
        if fact.tool_id == "local/run_readonly_query" and index not in sqls:
            elements.append(_div("SQL 未展示。"))
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
    if omissions and any(n < len(_data(f)[1]) for f, n in zip(delivery.facts, counts, strict=True)):
        elements.append(_div(FACTS_CLIPPED))
    if analysis:
        elements.append(_div("分析与建议（模型推断）"))
        elements.extend(_div(f"- {a.text}（依据：{', '.join(a.evidence_ids)}）") for a in analysis)
    if omissions and tuple(analysis) != delivery.analysis:
        elements.append(_div(ANALYSIS_CLIPPED))
    for index, fact in enumerate(delivery.facts):
        details: list[Message] = []
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
                    "header": {"title": _plain(f"技术详情 · {fact.evidence_id}")},
                    "elements": details,
                }
            )
    return {
        "card": {
            "schema": "2.0",
            "header": {"template": "blue", "title": _plain("小维 · 工具结果")},
            "body": {"elements": elements},
        }
    }


def build_feishu_message(
    delivery: Delivery,
    max_chars: int,
    chat_id: str,
    *,
    reply_to: str | None = None,
    max_bytes: int = MAX_REQUEST_BYTES,
) -> Message:
    """构造单个 SDK text/card 输入：必要说明 → 实际 SQL → 整行事实 → 分析 → 其他详情。

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

    notice = {"text": CAPACITY_NOTICE}
    if not fits(notice):
        raise ValueError("单消息预算无法保留容量说明")
    if not delivery.facts:
        if delivery.feishu_text is None:
            raw, prefix = delivery.content, ""
        else:
            raw = delivery.feishu_text
            title = (
                "需要澄清"
                if delivery.content.startswith("需要澄清")
                else "建议（未执行查询，模型生成，未经系统核实）"
            )
            prefix = title + "\n"
        full = {"text": mention_safe(display_text(prefix + raw))}
        if fits(full):
            return full
        low, high = 0, len(raw)
        while low < high:
            mid = (low + high + 1) // 2
            candidate = {
                "text": mention_safe(display_text(prefix + raw[:mid])) + "\n" + TEXT_CLIPPED
            }
            if fits(candidate):
                low = mid
            else:
                high = mid - 1
        clipped = {"text": mention_safe(display_text(prefix + raw[:low])) + "\n" + TEXT_CLIPPED}
        return clipped if fits(clipped) else notice

    totals = [len(_data(f)[1]) for f in delivery.facts]
    all_sql = {
        i
        for i, f in enumerate(delivery.facts)
        if f.tool_id == "local/run_readonly_query" and isinstance(f.metadata.get("sql"), str)
    }
    all_technical = set(range(len(delivery.facts)))
    full = _card(delivery, totals, all_sql, delivery.analysis, all_technical)
    if fits(full):
        return full
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
        return notice
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
    return _card(delivery, counts, sqls, analysis, technical)
