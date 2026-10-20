"""M1b：默认正文与可达明细分别验收；不证明真实飞书客户端可用。"""

import json

import pytest
from tests.sdk_core.test_feishu_render import delivery, fact, size, tables, text_parts

from xiaowei.feishu_delivery import _data_parts, _preview
from xiaowei.feishu_render import build_feishu_message
from xiaowei.models import Delivery, DeliveryFact, DeliveryText


def answer_with(source: DeliveryFact) -> Delivery:
    value = delivery(source, analysis="先看结论，明细用于核对。")
    if source.section in {"remaining", "action"}:
        value = value.model_copy(update={"blocks": (DeliveryText(text=value.texts[0]),)})
    return value


def query_rows(count: int, *, width: int = 30, **changes: object) -> DeliveryFact:
    rows = tuple({"name": f"row-{i:03d}-" + "x" * width} for i in range(count))
    metadata = {"sql": "SELECT name FROM biz.orders LIMIT 100", "row_count": count}
    return fact(
        rows=rows,
        metadata=metadata,
        result_json=json.dumps({"columns": ["name"], "rows": rows, **metadata}),
        **changes,
    )


@pytest.mark.parametrize("section", ["body", "remaining"])
def test_folded_facts_keep_limits_visible_and_every_value_reachable(section: str) -> None:
    source = query_rows(12, section=section, truncated=True)
    message = build_feishu_message(answer_with(source), 3500, "oc_test", platform_limits=True)
    visible, complete = ("\n".join(text_parts(message, folded=f)) for f in (False, True))
    assert "工具结果已截断，不能据此声称完整。" in visible
    assert "查看依据" in visible and "系统事实" not in visible
    assert "row-000" not in visible and "row-011" not in visible
    assert tables(message) == []
    for row in source.rows:
        assert complete.count(row["name"]) == 1
    assert metadata_sql(source) in complete
    assert "原文 JSON:" not in complete
    assert "逐项一致" in complete


def metadata_sql(source: DeliveryFact) -> str:
    return str(source.metadata["sql"])


def test_three_row_statistics_stay_open_but_remaining_defaults_to_details() -> None:
    source = query_rows(3)
    for section, open_rows in (("body", True), ("remaining", False)):
        message = build_feishu_message(
            answer_with(source.model_copy(update={"section": section})),
            3500,
            "oc_test",
            platform_limits=True,
        )
        visible = "\n".join(text_parts(message, folded=False))
        assert ("row-000" in visible) is open_rows
        assert "row-002" in "\n".join(text_parts(message))


@pytest.mark.parametrize("section", ["body", "remaining"])
def test_long_monitoring_scalar_can_fold_without_table_or_brief(section: str) -> None:
    value = "up{instance=host1} 0\n" * 140
    source = fact(
        tool_id="prometheus/query", columns=(), rows=(), metadata={"result": value}, section=section
    )
    message = build_feishu_message(answer_with(source), 3500, "oc_test", platform_limits=True)
    assert "up{instance" not in "\n".join(text_parts(message, folded=False))
    assert value in "\n".join(text_parts(message))
    assert source.view == "full"


def test_t2_single_long_diagnostic_sql_is_traceable_in_place() -> None:
    sql = "SELECT status FROM biz.orders WHERE status IN (" + "'paid'," * 499 + "'paid')"
    source = fact(
        tool_id="local/explain_query",
        columns=("plan",),
        rows=({"plan": "SCAN biz.orders"},),
        metadata={"sql": sql, "scope": "fat，最近一小时候选中的 SQL"},
        truncated=True,
    )
    message = build_feishu_message(answer_with(source), 3500, "oc_test", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    assert "查看依据" in visible and "工具结果已截断" in visible
    assert "fat，最近一小时" in "\n".join(text_parts(message))
    assert "SCAN biz.orders" not in visible and "SELECT status" not in visible
    assert sql in "\n".join(text_parts(message))


def test_action_status_without_complete_flag_never_folds() -> None:
    source = query_rows(12, section="action", requires_complete=False)
    message = build_feishu_message(answer_with(source), 3500, "oc_test", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    assert "row-000" in visible and "row-011" in visible
    assert len(tables(message)) == 1


def test_folded_raw_dedup_retains_single_message_at_capacity_boundary() -> None:
    source = query_rows(30, width=180)
    message = build_feishu_message(
        answer_with(source),
        3500,
        "oc_test",
        max_bytes=10_000,
        allow_preview=False,
        platform_limits=True,
    )
    assert size(message) <= 10_000
    complete = "\n".join(text_parts(message))
    assert complete.count(source.rows[0]["name"]) == 1
    assert "row-000" not in "\n".join(text_parts(message, folded=False))
    assert "原文 JSON:" not in complete
    assert size(message) + len(source.result_json.encode()) > 10_000


@pytest.mark.parametrize("section", ["body", "remaining"])
def test_attachment_preview_has_no_duplicate_large_detail_and_preserves_files(section: str) -> None:
    source = query_rows(100, width=450, section=section, truncated=True)
    parts = _data_parts(answer_with(source), 0, 3500, "oc_test", None)
    assert [p.kind for p in parts] == ["md", "md", "body"]
    report = "\n".join(p.body.decode() for p in parts if p.kind == "md")
    for row in source.rows:
        assert row["name"] in report
    assert metadata_sql(source) in report
    message = json.loads(parts[-1].body)
    complete = "\n".join(text_parts(message))
    visible = "\n".join(text_parts(message, folded=False))
    assert "本次完整结果见附件" in visible and "工具结果已截断" in visible
    assert "row-000" not in complete and "row-099" not in complete
    assert "展开查看" not in complete and "SQL 未展示" not in visible
    assert size(message) <= 30_000


def test_actual_folded_text_size_selects_file_instead_of_table_estimate() -> None:
    names = tuple(f"column-{i}-" + "long-name" * 22 for i in range(4))
    rows = tuple(dict.fromkeys(names, n) for n in range(40))
    source = fact(columns=names, rows=rows, metadata={})
    parts = _data_parts(answer_with(source), 0, 3500, "oc_test", None)
    assert [p.kind for p in parts] == ["md", "md", "body"]
    assert all(name in parts[1].body.decode() for name in names)


def test_explicit_attachment_preview_does_not_offer_collapsed_full_details() -> None:
    source = query_rows(12)
    message = _preview(
        answer_with(source), 3500, "oc_test", None, "本次完整结果见附件。", attachment=True
    )
    assert "row-000" not in "\n".join(text_parts(message))
    assert "见附件" in "\n".join(text_parts(message, folded=False))


def test_diagnostic_actual_sql_keeps_quotes_backslashes_and_newlines() -> None:
    sql = "SELECT \"name\", '\\_%'\nFROM biz.orders"
    source = query_rows(12).model_copy(
        update={"tool_id": "local/explain_query", "metadata": {"sql": sql}, "result_json": None}
    )
    message = build_feishu_message(answer_with(source), 3500, "oc_test", platform_limits=True)
    assert "实际 SQL:\n" + sql in text_parts(message)


def test_fold_dedup_does_not_remove_raw_fields_missing_from_visible_values() -> None:
    source = query_rows(12)
    original = json.loads(source.result_json)
    original["not_represented"] = {"integer": 9223372036854775807, "boolean": True}
    source = source.model_copy(update={"result_json": json.dumps(original)})
    message = build_feishu_message(answer_with(source), 3500, "oc_test", platform_limits=True)
    shown = "\n".join(text_parts(message))
    assert '"not_represented"' in shown and "9223372036854775807" in shown
    assert "原文 JSON:" in shown and "逐项一致" not in shown


@pytest.mark.parametrize("section", ["body", "remaining"])
@pytest.mark.parametrize("attachment", [False, True])
def test_folded_monitoring_keeps_warning_visible_and_actual_parameters_in_details(
    section: str,
    attachment: bool,
) -> None:
    source = fact(
        tool_id="prometheus/range_query",
        columns=(),
        rows=(),
        section=section,
        metadata={
            "result": "up{instance=host1} 0\n" * 140,
            "start_time": "1792440000",
            "end_time": "1792443600",
            "step": "60",
            "warnings": '["partial series returned"]',
        },
    )
    message = build_feishu_message(
        answer_with(source), 3500, "oc_test", platform_limits=True, attachment_preview=attachment
    )
    visible = "\n".join(text_parts(message, folded=False))
    assert "1792440000" not in visible and "step:" not in visible
    assert "数据源返回警告" in visible
    assert "up{instance=host1}" not in visible
    complete = "\n".join(text_parts(message))
    assert ("up{instance=host1}" in complete) is not attachment
    for original in ("1792440000", "1792443600", "partial series returned"):
        assert (original in complete) is not attachment


def test_failure_hint_for_single_card_does_not_claim_an_attachment() -> None:
    source = query_rows(12)
    message = _preview(
        answer_with(source), 3500, "oc_test", None, "完整结果尚未交付。请联系操作员。"
    )
    visible = "\n".join(text_parts(message, folded=False))
    assert "见附件" not in visible
    assert "row-011" in "\n".join(text_parts(message))


def test_non_table_failure_hint_states_missing_values_without_field_log() -> None:
    source = fact(
        tool_id="prometheus/range_query",
        columns=(),
        rows=(),
        metadata={
            "result": "host1 0\n" * 8000,
            "start_time": "1792440000",
            "end_time": "1792443600",
            "step": "60",
            "warnings": "partial series returned",
        },
    )
    message = _preview(answer_with(source), 3500, "oc_test", None, "完整结果尚未交付。")
    visible = "\n".join(text_parts(message, folded=False))
    assert "数据源返回警告" in visible and "完整结果尚未交付" in visible
    assert "部分明细未展示" in visible
    assert "个字段" not in visible and "1792440000" not in visible
    assert "（无数据行）" not in "\n".join(text_parts(message))


@pytest.mark.parametrize("view", ["full", "brief"])
def test_short_monitoring_metadata_is_details_only_even_below_old_density_limit(view):
    value = "up{instance=host1} 1\nup{instance=host2} 1"
    source = fact(
        tool_id="prometheus/range_query",
        columns=(),
        rows=(),
        view=view,
        metadata={"result": value, "start_time": "1792440000", "warnings": None},
    )
    message = build_feishu_message(answer_with(source), 3500, "oc_test", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    for unwanted in (
        "up{instance",
        "start_time",
        "warnings",
        "共 3 个字段",
        "系统事实",
        "其余结果",
    ):
        assert unwanted not in visible
    assert value in "\n".join(text_parts(message))


def test_current_brief_small_table_folds_but_remains_complete():
    source = query_rows(3).model_copy(update={"view": "brief", "summary": None})
    message = build_feishu_message(
        answer_with(source), 3500, "oc_test", platform_limits=True, allow_preview=False
    )
    visible = "\n".join(text_parts(message, folded=False))
    assert "row-000" not in visible and "摘要不可用" not in visible
    for row in source.rows:
        assert row["name"] in "\n".join(text_parts(message))


def test_current_brief_capacity_cannot_take_the_historical_summary_shortcut():
    source = query_rows(100, width=450).model_copy(update={"view": "brief", "summary": None})
    parts = _data_parts(answer_with(source), 0, 3500, "oc_test", None)
    files = [part.body.decode() for part in parts if part.kind == "md"]
    assert files and all(row["name"] in "\n".join(files) for row in source.rows)
    assert "历史摘要缺失" not in "\n".join(files)


@pytest.mark.parametrize("complete", [False, True])
def test_actions_ignore_brief_including_unprotected_status(complete):
    source = query_rows(3, section="action", requires_complete=complete).model_copy(
        update={"view": "brief", "summary": None}
    )
    message = build_feishu_message(answer_with(source), 3500, "oc_test", platform_limits=True)
    visible = "\n".join(text_parts(message, folded=False))
    if not complete:
        assert "row-000" in visible and "row-002" in visible
    else:
        assert "实际 SQL" in visible or "sql" in visible
