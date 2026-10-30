"""登记工具的业务展示契约；使用渠道投影，不调用模型或业务服务。"""

import json
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from tests.sdk_core.test_feishu_render import delivery, text_parts
from tests.sdk_core.test_presentation_v4 import v2_security_fact

from xiaowei.channel import RequestView
from xiaowei.dolphinscheduler import ARGS
from xiaowei.evidence import _fact, _shown, _table
from xiaowei.feishu_render import build_feishu_message, business_text
from xiaowei.models import AUDIENCES, EvidenceRecord, Identity
from xiaowei.storage import Readiness
from xiaowei.web import _view

CAPTURED_AT = datetime(2026, 10, 30, 4, 0, tzinfo=UTC)


def ds_payload(name, records, **extra):
    return {
        "environment": "DS 测试环境", "cluster": "ds-sr-fat", "kind": name,
        "records": records,
        "coverage": {
            "page_no": 1, "page_size": 20, "page_start_offset": 0,
            "upstream_total": len(records), "has_more": False,
            "boundary_excluded": 0, "projection_omitted": 0, "scope": "指定对象／页",
        },
        **extra,
    }


PROJECTS = ds_payload("ds_list_projects", [{"code": 101, "name": "数仓调度"}])
WORKFLOWS = ds_payload("ds_list_workflow_instances", [
    {"id": 201, "name": "日汇总", "state": "FAILURE", "projectCode": 101},
])
TASKS = ds_payload("ds_list_task_instances", [
    {"id": 301, "name": "汇总写入", "state": "FAILURE", "taskType": "SQL"},
])
DETAIL = ds_payload(
    "ds_get_workflow_instance", WORKFLOWS["records"],
    coverage={
        "projection_omitted": 0, "gaps": [], "tasks_scope": "指定有效任务；非完整历史尝试",
        "dependency_scope": "运行依赖状态不含等待起止；等待时长需带时间日志，静态 DAG 不证明耗时",
    },
    tasks=TASKS["records"], nodes=[{"code": 401, "name": "汇总写入", "taskType": "SQL"}],
    relations=[{"preTaskCode": 0, "postTaskCode": 401}], related=[],
)
LOG = ds_payload(
    "ds_read_task_log",
    [{"line": 1, "byte_offset": 0, "byte_start": 0, "byte_end": 27,
      "text": "2026-10-30 ERROR task failed\n", "utf8_replacements": False}],
    coverage={"read_byte_range": [0, 27], "received_bytes": 27, "redacted_bytes": 0,
              "reached_end": True, "truncation_reason": None, "projection_omitted": 0,
              "matches_in_read_range": None},
    task={"project_code": 101, "workflow_instance_id": 201, "id": 301, "state": "FAILURE"},
)
SEARCH_LOG = deepcopy(LOG)
SEARCH_LOG["kind"] = "ds_search_task_log"
SEARCH_LOG["coverage"]["matches_in_read_range"] = {"ERROR": 1}
HISTORY = {
    "records": [{
        "record_ref": "request:old-turn", "time": "2026-10-29T12:00:00+00:00",
        "state": "completed", "facts": [{
            "tool": "local/run_readonly_query", "target": "pre",
            "captured_at": "2026-10-29T12:00:00+00:00",
            "data": {"columns": ["total"], "rows": [{"total": 7}], "row_count": 1},
            "truncated": False,
        }], "gaps": {},
    }],
    "sources": [{"record_ref": "request:old-turn", "evidence_ids": ["ev_old"],
                 "analysis": False}],
    "gaps": {}, "earliest_at": "2026-10-29T12:00:00+00:00", "has_earlier": False,
    "notice": "历史状态不证明当前状态。",
}


def saved_record(tool, payload, *, channel="feishu", truncated=False):
    """按旧版保存的 envelope 读回；渲染不能改写任何用途的投影。"""
    envelope = json.dumps({"data": payload, "empty": False, "truncated": truncated})
    record = EvidenceRecord(
        evidence_id="ev_sample", identity=Identity(subject_id="tester", session_id="sample",
                                                  turn_id="old-turn", channel=channel),
        target_id=payload.get(
            "cluster", "pre" if tool.startswith("local/") else tool.split("/")[0],
        ),
        tool_id=tool, call_id="sample-call",
        tool_name=tool.rsplit("/", 1)[-1], arguments_digest="sample-digest",
        policy_id="sample-policy", policy_fingerprint="sample-fingerprint",
        captured_at=CAPTURED_AT, expires_at=CAPTURED_AT + timedelta(days=1), truncated=truncated,
        projections=dict.fromkeys(AUDIENCES, envelope),
    )
    return EvidenceRecord.model_validate_json(record.model_dump_json())


def rendered(tool, payload, *, channel="feishu", **changes):
    record = saved_record(tool, payload, channel=channel)
    original = record.model_dump_json()
    source = _fact(_shown(record, channel, None), channel).model_copy(update=changes)
    value = delivery(source).model_copy(update={"channel": channel, "answer_version": 4})
    assert record.model_dump_json() == original
    return source, value


def visible_text(value, channel):
    if channel == "feishu":
        return "\n".join(text_parts(build_feishu_message(value, 3500, "oc_test"), folded=False))
    response = json.loads(_view("sample", RequestView("completed", value), Readiness()).body)
    fact = response["delivery"]["facts"][0]
    shown = fact["presentation"]
    if fact["view"] == "brief" and fact["section"] != "action":
        return "\n".join([fact["summary"] or "", *shown["notices"]])
    if fact["section"] == "action":
        return "\n".join(str(cell) for cell in fact["metadata"].values())
    # 读取 Web 实际消费的 presentation 文本，不将 HTTP JSON 容器当作业务正文。
    return "\n".join([
        *(str(cell) for cell in shown["fields"].values()), *shown["columns"],
        *(str(cell) for row in shown["rows"] for cell in row.values()),
        *shown["notices"],
    ])


@pytest.mark.parametrize("channel", ["feishu", "web"])
@pytest.mark.parametrize("payload", [PROJECTS, WORKFLOWS, TASKS])
def test_ds_lists_render_saved_records_as_table_with_chinese_coverage(channel, payload):
    source, value = rendered("local/" + payload["kind"], payload, channel=channel)
    shown = visible_text(value, channel)
    assert source.rows and source.columns == tuple(payload["records"][0])
    assert "上游总数 1 条；已全部列出（1 条）" in shown
    for key in ("records", "coverage", "environment", "cluster", "kind", "upstream_total"):
        assert key not in shown
    assert '"name":' not in shown
    assert json.loads(source.result_json) == payload
    if channel == "feishu":
        all_text = "\n".join(text_parts(build_feishu_message(value, 3500, "oc_test")))
        assert source.result_json in all_text


@pytest.mark.parametrize("channel", ["feishu", "web"])
@pytest.mark.parametrize("rows", [[{"selected": "rows wins"}], []])
def test_rows_take_precedence_over_records_without_duplicate_body(channel, rows):
    payload = {"columns": ["selected"], "rows": rows, "records": [{"other": "not selected"}]}
    source, value = rendered("local/example", payload, channel=channel)
    assert source.rows == tuple(rows)
    assert "not selected" not in visible_text(value, channel)
    assert json.loads(source.result_json) == payload


@pytest.mark.parametrize("coverage,expected", [
    ({"upstream_total": 42, "has_more": True},
     "上游总数 42 条；本页范围 1–1（第 1 页），本次列出 1 条；还有后续页"),
    ({"upstream_total": 21, "page_no": 2, "page_start_offset": 20},
     "上游总数 21 条；本页范围 21–21（第 2 页），本次列出 1 条"),
    ({"upstream_total": 4, "boundary_excluded": 2, "projection_omitted": 1},
     "上游总数 4 条；本页范围 1–4（第 1 页），本次列出 1 条；时间边界排除 2 条；投影省略 1 条"),
    ({"upstream_total": 0}, "上游总数 0 条；已全部列出（0 条）"),
    ({"upstream_total": 2},
     "上游总数 2 条；第 1 页未返回记录（起始偏移 0），本次列出 0 条"),
    ({"upstream_total": 2, "page_no": 2, "page_start_offset": 20},
     "上游总数 2 条；第 2 页超出结果范围，本次列出 0 条"),
])
def test_ds_coverage_does_not_claim_last_or_filtered_page_is_complete(coverage, expected):
    payload = deepcopy(PROJECTS)
    payload["coverage"].update(coverage)
    if coverage.get("upstream_total") in (0, 2):
        payload["records"] = []
    _, value = rendered("local/ds_list_projects", payload)
    assert expected in business_text(value)
    assert "coverage:" not in business_text(value)


def test_truncated_ds_projection_cannot_claim_all_rows_listed():
    _, value = rendered("local/ds_list_projects", PROJECTS, truncated=True)
    text = business_text(value)
    assert "已全部列出" not in text
    assert "工具结果已截断" in text


@pytest.mark.parametrize("rows", [None, {"wrong": "shape"}, ["not a row"]])
def test_invalid_rows_do_not_fall_back_to_records(rows):
    assert _table({"rows": rows, "records": [{"valid": True}]}) is None


@pytest.mark.parametrize("channel", ["feishu", "web"])
def test_history_table_keeps_nested_values_typed_without_changing_saved_projection(channel):
    source, value = rendered("history/search_operations", HISTORY, channel=channel)
    assert source.columns == ("time", "state", "facts", "gaps")
    assert len(source.rows) == 1
    text = visible_text(value, channel)
    assert '"total": 7' in text and "completed" in text
    assert '\\"total\\"' not in text
    data = json.loads(source.result_json)
    assert "sources" not in data and "record_ref" not in data["records"][0]
    assert data["records"][0]["facts"] == HISTORY["records"][0]["facts"]


def json_containers(text):
    """找业务文本中的整段 JSON，不把传输层 JSON 或引号/括号本身当问题。"""
    decoder, found, start = json.JSONDecoder(), [], 0
    while start < len(text):
        if text[start] not in "[{":
            start += 1
            continue
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            start += 1
        else:
            found.append(value)
            start = end
    return found


# 外层列表不得再出现 JSON 兜底；例外只覆盖已有契约或本次明确不改的具体嵌套字段。
CASES = [
    ("sql", "local/run_readonly_query",
     {"columns": ["total"], "rows": [{"total": 7}], "row_count": 1, "sql": "SELECT 7"},
     (), {}),
    *((name, "local/" + payload["kind"], payload, (), {})
      for name, payload in (("ds_projects", PROJECTS), ("ds_workflows", WORKFLOWS),
                            ("ds_tasks", TASKS))),
    ("ds_detail", "local/ds_get_workflow_instance", DETAIL,
     tuple(DETAIL[key] for key in ("coverage", "tasks", "nodes", "relations", "related")), {}),
    ("ds_log", "local/ds_read_task_log", LOG, (LOG["coverage"], LOG["task"]), {}),
    ("ds_search_log", "local/ds_search_task_log", SEARCH_LOG,
     (SEARCH_LOG["coverage"], SEARCH_LOG["task"]), {}),
    # 用户确认仅修外层成表；facts/gaps 的专门展示留待独立任务。
    ("history", "history/search_operations", HISTORY,
     (HISTORY["records"][0]["facts"], HISTORY["records"][0]["gaps"], HISTORY["gaps"]), {}),
    ("alertmanager", "alertmanager/get_alerts",
     {"data": [{"labels": {"alertname": "HostDown"}, "status": {"state": "active"}}],
      "total": 1, "offset": 0, "limit": 20, "has_more": False},
     ([{"labels": {"alertname": "HostDown"}, "status": {"state": "active"}}],), {}),
    ("grafana", "grafana/get_annotations",
     {"annotations": [{"id": 1, "text": "deploy", "tags": ["ops"]}]},
     ([{"id": 1, "text": "deploy", "tags": ["ops"]}],), {}),
    ("prometheus", "prometheus/query", {"result": "up{instance=host} => 1 @[1792673695]"},
     (), {"view": "brief", "summary": "补充原值见技术详情。"}),
    ("security", v2_security_fact().tool_id, json.loads(v2_security_fact().result_json), (), {}),
    ("action", "alertmanager/get_action_status",
     {"action_id": "action-test", "state": "unknown", "reason": "执行结果未确认"},
     (), {"section": "action"}),
    ("task", "scheduled/list_tasks",
     {"tasks": [{"job_id": "task-test", "text": "定时巡检", "state": "active",
                 "next_run": "2026-10-30T14:00:00+08:00"}]}, (), {}),
    ("notification", "notifications/send_webhook_message",
     {"bot_alias": "测试群", "text": "测试完成", "status": "accepted"}, (), {}),
]


@pytest.mark.parametrize("channel", ["feishu", "web"])
@pytest.mark.parametrize("name,tool,payload,allowed,changes", CASES,
                         ids=[case[0] for case in CASES])
def test_registered_business_views_do_not_dump_unlisted_json(
    channel, name, tool, payload, allowed, changes,
):
    _, value = rendered(tool, payload, channel=channel, **changes)
    visible = visible_text(value, channel)
    expected = {
        "sql": "total", "ds_projects": "数仓调度", "ds_workflows": "日汇总",
        "ds_tasks": "汇总写入", "ds_detail": "日汇总", "ds_log": "ERROR task failed",
        "ds_search_log": "ERROR task failed", "history": "completed",
        "alertmanager": "HostDown", "grafana": "deploy", "prometheus": "补充原值",
        "security": "多语句（辅助信息）", "action": "执行结果", "task": "定时巡检",
        "notification": "测试完成",
    }
    assert expected[name] in visible
    containers = json_containers(visible)
    # 逐值、逐次数限定例外，不能靠隐藏值或增加同类 JSON 字段让检查变绿。
    assert Counter(json.dumps(item, sort_keys=True) for item in containers) == Counter(
        json.dumps(item, sort_keys=True) for item in allowed
    ), (name, containers)


def test_business_view_samples_cover_all_registered_ds_read_tools():
    assert {tool.removeprefix("local/") for _, tool, *_ in CASES
            if tool.startswith("local/ds_")} == set(ARGS)


def test_json_detector_recognizes_multiline_containers_but_not_plain_braces():
    assert json_containers('说明：{不是 JSON}\n字段\n[\n {"code": 1}\n]\ngaps: {}') == [
        [{"code": 1}], {},
    ]
