"""L1：意图与补槽（组件级）。

成功标准（§12）：候选集合与拒绝原因逐条固定；near-miss **不**匹配本能力；
缺槽时不产生计划、不调 Gateway。
"""

import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.admission import CONTEXT

from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.capabilities.specs import CAPABILITY_ID

_CORPUS = json.loads(
    (Path(__file__).parent / "corpus" / "l1_intent.json").read_text(encoding="utf-8")
)
_CASES: list[dict[str, Any]] = _CORPUS["cases"]
_SNAPSHOT = StaticCapabilityRegistry().snapshot()


def _draft(text: str) -> Any:
    return RuleBasedIntentInterpreter().interpret(text=text, context=CONTEXT)


def _by(category: str) -> list[dict[str, Any]]:
    return [case for case in _CASES if case["category"] == category]


def test_corpus_has_the_declared_case_counts() -> None:
    """语料规模是承诺的一部分：golden ×6、near-miss ×4、missing-context ×3。"""
    assert len(_by("golden")) == 6
    assert len(_by("near_miss")) == 4
    assert len(_by("missing_context")) == 3
    assert _by("slot_pollution")


@pytest.mark.parametrize("case", _by("golden"), ids=lambda c: c["id"])
def test_golden_phrasings_resolve_to_the_capability(case: dict[str, Any]) -> None:
    draft = _draft(case["text"])
    assert draft.intent == case["expect_intent"]
    for key, value in case.get("expect_slots", {}).items():
        assert draft.slots.get(key) == value
    candidates = DeterministicCapabilityResolver().resolve(
        draft=draft, context=CONTEXT, snapshot=_SNAPSHOT
    )
    assert {item.capability_id for item in candidates.items} == {CAPABILITY_ID}


@pytest.mark.parametrize("case", _by("near_miss"), ids=lambda c: c["id"])
def test_near_miss_phrasings_produce_no_candidate(case: dict[str, Any]) -> None:
    """匹配过宽等于把别的领域误路由过来；拒绝原因必须逐条固定。"""
    draft = _draft(case["text"])
    assert draft.intent == case["expect_intent"]
    candidates = DeterministicCapabilityResolver().resolve(
        draft=draft, context=CONTEXT, snapshot=_SNAPSHOT
    )
    assert candidates.items == ()
    assert {r.reason_code for r in candidates.rejections} == {"intent.does_not_match_capability"}


@pytest.mark.parametrize("case", _by("missing_context"), ids=lambda c: c["id"])
def test_missing_context_cases_do_not_invent_slots(case: dict[str, Any]) -> None:
    draft = _draft(case["text"])
    if case.get("expect_no_window_slot"):
        assert "window_minutes" not in draft.slots
    if case.get("expect_no_database_slot"):
        assert "database" not in draft.slots


@pytest.mark.parametrize("case", _by("slot_pollution"), ids=lambda c: c["id"])
def test_slot_pollution_never_reaches_the_draft(case: dict[str, Any]) -> None:
    draft = _draft(case["text"])
    for forbidden in case["forbidden_slots"]:
        assert forbidden not in draft.slots


def test_resolution_is_stable_across_calls() -> None:
    draft = _draft("最近30分钟有哪些慢查询")
    resolver = DeterministicCapabilityResolver()
    first = resolver.resolve(draft=draft, context=CONTEXT, snapshot=_SNAPSHOT)
    second = resolver.resolve(draft=draft, context=CONTEXT, snapshot=_SNAPSHOT)
    assert first == second
