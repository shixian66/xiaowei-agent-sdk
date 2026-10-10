"""真 SDK 的协议错误传播：正常握手后出错，正式入口须中止，不能误当源级故障。"""

import json
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from tests.sdk_core.mcp_fixture import ASGIApp, Recorder, recording, serve
from tests.sdk_core.test_app import answer, tool_call
from tests.sdk_core.test_monitoring_r1a import SOURCE, monitoring_config
from tests.sdk_core.test_runtime import Env
from tests.sdk_core.test_runtime import env as env  # pytest fixture

pytestmark = pytest.mark.loopback


def protocol_source(recorder: Recorder, failure: str) -> ASGIApp:
    """只用标准 HTTP/JSON-RPC；公开握手协商旧 SSE 协议，不修改 SDK 内部。"""

    async def endpoint(request: Request) -> Response:
        if request.method == "DELETE":
            return Response(status_code=200)
        if request.method != "POST":
            return Response(status_code=405)
        message = await request.json()
        method = message["method"]
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "protocol-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "query",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            params = message["params"]
            recorder.tool_calls.append((params["name"], params["arguments"]))
            if failure == "accepted":
                return Response(status_code=202)
            if failure == "redirect":
                return Response(status_code=302, headers={"Location": recorder.url + "/other"})
            if failure == "sse_eof":
                return Response(content="", media_type="text/event-stream")
            if failure == "invalid_request":
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32600, "message": "synthetic invalid request"},
                    },
                    status_code=400,
                )
            size = 12_000 if failure in ("json_limit", "sse_limit") else 1
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"result": "x" * size, "warnings": None}),
                    }
                ],
                "isError": False,
            }
        else:
            return Response(status_code=202)  # initialized 通知不需要结果。
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if method == "tools/call":
            if failure == "content_type":
                return Response(content=json.dumps(reply), media_type="text/plain")
            if failure == "sse_limit":
                return Response(
                    content="event: message\ndata: " + json.dumps(reply) + "\n\n",
                    media_type="text/event-stream",
                )
        return JSONResponse(reply, headers={"Mcp-Session-Id": "synthetic-session"})

    app = Starlette(routes=[Route("/mcp", endpoint, methods=["POST", "GET", "DELETE"])])
    return recording(app, recorder, token=None)


@pytest.mark.parametrize(
    "failure",
    [
        "content_type",
        "accepted",
        "redirect",
        "json_limit",
        "sse_limit",
        "sse_eof",
        "invalid_request",
    ],
)
async def test_protocol_failure_aborts_formal_turn_without_model_continuation(
    env: Env, failure: str
) -> None:
    with serve(lambda recorder: protocol_source(recorder, failure)) as source:
        values = monitoring_config(source.url)
        values["mcp_servers"][0]["max_response_bytes"] = 2048
        async with env.running(env.config(**values)) as served:
            await served.page()
            message = env.scripts.add(
                "协议异常不能继续调查",
                tool_call(f"{SOURCE}__query", query="up"),
                lambda call: answer([], analysis="", advice="未经数据验证，无法核实当前指标"),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "failed"
            assert "本轮未交付；不会自动重试" in body["delivery"]["content"]
            assert await env.scalar("SELECT failure_code FROM xiaowei_request") == "evidence_failed"
            assert len(env.scripts.calls[message]) == 1
        assert source.recorder.tool_calls == [("query", {"query": "up"})]
        assert all(target == "/mcp" for _, target, _ in source.recorder.requests)
        assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
        assert await env.scalar("SELECT count(*) FROM agent_messages") == 0
