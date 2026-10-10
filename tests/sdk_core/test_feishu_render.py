"""U2：纯文本静态卡片与两个完整容量预算，预期从公开请求和人工样本独立推导。"""

import json
import math
from datetime import UTC, datetime
from typing import Any

import pytest

from xiaowei import feishu_render
from xiaowei.feishu_render import (
    ANALYSIS_CLIPPED,
    CAPACITY_NOTICE,
    FACTS_CLIPPED,
    build_feishu_message,
    request_bytes,
)
from xiaowei.models import AnswerInference, Delivery, DeliveryFact
from xiaowei.starrocks_tools import DDL_NOTE, DDL_TOO_LARGE, PLAN_NOTE

AT = datetime(2026, 10, 9, 11, 47, 56, tzinfo=UTC)


def fact(**values: Any) -> DeliveryFact:
    return DeliveryFact(
        **{
            "evidence_id": "ev_original",
            "tool_id": "local/run_readonly_query",
            "target_id": "pre",
            "captured_at": AT,
            "truncated": False,
            "columns": ("name",),
            "rows": ({"name": "orders"},),
            "metadata": {
                "sql": "SELECT name FROM shop.orders LIMIT 5",
                "row_count": 1,
                "elapsed_ms": 17,
                "next_cursor": None,
            },
            "note": "采集时的结果，历史不代表当前状态。",
            **values,
        }
    )


def delivery(*facts: DeliveryFact, analysis: str | None = None) -> Delivery:
    ids = tuple(f.evidence_id for f in facts)
    return Delivery(
        content="旧转义文字不是卡片的数据源",
        channel="feishu",
        evidence_ids=ids,
        facts=facts,
        analysis=() if analysis is None else (AnswerInference(text=analysis, evidence_ids=ids),),
    )


def text_parts(value: Any, *, folded: bool = True) -> list[str]:
    """按公开卡片格式取所有文字；不借产品容量计算来构造预期。"""
    if isinstance(value, list):
        return [part for child in value for part in text_parts(child, folded=folded)]
    if not isinstance(value, dict):
        return []
    if value.get("tag") == "plain_text":
        return [value["content"]]
    if value.get("tag") == "collapsible_panel" and not folded and not value["expanded"]:
        return text_parts(value["header"], folded=False)
    if value.get("tag") == "table":
        return [c["display_name"] for c in value["columns"]] + [
            cell for row in value["rows"] for cell in row.values()
        ]
    if set(value) == {"text"}:
        return [value["text"]]
    return [part for child in value.values() for part in text_parts(child, folded=folded)]


def tables(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in message["card"]["body"]["elements"] if e["tag"] == "table"]


def message_text(message: str | dict[str, Any]) -> str:
    """测试发送记录：普通回执或卡片中的全部纯文本，不改变传给 SDK 的消息。"""
    return message if isinstance(message, str) else "\n".join(text_parts(message))


def size(message: dict[str, Any], chat: str = "oc_test", reply: str | None = None) -> int:
    kind = "interactive" if "card" in message else "text"
    content = json.dumps(message["card"] if "card" in message else message, ensure_ascii=False)
    body = {"content": content, "msg_type": kind, "uuid": "0" * 36}
    if reply is None:
        body["receive_id"] = chat
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())


def test_facts_visible_once_analysis_multiline_and_details_folded() -> None:
    source = fact()
    message = build_feishu_message(delivery(source, analysis="第一行\n第二行"), 3500, "oc_test")
    assert message["card"]["schema"] == "2.0"
    visible = "\n".join(text_parts(message, folded=False))
    assert "ev_original" in visible and "2026-10-09T11:47:56+00:00" in visible
    assert source.note in visible
    assert "分析与建议（模型推断）" in visible and "第一行\n第二行" in visible
    assert visible.count('"orders"') == 1
    assert "SELECT name" not in visible and "next_cursor" not in visible
    all_text = "\n".join(text_parts(message))
    assert "SELECT name FROM shop.orders LIMIT 5" in all_text
    assert "依据：ev_original" in visible
    assert visible.index('"orders"') < visible.index("分析与建议")
    assert "旧转义文字" not in all_text


@pytest.mark.parametrize("tool", ["local/list_databases", "local/describe_table_layout"])
def test_notes_and_capture_time_are_not_hidden_with_technical_fields(tool: str) -> None:
    message = build_feishu_message(delivery(fact(tool_id=tool)), 3500, "oc_test")
    visible = "\n".join(text_parts(message, folded=False))
    assert "采集时的结果" in visible and "采集于" in visible
    assert "row_count" not in visible and "elapsed_ms" not in visible


def test_ddl_is_plain_multiline_result_not_a_folded_code_block() -> None:
    ddl = 'CREATE TABLE orders (\n  id BIGINT DEFAULT 0\n)\nPROPERTIES ("x"="```<at id=all>");'
    message = build_feishu_message(
        delivery(
            fact(
                tool_id="local/show_create_table",
                columns=("database", "table", "ddl"),
                rows=({"database": "shop", "table": "orders", "ddl": ddl},),
            )
        ),
        3500,
        "oc_test",
    )
    assert ddl in text_parts(message, folded=False)
    assert tables(message) == []


def test_sql_and_json_result_columns_are_visible_instead_of_technical_details() -> None:
    sql = "SELECT 1\nFROM orders"
    raw = '{\n  "id": 9223372036854775807\n}'
    message = build_feishu_message(
        delivery(fact(columns=("stmt", "json"), rows=({"stmt": sql, "json": raw},))),
        3500,
        "oc_test",
    )
    visible = "\n".join(text_parts(message, folded=False))
    assert sql in visible and raw.replace('"', '\\"') in visible
    assert "原文 JSON" not in visible  # 额外的工具封装仍收起，不重画一份结果。


def test_plain_cells_distinguish_null_empty_missing_and_original_characters() -> None:
    message = build_feishu_message(
        delivery(
            fact(
                columns=("null", "word", "empty", "missing", "big", "nested", "actual", "literal"),
                rows=(
                    {
                        "null": None,
                        "word": "NULL",
                        "empty": "",
                        "big": "9223372036854775807",
                        "nested": '{"x": [1, null]}',
                        "actual": "a\nb",
                        "literal": "a\\nb",
                    },
                ),
            )
        ),
        3500,
        "oc_test",
    )
    (table,) = tables(message)
    assert all(c["data_type"] == "text" for c in table["columns"])
    cells = list(table["rows"][0].values())
    assert cells[:4] == ["NULL（空值）", '"NULL"', '""', "（未提供）"]
    assert "9223372036854775807" in cells[4]
    assert cells[6:] == ['"a\nb"', '"a\\\\nb"']
    assert '"x"' in cells[5].replace('\\"', '"')


def test_untrusted_values_cannot_add_elements_links_media_or_mentions() -> None:
    value = '<at id="all">\n[链接](https://evil.invalid)\n![图](img_key)\n<font color="red">```\n'
    source = fact(columns=(value,), rows=({value: value},), note=value)
    message = build_feishu_message(delivery(source, analysis=value), 3500, "oc_test")

    def check(node: Any) -> None:
        if isinstance(node, dict):
            assert node.get("tag") not in {"markdown", "lark_md", "at", "img", "button"}
            assert not ({"url", "card_link", "behaviors"} & set(node))
            if node.get("tag") == "table":
                assert all(c["data_type"] == "text" for c in node["columns"])
            for child in node.values():
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)

    check(message)
    assert len(tables(message)) == 1
    assert value in text_parts(message)  # 分析/note 原文，只作 plain_text。


@pytest.mark.parametrize("kind", ["需要澄清", "建议"])
def test_advice_and_clarification_keep_newlines_and_do_not_chunk(kind: str) -> None:
    raw = "SELECT 1\nFROM orders\n\u202e伪装" + "x" * 4000
    unverified = Delivery(
        content=f"{kind}\n旧转义内容",
        channel="feishu",
        evidence_ids=(),
        feishu_text=raw,
    )
    message = build_feishu_message(unverified, 200, "oc_test")
    assert set(message) == {"text"}
    assert "SELECT 1\nFROM orders" in message["text"]
    assert "\\u202e" in message["text"] and "\u202e" not in message["text"]
    assert "旧转义" not in message["text"] and "截断" in message["text"]
    assert len(message["text"]) <= 200


def test_query_sql_has_capacity_before_whole_rows_and_analysis() -> None:
    source = fact(rows=tuple({"name": str(n) + "x" * 100} for n in range(10)))
    message = build_feishu_message(delivery(source, analysis="analysis" * 100), 500, "oc_test")
    shown = "\n".join(text_parts(message))
    assert source.metadata["sql"] in shown
    assert FACTS_CLIPPED in shown and ANALYSIS_CLIPPED in shown
    # 用户批准事实优先；最后一点余量仍可容纳部分分析，但它不能挤掉任何完整结果行。
    assert tables(message)[0]["rows"] == [{"c0": '"' + str(i) + "x" * 100 + '"'} for i in (0, 1)]
    if tables(message):
        assert list(tables(message)[0]["rows"][0].values()) == ['"0' + "x" * 100 + '"']
    assert sum(map(len, text_parts(message))) <= 500


def test_too_long_sql_is_marked_and_does_not_force_another_query() -> None:
    source = fact(metadata={"sql": "SELECT " + "x" * 2000})
    message = build_feishu_message(delivery(source), 500, "oc_test")
    visible = "\n".join(text_parts(message, folded=False))
    assert "SQL 未展示" in visible and '"orders"' in visible
    assert "SELECT " not in visible


@pytest.mark.parametrize("reply", [None, "om_original"])
def test_full_outer_body_budget_equal_and_one_byte_below(reply: str | None) -> None:
    value = '引号"反斜杠\\' * 150
    source = fact(rows=({"name": value},), metadata={"sql": "SELECT name FROM orders"})
    full = build_feishu_message(delivery(source), 3500, "oc_test", reply_to=reply)
    needed = size(full, reply=reply)
    assert request_bytes(full, "oc_test", reply_to=reply) == needed
    exact = build_feishu_message(
        delivery(source), 3500, "oc_test", reply_to=reply, max_bytes=needed
    )
    assert exact == full
    shorter = build_feishu_message(
        delivery(source), 3500, "oc_test", reply_to=reply, max_bytes=needed - 1
    )
    assert shorter != full and size(shorter, reply=reply) < needed
    assert not tables(shorter)  # 不切半个单元格/行。


def test_character_budget_equal_and_one_character_below() -> None:
    source = fact(rows=({"name": "x" * 300},), metadata={"sql": "SELECT name FROM orders"})
    full = build_feishu_message(delivery(source), 3500, "oc_test")
    needed = sum(map(len, text_parts(full)))
    assert build_feishu_message(delivery(source), needed, "oc_test") == full
    shorter = build_feishu_message(delivery(source), needed - 1, "oc_test")
    assert shorter != full and sum(map(len, text_parts(shorter))) <= needed - 1
    assert FACTS_CLIPPED in "\n".join(text_parts(shorter))


def test_wide_table_hits_bytes_first_and_never_sends_oversized_envelope() -> None:
    columns = tuple(f"c{i}" for i in range(50))
    row = dict.fromkeys(columns, '"\\')
    source = fact(columns=columns, rows=tuple(dict(row) for _ in range(10)))
    full = build_feishu_message(delivery(source), 3500, "oc_test")
    # 结构键与重复的单元格封装不是显示字符，但都计入最终请求。
    budget = size(full) - 2000
    shown = build_feishu_message(delivery(source), 3500, "oc_test", max_bytes=budget)
    assert size(shown) <= budget and shown != full
    assert sum(map(len, text_parts(shown))) < 3500
    assert FACTS_CLIPPED in "\n".join(text_parts(shown))


def test_capacity_notice_preserves_no_fragment_of_mandatory_provenance() -> None:
    message = build_feishu_message(delivery(fact(note="必要限制" * 400)), 200, "oc_test")
    assert message == {"text": CAPACITY_NOTICE}
    assert len(message["text"]) <= 200 and size(message) <= 30_000


def test_pagination_is_not_reported_as_loss_but_real_value_loss_is() -> None:
    paged = fact(tool_id="local/list_tables", truncated=True, metadata={"next_cursor": "cursor"})
    shown = "\n".join(
        text_parts(build_feishu_message(delivery(paged), 3500, "oc_test"), folded=False)
    )
    assert "还有后续页" in shown and "工具结果已截断" not in shown
    lost = fact(
        truncated=True, rows=({"name": "原文…（原值已截短）"},), metadata={"next_cursor": "cursor"}
    )
    shown = "\n".join(
        text_parts(build_feishu_message(delivery(lost), 3500, "oc_test"), folded=False)
    )
    assert "还有后续页" in shown and "工具结果已截断" in shown


def test_six_sources_and_wide_results_use_plain_fields_after_platform_table_limit() -> None:
    sources = tuple(
        fact(evidence_id=f"ev_{i}", rows=({"name": str(i)},), metadata={}, note=None)
        for i in range(6)
    )
    message = build_feishu_message(delivery(*sources), 3500, "oc_test")
    assert len(tables(message)) == 5
    assert all(f"ev_{i}" in "\n".join(text_parts(message)) for i in range(6))
    assert '"5"' in "\n".join(text_parts(message))


def test_web_projection_is_never_accepted_by_feishu_renderer() -> None:
    with pytest.raises(ValueError, match="渠道"):
        build_feishu_message(
            delivery(fact()).model_copy(update={"channel": "web"}), 3500, "oc_test"
        )


def test_text_budget_counts_mention_sanitizing_before_the_sdk() -> None:
    source = Delivery(content="<at id=all>" * 1000, channel="feishu", evidence_ids=())
    message = build_feishu_message(source, 3500, "oc_test", max_bytes=1500)
    assert "<at" not in message["text"] and "＜at" in message["text"]
    assert size(message) <= 1500


def test_wide_fallback_stays_under_component_cap_and_cuts_whole_rows() -> None:
    # 51 列超出表格上限，使用纯文本字段；每行一个 div + plain_text，各组件计数。
    columns = tuple(f"c{i}" for i in range(51))
    source = fact(columns=columns, rows=tuple(dict.fromkeys(columns, i) for i in range(200)))
    message = build_feishu_message(delivery(source), 100_000, "oc_test", max_bytes=300_000)
    assert tables(message) == []
    elements = message["card"]["body"]["elements"]
    assert len(elements) * 2 + 1 <= 200
    rows = [
        e["text"]["content"]
        for e in elements
        if e["tag"] == "div" and e["text"]["content"].startswith("c0:")
    ]
    assert 0 < len(rows) < 200
    assert all(row.endswith(f"c50: {i}") for i, row in enumerate(rows))
    assert FACTS_CLIPPED in text_parts(message)


def test_empty_result_is_visible_without_a_fake_row_or_loss_marker() -> None:
    source = fact(rows=(), metadata={"row_count": 0, "sql": "SELECT name FROM orders LIMIT 5"})
    message = build_feishu_message(delivery(source), 3500, "oc_test")
    assert "（无数据行）" in text_parts(message, folded=False)
    assert tables(message) == [] and FACTS_CLIPPED not in text_parts(message)


def test_table_limit_explanation_is_visible_even_without_optional_json() -> None:
    source = fact(
        tool_id="local/show_create_table",
        columns=("table", "ddl"),
        rows=(),
        metadata={"message": DDL_TOO_LARGE, "row_count": 0},
        note=DDL_NOTE,
        truncated=True,
        result_json="OPTIONAL_JSON_SENTINEL" * 1000,
    )
    message = build_feishu_message(delivery(source), 3500, "oc_test")
    visible = "\n".join(text_parts(message, folded=False))
    assert DDL_TOO_LARGE in visible and DDL_NOTE in visible
    assert "工具结果已截断" in visible and "（无数据行）" in visible
    assert "OPTIONAL_JSON_SENTINEL" not in "\n".join(text_parts(message))
    assert "row_count" not in visible and size(message) <= 30_000
    too_small = build_feishu_message(delivery(source), 80, "oc_test")
    assert too_small == {"text": CAPACITY_NOTICE}


def test_absent_projection_is_not_described_as_empty_rows() -> None:
    source = fact(columns=(), rows=(), metadata={}, result_json=None)
    message = build_feishu_message(delivery(source), 3500, "oc_test")
    assert "（无结果）" in text_parts(message, folded=False)
    assert "（无数据行）" not in text_parts(message, folded=False)


def test_advice_keeps_unverified_label() -> None:
    message = build_feishu_message(
        Delivery(
            content="建议（本轮未执行业务查询）",
            feishu_text="你好",
            channel="feishu",
            evidence_ids=(),
        ),
        3500,
        "oc_test",
    )
    assert "未执行查询" in message["text"] and "未经系统核实" in message["text"]


def test_monitoring_failure_notice_is_visible_in_fact_card_and_advice() -> None:
    notice = "未核实监控源（系统记录）：prometheus-prod（上游认证或授权失败）。"
    with_fact = delivery(
        fact(
            tool_id="prometheus-other/query", target_id="prometheus-other", metadata={"query": "up"}
        )
    ).model_copy(update={"monitoring_notice": notice})
    card = build_feishu_message(with_fact, 3500, "oc_test")
    assert notice in "\n".join(text_parts(card, folded=False))

    no_fact = Delivery(
        content="排查建议（监控读取失败；模型生成，未经数据验证）\n" + notice,
        feishu_text=notice + "\n未经数据验证：检查采集端。",
        monitoring_notice=notice,
        channel="feishu",
        evidence_ids=(),
    )
    text_message = build_feishu_message(no_fact, 3500, "oc_test")
    assert notice in text_message["text"]
    assert "未经数据验证" in text_message["text"]


def test_short_fact_fits_when_zero_row_placeholders_would_not() -> None:
    source = fact(
        evidence_id="ev_123456789012345678901234",
        tool_id="local/explain_query",
        target_id="cluster_name_with_32_characters_",
        columns=("plan",),
        rows=({"plan": "SCAN"},),
        metadata={"sql": "EXPLAIN SELECT region FROM shop.orders"},
        result_json='{"plan":"SCAN"}',
        note=PLAN_NOTE,
    )
    complete = build_feishu_message(delivery(source), 3500, "oc_test")
    primary = {
        "card": {
            **complete["card"],
            "body": {
                "elements": [
                    e
                    for e in complete["card"]["body"]["elements"]
                    if e["tag"] != "collapsible_panel"
                ]
            },
        }
    }
    needed = sum(map(len, text_parts(primary)))
    for budget in (needed, needed + 1, needed + 12):
        shown = build_feishu_message(delivery(source), budget, "oc_test")
        assert "card" in shown
        assert tables(shown)[0]["rows"] == [{"c0": '"SCAN"'}]
        assert FACTS_CLIPPED not in text_parts(shown)
        assert sum(map(len, text_parts(shown))) <= budget and size(shown) <= 30_000
    assert build_feishu_message(delivery(source), needed, "oc_test") == primary
    needed_bytes = size(primary)
    for budget in (needed_bytes, needed_bytes + 1):
        byte_limited = build_feishu_message(delivery(source), 3500, "oc_test", max_bytes=budget)
        assert tables(byte_limited)[0]["rows"] == [{"c0": '"SCAN"'}]
        assert size(byte_limited) <= budget
    below = build_feishu_message(delivery(source), needed - 1, "oc_test")
    assert '"SCAN"' not in text_parts(below)
    assert sum(map(len, text_parts(below))) <= needed - 1 and size(below) <= 30_000


def test_short_prefix_survives_larger_zero_row_placeholders() -> None:
    short = fact(evidence_id="ev_short", columns=("a",), rows=({"a": 1},), metadata={})
    long = fact(evidence_id="ev_long", columns=("a",), rows=({"a": "x" * 1500},), metadata={})
    complete = build_feishu_message(delivery(short, long), 3500, "oc_test")
    elements = complete["card"]["body"]["elements"]
    second_table = tables(complete)[1]
    expected = {
        "card": {
            **complete["card"],
            "body": {
                "elements": [
                    {"tag": "div", "text": {"tag": "plain_text", "content": "结果行未展示。"}}
                    if e is second_table
                    else e
                    for e in elements
                ]
                + [{"tag": "div", "text": {"tag": "plain_text", "content": FACTS_CLIPPED}}]
            },
        }
    }
    needed = sum(map(len, text_parts(expected)))
    shown = build_feishu_message(delivery(short, long), needed, "oc_test")
    assert "card" in shown
    assert [t["rows"] for t in tables(shown)] == [[{"c0": "1"}]]
    assert FACTS_CLIPPED in text_parts(shown)
    assert sum(map(len, text_parts(shown))) <= needed and size(shown) <= 30_000


def test_capacity_search_builds_logarithmically_many_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    # 五条实际 SQL、每条 400 行：字节预算触发裁剪，字符预算不先挡住搜索。
    rows = tuple({"v": i} for i in range(400))
    sources = tuple(
        fact(
            evidence_id=f"ev_{i}",
            columns=("v",),
            rows=rows,
            metadata={"sql": f"SELECT v FROM db.t{i} LIMIT 400", "row_count": 400},  # noqa: S608 -- synthetic only.
        )
        for i in range(5)
    )
    actual_card = feishu_render._card
    builds = 0

    def counted(*args, **kwargs):
        nonlocal builds
        builds += 1
        return actual_card(*args, **kwargs)

    monkeypatch.setattr(feishu_render, "_card", counted)
    shown = build_feishu_message(delivery(*sources), 100_000, "oc_test")
    assert "card" in shown and 0 < sum(len(t["rows"]) for t in tables(shown)) < 2000
    assert size(shown) <= 30_000 and FACTS_CLIPPED in text_parts(shown)
    assert all(source.metadata["sql"] in "\n".join(text_parts(shown)) for source in sources)
    # 每次 SQL 选择复用同一前缀搜索；每条事实最多 log(399) 次内点、两个端点/下界。
    per_search = 2 + len(sources) * (math.ceil(math.log2(399)) + 2)
    bound = (len(sources) + 1) * per_search + len(sources) + 2
    assert builds <= bound, f"card builds={builds}, logarithmic bound={bound}"
