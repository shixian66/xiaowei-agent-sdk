"""R7 formal model/governance paths. Scripted choices are not real-model intent evidence."""

import json

import pytest
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.test_app import clarify, tool_call
from tests.sdk_core.test_starrocks_tools import Env, reassembled, tool_outputs
from tests.sdk_core.test_starrocks_tools import env as env

pytestmark = pytest.mark.loopback


async def test_target_rules_reach_model_without_legacy_guessing(env: Env) -> None:
    message = env.scripts.add("看同名表的结构", clarify("请选集群"))
    await env.deliver(env.ctx(), message)
    instructions = env.scripts.calls[message][0].instructions
    for rule in (
        "只用用户在本会话明确选定的环境",
        "不做跨环境发现",
        "执行许可不继承",
        "新建会话后重新询问",
        "不改查其他环境代替",
    ):
        assert rule in instructions
    for legacy in ("唯一可读", "发现不完整", "获准发现"):
        assert legacy not in instructions
    assert "追问保持原集群与限定表名" not in instructions
    assert "本轮只有一个可用集群时直接使用它" not in instructions
    assert env.drv.attempts == 0


@pytest.mark.parametrize(
    "case,code", [("unknown", "unknown_target"), ("unavailable", "schema_unavailable")]
)
async def test_unknown_or_unavailable_target_feedback_does_not_suggest_a_substitute(
    env: Env, case: str, code: str
) -> None:
    app = env.app
    if case == "unavailable":
        _, app = await reassembled(env, SR)
    message = env.scripts.add(
        "查看表结构",
        tool_call(
            "describe_table",
            cluster="R7-SENTINEL" if case == "unknown" else SR.target_id,
            database="shop",
            table="sales",
            cursor=None,
        ),
        clarify("请核实目标；当前目标不可用"),
    )
    await app.run_turn(env.ctx(), message)
    (raw,) = tool_outputs(env.scripts.calls[message][1])
    assert "Please try again" not in raw
    assert "请使用工具说明中列出的集群 ID" not in raw
    assert "R7-SENTINEL" not in raw
    output = json.loads(raw)
    assert output["code"] == code
    if case == "unknown":
        assert "核实" in output["reason"] and "不猜" in output["reason"]
    else:
        assert output["retry_this_turn"] is False
        assert "不得用其他集群替代本集群" in output["reason"]
        assert "用户明确要求的其他集群照常调查" in output["reason"]
    assert env.drv.attempts == 0
    assert await env.evidence_rows() == 0


async def test_real_pool_wait_is_bounded_before_adapter_entry(env: Env) -> None:
    from xiaowei.governance import Prechecked, ToolRejectedError
    from xiaowei.models import ToolRequest
    from xiaowei.starrocks_tools import RUN_QUERY

    bound = env.executes[(RUN_QUERY, SR.target_id)]
    assert isinstance(bound, Prechecked)
    entries = []

    async def counted(checked):
        entries.append(checked)
        return await bound.run(checked)

    execute = Prechecked(bound.check, counted)
    req = ToolRequest(
        tool_id=RUN_QUERY,
        target_id=SR.target_id,
        call_id="busy",
        tool_name="run_readonly_query",
        arguments={"cluster": SR.target_id, "sql": "SELECT region FROM shop.sales"},
    )
    ctx = env.ctx(max_tool_calls=1)
    for _ in range(SR.pool_size):
        await env.adapter._slots.acquire()
    try:
        for _ in range(2):
            with pytest.raises(ToolRejectedError) as error:
                await env.governed.invoke(ctx, req, execute)
            assert error.value.code == "busy"
        # Budget was refunded but the retry quota is spent. No new Adapter/pool wait.
        with pytest.raises(ToolRejectedError):
            await env.governed.invoke(ctx, req, execute)
        assert len(entries) == 2
        assert env.drv.attempts == 0
        assert env.governed.turn_runs(ctx.identity).started == ()
    finally:
        for _ in range(SR.pool_size):
            env.adapter._slots.release()
    # New turn is independent; real SQL can now complete.
    result = await env.governed.invoke(env.ctx(turn="fresh"), req, execute)
    assert result.evidence_id and len(entries) == 3
