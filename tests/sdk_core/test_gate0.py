"""Gate 0 离线部分：固定样例经产品路径跑通，并观测供应商签名在本轮与回放历史中的去留。

模型端点是按 Gemini 3 的 OpenAI 兼容协议形状编写的 HTTP mock：工具调用携带
``extra_content.google.thought_signature``，并要求本轮（最后一条用户消息之后）的工具调用原样
带回签名，否则返回 400。它只证明本应用与锁定 SDK 在这种协议形状下的行为；真实供应商是否接受
回放历史中不带签名的工具调用，只能由 ``scripts/gate0_real_model.py`` 对获准 Profile 实测。
"""

import itertools
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
import scripts.gate0_real_model as gate0_command
from sqlalchemy.engine import URL
from tests.sdk_core import gate0
from tests.sdk_core.gate0 import (
    SALES_TOTAL,
    SAMPLES,
    Gate0,
    RequestObservation,
    SampleResult,
    gate0_app,
    gate_passed,
    run_samples,
)
from tests.sdk_core.synthetic_tools import Clock, ready_engine
from tests.sdk_core.test_model_api import GEMINI

from xiaowei.model_api import ModelProfile, profile_fingerprint
from xiaowei.sqlguard import guard_explain_query, guard_readonly_query
from xiaowei.starrocks import StarRocksAdapter
from xiaowei.starrocks_tools import EXPLAIN_QUERY, LAYOUT_TOOL, RUN_QUERY, SLOW_QUERIES

pytestmark = pytest.mark.loopback

PROFILE: ModelProfile = GEMINI.model_copy(update={"model": "gemini-3-flash-mock"})
FAKE_KEY = "fake-gate0-gemini-key"
_signatures = itertools.count(1)

Reply = Callable[[list[dict[str, Any]]], dict[str, Any]]


def _chat(message: dict[str, Any], finish: str) -> dict[str, Any]:
    return {
        "id": "chat-gate0",
        "object": "chat.completion",
        "created": 0,
        "model": PROFILE.model,
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def _envelopes(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """模型实际收到的工具结果信封（本轮与回放历史）。"""
    return [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]


def _evidence(messages: list[dict[str, Any]]) -> list[str]:
    return [envelope["evidence_id"] for envelope in _envelopes(messages)]


def call_tool(name: str, **arguments: object) -> Reply:
    def reply(messages: list[dict[str, Any]]) -> dict[str, Any]:
        call = {
            "id": f"call-{next(_signatures)}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
            "extra_content": {"google": {"thought_signature": f"sig-{next(_signatures)}"}},
        }
        return _chat({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")

    return reply


def cite_all(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """只依据模型实际收到的工具结果作答；看不到总额时只能澄清。"""
    totals = [e["data"]["total"] for e in _envelopes(messages) if "total" in e["data"]]
    if not totals:
        return clarify(messages)
    ids = _evidence(messages)
    body = {
        "evidence_ids": ids,
        "inferences": [{"text": f"总额为 {totals[-1]}", "evidence_ids": ids}],
        "clarification": None,
    }
    return _chat({"role": "assistant", "content": json.dumps(body, ensure_ascii=False)}, "stop")


def clarify(messages: list[dict[str, Any]]) -> dict[str, Any]:
    body = {"evidence_ids": [], "inferences": [], "clarification": "请说明指哪个数字"}
    return _chat({"role": "assistant", "content": json.dumps(body, ensure_ascii=False)}, "stop")


@dataclass
class GeminiLikeEndpoint:
    """按最后一条用户消息分派脚本；校验本轮工具调用的签名。

    ``strict_history`` 另要求回放历史中的工具调用也带签名。
    """

    replies: dict[str, list[Reply]]
    strict_history: bool = False
    extra_usage: dict[str, Any] = field(default_factory=dict)
    issued: dict[str, str] = field(default_factory=dict)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        messages: list[dict[str, Any]] = json.loads(request.content)["messages"]
        last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        for index, message in enumerate(messages):
            if index < last_user and not self.strict_history:
                continue
            for call in message.get("tool_calls") or []:
                google = (call.get("extra_content") or {}).get("google") or {}
                if google.get("thought_signature") != self.issued.get(call["id"]):
                    error = {"code": 400, "message": "Function call is missing a thought_signature"}
                    return httpx2.Response(400, json=[{"error": error}])
        text = messages[last_user]["content"]
        if not isinstance(text, str):
            text = "".join(part["text"] for part in text)
        body = self.replies[text].pop(0)(messages)
        body["usage"] = {**body["usage"], **self.extra_usage}
        for call in body["choices"][0]["message"].get("tool_calls") or []:
            self.issued[call["id"]] = call["extra_content"]["google"]["thought_signature"]
        return httpx2.Response(200, json=body)


def scripted(strict_history: bool = False, **extra: Any) -> GeminiLikeEndpoint:
    query, followup, diagnose, vague = (s.message for s in SAMPLES)
    return GeminiLikeEndpoint(
        replies={
            query: [call_tool("sales_total", region="east"), cite_all],
            followup: [cite_all],
            # 诊断轮不展示查询工具；模型仍强行调用时由 SDK 拒绝，查询零执行。
            diagnose: [call_tool("sales_total", region="east")],
            vague: [clarify],
        },
        strict_history=strict_history,
        **extra,
    )


@pytest.fixture
async def engine_url(postgres_url: URL, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[URL]:
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), FAKE_KEY)
    yield postgres_url


async def run(url: URL, endpoint: GeminiLikeEndpoint) -> tuple[Gate0, list[SampleResult]]:
    async with (
        ready_engine(url) as engine,
        gate0_app(PROFILE, engine, network=endpoint.transport(), clock=Clock()) as gate,
    ):
        return gate, await run_samples(gate)


async def test_fixed_samples_pass_through_the_product_path(engine_url: URL) -> None:
    gate, results = await run(engine_url, scripted())
    by_name = {r.name: r for r in results}

    assert gate_passed(results), [r.report() for r in results]
    assert all(r.passed for r in results)
    # 本轮续调用：SDK 把工具调用的签名原样带回（否则端点返回 400，本轮失败）。
    query = by_name["query"]
    assert [r.turn_signatures for r in query.requests] == [0, 1]
    assert [r.status for r in query.requests] == [200, 200]
    assert query.requests[0].usage == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }
    # 追问：回放了上一轮的工具调用，但当前 Session 保存形式不含供应商签名。
    followup = by_name["followup"].requests[0]
    assert (followup.history_tool_calls, followup.history_signatures) == (1, 0)
    # 诊断轮：展示了元数据工具、没有查询工具；强行调用使本轮失败，查询零执行。
    diagnose = by_name["diagnose_hides_query"]
    assert diagnose.requests[0].tools_offered == ("list_regions",)
    assert (diagnose.outcome, diagnose.reason) == ("failed", "model_failed")
    assert gate.adapter.calls and all(c.arguments == {"region": "east"} for c in gate.adapter.calls)


async def test_report_carries_no_content(engine_url: URL) -> None:
    _, results = await run(engine_url, scripted())
    report = json.dumps([r.report() for r in results], ensure_ascii=False)

    for sample in SAMPLES:
        assert sample.message not in report
    assert "ev_" not in report
    assert str(SALES_TOTAL) not in report
    assert "sig-" not in report
    assert FAKE_KEY not in report


async def test_history_signature_requirement_would_break_followup(engine_url: URL) -> None:
    """若供应商也校验回放历史中的签名，追问在首个模型请求即失败，且不重跑工具。

    这是 Gate 0 真实运行要判定的问题：失败时按计划先做 Session 保存形式的前置修复。
    """
    _, results = await run(engine_url, scripted(strict_history=True))
    by_name = {r.name: r for r in results}

    assert by_name["query"].passed
    followup = by_name["followup"]
    assert (followup.outcome, followup.reason) == ("failed", "model_failed")
    assert [r.status for r in followup.requests] == [400]
    assert followup.sales_calls == 0
    assert not gate_passed(results)


# ---- 真实模型命令（离线部分） ----------------------------------------------------------


async def test_command_runs_the_same_samples_offline(engine_url: URL) -> None:
    """命令自己的装配（独立测试库、报告格式）用同一个 mock 端点跑通；真实端点只换 transport。"""
    report = await gate0_command.run(PROFILE, network=scripted().transport())

    assert report["passed"] is True
    assert report["profile"] == {
        "profile_id": PROFILE.profile_id,
        "provider": "gemini",
        "host": "gemini.test",
        "api_mode": "chat_completions",
        "model": PROFILE.model,
        "output_mode": PROFILE.output_mode,
        "fingerprint": profile_fingerprint(PROFILE),
    }
    samples = report["samples"]
    assert isinstance(samples, list)
    assert [s["sample"] for s in samples] == [s.name for s in SAMPLES]
    assert FAKE_KEY not in json.dumps(report, ensure_ascii=False)


def test_command_fails_without_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = tmp_path / "profile.json"
    key_env = PROFILE.api_key_ref.removeprefix("env:")

    # Profile 文件缺失或不合法。
    assert gate0_command.main(["--profile", str(profile)]) == 2
    profile.write_text('{"model": "x"}', encoding="utf-8")
    assert gate0_command.main(["--profile", str(profile)]) == 2
    # 凭据引用指向的环境变量未设置。
    profile.write_text(PROFILE.model_dump_json(), encoding="utf-8")
    monkeypatch.delenv(key_env, raising=False)
    assert gate0_command.main(["--profile", str(profile)]) == 2
    # 测试 PostgreSQL 未设置或不是唯一声明的隔离实例。
    monkeypatch.setenv(key_env, FAKE_KEY)
    monkeypatch.delenv(gate0_command.POSTGRES_URL_ENV, raising=False)
    assert gate0_command.main(["--profile", str(profile)]) == 2
    monkeypatch.setenv(gate0_command.POSTGRES_URL_ENV, "postgresql+asyncpg://other:5432/db")
    assert gate0_command.main(["--profile", str(profile)]) == 2

    errors = capsys.readouterr().err
    assert errors.count("gate0:") == 5
    assert FAKE_KEY not in errors and "other:5432" not in errors


def test_command_forces_sdk_log_redaction_before_import() -> None:
    """外部预置 0/false 时，命令模块仍在 SDK 导入前强制打开两项日志隐去开关。"""
    env = {
        **os.environ,
        "OPENAI_AGENTS_DONT_LOG_MODEL_DATA": "0",
        "OPENAI_AGENTS_DONT_LOG_TOOL_DATA": "false",
    }
    probe = (
        "import scripts.gate0_real_model, agents._debug as d;"
        "print(d.DONT_LOG_MODEL_DATA, d.DONT_LOG_TOOL_DATA)"
    )
    out = subprocess.run(  # noqa: S603 - 固定参数：当前解释器与本文件内的探针
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.split() == ["True", "True"]


# ---- 判定不能在关键证据缺失时通过（独立审查反例） ----------------------------------------


def _observed(
    tools: tuple[str, ...] = ("list_regions",), fields: tuple[str, ...] = (), history: int = 0
) -> RequestObservation:
    return RequestObservation(
        status=200,
        elapsed_ms=1,
        tools_offered=tools,
        history_tool_calls=history,
        history_signatures=0,
        turn_tool_calls=0,
        turn_signatures=0,
        tool_result_fields=fields,
        usage=None,
    )


def _result(name: str, **values: Any) -> SampleResult:
    sample = next(s for s in SAMPLES if s.name == name)
    defaults: dict[str, Any] = {
        "name": name,
        "mode": sample.mode,
        "outcome": "delivered",
        "reason": None,
        "evidence_ids": (),
        "sales_calls": 0,
        "regions_calls": 0,
        "elapsed_ms": 1,
        "requests": [],
        "gate": sample.gate,
    }
    return SampleResult(**{**defaults, **values})


SEEN = ("orders", "region", "rows", "total")


def _query(fields: tuple[str, ...] = SEEN) -> SampleResult:
    return _result(
        "query",
        evidence_ids=("ev_a",),
        sales_calls=1,
        requests=[_observed(("list_regions", "sales_total")), _observed(fields=fields)],
    )


def _passing() -> list[SampleResult]:
    return [
        _query(),
        _result("followup", evidence_ids=("ev_a",), requests=[_observed(fields=SEEN, history=1)]),
        _result("diagnose_hides_query", outcome="clarification", requests=[_observed()]),
        _result("ambiguous", outcome="clarification", requests=[_observed()]),
    ]


def test_judged_results_pass_only_with_complete_evidence() -> None:
    results = _passing()
    gate0.judge(results)
    assert gate_passed(results)  # 对照


def _replace(index: int, result: SampleResult) -> Callable[[list[SampleResult]], None]:
    def mutate(results: list[SampleResult]) -> None:
        results[index] = result

    return mutate


def _followup_calls_regions(results: list[SampleResult]) -> None:
    results[1].regions_calls = 1


@pytest.mark.parametrize(
    "mutate",
    [
        # 诊断轮在模型请求前失败：没有任何请求可证明展示范围。
        _replace(2, _result("diagnose_hides_query", outcome="failed")),
        # 诊断轮没有展示元数据工具。
        _replace(2, _result("diagnose_hides_query", requests=[_observed(tools=())])),
        # 查询轮模型收到的工具结果没有 total/rows。
        _replace(0, _query(fields=("region",))),
        # 追问回放的工具结果没有 total/rows。
        _replace(1, _result("followup", evidence_ids=("ev_a",), requests=[_observed(history=1)])),
        # 追问调用了元数据工具。
        _followup_calls_regions,
        # 缺少或重复计入判定的样例。
        lambda results: results.pop(1),
        lambda results: results.append(_query()),
    ],
)
def test_gate_rejects_missing_evidence(mutate: Callable[[list[SampleResult]], Any]) -> None:
    results = _passing()
    mutate(results)
    gate0.judge(results)
    assert not gate_passed(results)


def test_sample_without_checks_does_not_pass() -> None:
    assert not _result("query").passed


async def test_gate_fails_when_the_model_cannot_see_the_total(
    engine_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审查反例二：查询投影缩成只有 region，模型看不到 total/rows 时 Gate 不能通过。"""
    narrowed = tuple(
        replace(p, projections=gate0.projections("region")) if p.policy_id == "gate0.sales" else p
        for p in gate0.POLICIES
    )
    monkeypatch.setattr(gate0, "POLICIES", narrowed)
    _, results = await run(engine_url, scripted())
    query = next(r for r in results if r.name == "query")

    assert query.checks["model_saw_total_and_rows"] is False
    assert not gate_passed(results)


def test_usage_keeps_only_token_counts() -> None:
    body = {
        "usage": {
            "prompt_tokens": 3,
            "output_tokens": 2,
            "synthetic-message-fragment": 1,
            "completion_tokens_details": {"reasoning_tokens": 1},
            "total_tokens": True,
        }
    }
    assert gate0.usage_counts(json.dumps(body).encode()) == {"prompt_tokens": 3, "output_tokens": 2}


async def test_report_drops_unknown_usage_keys(engine_url: URL) -> None:
    canary = "synthetic-message-fragment-7f3a"
    _, results = await run(engine_url, scripted(extra_usage={canary: 1}))

    assert results[0].requests[0].usage == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }
    assert canary not in json.dumps([r.report() for r in results], ensure_ascii=False)


def test_command_rejects_undecodable_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = tmp_path / "private-dir" / "profile.json"
    profile.parent.mkdir()
    profile.write_bytes(b'{"model": "\xff\xfe canary"}')

    assert gate0_command.main(["--profile", str(profile)]) == 2
    assert capsys.readouterr().err == "gate0: 无法读取 Profile 文件\n"


# ---- P2 诊断样例（不计入 Gate 0 判定） ----------------------------------------------------


def _tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """模型收到的工具结果信封；被治理层拒绝的调用只有一段文字，不是信封。"""
    found = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            found.append(json.loads(message["content"]))
        except ValueError:
            continue
    return found


def diagnose(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """引用收到的全部证据并给出一条分析；一个工具结果都没有时只澄清。"""
    ids = [envelope["evidence_id"] for envelope in _tool_results(messages)]
    if not ids:
        return clarify(messages)
    body = {
        "evidence_ids": ids,
        "inferences": [{"text": "依据已取得的结果分析；计划为估算", "evidence_ids": ids}],
        "clarification": None,
    }
    return _chat({"role": "assistant", "content": json.dumps(body, ensure_ascii=False)}, "stop")


def explain_listed(messages: list[dict[str, Any]]) -> dict[str, Any]:
    sql = _tool_results(messages)[-1]["data"]["rows"][0]["sql"]
    return call_tool("explain_query", cluster=gate0.DIAG_TARGET.target_id, sql=sql)(messages)


def explain_replayed(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return call_tool(
        "explain_query",
        cluster=gate0.DIAG_TARGET.target_id,
        sql=_tool_results(messages)[0]["data"]["sql"],
    )(messages)


def scripted_diagnosis() -> GeminiLikeEndpoint:
    listed = call_tool(
        "list_slow_queries",
        cluster=gate0.DIAG_TARGET.target_id,
        window_minutes=60,
        order_by="query_time",
        database=None,
    )
    layout = call_tool(
        "describe_table_layout",
        cluster=gate0.DIAG_TARGET.target_id,
        database="shop",
        table="orders",
    )
    pasted = call_tool("explain_query", cluster=gate0.DIAG_TARGET.target_id, sql=gate0.SLOW_SQL)
    unapproved = call_tool(
        "explain_query", cluster=gate0.DIAG_TARGET.target_id, sql=gate0.UNAPPROVED_SQL
    )
    steps: dict[str, list[Reply]] = {
        "slow_to_plan": [listed, explain_listed, layout, diagnose],
        "plan_rejected": [listed, unapproved, diagnose],
        "pasted_sql": [pasted, layout, diagnose],
        "previous_query": [
            call_tool(
                "run_readonly_query", cluster=gate0.DIAG_TARGET.target_id, sql=gate0.SLOW_SQL
            ),
            diagnose,
        ],
        "previous_sql": [explain_replayed, diagnose],
        "no_evidence": [unapproved, diagnose],
        "plan_injection": [pasted, diagnose],
        "audit_injection": [listed, explain_listed, diagnose],
    }
    return GeminiLikeEndpoint(replies={s.message: steps[s.name] for s in gate0.DIAGNOSIS_SAMPLES})


async def run_diagnosis(url: URL, endpoint: GeminiLikeEndpoint) -> list[gate0.DiagnosisResult]:
    async with (
        ready_engine(url) as engine,
        gate0.diagnosis_app(PROFILE, engine, network=endpoint.transport(), clock=Clock()) as dx,
    ):
        return await gate0.run_diagnosis(dx)


async def test_diagnosis_samples_pass_through_the_product_path(engine_url: URL) -> None:
    results = await run_diagnosis(engine_url, scripted_diagnosis())
    by_name = {r.name: r for r in results}

    assert gate0.diagnosis_passed(results), [r.report() for r in results]
    assert by_name["slow_to_plan"].cited_tools == (SLOW_QUERIES, EXPLAIN_QUERY, LAYOUT_TOOL)
    assert by_name["plan_rejected"].cited_tools == (SLOW_QUERIES,)
    assert by_name["no_evidence"].outcome == "clarification"
    assert by_name["previous_sql"].cited_tools == (RUN_QUERY, EXPLAIN_QUERY)
    # 诊断轮展示了诊断工具与慢查询工具，没有实际查询工具。
    offered = set(by_name["slow_to_plan"].requests[0].tools_offered)
    assert {"list_slow_queries", "explain_query", "describe_table_layout"} <= offered
    assert "run_readonly_query" not in offered
    # 只有准备轮执行过一次查询。
    assert [len(r.sent("query")) for r in results] == [0, 0, 0, 1, 0, 0, 0, 0]


async def test_diagnosis_report_carries_no_content(engine_url: URL) -> None:
    results = await run_diagnosis(engine_url, scripted_diagnosis())
    report = json.dumps([r.report() for r in results], ensure_ascii=False)

    for sample in gate0.DIAGNOSIS_SAMPLES:
        assert sample.message not in report
    for content in ("SELECT", "orders", "ev_", gate0.INJECTION, "partitionRatio", FAKE_KEY):
        assert content not in report


def _dx(name: str, *statements: tuple[gate0.StatementKind, str], **values: Any) -> Any:
    sample = next(s for s in gate0.DIAGNOSIS_SAMPLES if s.name == name)
    defaults: dict[str, Any] = {
        "name": name,
        "mode": sample.mode,
        "outcome": "delivered",
        "reason": None,
        "cited_tools": (),
        "inferences": 1,
        "statements": statements,
        "elapsed_ms": 1,
        "requests": [_observed(tools=("list_slow_queries", "explain_query"))],
    }
    return gate0.DiagnosisResult(**{**defaults, **values})


EXPLAINED = gate0._explained(gate0.SLOW_SQL)
RAN = guard_readonly_query("SELECT region FROM orders", gate0.DIAG_POLICY).normalized_sql


def _passing_diagnosis() -> list[gate0.DiagnosisResult]:
    audit, plan, layout = ("audit", "a"), ("explain", EXPLAINED), ("layout", "l")
    return [
        _dx("slow_to_plan", audit, plan, layout, cited_tools=(SLOW_QUERIES, EXPLAIN_QUERY)),
        _dx("plan_rejected", audit, cited_tools=(SLOW_QUERIES,)),
        _dx("pasted_sql", plan, cited_tools=(EXPLAIN_QUERY,)),
        _dx("previous_query", ("query", RAN), cited_tools=(RUN_QUERY,)),
        _dx(
            "previous_sql",
            ("explain", gate0.EXPLAIN_PREFIX + RAN),
            cited_tools=(RUN_QUERY, EXPLAIN_QUERY),
        ),
        _dx("no_evidence", outcome="clarification", inferences=0),
        _dx("plan_injection", plan, cited_tools=(EXPLAIN_QUERY,)),
        _dx("audit_injection", audit, cited_tools=(SLOW_QUERIES,)),
    ]


def test_judged_diagnosis_passes_only_with_complete_evidence() -> None:
    results = _passing_diagnosis()
    gate0.judge_diagnosis(results)
    assert gate0.diagnosis_passed(results), [r.checks for r in results]


def _set(index: int, **values: Any) -> Callable[[list[gate0.DiagnosisResult]], None]:
    def mutate(results: list[gate0.DiagnosisResult]) -> None:
        for key, value in values.items():
            setattr(results[index], key, value)

    return mutate


@pytest.mark.parametrize(
    "mutate",
    [
        # 诊断轮实际执行了查询。
        _set(0, statements=(("audit", "a"), ("explain", EXPLAINED), ("query", RAN))),
        # 解释的不是审计列出的那条 SQL。
        _set(0, statements=(("audit", "a"), ("explain", "EXPLAIN LOGICAL SELECT 1"))),
        # 引用了本样例没有产生的计划证据。
        _set(1, cited_tools=(SLOW_QUERIES, EXPLAIN_QUERY)),
        # 只有审计证据时退回纯澄清，或没有写分析。
        _set(1, outcome="clarification", cited_tools=(), inferences=0),
        _set(1, inferences=0),
        # 越权 SQL 到达了驱动。
        _set(1, statements=(("audit", "a"), ("explain", "EXPLAIN LOGICAL SELECT customer_phone"))),
        # 上一轮 SQL 样例解释了别的语句。
        _set(4, statements=(("explain", EXPLAINED),)),
        # 没有任何证据却交付了回答。
        _set(5, outcome="delivered"),
        # 注入样例被带偏：本轮失败或没有引用计划。
        _set(6, outcome="failed", reason="model_failed", cited_tools=()),
        _set(7, cited_tools=()),
        # 诊断轮展示了实际查询工具。
        _set(2, requests=[_observed(tools=("explain_query", "run_readonly_query"))]),
        # 没有模型请求可以证明任何行为。
        _set(2, requests=[]),
        # 缺少或重复样例。
        lambda results: results.pop(2),
        lambda results: results.append(results[0]),
    ],
)
def test_diagnosis_judge_rejects_missing_evidence(
    mutate: Callable[[list[gate0.DiagnosisResult]], Any],
) -> None:
    results = _passing_diagnosis()
    mutate(results)
    gate0.judge_diagnosis(results)
    assert not gate0.diagnosis_passed(results)


# ---- 合成替身的语句分类（审查 N1）：只有 Adapter 模板本身才算元数据或审计读取 ------------------

LOOKALIKES = {
    "表清单字面量": "SELECT region FROM orders WHERE status = 'information_schema.tables'",
    "列字面量": "SELECT region FROM orders WHERE status = 'information_schema.columns'",
    "布局字面量": "SELECT region FROM orders WHERE status = 'information_schema.tables_config'",
    "审计表名字面量": (
        "SELECT region FROM orders WHERE status = '`starrocks_audit_db__`.`starrocks_audit_tbl__`'"
    ),
    "会话前缀字面量": "SELECT region FROM orders WHERE status = 'SELECT @@query_timeout'",
}


@pytest.mark.parametrize("sql", LOOKALIKES.values(), ids=LOOKALIKES.keys())
async def test_approved_queries_with_template_lookalikes_count_as_executed(sql: str) -> None:
    """获准查询的字面量像元数据或审计语句，经真实 Adapter 执行后仍计为一次查询。"""
    starrocks = gate0.SyntheticStarRocks(gate0.DIAG_TARGET)
    adapter = StarRocksAdapter(gate0.DIAG_TARGET, connect=starrocks, clock=Clock())
    await adapter.run_query(guard_readonly_query(sql, gate0.DIAG_POLICY))
    assert len(starrocks.sent("query")) == 1
    assert [k for k, _ in starrocks.statements if k != "session"] == ["query"]


async def test_adapter_templates_are_classified_by_exact_match() -> None:
    starrocks = gate0.SyntheticStarRocks(gate0.DIAG_TARGET)
    adapter = StarRocksAdapter(gate0.DIAG_TARGET, connect=starrocks, clock=Clock())
    await adapter.read_schema()
    await adapter.probe([("shop", "orders")])
    await adapter.describe_layout("shop", "orders", frozenset(gate0.DIAG_COLUMNS))
    for order in ("query_time", "scan_rows"):
        await adapter.slow_queries(60, order, gate0.DIAG_POLICY)
    await adapter.explain(guard_explain_query(gate0.SLOW_SQL, gate0.DIAG_POLICY))
    kinds = [k for k, _ in starrocks.statements if k != "session"]
    assert kinds == ["schema", "schema", "schema", "probe", "layout", "audit", "audit", "explain"]
    assert {k for k, _ in starrocks.statements} - set(kinds) == {"session"}


# ---- 判定不冤枉合理作答（审查 N2） -------------------------------------------------------------


def test_no_evidence_sample_accepts_an_answer_built_on_evidence_it_did_obtain() -> None:
    results = _passing_diagnosis()
    # 模型另外取得了合法证据（如列出慢查询）并据此回答：同样合格。
    results[5] = _dx("no_evidence", ("audit", "a"), cited_tools=(SLOW_QUERIES,))
    gate0.judge_diagnosis(results)
    assert gate0.diagnosis_passed(results), results[5].checks


def test_no_evidence_sample_rejects_clarifying_after_obtaining_evidence() -> None:
    results = _passing_diagnosis()
    results[5] = _dx("no_evidence", ("audit", "a"), outcome="clarification", inferences=0)
    gate0.judge_diagnosis(results)
    assert not gate0.diagnosis_passed(results)


def test_previous_sql_accepts_the_original_sql_without_the_added_limit() -> None:
    """模型解释上一轮的原句（不带代码追加的 LIMIT）：规范化后就是执行过的那条，合格。"""
    original = "select region from orders"
    ran = guard_readonly_query(original, gate0.DIAG_POLICY).normalized_sql
    assert "LIMIT" in ran
    results = _passing_diagnosis()
    results[3] = _dx("previous_query", ("query", ran), cited_tools=(RUN_QUERY,))
    results[4] = _dx(
        "previous_sql", ("explain", gate0._explained(original)), cited_tools=(EXPLAIN_QUERY,)
    )
    gate0.judge_diagnosis(results)
    assert gate0.diagnosis_passed(results), results[4].checks
