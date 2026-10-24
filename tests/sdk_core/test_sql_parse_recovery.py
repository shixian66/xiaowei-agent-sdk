"""SQL 解析失败：正式 SDK/治理/Adapter/Evidence/PolicySession，PG 为隔离实例。"""

import json
import logging

import pytest
from asyncmy.errors import ProgrammingError
from sqlalchemy.engine import URL
from tests.p1b.test_starrocks_adapter import Driver, Result, driver
from tests.sdk_core.test_app import ModelCall, answer, cite, last_output, tool_call
from tests.sdk_core.test_starrocks_tools import SR, assembled, executed_sql

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

    def connection():
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
            assert (feedback["line"], feedback["column"]) == (1, 17)
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
