"""R3 brief view keeps approved JSON folded; it never changes evidence values."""

import json

from tests.sdk_core.test_feishu_render import delivery, fact, text_parts

from xiaowei.feishu_render import build_feishu_message
from xiaowei.models import AgentAnswer, DeliveryFact


def brief_fact(**values):
    raw = values.pop("result_json", None)
    original = fact(
        result_json=raw
        or json.dumps(
            {"columns": ["name"], "rows": [{"name": "R3_ORIGINAL_VALUE"}], "row_count": 1}
        ),
        **values,
    )
    return DeliveryFact.model_validate(
        {
            **original.model_dump(),
            "view": "brief",
            "summary": "\u672c\u6b21\u8fd4\u56de\u884c\u6570\uff1a1\u3002",
        }
    )


def test_brief_card_summary_visible_original_folded():
    source = brief_fact()
    message = build_feishu_message(delivery(source), 3500, "oc_test")
    visible = "\n".join(text_parts(message, folded=False))
    assert source.summary in visible
    assert "ev_original" not in visible and source.note in visible
    assert "ev_original" in "\n".join(text_parts(message))
    assert "R3_ORIGINAL_VALUE" not in visible
    assert "R3_ORIGINAL_VALUE" in "\n".join(text_parts(message))
    assert not any(e["tag"] == "table" for e in message["card"]["body"]["elements"])


def test_old_answer_defaults_to_full_without_relaxing_extra_fields():
    old = AgentAnswer.model_validate(
        {"evidence_ids": ["ev_old"], "inferences": [], "clarification": None}
    )
    assert old.fact_views == ()


def test_brief_original_omission_is_visible_when_capacity_cannot_hold_json():
    source = brief_fact(result_json="R3_LARGE_ORIGINAL_" + "x" * 7000)
    message = build_feishu_message(delivery(source), 600, "oc_test")
    visible = "\n".join(text_parts(message, folded=False))
    assert source.summary in visible
    assert "\u83b7\u51c6\u539f\u6587\u672a\u5c55\u793a" in visible
    assert "R3_LARGE_ORIGINAL" not in "\n".join(text_parts(message))
    assert "\u5b8c\u6574\u53ef\u5c55\u5f00" not in visible


def test_brief_preserves_pagination_and_real_loss_notes():
    source = brief_fact(
        tool_id="local/list_tables",
        truncated=True,
        metadata={"row_count": 1, "next_cursor": "cursor", "warning": "R3_VALUE_LOSS"},
    )
    message = build_feishu_message(delivery(source), 3500, "oc_test")
    visible = "\n".join(text_parts(message, folded=False))
    assert "\u8fd8\u6709\u540e\u7eed\u9875" in visible
    assert "R3_VALUE_LOSS" in visible
    assert "\u5de5\u5177\u7ed3\u679c\u5df2\u622a\u65ad" not in visible
    source = source.model_copy(update={"tool_id": "local/run_readonly_query"})
    visible = "\n".join(
        text_parts(build_feishu_message(delivery(source), 3500, "oc_test"), folded=False)
    )
    assert "\u5de5\u5177\u7ed3\u679c\u5df2\u622a\u65ad" in visible
