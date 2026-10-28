"""新交付默认：可见业务事实、单一技术区、无技术附件或通知包装。"""

import html
import json

import pytest
from tests.sdk_core.test_feishu_render import delivery, fact, text_parts

from xiaowei.feishu_delivery import _data_parts
from xiaowei.feishu_render import build_feishu_message
from xiaowei.models import Delivery
from xiaowei.webhook import _notification_text


def business(*facts, **changes) -> Delivery:
    return delivery(*facts, analysis=changes.pop("analysis", "分析与建议：继续关注。")).model_copy(
        update={"answer_version": 4, **changes}
    )


def panels(message):
    return [e for e in message["card"]["body"]["elements"] if e["tag"] == "collapsible_panel"]


@pytest.mark.parametrize("state,expected", [
    ("active", "已激活"), ("draft", "尚未激活"), ("unknown", "结果未知"),
    ("rejected", "未执行"),
])
def test_task_receipts_show_business_state_and_real_delivery_limit_without_raw_fields(
    state, expected,
):
    from xiaowei.feishu_render import business_text

    data = {
        "job_id": "internal-job-id", "state": state, "at": "2026-10-26T14:10:00+08:00",
        "expires_at": "2026-11-23T22:13:00+08:00", "reason": "受最早数据期限限制",
        "text": None, "notification_state": "accepted", "receipt_id": "internal-receipt-id",
        "unverified_count": 0, "checked_at": None, "last_success_at": None,
    }
    source = fact(
        target_id="scheduled", tool_id="scheduled/get_task", columns=(), rows=(),
        metadata=data, result_json=json.dumps(data),
    )
    value = business(source)
    visible = "\n".join(text_parts(build_feishu_message(value, 3500, "oc"), folded=False))
    plain = business_text(value)
    for text in (visible, plain):
        assert expected in text and "2026-11-23" in text and "最早数据期限" in text
        assert "群内投递未确认" in text
        assert all(key not in text for key in (
            "internal-job-id", "internal-receipt-id", "notification_state", "字段", "采集于",
            "采集时的结果", "unverified_count",
        ))
        if state == "rejected":
            assert "已拒绝" not in text
    assert source.metadata["job_id"] == "internal-job-id"


def test_protected_approval_is_compact_but_keeps_bound_scope_and_command_in_card_and_plain():
    from xiaowei.feishu_render import business_text

    data = {
        "action_id": "00000000-0000-4000-8000-000000000001",
        "action": "scheduled/activate_reminder", "parameters": '{"job_id":"internal-job-id"}',
        "revision": "internal-version", "change": "目标 fat；只读能力：数据库安全巡检；每1小时",
        "impact": "仅只读，按计划通知指定群", "rollback": "可取消；已准入请求不能撤回",
        "approve_before": "2026-10-26T13:50:00+08:00",
        "approval_command": "@小维 /批准 00000000-0000-4000-8000-000000000001",
    }
    source = fact(
        target_id="scheduled", tool_id="scheduled/propose_agent_task", columns=(), rows=(),
        metadata=data, result_json=json.dumps(data), requires_complete=True, section="action",
    )
    value = business(source)
    visible = "\n".join(text_parts(build_feishu_message(value, 3500, "oc"), folded=False))
    for text in (visible, business_text(value)):
        assert all(part in text for part in (
            "fat", "数据库安全巡检", "每1小时", "仅只读", "可取消", "2026-10-26T13:50",
            "@小维 /批准 00000000-0000-4000-8000-000000000001",
        ))
        assert all(part not in text for part in (
            "internal-job-id", "internal-version", "scheduled/activate_reminder", "采集于",
            "采集时的结果", "字段", "当前版本",
        ))
    assert source.requires_complete and source.metadata == data


@pytest.mark.parametrize("target", ["alertmanager-prod", "notifications", "scheduled"])
@pytest.mark.parametrize("tool", ["propose_create_silence", "get_action_status"])
def test_compact_shared_write_material_keeps_actual_parameters_and_important_change(target, tool):
    from xiaowei.feishu_render import business_text

    source = fact(
        target_id=target, tool_id=f"{target}/{tool}",
        columns=(), rows=(), requires_complete=True, section="action",
        metadata={"action": f"{target}/create_silence",
                  "parameters": '{"matchers":"service=orders","duration":"30m"}',
                  "change": "静默 service=orders，从5分钟改为30分钟", "impact": "暂停通知30分钟",
                  "rollback": "可删除该静默恢复告警", "approve_before": "2026-10-26T13:50:00+08:00",
                  "approval_command": "@小维 /批准 00000000-0000-4000-8000-000000000001"},
    )
    value = business(source)
    visible = "\n".join(text_parts(build_feishu_message(value, 3500, "oc"), folded=False))
    for text in (visible, business_text(value)):
        assert all(part in text for part in (
            target, "service=orders", "duration", "5分钟改为30分钟", "暂停通知30分钟",
            "可删除该静默恢复告警", "2026-10-26T13:50",
            "@小维 /批准 00000000-0000-4000-8000-000000000001",
        ))
        assert "任务记录" not in text


def test_scheduled_execution_receipt_shows_local_task_result_without_internal_json():
    from xiaowei.feishu_render import business_text

    data = {"action_id": "internal-action", "state": "succeeded", "approver": "群成员",
            "result": json.dumps({"job_id": "internal-job", "state": "active",
                                  "at": "2026-10-26T14:10:00+08:00", "reason": "activated"})}
    source = fact(
        target_id="scheduled", tool_id="scheduled/request_action_execution",
        columns=(), rows=(), section="action", requires_complete=True,
        metadata=data, result_json=json.dumps(data),
    )
    value = business(source)
    visible = "\n".join(text_parts(build_feishu_message(value, 3500, "oc"), folded=False))
    for text in (visible, business_text(value)):
        assert "已激活" in text and "2026-10-26T14:10" in text
        assert "internal-job" not in text and '"job_id"' not in text


@pytest.mark.parametrize("target", ["scheduled", "notifications"])
def test_cluster_name_does_not_classify_business_query_as_control_receipt(target):
    from xiaowei.feishu_render import business_text

    source = fact(
        target_id=target, columns=("region", "total"), rows=({"region": "east", "total": 7},),
        metadata={}, result_json=None,
    )
    value = business(source)
    visible = "\n".join(text_parts(build_feishu_message(value, 3500, "oc"), folded=False))
    for text in (visible, business_text(value)):
        assert "east" in text and "7" in text
        assert f"数据范围：{target}" in text
        assert "任务记录" not in text and "通知状态" not in text


def security_fact(*, coverage=None, payload_updates=None, **changes):
    """人工核对的报告夹具；统计不通过检测器或展示函数计算。"""
    from xiaowei.starrocks_security import SECURITY_TOOL

    rows = [{
        "query_id": "q-client-probe", "timestamp": "2026-10-23T15:04:37+08:00",
        "user": "client", "state": "OK", "signals": "stacked_statements",
        "operations": "", "query_time_ms": 4, "pending_time_ms": 0,
        "requested_delay_seconds": None,
    }]
    data = {
        "window": {"start_time": "2026-10-23T15:00:00+08:00",
                   "end_time": "2026-10-23T16:00:00+08:00", "time_zone": "Asia/Shanghai"},
        "source": "audit.events", "rules_version": "manual-security-v1",
        "scanned_rows": 29232, "failed_rows": 6, "matched_rows": 1,
        "signal_counts": {"boolean_probe": 0, "delay_call": 0,
                          "stacked_statements": 1, "high_risk_operation": 0},
        "operation_counts": dict.fromkeys((
            "grant", "revoke", "account", "password", "drop", "truncate", "export",
            "insert_overwrite", "delete_no_where",
        ), 0),
        "columns": list(rows[0]), "rows": rows, "row_count": 1,
        "coverage": {"scan_limited": False, "checked_candidate_rows": 1,
                     "scanned_through": None, "window_oversize_rows": 3,
                     "unclassified_candidate_rows": 0, "match_counts_complete": True,
                     "omitted_samples": 0, **(coverage or {})},
        "limitations": ["有限规则；审计可能晚到。"],
        **(payload_updates or {}),
    }
    return fact(
        tool_id=SECURITY_TOOL, columns=tuple(data["columns"]), rows=tuple(data["rows"]),
        metadata={key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list))
                  else value for key, value in data.items() if key not in {"columns", "rows"}},
        result_json=json.dumps(data, ensure_ascii=False),
        **changes,
    )


def v2_security_fact(**changes):
    data = json.loads(security_fact().result_json)
    data["rules_version"] = "manual-security-v2"
    data["signal_counts"].pop("high_risk_operation")
    data["operation_counts"] = {}
    data["columns"].remove("operations")
    for row in data["rows"]:
        row.pop("operations")
    return security_fact(payload_updates=data, **changes)


@pytest.mark.parametrize("view", ["full", "brief"])
def test_v2_security_report_keeps_counts_without_legacy_operation_fields(view):
    from xiaowei.feishu_render import business_presentation, business_text
    from xiaowei.feishu_report import build_report

    source = v2_security_fact(view=view)
    shown = business_presentation(source)
    assert len(shown.checks) == 4
    assert "操作" not in shown.columns
    assert shown.rows[0]["信号"] == "多语句（辅助信息）"
    value = business(source)
    card = "\n".join(text_parts(build_feishu_message(value, 3500, "oc"), folded=False))
    texts = [
        card, business_text(value, plain_transport=True),
        json.dumps(shown.for_web(), ensure_ascii=False),
    ]
    texts.extend(build_report(value.model_copy(update={"report_format": fmt})).content.decode()
                 for fmt in ("md", "html"))
    for text in texts:
        assert "高风险操作" not in text and "九项" not in text
        assert "多语句（辅助信息）" in text and "29,232" in text
        assert "分类计数完整" in text and "无法判定候选 0" in text
    assert json.loads(source.result_json)["rules_version"] == "manual-security-v2"


@pytest.mark.parametrize("view", ["full", "brief"])
def test_security_counts_and_coverage_are_business_facts_even_when_brief(view):
    value = business(security_fact(view=view), analysis="🟡 有需核查线索，业务用途待核对。")
    message = build_feishu_message(value, 3500, "oc", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    assert "29,232" in visible and "全窗聚合（读取时）" in visible
    assert all(label in visible for label in (
        "注入探测", "时间延迟", "多语句", "高风险操作", "执行失败",
    ))
    assert "分类计数完整" in visible and "无法判定候选 0" in visible
    assert all(key not in visible for key in ("rules_version", "signal_counts", "q-client-probe"))
    assert "q-client-probe" in "\n".join(text_parts(message))
    assert len(panels(message)) == 1 and not panels(message)[0]["expanded"]
    if view == "brief":
        assert "client" not in visible


@pytest.mark.parametrize("limited,unclassified", [(True, 0), (False, 2)])
def test_security_incomplete_counts_are_lower_bounds_but_err_is_read_time_aggregate(
    limited, unclassified,
):
    source = security_fact(coverage={
        "scan_limited": limited, "unclassified_candidate_rows": unclassified,
        "match_counts_complete": False,
    }, truncated=True, view="brief")
    value = business(source)
    visible = "\n".join(text_parts(
        build_feishu_message(value, 3500, "oc", platform_limits=True), folded=False,
    ))
    assert ("候选已截断" in visible) is limited
    assert "分类计数不完整" in visible
    assert "已确认至少 1" in visible
    assert ("未发现（另有 2 条无法判定）" if unclassified else "已读取候选中未发现") in visible
    assert "九项均未命中" not in visible
    plain = _notification_text(value, "")
    assert "全窗聚合（读取时）" in plain and "执行失败" in plain
    assert "规则版本" not in plain and "q-client-probe" not in plain


def annotations_fact():
    """现有 Grafana get_annotations 契约：text 是字符串，tags 可为空或缺失。"""
    values = {"annotations": [
        {"id": 1, "time": 1792738800000, "text": "null", "tags": None},
        {"id": 2, "time": 1792738801000, "text": "<script>literal</script>"},
    ]}
    return fact(
        tool_id="grafana/get_annotations", columns=(), rows=(),
        metadata={"annotations": json.dumps(values["annotations"])},
        result_json=json.dumps(values),
    )


def test_non_security_nested_values_stay_typed_in_card_plain_and_web():
    from xiaowei.channel import RequestView
    from xiaowei.feishu_render import _raw_redundant
    from xiaowei.storage import Readiness
    from xiaowei.web import _view

    source = annotations_fact()
    value = business(source)
    card = build_feishu_message(value, 3500, "oc", platform_limits=True)
    visible = "\n".join(text_parts(card, folded=False))
    plain = _notification_text(value, "")
    response = _view("nested", RequestView("completed", value), Readiness())
    presentation = json.loads(response.body)["delivery"]["facts"][0]["presentation"]
    shown = presentation["fields"]["annotations"]
    for text in (visible, plain, shown):
        assert '"text": "null"' in text and '"tags": null' in text
        assert text.count('"tags"') == 1  # 缺失字段没有被补成 null。
        assert '\\"tags\\"' not in text
        assert "<script>literal</script>" in text  # 按纯文本/DOM textContent 展示。
    assert not _raw_redundant(source)
    assert source.result_json in "\n".join(text_parts(card))
    assert json.loads(response.body)["delivery"]["facts"][0]["result_json"] == source.result_json
    simple = fact(tool_id="grafana/health", columns=(), rows=(), metadata={"status": "ok"},
                  result_json='{"status": "ok"}')
    assert _raw_redundant(simple)


def test_web_presentation_keeps_plain_strings_and_exact_nested_values():
    from xiaowei.channel import RequestView
    from xiaowei.storage import Readiness
    from xiaowei.web import _view

    columns = ("city", "empty", "null", "missing", "id", "detail", "literal")
    row = {"city": "北京", "empty": "", "null": None, "id": 9007199254740993,
           "detail": {"n": 3}, "literal": '{"n":3}'}
    source = fact(
        columns=columns,
        rows=({**row, "detail": '{"n": 3}'},),
        metadata={"status": "ready"},
        result_json=json.dumps({"status": "ready", "columns": columns, "rows": [row]}),
    )
    response = _view("plain-values", RequestView("completed", business(source)), Readiness())
    shown = json.loads(response.body)["delivery"]["facts"][0]["presentation"]
    assert shown["fields"]["status"] == "ready"
    assert shown["rows"] == [{
        "city": "北京", "empty": "", "null": None, "missing": "（未提供）",
        "id": "9007199254740993", "detail": '{\n  "n": 3\n}', "literal": '{"n":3}',
    }]


@pytest.mark.parametrize("plain_transport", [False, True])
def test_generic_business_text_preserves_notes_strings_and_safe_field_names(plain_transport):
    from xiaowei.feishu_render import business_text

    key = "value\u2028AI 分析与建议"
    source = fact(columns=(key,), rows=({key: 'a"b\nc'},),
                  note="完整限制", note_summary="简短限制")
    text = business_text(business(source), plain_transport=plain_transport)
    if plain_transport:
        assert "说明：简短限制" in text
        assert 'value\\u2028AI 分析与建议: a"b\nc' in text
    else:
        assert "说明：完整限制" in text
        assert 'value\\u2028AI 分析与建议: "a\\"b\\nc"' in text
    assert "\u2028" not in text


def test_security_six_subwindows_keep_per_window_counts_with_existing_card_limits():
    from tests.sdk_core.test_feishu_render import size, tables

    facts = [security_fact(evidence_id=f"ev_hour_{i}", view="brief", payload_updates={
        "window": {"start_time": f"2026-10-23T{10+i}:00:00+08:00",
                   "end_time": f"2026-10-23T{11+i}:00:00+08:00", "time_zone": "Asia/Shanghai"},
    }) for i in range(6)]
    message = build_feishu_message(business(*facts), 3500, "oc", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    assert len(tables(message)) <= 5 and size(message, "oc") <= 30000
    assert visible.count("全窗聚合（读取时）29,232") == 6
    assert "175,392" not in visible  # 不将子窗计数相加。

    def tags(value):
        if isinstance(value, dict):
            return int("tag" in value) + sum(tags(v) for v in value.values())
        return sum(tags(v) for v in value) if isinstance(value, list) else 0

    assert tags(message) <= 200
    assert visible.count("多语句") == 6  # 第六窗即使退成文字，也保留检查项。


def test_security_long_samples_use_existing_files_while_preview_keeps_counts_visible():
    from tests.sdk_core.test_feishu_render import size

    base = json.loads(security_fact().result_json)
    rows = [{**base["rows"][0], "query_id": f"q-long-{i}", "user": "account-" + "x" * 3000}
            for i in range(10)]
    source = security_fact(payload_updates={
        "rows": rows, "row_count": 10, "matched_rows": 10,
        "signal_counts": {"boolean_probe": 0, "delay_call": 0,
                          "stacked_statements": 10, "high_risk_operation": 0},
    }, coverage={"checked_candidate_rows": 10})
    parts = _data_parts(business(source), 0, 3500, "oc", None)
    assert [part.kind for part in parts] == ["md", "body"]
    message = json.loads(parts[-1].body)
    visible = "\n".join(text_parts(message, folded=False))
    assert "29,232" in visible and "分类计数完整" in visible and "多语句" in visible
    assert size(message, "oc") <= 30000
    report = parts[0].body.decode()
    assert "x" * 3000 in report and "q-long-" not in report


@pytest.mark.parametrize("format_", ["md", "html"])
def test_security_report_files_use_the_same_chinese_counts_and_sample_mapping(format_):
    from xiaowei.feishu_report import build_report

    report = build_report(business(security_fact(), report_format=format_))
    text = html.unescape(report.content.decode())
    assert "全窗聚合（读取时）29,232" in text and "多语句" in text
    assert "执行失败" in text and "client" in text
    assert all(word not in text for word in ("signal_counts", "rules_version", "q-client-probe"))


def test_old_security_v4_uses_new_coverage_without_rewriting_model_text():
    from xiaowei.channel import RequestView
    from xiaowei.storage import Readiness
    from xiaowei.web import _view

    source = security_fact(coverage={"scanned_through": "2026-10-23T15:04:37+08:00"},
                           view="brief", summary="旧的 1 行摘要")
    value = business(source, analysis="旧说明：15:04:37 候选，原样保存。")
    response = _view("old-security", RequestView("completed", value), Readiness())
    shown = json.loads(response.body)["delivery"]
    assert "15:04:37" not in shown["facts"][0]["presentation"]["coverage"]
    assert "29,232" in shown["facts"][0]["presentation"]["coverage"]
    assert "15:04:37" in shown["facts"][0]["result_json"]
    assert shown["blocks"][0]["text"] == "旧说明：15:04:37 候选，原样保存。"


@pytest.mark.parametrize("transport", ["card", "plain"])
def test_historical_security_brief_keeps_statistics_without_expanding_samples(transport):
    value = business(security_fact(view="brief", summary="本次返回行数：1。"))
    if transport == "card":
        message = build_feishu_message(value, 3500, "oc", platform_limits=True)
        visible = "\n".join(text_parts(message, folded=False))
        assert "client" in "\n".join(text_parts(message))  # 明细仍可在技术区核对。
    else:
        visible = _notification_text(value, "")
    assert "29,232" in visible and "多语句" in visible and "分类计数完整" in visible
    assert "client" not in visible and "15:04:37" not in visible


@pytest.mark.parametrize("format_", ["md", "html"])
def test_split_security_sample_sections_keep_their_own_scope_and_capture_time(
    monkeypatch, format_,
):
    import re

    from xiaowei.feishu_report import build_reports

    # HTML 用默认400k，复现真实门槛；MD 用小容量同时覆盖整行分片，不改生产限额。
    if format_ == "md":
        monkeypatch.setattr("xiaowei.feishu_report.MAX_REPORT_BYTES", 6000)
    base = json.loads(security_fact().result_json)
    facts = []
    for hour in range(10 if format_ == "html" else 2):
        start = f"2026-10-23T{hour:02}:00:00+08:00"
        end = f"2026-10-23T{hour+1:02}:00:00+08:00"
        rows = [{**base["rows"][0], "query_id": f"q-{hour}-{i}",
                 "timestamp": f"2026-10-23T{hour:02}:01:00+08:00",
                 "user": f"window{hour}_" + "x" * 56} for i in range(100)]
        facts.append(security_fact(
            evidence_id=f"ev_window_{hour}", target_id="fat", payload_updates={
                "window": {"start_time": start, "end_time": end, "time_zone": "Asia/Shanghai"},
                "rows": rows, "row_count": 100, "matched_rows": 100,
                "signal_counts": {**base["signal_counts"], "stacked_statements": 100},
            }, coverage={"checked_candidate_rows": 100},
        ))
    reports = build_reports(business(*facts, report_format=format_))
    assert len(reports) > 1
    seen = set()
    for report in reports:
        text = html.unescape(report.content.decode())
        sections = (
            re.findall(r"<section\b.*?</section>", text, re.S)
            if format_ == "html" else text.split("\n## ")
        )
        for section in sections:
            if "代表样本" not in section:
                continue
            (hour,) = {int(h) for h in re.findall(r"window(\d+)_", section)}
            seen.add(hour)
            assert "fat" in section
            assert f"2026-10-23T{hour:02}:00:00+08:00" in section
            assert f"2026-10-23T{hour+1:02}:00:00+08:00" in section
            from zoneinfo import ZoneInfo

            assert (
                facts[hour].captured_at.astimezone(ZoneInfo("Asia/Shanghai")).isoformat() in section
            )
    assert seen == set(range(len(facts)))


@pytest.mark.parametrize("total", [0, 123])
def test_security_zero_hits_preserve_the_difference_between_no_data_and_no_signal(total):
    from xiaowei.feishu_render import business_presentation

    source = security_fact(payload_updates={
        "scanned_rows": total, "failed_rows": 0, "matched_rows": 0, "rows": [], "row_count": 0,
        "signal_counts": {"boolean_probe": 0, "delay_call": 0,
                          "stacked_statements": 0, "high_risk_operation": 0},
    }, coverage={"checked_candidate_rows": 0, "window_oversize_rows": 0})
    shown = business_presentation(source)
    assert len(shown.checks) == 5 and shown.rows == ()
    assert shown.checks[0]["审计行数"] == "0" and shown.checks[-1]["审计行数"] == "0"
    assert "九项均未命中" in shown.checks[3]["审计行数"]
    assert ("本窗无审计记录" in "".join(shown.notices)) is (total == 0)


def test_security_samples_omitted_does_not_turn_counts_into_lower_bounds():
    from xiaowei.feishu_render import business_presentation
    from xiaowei.feishu_report import build_reports

    source = security_fact(coverage={"omitted_samples": 3}, truncated=True)
    shown = business_presentation(source)
    assert shown.checks[2]["审计行数"] == "1"
    assert "样本未全部列出，分类计数完整。" in shown.notices
    assert "分类计数完整" in shown.coverage
    assert "已确认至少" not in str(shown.checks)
    reports = build_reports(business(source, report_format="md"))
    text = "\n".join(report.content.decode() for report in reports)
    assert "工具结果已截断" not in text
    assert "样本未全部列出，分类计数完整" in text


def test_security_missing_counts_are_unknown_and_nonzero_operations_stay_compact():
    from xiaowei.feishu_render import business_presentation

    missing = business_presentation(security_fact(payload_updates={"signal_counts": {}}))
    assert len(missing.checks) == 4
    assert all(row["审计行数"] == "未提供" for row in missing.checks[:-1])
    source = security_fact(payload_updates={
        "signal_counts": {"boolean_probe": 0, "delay_call": 0,
                          "stacked_statements": 0, "high_risk_operation": 3},
        "operation_counts": {"grant": 1, "drop": 1, "truncate": 1},
    })
    shown = business_presentation(source)
    assert len(shown.checks) == 5
    assert shown.checks[3]["审计行数"] == "3（授权 1；删表/库 1；清空表 1）"


def test_v4_single_technical_area_and_visible_business_scope() -> None:
    value = business(fact(), fact(evidence_id="ev_other", target_id="fat", truncated=True))
    message = build_feishu_message(value, 3500, "oc", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    assert len(panels(message)) == 1
    assert panels(message)[0]["header"]["title"]["content"] == "技术详情"
    assert panels(message)[0]["expanded"] is False
    for word in (
        "查看依据",
        "结果 1",
        "工具结果（系统根据证据生成）",
        "local/",
        "ev_",
        "SELECT",
        "elapsed_ms",
    ):
        assert word not in visible
    assert "orders" in visible and "pre" in visible and "fat" in visible
    assert "2026-10-09" in visible and "已截断" in visible
    assert "SELECT name" in "\n".join(text_parts(message))


def test_v4_non_tabular_prometheus_result_is_still_a_fact() -> None:
    source = fact(
        tool_id="prometheus-pre/query",
        columns=(),
        rows=(),
        metadata={
            "query": "avg_over_time(up[1h])",
            "result": '{instance="host"} => 0.5 @[1792673695]',
            "warnings": "部分样本不可用",
        },
    )
    message = build_feishu_message(business(source), 3500, "oc", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    assert "host" in visible and "0.5" in visible
    assert "数据源返回警告" in visible
    assert "avg_over_time" not in visible


def test_unasked_large_technical_data_does_not_create_an_attachment() -> None:
    source = fact(metadata={"sql": "SELECT '" + "x" * 40_000 + "'", "elapsed_ms": 42})
    parts = _data_parts(business(source), 0, 3500, "oc", None)
    assert [p.kind for p in parts] == ["body"]
    message = json.loads(parts[0].body)
    assert "orders" in "\n".join(text_parts(message, folded=False))
    assert "本次完整结果见附件" not in str(message)
    assert "技术详情超出消息容量，未附" in str(message)


@pytest.mark.parametrize("format_", ["md", "html"])
def test_v4_report_contains_business_not_duplicate_technical_files(format_: str) -> None:
    parts = _data_parts(business(fact(), report_format=format_), 0, 3500, "oc", None)
    assert [p.kind for p in parts] == [format_, "body"]
    report = parts[0].body.decode()
    assert "orders" in report and "pre" in report and "2026-10-09" in report
    for word in ("ev_original", "查看依据", "<details", "证据明细", "elapsed_ms", "SELECT name"):
        assert word not in report
    message = json.loads(parts[-1].body)
    assert len(panels(message)) == 1
    assert "SELECT name FROM shop.orders LIMIT 5" in "\n".join(text_parts(message))
    assert "SELECT name" not in "\n".join(text_parts(message, folded=False))


def test_v4_notification_omits_technical_metadata_and_approval_command() -> None:
    source = fact(
        metadata={"sql": "SELECT name", "approval_command": "/批准 act_x", "status": "unknown"}
    )
    text = _notification_text(business(source, notification_notice="结果未知，不会自动重发"), "")
    assert "orders" in text and "unknown" in text and "不会自动重发" in text
    assert "pre" in text and "2026-10-09" in text
    for word in ("SELECT", "/批准", "工具结果", "ev_", "local/"):
        assert word not in text


def test_v4_brief_monitoring_value_uses_the_single_technical_area() -> None:
    source = fact(
        tool_id="prometheus/query",
        view="brief",
        columns=(),
        rows=(),
        metadata={"query": "up", "result": 'node_uname_info{instance="host"} => 1'},
    )
    message = build_feishu_message(business(source), 3500, "oc", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    assert "node_uname_info" not in visible
    assert "node_uname_info" in "\n".join(text_parts(message))
    assert len(panels(message)) == 1


@pytest.mark.parametrize("format_", ["md", "html"])
@pytest.mark.parametrize("expand_brief", [False, True], ids=["automatic", "explicit"])
def test_v4_business_file_keeps_nested_value_types_without_technical_metadata(
    format_: str, expand_brief: bool
) -> None:
    from xiaowei.feishu_report import build_reports

    original = {
        "columns": ["value", "sql"],
        "rows": [{"value": {"nested": 1}, "sql": "SELECT business_row"}],
        "sql": "SELECT nested",
        "status": {"checked": True},
    }
    source = fact(
        columns=("value", "sql"),
        rows=({"value": '{"nested": 1}', "sql": "SELECT business_row"},),
        metadata={"sql": "SELECT nested", "status": '{"checked": true}'},
        result_json=json.dumps(original),
    )
    (file,) = build_reports(business(source, report_format=format_), expand_brief=expand_brief)
    content = html.unescape(file.content.decode())
    assert '"nested": 1' in content and '"checked": true' in content
    assert '\\"nested\\"' not in content and "SELECT nested" not in content
    assert "SELECT business_row" in content


@pytest.mark.parametrize(
    "tool,sql,label",
    [
        ("local/list_databases", "objects", "结构采集 SQL 模板"),
        ("local/list_tables", "objects", "结构采集 SQL 模板"),
        ("local/describe_table", "columns", "结构采集 SQL 模板"),
        ("local/run_readonly_query", "SELECT name\nFROM shop.orders", "实际 SQL"),
    ],
)
def test_v4_distinguishes_schema_templates_from_actual_sql(tool: str, sql: str, label: str) -> None:
    from xiaowei.starrocks import SCHEMA_COLUMNS_SQL, SCHEMA_OBJECTS_SQL

    actual = {"objects": SCHEMA_OBJECTS_SQL, "columns": SCHEMA_COLUMNS_SQL}.get(sql, sql)
    message = build_feishu_message(
        business(fact(tool_id=tool, metadata={"sql": actual})),
        3500,
        "oc",
        platform_limits=True,
    )
    assert actual not in "\n".join(text_parts(message, folded=False))
    detail = "\n".join(text_parts(message))
    assert f"{label}:\n{actual}" in detail
    if label == "结构采集 SQL 模板":
        assert "实际 SQL" not in detail


def task_status_delivery() -> Delivery:
    """各载体使用同一任务事实；两个状态时间不同于采集时间。"""
    return business(
        fact(
            tool_id="scheduled/get_task",
            target_id="scheduled",
            columns=(),
            rows=(),
            metadata={
                "task_id": "job_status",
                "state": "active",
                "last_success_at": "2026-10-09T10:05:00+00:00",
                "checked_at": "2026-10-09T10:06:00+00:00",
                "receipt_id": "nr_internal",
            },
        )
    )


@pytest.mark.parametrize("carrier", ["feishu", "notification"])
def test_v4_task_status_times_are_visible_without_opening_details(carrier: str) -> None:
    value = task_status_delivery()
    if carrier == "feishu":
        message = build_feishu_message(value, 3500, "oc", platform_limits=True)
        visible = "\n".join(text_parts(message, folded=False))
        assert len(panels(message)) <= 1
    else:
        visible = _notification_text(value, "")
    assert "2026-10-09T10:05:00+00:00" in visible
    assert "2026-10-09T10:06:00+00:00" in visible
    assert "nr_internal" not in visible


@pytest.mark.parametrize("sql_bytes", [80, 40_000])
def test_v4_automatic_attachment_keeps_one_technical_area_or_explicit_omission(
    sql_bytes: int,
) -> None:
    source = fact(
        rows=tuple({"name": f"orders-{i}-" + "x" * 80} for i in range(400)),
        metadata={"sql": "SELECT '" + "x" * sql_bytes + "'", "elapsed_ms": 42},
    )
    parts = _data_parts(business(source), 0, 3500, "oc", None)
    assert [p.kind for p in parts] == ["md", "body"]
    report = parts[0].body.decode()
    assert "orders-399-" in report
    assert "elapsed_ms" not in report and "SELECT '" not in report
    message = json.loads(parts[-1].body)
    assert len(panels(message)) <= 1
    visible = "\n".join(text_parts(message, folded=False))
    if sql_bytes == 80:
        assert len(panels(message)) == 1
        assert source.metadata["sql"] in "\n".join(text_parts(message))
        assert source.metadata["sql"] not in visible
    else:
        assert "技术详情" in visible and "未附" in visible


@pytest.mark.parametrize(
    "sql_value,keep_technical",
    [("x" * 80, True), ('"' * 5000, False), ("x" * 13_000, False)],
    ids=["short-sql", "long-escaped-sql", "long-sql"],
)
def test_v4_attachment_keeps_complete_answer_before_technical_details(
    sql_value: str, keep_technical: bool
) -> None:
    from xiaowei.models import AgentAnswer, DeliveryText, FactBlock, TextBlock

    answer = "巡检结论：" + "巡" * 5300
    # 先验证回答符合真实模型输出的字节预算，不能用越界文字制造容量缺陷。
    AgentAnswer(
        blocks=(
            TextBlock(type="text", text=answer),
            FactBlock(type="fact", evidence_id="ev_original", view="full"),
        ),
        report_format=None,
        awaiting_user=False,
    )
    source = fact(
        rows=tuple({"name": f"orders-{i}-" + "x" * 80} for i in range(400)),
        metadata={"sql": "SELECT '" + sql_value + "' AS value LIMIT 1", "elapsed_ms": 42},
    )
    value = business(source)
    value = value.model_copy(update={"blocks": (DeliveryText(text=answer), *value.blocks[1:])})
    parts = _data_parts(value, 0, 3500, "oc", None)
    assert [p.kind for p in parts] == ["md", "body"]
    assert "orders-399-" in parts[0].body.decode()
    message = json.loads(parts[-1].body)
    visible = "\n".join(text_parts(message, folded=False))
    assert answer in visible
    assert "模型说明展示已截断" not in visible
    assert "本次完整结果见附件" in visible
    assert "采集时的结果，历史不代表当前状态。" in visible
    assert len(panels(message)) == int(keep_technical)
    if keep_technical:
        assert source.metadata["sql"] in "\n".join(text_parts(message))
        assert "技术详情超出消息容量，未附。" not in visible
    else:
        assert "技术详情超出消息容量，未附。" in visible
        assert source.metadata["sql"] not in "\n".join(text_parts(message))


def test_v4_attachment_without_technical_details_keeps_answer_with_many_short_facts() -> None:
    from xiaowei.models import AgentAnswer, DeliveryText, FactBlock, TextBlock

    answer = "x" * 8000
    sources = tuple(
        fact(
            evidence_id=f"ev_{i}",
            rows=({"name": f"orders-{i}"},),
            metadata={"sql": "SELECT x"},
            note=None,
        )
        for i in range(34)
    )
    AgentAnswer(
        blocks=(
            TextBlock(type="text", text=answer),
            *(FactBlock(type="fact", evidence_id=f.evidence_id, view="full") for f in sources),
        ),
        report_format="md",
        awaiting_user=False,
    )
    value = business(*sources, report_format="md")
    value = value.model_copy(update={"blocks": (DeliveryText(text=answer), *value.blocks[1:])})
    parts = _data_parts(value, 0, 3500, "oc", None)
    assert [p.kind for p in parts] == ["md", "body"]
    assert "orders-33" in parts[0].body.decode()
    message = json.loads(parts[-1].body)
    visible = "\n".join(text_parts(message, folded=False))
    assert answer in visible
    assert "orders-33" in visible
    assert "模型说明展示已截断" not in visible
    assert len(panels(message)) == 0
    assert "技术详情超出消息容量，未附。" in visible


def test_v4_attachment_fallback_explicitly_reports_omitted_technical_details() -> None:
    source = fact(note="限制说明 " * 5000)
    parts = _data_parts(business(source, report_format="md"), 0, 3500, "oc", None)
    assert [p.kind for p in parts] == ["md", "body"]
    message = json.loads(parts[-1].body)
    visible = "\n".join(text_parts(message, folded=False))
    assert "预览无法在本消息容量内展示" in visible
    assert "技术详情" in visible and "未附" in visible
    assert "SELECT name" not in visible
    assert "限制说明" in parts[0].body.decode()


def test_v4_attachment_uses_real_layout_when_empty_placeholders_are_larger() -> None:
    from xiaowei.models import DeliveryText

    sources = tuple(
        fact(
            evidence_id=f"ev_{i}",
            rows=({"name": f"orders-{i}-" + "x" * 80},),
            metadata={"sql": "SELECT x"},
            note=None,
        )
        for i in range(22)
    )
    large = fact(
        evidence_id="ev_large",
        rows=tuple({"name": f"large-{i}-" + "x" * 80} for i in range(400)),
        metadata={"sql": "SELECT name"},
        note=None,
    )
    value = business(*sources, large)
    answer = "x" * 8000
    # 完整回答、短事实与技术区能共存；零行占位反而超出组件预算，不能作为拒收依据。
    value = value.model_copy(update={"blocks": (DeliveryText(text=answer), *value.blocks[1:])})
    parts = _data_parts(value, 0, 3500, "oc", None)
    assert [p.kind for p in parts] == ["md", "body"]
    assert "large-399-" in parts[0].body.decode()
    message = json.loads(parts[-1].body)
    visible = "\n".join(text_parts(message, folded=False))
    assert answer in visible
    assert len(panels(message)) == 1
    assert "技术详情超出消息容量，未附" not in visible
    assert "SELECT x" in "\n".join(text_parts(message))


def many_security_samples(*, count=61, **changes):
    from datetime import UTC, datetime

    data = json.loads(security_fact().result_json)
    rows = [
        {
            **data["rows"][0],
            "query_id": f"q-plain-{i}",
            "user": "etl-reader",
            "signals": "high_risk_operation",
            "operations": "insert_overwrite",
        }
        for i in range(count)
    ]
    return security_fact(
        captured_at=datetime(2026, 10, 23, 8, tzinfo=UTC),
        payload_updates={
            "rows": rows,
            "row_count": count,
            "matched_rows": count,
            "signal_counts": {
                **dict.fromkeys(data["signal_counts"], 0),
                "high_risk_operation": count,
            },
            "operation_counts": {**data["operation_counts"], "insert_overwrite": count},
            "window": {
                "start_time": "2026-10-23T07:00:00+00:00",
                "end_time": "2026-10-23T08:00:00+00:00",
                "time_zone": "Asia/Shanghai",
            },
        },
        coverage={"checked_candidate_rows": count},
        **changes,
    )


@pytest.mark.parametrize("view", ["full", "brief"])
@pytest.mark.parametrize("plain_transport", [False, True])
def test_security_plain_61_samples_only_shows_counts_and_one_followup_hint(view, plain_transport):
    from xiaowei.feishu_render import business_text
    from xiaowei.models import DeliveryFactRef, DeliveryText

    source = many_security_samples(view=view)
    before, after = "🟡 需核对变更记录。", "本次代表样本中有同类覆盖写入，归纳为模型说明。"
    value = business(
        source,
        blocks=(
            DeliveryText(text=before),
            DeliveryFactRef(evidence_id=source.evidence_id),
            DeliveryText(text=after),
        ),
        inspection_notice="⚪ 本次另一个集群未完成",
    )
    result = business_text(value, plain_transport=plain_transport)
    assert result.startswith(value.inspection_notice)
    assert result.index(before) < result.index("数据范围") < result.index(after)
    assert "覆盖写入 61" in result and "代表样本 61 条未在消息中列出，可在会话中继续追问" in result
    assert "全窗聚合（读取时）29,232" in result
    assert "第 1 行" not in result and "etl-reader" not in result and "q-plain" not in result
    assert '"' not in result and '\\"' not in result
    assert len(value.facts[0].rows) == len(json.loads(value.facts[0].result_json)["rows"]) == 61


def test_security_plain_historical_brief_and_zero_samples():
    from xiaowei.feishu_render import business_text

    old = many_security_samples(view="brief", summary="本次返回行数：61。")
    assert old.historical_brief
    text = business_text(business(old), plain_transport=True)
    assert "代表样本 61 条未在消息中列出" in text
    assert "本次返回行数" not in text and "etl-reader" not in text
    zero = business_text(business(many_security_samples(count=0)), plain_transport=True)
    assert "代表样本" not in zero and "高风险操作（审计行）: 0" in zero


@pytest.mark.parametrize(
    "zone,start,captured",
    [
        ("Asia/Shanghai", "2026-10-23T15:00:00+08:00", "2026-10-23T16:00:00+08:00"),
        ("America/New_York", "2026-10-23T03:00:00-04:00", "2026-10-23T04:00:00-04:00"),
    ],
)
def test_security_display_uses_the_audit_zone_for_window_and_capture(zone, start, captured):
    from xiaowei.channel import RequestView
    from xiaowei.feishu_render import business_presentation, fact_scope
    from xiaowei.feishu_report import build_report
    from xiaowei.storage import Readiness
    from xiaowei.web import _view

    data = json.loads(many_security_samples().result_json)
    data["window"]["time_zone"] = zone
    source = many_security_samples().model_copy(update={"result_json": json.dumps(data)})
    shown = business_presentation(source)
    assert start in shown.coverage and zone in shown.coverage
    assert captured in fact_scope(source)
    value = business(source)
    api = json.loads(
        _view("request", RequestView(state="completed", delivery=value), Readiness()).body
    )
    assert api["delivery"]["facts"][0]["captured_at"] == captured
    for format_ in ("md", "html"):
        report = build_report(value.model_copy(update={"report_format": format_}))
        assert captured in report.content.decode() and "etl-reader" in report.content.decode()


@pytest.mark.parametrize(
    "unclassified,limited,expected",
    [
        (2, False, "未发现（另有 2 条无法判定）"),
        (0, True, "已读取候选中未发现"),
        (None, False, "未提供"),
    ],
)
def test_security_zero_lower_bound_is_readable_and_does_not_invent_unknown_rows(
    unclassified,
    limited,
    expected,
):
    from xiaowei.feishu_render import business_presentation

    shown = business_presentation(
        security_fact(
            coverage={
                "match_counts_complete": False,
                "unclassified_candidate_rows": unclassified,
                "scan_limited": limited,
            }
        )
    )
    assert expected in shown.checks[0]["审计行数"]
    assert "至少 0" not in str(shown.checks) and "另有 0" not in str(shown.checks)
    assert shown.checks[-1]["审计行数"] == "6"


def test_security_historical_missing_zone_keeps_original_time_and_api_iso():
    from datetime import datetime

    from xiaowei.channel import RequestView
    from xiaowei.feishu_render import business_presentation
    from xiaowei.storage import Readiness
    from xiaowei.web import _view

    source = many_security_samples(view="brief")
    data = json.loads(source.result_json)
    del data["window"]["time_zone"]
    source = source.model_copy(update={"result_json": json.dumps(data)})
    shown = business_presentation(source)
    assert "2026-10-23T07:00:00+00:00" in shown.coverage
    assert "审计时区不可换算" in shown.coverage
    assert "2026-10-23T15:00:00+08:00" not in shown.coverage
    api = json.loads(
        _view("old", RequestView(state="completed", delivery=business(source)), Readiness()).body
    )
    captured = api["delivery"]["facts"][0]["captured_at"]
    assert captured == source.captured_at.isoformat()
    assert datetime.fromisoformat(captured) == source.captured_at


def test_security_plain_sample_hint_counts_authorized_rows_not_total_matches():
    from xiaowei.feishu_render import business_text

    source = many_security_samples()
    data = json.loads(source.result_json)
    data["matched_rows"] = data["signal_counts"]["high_risk_operation"] = 999
    data["operation_counts"]["insert_overwrite"] = 999
    data["coverage"]["omitted_samples"] = 938
    source = source.model_copy(update={"result_json": json.dumps(data), "truncated": True})
    text = business_text(business(source), plain_transport=True)
    assert "覆盖写入 999" in text and "代表样本 61 条未在消息中列出" in text
    assert "代表样本 999" not in text and "分类计数完整" in text
