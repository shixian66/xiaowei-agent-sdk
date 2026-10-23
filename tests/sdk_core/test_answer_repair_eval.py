"""P4 channel evidence: real SDK/runtime/storage/delivery; external I/O is synthetic."""

import asyncio
import json
from dataclasses import replace

import pytest
from tests.sdk_core import dialogue_gate
from tests.sdk_core.test_app import Scripts, answer, evidence_in, tool_call
from tests.sdk_core.test_autonomy_tasks import TOP3_SQL
from tests.sdk_core.test_model_api import OPENAI as PROFILE
from tests.sdk_core.test_runtime import env as env  # pytest fixture

pytestmark = pytest.mark.loopback


async def test_collector_sends_bound_files_and_keeps_content_out_of_report(
    postgres_url, monkeypatch
):
    from tests.sdk_core.dialogue_delivery import ChannelDelivery

    original_send = ChannelDelivery.send

    async def delayed_body(self, chat_id, value, **kwargs):
        if isinstance(value, dict) and kwargs.get("uuid") is not None:
            await asyncio.sleep(0.02)
        return await original_send(self, chat_id, value, **kwargs)

    monkeypatch.setattr(ChannelDelivery, "send", delayed_body)
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "synthetic-p4-key")
    case = replace(dialogue_gate.AUTONOMY_SCENARIOS[0], name="p4_delivery_success")
    scripts = Scripts()
    scripts.add(
        case.messages[0][1],
        tool_call("run_readonly_query", cluster="fat", sql=TOP3_SQL),
        lambda call: answer(evidence_in(call), "合成关键答案", report_format="md"),
    )
    samples = []

    async def review(sample):
        samples.append(sample)
        return dialogue_gate.ReviewVerdict()

    reports = await dialogue_gate.run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(case,), repeats=1, manual_review=review
    )
    result = reports[0]
    assert result["delivery"]["state"] == "sent"
    assert result["delivery"]["confirmed_parts"] == result["delivery"]["file_count"] + 1
    assert result["delivery"]["file_count"] >= 1
    assert (
        0 <= result["delivery"]["first_body_ack_ms"] <= result["delivery"]["complete_delivery_ms"]
    )
    captured = samples[0].channel_delivery
    # A file receipt is deliberately earlier; it must not be labelled as a body receipt.
    assert captured.first_body_ack_ms > min(captured.receipt_times.values())
    assert "合成关键答案" in captured.visible_text
    originals = "\n".join(f.content.decode() for f in captured.files)
    assert all(
        value in originals
        for value in ("slow-a", "slow-b", "third", "DROP TABLE restricted.secret")
    )
    assert result["io_counts"]["query"] == 1
    assert result["review"]["intent_preserved"] is None
    assert result["manual_review_required"] is True
    serialized = json.dumps(result, ensure_ascii=False)
    assert "restricted.secret" not in serialized and "合成关键答案" not in serialized


@pytest.mark.parametrize("format_", ["md", "html"])
async def test_upload_unknown_never_reports_complete_or_retries(postgres_url, monkeypatch, format_):
    from tests.sdk_core.dialogue_delivery import ChannelDelivery

    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "synthetic-p4-key")
    case = replace(dialogue_gate.AUTONOMY_SCENARIOS[0], name="p4_delivery_unknown")
    scripts = Scripts()
    scripts.add(
        case.messages[0][1],
        tool_call("run_readonly_query", cluster="fat", sql=TOP3_SQL),
        lambda call: answer(evidence_in(call), "合成关键答案", report_format=format_),
    )
    original = ChannelDelivery.upload
    attempted_files = []

    async def unknown_upload(self, report):
        attempted_files.append(report.content)
        await original(self, report)
        self.failure_simulated = True
        raise TimeoutError("synthetic upload outcome unknown")

    monkeypatch.setattr(ChannelDelivery, "upload", unknown_upload)
    reports = await dialogue_gate.run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(case,), repeats=1
    )
    result = reports[0]
    assert result["state"] == "completed"
    assert result["delivery"]["state"] == "unknown"
    assert result["delivery_failure_simulated"] is True
    assert result["delivery"]["complete_delivery_ms"] is None
    assert result["delivery"]["confirmed_parts"] == 0
    assert result["delivery"]["first_body_ack_ms"] is None
    assert result["delivery"]["file_count"] == len(attempted_files)
    assert len(attempted_files) == len(set(attempted_files))
    assert result["io_counts"]["query"] == 1
    assert len(result["requests"]) == 2


async def test_original_six_turns_reuse_history_and_deliver_only_requested_fields(
    postgres_url, monkeypatch
):
    from tests.sdk_core.dialogue_monitoring import METRICS, SOURCE, synthetic_monitoring_sources

    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "synthetic-p4-key")
    case = next(s for s in dialogue_gate.P4_SCENARIOS if s.name == "p4_original_dialogue")
    messages = case.messages
    scripts = Scripts()
    scripts.add(messages[0][1], lambda call: answer([], "已选择 fat。"))
    scripts.add(
        messages[1][1],
        tool_call("run_readonly_query", cluster="fat", sql=TOP3_SQL),
        lambda call: answer(evidence_in(call), "本轮查询三条。"),
    )
    scripts.add(messages[2][1], lambda call: answer([], "本轮实际查询三条，没有查 100 行。"))
    pairs = "192.0.2.11 — fat-be-1\n192.0.2.12 — fat-be-2\n192.0.2.13 — fat-fe-1"

    def hosts(call):
        evidence = evidence_in(call)[-1]
        return answer([evidence], pairs, fact_views=[{"evidence_id": evidence, "view": "brief"}])

    scripts.add(messages[3][1], tool_call(f"{SOURCE}__query", query="node_uname_info"), hosts)
    scripts.add(messages[4][1], hosts)
    scripts.add(messages[5][1], hosts)
    samples = []

    async def review(sample):
        samples.append(sample)
        return dialogue_gate.ReviewVerdict()

    with synthetic_monitoring_sources() as monitoring:
        reports = await dialogue_gate.run_dialogue(
            PROFILE,
            postgres_url,
            scripts.transport,
            scenarios=(case,),
            repeats=1,
            manual_review=review,
            monitoring=monitoring,
        )
    assert [r["state"] for r in reports] == ["completed"] * 6
    assert [row["queryId"] for row in samples[1].delivery.facts[0].rows] == [
        "slow-a",
        "slow-b",
        "third",
    ]
    assert reports[1]["io_counts"]["query"] == 1 and reports[1]["io_counts"].get("audit", 0) == 0
    assert reports[3]["mcp_fetches"] == {SOURCE: {"query": 1}}
    assert reports[3]["tool_choices_by_target"][SOURCE][f"{SOURCE}__query"] == 1
    assert reports[3]["environment_scope"]["starrocks_calls"] == 0
    for i in (3, 4, 5):
        captured = samples[i].channel_delivery
        assert pairs in captured.visible_text
        assert "node_uname_info" not in captured.visible_text
        assert "warnings: NULL" not in captured.visible_text
        assert reports[i]["delivery"]["state"] == "sent"
    raw_message = json.dumps(samples[3].channel_delivery.messages, ensure_ascii=False)
    assert "node_uname_info" in raw_message and "fat-be-1" in raw_message
    assert samples[3].delivery.facts[0].metadata["result"] == METRICS
    for i in (2, 4, 5):
        assert reports[i]["fetch_by_target"] == {} and reports[i]["mcp_fetches"] == {}
        assert reports[i]["delivery"]["file_count"] == 0
    assert "100 行" in samples[2].channel_delivery.visible_text
    assert all(r["review"]["intent_preserved"] is None for r in reports)
    encoded = json.dumps(reports, ensure_ascii=False)
    assert all(
        value not in encoded
        for value in ("192.0.2.11", "fat-be-1", "restricted.secret", "node_uname_info")
    )


async def test_audit_fixture_executes_per_user_aggregation(postgres_url, monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "synthetic-p4-key")
    base = dialogue_gate.AUTONOMY_SCENARIOS[0]
    case = replace(base, name="p4_user_stats", now=datetime(2026, 10, 20, 4, tzinfo=UTC))
    sql = (
        "SELECT `user`, COUNT(*) AS n FROM starrocks_audit_db__.starrocks_audit_tbl__ "
        "WHERE timestamp >= '2026-10-19 16:00:00' AND timestamp < '2026-10-20 16:00:00' "
        "GROUP BY `user` ORDER BY n DESC"
    )
    scripts = Scripts()
    scripts.add(
        case.messages[0][1],
        tool_call("run_readonly_query", cluster="fat", sql=sql),
        lambda call: answer(evidence_in(call)),
    )
    samples = []

    async def review(sample):
        samples.append(sample)
        return dialogue_gate.ReviewVerdict()

    reports = await dialogue_gate.run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(case,), repeats=1, manual_review=review
    )
    assert reports[0]["state"] == "completed"
    assert list(samples[0].delivery.facts[0].rows) == [
        {"user": "analyst", "n": 3},
        {"user": "batch", "n": 2},
    ]
    assert reports[0]["io_counts"]["query"] == 1


async def run_p4_case(postgres_url, monkeypatch, name, turns):
    from tests.sdk_core.dialogue_monitoring import synthetic_monitoring_sources

    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "synthetic-p4-key")
    case = next(s for s in dialogue_gate.P4_SCENARIOS if s.name == name)
    scripts = Scripts()
    for (_, message), steps in zip(case.messages, turns, strict=True):
        scripts.add(message, *steps)
    samples = []

    async def review(sample):
        samples.append(sample)
        return dialogue_gate.ReviewVerdict()

    with synthetic_monitoring_sources() as monitoring:
        reports = await dialogue_gate.run_dialogue(
            PROFILE,
            postgres_url,
            scripts.transport,
            scenarios=(case,),
            repeats=1,
            manual_review=review,
            monitoring=monitoring,
        )
    return samples, reports


@pytest.mark.parametrize("format_", ["md", "html"])
async def test_mixed_report_delivers_current_values_without_exporting_history(
    postgres_url, monkeypatch, format_
):
    from html import unescape

    from tests.sdk_core.dialogue_monitoring import METRICS, SOURCE

    def final(call):
        ids = evidence_in(call)
        return answer(
            ids,
            "上一轮三条；192.0.2.11 — fat-be-1；192.0.2.12 — fat-be-2；"
            "192.0.2.13 — fat-fe-1。原文见证据入口。",
            report_format=format_,
            fact_views=[{"evidence_id": e, "view": "brief"} for e in ids],
        )

    samples, reports = await run_p4_case(
        postgres_url,
        monkeypatch,
        f"p4_mixed_report_{format_}",
        [
            [
                tool_call("run_readonly_query", cluster="fat", sql=TOP3_SQL),
                lambda call: answer(evidence_in(call)),
            ],
            [tool_call(f"{SOURCE}__query", query="node_uname_info"), final],
        ],
    )
    assert all(r["state"] == "completed" for r in reports)
    facts = {f.tool_id: f for f in samples[1].delivery.facts}
    assert facts["local/run_readonly_query"].summary
    assert facts[f"{SOURCE}/query"].summary is None
    captured = samples[1].channel_delivery
    assert captured.state == "sent" and captured.files
    file_text = unescape("\n".join(f.content.decode() for f in captured.files))
    # Explicit files preserve the existing authorized-history export contract.
    assert "slow-a" in file_text and "restricted.secret" in file_text
    if format_ == "md":
        bodies = [
            f.content.decode()
            for f in captured.files
            if f.content.startswith("# 小维 · 报告正文".encode())
        ]
        assert bodies and any("slow-a" in text for text in bodies)
        assert len(captured.files) == 1  # v4 所选业务事实直接进入文件，不另造技术明细附件。
    assert METRICS in file_text or json.dumps(METRICS) in file_text
    assert "原文未展示" not in file_text
    assert "node_uname_info" not in captured.visible_text
    assert reports[1]["io_counts"].get("query", 0) == 0
    assert reports[1]["mcp_fetches"] == {SOURCE: {"query": 1}}
    assert reports[1]["manual_review_required"] is True


@pytest.mark.parametrize(
    ("case", "sql", "ids"),
    [
        (
            "p4_deduplicated_top3",
            "SELECT stmt, MAX(queryTime) AS ms FROM starrocks_audit_db__.starrocks_audit_tbl__ "
            "WHERE timestamp >= '2026-10-19 16:00:00' AND timestamp < '2026-10-20 16:00:00' "
            "GROUP BY stmt ORDER BY ms DESC LIMIT 3",
            ["SELECT hidden FROM restricted.secret", "DROP TABLE restricted.secret", "SELECT 1"],
        ),
        (
            "p4_timezone_override",
            "SELECT queryId, queryTime FROM starrocks_audit_db__.starrocks_audit_tbl__ "
            "WHERE timestamp >= '2026-10-19 04:00:00' AND timestamp < '2026-10-20 04:00:00' "
            "ORDER BY queryTime DESC, queryId ASC LIMIT 3",
            ["yesterday", "slow-a", "slow-b"],
        ),
    ],
)
async def test_additional_audit_tasks_use_database_results(
    postgres_url, monkeypatch, case, sql, ids
):
    samples, reports = await run_p4_case(
        postgres_url,
        monkeypatch,
        case,
        [
            [
                tool_call("run_readonly_query", cluster="fat", sql=sql),
                lambda call: answer(evidence_in(call)),
            ]
        ],
    )
    assert reports[0]["state"] == "completed"
    field = "stmt" if case == "p4_deduplicated_top3" else "queryId"
    assert [row[field] for row in samples[0].delivery.facts[0].rows] == ids
    assert reports[0]["io_counts"]["query"] == 1
    assert reports[0]["io_counts"].get("audit", 0) == 0
    assert reports[0]["mcp_fetches"] == {}


async def test_candidates_only_capability_is_visible_and_honestly_limited(
    postgres_url, monkeypatch
):
    def final(call):
        assert "run_readonly_query" not in call.tools
        return answer(evidence_in(call), "只能提供受限候选，精确 Top 3 请求未完成。")

    samples, reports = await run_p4_case(
        postgres_url,
        monkeypatch,
        "p4_candidates_only",
        [
            [
                tool_call(
                    "list_slow_queries",
                    cluster="fat",
                    window_minutes=60,
                    order_by="query_time",
                    database=None,
                ),
                final,
            ]
        ],
    )
    assert reports[0]["state"] == "completed"
    assert reports[0]["io_counts"].get("query", 0) == 0
    assert reports[0]["io_counts"]["audit"] == 1
    assert "精确 Top 3 请求未完成" in samples[0].channel_delivery.visible_text
    assert "获准候选" in samples[0].channel_delivery.visible_text
    assert reports[0]["manual_review_required"] is True


async def test_execution_time_audit_revocation_stops_without_candidate_substitution(
    postgres_url, monkeypatch
):
    samples, reports = await run_p4_case(
        postgres_url,
        monkeypatch,
        "p4_exact_denied",
        [
            [
                tool_call("run_readonly_query", cluster="fat", sql=TOP3_SQL),
                lambda call: pytest.fail("终止类失败后不得续轮"),
            ]
        ],
    )
    result = reports[0]
    assert result["state"] == "failed" and result["failure"]
    assert samples[0].delivery.facts == ()
    assert result["io_counts"].get("audit", 0) == 0
    assert result["mcp_fetches"] == {}
    assert len(result["requests"]) == 1
    assert (
        result["delivery"]["complete_delivery_ms"] is not None
    )  # 固定失败回执已送达，不是查询成功。
    assert result["delivery"]["file_count"] == 0


async def test_explicit_original_reuses_history_and_keeps_complete_raw_value(
    postgres_url, monkeypatch
):
    from tests.sdk_core.dialogue_monitoring import METRICS, SOURCE
    from tests.sdk_core.test_feishu_render import text_parts

    def hosts(call, view="brief"):
        ids = evidence_in(call)
        return answer(
            ids,
            "192.0.2.11 — fat-be-1；192.0.2.12 — fat-be-2；"
            "192.0.2.13 — fat-fe-1。原文见依据；未刷新。",
            fact_views=[{"evidence_id": e, "view": view} for e in ids],
        )

    samples, reports = await run_p4_case(
        postgres_url,
        monkeypatch,
        "p4_monitoring_original",
        [
            [tool_call(f"{SOURCE}__query", query="node_uname_info"), hosts],
            [lambda call: hosts(call, "full")],
        ],
    )
    assert all(r["state"] == "completed" for r in reports)
    original, historical = (sample.delivery.facts[0] for sample in samples)
    assert original.view == "brief" and historical.view == "full"
    assert historical.metadata["result"] == original.metadata["result"] == METRICS
    assert historical.captured_at == original.captured_at
    expanded = "\n".join(text_parts(samples[1].channel_delivery.messages))
    assert METRICS in expanded.replace('\\"', '"')
    assert (
        historical.note_summary
        and historical.note_summary in samples[1].channel_delivery.visible_text
    )
    assert reports[1]["mcp_fetches"] == {} and reports[1]["fetch_by_target"] == {}
    assert reports[1]["delivery"]["file_count"] == 0


@pytest.mark.browser
async def test_formal_web_host_only_followups_keep_raw_in_details_without_refetch(
    env, chrome_binary
):
    from tests.sdk_core.browser import launch
    from tests.sdk_core.dialogue_monitoring import SOURCE, synthetic_monitoring
    from tests.sdk_core.test_monitoring_r1a import monitoring_config
    from tests.sdk_core.test_runtime import statements
    from tests.sdk_core.test_web_browser import item, send, settled

    messages = (
        "只给 fat 三台主机的 IP 和主机名",
        "不要原始指标，只要这两项",
        "改成每台一行，不重新查询",
    )
    pairs = "192.0.2.11 — fat-be-1\n192.0.2.12 — fat-be-2\n192.0.2.13 — fat-fe-1"

    def hosts(call):
        ids = evidence_in(call)
        return answer(ids, pairs, fact_views=[{"evidence_id": e, "view": "brief"} for e in ids])

    env.scripts.add(messages[0], tool_call(f"{SOURCE}__query", query="node_uname_info"), hosts)
    for message in messages[1:]:
        env.scripts.add(message, hosts)
    with synthetic_monitoring() as monitoring:
        async with env.running(env.config(**monitoring_config(monitoring.url))):
            async with launch(chrome_binary) as chrome:
                page = await chrome.page()
                await page.navigate(env.origin)
                for index, message in enumerate(messages):
                    await send(page, message, "query")
                    await settled(page, index, "completed")
                    visible = await page.evaluate(f"{item(index)}.innerText")
                    assert all(
                        value in visible
                        for value in (
                            "192.0.2.11",
                            "192.0.2.12",
                            "192.0.2.13",
                            "fat-be-1",
                            "fat-be-2",
                            "fat-fe-1",
                        )
                    )
                    assert "node_uname_info" not in visible
                await page.evaluate(f"{item(0)}.querySelector('details').open = true")
                expanded = await page.evaluate(f"{item(0)}.innerText")
                assert "node_uname_info" in expanded and "192.0.2.13" in expanded
            assert monitoring.tool_calls == [("query", {"query": "node_uname_info"})]
            assert statements(env.drv) == []


async def test_busy_before_sql_can_reply_without_changing_query_constraints(
    postgres_url, monkeypatch
):
    def explain(call):
        outputs = [
            json.loads(i["output"]) for i in call.input if i.get("type") == "function_call_output"
        ]
        assert outputs[-1]["status"] == "rejected"
        return answer([], "资源繁忙，本次精确 Top 3 未完成；没有替换成候选。")

    samples, reports = await run_p4_case(
        postgres_url,
        monkeypatch,
        "p4_exact_busy",
        [[tool_call("run_readonly_query", cluster="fat", sql=TOP3_SQL), explain]],
    )
    assert reports[0]["state"] == "completed" and samples[0].delivery.facts == ()
    assert reports[0]["io_counts"].get("query", 0) == reports[0]["io_counts"].get("audit", 0) == 0
    assert "精确 Top 3 未完成" in samples[0].channel_delivery.visible_text
    assert len(reports[0]["requests"]) == 2


async def test_automatic_capacity_report_keeps_history_summary_only(postgres_url, monkeypatch):
    from functools import partial

    from tests.sdk_core.dialogue_monitoring import SOURCE

    from xiaowei.feishu_render import build_feishu_message

    # The public max_bytes argument controls rendering; the final sender checks the same bound.
    monkeypatch.setattr(
        "xiaowei.feishu_delivery.build_feishu_message",
        partial(build_feishu_message, max_bytes=2000),
    )
    monkeypatch.setattr("xiaowei.feishu_delivery.MAX_REQUEST_BYTES", 2000)

    def final(call):
        ids = evidence_in(call)
        return answer(
            ids,
            "192.0.2.11 — fat-be-1；192.0.2.12 — fat-be-2；192.0.2.13 — fat-fe-1。",
            fact_views=[{"evidence_id": e, "view": "brief"} for e in ids],
        )

    samples, reports = await run_p4_case(
        postgres_url,
        monkeypatch,
        "p4_mixed_auto",
        [
            [
                tool_call("run_readonly_query", cluster="fat", sql=TOP3_SQL),
                lambda call: answer(evidence_in(call)),
            ],
            [tool_call(f"{SOURCE}__query", query="node_uname_info"), final],
        ],
    )
    captured = samples[1].channel_delivery
    assert captured.state == "sent" and reports[1]["report_format"] is None, (
        [(r["state"], r["failure"], r.get("delivery")) for r in reports],
        captured.visible_text,
    )
    assert captured.files == [] and reports[1]["delivery"]["confirmed_parts"] == 1
    # v4 补充原值不单独触发附件；短业务回答及必要范围/容量说明可直接交付。
    visible = captured.visible_text
    assert "slow-a" not in visible and "restricted.secret" not in visible
    assert "本次返回行数：3" in visible
    assert "192.0.2.11" in visible and "fat-be-1" in visible
    assert reports[1]["io_counts"].get("query", 0) == 0
    assert reports[1]["mcp_fetches"] == {SOURCE: {"query": 1}}


@pytest.mark.browser
async def test_explicit_mixed_html_body_is_concise_but_details_keep_authorized_originals(
    postgres_url, monkeypatch, chrome_binary
):
    from urllib.parse import quote

    from tests.sdk_core.browser import launch
    from tests.sdk_core.dialogue_monitoring import SOURCE

    def final(call):
        ids = evidence_in(call)
        return answer(
            ids,
            "<h2>192.0.2.11 — fat-be-1；192.0.2.12 — fat-be-2；192.0.2.13 — fat-fe-1</h2>",
            fact_views=[{"evidence_id": e, "view": "brief"} for e in ids],
            report_format="html",
        )

    samples, reports = await run_p4_case(
        postgres_url,
        monkeypatch,
        "p4_mixed_report_html",
        [
            [
                tool_call("run_readonly_query", cluster="fat", sql=TOP3_SQL),
                lambda call: answer(evidence_in(call)),
            ],
            [tool_call(f"{SOURCE}__query", query="node_uname_info"), final],
        ],
    )
    assert reports[1]["delivery"]["state"] == "sent"
    (report,) = samples[1].channel_delivery.files
    async with launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate("data:text/html;charset=utf-8," + quote(report.content.decode()))
        visible = await page.evaluate("document.body.innerText")
        assert "192.0.2.11" in visible and "本次返回行数：3" in visible
        assert "slow-a" not in visible and "node_uname_info" not in visible
        await page.evaluate(
            "document.querySelectorAll('details').forEach(node => node.open = true)"
        )
        expanded = await page.evaluate("document.body.innerText")
        assert "slow-a" in expanded and "node_uname_info" in expanded and "192.0.2.13" in expanded


async def test_unsupported_fixture_expression_is_not_an_agent_quality_failure(
    postgres_url, monkeypatch
):
    from tests.sdk_core.dialogue_monitoring import SOURCE

    samples, reports = await run_p4_case(
        postgres_url,
        monkeypatch,
        "p4_monitoring_original",
        [
            [
                tool_call(f"{SOURCE}__query", query="sum(node_uname_info)"),
                lambda call: answer([], "合成夹具未支持这个表达式，真实指标未核实。"),
            ],
            [lambda call: answer([], "没有已核实的原值，不虚构指标。")],
        ],
    )
    assert reports[0]["fixture_unsupported_mcp_calls"] == 1
    assert reports[0]["state"] == "completed" and samples[0].delivery.facts == ()
    assert reports[0]["review"]["intent_preserved"] is None
    assert reports[0]["manual_review_required"] is True
    assert "sum(node_uname_info)" not in json.dumps(reports)


async def test_sqlite_execution_error_is_not_an_agent_quality_verdict(postgres_url, monkeypatch):
    sql = (
        "SELECT stmt, MAX(queryTime) AS ms FROM starrocks_audit_db__.starrocks_audit_tbl__ "
        "WHERE timestamp >= DATE_SUB('2026-10-20 16:00:00', INTERVAL 1 DAY) "
        "AND timestamp < '2026-10-20 16:00:00' GROUP BY stmt ORDER BY ms DESC LIMIT 3"
    )
    samples, reports = await run_p4_case(
        postgres_url,
        monkeypatch,
        "p4_deduplicated_top3",
        [
            [
                tool_call("run_readonly_query", cluster="fat", sql=sql),
                lambda call: answer([], "未核实目标数据库语义。"),
            ]
        ],
    )
    assert reports[0]["fixture_sql_errors_by_target"] == {"fat": 1}
    assert reports[0]["review"]["query_constraints_preserved"] is None
    assert reports[0]["manual_review_required"] is True
    assert samples[0].fixture_limits["sql_execution_errors"] == 1
    assert "DATE_SUB" not in json.dumps(reports)


@pytest.mark.parametrize("malformed_name", [[], {}])
def test_observer_leaves_malformed_provider_tool_names_for_sdk_validation(malformed_name):
    raw = json.dumps(
        {
            "output": [
                {"type": "function_call", "name": malformed_name, "arguments": '{"query":"up"}'}
            ]
        }
    ).encode()
    assert dialogue_gate.tool_choices(
        raw, frozenset({"fat"}), {"prometheus-prod__query": "prometheus-prod"}
    ) == [("other-tool", "other-target")]


@pytest.mark.parametrize("failed_first", [False, True])
async def test_p4_partial_monitoring_failure_preserves_other_source_without_fixed_order(
    postgres_url, monkeypatch, failed_first
):
    from tests.sdk_core.dialogue_monitoring import DENIED_SOURCE, SOURCE

    good = tool_call(f"{SOURCE}__query", query="up")
    bad = tool_call(f"{DENIED_SOURCE}__query", query="up")

    def final(call):
        outputs = [
            json.loads(i["output"]) for i in call.input if i.get("type") == "function_call_output"
        ]
        ids = [o["evidence_id"] for o in outputs if "evidence_id" in o]
        return answer(ids, "有不可用采样，拒绝源未核实；不能判断所有节点健康。")

    samples, reports = await run_p4_case(
        postgres_url,
        monkeypatch,
        "p4_partial_sources",
        [[bad, good, final] if failed_first else [good, bad, final]],
    )
    result = reports[0]
    assert result["state"] == "completed"
    assert {f.target_id for f in samples[0].delivery.facts} == {SOURCE}
    assert result["mcp_fetches"] == {SOURCE: {"query": 1}, DENIED_SOURCE: {"query": 1}}
    visible = samples[0].channel_delivery.visible_text
    assert DENIED_SOURCE in visible and "未核实" in visible
    assert result["fixture_unsupported_mcp_calls"] == 0
    assert result["review"]["intent_preserved"] is None
