"""L2：只读闭环（端到端）。

成功标准（§12）：计划、``plan_hash``、两条 SQL、``tool_call_hash`` 逐次相同；证据
带来源、采样时间、目标范围、限制与可复现参数；**空证据不得渲染成成功**；
``empty_in_scope_but_traffic_elsewhere`` 必须降级为 ``indeterminate``。

**离线 eval 结果不得表述为部署、canary 或用户验收。** M3 结束时能力状态最强为
``tests``。
"""

import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes import recordings
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.contracts import TaskStatus
from xiaowei_agent.planning import compute_plan_hash, compute_tool_call_hash

_CORPUS = json.loads(
    (Path(__file__).parent / "corpus" / "l2_readonly.json").read_text(encoding="utf-8")
)
_CASES: list[dict[str, Any]] = _CORPUS["cases"]


def _harness(case: dict[str, Any]) -> RuntimeHarness:
    return RuntimeHarness(
        getattr(recordings, case["recording"]),
        synthetic_write=case.get("synthetic_write", False),
    )


def test_corpus_covers_every_declared_scenario() -> None:
    ids = {case["id"] for case in _CASES}
    assert {
        "L2-golden",
        "L2-empty-with-traffic",
        "L2-empty-without-traffic",
        "L2-empty-in-scope-but-traffic-elsewhere",
        "L2-adapter-timeout",
        "L2-malformed",
        "L2-pending",
    } <= ids


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["id"])
async def test_end_to_end_case(case: dict[str, Any]) -> None:
    harness = _harness(case)
    payload = await harness.handle(case["text"])
    assert payload.status is TaskStatus(case["expect_status"])
    assert [call.operation for call in harness.adapter.calls] == case["expect_operations"]
    assert len(payload.refs) >= case["expect_evidence"] or payload.status is (
        TaskStatus.AWAITING_APPROVAL
    )


@pytest.mark.parametrize(
    "case", [c for c in _CASES if not c.get("synthetic_write")], ids=lambda c: c["id"]
)
async def test_repeated_runs_are_byte_identical(case: dict[str, Any]) -> None:
    """固定输入 → 同一计划、同一 plan_hash、同一 tool_call_hash。"""
    first, second = _harness(case), _harness(case)
    await first.handle(case["text"])
    await second.handle(case["text"])
    assert [compute_tool_call_hash(c) for c in first.adapter.calls] == [
        compute_tool_call_hash(c) for c in second.adapter.calls
    ]
    if first.task_id and second.task_id:
        assert compute_plan_hash(
            (await first.plan_store.load(task_id=first.task_id)).plan
        ) == compute_plan_hash((await second.plan_store.load(task_id=second.task_id)).plan)


async def test_empty_evidence_is_never_rendered_as_success() -> None:
    for case in _CASES:
        if case["expect_evidence"] == 0 and not case.get("synthetic_write"):
            harness = _harness(case)
            payload = await harness.handle(case["text"])
            assert payload.status is not TaskStatus.SUCCEEDED


async def test_evidence_carries_source_time_scope_and_limitations() -> None:
    harness = RuntimeHarness(recordings.GOLDEN)
    await harness.handle("最近30分钟有哪些慢查询")
    stored = await harness.ledger.load(task_id=harness.task_id)
    assert stored
    envelope = stored[0]
    assert envelope.source == "starrocks-fake"
    assert envelope.captured_at is not None
    assert envelope.readonly is True
    joined = " ".join(envelope.limitations)
    assert "window" in joined
    assert "row limit" in joined
    assert "threshold" in joined


async def test_b1_regression_downgrades_instead_of_answering_confidently() -> None:
    """B1：sales 无审计数据、别的库有流量 → 必须降级，不能自信地回答。"""
    harness = RuntimeHarness(recordings.EMPTY_IN_SCOPE_BUT_TRAFFIC_ELSEWHERE)
    payload = await harness.handle("查一下 sales 库最近30分钟的慢查询")
    assert payload.status is TaskStatus.INDETERMINATE
    assert [c.operation for c in harness.adapter.calls] == [
        "list_slow_queries",
        "count_queries_in_window",
    ]
    # 两次调用都带 sales 过滤——这正是 count 复用目标范围的执行期证据。
    assert all(c.typed_args.get("database") == "sales" for c in harness.adapter.calls)
