"""Review regressions: concise visible caveats and complete action report sections."""

import json

import pytest
from tests.sdk_core.test_feishu_render import text_parts
from tests.sdk_core.test_feishu_reports import _delivery
from tests.sdk_core.test_grafana_g3 import g3_config
from tests.sdk_core.test_monitoring_r1a import monitoring_config
from tests.sdk_core.test_runtime import serve_config

from xiaowei.feishu_delivery import _data_parts
from xiaowei.feishu_render import MessageCapacityError, build_feishu_message, fact_limit_notices
from xiaowei.feishu_report import build_reports
from xiaowei.models import DeliveryFactRef, DeliveryText
from xiaowei.runtime import ServeConfig, _monitoring_catalog


def reviewed_delivery(format_="md", *, action=False, complete=False):
    delivery = _delivery(format_)
    metadata = (
        {"state": "executed", "action_id": "ac_review", "value": "ACTION_CANARY"}
        if action
        else {"omitted_sqlguard": 17, "unchecked_candidates": 0, "more_candidates": False}
    )
    source = delivery.facts[0].model_copy(
        update={
            "tool_id": "local/list_slow_queries" if not action else "monitoring/action_status",
            "columns": () if action else ("value",),
            "rows": () if action else ({"value": "a"}, {"value": "b"}, {"value": "c"}),
            "metadata": metadata,
            "result_json": json.dumps(metadata)
            if action
            else json.dumps(
                {"columns": ["value"], "rows": [{"value": c} for c in "abc"], **metadata}
            ),
            "section": "action" if action else "body",
            "requires_complete": complete,
        }
    )
    return delivery.model_copy(
        update={
            "facts": (source,),
            "blocks": tuple(b for b in delivery.blocks if not action or b.kind == "text"),
        }
    )


@pytest.mark.parametrize("channel", ["card", "md"])
def test_filtered_candidates_are_visible_without_truncated_flag(channel):
    delivery = reviewed_delivery()
    message = build_feishu_message(delivery, 3500, "oc_review", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    if channel == "card":
        assert "规则或权限过滤 17 条" in visible
    assert "omitted_sqlguard" not in visible and "SELECT" not in visible
    reports = build_reports(delivery)
    if channel == "md":
        assert "规则或权限过滤 17 条" in reports[0].content.decode()
    assert "omitted_sqlguard" not in reports[0].content.decode()
    assert "omitted_sqlguard" in reports[-1].content.decode()


def action_with_large_result(format_):
    value = reviewed_delivery(format_, action=True, complete=False)
    data = _delivery(format_).facts[0]
    rows = tuple({"value": f"row-{i:03d}-" + "x" * 500} for i in range(100))
    data = data.model_copy(
        update={
            "evidence_id": "ev_large_result",
            "rows": rows,
            "result_json": json.dumps({"columns": ["value"], "rows": rows}),
        }
    )
    return value.model_copy(
        update={
            "facts": (*value.facts, data),
            "evidence_ids": (*value.evidence_ids, data.evidence_id),
            "blocks": (
                DeliveryText(text="需要复核"),
                DeliveryFactRef(evidence_id=data.evidence_id),
            ),
            "report_native": True,
        }
    )


@pytest.mark.parametrize("format_", ["md", "html"])
def test_action_status_stays_once_in_message_and_out_of_every_data_file(format_):
    value = action_with_large_result(format_)
    assert all(
        b.kind != "fact" or b.evidence_id != value.facts[0].evidence_id for b in value.blocks
    )
    with pytest.raises(MessageCapacityError):
        build_feishu_message(value, 3500, "oc_review", allow_preview=False, platform_limits=True)
    parts = _data_parts(value, 0, 3500, "oc_review", None)
    files = [p.body.decode() for p in parts if p.kind in {"md", "html"}]
    bodies = [json.loads(p.body) for p in parts if p.kind == "body"]
    visible = "\n".join(part for body in bodies for part in text_parts(body, folded=False))
    assert files and bodies
    for field in ("ACTION_CANARY", "ac_review", "executed"):
        assert visible.count(field) == 1
        assert all(field not in content for content in files)
    assert all(f"row-{i:03d}" in "\n".join(files) for i in range(100))


def test_note_summary_is_visible_in_md_body_but_full_note_is_details_only():
    delivery = reviewed_delivery()
    fact = delivery.facts[0].model_copy(
        update={"note_summary": "执行计划为估算，未实际运行。", "note": "LONG_NOTE_CANARY"}
    )
    body, detail = build_reports(delivery.model_copy(update={"facts": (fact,)}))
    assert "执行计划为估算，未实际运行" in body.content.decode()
    assert "LONG_NOTE_CANARY" not in body.content.decode()
    assert "LONG_NOTE_CANARY" in detail.content.decode()


@pytest.mark.parametrize(
    "summary",
    [
        "仅列获准候选，为空不代表没有慢查询。",
        "执行计划为估算，未实际运行。",
        "仪表盘配置快照，不是实时指标。",
        "采集时结果，不代表当前状态。",
    ],
)
def test_fixed_caveats_are_visible_without_full_note(summary):
    delivery = reviewed_delivery()
    fact = delivery.facts[0].model_copy(update={"note_summary": summary, "note": None})
    delivery = delivery.model_copy(update={"facts": (fact,)})
    card = build_feishu_message(delivery, 3500, "oc_review", platform_limits=True)
    assert summary in "\n".join(text_parts(card, folded=False))
    assert summary in build_reports(delivery)[0].content.decode()


def test_complete_approval_delivery_is_still_rejected_as_report():
    with pytest.raises(ValueError, match="不能作为只读事实报告"):
        build_reports(
            reviewed_delivery(action=True, complete=True).model_copy(
                update={"requires_complete": True}
            )
        )


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"omitted_self": 2, "omitted_revoked": 1}, "规则或权限过滤 3 条"),
        ({"omitted_value_too_large": 2}, "容量省略 2 条"),
        ({"unchecked_candidates": 4, "more_candidates": True}, "未检查 4 条"),
        ({"more_candidates": True}, "还有未读取候选，数量未知"),
    ],
)
def test_candidate_incompleteness_is_not_only_the_truncated_flag(metadata, expected):
    delivery = reviewed_delivery()
    fact = delivery.facts[0].model_copy(update={"metadata": metadata})
    card = build_feishu_message(
        delivery.model_copy(update={"facts": (fact,)}), 3500, "oc_review", platform_limits=True
    )
    assert expected in "\n".join(text_parts(card, folded=False))


def test_complete_candidate_and_missing_window_do_not_invent_a_warning_or_interval():
    delivery = reviewed_delivery()
    fact = delivery.facts[0].model_copy(
        update={"metadata": {"omitted_sqlguard": 0, "start_time": "0"}}
    )
    card = build_feishu_message(
        delivery.model_copy(update={"facts": (fact,)}), 3500, "oc_review", platform_limits=True
    )
    visible = "\n".join(text_parts(card, folded=False))
    assert "候选未完全列出" not in visible and "查询窗口" not in visible


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"from": 0, "to": 60_000}, "1970-01-01 08:01:00+08:00"),
        ({"has_more": True}, "还有后续页"),
        ({"hasMore": True}, "还有后续页"),
    ],
)
def test_other_monitoring_windows_and_pagination_keep_their_scope_visible(metadata, expected):
    value = reviewed_delivery()
    fact = value.facts[0].model_copy(update={"metadata": metadata})
    card = build_feishu_message(
        value.model_copy(update={"facts": (fact,)}), 3500, "oc_review", platform_limits=True
    )
    assert expected in "\n".join(text_parts(card, folded=False))


@pytest.mark.parametrize("channel", ["card", "md"])
@pytest.mark.parametrize(
    "name,policy_id,expected",
    [
        ("range_query", "prometheus.range_query", "不代表未查范围"),
        ("list_datasources", "grafana.list_datasources", "不证明连接可用或对应外部 Prometheus 源"),
    ],
)
def test_actual_monitoring_catalog_caveats_reach_main_view(channel, name, policy_id, expected):
    configure = monitoring_config if policy_id.startswith("prometheus.") else g3_config
    values = configure("http://127.0.0.1:9999/mcp")
    values["mcp_servers"][0]["allowed_tools"][name] = policy_id
    contracts, policies = _monitoring_catalog(
        ServeConfig.model_validate(serve_config(8501, **values))
    )
    policy = next(p for p in policies if p.policy_id == policy_id)
    delivery = reviewed_delivery()
    fact = delivery.facts[0].model_copy(
        update={
            "tool_id": next(c.tool_id for c in contracts if c.policy_id == policy_id),
            "metadata": {},
            "note": policy.fact_note,
            "note_summary": policy.fact_note_summary,
        }
    )
    delivery = delivery.model_copy(update={"facts": (fact,)})
    if channel == "card":
        message = build_feishu_message(delivery, 3500, "oc_review", platform_limits=True)
        visible = "\n".join(text_parts(message, folded=False))
    else:
        visible = build_reports(delivery)[0].content.decode()
    assert expected in visible


@pytest.mark.parametrize("truncated", [False, True])
def test_limit_notice_only_claims_restriction_when_the_result_is_truncated(truncated):
    value = reviewed_delivery()
    fact = value.facts[0].model_copy(
        update={"truncated": truncated, "metadata": {"requested_limit": None, "applied_limit": 100}}
    )
    assert any("实际 LIMIT 100" in note for note in fact_limit_notices(fact)) is truncated


@pytest.mark.parametrize("key", ["hasMore", "has_more"])
def test_pagination_and_truncation_share_one_incompleteness_reminder(key):
    value = reviewed_delivery()
    fact = value.facts[0].model_copy(update={"truncated": True, "metadata": {key: True}})
    notes = fact_limit_notices(fact)
    assert len(notes) == 1 and "还有后续页" in notes[0]
