"""证据构造：把一次工具调用的结果包成可审计的事实。"""

import datetime as dt

import pytest
from tests.fakes.admission import PARAMS, REGISTRY_SNAPSHOT, TARGET, slow_query_plan

from xiaowei_agent.capabilities.specs import SLOW_QUERY_SURFACE as SURFACE
from xiaowei_agent.contracts import AdapterStatus, ExternalSource, ToolResult
from xiaowei_agent.evidence.builder import build_evidence, evidence_id
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.fake import RecordingToolAdapter
from xiaowei_agent.tools.gateway import DeterministicToolGateway

_AT = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)
_TASK = "task-1"

_ROW = {
    "queryId": "q1",
    "timestamp": "2026-09-02 11:40:00",
    "queryTime": 12000,
    "db": "sales",
    "user": "app_user_1",
    "state": "FINISHED",
    "errorCode": "",
}


def _result(rows: tuple[dict[str, object], ...]) -> ToolResult:
    """经**真实 Gateway** 取得 ToolResult。

    不给 tools/ 加一个"测试用构造器"：那会把 Gateway 的签发凭据暴露到签发模块之外，
    而 test_only_the_issuing_module_references_a_construction_witness 正是禁止这件事的。
    走真实路径既不破边界，也顺带证明了证据构造消费的确实是 Gateway 归一化后的结果。
    """
    import asyncio

    from tests.conftest import make_certificate
    from tests.fakes.admission import CONTEXT, slow_query_call

    adapter = RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.OK,
                payload=rows,
                source="starrocks-fake",
                error=None,
                elapsed_ms=3,
            ),
        )
    )
    gateway = DeterministicToolGateway(adapters={"starrocks": adapter})
    call = slow_query_call()
    return asyncio.run(gateway.invoke(call, context=CONTEXT, admission=make_certificate(call)))


def _build(rows: tuple[dict[str, object], ...] = (_ROW,)) -> object:
    plan = slow_query_plan()
    return build_evidence(
        task_id=_TASK,
        step=plan.steps[0],
        plan=plan,
        result=_result(rows),
        surface=SURFACE,
        params=PARAMS,
        captured_at=_AT,
    )


def test_evidence_id_is_deterministic_and_addressable() -> None:
    """证据 id 同时是 ledger 的键与 TaskOutcome.evidence_refs 的元素。"""
    assert evidence_id(task_id=_TASK, step_id="s1") == evidence_id(task_id=_TASK, step_id="s1")
    assert evidence_id(task_id=_TASK, step_id="s1") != evidence_id(task_id=_TASK, step_id="s2")
    assert _build().evidence_id == evidence_id(task_id=_TASK, step_id="s1")


def test_capability_identity_comes_from_the_plan_not_the_adapter() -> None:
    plan = slow_query_plan()
    envelope = _build()
    assert envelope.capability_id == plan.capability_id
    assert envelope.capability_version == plan.capability_version


def test_evidence_builder_drops_columns_outside_the_whitelist() -> None:
    """adapter 多返回的列一律丢弃。

    Gateway 归一化过的结果仍可能含未声明列——归一化管的是**形状**，不是**字段集**。
    """
    row = {**_ROW, "stmt": "SELECT 1", "clientIp": "10.0.0.1", "secretish": "x"}
    facts = _build((row,)).facts
    assert set(facts[0]) <= set(SURFACE.allowed_columns)
    assert "stmt" not in facts[0]
    assert "clientIp" not in facts[0]


def test_declared_columns_survive_the_filter() -> None:
    """反例配对：过滤不得把声明列一起丢掉，否则证据永远是空的。"""
    facts = _build().facts
    assert facts[0]["queryId"] == "q1"
    assert facts[0]["queryTime"] == 12000


def test_evidence_marks_sampling_when_the_row_cap_is_hit() -> None:
    rows = tuple({**_ROW, "queryId": f"q{i}"} for i in range(PARAMS.row_limit))
    assert _build(rows).sampled is True


def test_evidence_is_not_sampled_below_the_cap() -> None:
    assert _build().sampled is False


def test_evidence_limitations_contain_the_scope_and_the_window() -> None:
    limitations = " ".join(_build().limitations)
    assert "2026-09-02 11:30:00" in limitations
    assert "2026-09-02 12:00:00" in limitations
    assert str(PARAMS.row_limit) in limitations
    assert str(PARAMS.min_query_time_ms) in limitations


def test_evidence_never_contains_sql_text() -> None:
    dumped = _build().model_dump_json()
    assert "SELECT" not in dumped
    assert "starrocks_audit_tbl__" not in dumped


def test_evidence_is_always_readonly() -> None:
    assert _build().readonly is True


def test_source_kind_is_tool() -> None:
    envelope = _build()
    assert envelope.source_kind is ExternalSource.TOOL
    assert envelope.source == "starrocks-fake"


def test_empty_result_produces_empty_facts_not_a_missing_envelope() -> None:
    """空证据必须是"一份说明了限制的空证据"，不是没有证据。"""
    envelope = _build(())
    assert envelope.facts == ()
    assert envelope.sampled is False
    assert envelope.limitations


def test_captured_at_is_injected_not_read_from_the_clock() -> None:
    assert _build().captured_at == _AT


def test_naive_captured_at_is_rejected() -> None:
    from pydantic import ValidationError

    plan = slow_query_plan()
    with pytest.raises(ValidationError):
        build_evidence(
            task_id=_TASK,
            step=plan.steps[0],
            plan=plan,
            result=_result((_ROW,)),
            surface=SURFACE,
            params=PARAMS,
            captured_at=dt.datetime(2026, 9, 2, 12, 0),
        )


def test_builder_does_not_depend_on_the_target() -> None:
    """构造器签名里没有 target：证据描述"取到了什么"，目标由指纹绑定承载。"""
    import inspect

    assert "target" not in inspect.signature(build_evidence).parameters
    assert TARGET is not None  # 夹具存在，只是刻意不进构造器


def test_snapshot_is_not_needed_to_build_evidence() -> None:
    import inspect

    assert "snapshot" not in inspect.signature(build_evidence).parameters
    assert REGISTRY_SNAPSHOT is not None
