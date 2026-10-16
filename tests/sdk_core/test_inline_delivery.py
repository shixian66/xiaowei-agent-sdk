"""新语义飞书交付：平台限制门槛、回答优先预览与如实的预览范围。"""

import json
from datetime import UTC, datetime

import pytest

from xiaowei.channel_store import DeliveryPart
from xiaowei.feishu_delivery import _check_capacity, _data_parts, _preview
from xiaowei.feishu_render import (
    RAW_OMITTED,
    MessageCapacityError,
    _card,
    _elements,
    build_feishu_message,
    request_bytes,
)
from xiaowei.models import AnswerInference, Delivery, DeliveryFact

CHAT = "oc_inline"
AUDIT = ("queryId", "user", "duration_ms", "sql_text")


def fact(columns, rows, *, metadata=None, raw=None, tool_id="local/run_readonly_query"):
    metadata = {"sql": "SELECT 1"} if metadata is None else metadata
    if raw is None:
        raw = json.dumps(
            {**metadata, "columns": list(columns), "rows": list(rows)}, ensure_ascii=False
        )
    return DeliveryFact(
        evidence_id="ev_inline",
        tool_id=tool_id,
        target_id="fat",
        captured_at=datetime(2026, 10, 15, tzinfo=UTC),
        truncated=False,
        columns=tuple(columns),
        rows=tuple(rows),
        metadata=metadata,
        result_json=raw,
    )


def delivery(*facts, analysis="最慢的查询来自 etl 用户。"):
    return Delivery(
        content="结果",
        evidence_ids=("ev_inline",),
        channel="feishu",
        facts=facts,
        analysis=(AnswerInference(text=analysis, evidence_ids=("ev_inline",)),) if analysis else (),
    )


def audit_rows(count, stmt_chars):
    return [
        {
            "queryId": f"q-{i}",
            "user": "etl",
            "duration_ms": 120000 + i,
            "sql_text": "S" * stmt_chars,
        }
        for i in range(count)
    ]


def shown(message) -> str:
    return json.dumps(message, ensure_ascii=False)


def test_medium_result_is_inline_under_platform_limits_but_not_under_reply_chars() -> None:
    d = delivery(fact(AUDIT, audit_rows(3, 1500)))
    with pytest.raises(MessageCapacityError):
        build_feishu_message(d, 3500, CHAT, allow_preview=False)
    message = build_feishu_message(d, 3500, CHAT, allow_preview=False, platform_limits=True)
    text = shown(message)
    assert "card" in message and request_bytes(message, CHAT) <= 30_000
    assert text.count("S" * 1500) == 3 and "预览" not in text
    assert "最慢的查询来自 etl 用户。" in text and "SELECT 1" in text


def test_redundant_raw_json_is_omitted_with_honest_reason_and_technical_fields_stay() -> None:
    rows = audit_rows(2, 100)
    metadata = {"sql": "SELECT 1", "row_count": 2, "elapsed_ms": 1234567}
    d = delivery(fact(AUDIT, rows, metadata=metadata))
    text = shown(build_feishu_message(d, 3500, CHAT, allow_preview=False, platform_limits=True))
    # 可见表格已逐值保留原类型：原文只是第二份，直接省略并说明不是容量原因。
    assert "原文 JSON:" not in text and RAW_OMITTED in text and "容量" not in RAW_OMITTED
    assert "elapsed_ms: 1234567" in text and "row_count: 2" in text
    # 旧路径不受影响：仍附原文。
    legacy = shown(build_feishu_message(d, 3500, CHAT, allow_preview=False))
    assert "原文 JSON:" in legacy and RAW_OMITTED not in legacy


def _lossy_rows(kind: str):
    """可见单元格与原值类型不同（大整数→字符串、嵌套→JSON 文本），且带足够长的填充。"""
    big = 2**60
    if kind == "bigint":
        shown_rows = [{"id": str(big + i), "sql_text": "S" * 3000} for i in range(6)]
        raw_rows = [{"id": big + i, "sql_text": "S" * 3000} for i in range(6)]
    else:
        shown_rows = [{"tags": json.dumps(["a", i]), "sql_text": "S" * 3000} for i in range(6)]
        raw_rows = [{"tags": ["a", i], "sql_text": "S" * 3000} for i in range(6)]
    columns = tuple(shown_rows[0])
    raw = json.dumps({"sql": "SELECT 1", "columns": list(columns), "rows": raw_rows})
    return columns, shown_rows, raw


@pytest.mark.parametrize("kind", ["bigint", "nested"])
def test_lossy_fact_keeps_raw_json_required(kind: str) -> None:
    # 表格单独约 18 KB 放得下，表格加原文放不下：原文不能省，整体必须换附件。
    columns, rows, raw = _lossy_rows(kind)
    d = delivery(fact(columns, rows, raw=raw))
    with pytest.raises(MessageCapacityError):
        build_feishu_message(d, 3500, CHAT, allow_preview=False, platform_limits=True)
    *files, body = _data_parts(d, 0, 3500, CHAT, None)
    assert files and RAW_OMITTED not in body.body.decode()


def test_plain_text_keeps_reply_char_limit() -> None:
    receipt = Delivery(
        content="会" * 300, evidence_ids=(), channel="feishu", session_notice="提示" * 10
    )
    with pytest.raises(ValueError):
        build_feishu_message(receipt, 200, CHAT, allow_preview=False, platform_limits=True)


def test_preview_keeps_answer_first_and_states_exact_range() -> None:
    d = delivery(fact(AUDIT, audit_rows(3, 12_000)), analysis="结论：三条都是 etl 的批量写入。")
    text = shown(build_feishu_message(d, 3500, CHAT, platform_limits=True))
    assert "结论：三条都是 etl 的批量写入。" in text
    shown_rows = text.count("S" * 12_000)
    assert shown_rows < 3 and f"预览 {shown_rows}／本次返回 3 行" in text
    assert "（无数据行）" not in text


def test_zero_row_preview_never_claims_no_data() -> None:
    d = delivery(fact(("sql_text",), ({"sql_text": "S" * 40_000},)), analysis=None)
    text = shown(build_feishu_message(d, 3500, CHAT, platform_limits=True))
    assert "（无数据行）" not in text
    assert "预览 0／本次返回 1 行" in text and "结果行未展示。" in text


def test_metadata_fact_preview_counts_fields() -> None:
    metadata = {f"k{i}": "v" * 3000 for i in range(12)}
    d = delivery(
        fact((), (), metadata=metadata, raw=json.dumps(metadata), tool_id="grafana/search"),
        analysis=None,
    )
    text = shown(build_feishu_message(d, 3500, CHAT, platform_limits=True))
    assert "／共 12 个字段" in text and "预览" in text


def test_single_large_metadata_field_preview_is_honest_but_empty() -> None:
    # 已知边界（本切片不做 MCP 行视图）：看板列表通常是一个字符串字段，超限时预览不到条目。
    metadata = {"dashboards": json.dumps([{"title": "N" * 100}] * 400)}
    raw = json.dumps({"dashboards": json.loads(metadata["dashboards"])})
    d = delivery(fact((), (), metadata=metadata, raw=raw, tool_id="grafana/search"), analysis=None)
    text = shown(build_feishu_message(d, 3500, CHAT, platform_limits=True))
    assert "预览 0／共 1 个字段" in text and "结果行未展示。" in text
    assert "（无数据行）" not in text


def test_medium_result_without_file_request_has_no_attachment() -> None:
    d = delivery(fact(AUDIT, audit_rows(3, 1500)))
    (body,) = _data_parts(d, 0, 3500, CHAT, None)
    assert body.kind == "body" and body.body.decode().count("S" * 1500) == 3


def test_oversized_result_gets_file_and_body_with_answer_and_exact_preview() -> None:
    d = delivery(fact(AUDIT, audit_rows(3, 12_000)), analysis="结论：三条都是 etl 的批量写入。")
    *files, body = _data_parts(d, 0, 3500, CHAT, None)
    assert [f.kind for f in files] == ["md"]
    text = body.body.decode()
    assert "本次完整结果见附件。" in text and "结论：三条都是 etl 的批量写入。" in text
    assert "（无数据行）" not in text and "／本次返回 3 行" in text


def test_preview_of_metadata_fact_shows_some_fields() -> None:
    metadata = {f"host{i}": f"10.0.0.{i}" for i in range(400)}
    metadata["dashboards"] = "D" * 40_000
    d = delivery(
        fact((), (), metadata=metadata, raw=json.dumps(metadata), tool_id="grafana/search"),
        analysis=None,
    )
    message = _preview(
        d.model_copy(update={"report_format": "md"}), 3500, CHAT, None, "本次完整结果见附件。"
    )
    text = shown(message)
    assert "预览无法在本消息容量内展示" not in text and "10.0.0.0" in text


def test_element_budget_still_forces_attachment() -> None:
    facts = tuple(
        fact(("v",), ({"v": i},)).model_copy(update={"evidence_id": f"ev_{i}"}) for i in range(25)
    )
    d = delivery(*facts, analysis=None)
    everything = set(range(len(facts)))
    full = _card(d, [1] * len(facts), everything, (), everything)
    # 前提：完整卡片字节合规，只有组件超过 200。
    assert request_bytes(full, CHAT) <= 30_000 and _elements(full) > 200
    with pytest.raises(MessageCapacityError):
        build_feishu_message(d, 3500, CHAT, allow_preview=False, platform_limits=True)
    *files, body = _data_parts(d, 0, 3500, CHAT, None)
    assert [f.kind for f in files] == ["md"]
    # 25 个结果的固定组件即超过 200，放不下任何预览：如实说明，完整结果在附件。
    assert json.loads(body.body) == {"text": "本次完整结果见附件。\n预览无法在本消息容量内展示。"}


def test_old_saved_body_part_still_passes_capacity_check() -> None:
    small = delivery(fact(("v",), ({"v": 1},)))
    old = build_feishu_message(small, 3500, CHAT, allow_preview=False)
    part = DeliveryPart(0, "body", json.dumps(old, ensure_ascii=False).encode())
    _check_capacity((part,), 3500, CHAT, None, platform_limits=True)


def test_approval_parts_keep_reply_char_rule() -> None:
    card = {
        "card": {
            "schema": "2.0",
            "body": {
                "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": "批" * 4000}}]
            },
        }
    }
    part = DeliveryPart(0, "approval", json.dumps(card, ensure_ascii=False).encode())
    with pytest.raises(MessageCapacityError):
        _check_capacity((part,), 3500, CHAT, None, platform_limits=False)
    _check_capacity((part,), 3500, CHAT, None, platform_limits=True)
    text = DeliveryPart(1, "body", json.dumps({"text": "字" * 4000}, ensure_ascii=False).encode())
    with pytest.raises(MessageCapacityError):  # 纯文本在平台模式下仍受字符上限
        _check_capacity((text,), 3500, CHAT, None, platform_limits=True)


def test_production_round_r7_audit_rows_stay_inline_when_under_limit() -> None:
    rows = [
        {
            "execution_time": f"2026-10-15 14:0{i}:00",
            "queryId": f"6f1c-{i}",
            "user": "etl",
            "db": "dw",
            "duration_ms": 120000 + i,
            "duration_sec": 120.0 + i,
            "state": "EOF",
            "sql_text": "INSERT INTO dw.t SELECT *\nFROM ods.s\nWHERE dt = '2026-10-15'",
        }
        for i in range(3)
    ]
    (body,) = _data_parts(delivery(fact(tuple(rows[0]), rows)), 0, 3500, CHAT, None)
    text = body.body.decode()
    assert "（无数据行）" not in text and "6f1c-2" in text


def test_production_round_r14_dashboard_search_stays_inline() -> None:
    dashboards = [{"uid": f"rYdddlPWk{i}", "title": f"Node Exporter Full {i}"} for i in range(20)]
    metadata = {"dashboards": json.dumps(dashboards)}
    raw = json.dumps({"dashboards": dashboards})
    d = delivery(fact((), (), metadata=metadata, raw=raw, tool_id="grafana/search_dashboards"))
    parts = _data_parts(d, 0, 3500, CHAT, None)
    assert [p.kind for p in parts] == ["body"]
    assert "Node Exporter Full 19" in parts[0].body.decode()


# ---- 独立审查（494de3c）反例：原文只有在卡片确实完整展示同一内容时才可省略 ----


@pytest.mark.parametrize(
    "case",
    ["zero_rows", "null_metadata", "odd_columns", "extra_row_key", "null_sql", "hidden_char"],
)
def test_raw_json_kept_when_card_does_not_show_everything(case: str) -> None:
    columns, rows, metadata = ("x", "y"), [{"x": 1, "y": 2}], {"sql": "SELECT 1"}
    raw = None
    if case == "zero_rows":
        rows = []
    elif case == "null_metadata":
        metadata = {"sql": "SELECT 1", "scope": None}
    elif case == "odd_columns":
        raw = json.dumps({"sql": "SELECT 1", "columns": "WEIRD_COLS", "rows": rows})
    elif case == "extra_row_key":
        rows = [{"x": 1, "y": 2, "z": 3}]
    elif case == "null_sql":
        metadata = {"sql": None}
    else:
        columns, rows = ("a​b",), [{"a​b": 1}]
    d = delivery(fact(columns, rows, metadata=metadata, raw=raw))
    text = shown(build_feishu_message(d, 3500, CHAT, allow_preview=False, platform_limits=True))
    assert RAW_OMITTED not in text and "原文 JSON:" in text


def test_preview_never_claims_raw_json_matches_partial_rows() -> None:
    d = delivery(fact(AUDIT, audit_rows(10, 3000)), analysis=None)
    message = _preview(d.model_copy(update={"report_format": "md"}), 3500, CHAT, None, "见附件")
    text = shown(message)
    assert "／本次返回 10 行" in text and RAW_OMITTED not in text


def test_legacy_truncated_card_says_shown_not_preview() -> None:
    # 旧 v0 截断卡片没有附件：只说“展示 N／本次返回 M 行”。
    d = delivery(fact(AUDIT, audit_rows(3, 1500)), analysis=None)
    text = shown(build_feishu_message(d, 3500, CHAT))
    assert "展示 " in text and "／本次返回 3 行" in text and "预览 " not in text
