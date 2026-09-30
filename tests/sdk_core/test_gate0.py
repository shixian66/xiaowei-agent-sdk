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
