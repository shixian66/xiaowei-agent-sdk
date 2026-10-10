"""R1b：断线后的下一轮按源重连，不重放失败调用。"""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from agents import Runner, UserError
from agents.mcp import MCPServerStreamableHttp
from agents.testing import ModelStep, ScriptedModel
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from sqlalchemy.engine import URL
from tests.sdk_core.mcp_fixture import (
    LOOPBACK,
    ASGIApp,
    Recorder,
    Running,
    recording,
    rerouted,
    serve,
)
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_mcp_integration import (
    FIXTURE_TARGET,
    TOKEN_ENV,
    call,
    config,
    core,
    mcp_app,
    mcp_catalog,
    mcp_context,
    names,
)
from tests.sdk_core.test_mcp_integration import (
    cite as mcp_cite,
)
from tests.sdk_core.test_monitoring_r1a import (
    SOURCE,
    monitoring_config,
    prometheus_api,
    query_server,
)
from tests.sdk_core.test_prometheus_mcp_protocol import official_server
from tests.sdk_core.test_runtime import Env, free_port
from tests.sdk_core.test_runtime import env as env  # pytest fixture

from xiaowei.mcp import _connection_lost, _Source

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MCPError(-32000, "Connection closed"), True),
        (MCPError(-32000, "synthetic closed"), False),
        (MCPError(-32000, "Connection closed extra"), False),
        (MCPError(-32000, " Connection closed"), False),
        (MCPError(-32000, "Connection closed", data={}), False),
        (MCPError(-32600, "Session terminated"), True),
        (MCPError(-32600, "synthetic session ended"), False),
        (MCPError(-32600, "Session terminated extra"), False),
        (MCPError(-32600, " Session terminated"), False),
        (MCPError(-32600, "Session terminated", data={}), False),
        (MCPError(-32001, "synthetic timeout"), False),
        (MCPError(-32603, "synthetic server error"), False),
        (httpx2.ConnectError("synthetic connect failure"), True),
        (httpx2.ReadTimeout("synthetic timeout"), False),
        (ValueError("synthetic protocol mismatch"), False),
    ],
)
def test_only_typed_connection_failures_mark_source_disconnected(
    error: Exception, expected: bool
) -> None:
    assert _connection_lost(error) is expected


def test_reconnect_backoff_caps_at_sixty_seconds_and_resets() -> None:
    source = _Source(config("http://127.0.0.1:1/mcp", tools=("lookup",)))
    for seconds in (2, 4, 8, 16, 32, 60, 60):
        source.back_off()
        assert source.retry_at - time.monotonic() == pytest.approx(seconds, abs=0.1)
    source.recovered()
    assert source.retry_at == 0
    source.back_off()
    assert source.retry_at - time.monotonic() == pytest.approx(2, abs=0.1)


class Restartable:
    def __init__(self) -> None:
        self.port = free_port()
        self._context: AbstractContextManager[Running] | None = None

    def start(self, build: Callable[[Recorder], ASGIApp] = query_server) -> Running:
        assert self._context is None
        context = serve(build, port=self.port)
        running = context.__enter__()
        self._context = context
        return running

    def stop(self) -> None:
        if self._context is not None:
            self._context.__exit__(None, None, None)
            self._context = None


async def test_stopped_mcp_connection_reports_connection_closed_code() -> None:
    source = Restartable()
    running = source.start(mcp_app())
    client = MCPServerStreamableHttp(
        params={"url": running.url, "timeout": 1},
        name="closed-session-check",
        client_session_timeout_seconds=1,
        max_retry_attempts=0,
    )
    try:
        await client.connect()
        source.stop()
        with pytest.raises(MCPError) as closed:
            await client.call_tool("lookup", {"key": "x"})
        assert closed.value.code == -32000
        assert closed.value.message == "Connection closed"
        assert closed.value.data is None
    finally:
        await client.cleanup()
        source.stop()


async def test_slow_mcp_call_reports_result_unknown_timeout_code() -> None:
    with serve(mcp_app(slow_seconds=1)) as running:
        client = MCPServerStreamableHttp(
            params={"url": running.url, "timeout": 0.2},
            name="timeout-session-check",
            client_session_timeout_seconds=0.2,
            max_retry_attempts=0,
        )
        try:
            await client.connect()
            with pytest.raises(MCPError) as timed_out:
                await client.call_tool("slow", {"key": "x"})
            assert timed_out.value.code == -32001
            assert running.recorder.tool_calls == [("slow", {"key": "x"})]
        finally:
            await client.cleanup()


async def test_formal_web_recovers_on_next_turn_without_replaying_failed_query(env: Env) -> None:
    source = Restartable()
    first = source.start()
    try:
        config = env.config(**monitoring_config(first.url))
        async with env.running(config) as served:
            await served.page()
            first_message = env.scripts.add(
                "首次查询 up", tool_call(f"{SOURCE}__query", query="up"), cite()
            )
            assert (await served.turn(first_message, "query", "r1")).json()["state"] == "completed"
            assert first.recorder.tool_calls == [("query", {"query": "up"})]

            source.stop()
            failed_message = env.scripts.add(
                "断线时查 up", tool_call(f"{SOURCE}__query", query="up")
            )
            assert (await served.turn(failed_message, "query", "r2")).json()["state"] == "failed"
            assert (await served.ready())[f"mcp.{SOURCE}"] == "disconnected"

            recovered = source.start()
            recovered_message = env.scripts.add(
                "恢复后查 up", tool_call(f"{SOURCE}__query", query="up"), cite()
            )
            response = await served.turn(recovered_message, "query", "r3")
            assert response.json()["state"] == "completed"
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
            assert recovered.recorder.tool_calls == [("query", {"query": "up"})]
            assert SOURCE in response.json()["delivery"]["content"]
    finally:
        source.stop()


@pytest.mark.skipif(not os.getenv("XW_TEST_PROMETHEUS_MCP_BIN"), reason="官方二进制需显式设置")
async def test_official_session_termination_recovers_via_formal_web(env: Env) -> None:
    binary = Path(os.environ["XW_TEST_PROMETHEUS_MCP_BIN"])
    port = free_port()
    with prometheus_api() as (backend_port, upstream_requests):
        first = official_server(binary, backend_port, port=port)
        url = first.__enter__()
        first_open = True
        try:
            config = env.config(**monitoring_config(url))
            async with env.running(config) as served:
                raw = MCPServerStreamableHttp(
                    params={"url": url, "timeout": 2},
                    name="official-session-check",
                    client_session_timeout_seconds=2,
                    max_retry_attempts=0,
                )
                try:
                    await raw.connect()
                    await served.page()
                    initial = env.scripts.add(
                        "官方源初查", tool_call(f"{SOURCE}__query", query="up"), cite()
                    )
                    assert (await served.turn(initial, "query", "official-1")).json()[
                        "state"
                    ] == "completed"
                    first.__exit__(None, None, None)
                    first_open = False
                    with official_server(binary, backend_port, port=port) as restarted:
                        assert restarted == url
                        with pytest.raises(MCPError) as ended:
                            await raw.call_tool("query", {"query": "up"})
                        assert ended.value.code == -32600
                        assert ended.value.message == "Session terminated"
                        assert ended.value.data is None
                        failed = env.scripts.add(
                            "旧会话再查", tool_call(f"{SOURCE}__query", query="up")
                        )
                        assert (await served.turn(failed, "query", "official-2")).json()[
                            "state"
                        ] == "failed"
                        assert (await served.ready())[f"mcp.{SOURCE}"] == "disconnected"
                        recovered = env.scripts.add(
                            "官方源恢复后查", tool_call(f"{SOURCE}__query", query="up"), cite()
                        )
                        answer = await served.turn(recovered, "query", "official-3")
                        assert answer.json()["state"] == "completed"
                        assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
                        assert SOURCE in answer.json()["delivery"]["content"]
                finally:
                    await raw.cleanup()
                assert len(upstream_requests) == 2
        finally:
            if first_open:
                first.__exit__(None, None, None)


async def test_ungranted_source_is_not_reconnected_after_cooldown(env: Env) -> None:
    source = Restartable()
    url = f"http://127.0.0.1:{source.port}/mcp"
    config = env.config(**monitoring_config(url, granted=False))
    async with env.running(config) as served:
        assert (await served.ready())[f"mcp.{SOURCE}"] == "unavailable"
        recovered = source.start()
        try:
            await asyncio.sleep(2.1)
            await served.page()
            message = env.scripts.add(
                "只查询本地表",
                tool_call(
                    "list_tables",
                    cluster="sr-test",
                    keyword=".",
                    database=None,
                    page_size=5,
                    cursor=None,
                ),
                cite(),
            )
            response = await served.turn(message, "query", "r-ungranted")
            assert response.json()["state"] == "completed"
            assert recovered.recorder.requests == []
            assert (await served.ready())[f"mcp.{SOURCE}"] == "unavailable"
        finally:
            source.stop()


async def test_formal_web_reconnect_failure_hides_source_but_keeps_starrocks(env: Env) -> None:
    source = Restartable()
    first = source.start()
    try:
        values = monitoring_config(first.url)
        values["mcp_servers"][0]["timeout_seconds"] = 0.3
        config = env.config(**values)
        async with env.running(config) as served:
            await served.page()
            source.stop()
            broken = env.scripts.add("源已断开", tool_call(f"{SOURCE}__query", query="up"))
            assert (await served.turn(broken, "query", "failure-1")).json()["state"] == "failed"
            assert (await served.ready())[f"mcp.{SOURCE}"] == "disconnected"

            hanging = source.start(hanging_server)
            unavailable = env.scripts.add("冷却中的源", tool_call(f"{SOURCE}__query", query="up"))
            assert (await served.turn(unavailable, "query", "failure-2")).json()[
                "state"
            ] == "failed"
            assert (await served.ready())[f"mcp.{SOURCE}"] == "cooldown"
            assert hanging.recorder.tool_calls == []

            local = env.scripts.add(
                "查本地表",
                tool_call(
                    "list_tables",
                    cluster="sr-test",
                    keyword=".",
                    database=None,
                    page_size=5,
                    cursor=None,
                ),
                cite(),
            )
            assert (await served.turn(local, "query", "failure-3")).json()["state"] == "completed"
            assert hanging.recorder.tool_calls == []
    finally:
        source.stop()


def hanging_server(recorder: Recorder, *, delay: float = 2) -> ASGIApp:
    normal = query_server(recorder)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            recorder.requests.append((scope["method"], scope["path"], None))
            await asyncio.sleep(delay)
            return
        await normal(scope, receive, send)

    return app


async def test_formal_web_starrocks_turns_back_off_and_recovery_resets_delay(env: Env) -> None:
    source = Restartable()
    first = source.start()
    try:
        values = monitoring_config(first.url)
        values["mcp_servers"][0]["timeout_seconds"] = 5
        async with env.running(env.config(**values)) as served:
            await served.page()
            source.stop()
            failed = env.scripts.add("断线查询", tool_call(f"{SOURCE}__query", query="up"))
            assert (await served.turn(failed, "query", "backoff-disconnect")).json()[
                "state"
            ] == "failed"
            assert (await served.ready())[f"mcp.{SOURCE}"] == "disconnected"
            hanging = source.start(lambda recorder: hanging_server(recorder, delay=6))

            async def local_turn(label: str) -> float:
                message = env.scripts.add(
                    label,
                    tool_call(
                        "list_tables",
                        cluster="sr-test",
                        keyword=".",
                        database=None,
                        page_size=5,
                        cursor=None,
                    ),
                    cite(),
                )
                started = time.monotonic()
                response = await served.turn(message, "query", label)
                assert response.json()["state"] == "completed", response.json()
                return time.monotonic() - started

            def attempts() -> int:
                return len(
                    [request for request in hanging.recorder.requests if request[0] == "POST"]
                )

            first_delay = await local_turn("backoff-first")
            assert attempts() == 1
            assert await local_turn("backoff-within-two-seconds") < 2
            assert attempts() == 1

            await asyncio.sleep(2.1)
            second_delay = await local_turn("backoff-second")
            assert attempts() == 2
            await asyncio.sleep(2.1)
            assert (await served.ready())[f"mcp.{SOURCE}"] == "cooldown"
            assert await local_turn("backoff-within-four-seconds") < 2
            assert attempts() == 2
            assert 2.5 < first_delay < 5
            assert 2.5 < second_delay < 5

            await asyncio.sleep(2.1)
            assert (await served.ready())[f"mcp.{SOURCE}"] == "unavailable"
            source.stop()
            source.start()
            await local_turn("backoff-recovered")
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
            # 正式入口的会话历史上限为 5 轮；续验恢复后的新故障从新会话开始。
            assert (
                await served.client.post(
                    "/api/sessions",
                    json={},
                    headers={"origin": str(served.client.base_url).rstrip("/")},
                )
            ).status_code == 200

            source.stop()
            failed_again = env.scripts.add("再次断线", tool_call(f"{SOURCE}__query", query="up"))
            assert (await served.turn(failed_again, "query", "backoff-disconnect-again")).json()[
                "state"
            ] == "failed"
            await local_turn("backoff-reset-failure")
            assert (await served.ready())[f"mcp.{SOURCE}"] == "cooldown"
            await asyncio.sleep(2.1)
            source.start()
            await local_turn("backoff-reset-recovery")
            assert (await served.ready())[f"mcp.{SOURCE}"] == "available"
    finally:
        source.stop()


async def test_concurrent_reconnect_is_single_flight_bounded_and_cools_down(
    postgres_url: URL,
) -> None:
    broken = Restartable()
    first = broken.start(mcp_app())
    try:
        with serve(mcp_app()) as healthy:
            configs = (
                config(first.url, server_id="alpha", tools=("lookup",), timeout=0.3),
                config(healthy.url, server_id="beta", tools=("lookup",)),
            )
            async with (
                core(postgres_url, mcp_catalog("alpha", "beta")) as c,
                c.integration(*configs) as integration,
            ):
                ctx = mcp_context("alpha", "beta")
                old_tools = integration.tools_for(ctx)
                broken.stop()
                await c.aborted(old_tools, "alpha__lookup", ctx)
                assert integration.source_status["alpha"] == "disconnected"

                hanging = broken.start(hanging_server)

                async def recover() -> None:
                    await integration.reconnect_for(
                        frozenset({"alpha/lookup"}),
                        target_scope=frozenset({FIXTURE_TARGET}),
                        turn_timeout_seconds=1,
                    )

                pending = [asyncio.create_task(recover()) for _ in range(12)]
                await asyncio.sleep(0.05)
                started = time.monotonic()
                await c.run(integration.tools_for(ctx), [call("beta__lookup"), mcp_cite()], ctx)
                other_elapsed = time.monotonic() - started
                await asyncio.wait_for(asyncio.gather(*pending), timeout=1)
                assert other_elapsed < 0.5
                assert integration.source_status["alpha"] == "cooldown"
                assert integration.source_status["beta"] == "available"
                posts = [request for request in hanging.recorder.requests if request[0] == "POST"]
                assert len(posts) == 1
                await recover()
                assert [
                    request for request in hanging.recorder.requests if request[0] == "POST"
                ] == posts

                broken.stop()
                recovered = broken.start(mcp_app())
                await asyncio.sleep(2.1)
                await asyncio.gather(*(recover() for _ in range(12)))
                assert integration.source_status["alpha"] == "available"
                # 一次成功握手发送 initialize 与 initialized 两个 POST。
                assert len([r for r in recovered.recorder.requests if r[0] == "POST"]) == 2
    finally:
        broken.stop()


async def test_reconnect_reloads_auth_and_missing_auth_makes_zero_remote_requests(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Restartable()
    monkeypatch.setenv(TOKEN_ENV, "synthetic-old-read-token")
    first = source.start(mcp_app(token="synthetic-old-read-token"))  # noqa: S106
    try:
        with serve(mcp_app()) as other:
            configs = (
                config(
                    first.url,
                    tools=("lookup",),
                    auth_ref=f"env:{TOKEN_ENV}",
                ),
                config(other.url, server_id="other", tools=("lookup",)),
            )
            async with (
                core(postgres_url, mcp_catalog("fixture", "other")) as c,
                c.integration(*configs) as integration,
            ):
                ctx = mcp_context("fixture", "other")
                old = integration.tools_for(ctx)
                await c.run(old, [call("fixture__lookup"), mcp_cite()], ctx)
                source.stop()
                failed_ctx = ctx.model_copy(
                    update={"identity": ctx.identity.model_copy(update={"turn_id": "auth-failed"})}
                )
                await c.aborted(old, "fixture__lookup", failed_ctx)

                new = source.start(mcp_app(token="synthetic-new-read-token"))  # noqa: S106
                monkeypatch.delenv(TOKEN_ENV)
                await integration.reconnect_for(
                    frozenset({"fixture/lookup"}),
                    target_scope=frozenset({FIXTURE_TARGET}),
                    turn_timeout_seconds=30,
                )
                assert integration.source_status["fixture"] == "cooldown"
                assert new.recorder.requests == []
                other_ctx = ctx.model_copy(
                    update={"identity": ctx.identity.model_copy(update={"turn_id": "other"})}
                )
                await c.run(
                    integration.tools_for(other_ctx), [call("other__lookup"), mcp_cite()], other_ctx
                )
                assert other.recorder.tool_calls == [("lookup", {"key": "k1"})]

                monkeypatch.setenv(TOKEN_ENV, "synthetic-new-read-token")
                await asyncio.sleep(2.1)
                await integration.reconnect_for(
                    frozenset({"fixture/lookup"}),
                    target_scope=frozenset({FIXTURE_TARGET}),
                    turn_timeout_seconds=30,
                )
                assert integration.source_status["fixture"] == "available"
                assert {authorization for _, _, authorization in new.recorder.requests} == {
                    "Bearer synthetic-new-read-token"
                }
                assert first.recorder.tool_calls == [("lookup", {"key": "k1"})]
                assert new.recorder.tool_calls == []
    finally:
        source.stop()


async def test_reconnect_exception_group_keeps_fixed_endpoint_and_enters_cooldown(
    postgres_url: URL, caplog: pytest.LogCaptureFixture
) -> None:
    source = Restartable()
    first = source.start(mcp_app())
    try:
        async with (
            core(postgres_url) as c,
            c.integration(config(first.url, tools=("lookup",))) as integration,
        ):
            ctx = mcp_context()
            source.stop()
            await c.aborted(integration.tools_for(ctx), "fixture__lookup", ctx)
            redirected = source.start(rerouted("/mcp", "/mcp?other-service=1"))
            with caplog.at_level(logging.WARNING, logger="xiaowei.mcp"):
                await integration.reconnect_for(
                    frozenset({"fixture/lookup"}),
                    target_scope=frozenset({FIXTURE_TARGET}),
                    turn_timeout_seconds=30,
                )
            assert integration.source_status["fixture"] == "cooldown"
            assert redirected.recorder.requests
            assert {target for _, target, _ in redirected.recorder.requests} == {"/mcp"}
            assert any(
                "重连失败（ExceptionGroup）" in record.getMessage()
                for record in caplog.records
                if record.name == "xiaowei.mcp"
            )
    finally:
        source.stop()


async def test_cancelled_reconnect_closes_candidate_and_allows_later_recovery(
    postgres_url: URL,
) -> None:
    source = Restartable()
    first = source.start(mcp_app())
    try:
        async with (
            core(postgres_url) as c,
            c.integration(config(first.url, tools=("lookup",), timeout=5)) as integration,
        ):
            ctx = mcp_context()
            old = integration.tools_for(ctx)
            source.stop()
            await c.aborted(old, "fixture__lookup", ctx)
            hanging = source.start(hanging_server)

            def reconnect() -> Any:
                return integration.reconnect_for(
                    frozenset({"fixture/lookup"}),
                    target_scope=frozenset({FIXTURE_TARGET}),
                    turn_timeout_seconds=10,
                )

            pending = asyncio.create_task(reconnect())
            async with asyncio.timeout(2):
                while not hanging.recorder.requests:
                    await asyncio.sleep(0.01)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert integration.source_status["fixture"] == "disconnected"
            await asyncio.sleep(2.1)
            assert hanging.open_connections() == 0

            source.stop()
            restored = source.start(mcp_app())
            await reconnect()
            assert integration.source_status["fixture"] == "available"
            assert restored.open_connections() > 0
        assert restored.open_connections() == 0
    finally:
        source.stop()


async def test_shutdown_waits_for_in_progress_reconnect_and_closes_every_connection(
    postgres_url: URL,
) -> None:
    source = Restartable()
    first = source.start(mcp_app())
    try:
        async with core(postgres_url) as c:
            integration = c.integration(config(first.url, tools=("lookup",), timeout=0.5))
            await integration.__aenter__()
            closed = False
            try:
                ctx = mcp_context()
                old = integration.tools_for(ctx)
                source.stop()
                await c.aborted(old, "fixture__lookup", ctx)
                hanging = source.start(hanging_server)
                pending = asyncio.create_task(
                    integration.reconnect_for(
                        frozenset({"fixture/lookup"}),
                        target_scope=frozenset({FIXTURE_TARGET}),
                        turn_timeout_seconds=2,
                    )
                )
                async with asyncio.timeout(2):
                    while not hanging.recorder.requests:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(integration.__aexit__(None, None, None), timeout=2)
                closed = True
                await pending
                await asyncio.sleep(2.1)
                assert hanging.open_connections() == 0
            finally:
                if not closed:
                    await integration.__aexit__(None, None, None)
    finally:
        source.stop()


def drifted_lookup(recorder: Recorder) -> ASGIApp:
    server = MCPServer("changed-contract")

    @server.tool()
    def lookup(key: int) -> dict[str, object]:
        recorder.tool_calls.append(("lookup", {"key": key}))
        return {"key": str(key), "value": 1, "note": "drifted"}

    return recording(server.streamable_http_app(host=LOOPBACK), recorder, token=None)


@pytest.mark.parametrize("schema_drift", [False, True], ids=["compatible", "schema-drift"])
async def test_old_runner_tool_stays_on_old_connection_after_reconnect(
    postgres_url: URL, schema_drift: bool
) -> None:
    source = Restartable()
    first = source.start(mcp_app())
    try:
        async with (
            core(postgres_url) as c,
            c.integration(config(first.url, tools=("lookup",))) as integration,
        ):
            first_ctx = mcp_context()
            old_tools = integration.tools_for(first_ctx)
            assert names(old_tools) == {"fixture__lookup"}
            first_done = asyncio.Event()
            continue_turn = asyncio.Event()

            async def second_call(_call: Any) -> Any:
                first_done.set()
                await continue_turn.wait()
                return call("fixture__lookup", "k2", "second")

            model = ScriptedModel(
                [call("fixture__lookup", "k1", "first"), ModelStep(responder=second_call)]
            )
            turn = asyncio.create_task(
                Runner.run(c.agent(model, old_tools), "查两次", context=first_ctx)
            )
            try:
                await asyncio.wait_for(first_done.wait(), timeout=3)
                assert first.recorder.tool_calls == [("lookup", {"key": "k1"})]

                source.stop()
                failed_ctx = first_ctx.model_copy(
                    update={"identity": first_ctx.identity.model_copy(update={"turn_id": "failed"})}
                )
                await c.aborted(old_tools, "fixture__lookup", failed_ctx)
                assert integration.source_status == {"fixture": "disconnected"}

                changed = source.start(drifted_lookup if schema_drift else mcp_app())
                await integration.reconnect_for(
                    frozenset({"fixture/lookup"}),
                    target_scope=frozenset({FIXTURE_TARGET}),
                    turn_timeout_seconds=30,
                )
                assert integration.source_status == {
                    "fixture": "contract_mismatch" if schema_drift else "available"
                }
                assert names(integration.tools_for(first_ctx)) == (
                    set() if schema_drift else {"fixture__lookup"}
                )
                assert names(old_tools) == {"fixture__lookup"}
                continue_turn.set()
                with pytest.raises(UserError):
                    await turn
                assert len(model.calls) == 2
                assert changed.recorder.tool_calls == []
                assert integration.source_status == {
                    "fixture": "contract_mismatch" if schema_drift else "available"
                }
            finally:
                continue_turn.set()
                if not turn.done():
                    turn.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await turn

            if schema_drift:
                source.stop()
                fixed = source.start(mcp_app())
                await asyncio.sleep(2.1)
                await integration.reconnect_for(
                    frozenset({"fixture/lookup"}),
                    target_scope=frozenset({FIXTURE_TARGET}),
                    turn_timeout_seconds=30,
                )
                assert names(integration.tools_for(first_ctx)) == {"fixture__lookup"}
                assert fixed.recorder.tool_calls == []
    finally:
        source.stop()
