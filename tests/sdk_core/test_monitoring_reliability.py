"""离线只验证真实 SDK 请求、契约透传与评测入口，不给模型诊断能力打分。"""

import json

import httpx2
import pytest
from tests.sdk_core import dialogue_gate
from tests.sdk_core.dialogue_monitoring import synthetic_monitoring_sources
from tests.sdk_core.mcp_fixture import serve
from tests.sdk_core.monitoring_reliability import CASES, SCENARIOS
from tests.sdk_core.test_app import Scripts, answer, tool_call
from tests.sdk_core.test_model_api import OPENAI as PROFILE
from tests.sdk_core.test_prometheus_p2 import SOURCE, cite, p2_config, p2_server
from tests.sdk_core.test_runtime import Env
from tests.sdk_core.test_runtime import env as env  # pytest fixture

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize("tool", ["query", "range_query"])
async def test_formal_model_receives_reliability_contract_and_history_is_preserved(
    env: Env, tool: str
) -> None:
    requests = []

    async def capture(request):
        requests.append(json.loads(request.content))
        return await env.scripts._handle(request)

    expression = 'min_over_time(up{env="fat"}[3h])'
    arguments = {"query": expression}
    if tool == "range_query":
        arguments |= {"start_time": "1792569994", "end_time": "1792569994", "step": "3600"}
    with serve(p2_server) as source:
        async with env.running(
            env.config(**p2_config(source.url)), model_transport=httpx2.MockTransport(capture)
        ) as served:
            await served.page()
            message = env.scripts.add(
                "合成历史调用，只验证表达式透传",
                tool_call(f"{SOURCE}__{tool}", **arguments),
                cite("合成返回；本用例不判断历史采集是否完整"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed"
            (fact,) = body["delivery"]["facts"]
            assert fact["metadata"]["query"] == expression
            if tool == "range_query":
                assert fact["metadata"]["start_time"] == arguments["start_time"]
                assert fact["metadata"]["end_time"] == arguments["end_time"]
            sent = requests[0]
            instructions = sent["instructions"]
            assert "历史查询命中的对象本身不构成范围变更" in instructions
            assert "证据足够就回答" in instructions
            assert "统计口径" in instructions
            query = next(t for t in sent["tools"] if t["name"] == f"{SOURCE}__query")
            assert "表达式可以计算历史统计" in query["description"]
            assert "不代表历史窗口" not in query["description"]
        assert source.recorder.tool_calls[0][1]["query"] == expression


async def test_representative_cases_replay_without_automatic_quality_pass(
    postgres_url, monkeypatch
):
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "offline-monitoring-key")
    scripts = Scripts()
    for case in CASES:
        for _, message in case.scenario.messages:
            scripts.add(message, lambda _: answer([], analysis="脚本占位回答，不代表诊断正确"))
    samples = []

    async def review(sample):
        samples.append(sample)
        return dialogue_gate.ReviewVerdict()

    with synthetic_monitoring_sources() as monitoring:
        reports = await dialogue_gate.run_dialogue(
            PROFILE, postgres_url, scripts.transport,
            scenarios=SCENARIOS, repeats=1, manual_review=review, monitoring=monitoring,
        )
    assert len(reports) == sum(len(s.messages) for s in SCENARIOS)
    assert all(r["state"] == "completed" for r in reports)
    assert all(r["manual_review_required"] is True for r in reports)
    assert all(r["review"]["diagnosis_basis_sufficient"] is None for r in reports)
    assert all(r["fetch_by_target"] == {} and r["mcp_fetches"] == {} for r in reports)
    assert all(not sample.monitoring_calls for sample in samples)
    # 判据只供评审者看，不作为模型的额外指令或机器报告中的正文。
    encoded = json.dumps(reports, ensure_ascii=False)
    for case in CASES:
        assert case.criteria not in encoded
        for _, message in case.scenario.messages:
            assert case.criteria not in scripts.calls[message][0].instructions
    assert "脚本占位回答" not in encoded
