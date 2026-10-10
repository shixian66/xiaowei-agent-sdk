"""R7 Web formal startup with synthetic endpoints, not real model behavior."""

import json

import httpx2
import pytest
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_gate0 import vertex_call
from tests.sdk_core.test_model_api import GEMINI, _chat_tool_call
from tests.sdk_core.test_runtime import Env, statements
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_starrocks_tools import tool_outputs
from tests.sdk_core.test_vertex_model import VERTEX

pytestmark = pytest.mark.loopback


async def test_formal_web_corrects_invalid_sql_then_delivers_once(env: Env) -> None:
    message = env.scripts.add(
        "查所选集群的销售",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT R7_PRIVATE_SENTINEL FROM sales"
        ),
        tool_call(
            "run_readonly_query",
            cluster=SR.target_id,
            sql="SELECT region, total FROM shop.sales LIMIT 5",
        ),
        cite(""),
    )
    cfg = env.config(budget={**env.config().budget.model_dump(), "max_tool_calls": 1})
    async with env.running(cfg) as served:
        await served.page()
        body = (await served.turn(message, "query")).json()
        assert body["state"] == "completed", body
        assert len(statements(env.drv)) == 1
        assert "R7_PRIVATE_SENTINEL" not in statements(env.drv)[0]
        (raw,) = tool_outputs(env.scripts.calls[message][1])
        feedback = json.loads(raw)
        assert feedback["query_reject"] == "column_not_allowed"
        assert feedback["retry_this_turn"] is True
        assert "R7_PRIVATE_SENTINEL" not in raw and "Please try again" not in raw
        (fact,) = body["delivery"]["facts"]
        assert fact["target_id"] == SR.target_id and fact["tool_id"] == "local/run_readonly_query"
        # Rejection feedback is not stored as evidence or replayed as facts.
        again = env.scripts.add("刚才结果再发一下", cite(""))
        replay = (await served.turn(again, "query", "r2")).json()
        assert replay["state"] == "completed" and len(statements(env.drv)) == 1
        assert len(replay["delivery"]["facts"]) == 1
        assert "R7_PRIVATE_SENTINEL" not in str(tool_outputs(env.scripts.calls[again][0]))


@pytest.mark.parametrize("provider", ["chat_completions", "vertex"])
async def test_formal_hidden_tool_still_aborts_before_adapter_io(
    env: Env, provider: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    profile = GEMINI if provider == "chat_completions" else VERTEX
    monkeypatch.setenv(profile.api_key_ref.removeprefix("env:"), "R7-SYNTHETIC-KEY")
    requests = []
    args = {"cluster": SR.target_id, "sql": "SELECT R7_HIDDEN_SENTINEL FROM shop.sales"}

    async def endpoint(req: httpx2.Request) -> httpx2.Response:
        body = json.loads(req.content)
        requests.append(body)
        if provider == "chat_completions":
            result = _chat_tool_call("run_readonly_query", args, "r7-hidden")
        else:
            result = vertex_call("run_readonly_query", **args)(body["contents"])
        return httpx2.Response(200, json=result)

    async with env.running(
        env.config(model=profile.model_dump(mode="json")),
        model_transport=httpx2.MockTransport(endpoint),
    ) as served:
        await served.page()
        body = (await served.turn("只诊断，不执行", "diagnose")).json()
        assert body["state"] == "failed", body
        assert env.drv.attempts == 0 and statements(env.drv) == []
    assert len(requests) == 1
    offered = requests[0].get("tools", [])
    names = (
        {t["function"]["name"] for t in offered}
        if provider == "chat_completions"
        else {f["name"] for t in offered for f in t.get("functionDeclarations", [])}
    )
    assert "run_readonly_query" not in names and "describe_table" in names
    assert "R7_HIDDEN_SENTINEL" not in caplog.text


async def test_formal_budget_exhaustion_stops_new_io_and_model_is_bounded(env: Env) -> None:
    message = env.scripts.add(
        "分别查两次",
        tool_call("run_readonly_query", cluster=SR.target_id, sql="SELECT region FROM shop.sales"),
        tool_call("run_readonly_query", cluster=SR.target_id, sql="SELECT total FROM shop.sales"),
        cite("只能取得第一项"),
    )
    cfg = env.config(budget={**env.config().budget.model_dump(), "max_tool_calls": 1})
    async with env.running(cfg) as served:
        await served.page()
        body = (await served.turn(message, "query")).json()
        assert body["state"] == "completed", body
        assert len(statements(env.drv)) == 1
        outputs = tool_outputs(env.scripts.calls[message][2])
        rejected = json.loads(outputs[-1])
        assert rejected["code"] == "budget_exhausted"
        assert rejected["retry_this_turn"] is False
        assert len(env.scripts.calls[message]) <= cfg.budget.max_turns + 1
        assert "只能取得第一项" in body["delivery"]["content"]
