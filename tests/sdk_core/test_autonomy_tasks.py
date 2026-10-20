"""T1/T2/T4 的正式 runtime 离线材料；脚本输出不证明 Agent 自主质量。T3 复用监控入口。"""

import pytest
from tests.sdk_core import dialogue_gate
from tests.sdk_core.test_app import Scripts, answer, cite, clarify, evidence_in, tool_call
from tests.sdk_core.test_model_api import OPENAI as PROFILE

pytestmark = pytest.mark.loopback

ORDER_SQL = (
    "SELECT status, COUNT(*) AS n FROM biz.orders "
    "WHERE created_at >= '2026-10-19 00:00:00' "
    "AND created_at < '2026-10-20 00:00:00' GROUP BY status"
)


async def collect_case(postgres_url, monkeypatch, task, steps):
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "synthetic-autonomy-key")
    scenario = next(case for case in dialogue_gate.AUTONOMY_SCENARIOS if case.name == task)
    scripts = Scripts()
    scripts.add(scenario.messages[0][1], *steps)
    samples = []

    async def inspect_sample(sample):
        samples.append(sample)
        return dialogue_gate.ReviewVerdict()  # 不自动给自然语言/自主性判通过。

    reports = await dialogue_gate.run_dialogue(
        PROFILE,
        postgres_url,
        scripts.transport,
        scenarios=(scenario,),
        repeats=1,
        manual_review=inspect_sample,
    )
    return samples[0], reports[0]


@pytest.mark.parametrize("task", ["T1", "T1-chat"])
async def test_t1_order_status_counts_keep_business_window_and_evidence(
    postgres_url,
    monkeypatch,
    task,
):
    def final(call):
        return answer(
            evidence_in(call),
            "合成统计为 100 笔，按创建时间统计。"
            if task == "T1-chat"
            else '<h2 style="color: #fff; background: #111">订单状态</h2>',
            report_format="html" if task == "T1" else None,
        )

    sample, report = await collect_case(
        postgres_url,
        monkeypatch,
        task,
        [
            tool_call("run_readonly_query", cluster="fat", sql=ORDER_SQL),
            final,
        ],
    )
    assert report["state"] == "completed"
    assert report["report_format"] == ("html" if task == "T1" else None)
    fact = sample.delivery.facts[0]
    assert fact.columns == ("status", "n")
    assert len(fact.rows) == 3
    assert {row["status"]: row["n"] for row in fact.rows} == {
        "paid": 70,
        "cancelled": 20,
        "pending": 10,
    }
    assert fact.metadata["sql"].startswith(ORDER_SQL)
    assert report["environment_scope"]["out_of_scope_calls"] == 0
    assert report["io_counts"]["query"] == 1
    assert report["requests"] and report["elapsed_ms"] >= 0
    assert report["review"]["evidence_consistent"] is None


async def test_t2_newest_visible_candidate_differs_from_slowest_without_executing_sql(
    postgres_url,
    monkeypatch,
):
    sample, report = await collect_case(
        postgres_url,
        monkeypatch,
        "T2-chat",
        [
            tool_call(
                "list_slow_queries",
                cluster="fat",
                window_minutes=60,
                order_by="query_time",
                database=None,
            ),
            cite("较晚的候选才是本次可见范围内最新；没有 Profile，建议未经执行验证。"),
        ],
    )
    assert report["state"] == "completed"
    fact = sample.delivery.facts[0]
    assert fact.rows[0]["query_time_ms"] > fact.rows[1]["query_time_ms"]
    assert fact.rows[0]["started_at"] < fact.rows[1]["started_at"]
    assert fact.rows[0]["sql"] != fact.rows[1]["sql"]
    assert report["io_counts"].get("query", 0) == 0
    assert report["io_counts"].get("explain", 0) == 0
    assert report["io_counts"]["audit"] == 1


async def test_t4_missing_environment_has_zero_database_tool_io(postgres_url, monkeypatch):
    sample, report = await collect_case(postgres_url, monkeypatch, "T4", [clarify()])
    assert report["state"] == "completed"
    assert sample.delivery.awaiting_user
    assert sample.delivery.facts == ()
    assert report["environment_scope"]["starrocks_calls"] == 0
    assert report["io_counts"].get("query", 0) == 0
    assert report["tool_choices_by_target"] == {}


async def test_t1_fixture_exposes_a_wrong_time_window_instead_of_returning_expected_totals(
    postgres_url,
    monkeypatch,
):
    wrong = ORDER_SQL.replace("2026-10-19", "2026-10-18").replace("2026-10-20", "2026-10-19")
    sample, report = await collect_case(
        postgres_url,
        monkeypatch,
        "T1-chat",
        [
            tool_call("run_readonly_query", cluster="fat", sql=wrong),
            cite("需人工核对口径"),
        ],
    )
    assert list(sample.delivery.facts[0].rows) == [{"status": "paid", "n": 1}]
    assert report["review"]["evidence_consistent"] is None
