"""S1–S5 离线入口：真 SDK/治理/Evidence/PostgreSQL，驱动和模型使用合成替身。"""

import json

import pytest
from tests.p1b.test_starrocks_adapter import SCHEMA_TABLES, schema_results
from tests.p1b.test_starrocks_audit import AUDIT, AUDIT_TARGET
from tests.p1b.test_starrocks_security_audit import END, SOURCE, START, collected, record
from tests.sdk_core.test_app import PROFILE, answer, cite, clarify, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_starrocks_tools import app_config, assembled, tool_outputs

from xiaowei.app import DataPolicy
from xiaowei.evidence import AnswerRejectedError
from xiaowei.feishu_render import build_feishu_message
from xiaowei.feishu_report import build_report
from xiaowei.governance import ToolRejectedError
from xiaowei.models import ToolRequest
from xiaowei.session import SessionInputPolicy
from xiaowei.starrocks_security import SECURITY_TOOL
from xiaowei.starrocks_tools import AUDIT_TOOLS, DIAGNOSE_TOOLS, QUERY_TOOLS

pytestmark = pytest.mark.loopback
TOOLS = QUERY_TOOLS | AUDIT_TOOLS | {SECURITY_TOOL}
ARGS = {
    "cluster": AUDIT_TARGET.target_id, "start_time": START.isoformat(), "end_time": END.isoformat(),
}


def security_driver(*rows, totals=(12, 2, 0), missing=()):
    columns = tuple(
        (name, "bigint" if name in {"queryTime", "pendingTimeMs"} else "varchar", "YES", None)
        for name in SOURCE if name not in {*missing, "stmt_present"}
    )
    extra = schema_results({
        **SCHEMA_TABLES, (AUDIT.database, AUDIT.table): columns,
    })
    drv = collected(*rows, totals=totals)
    make = drv.make

    def connection():
        conn = make()
        conn.results.update(extra)
        return conn

    drv.make = connection
    return drv


def audit_reads(drv):
    """仅审计聚合/候选；当前权限和依赖复核的 I/O 不属于重新取数。"""
    return [(sql, args) for c in drv.connections for sql, args in c.executed
            if "AS window_rows" in sql or "stmt_present" in sql]


def enabled(env):
    env.grants.grant("alice", SECURITY_TOOL, target=AUDIT_TARGET.target_id)
    env.app = env.application(app_config(
        purposes={"query": TOOLS, "diagnose": DIAGNOSE_TOOLS | AUDIT_TOOLS},
        data_policies={PROFILE.data_policy_id: DataPolicy(
            input=SessionInputPolicy(max_bytes=2000), model_tools=TOOLS,
        )},
    ))


def security_channel(env):
    """复用本地 StarRocks 装配，入口与 Evidence 使用同一授权来源。"""
    from pydantic import SecretStr
    from tests.sdk_core.test_channel_service import Access

    from xiaowei.channel import AccessDecision, ChannelService, ResultDelivery
    from xiaowei.channel_store import ChannelStore
    from xiaowei.evidence import EvidenceStore
    from xiaowei.governance import GovernedTools
    from xiaowei.starrocks_schema import DependencyCheck
    from xiaowei.storage import Readiness

    class AuditAccess(Access):
        async def resolve(self, channel, subject_id):
            tools = frozenset(t for s, target, t in self.grants.allowed
                              if s == subject_id and target == AUDIT_TARGET.target_id)
            return AccessDecision(
                subject_id=subject_id, target_ids=frozenset({AUDIT_TARGET.target_id}),
                authorized_tools=tools, policy_version="p1",
            ) if tools else None

    access = AuditAccess(env.grants)
    evidence = EvidenceStore(
        env.engine, env.evidence.catalog, authorize=access.authorize, clock=env.clock,
        retention_seconds=3600,
        verify_dependencies=DependencyCheck({AUDIT_TARGET.target_id: env.adapter}),
    )
    env.governed = GovernedTools(evidence)
    enabled(env)
    store = ChannelStore(
        env.engine, key=SecretStr("synthetic-security-channel-key"), clock=env.clock,
        readiness=Readiness(),
        request_retention_seconds=3600, session_retention_seconds=7200,
        evidence_retention_seconds=3600, max_answer_bytes=4000,
    )
    return ChannelService(env.app, ResultDelivery(
        store, evidence, access, budget=env.ctx(authorized=TOOLS).budget,
    )), store


@pytest.mark.parametrize("cite_fact", [True, False])
async def test_s1_formal_sdk_path_keeps_totals_samples_dependencies_and_no_raw_text(
    postgres_url,
    monkeypatch,
    cite_fact,
):
    drv = security_driver(
        record("SELECT 0 OR 1=1", queryId="q0"),
        record("SELECT SLEEP(5)", queryId="q1", state="ERR"),
        record("GRANT SELECT ON shop.t TO u /* security-raw-canary */", queryId="q2"),
    )
    async with assembled(postgres_url, monkeypatch, drv, AUDIT_TARGET) as env:
        enabled(env)
        finish = (
            cite("发现需核查的安全线索，尚不能确认攻击成功")
            if cite_fact
            else lambda call: answer([], "已检查，请核对系统事实")
        )
        steps = [tool_call("inspect_audit_security", **ARGS), finish]
        if not cite_fact:

            def repaired(call):
                assert call.tools == []
                return cite("补充本轮已取得的安全线索，尚不能确认攻击成功")(call)

            steps.append(repaired)
        message = env.scripts.add("检查测试环境本窗数据库安全情况", *steps)
        delivery = await env.deliver(env.ctx(authorized=TOOLS), message)
        assert delivery.facts, tool_outputs(env.scripts.calls[message][1])
        (fact,) = delivery.facts
        data = json.loads(fact.result_json)
        assert (data["scanned_rows"], data["matched_rows"], data["failed_rows"]) == (12, 2, 2)
        assert data["coverage"]["checked_candidate_rows"] == 3
        assert data["coverage"]["match_counts_complete"] and not fact.truncated
        assert fact.columns and len(fact.rows) == 2  # 正式 Web 事实表可读。
        assert {r["query_id"] for r in fact.rows} == {"q0", "q1"}
        assert "security-raw-canary" not in json.dumps(env.scripts.calls[message], default=str)
        assert "security-raw-canary" not in fact.result_json
        assert "stmt" not in data and "sql" not in data
        assert await env.evidence_rows() == 1
        assert len(env.scripts.calls[message]) == (2 if cite_fact else 3)
        assert sum("AS window_rows" in sql for c in drv.connections for sql, _ in c.executed) == 1


@pytest.mark.parametrize("case", ["wrong_window", "missing_column", "denied"])
async def test_s5_rejection_has_no_collection_io_or_success_evidence(
    postgres_url, monkeypatch, case,
):
    drv = security_driver(record(), missing=("stmt",) if case == "missing_column" else ())
    async with assembled(postgres_url, monkeypatch, drv, AUDIT_TARGET) as env:
        enabled(env)
        args = {**ARGS, **({"end_time": START.isoformat()} if case == "wrong_window" else {})}
        if case == "denied":
            env.grants.allowed.discard(("alice", AUDIT_TARGET.target_id, SECURITY_TOOL))
        message = env.scripts.add(
            "检查窗口", tool_call("inspect_audit_security", **args), clarify(),
        )
        result = await env.app.run_turn(env.ctx(authorized=TOOLS), message)
        assert result.answer.awaiting_user
        assert drv.attempts == 0 and await env.evidence_rows() == 0
        output = " ".join(tool_outputs(env.scripts.calls[message][1]))
        assert ("incomplete" if case == "missing_column" else "rejected") in output


async def test_explicit_diagnose_entry_rejects_security_before_collection(
    postgres_url, monkeypatch,
):
    drv = security_driver(record())
    async with assembled(postgres_url, monkeypatch, drv, AUDIT_TARGET) as env:
        enabled(env)
        ctx = env.ctx(mode="diagnose", authorized=TOOLS)
        assert SECURITY_TOOL not in ctx.tool_scope
        with pytest.raises(ToolRejectedError):
            await env.governed.invoke(ctx, ToolRequest(
                tool_id=SECURITY_TOOL, target_id=AUDIT_TARGET.target_id, call_id="denied",
                tool_name="inspect_audit_security", arguments=ARGS,
            ), env.executes[(SECURITY_TOOL, AUDIT_TARGET.target_id)])
        assert drv.attempts == 0 and await env.evidence_rows() == 0


async def test_security_evidence_is_blocked_after_revocation_without_recollection(
    postgres_url, monkeypatch,
):
    drv = security_driver(record())
    async with assembled(postgres_url, monkeypatch, drv, AUDIT_TARGET) as env:
        enabled(env)
        ctx = env.ctx(authorized=TOOLS)
        message = env.scripts.add("巡检后撤权", tool_call("inspect_audit_security", **ARGS), cite())
        turn = await env.app.run_turn(ctx, message)
        attempts = drv.attempts
        env.grants.allowed.discard(("alice", AUDIT_TARGET.target_id, SECURITY_TOOL))
        with pytest.raises(AnswerRejectedError):
            await env.evidence.validate_answer(turn, ctx)
        assert drv.attempts == attempts


@pytest.mark.parametrize(("case", "statements", "matched", "signal_counts"), [
    ("S2", ["SELECT IF(1=1,SLEEP(5),0)", "SELECT IF(2=2,SLEEP(5),0)",
            "SELECT IF(3=3,SLEEP(5),0)"], 3, {"delay_call": 3}),
    ("S3", ["SELECT 'sleep(5); DROP TABLE t; OR 1=1'", "SELECT 1 /* OR 1=1 */",
            "SELECT 1 UNION SELECT 2", "TRUNCATE TABLE t"], 0, {}),
    ("S4", ["GRANT SELECT ON t TO u", "DROP TABLE t", "TRUNCATE TABLE t"],
     0, {}),
])
async def test_s2_s4_business_facts_use_the_same_sdk_and_evidence_path(
    postgres_url, monkeypatch, case, statements, matched, signal_counts,
):
    rows = [record(sql, queryId=f"q{i}", state="ERR" if i == 1 and case == "S4" else "OK")
            for i, sql in enumerate(statements)]
    drv = security_driver(*rows, totals=(len(rows), int(case == "S4"), 0))
    async with assembled(postgres_url, monkeypatch, drv, AUDIT_TARGET) as env:
        enabled(env)
        message = env.scripts.add(
            f"{case} 检查本窗线索，维护已获批", tool_call("inspect_audit_security", **ARGS), cite(),
        )
        delivery = await env.deliver(env.ctx(authorized=TOOLS), message)
        data = json.loads(delivery.facts[0].result_json)
        from xiaowei.feishu_render import business_presentation

        shown = business_presentation(delivery.facts[0])
        assert data["matched_rows"] == matched
        assert {k: v for k, v in data["signal_counts"].items() if v} == signal_counts
        if case == "S4":
            assert data["operation_counts"] == {} and data["rows"] == []
            assert data["failed_rows"] == 1
        if case == "S2":
            assert all(row["requested_delay_seconds"] == 5 for row in data["rows"])
            assert all(row["请求延迟（s）"] == 5 and row["排队时间（ms）"] == 0
                       and row["实际耗时（ms）"] == 5 for row in shown.rows)


async def test_security_facts_render_with_existing_report_and_feishu_components(
    postgres_url, monkeypatch,
):
    drv = security_driver(record(queryId="q-security-report"))
    async with assembled(postgres_url, monkeypatch, drv, AUDIT_TARGET) as env:
        enabled(env)
        message = env.scripts.add(
            "巡检事实交付", tool_call("inspect_audit_security", **ARGS), cite("需核对变更背景"),
        )
        delivery = await env.deliver(env.ctx(authorized=TOOLS, channel="feishu"), message)
        rendered = message_text(build_feishu_message(
            delivery, 3500, "oc_security", platform_limits=True,
        ))
        assert "q-security-report" in rendered and "scanned_rows" in rendered
        for format_ in ("md", "html"):
            report = build_report(delivery.model_copy(update={"report_format": format_}))
            body = report.content.decode()
            assert "全窗聚合（读取时）12" in body and "时间延迟调用线索" in body
            assert "无法判定候选 0" in body
            assert "代表样本" in body and "需要核查 · 代表样本" not in body
            assert "q-security-report" not in body and "scanned_rows" not in body


@pytest.mark.parametrize("case", [1, 2, 3, 4, 5])
async def test_real_model_security_suite_uses_the_formal_runtime_with_synthetic_io(
    postgres_url, monkeypatch, case,
):
    from tests.sdk_core.dialogue_gate import SECURITY_SCENARIOS, ReviewVerdict, run_dialogue
    from tests.sdk_core.test_app import Scripts

    scenario = SECURITY_SCENARIOS[case - 1]
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "synthetic-security-profile")
    scripts = Scripts()
    scripts.add(scenario.messages[0][1], tool_call(
        "inspect_audit_security", cluster="pre", start_time="2026-10-22T10:00:00+00:00",
        end_time="2026-10-22T11:00:00+00:00",
    ), cite())
    reviewed = []

    async def review(sample):
        assert sample.delivery is not None
        data = json.loads(sample.delivery.facts[0].result_json)
        expected = ((12, 2, 2), (4, 3, 1), (5, 0, 0), (3, 0, 1), (206, 200, 0))[case - 1]
        assert (data["scanned_rows"], data["matched_rows"], data["failed_rows"]) == expected
        if case == 5:
            assert data["coverage"]["scan_limited"]
            assert not data["coverage"]["match_counts_complete"]
        reviewed.append(sample)
        return ReviewVerdict(evidence_consistent=True)

    results = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(scenario,), repeats=1,
        manual_review=review,
    )
    assert len(results) == len(reviewed) == 1


async def test_global_oversize_rows_do_not_create_a_truncated_fact(postgres_url, monkeypatch):
    drv = security_driver(record(), totals=(237398, 3, 135))
    async with assembled(postgres_url, monkeypatch, drv, AUDIT_TARGET) as env:
        enabled(env)
        message = env.scripts.add("统计覆盖", tool_call("inspect_audit_security", **ARGS), cite())
        delivery = await env.deliver(env.ctx(authorized=TOOLS), message)
        (fact,) = delivery.facts
        data = json.loads(fact.result_json)
        assert data["coverage"]["window_oversize_rows"] == 135
        assert data["coverage"]["unclassified_candidate_rows"] == 0
        assert data["coverage"]["match_counts_complete"] and not fact.truncated


@pytest.mark.parametrize("incomplete", [False, True], ids=["R1-complete", "R4-lower-bound"])
async def test_report_presentation_through_formal_sdk_and_channel_projection(
    postgres_url, monkeypatch, incomplete,
):
    from xiaowei.channel import RequestView
    from xiaowei.storage import Readiness
    from xiaowei.web import _view

    rows = [record("SHOW VARIABLES; SELECT 1 /* raw-report-canary */", queryId=f"q-report-{i}")
            for i in range(11 if incomplete else 1)]
    drv = security_driver(*rows, totals=(29232, 6, 3))
    async with assembled(postgres_url, monkeypatch, drv, AUDIT_TARGET) as env:
        enabled(env)
        message = env.scripts.add("本窗报告", tool_call("inspect_audit_security", **ARGS), cite())
        delivery = await env.deliver(env.ctx(authorized=TOOLS), message)
        response = _view("security-report", RequestView(state="completed", delivery=delivery),
                         Readiness())
        shown = json.loads(response.body)["delivery"]["facts"][0]["presentation"]
        assert "29,232" in shown["coverage"] and "无法判定候选 0" in shown["coverage"]
        assert ("分类计数不完整" in shown["coverage"]) is incomplete
        assert len(shown["checks"]) == 4
        assert shown["checks"][-1]["审计行数"] == "6"
        assert ("已确认至少" in shown["checks"][2]["审计行数"]) is incomplete
        assert "raw-report-canary" not in response.body.decode()
        assert len(env.scripts.calls[message]) == 2 and await env.evidence_rows() == 1
        assert sum("AS window_rows" in sql for c in drv.connections for sql, _ in c.executed) == 1


async def test_sdk_can_correct_window_after_precheck_without_a_custom_retry_loop(
    postgres_url, monkeypatch,
):
    drv = security_driver(record())
    async with assembled(postgres_url, monkeypatch, drv, AUDIT_TARGET) as env:
        enabled(env)
        message = env.scripts.add(
            "修正错误窗口",
            tool_call("inspect_audit_security", **{**ARGS, "end_time": ARGS["start_time"]}),
            tool_call("inspect_audit_security", **ARGS), cite(),
        )
        delivery = await env.deliver(env.ctx(authorized=TOOLS, max_tool_calls=1), message)
        assert delivery.facts
        assert sum("AS window_rows" in sql for c in drv.connections for sql, _ in c.executed) == 1


async def test_sample_capacity_uses_actual_scalar_cells_not_internal_lists(
    postgres_url, monkeypatch,
):
    drv = security_driver(record("SELECT SLEEP(1) OR 1=1; DROP TABLE t"))
    target = AUDIT_TARGET.model_copy(update={"max_value_bytes": 70})
    async with assembled(postgres_url, monkeypatch, drv, target) as env:
        enabled(env)
        message = env.scripts.add("完整线索", tool_call("inspect_audit_security", **ARGS), cite())
        delivery = await env.deliver(env.ctx(authorized=TOOLS), message)
        (fact,) = delivery.facts
        assert len(fact.rows) == 1 and not fact.truncated
        assert all(len(json.dumps(v).encode()) <= 70 for v in fact.rows[0].values())


@pytest.mark.parametrize("max_result_bytes", [20_000, 1200])
async def test_sample_limits_preserve_late_attack_representatives(
    postgres_url, monkeypatch, max_result_bytes,
):
    target = type(AUDIT_TARGET).model_validate({
        **AUDIT_TARGET.model_dump(), "max_value_bytes": 500,
        "max_result_bytes": max_result_bytes,
        "audit": {**AUDIT.model_dump(), "max_rows": 20, "candidate_rows": 200},
    })
    rows = [record("SHOW VARIABLES; SELECT 1", queryId=f"q{i}") for i in range(25)]
    rows.append(record("SELECT 0 OR 1=1", queryId="q25"))
    drv = security_driver(*rows, totals=(26, 0, 0))
    async with assembled(postgres_url, monkeypatch, drv, target) as env:
        enabled(env)
        message = env.scripts.add("检查注入及辅助多语句", tool_call(
            "inspect_audit_security", **ARGS,
        ), cite())
        delivery = await env.deliver(env.ctx(authorized=TOOLS), message)
        (fact,) = delivery.facts
        data = json.loads(fact.result_json)
        assert data["matched_rows"] == 26 and data["signal_counts"]["boolean_probe"] == 1
        assert data["signal_counts"]["stacked_statements"] == 25
        assert data["operation_counts"] == {}
        assert {row["signals"] for row in data["rows"]} == {"boolean_probe", "stacked_statements"}
        assert "q25" in {row["query_id"] for row in data["rows"]}
        assert data["coverage"]["omitted_samples"] == 26 - len(data["rows"])
        assert data["coverage"]["match_counts_complete"] and fact.truncated
        assert len(json.dumps(data["rows"], ensure_ascii=False).encode()) <= max_result_bytes


async def test_read_oversize_delay_candidate_is_unclassified_not_a_clean_window(
    postgres_url, monkeypatch,
):
    drv = security_driver(
        record(None, stmt_present=1), record("SELECT SLEEP(1)"), totals=(100, 0, 1),
    )
    async with assembled(postgres_url, monkeypatch, drv, AUDIT_TARGET) as env:
        enabled(env)
        message = env.scripts.add(
            "检查含超长候选的窗口", tool_call("inspect_audit_security", **ARGS), cite(),
        )
        delivery = await env.deliver(env.ctx(authorized=TOOLS), message)
        data = json.loads(delivery.facts[0].result_json)
        assert data["signal_counts"]["delay_call"] == 1
        assert data["coverage"]["window_oversize_rows"] == 1
        assert data["coverage"]["unclassified_candidate_rows"] == 1
        assert not data["coverage"]["match_counts_complete"] and delivery.facts[0].truncated


async def test_saved_v1_evidence_remains_readable_with_current_catalog(postgres_url, monkeypatch):
    from tests.sdk_core.test_presentation_v4 import security_fact

    from xiaowei.evidence import EvidenceStore
    from xiaowei.models import ToolObservation
    from xiaowei.starrocks_schema import DependencyCheck

    async with assembled(postgres_url, monkeypatch, security_driver(), AUDIT_TARGET) as env:
        enabled(env)
        ctx = env.ctx(authorized=TOOLS)
        request = ToolRequest(
            tool_id=SECURITY_TOOL, target_id=AUDIT_TARGET.target_id,
            call_id="old-inspection", tool_name="inspect_audit_security", arguments=ARGS,
        )
        checked = env.executes[(SECURITY_TOOL, AUDIT_TARGET.target_id)].check(request)
        payload = json.loads(security_fact().result_json)
        payload["source"] = f"{AUDIT.database}.{AUDIT.table}"
        payload["window"] = {"start_time": ARGS["start_time"], "end_time": ARGS["end_time"],
                             "time_zone": AUDIT.time_zone}
        payload["rows"][0]["timestamp"] = ARGS["start_time"]
        saved = await env.evidence.record(ctx, request, ToolObservation(
            payload=payload, captured_at=env.clock(), truncated=False,
            dependencies=(checked.dependency,),
        ))
        reopened = EvidenceStore(
            env.engine, env.evidence.catalog, authorize=env.grants,
            clock=env.clock, retention_seconds=3600,
            verify_dependencies=DependencyCheck({AUDIT_TARGET.target_id: env.adapter}),
        )
        loaded = await reopened.inspect(saved.evidence_id, ctx)
        assert json.loads(loaded.projections["model"])["data"] == payload
        assert payload["rules_version"] == "manual-security-v1"
        assert "high_risk_operation" in payload["signal_counts"]
        assert "operations" in payload["rows"][0]
        assert not audit_reads(env.drv)
