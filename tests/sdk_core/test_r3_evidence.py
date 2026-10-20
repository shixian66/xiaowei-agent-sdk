"""R3 real Evidence store: display choices do not relax any read boundary."""

import json
from typing import Any

import pytest
from agents.agent_output import AgentOutputSchema
from pydantic import ValidationError
from sqlalchemy.engine import URL
from tests.sdk_core.synthetic_tools import (
    CONTRACTS,
    TOTAL_TOOL,
    Clock,
    Grants,
    RegionArgs,
    context,
    ready_engine,
    request,
    store,
)

from xiaowei.evidence import AnswerRejectedError
from xiaowei.governance import Projection, ToolCatalog, ToolPolicy
from xiaowei.models import AUDIENCES, AgentAnswer, ToolObservation, TurnAnswer

pytestmark = pytest.mark.loopback
FIELDS = ("columns", "rows", "row_count", "sql", "next_cursor")


def tools(*, effect="read") -> ToolCatalog:
    policy = ToolPolicy(
        policy_id="synthetic.region",
        arguments=RegionArgs,
        projections={
            a: Projection(fields=FIELDS + (("web_only",) if a == "web" else ()), max_bytes=5000)
            for a in AUDIENCES
        },
        fact_note="R3_FIXED_NOTE",
        effect=effect,
    )
    return ToolCatalog(CONTRACTS, (policy,))


def selected(ids, entries=None, **extra) -> TurnAnswer:
    """S3：显示方式写在事实块上；同一证据的多个块由归一化去重（冲突取完整）。"""
    entries = [{"evidence_id": e, "view": "brief"} for e in ids] if entries is None else entries
    blocks = [
        {"type": "fact", **entry}
        for e in ids
        for entry in (
            [x for x in entries if x["evidence_id"] == e] or [{"evidence_id": e, "view": "full"}]
        )
    ] + [{"type": "fact", **x} for x in entries if x["evidence_id"] not in ids]
    body = {"blocks": blocks, "report_format": None, "awaiting_user": False, **extra}
    return TurnAnswer(answer=AgentAnswer.model_validate(body), context_evidence=tuple(ids))


async def recorded(evidence, ctx, *, count=2, rows=None, **extra):
    rows = [{"value": 100}, {"value": 200}] if rows is None else rows
    payload: dict[str, Any] = {
        "columns": ["value"],
        "rows": rows,
        "row_count": count,
        "sql": "SELECT value FROM records LIMIT 2",
        "next_cursor": None,
        "web_only": "R3_WEB_ONLY",
        **extra,
    }
    return await evidence.record(
        ctx,
        request(),
        ToolObservation(
            payload=payload,
            captured_at=Clock().now,
            truncated=False,
        ),
    )


@pytest.mark.parametrize("channel", ["web", "feishu"])
async def test_brief_uses_only_receiving_channel_and_keeps_original(postgres_url: URL, channel):
    grants = Grants()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, Clock(), tools())
        old = context(channel=channel)
        recorded_result = await recorded(evidence, old)
        ctx = context(channel=channel, turn="followup")
        delivery = await evidence.validate_answer(selected([recorded_result.evidence_id]), ctx)
        (fact,) = delivery.facts
        assert (
            fact.view == "brief"
            and fact.summary == "\u672c\u6b21\u8fd4\u56de\u884c\u6570\uff1a2\u3002"
        )
        assert fact.rows == ({"value": 100}, {"value": 200})
        assert fact.note == "R3_FIXED_NOTE"
        assert "采集时结果，不代表当前状态" in (fact.note_summary or "")
        assert json.loads(fact.result_json)["row_count"] == 2
        assert ("R3_WEB_ONLY" in fact.result_json) == (channel == "web")
        assert ("web_only" in fact.metadata) == (channel == "web")


@pytest.mark.parametrize(
    "count, rows, expected",
    [
        (0, [], "brief"),
        (1, [{"value": 9999}], "brief"),
        (None, None, "full"),
        (True, None, "full"),
        ("2", None, "full"),
        (-1, None, "full"),
        (10, None, "full"),
    ],
)
async def test_only_supported_returned_row_count_is_summarized(
    postgres_url: URL, count, rows, expected
):
    grants = Grants()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, Clock(), tools())
        result = await recorded(evidence, context(), count=count, rows=rows)
        fact = (
            await evidence.validate_answer(selected([result.evidence_id]), context(turn="followup"))
        ).facts[0]
        assert fact.view == expected
        if expected == "brief":
            assert fact.summary == f"\u672c\u6b21\u8fd4\u56de\u884c\u6570\uff1a{count}\u3002"
            assert "9999" not in fact.summary
        else:
            assert fact.summary is None


async def test_conflicting_display_entries_normalize_to_full(postgres_url: URL):
    grants = Grants()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, Clock(), tools())
        result = await recorded(evidence, context())
        entries = [
            {"evidence_id": result.evidence_id, "view": "brief"},
            {"evidence_id": result.evidence_id, "view": "full"},
        ]
        delivery = await evidence.validate_answer(
            selected([result.evidence_id], entries), context(turn="next")
        )
        assert [f.view for f in delivery.facts] == ["full"]


@pytest.mark.parametrize("entries", [[{"evidence_id": "NOT_CITED", "view": "brief"}]])
async def test_display_entries_must_be_cited(postgres_url: URL, entries):
    grants = Grants()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, Clock(), tools())
        result = await recorded(evidence, context())
        entries = [
            {
                **e,
                "evidence_id": result.evidence_id if e["evidence_id"] == "ID" else e["evidence_id"],
            }
            for e in entries
        ]
        with pytest.raises(AnswerRejectedError) as caught:
            await evidence.validate_answer(
                selected([result.evidence_id], entries), context(turn="next")
            )
        # 显示方式不再是独立列表：选择不可见证据就是引用不可见证据。
        assert caught.value.code == "unknown_evidence"


async def test_brief_does_not_make_invisible_evidence_readable(postgres_url: URL):
    grants = Grants()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, Clock(), tools())
        result = await recorded(evidence, context())
        turn = selected([result.evidence_id]).model_copy(update={"context_evidence": ()})
        with pytest.raises(AnswerRejectedError) as caught:
            await evidence.validate_answer(turn, context(turn="next"))
        assert caught.value.code == "unknown_evidence"


@pytest.mark.parametrize("boundary", ["revoked", "expired", "wrong_session", "wrong_target"])
async def test_brief_still_rechecks_read_boundaries(postgres_url: URL, boundary):
    grants, clock = Grants(), Clock()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, clock, tools())
        result = await recorded(evidence, context())
        ctx = context(turn="next")
        if boundary == "revoked":
            grants.revoke_all()
        elif boundary == "expired":
            clock.advance(100_000)
        elif boundary == "wrong_session":
            ctx = context(turn="next", session="other")
        else:
            ctx = context(turn="next", targets=frozenset())
        with pytest.raises(AnswerRejectedError) as caught:
            await evidence.validate_answer(selected([result.evidence_id]), ctx)
        assert caught.value.code == "evidence_unavailable"


async def test_current_and_historical_facts_can_mix_without_hiding_new_result(postgres_url: URL):
    grants = Grants()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, Clock(), tools())
        old = await recorded(evidence, context())
        ctx = context(turn="next")
        new = await recorded(evidence, ctx)
        ids = [old.evidence_id, new.evidence_id]
        answer = selected(ids, [{"evidence_id": old.evidence_id, "view": "brief"}])
        facts = (await evidence.validate_answer(answer, ctx)).facts
        assert [f.view for f in facts] == ["brief", "full"]
        assert facts[1].rows == facts[0].rows


async def test_proposals_cannot_be_compacted(postgres_url: URL):
    grants = Grants()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, Clock(), tools(effect="propose"))
        old = await recorded(evidence, context())
        delivered = await evidence.validate_answer(
            selected([old.evidence_id]), context(turn="next")
        )
        assert delivered.requires_complete
        assert delivered.facts[0].requires_complete
        assert delivered.facts[0].view == "full"


def test_sdk_closed_display_schema_and_old_default():
    schema = AgentOutputSchema(AgentAnswer).json_schema()
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["FactBlock"]["properties"]["view"]["enum"] == ["full", "brief"]
    assert schema["$defs"]["FactBlock"]["additionalProperties"] is False
    with pytest.raises(ValidationError):
        AgentAnswer.model_validate(
            {
                "blocks": [{"type": "fact", "evidence_id": "ev1", "view": "brief", "count": 123}],
                "report_format": None,
                "awaiting_user": False,
            }
        )


async def test_current_brief_has_no_summary_and_replay_keeps_original_turn_semantics(postgres_url):
    grants, clock = Grants(), Clock()
    grants.grant("alice", TOTAL_TOOL)
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, clock, tools())
        ctx = context()
        result = await recorded(evidence, ctx)
        turn = selected([result.evidence_id])
        for history in (False, True):
            shown = await evidence.validate_answer(turn, ctx, history=history)
            assert shown.facts[0].view == "brief" and shown.facts[0].summary is None
            assert shown.facts[0].rows == ({"value": 100}, {"value": 200})
        followup = await evidence.validate_answer(turn, context(turn="next"))
        assert followup.facts[0].summary == "本次返回行数：2。"
        (record,) = await evidence._readable(ctx, "web", [result.evidence_id], history=False)
        status = evidence._delivery_fact(record, "action", "brief", ctx)
        assert status.section == "action" and not status.requires_complete
        assert status.view == "full" and status.rows
