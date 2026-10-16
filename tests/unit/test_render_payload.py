"""RenderPayload：跨渠道的统一回答投影。

渠道只选择展示方式，不重算业务结果——因此投影里**不含**原始 rows、SQL、表名或
policy 细节。
"""

import datetime as dt

from xiaowei_agent.capabilities.specs import CAPABILITY_ID, CAPABILITY_VERSION
from xiaowei_agent.contracts import EvidenceEnvelope, ExternalSource, TaskStatus
from xiaowei_agent.reflection.answerability import assess
from xiaowei_agent.rendering.slow_query import render, render_pending

_AT = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)


def _evidence(step_id: str, facts: tuple[dict[str, object], ...]) -> EvidenceEnvelope:
    return EvidenceEnvelope(
        evidence_id=f"task-1:{step_id}",
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_VERSION,
        facts=facts,
        source="starrocks-fake",
        source_kind=ExternalSource.TOOL,
        captured_at=_AT,
        sampled=False,
        limitations=(
            "window: [2026-09-02 11:30:00, 2026-09-02 12:00:00)",
            "row limit: 20",
            "slow query threshold ms: 10000",
            "database filter: sales",
        ),
    )


_ROWS = ({"queryId": "q1", "queryTime": 32_000, "db": "sales", "user": "app_user_1"},)


def _render(evidences: tuple[EvidenceEnvelope, ...], status: TaskStatus) -> object:
    return render(evidences=evidences, verdict=assess(evidences=evidences), status=status)


def test_render_payload_contains_no_sql_and_no_table_names() -> None:
    dumped = _render((_evidence("s1", _ROWS),), TaskStatus.SUCCEEDED).model_dump_json()
    assert "SELECT" not in dumped
    assert "starrocks_audit_db__" not in dumped
    assert "starrocks_audit_tbl__" not in dumped


def test_render_payload_carries_reproducible_parameters() -> None:
    """可复现参数是证据契约的一部分：窗口、目标范围、阈值、行数上限。"""
    payload = _render((_evidence("s1", _ROWS),), TaskStatus.SUCCEEDED)
    dumped = payload.model_dump_json()
    assert "2026-09-02 11:30:00" in dumped
    assert "10000" in dumped
    assert "20" in dumped


def test_render_payload_references_the_evidence() -> None:
    payload = _render((_evidence("s1", _ROWS),), TaskStatus.SUCCEEDED)
    assert "task-1:s1" in payload.refs


def test_empty_evidence_is_not_rendered_as_success() -> None:
    """空证据不得渲染成成功。"""
    payload = _render((), TaskStatus.INDETERMINATE)
    assert payload.status is TaskStatus.INDETERMINATE
    assert payload.refs == ()


def test_indeterminate_answer_says_it_could_not_be_confirmed() -> None:
    evidences = (_evidence("s1", ()), _evidence("s2", ({"query_count": 0},)))
    payload = _render(evidences, TaskStatus.INDETERMINATE)
    assert payload.status is TaskStatus.INDETERMINATE
    assert payload.next_steps


def test_limitations_reach_the_payload() -> None:
    payload = _render((_evidence("s1", _ROWS),), TaskStatus.SUCCEEDED)
    assert any("row limit" in section.body for section in payload.sections)


def test_direction_hints_are_marked_as_suspected_and_stay_out_of_facts() -> None:
    """方向判断一律带"疑似"，且只进说明段。"""
    payload = _render((_evidence("s1", _ROWS),), TaskStatus.SUCCEEDED)
    bodies = " ".join(section.body for section in payload.sections)
    if "疑似" in bodies or "suspected" in bodies.lower():
        assert "根因" not in payload.answer


def test_rendering_is_deterministic() -> None:
    evidences = (_evidence("s1", _ROWS),)
    first = _render(evidences, TaskStatus.SUCCEEDED)
    second = _render(evidences, TaskStatus.SUCCEEDED)
    assert first == second


def test_pending_payload_is_auditable_and_leaks_nothing() -> None:
    payload = render_pending(approval_ref="task-1:s1", evidences=())
    assert payload.status is TaskStatus.AWAITING_APPROVAL
    assert payload.refs == ("task-1:s1",)
    assert "SELECT" not in payload.model_dump_json()


def test_pending_payload_carries_the_evidence_gathered_so_far() -> None:
    payload = render_pending(approval_ref="task-1:s2", evidences=(_evidence("s1", _ROWS),))
    assert "task-1:s1" in payload.refs
    assert "task-1:s2" in payload.refs
