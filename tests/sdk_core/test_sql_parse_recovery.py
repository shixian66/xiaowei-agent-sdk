"""SQL 解析失败：正式 SDK/治理/Adapter/Evidence/PolicySession，PG 为隔离实例。"""

import asyncio
import json
import logging

import pytest
from asyncmy.errors import OperationalError, ProgrammingError
from sqlalchemy.engine import URL
from tests.p1b.test_starrocks_adapter import Driver, FakeConnection, Result, driver
from tests.sdk_core.test_app import ModelCall, answer, cite, last_output, tool_call
from tests.sdk_core.test_starrocks_tools import SR, assembled, executed_sql

from xiaowei.app import TurnError
from xiaowei.governance import SqlParseError, ToolExecutionError, ToolRejectedError
from xiaowei.models import ToolRequest
from xiaowei.starrocks import EXPLAIN_PREFIX, StarRocksErrorCode
from xiaowei.starrocks_tools import LAYOUT_TOOL, RUN_QUERY

pytestmark = pytest.mark.loopback
PRIOR = "SELECT region FROM shop.sales WHERE total > 0"
BAD = "SELECT region AS rows FROM shop.sales"
FIXED = "SELECT region AS `rows` FROM shop.sales"
SENTINEL = "sql-error-private-4d87"
PARSE_MESSAGE = (
    "Getting syntax error at line 1, column 17. Detail message: "
    f"Unexpected input 'rows' {SENTINEL}."
)


def parse_driver() -> Driver:
    drv = driver(Result(("region",), [("east",)]))
    original = drv.make

    def connection() -> FakeConnection:
        conn = original()
        conn.results[f"{BAD} LIMIT 6"] = ProgrammingError(1064, PARSE_MESSAGE)
        conn.results[f"{FIXED} LIMIT 6"] = Result(("rows",), [("east",)])
        return conn

    drv.make = connection
    return drv


async def test_s1_parse_failure_can_be_corrected_without_losing_earlier_evidence(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="xiaowei.governance")
    async with assembled(postgres_url, monkeypatch, parse_driver()) as env:

        def correct(call: ModelCall):
            feedback = last_output(call)
            assert feedback["status"] == "invalid_sql"
            assert feedback["source"] == SR.target_id
            assert feedback["correction_allowed"] is True
            assert "line" not in feedback and "column" not in feedback
            assert "evidence_id" not in feedback
            assert SENTINEL not in json.dumps(feedback)
            return tool_call("run_readonly_query", cluster=SR.target_id, sql=FIXED)(call)

        message = env.scripts.add(
            "排查 pre 上该 BE 的 Compaction 压力告警",
            tool_call("run_readonly_query", cluster=SR.target_id, sql=PRIOR),
            tool_call("run_readonly_query", cluster=SR.target_id, sql=BAD),
            correct,
            cite("已取得两份查询事实；这些数据还不足以确认 Compaction 根因"),
        )
        delivered = await env.deliver(env.ctx(max_tool_calls=3), message)
        assert len(delivered.facts) == 2
        assert [fact.metadata["sql"] for fact in delivered.facts] == [
            f"{PRIOR} LIMIT 6",
            f"{FIXED} LIMIT 6",
        ]
        assert await env.evidence_rows() == 2
        assert executed_sql(env.drv) == [
            f"{PRIOR} LIMIT 6",
            f"{BAD} LIMIT 6",
            f"{FIXED} LIMIT 6",
        ]
        assert len(env.scripts.calls[message]) == 4
    logs = [r.getMessage() for r in caplog.records if r.getMessage().startswith("sql_parse ")]
    assert len(logs) == 1
    assert "code=invalid_sql" in logs[0] and "correction_allowed=True" in logs[0]
    assert SENTINEL not in logs[0]


async def test_s2_only_parse_failure_can_finish_at_existing_step_and_tool_limits(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assembled(postgres_url, monkeypatch, parse_driver()) as env:

        def finish(call: ModelCall):
            assert call.tools == []
            feedback = last_output(call)
            assert feedback["status"] == "invalid_sql"
            assert feedback["correction_allowed"] is False
            return answer([], "本次排查未完成，SQL 未能解析，尚无数据验证")

        message = env.scripts.add(
            "只在现有预算内排查",
            tool_call("run_readonly_query", cluster=SR.target_id, sql=BAD),
            finish,
        )
        ctx = env.ctx(max_tool_calls=1)
        ctx = ctx.model_copy(update={"budget": ctx.budget.model_copy(update={"max_turns": 1})})
        turn = await env.app.run_turn(ctx, message)
        assert turn.limit_reached
        delivered = await env.evidence.validate_answer(turn, ctx)
        assert delivered.facts == ()
        assert await env.evidence_rows() == 0
        assert executed_sql(env.drv) == [f"{BAD} LIMIT 6"]
        assert len(env.scripts.calls[message]) == 2


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OperationalError(1064, "mem limit exceeded"), StarRocksErrorCode.QUERY_FAILED),
        (OperationalError(1064, "query timeout"), StarRocksErrorCode.QUERY_FAILED),
        (OperationalError(1064, f"runtime: {PARSE_MESSAGE}"), StarRocksErrorCode.QUERY_FAILED),
        (OperationalError(1142, "SELECT denied"), StarRocksErrorCode.PERMISSION_DENIED),
        (OperationalError(2013, "connection lost"), StarRocksErrorCode.CONNECTION_LOST),
        (OperationalError(1105, "unknown execution failure"), StarRocksErrorCode.QUERY_FAILED),
    ],
)
async def test_s3_other_sql_failures_remain_terminal(
    postgres_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    code: StarRocksErrorCode,
) -> None:
    async with assembled(postgres_url, monkeypatch, driver(error)) as env:
        message = env.scripts.add(
            "遇到原有不可恢复错误时停止",
            tool_call("run_readonly_query", cluster=SR.target_id, sql=PRIOR),
        )
        with pytest.raises(TurnError) as failed:
            await env.app.run_turn(env.ctx(), message)
        assert failed.value.reason == "tool_failed"
        assert failed.value.tool_error is code
        assert len(env.scripts.calls[message]) == 1
        assert len(executed_sql(env.drv)) == 1
        assert await env.evidence_rows() == 0


def request(call_id: str, sql: str = FIXED) -> ToolRequest:
    return ToolRequest(
        tool_id=RUN_QUERY,
        tool_name="run_readonly_query",
        target_id=SR.target_id,
        call_id=call_id,
        arguments={"cluster": SR.target_id, "sql": sql},
    )


async def test_busy_retry_parse_failure_does_not_block_the_corrected_query(
    postgres_url: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with assembled(postgres_url, monkeypatch, parse_driver()) as env:
        ctx = env.ctx(max_tool_calls=2)
        execute = env.executes[(RUN_QUERY, SR.target_id)]
        for _ in range(SR.pool_size):
            await env.adapter._slots.acquire()
        try:
            with pytest.raises(ToolRejectedError) as busy:
                await env.governed.invoke(ctx, request("busy"), execute)
            assert busy.value.code == "busy" and busy.value.remaining_retries == 1
            assert env.drv.attempts == 0
        finally:
            for _ in range(SR.pool_size):
                env.adapter._slots.release()
        with pytest.raises(SqlParseError):
            await env.governed.invoke(ctx, request("parse", BAD), execute)
        runs = env.governed.turn_runs(ctx.identity)
        assert SR.target_id not in runs.slot_busy
        assert runs.slot_retries == frozenset({SR.target_id})
        result = await env.governed.invoke(ctx, request("correct"), execute)
        assert result.evidence_id
        runs = env.governed.turn_runs(ctx.identity)
        assert runs.started == (RUN_QUERY, RUN_QUERY)
        assert runs.sql_parse_failures == (RUN_QUERY,)
        assert runs.slot_retries == frozenset({SR.target_id})
        assert len(runs.produced) == 1 and await env.evidence_rows() == 1


async def test_parallel_parse_failure_and_success_both_remain_accounted(
    postgres_url: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drv = parse_driver()
    original = drv.make
    entered = 0
    ready = asyncio.Event()

    def connection() -> FakeConnection:
        conn = original()
        original_execute = conn.execute

        async def execute(sql, args):
            nonlocal entered
            if sql in {f"{BAD} LIMIT 6", f"{FIXED} LIMIT 6"}:
                entered += 1
                if entered == 2:
                    ready.set()
                await ready.wait()
            return await original_execute(sql, args)

        conn.execute = execute
        return conn

    drv.make = connection
    async with assembled(postgres_url, monkeypatch, drv) as env:
        ctx = env.ctx(max_tool_calls=2)
        execute = env.executes[(RUN_QUERY, SR.target_id)]
        results = await asyncio.gather(
            env.governed.invoke(ctx, request("parallel-bad", BAD), execute),
            env.governed.invoke(ctx, request("parallel-good"), execute),
            return_exceptions=True,
        )
        assert isinstance(results[0], SqlParseError)
        assert not isinstance(results[1], BaseException)
        runs = env.governed.turn_runs(ctx.identity)
        assert runs.started == (RUN_QUERY, RUN_QUERY)
        assert runs.sql_parse_failures == (RUN_QUERY,)
        assert runs.produced == ((RUN_QUERY, results[1].evidence_id),)
        assert await env.evidence_rows() == 1
        attempts = drv.attempts
        with pytest.raises(ToolRejectedError) as exhausted:
            await env.governed.invoke(ctx, request("over-budget"), execute)
        assert exhausted.value.code == "budget_exhausted" and drv.attempts == attempts


@pytest.mark.parametrize("line", [1, 2])
async def test_explain_feedback_omits_unverified_positions(
    postgres_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    line: int,
) -> None:
    sql = "SELECT region\nFROM shop.sales"
    message = f"Getting syntax error at line {line}, column 21. Detail message: bad token."
    if line == 2:
        message = "Getting syntax error at line 2, column 5. Detail message: bad token."
    async with assembled(postgres_url, monkeypatch, driver(ProgrammingError(1064, message))) as env:

        def finish(call):
            feedback = last_output(call)
            assert feedback["status"] == "invalid_sql"
            assert "line" not in feedback and "column" not in feedback
            return answer([], "执行计划未取得，尚不能判断根因")

        script = env.scripts.add(
            "只读诊断 SQL",
            tool_call("explain_query", cluster=SR.target_id, sql=sql),
            finish,
        )
        delivered = await env.deliver(env.ctx("diagnose"), script)
        assert delivered.facts == () and await env.evidence_rows() == 0
        assert executed_sql(env.drv) == [EXPLAIN_PREFIX + sql]


async def test_code_owned_metadata_parse_failure_is_not_a_model_correction(
    postgres_url: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with assembled(
        postgres_url, monkeypatch, driver(ProgrammingError(1064, PARSE_MESSAGE))
    ) as env:
        ctx = env.ctx()
        metadata = ToolRequest(
            tool_id=LAYOUT_TOOL,
            tool_name="describe_table_layout",
            target_id=SR.target_id,
            call_id="metadata",
            arguments={"cluster": SR.target_id, "database": "shop", "table": "sales"},
        )
        with pytest.raises(ToolExecutionError) as failed:
            await env.governed.invoke(ctx, metadata, env.executes[(LAYOUT_TOOL, SR.target_id)])
        assert failed.value.code is StarRocksErrorCode.QUERY_FAILED
        assert env.governed.turn_runs(ctx.identity).sql_parse_failures == ()
        assert await env.evidence_rows() == 0


@pytest.mark.parametrize("legacy", [False, True])
async def test_native_pause_restores_sql_failure_ledger_without_replaying_queries(
    postgres_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
) -> None:
    from agents import Agent
    from agents.testing import ModelStep, ScriptedModel, function_call
    from pydantic import TypeAdapter
    from sqlalchemy import text
    from tests.spike.test_sdk_hitl_reuse import restore, run
    from tests.spike.test_sdk_resume_ledger import cite_all, save_checkpoint

    from xiaowei.governance import GovernedTools, TurnLedger
    from xiaowei.models import AgentAnswer, RunContext, TurnAnswer
    from xiaowei.tools import routed_function_tool

    async with assembled(postgres_url, monkeypatch, parse_driver()) as env:
        ctx = env.ctx(max_tool_calls=3)
        first_sql = PRIOR if legacy else BAD

        async def gate(_ctx, _arguments, call_id):
            # 测试只门控第二次读取，用 SDK 原生暂停验证账本；不新增生产审批能力。
            return call_id == "c2"

        def agent(model, governance):
            tool = routed_function_tool(
                RUN_QUERY, governance, {SR.target_id: env.executes[(RUN_QUERY, SR.target_id)]}
            )
            tool.needs_approval = gate
            return Agent(name="sql-ledger", model=model, tools=[tool], output_type=AgentAnswer)

        first_agent = agent(
            ScriptedModel(
                [
                    [
                        function_call(
                            "run_readonly_query",
                            {"cluster": SR.target_id, "sql": first_sql},
                            call_id="c1",
                        )
                    ],
                    [
                        function_call(
                            "run_readonly_query",
                            {"cluster": SR.target_id, "sql": FIXED},
                            call_id="c2",
                        )
                    ],
                ]
            ),
            env.governed,
        )
        session = env.app._session(ctx)
        paused = await run(first_agent, "SDK SQL 修正任务", ctx=ctx, session=session)
        assert len(paused.interruptions) == 1
        assert executed_sql(env.drv) == [f"{first_sql} LIMIT 6"]
        await save_checkpoint(env, ctx, paused)
        await session.discard_pending()
        async with env.engine.begin() as conn:
            raw_ctx, saved, raw_ledger = (
                await conn.execute(text("SELECT context, state, ledger FROM sdk_spike_checkpoint"))
            ).one()
            if legacy:
                old = json.loads(raw_ledger)
                del old["runs"]["sql_parse_failures"]
                raw_ledger = json.dumps(old)
                await conn.execute(
                    text("UPDATE sdk_spike_checkpoint SET ledger=:ledger"), {"ledger": raw_ledger}
                )
        resumed_ctx = RunContext.model_validate_json(raw_ctx)
        ledger = TypeAdapter(TurnLedger).validate_json(raw_ledger, strict=True)
        assert ledger.used == 1
        assert ledger.runs.sql_parse_failures == (() if legacy else (RUN_QUERY,))
        governance = GovernedTools(env.evidence)
        governance.restore(resumed_ctx, ledger)
        resumed_agent = agent(ScriptedModel([ModelStep(responder=cite_all)]), governance)
        state = await restore(resumed_agent, saved, resumed_ctx)
        state.approve(state.get_interruptions()[0], always_approve=False)
        result = await run(resumed_agent, state)
        restored = governance.checkpoint(ctx.identity)
        assert restored.used == 2
        assert restored.runs.sql_parse_failures == ledger.runs.sql_parse_failures
        assert len(restored.runs.started) == 2
        assert len(restored.runs.produced) + len(restored.runs.sql_parse_failures) == 2
        seen = tuple(e for _, e in restored.runs.produced)
        resumed_session = env.app._session(resumed_ctx)
        await resumed_session.get_items()
        await resumed_session.add_items(
            [{"role": "user", "content": "SDK SQL 修正任务"}]
            + [
                item.to_input_item()
                for item in result.new_items
                if item.type != "tool_approval_item"
            ]
        )
        await resumed_session.commit_validated(seen)
        delivered = await env.evidence.validate_answer(
            TurnAnswer(answer=result.final_output, context_evidence=seen), resumed_ctx
        )
        assert len(delivered.facts) == (2 if legacy else 1)
        assert executed_sql(env.drv) == [f"{first_sql} LIMIT 6", f"{FIXED} LIMIT 6"]
