"""飞书单消息展示：只读已验证的当前渠道投影；模型说明可用受限格式，不执行 I/O。

JSON 2.0 要求客户端 >=7.20。纯文本表格最多五个，额外/宽表用纯文本字段；所有组件和
折叠文字均计入预算。完整 HTTP 请求预算包括 SDK 的内层 JSON 字符串与外层再次转义。

来源区分：事实、动作、补全结果与系统说明是代码生成的组件；模型文字只经白名单格式器转换，
由代码按区域标为“AI 分析与建议”或“系统事实”。post 只用于纯模型文字；模型文字伴随任何
代码说明时用卡片分区，代码说明与模型文字不在同一 post 或纯文本中混排。
"""

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xiaowei.feishu_markdown import format_prose, prose_post
from xiaowei.models import Delivery, DeliveryFact, DeliveryFactRef
from xiaowei.starrocks_security import SECURITY_TOOL

MAX_REQUEST_BYTES: Final = 30_000
CAPACITY_NOTICE: Final = "结果未展示：单消息容量不足。可缩小问题范围后重新提问；本次仅发送此说明。"
FACTS_CLIPPED: Final = "事实展示已截断，未展示全部结果。"
ANALYSIS_CLIPPED: Final = "模型说明展示已截断，未展示全部模型说明。"
MODEL_TEXT_LABEL: Final = "AI 分析与建议"
FACT_LABEL: Final = "系统事实"
REMAINING_LABEL: Final = "其余结果（系统补全本轮获准结果）"
TEXT_CLIPPED: Final = "（内容超过单消息上限，已截断）"
BRIEF_ORIGINAL_OMITTED: Final = "获准原文未展示（单消息容量限制）；如需原文请明确提出。"
RAW_OMITTED: Final = "原文 JSON 与上方展示的值逐项一致，未重复展示；如需原文可要求 MD/HTML 文件。"
TECHNICAL_OMITTED: Final = "技术详情超出消息容量，未附。"
_TECHNICAL = frozenset({"sql", "row_count", "elapsed_ms", "next_cursor"})
# 只控制展示，不改变 Evidence 投影、接收许可或保存期限。
_BUSINESS_HIDDEN = _TECHNICAL | frozenset(
    {
        "query",
        "expression",
        "receipt_id",
        "requested_limit",
        "applied_limit",
    }
)
_PAGED = frozenset({"local/list_databases", "local/list_tables", "local/describe_table"})
_ACTUAL_SQL = frozenset({"local/run_readonly_query", "local/explain_query"})
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


def display_text(text: str, *, multiline: bool = True) -> str:
    """保留真实换行/缩进；将双向、零宽等不可见控制符显示为转义，不改证据原值。"""
    return "".join(
        json.dumps(c)[1:-1]
        if (not multiline or c not in ("\n", "\t"))
        and unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"}
        else c
        for c in text
    )


def fact_limit_notices(fact: DeliveryFact) -> tuple[str, ...]:
    """从获准投影生成影响结论的简短范围和完整性提醒，供各渠道复用。"""
    cursor = fact.metadata.get("next_cursor")
    notices = []
    if (
        fact.tool_id == "notifications/send_webhook_message"
        and fact.metadata.get("reason") == "recipient_not_authorized"
    ):
        notices.append(
            "未发送：请管理员核对机器人的 receive_tools/receive_targets。"
            "群内用过提醒时，还须显式配置 scheduled 任务记录及其依赖业务源的接收许可；"
            "不能自动扩大授权。"
        )
    paged = fact.metadata.get("has_more") is True or fact.metadata.get("hasMore") is True
    if cursor is not None or paged:
        notices.append("还有后续页，本页不是全部结果。")
    if fact.truncated and not (paged or (fact.tool_id in _PAGED and cursor is not None)):
        notices.append("工具结果已截断，不能据此声称完整。")
    warnings = fact.metadata.get("warnings")
    if warnings not in (None, "", "[]", "null"):
        notices.append("数据源返回警告，详见依据。")
    metadata = fact.metadata
    filtered = sum(
        value
        for key in ("omitted_sqlguard", "omitted_self", "omitted_revoked")
        if isinstance(value := metadata.get(key), int)
    )
    capacity = sum(
        value
        for key in (
            "omitted_statement_too_long",
            "omitted_value_too_large",
            "omitted_result_too_large",
        )
        if isinstance(value := metadata.get(key), int)
    )
    unchecked = metadata.get("unchecked_candidates", 0) or 0
    if filtered or capacity or unchecked or metadata.get("more_candidates"):
        notices.append(
            f"候选未完全列出：规则或权限过滤 {filtered} 条、容量省略 {capacity} 条、"
            f"未检查 {unchecked} 条。"
            + ("还有未读取候选，数量未知。" if metadata.get("more_candidates") else "")
        )
    window_keys = ("start_time", "end_time")
    scale = 1
    if metadata.get("from") is not None and metadata.get("to") is not None:
        window_keys, scale = ("from", "to"), 1000  # Grafana annotations use Unix milliseconds.
    if all(metadata.get(key) is not None for key in window_keys):
        try:
            window = [
                datetime.fromtimestamp(
                    float(str(metadata[key])) / scale, ZoneInfo("Asia/Shanghai")
                ).isoformat(sep=" ")
                for key in window_keys
            ]
        except (ValueError, OverflowError, OSError):
            notices.append("查询窗口见依据（时间格式无法显示）。")
        else:
            step = metadata.get("step")
            notices.append(
                f"查询窗口（北京时间）：{window[0]} 至 {window[1]}。"
                + (f"步长 {step} 秒。" if step is not None else "")
            )
    applied = metadata.get("applied_limit")
    if fact.truncated and applied is not None and metadata.get("requested_limit") != applied:
        notices.append(f"返回量受限：实际 LIMIT {applied}，多取 1 行用于判断截断。")
    return tuple(notices)


def capacity_notice(
    session_notice: str | None = None, *, notification_notice: str | None = None
) -> Message:
    """容量回执同条保留必要系统提醒；也供发送端识别省略结果的消息。"""
    text = "\n".join(n for n in (CAPACITY_NOTICE, session_notice, notification_notice) if n)
    return {"text": mention_safe(display_text(text))}


def _plain(text: str) -> Message:
    return {"tag": "plain_text", "content": display_text(text)}


def _div(text: str) -> Message:
    return {"tag": "div", "text": _plain(text)}


def _cell(row: Mapping[str, object], name: str) -> str:
    if name not in row:
        return "（未提供）"
    value = row[name]
    if value is None:
        return "NULL（空值）"
    if isinstance(value, str):
        # 字符串加引号，保留类型区别；先转义字面反斜杠/引号，再显示不可见控制符。
        return '"' + display_text(value.replace("\\", "\\\\").replace('"', '\\"')) + '"'
    return display_text(json.dumps(
        value, ensure_ascii=False, allow_nan=False,
        indent=2 if isinstance(value, (dict, list)) else None,
    ))


def _web_cell(row: Mapping[str, object], name: str) -> str | None:
    if name in row:
        value = row[name]
        if value is None:
            return None
        if isinstance(value, str):
            return display_text(value)
    return _cell(row, name)


@dataclass(frozen=True)
class FactPresentation:
    """从当前渠道获准事实即时派生；不保存、不授予权限，也不进入模型。"""

    fields: dict[str, object]
    columns: tuple[str, ...]
    rows: tuple[dict[str, object], ...]
    coverage: str | None = None
    checks: tuple[dict[str, str], ...] = ()
    notices: tuple[str, ...] = ()

    def for_web(self) -> dict[str, object]:
        """保留 Web 标量显示；嵌套值和大整数在服务端保真格式化。"""
        return {
            "fields": {key: _web_cell(self.fields, key) for key in self.fields},
            "columns": self.columns,
            "rows": [{key: _web_cell(row, key) for key in self.columns} for row in self.rows],
            "coverage": self.coverage, "checks": self.checks, "notices": self.notices,
        }


def audit_local_time(value: object, time_zone: object, *, annotate: bool = True) -> str:
    """只转换已有带时区时刻；历史缺失/损坏保留原值并明确不能换算。"""
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if moment.tzinfo is None or not isinstance(time_zone, str):
            raise ValueError
        return moment.astimezone(ZoneInfo(time_zone)).isoformat()
    except (ValueError, OverflowError, ZoneInfoNotFoundError):
        original = value.isoformat() if isinstance(value, datetime) else str(value)
        return f"{original}（原时刻，审计时区不可换算）" if annotate else original


def fact_values(fact: DeliveryFact) -> dict[str, object]:
    """只恢复同一 DeliveryFact 的获准原值；不解析长得像 JSON 的业务字符串。"""
    if fact.result_json is not None:
        original = json.loads(fact.result_json)
        if isinstance(original, dict):
            return cast(dict[str, object], original)
    values: dict[str, object] = dict(fact.metadata)
    if fact.columns:
        values.update(columns=fact.columns, rows=fact.rows)
    return values


_SIGNAL_LABELS: Final = {
    "boolean_probe": "注入探测（OR 同值常量线索）",
    "delay_call": "时间延迟调用线索",
    "stacked_statements": "多语句",
    "high_risk_operation": "高风险操作（审计行）",
}
_OPERATION_LABELS: Final = {
    "grant": "授权", "revoke": "撤权", "account": "账号/角色", "password": "口令修改",
    "drop": "删表/库", "truncate": "清空表", "export": "导出",
    "insert_overwrite": "覆盖写入", "delete_no_where": "无 WHERE DELETE",
}


def _security_presentation(fact: DeliveryFact, data: dict[str, object]) -> FactPresentation:
    coverage = data.get("coverage")
    window = data.get("window")
    signals = data.get("signal_counts")
    operations = data.get("operation_counts")
    cov = coverage if isinstance(coverage, dict) else {}
    win = window if isinstance(window, dict) else {}
    counts = signals if isinstance(signals, dict) else {}
    ops = operations if isinstance(operations, dict) else {}
    complete = cov.get("match_counts_complete") is True

    def number(value: object) -> str:
        return f"{value:,}" if isinstance(value, int) else "未提供"

    def count(value: object) -> str:
        shown = number(value)
        if complete or not isinstance(value, int):
            return shown
        if value > 0:
            return f"已确认至少 {shown} 条"
        unknown = cov.get("unclassified_candidate_rows")
        if not isinstance(unknown, int):
            return "已判定部分未发现（无法判定数未提供）"
        if unknown > 0:
            return f"未发现（另有 {unknown:,} 条无法判定）"
        return "已读取候选中未发现" if cov.get("scan_limited") else "已判定部分未发现"

    checks = []
    for key, label in _SIGNAL_LABELS.items():
        shown = count(counts.get(key))
        if key == "high_risk_operation":
            details = [f"{name} {count(ops.get(op))}" for op, name in _OPERATION_LABELS.items()
                       if isinstance(ops.get(op), int) and ops[op] != 0]
            if details:
                shown += "（" + "；".join(details) + "）"
            elif complete and counts.get(key) == 0 and all(
                ops.get(op) == 0 for op in _OPERATION_LABELS
            ):
                shown += "（授权、删表、清表等九项均未命中）"
        checks.append({"检查项": label, "审计行数": shown})
    checks.append({"检查项": "执行失败（ERR，全窗聚合，读取时）",
                   "审计行数": number(data.get("failed_rows"))})

    limited = cov.get("scan_limited")
    scope = (
        f"{fact.target_id} · ["
        f"{audit_local_time(win.get('start_time', '未提供'), win.get('time_zone'))}, "
        f"{audit_local_time(win.get('end_time', '未提供'), win.get('time_zone'))})"
        f"（{win.get('time_zone', '时区未提供')}）｜全窗聚合（读取时）"
        f"{number(data.get('scanned_rows'))} 条｜"
        + (
            "候选已截断"
            if limited is True
            else "候选未截断"
            if limited is False
            else "候选范围未提供"
        )
        + ("｜分类计数完整" if complete else "｜分类计数不完整")
        + f"｜全窗超长记录 {number(cov.get('window_oversize_rows'))} 条"
        + f"｜无法判定候选 {number(cov.get('unclassified_candidate_rows'))} 条"
    )
    notices = ["仅为有限规则的安全线索；审计可能晚到，采集与规则覆盖存在缺口，"
               "不证明攻击成功或维护未经授权。"]
    if data.get("scanned_rows") == 0:
        notices.insert(0, "本窗无审计记录，无法据此判断。")
    omitted = cov.get("omitted_samples")
    if isinstance(omitted, int) and omitted > 0:
        notices.append("样本未全部列出，分类计数完整。" if complete else "样本未全部列出。")

    def labels(value: object, names: dict[str, str]) -> str:
        if isinstance(value, str):
            keys = value.split(", ") if value else []
        else:
            keys = cast(list[str], value) if isinstance(value, list) else []
        return "、".join(names.get(key, key) for key in keys) or "无"

    source_rows = cast(Sequence[dict[str, object]], data.get("rows", fact.rows))
    columns: tuple[str, ...] = ("样本", "时间", "账号", "信号", "操作", "执行状态")
    delay = any("delay_call" in str(row.get("signals", "")) for row in source_rows)
    if delay:
        columns += ("实际耗时（ms）", "排队时间（ms）", "请求延迟（s）")
    rows = []
    for index, row in enumerate(source_rows, 1):
        shown_row: dict[str, object] = {"样本": index}
        for name, key in (("时间", "timestamp"), ("账号", "user"), ("执行状态", "state"),
                          ("实际耗时（ms）", "query_time_ms"),
                          ("排队时间（ms）", "pending_time_ms"),
                          ("请求延迟（s）", "requested_delay_seconds")):
            if name in columns and key in row:
                shown_row[name] = row[key]
        shown_row["信号"] = labels(row.get("signals"), _SIGNAL_LABELS)
        shown_row["操作"] = labels(row.get("operations"), _OPERATION_LABELS)
        rows.append(shown_row)
    return FactPresentation({}, columns, tuple(rows), scope, tuple(checks), tuple(notices))


def business_presentation(fact: DeliveryFact) -> FactPresentation:
    """各渠道共享业务选择与原值，工具专属口径只在这里维护。"""
    data = fact_values(fact)
    if fact.tool_id == SECURITY_TOOL:
        return _security_presentation(fact, data)
    if _control_fact(fact):
        return _control_presentation(fact, data)
    shown = business_fact(fact)
    return FactPresentation(
        {key: data[key] for key in shown.metadata if key in data},
        shown.columns,
        tuple(cast(Sequence[dict[str, object]], data.get("rows", shown.rows)))
        if shown.columns else (),
        notices=fact_limit_notices(fact),
    )


def is_task_fact(fact: DeliveryFact) -> bool:
    return fact.tool_id in {
        "scheduled/get_task", "scheduled/control_task", "scheduled/list_tasks",
        "scheduled/activate_reminder", "scheduled/propose_reminder",
        "scheduled/propose_agent_task",
    } or fact.metadata.get("action") == "scheduled/activate_reminder"


def _control_fact(fact: DeliveryFact) -> bool:
    return (fact.section == "action" or is_task_fact(fact)
            or fact.tool_id == "notifications/send_webhook_message")


def _control_presentation(fact: DeliveryFact, data: dict[str, object]) -> FactPresentation:
    states = {
        "draft": "尚未激活", "active": "已激活", "paused": "已暂停", "cancelled": "已取消",
        "completed": "已结束", "disabled": "已停用", "pending": "等待确认，尚未执行",
        "executing": "执行中，结果尚未确认", "succeeded": "已执行", "rejected": "未执行",
        "unknown": "执行结果未知，不会自动重试", "unavailable": "当前不可核实",
    }

    def task_fields(values: Mapping[str, object]) -> dict[str, object]:
        state = values.get("state")
        label = states.get(str(state), str(state)) if state is not None else None
        if label and fact.note_summary == "此前查询时":
            label = "此前查询时：" + label
        reason = values.get("reason")
        if isinstance(reason, str):
            reason = reason.removeprefix("pending；")
        notify = values.get("notification_state")
        return {key: value for key, value in {
            "任务": values.get("text"), "状态": label,
            "计划时间": values.get("at", values.get("next_run")),
            "有效期": values.get("expires_at"),
            "上次成功时间": values.get("last_success_at"),
            "最近核验时间": values.get("checked_at"),
            "说明": reason if reason not in (None, "pending", "") else None,
            "通知": {
                "accepted": "通知接口已接受，群内投递未确认",
                "unknown": "通知结果未知，不会自动重发",
                "sending": "通知结果尚未确认，不会自动重发", "failed": "通知失败",
            }.get(str(notify)),
        }.items() if value is not None}

    if fact.tool_id == "scheduled/list_tasks":
        tasks = cast(list[dict[str, object]], data.get("tasks", []))
        rows = tuple({"任务": item.get("text") or item["job_id"],
                      **task_fields(item)} for item in tasks)
        columns = ("任务", "状态", "计划时间", "有效期", "说明")
        return FactPresentation({}, columns, rows, notices=fact_limit_notices(fact))
    if fact.tool_id in {
        "scheduled/get_task", "scheduled/control_task", "scheduled/activate_reminder",
    }:
        return FactPresentation(task_fields(data), (), ())
    if (is_task_fact(fact) or fact.tool_id == "scheduled/request_action_execution") and isinstance(
        data.get("result"), str
    ):
        # Action.result 是本地执行器固定保存的 JSON，不解析任意业务字符串。
        try:
            result = json.loads(cast(str, data["result"]))
        except ValueError:
            result = None
        if isinstance(result, dict) and "job_id" in result:
            return FactPresentation(task_fields(result), (), ())
    if fact.tool_id == "notifications/send_webhook_message":
        fields = task_fields({**data, "notification_state": data.get("status")})
        fields.pop("状态", None)
        if "任务" in fields:
            fields["内容"] = fields.pop("任务")
        if data.get("bot_alias") is not None:
            fields["接收方"] = data["bot_alias"]
        if data.get("status") == "not_sent":
            fields["通知"] = "未发送"
        if data.get("reason") == "proxy_http_success":
            fields["通知"] = "代理返回 HTTP 2xx 成功状态，群内投递未确认"
            fields.pop("说明", None)
        return FactPresentation(fields, (), (), notices=fact_limit_notices(fact))
    hidden = {"action_id", "action", "revision"}
    if is_task_fact(fact):
        hidden.add("parameters")  # 仅是内部 job_id；其他写动作的实际参数仍须默认可见。
    fields = {key: value for key, value in data.items() if key not in hidden}
    if "state" in fields:
        fields["state"] = states.get(str(fields["state"]), fields["state"])
        if fact.note_summary == "此前查询时":
            fields["state"] = "此前查询时：" + str(fields["state"])
    return FactPresentation(fields, (), ())


def _raw_redundant(fact: DeliveryFact) -> bool:
    """卡片完整展示时，可见表格/字段已逐值保留原类型，原文 JSON 只是同一数据的第二份。

    只按卡片实际渲染的内容判定：名称经显示转义后不变、列与原文一致、无隐藏的空值字段，
    且实际 SQL 是可展示的字符串；任何不确定都保留原文。
    """
    if fact.result_json is None or fact.historical_brief:
        return False
    data = json.loads(fact.result_json)
    if not isinstance(data, dict):
        return False
    names = [*fact.columns, *fact.metadata]
    if any(display_text(name) != name or "\n" in name or ": " in name for name in names):
        return False  # 转义或“名: 值”文字排版可能使不同名称显示相同
    # 实际 SQL 与 DDL 直接以文字展示：必须是字符串，且显示转义不改写（如 U+2028 与字面 \u2028）。
    if fact.tool_id in _ACTUAL_SQL:
        sql = fact.metadata.get("sql")
        if not isinstance(sql, str) or display_text(sql) != sql:
            return False
    if fact.tool_id == "local/show_create_table" and any(
        not isinstance(ddl := row.get("ddl"), str) or display_text(ddl) != ddl for row in fact.rows
    ):
        return False

    def same(left: object, right: object) -> bool:
        # 按 JSON 文本比较，区分 true/1、大整数与其字符串、嵌套值与其 JSON 文本。
        return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(
            right, sort_keys=True, ensure_ascii=False
        )

    if fact.columns:
        if (
            not fact.rows  # 零行时卡片只写“无数据行”，列名不可见
            or data.get("columns") != list(fact.columns)
            or any(set(row) - set(fact.columns) for row in fact.rows)
            or any(v is None for k, v in fact.metadata.items() if k not in _TECHNICAL)
            or not same(data.get("rows"), [dict(row) for row in fact.rows])
        ):
            return False
        data = {k: v for k, v in data.items() if k not in ("rows", "columns")}
    return same(data, fact.metadata)


def business_fact(fact: DeliveryFact) -> DeliveryFact:
    """用户所需事实的展示视图；实际 SQL 保留在单一技术区，动作批准材料完整保留。"""
    if fact.requires_complete:
        return fact
    return fact.model_copy(
        update={
            "metadata": {
                k: v
                for k, v in fact.metadata.items()
                if k not in _BUSINESS_HIDDEN
                and (k != "warnings" or v not in (None, "", "[]", "null"))
            },
            "result_json": None,
        }
    )


def fact_capture_time(fact: DeliveryFact) -> str:
    """安全事实沿采集时审计时区显示；缺失时保留 ISO，覆盖行说明未换算。"""
    if fact.tool_id == SECURITY_TOOL:
        window = fact_values(fact).get("window")
        zone = window.get("time_zone") if isinstance(window, dict) else None
        return audit_local_time(fact.captured_at, zone, annotate=False)
    return fact.captured_at.isoformat()


def fact_scope(fact: DeliveryFact) -> str:
    """业务来源与快照时间不属于可隐藏的技术包装。"""
    source = (
        "任务记录"
        if fact.target_id == "scheduled"
        else ("通知状态" if fact.target_id == "notifications" else f"数据范围：{fact.target_id}")
    )
    return f"{source} · 采集于 {fact_capture_time(fact)}"


def business_text(
    delivery: Delivery, *, include_approval_command: bool = True, plain_transport: bool = False
) -> str:
    """各纯文本出口复用的业务正文；不读存储、不改投影、不附技术详情。"""

    def rendered(value: str) -> str:
        return display_text(value, multiline=plain_transport)

    def lines(fact: DeliveryFact) -> list[str]:
        if delivery.answer_version == 4 and _control_fact(fact):
            view = business_presentation(fact)
            title = "任务记录" if is_task_fact(fact) else f"动作目标：{fact.target_id}"
            return [title, *(
                rendered(f"{_ACTION_LABELS.get(key, key)}：{value}")
                for key, value in view.fields.items()
                if key != "approval_command" or include_approval_command
            ), *(rendered("；".join(f"{k}：{row.get(k, '未提供')}" for k in view.columns))
                 for row in view.rows), *view.notices]
        if delivery.answer_version == 4 and not fact.requires_complete:
            view = business_presentation(fact)

            def value_text(row: Mapping[str, object], key: str) -> str:
                if key not in row:
                    return "（未提供）"
                value = row[key]
                if plain_transport and isinstance(value, str):
                    return value
                return json.dumps(
                    value, ensure_ascii=False, allow_nan=False,
                    indent=2 if isinstance(value, (dict, list)) else None,
                )

            result = [fact_scope(fact)]
            if view.coverage is not None:
                result.append(view.coverage)
            elif fact.note_summary or fact.note:
                note = (fact.note_summary or fact.note) if plain_transport else (
                    fact.note or fact.note_summary
                )
                result.append("说明：" + rendered(note or ""))
            if fact.tool_id == SECURITY_TOOL:
                result.extend(f"{item['检查项']}: {item['审计行数']}" for item in view.checks)
                result.extend(view.notices)
                if view.rows:
                    result.append(f"代表样本 {len(view.rows)} 条未在消息中列出，可在会话中继续追问")
                return result
            result.extend(view.notices)
            result.extend(f"{item['检查项']}: {item['审计行数']}" for item in view.checks)
            if fact.historical_brief:
                return [*result, fact.summary or ""]
            result.extend(
                rendered(f"{_ACTION_LABELS.get(key, key)}: {value_text(view.fields, key)}")
                for key in view.fields if key != "approval_command" or include_approval_command
            )
            for index, business_row in enumerate(view.rows, 1):
                result.append(f"第 {index} 行")
                result.extend(
                    rendered(f"{key}: {value_text(business_row, key)}") for key in view.columns
                )
            if view.columns and not view.rows:
                result.append("（无数据行）")
            return result
        source = business_fact(fact)
        result = [
            fact_scope(fact),
            *(
                [
                    "说明："
                    + rendered(
                        (fact.note_summary or fact.note or "")
                        if plain_transport
                        else (fact.note or fact.note_summary or "")
                    )
                ]
                if fact.note_summary or fact.note
                else []
            ),
            *fact_limit_notices(fact),
        ]
        if fact.historical_brief:
            return [*result, fact.summary or ""]
        for key, value in source.metadata.items():
            if key != "approval_command" or include_approval_command:
                shown = (
                    value
                    if plain_transport and isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False)
                )
                result.append(f"{_ACTION_LABELS.get(key, key)}: {rendered(shown)}")
        if source.columns:
            if not source.rows:
                result.append("（无数据行）")
            for index, row in enumerate(source.rows, 1):
                result.append(f"第 {index} 行")
                for key in source.columns:
                    value = row.get(key)
                    shown = (
                        value
                        if plain_transport and isinstance(value, str)
                        else json.dumps(value, ensure_ascii=False)
                    )
                    result.append(
                        rendered(f"{key}: {shown}" if key in row else f"{key}: （未提供）")
                    )
        return result

    facts = {f.evidence_id: f for f in delivery.facts}
    result = list(delivery.notices)
    result.extend(line for f in delivery.section("action") for line in lines(f))
    for block in delivery.blocks:
        if isinstance(block, DeliveryFactRef):
            if block.evidence_id in facts:
                result.extend(lines(facts[block.evidence_id]))
        else:
            result.extend(
                (MODEL_TEXT_LABEL, block.text if plain_transport else rendered(block.text))
            )
    return "\n".join(result)


def _data(
    fact: DeliveryFact, *, business: bool = False
) -> tuple[tuple[str, ...], tuple[dict[str, object], ...]]:
    if business:
        shown = business_presentation(fact)
        if fact.historical_brief and not shown.checks:
            return (), ()
        if shown.columns:
            return shown.columns, shown.rows
        return ("字段", "值"), tuple({"字段": k, "值": v} for k, v in shown.fields.items())
    if fact.historical_brief:
        return (), ()
    if fact.columns:
        return fact.columns, tuple(dict(row) for row in fact.rows)
    # 非表格 MCP/标量结果也展示获准事实，技术字段留在折叠区。
    rows: tuple[dict[str, object], ...] = tuple(
        {"字段": k, "值": v} for k, v in fact.metadata.items() if k not in _TECHNICAL
    )
    return ("字段", "值"), rows


def _row_text(
    fact: DeliveryFact, rows: Sequence[dict[str, object]], *, business: bool = False
) -> str:
    columns, _ = _data(fact, business=business)
    return "\n\n".join(
        f"第 {index} 行\n"
        + "\n".join(
            str(row[name])
            if name == "ddl" and fact.tool_id == "local/show_create_table"
            else f"{name}: {_cell(row, name)}"
            for name in columns
        )
        for index, row in enumerate(rows, 1)
    )


def fold_details(fact: DeliveryFact) -> bool:
    """只决定展开密度，不限制结果；动作与历史摘要沿既有契约。

    十行沿现有表格 page_size；2 KiB 文本避免单行长 SQL/监控值铺满正文。
    请求是否容纳仍按最终组件及字节试排，折叠没有压缩或删减内容。
    """
    if fact.section == "action" or fact.requires_complete:
        return False
    if fact.view == "brief" or not fact.columns:
        return True
    _, rows = _data(fact)
    details = _row_text(fact, rows) + "\n" + str(fact.metadata.get("sql", ""))
    return fact.section == "remaining" or len(rows) > 10 or len(details.encode()) > 2048


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
    texts: Sequence[str],
    technical: set[int],
    *,
    omissions: bool = True,
    formatted: bool = False,
    raw_hidden: AbstractSet[int] = frozenset(),
    preview: bool = False,
    folded: AbstractSet[int] = frozenset(),
    attachment: bool = False,
) -> Message:
    """系统说明 → 动作区域 → 正文与就地依据 → 其余结果；旧 v0 仍将依据置后。

    ``texts`` 是本卡展示的正文文字（按顺序，最后一段可能截断）；``counts`` 按 ``facts`` 下标。
    """
    modern = delivery.answer_version == 4
    details_area: list[Message] = []
    elements: list[Message] = [_div(notice) for notice in delivery.notices]
    tables = 0
    region: str | None = None
    row_details: dict[int, str] = {}
    index_of = {fact.evidence_id: i for i, fact in enumerate(delivery.facts)}

    def add_table(
        columns: tuple[str, ...], rows: Sequence[Mapping[str, object]], *, formatted: bool = False
    ) -> None:
        nonlocal tables

        def cell(row: Mapping[str, object], name: str) -> str:
            return display_text(str(row[name])) if formatted else _cell(row, name)

        if len(columns) <= 50 and tables < 5:
            elements.append({
                "tag": "table", "page_size": 10, "row_height": "auto",
                "columns": [
                    {"name": f"c{i}", "display_name": display_text(name), "data_type": "text"}
                    for i, name in enumerate(columns)
                ],
                "rows": [
                    {f"c{i}": cell(row, name) for i, name in enumerate(columns)} for row in rows
                ],
            })
            tables += 1
        else:
            elements.extend(_div("\n".join(f"{name}: {cell(row, name)}" for name in columns))
                            for row in rows)

    def fact_elements(index: int) -> None:
        nonlocal tables, region
        fact = delivery.facts[index]
        if modern and _control_fact(fact):
            control = business_presentation(fact)
            title = (
                "动作执行事实" if fact.requires_complete and "state" in fact.metadata
                else "待你确认（尚未执行）" if "approval_command" in fact.metadata
                else "任务记录" if is_task_fact(fact)
                else f"动作状态 · {fact.target_id}"
            )
            elements.append(_div(title))
            if not is_task_fact(fact) and fact.tool_id != "notifications/send_webhook_message":
                elements.append(_div(f"目标：{fact.target_id}"))
            for key, value in control.fields.items():
                text = display_text(value) if isinstance(value, str) else _cell({key: value}, key)
                elements.append(_div(f"{_ACTION_LABELS.get(key, key)}：{text}"))
            if control.columns:
                add_table(control.columns, control.rows)
            elements.extend(_div(notice) for notice in control.notices)
            return
        if not modern and region != fact.section and (fact.section == "action" or not preview):
            elements.append(_div("动作材料" if fact.section == "action" else FACT_LABEL))
        region = fact.section
        shown = business_presentation(fact) if modern and not fact.requires_complete else None
        columns, all_rows = _data(fact, business=modern)
        rows = all_rows[: counts[index]]
        if fact.requires_complete:
            # 独立审批材料保留原来的完整来源、说明和正文。
            elements.append(
                _div(
                    f"{delivery.result_label(fact.evidence_id)} · 来源 {fact.tool_id}"
                    f" · 目标 {fact.target_id}\n"
                    f"采集于 {fact.captured_at.isoformat()}"
                )
            )
        elif modern:
            elements.append(_div(fact_scope(fact)))
        elif not preview or fact.section == "action":
            elements.append(_div(f"{delivery.result_label(fact.evidence_id)} · {fact.target_id}"))
        elif index not in folded and not fact.historical_brief:
            elements.append(_div(f"数据 · {fact.target_id}"))
        if not (shown and shown.coverage) and (fact.note_summary or (
            fact.note is not None and (modern or not preview or fact.section == "action")
        )):
            elements.append(
                _div(
                    f"说明：{fact.note}"
                    if fact.requires_complete
                    else fact.note_summary or fact.note or ""
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
            return
        if shown is not None and shown.coverage is not None:
            elements.append(_div(shown.coverage))
            add_table(("检查项", "审计行数"), shown.checks, formatted=True)
        visible_metadata = (
            {
                key: value
                for key, value in (
                    shown.fields if shown is not None else fact.metadata
                ).items()
                if key not in _TECHNICAL and fact.columns and (modern or value is not None)
            }
            if modern or not preview or fact.section == "action"
            else {}
        )
        if visible_metadata:
            elements.extend(
                _div(f"{key}: {_cell({key: value}, key)}")
                for key, value in visible_metadata.items()
            )
        elements.extend(_div(notice) for notice in (
            shown.notices if shown is not None else fact_limit_notices(fact)
        ))
        if index in folded:
            if modern and index not in technical and not attachment:
                elements.append(_div(BRIEF_ORIGINAL_OMITTED))
            if not attachment:
                row_details[index] = _row_text(fact, rows, business=modern) or (
                    "明细未展示。" if all_rows else "（无数据行）"
                )
                if not modern and fact.tool_id in _ACTUAL_SQL and index not in sqls:
                    elements.append(_div("SQL 未展示。"))
                if omissions and len(rows) < len(all_rows):
                    elements.append(
                        _div(
                            f"展示 {len(rows)}／本次返回 {len(all_rows)} 行"
                            if fact.columns
                            else "部分明细未展示；完整原值尚未交付。"
                        )
                    )
            return
        if not modern and fact.tool_id == "local/run_readonly_query" and index not in sqls:
            elements.append(_div("SQL 未展示。"))
        if omissions and not fact.historical_brief and len(rows) < len(all_rows):
            elements.append(
                _div(
                    # 新语义预览旁有完整附件；旧 v0 截断卡片没有附件，只说“展示”。
                    ("预览" if preview else "展示")
                    + (
                        f" {len(rows)}／本次返回 {len(all_rows)} 行"
                        if fact.columns
                        else f" {len(rows)}／共 {len(all_rows)} 个字段"
                    )
                )
            )
        if fact.historical_brief and not (shown and shown.checks):
            elements.append(_div(fact.summary or ""))
            if omissions and fact.result_json is not None and index not in technical:
                elements.append(_div(BRIEF_ORIGINAL_OMITTED))
            return
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
        else:
            add_table(columns, rows)

    def fact_details(index: int) -> None:
        fact = delivery.facts[index]
        if modern and _control_fact(fact):
            details_area.append(_div(f"{fact.tool_id} · {fact.evidence_id}\n{fact_scope(fact)}"))
            if fact.note:
                details_area.append(_div(fact.note))
            details_area.append(_div(
                fact.result_json or json.dumps(fact.metadata, ensure_ascii=False)
            ))
            return
        if fact.requires_complete or (modern and index not in technical):
            return
        if modern:
            details_area.append(_div(f"{fact.tool_id} · {fact.evidence_id}\n{fact_scope(fact)}"))
            for key, value in fact.metadata.items():
                if key not in _BUSINESS_HIDDEN:
                    continue
                label = (
                    "结构采集 SQL 模板"
                    if key == "sql" and fact.tool_id in _PAGED
                    else "实际 SQL"
                    if key == "sql" and fact.tool_id in _ACTUAL_SQL
                    else key
                )
                details_area.append(
                    _div(
                        f"{label}:\n{value}"
                        if key == "sql"
                        else f"{label}: {_cell({key: value}, key)}"
                    )
                )
            if fact.note and fact.note_summary:
                details_area.append(_div(fact.note))
            if index in row_details:
                details_area.append(_div(row_details[index]))
            if fact.result_json is not None and (fact.historical_brief or not _raw_redundant(fact)):
                # 不能由已展示值保真替代时，原始获准投影仍可在同一区核对。
                details_area.append(_div(fact.result_json))
            return
        details: list[Message] = [
            _div(
                f"依据：{delivery.result_label(fact.evidence_id)} · 来源 {fact.tool_id}"
                f" · 目标 {fact.target_id}\n"
                f"采集于 {fact.captured_at.isoformat()}"
            )
        ]
        if fact.note and (preview or fact.note_summary):
            details.append(_div(fact.note))
        if index in row_details:
            details.append(_div(row_details[index]))
        if index in sqls:
            details.append(_div(f"实际 SQL:\n{fact.metadata['sql']}"))
        if index in technical and not (attachment and index in folded):
            for key, value in fact.metadata.items():
                if (
                    key in _TECHNICAL or (preview and (fact.columns or index not in row_details))
                ) and not (key == "sql" and fact.tool_id in _ACTUAL_SQL):
                    label = "结构采集 SQL 模板" if key == "sql" and fact.tool_id in _PAGED else key
                    details.append(_div(f"{label}: {_cell({key: value}, key)}"))
            if fact.result_json is not None:
                # 只有可由可见值无损替代的原文才会列入 raw_hidden，并如实说明是主动省略。
                # 只在本卡完整展示该事实（全部行与实际 SQL）时才算重复，否则不能称“逐项一致”。
                complete = counts[index] >= len(_data(fact)[1]) and (
                    fact.tool_id not in _ACTUAL_SQL or index in sqls
                )
                details.append(
                    _div(RAW_OMITTED)
                    if index in raw_hidden and complete
                    else _div(f"原文 JSON:\n{fact.result_json}")
                )
        if details:
            elements.append(
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": _plain(
                            (
                                f"查看依据 · {fact.target_id}"
                                if preview
                                else ("查看明细与依据" if index in row_details else "查看依据")
                                + f" · {delivery.result_label(fact.evidence_id)}"
                            )
                            + (
                                " · 获准原文与查询信息"
                                if fact.historical_brief and index in technical
                                else ""
                            )
                        )
                    },
                    "elements": details,
                }
            )

    def place_fact(index: int) -> None:
        fact_elements(index)
        if preview and not modern:
            fact_details(index)

    for index, fact in enumerate(delivery.facts):
        if fact.section == "action":
            place_fact(index)
    shown_texts = 0
    for block in delivery.blocks:
        if isinstance(block, DeliveryFactRef):
            # 拆分发送时动作材料已独立成分项，本消息不含它；其余事实都在本消息中。
            if block.evidence_id in index_of:
                place_fact(index_of[block.evidence_id])
            continue
        if shown_texts >= len(texts):
            continue
        if region != "model" and (not preview or shown_texts == 0):
            # 代码加注的来源标签：模型文字不能移除，也不能借文字取得事实区域的身份。
            elements.append(_div(MODEL_TEXT_LABEL))
            region = "model"
        text = texts[shown_texts]
        elements.extend(format_prose(display_text(text)) if formatted else [_div(text)])
        shown_texts += 1
    if omissions and tuple(texts) != delivery.texts:
        elements.append(_div(ANALYSIS_CLIPPED))
    placed = {b.evidence_id for b in delivery.blocks if isinstance(b, DeliveryFactRef)}
    for index, fact in enumerate(delivery.facts):
        # 正文事实都应由块放置；任何未被放置的事实仍完整展示，不静默丢弃。
        if fact.section == "body" and fact.evidence_id not in placed:
            place_fact(index)
    remaining = [i for i, f in enumerate(delivery.facts) if f.section == "remaining"]
    if remaining:
        if not preview:
            elements.append(_div(REMAINING_LABEL))
        for index in remaining:
            place_fact(index)
    if omissions and any(
        not f.requires_complete
        and n < len(_data(f, business=modern)[1])
        and not (attachment and i in folded)
        for i, (f, n) in enumerate(zip(delivery.facts, counts, strict=True))
    ):
        elements.append(_div(FACTS_CLIPPED))
    if not preview or modern:
        for index in range(len(delivery.facts)):
            fact_details(index)
    if modern:
        if details_area:
            elements.append(
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {"title": _plain("技术详情")},
                    "elements": details_area,
                }
            )
    else:
        elements.extend(_basis_panels(delivery.basis))
    return _static_card(
        elements,
        (
            ("小维 · 动作确认" if modern else "小维 · 工具结果")
            if any(f.requires_complete for f in delivery.facts)
            else "小维 · AI 运维助手"
        ),
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
    raw: str,
    *,
    notices: Sequence[str] = (),
    clipped: bool = False,
    formatted: bool = False,
    basis: tuple[tuple[str, str], ...] = (),
) -> Message:
    """只有文字的回答：系统说明与模型文字分区，有代码内容时模型文字带代码标签。

    截断说明也是代码内容；标签在候选卡片内生成，调用方的容量检查已计入它。
    """
    text = display_text(raw)
    elements = [_div(n) for n in notices]
    if notices or basis or clipped:
        elements.append(_div(MODEL_TEXT_LABEL))
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
    platform_limits: bool = False,
    attachment_preview: bool = False,
) -> Message:
    """生成受控 post 或结果卡片：必要提醒、回答、完整事实与折叠依据。

    来源/限制放不下时只发固定容量说明；本地异常由调用方记 failed。这里不读另一渠道的投影，
    不靠 SDK 分片，不在发送开始后改发另一消息。

    platform_limits（新语义交付）：卡片与预建 post 只受请求字节与组件约束，纯文本仍受
    max_chars；可由可见值无损替代的原文 JSON 不重复展示；预览先放回答，再放行与 SQL。
    """
    if delivery.channel != "feishu":
        raise ValueError("飞书展示仅接受飞书渠道")

    def fits(message: Message) -> bool:
        # SDK 只对纯文本按字符分段；卡片与预建 post 原样发送，平台只限字节与组件。
        limit_chars = not platform_limits or set(message) == {"text"}
        return (
            (not limit_chars or _chars(message) <= max_chars)
            and _elements(message) <= 200
            and request_bytes(message, chat_id, reply_to=reply_to) <= max_bytes
        )

    notice = capacity_notice(
        delivery.session_notice, notification_notice=delivery.notification_notice
    )

    def omitted_message() -> Message:
        # 完整消息可能比容量回执更短；只在确实需要回执时检查它是否可发送。
        if not fits(notice):
            raise ValueError("单消息预算无法保留容量说明")
        return notice

    if not delivery.facts and not delivery.texts:
        # 只有代码内容（固定回执、系统说明）：纯文本，不含模型文字。
        code_text = "\n".join(
            dict.fromkeys(
                part
                for part in (
                    delivery.content,
                    *(n for n in delivery.notices if n not in delivery.content),
                )
                if part
            )
        )
        if delivery.session_notice is not None or delivery.notification_notice is not None:
            # 固定会话回执必须完整保留原因和操作方法；预算不足记明确发送失败，不裁掉限制。
            receipt = {"text": mention_safe(display_text(code_text))}
            if not fits(receipt):
                raise ValueError("单消息预算无法保留会话回执")
            return receipt

        def code_message(text: str, *, clipped: bool = False) -> Message:
            return {
                "text": mention_safe(display_text(text)) + ("\n" + TEXT_CLIPPED if clipped else "")
            }

        full_code = code_message(code_text)
        if fits(full_code):
            return full_code
        if not allow_preview:
            raise MessageCapacityError("完整正文超过单消息容量")
        low, high = 0, len(code_text)
        while low < high:
            mid = (low + high + 1) // 2
            if fits(code_message(code_text[:mid], clipped=True)):
                low = mid
            else:
                high = mid - 1
        clipped_code = code_message(code_text[:low], clipped=True)
        return clipped_code if fits(clipped_code) else omitted_message()

    if not delivery.facts:
        raw = "\n\n".join(delivery.texts)
        system = delivery.has_system_content

        def prose_message(text: str, *, clipped: bool = False, formatted: bool = False) -> Message:
            return _prose_card(
                text,
                notices=delivery.notices,
                clipped=clipped,
                formatted=formatted,
                basis=() if delivery.answer_version == 4 else delivery.basis,
            )

        shown = display_text(raw)
        if not system:
            # post 只用于纯模型文字：没有任何代码说明、事实或调查依据。
            post = prose_post(shown)
            if post is not None:
                # SDK 降级直接拼接 text runs，不经过 Transport 的 mention_safe。
                # 转换可将 <**at** 拼成 <at，故检查完整 AST 的潜在降级文字。
                fallback_text = "\n".join(
                    "".join(node["text"] for node in row)
                    for row in post["post"]["zh_cn"]["content"]
                )
                if mention_safe(fallback_text) == fallback_text and fits(post):
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

    modern = delivery.answer_version == 4
    folded = frozenset(
        i
        for i, fact in enumerate(delivery.facts)
        if (fact.view == "brief" if modern else platform_limits and fold_details(fact))
        and (not fact.historical_brief or (modern and fact.tool_id == SECURITY_TOOL))
        and fact.section != "action"
    )
    # 附件已接收完整 Delivery，主消息不再复制被收起的大段原值。
    totals = [
        0 if attachment_preview and i in folded else len(_data(f, business=modern)[1])
        for i, f in enumerate(delivery.facts)
    ]
    # 动作区域的事实不预览截断：要么完整展示，要么本消息放不下。
    fixed = {i for i, f in enumerate(delivery.facts) if f.section == "action"}
    all_sql = {
        i
        for i, f in enumerate(delivery.facts)
        if not modern
        and f.tool_id in _ACTUAL_SQL
        and isinstance(f.metadata.get("sql"), str)
        and not (attachment_preview and i in folded)
    }
    all_technical = set(range(len(delivery.facts))) if modern or not attachment_preview else set()
    hidden = (
        frozenset(i for i, f in enumerate(delivery.facts) if _raw_redundant(f))
        if platform_limits
        else frozenset()
    )

    def card(
        counts: Sequence[int],
        sqls: set[int],
        texts: Sequence[str],
        technical: set[int],
        *,
        omissions: bool = True,
        formatted: bool = False,
    ) -> Message:
        return _card(
            delivery,
            counts,
            sqls,
            texts,
            technical,
            omissions=omissions,
            formatted=formatted,
            raw_hidden=hidden,
            preview=platform_limits,
            folded=folded,
            attachment=attachment_preview,
        )

    full = card(totals, all_sql, delivery.texts, all_technical)
    if fits(full):
        formatted = card(totals, all_sql, delivery.texts, all_technical, formatted=True)
        return formatted if fits(formatted) else full
    if modern and not attachment_preview:
        # 技术区放不下时明确未附；不导出技术附件，也不挤掉必需业务说明。
        delivery = delivery.model_copy(
            update={
                "report_notice": "\n".join(
                    filter(
                        None,
                        (
                            delivery.report_notice,
                            TECHNICAL_OMITTED,
                        ),
                    )
                )
            }
        )
        business_only = card(totals, set(), delivery.texts, set(), formatted=True)
        if fits(business_only):
            return business_only
    if not allow_preview:
        # 历史brief的小原文可折叠展示；放不下则只交付已选择的摘要，不因此自动导出原值。
        if any(f.historical_brief for f in delivery.facts):
            brief_technical = {i for i, f in enumerate(delivery.facts) if not f.historical_brief}
            brief_sql = all_sql & brief_technical
            brief = card(totals, brief_sql, delivery.texts, brief_technical)
            if fits(brief):
                return brief
        raise MessageCapacityError("完整结果超过单消息容量")
    texts: list[str] = []
    technical: set[int] = set()

    def fitting_prefix(sqls: set[int]) -> list[int] | None:
        counts = [totals[i] if i in fixed else 0 for i in range(len(totals))]
        best = counts.copy() if fits(card(counts, sqls, texts, technical)) else None
        # 零行占位与截断提示可能比完整短结果更大；只用不含这些提示的下界提前结束搜索。
        # 下界永不交付，实际候选必须包含所有必要的未展示/截断说明。
        if not fits(card(counts, sqls, texts, technical, omissions=False)):
            return best
        for index, total in enumerate(totals):
            if total == 0 or index in fixed:
                continue
            # 完整端点可能移除截断提示，零行端点可能移除/增加占位，不能混进二分区间。
            counts[index] = total
            if fits(card(counts, sqls, texts, technical)):
                best = counts.copy()
                continue
            low, high = 0, total - 1
            while low < high:
                mid = (low + high + 1) // 2
                counts[index] = mid
                if fits(card(counts, sqls, texts, technical)):
                    low = mid
                    best = counts.copy()
                else:
                    high = mid - 1
            counts[index] = total
            if not fits(card(counts, sqls, texts, technical, omissions=False)):
                return best
            # 此端点不合容量但下界合容量：后续短事实可能移除更大的占位，继续检查。
        return best

    sqls: set[int] = set()

    def fit_texts(counts: list[int]) -> None:
        for text in delivery.texts:
            if fits(card(counts, sqls, [*texts, text], technical)):
                texts.append(text)
                continue
            low, high = 0, len(text)
            while low < high:
                mid = (low + high + 1) // 2
                if fits(card(counts, sqls, [*texts, text[:mid]], technical)):
                    low = mid
                else:
                    high = mid - 1
            if low:
                texts.append(text[:low])
            break

    if modern and attachment_preview:
        # 技术区只使用完整回答与必需说明之外的容量；业务预览行可以让位。
        texts.extend(delivery.texts)
        technical.update(all_technical)
        selected_counts = fitting_prefix(set())
        if selected_counts is None:
            technical.clear()
            delivery = delivery.model_copy(
                update={
                    "report_notice": "\n".join(
                        filter(None, (delivery.report_notice, TECHNICAL_OMITTED))
                    )
                }
            )
            selected_counts = fitting_prefix(set())
        if selected_counts is not None:
            plain = card(selected_counts, set(), texts, technical)
            formatted = card(selected_counts, set(), texts, technical, formatted=True)
            return formatted if fits(formatted) else plain
        texts.clear()

    if platform_limits:
        # 正文先回答：模型文字先占预算，fitting_prefix 经闭包读取 texts 再放整行与 SQL。
        fit_texts([totals[i] if i in fixed else 0 for i in range(len(totals))])
    counts = fitting_prefix(sqls)
    if counts is None:
        return omitted_message()
    # SQL 每条完整保留或明示未展示；放不下不阻止展示已有结果行。
    for index in sorted(all_sql):
        selected_counts = fitting_prefix(sqls | {index})
        if selected_counts is not None:
            sqls.add(index)
            counts = selected_counts
    if not platform_limits:
        fit_texts(counts)
    if not (modern and attachment_preview):
        for index in range(len(totals)):
            candidate = card(counts, sqls, texts, technical | {index})
            if fits(candidate):
                technical.add(index)
    plain = card(counts, sqls, texts, technical)
    formatted = card(counts, sqls, texts, technical, formatted=True)
    return formatted if fits(formatted) else plain
