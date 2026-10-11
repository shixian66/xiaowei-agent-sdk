"""R3 eval preparation stays on the same formal, version-bound harness."""

import pytest
from tests.sdk_core import dialogue_gate
from tests.sdk_core.dialogue_gate import Scenario, run_dialogue
from tests.sdk_core.test_app import Scripts, cite, tool_call
from tests.sdk_core.test_model_api import OPENAI as PROFILE
from tests.sdk_core.test_r3_entrypoints import brief

pytestmark = pytest.mark.loopback


def test_fixed_r3_tasks_cover_short_counts_original_ddl_pagination_and_no_execute():
    scenarios = getattr(dialogue_gate, "R3_SCENARIOS", ())
    names = {s.name for s in scenarios}
    assert {
        "r3_five_rows",
        "r3_aggregate",
        "r3_databases",
        "r3_structure",
        "r3_ddl_history_and_refresh",
        "r3_pages_with_empty_middle",
        "r3_sql_only",
        "r3_raw_over_limit",
    } <= names


async def test_offline_harness_observes_brief_without_call_order_requirement(
    postgres_url,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "offline-r3-key")
    scripts = Scripts()
    first = scripts.add(
        "Read synthetic five rows",
        tool_call(
            "run_readonly_query",
            cluster="pre",
            sql="SELECT region, amount FROM data_center.quota_virtual_account_quasi_rt LIMIT 5",
        ),
        cite(""),
    )
    short = scripts.add("How many came back?", brief)
    case = Scenario("offline_r3", (("query", first), ("query", short)))
    results = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(case,), repeats=1
    )
    assert [r["state"] for r in results] == ["completed", "completed"]
    assert results[0]["facts"][0]["view"] == "full"
    assert results[1]["facts"][0]["view"] == "brief"
    assert results[1]["facts"][0]["rows"] == 5
    assert "query" not in results[1]["io_counts"]
    assert results[1]["manual_review_required"] and results[1]["review"] is None
