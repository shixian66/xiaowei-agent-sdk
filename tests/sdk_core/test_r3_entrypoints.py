"""R3 formal runtime: scripted HTTP model, real Runner/PostgreSQL/governance."""

import pytest
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.test_app import ModelCall, answer, cite, evidence_in, tool_call
from tests.sdk_core.test_runtime import Env, statements
from tests.sdk_core.test_runtime import env as env

pytestmark = pytest.mark.loopback


def brief(call: ModelCall):
    ids = evidence_in(call)
    return answer(ids, "", fact_views=[{"evidence_id": e, "view": "brief"} for e in ids])


async def test_formal_history_brief_preserves_result_without_querying_again(env: Env) -> None:
    original = env.scripts.add(
        "Read five sales rows",
        tool_call(
            "run_readonly_query",
            cluster=SR.target_id,
            sql="SELECT region, total FROM shop.sales LIMIT 5",
        ),
        cite(""),
    )
    followup = env.scripts.add("How many rows did that return?", brief)
    async with env.running(env.config()) as served:
        await served.page()
        full = (await served.turn(original, "query", "r3-full")).json()
        assert full["state"] == "completed", full
        current = full["delivery"]["facts"][0]
        result = (await served.turn(followup, "query", "r3-brief")).json()
        assert result["state"] == "completed", result
        (fact,) = result["delivery"]["facts"]
        assert fact["view"] == "brief"
        assert fact["summary"] == "\u672c\u6b21\u8fd4\u56de\u884c\u6570\uff1a2\u3002"
        for key in ("evidence_id", "captured_at", "note", "rows", "metadata", "result_json"):
            assert fact[key] == current[key]
        assert len(statements(env.drv)) == 1


async def test_formal_current_brief_keeps_complete_values(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="xiaowei.app")
    message = env.scripts.add(
        "Read new sales",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region FROM shop.sales LIMIT 5"
        ),
        brief,  # 本轮新事实收起，但原值完整；不触发回答改写
    )
    async with env.running(env.config()) as served:
        await served.page()
        body = (await served.turn(message, "query", "r3-full")).json()
        assert body["state"] == "completed", body
        assert [f["view"] for f in body["delivery"]["facts"]] == ["brief"]
        assert body["delivery"]["facts"][0]["summary"] is None
        assert body["delivery"]["facts"][0]["rows"]
        assert body["delivery"]["facts"][0]["summary"] is None
        assert "stage=answer_repair" not in caplog.text
        assert len(statements(env.drv)) == 1


async def test_formal_feishu_short_answer_uses_card_without_repeating_query(env: Env) -> None:
    from tests.sdk_core.test_feishu import FakeChannel
    from tests.sdk_core.test_feishu_render import text_parts
    from tests.sdk_core.test_runtime import feishu_config, feishu_event, until

    channel = FakeChannel()
    original = env.scripts.add(
        "Read selected sales",
        tool_call(
            "run_readonly_query",
            cluster=SR.target_id,
            sql="SELECT region, total FROM shop.sales LIMIT 5",
        ),
        cite(""),
    )
    short = env.scripts.add("How many rows came back?", brief)
    async with env.running(env.config(feishu=feishu_config()), feishu_channel=channel) as served:
        channel.emit_raw_from_sdk_thread(feishu_event(env, original, "om_r3_full"))
        await until(lambda: len(channel.sends) == 1)
        channel.emit_raw_from_sdk_thread(feishu_event(env, short, "om_r3_brief"))
        await until(lambda: len(channel.sends) == 2)
        message = channel.sends[1][1]
        visible = "\n".join(text_parts(message, folded=False))
        assert "\u672c\u6b21\u8fd4\u56de\u884c\u6570\uff1a2\u3002" in visible
        assert "\u91c7\u96c6\u4e8e" not in visible and "local/run_readonly_query" not in visible
        full = "\n".join(text_parts(message))
        assert "\u91c7\u96c6\u4e8e" in full and "local/run_readonly_query" in full
        assert "east" not in visible and "west" not in visible
        assert "east" in "\n".join(text_parts(message))
        assert len(statements(env.drv)) == 1
        assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_request WHERE delivery='sent'") == 2


@pytest.mark.parametrize("provider", ["chat_completions", "vertex"])
async def test_formal_profile_protocol_accepts_history_brief(
    env: Env,
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
):
    import json

    import httpx2
    from tests.sdk_core.test_gate0 import _evidence, _vertex_envelopes, vertex_call
    from tests.sdk_core.test_model_api import GEMINI, _chat_text, _chat_tool_call
    from tests.sdk_core.test_vertex_model import VERTEX, _answer

    profile = GEMINI if provider == "chat_completions" else VERTEX
    monkeypatch.setenv(profile.api_key_ref.removeprefix("env:"), "offline-r3-key")
    requests = []
    args = {"cluster": SR.target_id, "sql": "SELECT region FROM shop.sales LIMIT 5"}

    async def endpoint(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            response = (
                _chat_tool_call("run_readonly_query", args, "r3-call")
                if provider == "chat_completions"
                else vertex_call("run_readonly_query", **args)(body["contents"])
            )
        else:
            ids = (
                _evidence(body["messages"])
                if provider == "chat_completions"
                else [e["evidence_id"] for e in _vertex_envelopes(body["contents"])]
            )
            view = "brief" if len(requests) == 3 else "full"
            answer_body = {
                "blocks": [{"type": "fact", "evidence_id": e, "view": view} for e in ids],
                "report_format": None,
                "awaiting_user": False,
            }
            text = json.dumps(answer_body)
            response = _chat_text(text) if provider == "chat_completions" else _answer(text)
        return httpx2.Response(200, json=response)

    async with env.running(
        env.config(model=profile.model_dump(mode="json")),
        model_transport=httpx2.MockTransport(endpoint),
    ) as served:
        await served.page()
        full = (await served.turn("Read selected sales", "query", "r3-p1")).json()
        assert full["state"] == "completed", full
        assert full["delivery"]["facts"][0]["view"] == "full"
        short = (await served.turn("How many came back?", "query", "r3-p2")).json()
        assert short["state"] == "completed", short
        assert short["delivery"]["facts"][0]["view"] == "brief"
        assert len(statements(env.drv)) == 1
    assert len(requests) == 3
