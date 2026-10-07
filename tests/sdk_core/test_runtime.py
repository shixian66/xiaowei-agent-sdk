"""P1-B Task 8：正式运行装配 ``xiaowei.runtime``。

真实 PostgreSQL（隔离数据库）+ 正式装配顺序（实例锁、启动恢复、StarRocks 工具、唯一授权来源、
Evidence、Application、ChannelService）+ 真实 Uvicorn 监听 loopback。只替换最底层 I/O：模型是
HTTP mock 脚本（``model_transport``），StarRocks 是驱动替身（``starrocks_connect``），飞书是
SDK 公开面替身（``feishu_channel``）。

替身不能证明：真实模型、真实 StarRocks、真实飞书长连接与发送（Task 9，需授权）；CLI 参数、环境
变量强制与信号处理由 ``test_cli.py`` 的子进程用例覆盖。
"""

import asyncio
import copy
import json
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from asyncmy.errors import OperationalError, ProgrammingError
from lark_channel.channel.errors import FeishuChannelErrorCode, SendError
from lark_channel.channel.types import SendResult
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import URL
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.p1b.test_starrocks_adapter import Result, driver, schema_results
from tests.sdk_core.synthetic_tools import Clock
from tests.sdk_core.test_app import (
    SEARCH_ALL,
    Scripts,
    after,
    cite,
    clarify,
    tool_call,
    upstream_error,
)
from tests.sdk_core.test_feishu import FakeChannel
from tests.sdk_core.test_gate0 import VertexLikeEndpoint, _vertex_body, vertex_call
from tests.sdk_core.test_model_api import (
    DEEPSEEK,
    GEMINI,
    _chat_tool_call,
    _responses_tool_call,
    _text,
)
from tests.sdk_core.test_model_api import OPENAI as PROFILE
from tests.sdk_core.test_model_api import Endpoint as ChatEndpoint
from tests.sdk_core.test_vertex_model import VERTEX

from xiaowei import runtime
from xiaowei.channel import AccessDeniedError, ResultUnavailableError
from xiaowei.channel_store import ChannelStore, RequestUnavailableError
from xiaowei.config import SecretRefError
from xiaowei.model_api import ModelProfile
from xiaowei.models import AUDIENCES, Identity
from xiaowei.starrocks import EXPLAIN_PREFIX, StarRocksAdapter
from xiaowei.starrocks_schema import SchemaCache
from xiaowei.starrocks_tools import (
    AUDIT_NOTE,
    AUDIT_TOOLS,
    DIAGNOSE_TOOLS,
    PLAN_NOTE,
    QUERY_TOOLS,
    SLOW_QUERIES,
    starrocks_tools,
)
from xiaowei.storage import (
    Readiness,
    StorageBusyError,
    StorageDigestKeyMismatchError,
    StorageNotInitializedError,
    hold_instance_lock,
    initialize_storage,
    open_engine,
)

pytestmark = pytest.mark.loopback

DB_ENV, KEY_ENV, FEISHU_ENV = "XW_TEST_DATABASE_URL", "XW_TEST_DIGEST_KEY", "XW_TEST_FEISHU_SECRET"
MODEL_ENV = PROFILE.api_key_ref.removeprefix("env:")
SR_ENV = SR.password_ref.removeprefix("env:")
OPERATOR = "operator"
SALES = Result(("region", "total"), [("east", 100), ("west", 50)])
APP_ID, TENANT = "cli_test", "tenant-1"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


BUDGET_CONFIG: dict[str, Any] = {
    "max_turns": 6,
    "max_tool_calls": 3,
    "timeout_seconds": 30,
    "max_scope_checks": 1000,
}


def target_config(
    starrocks: dict[str, Any] | None = None, business_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """``targets`` 中的一项：默认是 Task 2 驱动替身对应的合成 StarRocks 目标。"""
    return {
        "type": "starrocks",
        "description": "合成销售库",
        "business_context": business_context,
        "starrocks": starrocks or SR.model_dump(mode="json"),
    }


def serve_config(port: int, **overrides: Any) -> dict[str, Any]:
    """与正式 JSON 配置同构的字典；凭据只以 env: 引用出现。

    ``starrocks`` 覆盖唯一默认目标的连接配置；多目标用 ``targets`` 整体覆盖。
    """
    starrocks = overrides.pop("starrocks", None)
    values: dict[str, Any] = {
        "storage": {
            "database_url_ref": f"env:{DB_ENV}",
            "digest_key_ref": f"env:{KEY_ENV}",
            "evidence_retention_seconds": 3600,
            "request_retention_seconds": 3600,
            "max_answer_bytes": 20_000,
        },
        "model": PROFILE.model_dump(mode="json"),
        "data_policy": {"input": {"max_bytes": 2000}, "model_tools": sorted(QUERY_TOOLS)},
        "session_limits": {
            "max_history_turns": 5,
            "max_history_bytes": 200_000,
            "retention_seconds": 7200,
        },
        "budget": BUDGET_CONFIG,
        "max_concurrent_turns": 4,
        "targets": [target_config(starrocks)],
        "projection_bytes": dict.fromkeys(AUDIENCES, 60_000),
        "access": {
            "policy_version": "p1",
            "grants": {OPERATOR: sorted(QUERY_TOOLS), "alice": sorted(QUERY_TOOLS)},
        },
        "web": {
            "operator_id": OPERATOR,
            "allowed_origins": [f"http://127.0.0.1:{port}"],
            "secure_cookie": False,
            "max_body_bytes": 65_536,
        },
        "listen_port": port,
        "shutdown_timeout_seconds": 5,
        "lock_check_seconds": 0.2,
    }
    values.update(overrides)
    return values


def feishu_config(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "app_id": APP_ID,
        "app_secret_ref": f"env:{FEISHU_ENV}",
        "tenant_key": TENANT,
        "users": {"ou_alice": "alice"},
        "max_event_age_seconds": 300,
        "max_message_chars": 1000,
        "max_reply_chars": 2000,
        "queue_size": 4,
        "consumer_count": 1,
        "send_timeout_seconds": 5,
        "connect_timeout_seconds": 5,
        "stop_timeout_seconds": 5,
    }
    values.update(overrides)
    return values


@dataclass
class Env:
    url: URL
    port: int
    scripts: Scripts = field(default_factory=Scripts)
    clock: Clock = field(default_factory=Clock)
    drv: Any = field(default_factory=lambda: driver(SALES))

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def config(self, **overrides: Any) -> runtime.ServeConfig:
        return runtime.ServeConfig.model_validate(serve_config(self.port, **overrides))

    def connect(self, config: runtime.ServeConfig) -> dict[str, Any]:
        """各目标都连到同一个驱动替身。"""
        return {t.target_id: self.drv for t in config.targets}

    async def serve(self, config: runtime.ServeConfig, stop: asyncio.Event, **kw: Any) -> int:
        return await runtime.serve(
            config,
            stop=stop,
            clock=self.clock,
            **{
                "model_transport": self.scripts.transport(),
                "starrocks_connect": self.connect(config),
                **kw,
            },
        )

    @asynccontextmanager
    async def running(self, config: runtime.ServeConfig, **kw: Any) -> AsyncIterator["Served"]:
        stop = asyncio.Event()
        task = asyncio.create_task(self.serve(config, stop, **kw))
        async with httpx.AsyncClient(base_url=self.origin, timeout=30) as client:
            served = Served(client, task, stop)
            await served.ready()
            # 启动时各目标先刷新一次结构快照；用例只统计请求本身到达驱动的 I/O。
            for drv in (kw.get("starrocks_connect") or {"default": self.drv}).values():
                forget(drv)
            try:
                yield served
            finally:
                stop.set()
                await asyncio.wait({task}, timeout=30)

    async def scalar(self, sql: str) -> Any:
        async with open_engine(SecretStr(self.url.render_as_string(hide_password=False))) as e:
            async with e.connect() as conn:
                return await conn.scalar(text(sql))


def forget(drv: Any) -> None:
    """清掉驱动替身已有的连接与语句记录（启动时结构快照刷新留下的）。"""
    drv.attempts = 0
    for name in ("connections", "statements"):
        if hasattr(drv, name):
            getattr(drv, name).clear()


@dataclass
class Served:
    client: httpx.AsyncClient
    task: asyncio.Task[int]
    stop: asyncio.Event

    async def ready(self) -> dict[str, Any]:
        async with asyncio.timeout(20):
            while True:
                if self.task.done():
                    pytest.fail(f"serve 提前结束：{self.task.result()}")
                try:
                    response = await self.client.get("/readyz")
                except httpx.TransportError:
                    await asyncio.sleep(0.05)
                    continue
                body: dict[str, Any] = response.json()
                return body

    async def page(self) -> None:
        assert (await self.client.get("/")).status_code == 200

    async def turn(self, message: str, mode: str = "diagnose", request_id: str = "r1") -> Any:
        response = await self.client.post(
            "/api/turns",
            json={"request_id": request_id, "mode": mode, "message": message},
            headers={"origin": str(self.client.base_url).rstrip("/")},
        )
        return response

    async def finish(self) -> int:
        self.stop.set()
        return await asyncio.wait_for(self.task, 30)


@pytest.fixture
async def env(postgres_url: URL, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    monkeypatch.setenv(DB_ENV, postgres_url.render_as_string(hide_password=False))
    monkeypatch.setenv(KEY_ENV, "runtime-test-digest-key")
    monkeypatch.setenv(MODEL_ENV, "sk-test-runtime-model")
    monkeypatch.setenv(FEISHU_ENV, "feishu-test-secret")
    async with open_engine(SecretStr(postgres_url.render_as_string(hide_password=False))) as e:
        await initialize_storage(e, digest_key=SecretStr("runtime-test-digest-key"))
    yield Env(postgres_url, free_port())


async def lock_is_free(env: Env) -> bool:
    secret = SecretStr(env.url.render_as_string(hide_password=False))
    async with open_engine(secret) as engine:
        try:
            async with hold_instance_lock(engine, Readiness()):
                return True
        except StorageBusyError:
            return False


@contextmanager
def port_is_released(port: int) -> Iterator[None]:
    """结束后没有进程在监听该端口（连接被拒绝）；TIME_WAIT 不影响这一判断。"""
    yield
    with socket.socket() as probe, pytest.raises(ConnectionRefusedError):
        probe.connect(("127.0.0.1", port))


# ---- 成功路径 ------------------------------------------------------------------------


async def test_formal_assembly_answers_web_turns_and_stops_cleanly(env: Env) -> None:
    config = env.config()
    with port_is_released(env.port):
        async with env.running(config) as served:
            assert await served.ready() == {"status": "ready", "feishu": "disabled"}
            await served.page()
            diagnose = env.scripts.add(
                "诊断表结构", tool_call("list_tables", cluster=SR.target_id, **SEARCH_ALL), cite()
            )
            response = await served.turn(diagnose)
            assert response.status_code == 200 and response.json()["state"] == "completed"
            assert all(
                "run_readonly_query" not in seen for seen in env.scripts.tools_seen(diagnose)
            )

            query = env.scripts.add(
                "各地区销售额",
                tool_call(
                    "run_readonly_query",
                    cluster=SR.target_id,
                    sql="SELECT region, total FROM sales",
                ),
                cite(),
            )
            body = (await served.turn(query, "query", "r2")).json()
            assert body["state"] == "completed"
            facts = body["delivery"]["facts"]
            (fact,) = [f for f in facts if f["tool_id"] == "local/run_readonly_query"]
            assert fact["rows"] == [
                {"region": "east", "total": 100},
                {"region": "west", "total": 50},
            ]
            assert not await lock_is_free(env)  # 运行中持有实例锁
            assert await served.finish() == 0
    assert await lock_is_free(env)
    assert await env.scalar("SELECT count(*) FROM xiaowei_request WHERE state = 'completed'") == 2


async def test_container_entry_binds_inside_the_container_without_changing_json(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[tuple[str, int]] = []
    real_bind = runtime._bind

    def bind_inside_container(host: str, port: int) -> socket.socket:
        requested.append((host, port))
        return real_bind("127.0.0.1", port)

    monkeypatch.setattr(runtime, "_bind", bind_inside_container)
    stop = asyncio.Event()
    stop.set()
    config = env.config()
    result = await runtime._container_serve(
        config,
        stop=stop,
        clock=env.clock,
        model_transport=env.scripts.transport(),
        starrocks_connect=env.connect(config),
    )

    assert result == 0 and config.listen_host == "127.0.0.1"
    assert requested == [("0.0.0.0", env.port)]  # noqa: S104 - 断言容器专用绑定


async def test_native_runtime_requests_only_the_configured_loopback_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = 18501
    config = runtime.ServeConfig.model_validate(serve_config(port))
    requested: list[tuple[str, int]] = []

    def reject_after_recording(host: str, port: int) -> socket.socket:
        requested.append((host, port))
        raise runtime.ListenError(host, port)

    monkeypatch.setattr(runtime, "_bind", reject_after_recording)
    with pytest.raises(runtime.ListenError, match="无法监听"):
        await runtime.serve(config, stop=asyncio.Event())

    assert requested == [("127.0.0.1", port)]


async def test_feishu_consumer_cancellation_is_bounded_and_marks_stop_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def resistant_consumer() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    class Gateway:
        async def drain(self, timeout: float) -> bool:
            del timeout
            return True

    class Transport:
        stops = 0

        async def stop(self) -> bool:
            self.stops += 1
            return True

    monkeypatch.setattr(runtime, "_FEISHU_CONSUMER_CANCEL_TIMEOUT_SECONDS", 0.01, raising=False)
    consumers = asyncio.create_task(resistant_consumer())
    await entered.wait()
    transport = Transport()
    stopping = asyncio.create_task(
        runtime._stop_feishu(
            runtime.ServeConfig.model_validate(serve_config(18502, feishu=feishu_config())),
            runtime._Feishu(Gateway(), transport, consumers),  # type: ignore[arg-type]
        )
    )
    try:
        assert await asyncio.wait_for(asyncio.shield(stopping), 0.2) is False
        assert transport.stops == 1
    finally:
        release.set()
        if not stopping.done():
            stopping.cancel()
        await asyncio.gather(stopping, consumers, return_exceptions=True)


PLAN = Result(("Explain String",), [("- Output => [1:region]",), ("    - SCAN [sales]",)])


def statements(drv: Any) -> list[str]:
    """到达驱动的业务与元数据语句（不含会话设置与回读、结构快照读取与零行权限探测）。"""
    return [
        sql
        for conn in drv.connections
        for sql, _ in conn.executed
        if not sql.startswith(("SET ", "SELECT @@", "SELECT 1 FROM `")) and sql not in SCHEMA_SQL
    ]


SCHEMA_SQL = frozenset(schema_results()) - {"*"}


async def test_formal_assembly_diagnoses_with_a_plan_and_never_runs_the_query(env: Env) -> None:
    env.drv = driver(PLAN)
    async with env.running(env.config()) as served:
        await served.page()
        message = env.scripts.add(
            "为什么按地区查询慢",
            tool_call("explain_query", cluster=SR.target_id, sql="SELECT region, total FROM sales"),
            cite("扫描了整张表"),
        )
        body = (await served.turn(message)).json()
        assert body["state"] == "completed"
        (seen,) = env.scripts.tools_seen(message)[:1]
        assert "explain_query" in seen and "run_readonly_query" not in seen

        (sent,) = statements(env.drv)
        assert sent.startswith(EXPLAIN_PREFIX) and "LIMIT" not in sent
        (fact,) = body["delivery"]["facts"]
        assert fact["tool_id"] == "local/explain_query" and fact["note"] == PLAN_NOTE
        assert fact["rows"] == [{"plan": "- Output => [1:region]"}, {"plan": "    - SCAN [sales]"}]
        assert f"说明：{PLAN_NOTE}" in body["delivery"]["content"]
        assert await served.finish() == 0


async def test_formal_assembly_rejects_out_of_scope_explain_without_io(env: Env) -> None:
    env.drv = driver(PLAN)
    async with env.running(env.config()) as served:
        await served.page()
        message = env.scripts.add(
            "看看 secret 列的计划",
            tool_call("explain_query", cluster=SR.target_id, sql="SELECT secret FROM sales"),
            clarify("secret 列不在可查看范围内"),
        )
        body = (await served.turn(message)).json()
        # 越权 SQL 在任何连接之前被拒绝，拒绝原因交给模型；没有可用证据时只返回澄清。
        assert body["state"] == "completed"
        assert body["delivery"]["facts"] == [] and "需要澄清" in body["delivery"]["content"]
        assert env.drv.attempts == 0
        assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


LAYOUT = Result(
    (
        "model",
        "partition_key",
        "distribute_type",
        "distribute_key",
        "buckets",
        "sort_key",
        "primary_key",
    ),
    [("DUP_KEYS", "`region`", "HASH", "`id`", 4, "`region`", "")],
)


async def test_formal_assembly_cites_table_layout_with_hidden_keys(env: Env) -> None:
    env.drv = driver(LAYOUT)
    async with env.running(env.config()) as served:
        await served.page()
        message = env.scripts.add(
            "sales 表的分桶合理吗",
            tool_call(
                "describe_table_layout", cluster=SR.target_id, database="shop", table="sales"
            ),
            cite(),
        )
        body = (await served.turn(message)).json()
        assert body["state"] == "completed"
        (fact,) = body["delivery"]["facts"]
        assert fact["tool_id"] == "local/describe_table_layout"
        # SR 的 sales 不开放 id 列：分桶键整段不显示。
        assert fact["rows"] == [
            {
                "model": "DUP_KEYS",
                "partition_key": "region",
                "distribute_type": "HASH",
                "distribute_key": "（含未获准列，未显示）",
                "buckets": 4,
                "sort_key": "region",
                "primary_key": "",
            }
        ]
        (sent,) = statements(env.drv)
        assert "information_schema.tables_config" in sent and "PROPERTIES" not in sent
        assert await served.finish() == 0


async def test_formal_assembly_rejects_layout_of_unlisted_tables_without_io(env: Env) -> None:
    env.drv = driver(LAYOUT)
    async with env.running(env.config()) as served:
        await served.page()
        message = env.scripts.add(
            "users 表的布局",
            tool_call(
                "describe_table_layout", cluster=SR.target_id, database="shop", table="users"
            ),
            clarify("users 不在可查看范围内"),
        )
        body = (await served.turn(message)).json()
        assert body["state"] == "completed"
        assert body["delivery"]["facts"] == [] and "需要澄清" in body["delivery"]["content"]
        assert env.drv.attempts == 0
        assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


AUDIT_SOURCE = (
    "queryId",
    "timestamp",
    "queryTime",
    "scanBytes",
    "scanRows",
    "returnRows",
    "cpuCostNs",
    "memCostBytes",
    "pendingTimeMs",
    "state",
    "digest",
    "db",
    "stmt",
)


def audit_record(stmt: str, query_id: str, db: str = "shop") -> tuple[object, ...]:
    started = datetime(2026, 9, 29, 23, 30)
    return (query_id, started, 7000, 2048, 300, 2, 5_000_000, 8192, 0, "EOF", "d1", db, stmt)


async def test_formal_assembly_lists_only_approved_slow_queries(env: Env) -> None:
    env.drv = driver(
        Result(
            AUDIT_SOURCE,
            [
                audit_record("SELECT secret FROM sales", "q-hidden"),
                audit_record("SELECT region, total FROM sales", "q-ok"),
            ],
        )
    )
    config = runtime.ServeConfig.model_validate(audit_config(env.port))
    async with env.running(config) as served:
        await served.page()
        message = env.scripts.add(
            "最近一小时的慢查询",
            tool_call(
                "list_slow_queries",
                cluster=SR.target_id,
                window_minutes=60,
                order_by="query_time",
                database=None,
            ),
            cite(),
        )
        body = (await served.turn(message)).json()
        assert body["state"] == "completed"
        (fact,) = body["delivery"]["facts"]
        assert fact["tool_id"] == SLOW_QUERIES and fact["note"] == AUDIT_NOTE
        assert [row["query_id"] for row in fact["rows"]] == ["q-ok"]
        assert "secret" not in json.dumps(body, ensure_ascii=False)
        assert await served.finish() == 0


async def test_formal_assembly_stops_the_turn_when_the_audit_table_is_missing(env: Env) -> None:
    env.drv = driver(ProgrammingError(5502, "Unknown table canary"))
    config = runtime.ServeConfig.model_validate(audit_config(env.port))
    async with env.running(config) as served:
        await served.page()
        message = env.scripts.add(
            "最近一小时的慢查询",
            tool_call(
                "list_slow_queries",
                cluster=SR.target_id,
                window_minutes=60,
                order_by="query_time",
                database=None,
            ),
            cite(),
        )
        body = (await served.turn(message)).json()
        assert body["state"] == "failed" and "canary" not in json.dumps(body)
        assert env.drv.attempts == 1  # 不重试
        assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_model_failure_is_a_fixed_receipt_through_the_formal_assembly(env: Env) -> None:
    async with env.running(env.config()) as served:
        await served.page()
        response = await served.turn(env.scripts.add("上游报错", upstream_error))
        body = response.json()
        assert body["state"] == "failed" and "模型未能完成本轮" in body["delivery"]["content"]
        assert await served.finish() == 0


def _vertex_cite_seen(contents: list[dict[str, Any]]) -> dict[str, Any]:
    """引用 Vertex 请求中（本轮与回放历史）全部工具结果的证据。"""
    ids = [
        json.loads(part["functionResponse"]["response"]["output"])["evidence_id"]
        for content in contents
        for part in content["parts"]
        if "functionResponse" in part
    ]
    body = {
        "evidence_ids": ids,
        "inferences": [{"text": "数据平稳", "evidence_ids": ids}],
        "clarification": None,
    }
    return _vertex_body([{"text": json.dumps(body, ensure_ascii=False)}])


async def test_formal_assembly_replays_vertex_signatures_across_web_turns(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正式入口用 Vertex Profile：第一轮工具往返，追问时端点核对上一轮调用的签名。"""
    monkeypatch.setenv(VERTEX.api_key_ref.removeprefix("env:"), "fake-runtime-vertex-key")
    query, followup = "各地区销售额", "刚才的结果？"
    sql = "SELECT region, total FROM sales"
    endpoint = VertexLikeEndpoint(
        replies={
            query: [
                vertex_call("run_readonly_query", cluster=SR.target_id, sql=sql),
                _vertex_cite_seen,
            ],
            followup: [_vertex_cite_seen],
        }
    )
    config = env.config(model=VERTEX.model_dump(mode="json"))
    async with env.running(config, model_transport=endpoint.transport()) as served:
        await served.page()
        first = (await served.turn(query, "query", "r1")).json()
        assert first["state"] == "completed", first
        ran = statements(env.drv)
        assert len(ran) == 1
        second = (await served.turn(followup, "query", "r2")).json()
        assert second["state"] == "completed", second
        assert statements(env.drv) == ran  # 追问不重跑查询
        assert await served.finish() == 0
    (signature,) = endpoint.issued.values()
    assert signature not in json.dumps([first, second], ensure_ascii=False)


async def test_vertex_signature_rejection_is_a_fixed_receipt(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游拒绝签名（400）：正式入口给出固定回执，不含签名或上游信息，工具不重跑。"""
    monkeypatch.setenv(VERTEX.api_key_ref.removeprefix("env:"), "fake-runtime-vertex-key")
    query = "各地区销售额"
    sql = "SELECT region, total FROM sales"
    endpoint = VertexLikeEndpoint(
        replies={query: [vertex_call("run_readonly_query", cluster=SR.target_id, sql=sql)]}
    )

    async def reject_replayed_signature(request: httpx2.Request) -> httpx2.Response:
        if len(json.loads(request.content)["contents"]) > 1:
            error = {"code": 400, "message": "upstream-signature-detail", "status": "X"}
            return httpx2.Response(400, json={"error": error})
        return await endpoint.transport().handle_async_request(request)

    config = env.config(model=VERTEX.model_dump(mode="json"))
    transport = httpx2.MockTransport(reject_replayed_signature)
    async with env.running(config, model_transport=transport) as served:
        await served.page()
        body = (await served.turn(query, "query", "r1")).json()
        assert body["state"] == "failed" and "模型未能完成本轮" in body["delivery"]["content"]
        assert len(statements(env.drv)) == 1  # 工具执行一次，不重跑
        assert await served.finish() == 0
    (signature,) = endpoint.issued.values()
    exposed = json.dumps(body, ensure_ascii=False)
    assert signature not in exposed and "upstream-signature-detail" not in exposed


async def test_startup_recovers_interrupted_requests_before_serving(env: Env) -> None:
    secret = SecretStr(env.url.render_as_string(hide_password=False))
    async with open_engine(secret) as engine:
        store = ChannelStore(
            engine,
            key=SecretStr("runtime-test-digest-key"),
            clock=env.clock,
            readiness=Readiness(),
            request_retention_seconds=3600,
            session_retention_seconds=7200,
            evidence_retention_seconds=3600,
            max_answer_bytes=20_000,
        )
        acceptance = await store.accept(
            channel="web",
            subject_id=OPERATOR,
            conversation="c" * 43,
            request_id="crashed",
            mode="diagnose",
            message="上次进程崩溃时的请求",
            policy_version="p1",
        )
        await store.start(acceptance.record, message="上次进程崩溃时的请求", policy_version="p1")
    async with env.running(env.config()) as served:
        await served.ready()
        state = await env.scalar("SELECT state FROM xiaowei_request")
        assert state == "interrupted"
        assert await served.finish() == 0
    assert env.scripts.calls == {}  # 恢复不重跑


# ---- model check：既有 Provider 走同一条窄装配 -------------------------------------------

CHECK_ADVICE = {"evidence_ids": [], "inferences": [], "clarification": None, "advice": "数值为 42"}


def _check_config(profile: ModelProfile) -> runtime.ServeConfig:
    values = serve_config(18501, model=profile.model_dump(mode="json"))
    return runtime.ServeConfig.model_validate(values)


def _check_call(profile: ModelProfile) -> dict[str, Any]:
    build = _responses_tool_call if profile.api_mode == "responses" else _chat_tool_call
    return build("model_check_lookup", {"region": "east"}, "call-1")


@pytest.mark.parametrize("profile", [PROFILE, GEMINI, DEEPSEEK], ids=["responses", "chat", "json"])
async def test_model_check_runs_on_existing_providers(
    profile: ModelProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据库、摘要与 StarRocks 环境均未设置：检查只用模型 Key 与一次工具往返。"""
    for name in (DB_ENV, KEY_ENV, SR_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(profile.api_key_ref.removeprefix("env:"), "fake-check-key")
    endpoint = ChatEndpoint(
        [_check_call(profile), _text(profile, json.dumps(CHECK_ADVICE, ensure_ascii=False))]
    )

    await runtime.check_model(_check_config(profile), transport=endpoint.transport)

    assert len(endpoint.requests) == 2
    assert endpoint.requests[0].headers["authorization"] == "Bearer fake-check-key"


@pytest.mark.parametrize(
    ("status", "reason"), [(401, "auth_failed"), (429, "rate_limited"), (500, "upstream_error")]
)
async def test_model_check_classifies_openai_client_status_errors(
    monkeypatch: pytest.MonkeyPatch, status: int, reason: str
) -> None:
    monkeypatch.setenv(GEMINI.api_key_ref.removeprefix("env:"), "fake-check-key")
    endpoint = ChatEndpoint([httpx2.Response(status, json={"error": {"message": "upstream"}})])

    with pytest.raises(runtime.ModelCheckError) as raised:
        await runtime.check_model(_check_config(GEMINI), transport=endpoint.transport)

    assert raised.value.reason == reason and str(raised.value) == reason
    assert len(endpoint.requests) == 1  # 不重试


# ---- 启动失败：不对外服务，逆序释放 ----------------------------------------------------


async def test_second_instance_is_refused_and_releases_its_port(env: Env) -> None:
    secret = SecretStr(env.url.render_as_string(hide_password=False))
    async with open_engine(secret) as engine, hold_instance_lock(engine, Readiness()):
        with port_is_released(env.port), pytest.raises(StorageBusyError):
            await env.serve(env.config(), asyncio.Event())


async def test_uninitialized_database_is_refused(
    env: Env, postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = SecretStr(env.url.render_as_string(hide_password=False))
    async with open_engine(secret) as engine, engine.begin() as conn:
        await conn.execute(text("DROP TABLE xiaowei_request"))
    with pytest.raises(StorageNotInitializedError):
        await env.serve(env.config(), asyncio.Event())
    assert await lock_is_free(env)


async def test_digest_key_mismatch_is_refused_before_recovery_or_external_io(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = SecretStr(env.url.render_as_string(hide_password=False))
    async with open_engine(secret) as engine:
        store = ChannelStore(
            engine,
            key=SecretStr("runtime-test-digest-key"),
            clock=env.clock,
            readiness=Readiness(),
            request_retention_seconds=3600,
            session_retention_seconds=7200,
            evidence_retention_seconds=3600,
            max_answer_bytes=20_000,
        )
        accepted = await store.accept(
            channel="web",
            subject_id=OPERATOR,
            conversation="c" * 43,
            request_id="must-not-recover",
            mode="diagnose",
            message="不能被恢复",
            policy_version="p1",
        )
        await store.start(accepted.record, message="不能被恢复", policy_version="p1")

    monkeypatch.setenv(KEY_ENV, "different-digest-key")
    forget(env.drv)
    with port_is_released(env.port), pytest.raises(StorageDigestKeyMismatchError):
        await env.serve(env.config(), asyncio.Event())

    assert await env.scalar("SELECT state FROM xiaowei_request") == "running"
    assert env.drv.attempts == 0 and env.scripts.calls == {}
    assert await lock_is_free(env)


async def test_partial_startup_failure_releases_the_lock(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(MODEL_ENV)  # 模型凭据在锁与恢复之后才解析
    with port_is_released(env.port), pytest.raises(SecretRefError):
        await env.serve(env.config(), asyncio.Event())
    assert await lock_is_free(env)


async def test_stop_during_startup_recovery_cancels_before_external_io(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()

    async def hanging_recovery(self: ChannelStore, lock: object) -> None:
        del self, lock
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ChannelStore, "recover", hanging_recovery)
    stop = asyncio.Event()
    with port_is_released(env.port):
        task = asyncio.create_task(env.serve(env.config(), stop))
        await asyncio.wait_for(entered.wait(), 5)
        stop.set()
        assert await asyncio.wait_for(task, 3) == 0

    assert env.drv.attempts == 0 and env.scripts.calls == {}
    assert await lock_is_free(env)


async def test_stop_during_initial_schema_refresh_cancels_startup(env: Env) -> None:
    starrocks = SR.model_dump(mode="json")
    starrocks["connect_timeout_seconds"] = 60
    starrocks["schema_limits"] = {
        **starrocks["schema_limits"],
        "refresh_seconds": 60,
        "refresh_timeout_seconds": 60,
    }
    config = env.config(starrocks=starrocks)
    hanging = driver(SALES)
    hanging.connect_hang = True
    stop = asyncio.Event()
    with port_is_released(env.port):
        task = asyncio.create_task(
            runtime.serve(
                config,
                stop=stop,
                clock=env.clock,
                model_transport=env.scripts.transport(),
                starrocks_connect={SR.target_id: hanging},
            )
        )
        await until(lambda: hanging.attempts > 0)
        stop.set()
        assert await asyncio.wait_for(task, 3) == 0
    assert await lock_is_free(env)


async def test_schema_close_timeout_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingCloseSchema:
        async def refresh(self) -> None:
            return None

        async def run(self) -> None:
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "_SCHEMA_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def close_schema() -> None:
        with pytest.raises(runtime.RuntimeCloseError, match="结构刷新关闭超时"):
            async with runtime._refreshing(  # type: ignore[dict-item]
                {"synthetic": HangingCloseSchema()}
            ):
                pass

    await asyncio.wait_for(close_schema(), 0.2)


@dataclass
class HangingStartChannel(FakeChannel):
    """飞书启动永不完成；只用于证明启动阶段也响应停止信号。"""

    entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def start_background(self, *, timeout: float) -> None:
        self.entered.set()
        await asyncio.Event().wait()


async def test_stop_during_feishu_connection_start_closes_transport_and_runtime(env: Env) -> None:
    channel = HangingStartChannel()
    stop = asyncio.Event()
    task: asyncio.Task[int] | None = None
    try:
        with port_is_released(env.port):
            task = asyncio.create_task(
                env.serve(env.config(feishu=feishu_config()), stop, feishu_channel=channel)
            )
            await asyncio.wait_for(channel.entered.wait(), 5)
            stop.set()
            assert await asyncio.wait_for(task, 3) == 0
        assert channel.stops == 1
        assert await lock_is_free(env)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        channel.close_loop()


async def test_occupied_port_fails_before_touching_the_database(env: Env) -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", env.port))
        holder.listen()
        with pytest.raises(runtime.ListenError):
            await env.serve(env.config(), asyncio.Event())
    assert await lock_is_free(env)


# ---- 运行中失败 ----------------------------------------------------------------------


# 只终止持有会话级实例锁（排他）的后端；接收事务的共享屏障锁不受影响。
TERMINATE_LOCK = (
    "SELECT count(pg_terminate_backend(pid)) FROM pg_locks"
    " WHERE locktype = 'advisory' AND granted AND mode = 'ExclusiveLock'"
)
WAITING_ON_LOCK = "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"


async def waiting_on_locks(env: Env, count: int, timeout: float = 10) -> bool:
    """在期限内至少有 ``count`` 个后端在数据库锁上等待。"""
    for _ in range(int(timeout / 0.02)):
        if await env.scalar(WAITING_ON_LOCK) >= count:
            return True
        await asyncio.sleep(0.02)
    return False


async def not_serving(served: Served, timeout: float = 2) -> None:
    """``/readyz`` 在期限内不再返回 200（503 或监听已关闭都算）。"""
    async with asyncio.timeout(timeout):
        while True:
            try:
                if (await served.client.get("/readyz")).status_code != 200:
                    return
            except httpx.TransportError:
                return
            await asyncio.sleep(0.02)


async def test_losing_the_instance_lock_stops_the_process(env: Env) -> None:
    async with env.running(env.config()) as served:
        assert await env.scalar(TERMINATE_LOCK) == 1
        assert await asyncio.wait_for(served.task, 30) == 1


async def test_normal_stop_during_a_lock_check_is_not_a_lock_loss(env: Env) -> None:
    """核对查询频繁在途时正常停止：不能因中途取消核对而让持锁连接失效、被判为丢锁。"""
    config = env.config(lock_check_seconds=0.001)
    for _ in range(5):
        async with env.running(config) as served:
            await asyncio.sleep(0.05)
            assert await served.finish() == 0


async def test_lock_loss_is_noticed_at_once_not_at_the_next_check(env: Env) -> None:
    """周期核对远在测试期限之外：只能靠连接终止通知立即锁低 readiness。"""
    config = env.config(lock_check_seconds=60)
    async with env.running(config) as served:
        await served.page()
        assert await env.scalar(TERMINATE_LOCK) == 1
        await not_serving(served)
        late = env.scripts.add(
            "丢锁后的新请求", tool_call("list_tables", cluster=SR.target_id, **SEARCH_ALL), cite()
        )
        try:
            response = await served.turn(late, request_id="late")
        except httpx.TransportError:
            pass  # 监听已关闭
        else:
            assert response.status_code == 503
        assert await asyncio.wait_for(served.task, 30) == 1
    assert late not in env.scripts.calls  # 模型与工具都没有被调用
    assert env.drv.attempts == 0


async def test_second_instance_after_lock_loss_never_runs_the_old_request(env: Env) -> None:
    """旧实例丢锁 → 第二实例取得锁并恢复 → 旧实例在途轮次的结果不能覆盖恢复结论。"""
    gate, entered = asyncio.Event(), asyncio.Event()
    slow = env.scripts.add(
        "丢锁时在途",
        after(gate, tool_call("list_tables", cluster=SR.target_id, **SEARCH_ALL), entered=entered),
        cite(),
    )
    old = env.config(lock_check_seconds=60, shutdown_timeout_seconds=20)
    async with env.running(old) as served:
        await served.page()
        pending = asyncio.create_task(served.turn(slow, request_id="inflight"))
        await asyncio.wait_for(entered.wait(), 20)
        assert await env.scalar(TERMINATE_LOCK) == 1
        await not_serving(served)

        second = Env(env.url, free_port(), env.scripts, env.clock, env.drv)
        async with second.running(second.config()) as fresh:
            assert await fresh.ready() == {"status": "ready", "feishu": "disabled"}
            # 第二实例的启动恢复已把旧实例的在途请求标为 interrupted，且没有重跑它。
            assert await env.scalar("SELECT state FROM xiaowei_request") == "interrupted"
            gate.set()
            response = await asyncio.wait_for(pending, 30)
            assert response.status_code != 200 or response.json()["state"] != "completed"
            assert await asyncio.wait_for(served.task, 30) == 1
            assert await env.scalar("SELECT state FROM xiaowei_request") == "interrupted"
            assert len(env.scripts.calls[slow]) <= 2  # 只来自旧实例的那一次运行
            assert await fresh.finish() == 0


async def test_accept_paused_across_a_takeover_ends_interrupted_not_stranded(env: Env) -> None:
    """旧实例的接收事务未提交时失去锁、新实例接管：恢复等它提交后再读取，请求不停在 accepted；
    重复请求在新实例得到已有终态，不运行模型或查询。"""
    first = env.scripts.add(
        "先建立会话", tool_call("list_tables", cluster=SR.target_id, **SEARCH_ALL), cite()
    )
    late = env.scripts.add(
        "接管时尚未提交", tool_call("list_tables", cluster=SR.target_id, **SEARCH_ALL), cite()
    )
    old = env.config(lock_check_seconds=60)
    async with env.running(old) as served:
        await served.page()
        assert (await served.turn(first, request_id="first")).json()["state"] == "completed"
        attempts = env.drv.attempts
        secret = SecretStr(env.url.render_as_string(hide_password=False))
        async with open_engine(secret) as admin:
            # 暂停点：行锁挡住接收事务读取 current 映射，此时它已开始、未提交。
            blocker = await admin.connect()
            await blocker.begin()
            await blocker.execute(text("SELECT 1 FROM xiaowei_channel_session FOR UPDATE"))
            pending = asyncio.create_task(served.turn(late, request_id="late"))
            assert await waiting_on_locks(env, 1)
            assert await env.scalar(TERMINATE_LOCK) == 1

            # 新实例用自己的驱动：它只做启动时的结构快照刷新，不能为任何请求运行查询。
            second = Env(env.url, free_port(), env.scripts, env.clock, driver(SALES))
            stop = asyncio.Event()
            task = asyncio.create_task(second.serve(second.config(), stop))
            # 新实例的恢复应在接收屏障上等待；没有屏障时它直接完成，这里只是等到期限。
            await waiting_on_locks(env, 2, timeout=2)
            await blocker.rollback()
            await blocker.close()
        response = await asyncio.wait_for(pending, 30)
        assert response.status_code != 200 or response.json()["state"] != "completed"
        assert await asyncio.wait_for(served.task, 30) == 1

        async with httpx.AsyncClient(
            base_url=second.origin, timeout=30, cookies=served.client.cookies
        ) as client:
            fresh = Served(client, task, stop)
            await fresh.ready()
            states = await env.scalar("SELECT array_agg(state ORDER BY state) FROM xiaowei_request")
            assert states == ["completed", "interrupted"]
            again = await fresh.turn(late, request_id="late")
            assert again.status_code == 200 and again.json()["state"] == "interrupted"
            assert await fresh.finish() == 0
    assert late not in env.scripts.calls  # 模型与查询都没有为它运行
    assert env.drv.attempts == attempts
    assert statements(second.drv) == [] and all(  # 新实例只有结构刷新的读取与探测
        sql.startswith(("SET ", "SELECT @@", "SELECT 1 FROM `")) or sql in SCHEMA_SQL
        for conn in second.drv.connections
        for sql, _ in conn.executed
    )


# ---- 飞书 ----------------------------------------------------------------------------


def feishu_event(env: Env, message: str, message_id: str) -> dict[str, Any]:
    millis = str(int(env.clock().timestamp() * 1000))
    return {
        "schema": "2.0",
        "header": {
            "event_id": f"ev_{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": millis,
            "tenant_key": TENANT,
            "app_id": APP_ID,
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_alice"},
                "sender_type": "user",
                "tenant_key": TENANT,
            },
            "message": {
                "message_id": message_id,
                "create_time": millis,
                "chat_id": "oc_alice",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": message}, ensure_ascii=False),
            },
        },
    }


async def until(predicate: Any, timeout: float = 20) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@dataclass
class OrderedChannel(FakeChannel):
    """记录发送与关闭的先后：停止时 drain 必须在关闭长连接之前完成投递。"""

    log: list[str] = field(default_factory=list)

    async def send(self, to: str, message: Any, opts: Any = None) -> Any:
        result = await super().send(to, message, opts)
        self.log.append("send")
        return result

    def stop(self, *, join_timeout: float) -> None:
        self.log.append("stop")
        super().stop(join_timeout=join_timeout)


async def test_feishu_turn_is_drained_before_the_long_connection_closes(env: Env) -> None:
    config = env.config(feishu=feishu_config())
    channel = OrderedChannel()
    gate, entered = asyncio.Event(), asyncio.Event()
    message = env.scripts.add(
        "飞书慢轮",
        after(gate, tool_call("list_tables", cluster=SR.target_id, **SEARCH_ALL), entered=entered),
        cite(),
    )
    async with env.running(config, feishu_channel=channel) as served:
        assert (await served.ready())["feishu"] == "connected"
        channel.emit_raw_from_sdk_thread(feishu_event(env, message, "om_1"))
        await asyncio.wait_for(entered.wait(), 20)
        served.stop.set()
        await asyncio.sleep(0.3)
        assert not served.task.done()  # 在途飞书轮次未结束前不关闭
        gate.set()
        assert await asyncio.wait_for(served.task, 30) == 0
    assert channel.log == ["send", "stop"]
    (sent,) = channel.sends
    assert sent[0] == "oc_alice" and sent[2] == {"receive_id_type": "chat_id"}


async def test_stop_during_a_long_feishu_turn_is_bounded_and_fails_closed(env: Env) -> None:
    config = env.config(
        shutdown_timeout_seconds=0.05,
        feishu=feishu_config(stop_timeout_seconds=0.2),
    )
    channel = OrderedChannel()
    gate, entered = asyncio.Event(), asyncio.Event()
    message = env.scripts.add(
        "停止时仍在运行",
        after(gate, tool_call("list_tables", cluster=SR.target_id, **SEARCH_ALL), entered=entered),
        cite(),
    )

    async with env.running(config, feishu_channel=channel) as served:
        assert (await served.ready())["feishu"] == "connected"
        channel.emit_raw_from_sdk_thread(feishu_event(env, message, "om_stop"))
        await asyncio.wait_for(entered.wait(), 20)
        served.stop.set()
        assert await asyncio.wait_for(served.task, 5) == 1

    assert channel.stops == 1 and channel.sends == []
    assert await env.scalar("SELECT state FROM xiaowei_request") == "running"


async def test_feishu_unavailable_at_startup_leaves_web_serving(env: Env) -> None:
    channel = FakeChannel(start_error=TimeoutError())
    async with env.running(env.config(feishu=feishu_config()), feishu_channel=channel) as served:
        assert await served.ready() == {"status": "ready", "feishu": "unavailable"}
        await served.page()
        assert (await served.turn(env.scripts.add("飞书断开时的网页", cite()))).status_code == 200
        assert await served.finish() == 0


# ---- 显式重发 ------------------------------------------------------------------------


async def failed_feishu_result(env: Env, config: runtime.ServeConfig) -> str:
    """经正式装配跑一条飞书轮次，发送明确失败（failed）；返回消息正文。"""
    refused = SendResult.fail(
        SendError(code=FeishuChannelErrorCode.PERMISSION_DENIED, retryable=False)
    )
    channel = FakeChannel(result=refused)
    message = env.scripts.add(
        "飞书待重发", tool_call("list_tables", cluster=SR.target_id, **SEARCH_ALL), cite()
    )
    async with env.running(config, feishu_channel=channel) as served:
        channel.emit_raw_from_sdk_thread(feishu_event(env, message, "om_resend"))
        await until(lambda: len(channel.sends) == 1)
        assert await served.finish() == 0
    assert await env.scalar("SELECT delivery FROM xiaowei_request") == "failed"
    return message


async def test_resend_sends_a_failed_result_once_without_rerunning(env: Env) -> None:
    config = env.config(feishu=feishu_config())
    message = await failed_feishu_result(env, config)
    sends: list[Any] = []

    async def resend(**changes: str) -> Any:
        # 每条重发命令都是独立进程：新建发送通道，结束时关闭。
        channel = FakeChannel()
        target = {"subject_id": "alice", "chat_id": "oc_alice", "message_id": "om_resend"}
        try:
            return await runtime.resend(
                config,
                clock=env.clock,
                feishu_channel=channel,
                starrocks_connect=env.connect(config),
                **{**target, **changes},
            )
        finally:
            sends.extend(channel.sends)
            assert channel.stops == 1

    with pytest.raises(RequestUnavailableError):
        await resend(chat_id="oc_other")  # 目的地参与会话摘要：不能改投别处
    with pytest.raises(RequestUnavailableError):
        await resend(message_id="om_unknown")
    with pytest.raises(AccessDeniedError):
        await resend(subject_id="mallory")
    assert sends == []
    assert await resend() == "sent"
    assert await resend() is None  # 已 sent：不再发送
    assert len(sends) == 1 and sends[0][0] == "oc_alice"
    assert len(env.scripts.calls[message]) == 2  # 只有原轮次的两次模型调用
    # 重发不重跑工具：之后到达 StarRocks 的只有证据依赖复核（零行探测与版本读取）。
    assert statements(env.drv) == []


@dataclass
class GatedChannel(FakeChannel):
    """发送进入后停住，直到测试放行；在 SDK 循环上等待，放行用线程安全事件。"""

    entered: threading.Event = field(default_factory=threading.Event)
    gate: threading.Event = field(default_factory=threading.Event)

    async def send(self, to: str, message: Any, opts: Any = None) -> Any:
        self.sends.append((to, message, opts))
        self.entered.set()
        while not self.gate.is_set():
            await asyncio.sleep(0.01)
        return self.result


async def test_serve_recovery_leaves_a_running_resend_to_finish(env: Env) -> None:
    """独立的 ``requests resend`` 正在发送时启动 serve：启动恢复不把它当作遗留 sending，
    重发照常落定为 sent；它进行中时其他重发取不到投递权。"""
    config = env.config(feishu=feishu_config())
    await failed_feishu_result(env, config)
    target = {"subject_id": "alice", "chat_id": "oc_alice", "message_id": "om_resend"}
    channel = GatedChannel()
    running = asyncio.create_task(
        runtime.resend(
            config,
            clock=env.clock,
            feishu_channel=channel,
            starrocks_connect=env.connect(config),
            **target,
        )
    )
    await until(channel.entered.is_set)
    async with env.running(config, feishu_channel=FakeChannel()) as served:
        await served.ready()
        assert await env.scalar("SELECT delivery FROM xiaowei_request") == "sending"
        other = FakeChannel()
        assert (
            await runtime.resend(
                config,
                clock=env.clock,
                feishu_channel=other,
                starrocks_connect=env.connect(config),
                **target,
            )
            is None
        )
        assert other.sends == []
        channel.gate.set()
        assert await asyncio.wait_for(running, 30) == "sent"
        assert await env.scalar("SELECT delivery FROM xiaowei_request") == "sent"
        assert await served.finish() == 0
    assert len(channel.sends) == 1


async def test_resend_refuses_after_the_evidence_expired(env: Env) -> None:
    config = env.config(feishu=feishu_config())
    await failed_feishu_result(env, config)
    env.clock.advance(3601)
    channel = FakeChannel()
    with pytest.raises(RequestUnavailableError):
        await runtime.resend(
            config,
            subject_id="alice",
            chat_id="oc_alice",
            message_id="om_resend",
            clock=env.clock,
            feishu_channel=channel,
            starrocks_connect=env.connect(config),
        )
    assert channel.sends == []


async def test_resend_requires_feishu(env: Env) -> None:
    with pytest.raises(runtime.ResendNotConfiguredError):
        await runtime.resend(env.config(), subject_id="alice", chat_id="oc", message_id="om")


# ---- 维护命令 ------------------------------------------------------------------------


async def test_storage_commands_use_the_configured_database(env: Env) -> None:
    config = env.config()
    await runtime.initialize(config)  # 已初始化时幂等
    assert await runtime.upgrade(config) == 6
    report = await runtime.cleanup(config, batch_size=10)
    assert (report.sessions, report.unregistered) == (0, 0)


async def test_maintenance_commands_reject_a_digest_key_mismatch_before_business_io(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY_ENV, "different-digest-key")
    config = env.config(feishu=feishu_config())
    channel = FakeChannel()
    forget(env.drv)

    with pytest.raises(StorageDigestKeyMismatchError):
        await runtime.upgrade(config)
    with pytest.raises(StorageDigestKeyMismatchError):
        await runtime.cleanup(config, batch_size=10)
    with pytest.raises(StorageDigestKeyMismatchError):
        await runtime.resend(
            config,
            subject_id="alice",
            chat_id="oc_alice",
            message_id="om_never_send",
            clock=env.clock,
            feishu_channel=channel,
            starrocks_connect=env.connect(config),
        )

    assert await env.scalar("SELECT version FROM xiaowei_schema_version") == 6
    assert env.drv.attempts == 0 and channel.sends == []


async def test_container_config_check_rejects_port_and_stop_grace_mismatches(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SR_ENV, "sr-test-password")
    config = env.config()
    minimum = runtime.minimum_stop_grace_seconds(config)
    runtime.validate_config(config, container_port=env.port, stop_grace_seconds=minimum)
    with pytest.raises(runtime.ConfigError, match="listen_port"):
        runtime.validate_config(config, container_port=env.port + 1, stop_grace_seconds=minimum)
    with pytest.raises(runtime.ConfigError, match="停止宽限"):
        runtime.validate_config(config, container_port=env.port, stop_grace_seconds=minimum - 1)


def test_stop_upper_bound_includes_feishu_consumer_cancellation() -> None:
    config = runtime.ServeConfig.model_validate(
        serve_config(
            18503,
            shutdown_timeout_seconds=1,
            feishu=feishu_config(stop_timeout_seconds=7),
        )
    )

    assert runtime.stop_upper_bound_seconds(config) == 57
    assert runtime.minimum_stop_grace_seconds(config) == 62


def _runtime_without_dependencies() -> runtime.Runtime:
    return runtime.Runtime(  # type: ignore[arg-type]
        engine=None,
        lock=None,
        readiness=Readiness(),
        recovery=None,
        service=None,
    )


async def test_web_exit_timeout_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StuckServer:
        should_exit = False

        def __init__(self, config: object) -> None:
            del config

        async def serve(self, *, sockets: list[object]) -> None:
            del sockets
            await asyncio.Event().wait()

    async def no_feishu(*args: object) -> None:
        del args
        return None

    async def cooperative_watch(lock: object, interval: float, stopping: asyncio.Event) -> None:
        del lock, interval
        await stopping.wait()

    monkeypatch.setattr(runtime.uvicorn, "Server", StuckServer)
    monkeypatch.setattr(runtime, "create_web_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(runtime, "_start_feishu", no_feishu)
    monkeypatch.setattr(runtime, "_watch", cooperative_watch)
    monkeypatch.setattr(runtime, "_WEB_EXIT_OVERHEAD_SECONDS", -1)
    stop = asyncio.Event()
    stop.set()

    result = await asyncio.wait_for(
        runtime._serve(
            runtime.ServeConfig.model_validate(serve_config(18504, shutdown_timeout_seconds=0.1)),
            _runtime_without_dependencies(),
            None,  # type: ignore[arg-type]
            stop,
            runtime.now,
            None,
        ),
        0.2,
    )

    assert result == 1


async def test_lock_watch_stop_timeout_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watch_started = asyncio.Event()
    release_watch = asyncio.Event()
    watch_finished = asyncio.Event()

    class CooperativeServer:
        should_exit = False

        def __init__(self, config: object) -> None:
            del config

        async def serve(self, *, sockets: list[object]) -> None:
            del sockets
            while not self.should_exit:
                await asyncio.sleep(0)

    async def no_feishu(*args: object) -> None:
        del args
        return None

    async def stuck_watch(lock: object, interval: float, stopping: asyncio.Event) -> None:
        del lock, interval, stopping
        watch_started.set()
        await release_watch.wait()
        watch_finished.set()

    monkeypatch.setattr(runtime.uvicorn, "Server", CooperativeServer)
    monkeypatch.setattr(runtime, "create_web_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(runtime, "_start_feishu", no_feishu)
    monkeypatch.setattr(runtime, "_watch", stuck_watch)
    monkeypatch.setattr(runtime, "_WATCH_STOP_TIMEOUT_SECONDS", 0.01)
    stop = asyncio.Event()
    stop.set()
    try:
        result = await asyncio.wait_for(
            runtime._serve(
                runtime.ServeConfig.model_validate(serve_config(18505)),
                _runtime_without_dependencies(),
                None,  # type: ignore[arg-type]
                stop,
                runtime.now,
                None,
            ),
            0.2,
        )
        assert result == 1 and watch_started.is_set()
    finally:
        release_watch.set()
        await asyncio.wait_for(watch_finished.wait(), 1)


# ---- 配置 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "path"),
    [
        ({"projection_bytes": {"model": 1}}, "projection_bytes"),
        ({"access": {"policy_version": "p1", "grants": {"a": ["local/drop_table"]}}}, "<root>"),
        ({"storage": {**serve_config(1)["storage"], "request_retention_seconds": 7200}}, "<root>"),
        ({"feishu": feishu_config(users={"ou_op": OPERATOR})}, "<root>"),
        ({"storage": {**serve_config(1)["storage"], "digest_key_ref": "plain-key"}}, "storage"),
        ({"unexpected": 1}, "unexpected"),
    ],
    ids=[
        "missing audiences",
        "unknown tool",
        "retention",
        "shared subject",
        "secret not ref",
        "extra key",
    ],
)
def test_invalid_configuration_is_reported_without_values(
    tmp_path: Path, changes: dict[str, Any], path: str
) -> None:
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(serve_config(8501, **changes)), encoding="utf-8")
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.load_config(file)
    assert path in str(raised.value)
    assert "plain-key" not in str(raised.value)


AUDIT_CONFIG = {
    "database": "starrocks_audit_db__",
    "table": "starrocks_audit_tbl__",
    "time_zone": "UTC",
    "stmt_limit": SR.policy.max_sql_bytes + 4,
}
WITH_AUDIT = sorted(QUERY_TOOLS | AUDIT_TOOLS)


def audit_config(port: int, **audit: object) -> dict[str, Any]:
    return serve_config(
        port,
        starrocks={**SR.model_dump(mode="json"), "audit": {**AUDIT_CONFIG, **audit}},
        data_policy={"input": {"max_bytes": 2000}, "model_tools": WITH_AUDIT},
        access={"policy_version": "p1", "grants": {OPERATOR: WITH_AUDIT, "alice": WITH_AUDIT}},
    )


def test_slow_queries_can_only_be_opened_with_an_audit_source(tmp_path: Path) -> None:
    file = tmp_path / "xiaowei.json"
    for changes in (
        {"data_policy": {"input": {"max_bytes": 2000}, "model_tools": WITH_AUDIT}},
        {"access": {"policy_version": "p1", "grants": {OPERATOR: WITH_AUDIT}}},
    ):
        file.write_text(json.dumps(serve_config(8501, **changes)), encoding="utf-8")
        with pytest.raises(runtime.ConfigError, match="已登记的 StarRocks 工具"):
            runtime.load_config(file)

    file.write_text(json.dumps(audit_config(8501)), encoding="utf-8")
    config = runtime.load_config(file)
    purposes = runtime._app_config(config).purposes
    assert purposes["diagnose"] == DIAGNOSE_TOOLS | AUDIT_TOOLS
    assert purposes["query"] == QUERY_TOOLS | AUDIT_TOOLS


@pytest.mark.parametrize(
    ("audit", "path"),
    [
        ({"stmt_limit": None}, "targets.0.starrocks.audit.stmt_limit"),
        ({"stmt_limit": SR.policy.max_sql_bytes + 3}, "targets.0.starrocks"),
        ({"table": "audit`canary"}, "targets.0.starrocks.audit.table"),
        ({"time_zone": "canary/Zone"}, "targets.0.starrocks.audit.time_zone"),
        ({"candidate_bytes": 10}, "targets.0.starrocks"),
    ],
    ids=["缺少 stmt_limit", "stmt_limit 太小", "表名非法", "时区非法", "候选字节太小"],
)
def test_invalid_audit_source_is_reported_without_values(
    tmp_path: Path, audit: dict[str, object], path: str
) -> None:
    values = audit_config(8501, **audit)
    if audit.get("stmt_limit", 0) is None:
        del values["targets"][0]["starrocks"]["audit"]["stmt_limit"]
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.load_config(file)
    assert path in str(raised.value) and "canary" not in str(raised.value)


def test_valid_configuration_round_trips_through_json(tmp_path: Path) -> None:
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(serve_config(8501, feishu=feishu_config())), encoding="utf-8")
    config = runtime.load_config(file)
    assert config.listen_port == 8501 and config.feishu is not None
    (target,) = config.targets
    assert target.starrocks == SR and target.description == "合成销售库"


@pytest.mark.parametrize(
    ("host", "origins"),
    [
        ("0.0.0.0", None),  # noqa: S104 - 内网访问：允许监听所有地址
        ("172.20.0.8", ["http://127.0.0.1:9"]),
        ("127.0.0.1", ["https://127.0.0.1:8501"]),
    ],
)
def test_any_listen_host_and_legacy_origins_are_accepted(
    tmp_path: Path, host: str, origins: list[str] | None
) -> None:
    web = {**serve_config(1)["web"]}
    web.pop("allowed_origins", None)
    if origins is not None:
        web["allowed_origins"] = origins
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(serve_config(8501, listen_host=host, web=web)), encoding="utf-8")
    assert runtime.load_config(file).listen_host == host


def test_unreadable_configuration(tmp_path: Path) -> None:
    with pytest.raises(runtime.ConfigError, match="不可读"):
        runtime.load_config(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text('{"storage": "secret-looking-value', encoding="utf-8")
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.load_config(broken)
    assert "secret-looking-value" not in str(raised.value)


async def test_resend_refusal_with_the_real_sdk_makes_no_network_call(env: Env) -> None:
    """真实 ``FeishuChannel``（未启动长连接）：记录不存在时在任何发送前拒绝；pytest-socket 只放行
    127.0.0.1，构造与关闭 SDK 通道若访问飞书会直接报错。"""
    config = env.config(feishu=feishu_config())
    with pytest.raises(RequestUnavailableError):
        await runtime.resend(
            config,
            subject_id="alice",
            chat_id="oc_alice",
            message_id="om_none",
            clock=env.clock,
            starrocks_connect=env.connect(config),
        )


def test_example_configuration_is_valid_and_fits_the_projections() -> None:
    """README 引用的示例配置：通过校验，且声明的结果上限装得进四种投影（启动时同样检查）。"""
    config = runtime.load_config(
        Path(__file__).resolve().parents[2] / "examples/xiaowei.example.json"
    )
    for target in config.targets:
        adapter = StarRocksAdapter(target.starrocks, connect=driver(SALES), clock=Clock())
        starrocks_tools(
            adapter, config.projection_bytes, schema=SchemaCache(adapter, clock=Clock())
        )
    assert config.listen_port == 8501 and config.feishu is None
    # 主模板是最小结构：一个 Vertex Profile、一个配置了审计源的目标、Web、飞书关闭。第二个目标与
    # 飞书只在 OPERATIONS / 飞书片段里说明，不维护第二份完整主配置。
    assert [t.target_id for t in config.targets] == ["warehouse"]
    assert config.targets[0].starrocks.audit is not None
    model = config.model
    assert model.provider == "vertex" and model.reasoning_effort is None
    assert (model.base_url, model.api_mode, model.output_mode) == (None, None, None)
    assert model.api_key_ref == "env:XW_MODEL_API_KEY"


def test_example_feishu_group_section_is_valid(tmp_path: Path) -> None:
    """README 引用的飞书段示例（含指定群）放进示例配置后通过校验：群工具都已登记、停机期限满足
    启用群的下限、等待期限短于请求保留期。"""
    examples = Path(__file__).resolve().parents[2] / "examples"
    values = json.loads((examples / "xiaowei.example.json").read_text())
    values["feishu"] = json.loads((examples / "feishu-group.example.json").read_text())
    path = tmp_path / "xiaowei.json"
    path.write_text(json.dumps(values, ensure_ascii=False))
    config = runtime.load_config(path)
    assert config.feishu is not None and config.feishu.group is not None
    assert config.feishu.group.chat_id.startswith("oc_")


GROUP_SECTION = {
    "chat_id": "oc_group",
    "tools": sorted(QUERY_TOOLS),
    "member_page_size": 100,
    "member_max_pages": 1,
    "member_timeout_seconds": 5,
    "max_waiting": 2,
    "max_wait_seconds": 60,
    "wait_check_seconds": 5,
}


def _load(tmp_path: Path, values: dict[str, Any]) -> runtime.ServeConfig:
    path = tmp_path / "xiaowei.json"
    path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    return runtime.load_config(path)


def test_empty_feishu_users_without_group_is_valid(tmp_path: Path) -> None:
    """首次部署可以先不登记任何单聊用户：服务能启动，单聊没有人获得授权。"""
    config = _load(tmp_path, serve_config(8501, feishu=feishu_config(users={})))
    assert config.feishu is not None and config.feishu.users == {}


def test_empty_feishu_users_with_group_keeps_group_tools_valid(tmp_path: Path) -> None:
    """空单聊名单不影响既有指定群授权：群成员仍按 ``group.tools`` 使用。"""
    feishu = feishu_config(users={}, stop_timeout_seconds=7, group=GROUP_SECTION)
    config = _load(tmp_path, serve_config(8501, feishu=feishu))
    assert config.feishu is not None and config.feishu.users == {}
    assert config.feishu.group is not None
    assert config.feishu.group.tools == QUERY_TOOLS


@pytest.mark.parametrize(
    "grants",
    [
        {OPERATOR: sorted(QUERY_TOOLS)},
        {OPERATOR: sorted(QUERY_TOOLS), "alice": []},
    ],
    ids=["subject 不在 grants", "grant 为空"],
)
def test_registered_feishu_subject_requires_nonempty_grant(
    tmp_path: Path, grants: dict[str, list[str]]
) -> None:
    """已登记的单聊用户必须同时有非空授权；否则在启动前离线拒绝，而不是运行时静默无权限。"""
    values = serve_config(
        8501,
        feishu=feishu_config(users={"ou_canary_open_id": "alice"}),
        access={"policy_version": "p1", "grants": grants},
    )
    with pytest.raises(runtime.ConfigError) as raised:
        _load(tmp_path, values)
    message = str(raised.value)
    assert "alice" in message and "有效授权" in message
    assert "ou_canary_open_id" not in message


SENSITIVE_OPEN_ID = "ou_sensitive_user"


@pytest.mark.parametrize(
    ("users", "grants"),
    [
        ({SENSITIVE_OPEN_ID: ""}, {}),
        ({SENSITIVE_OPEN_ID: "s" * 201}, {}),
        ({"bad-open-id": "alice"}, {"alice": sorted(QUERY_TOOLS)}),
        ({SENSITIVE_OPEN_ID: "bob"}, {}),
        ({SENSITIVE_OPEN_ID: "bob"}, {"bob": []}),
        ({SENSITIVE_OPEN_ID: "<内部 subject>"}, {"<内部 subject>": sorted(QUERY_TOOLS)}),
    ],
    ids=["空 subject", "超长 subject", "非法 open_id 键", "缺 grant", "grant 为空", "占位符"],
)
def test_feishu_user_errors_stop_at_the_users_field(
    tmp_path: Path, users: dict[str, str], grants: dict[str, list[str]]
) -> None:
    """``feishu.users`` 的键是 open_id：任何配置错误都只定位到 ``feishu.users``，不把键拼进路径。"""
    values = serve_config(8501, feishu=feishu_config(users=users))
    values["access"]["grants"] |= grants
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.validate_placeholders(_load(tmp_path, values))
    message = str(raised.value)
    assert "feishu.users" in message or "有效授权" in message
    assert all(open_id not in message for open_id in users)
    assert "feishu.users." not in message


def test_ordinary_config_errors_keep_precise_field_paths(tmp_path: Path) -> None:
    values = serve_config(8501, feishu=feishu_config(queue_size=0))
    with pytest.raises(runtime.ConfigError) as raised:
        _load(tmp_path, values)
    assert "feishu.queue_size" in str(raised.value)


EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
FILLED = {
    "replace-with-approved-vertex-model": "filled-vertex-model",
    "cli_replacewithappid": "cli_filledapp",
    "ou_replace_with_open_id": "ou_filled_user",
    "oc_replace_with_chat_id": "oc_filled_group",
}


def filled(value: Any) -> Any:
    """把模板占位符换成合成的实际值：需要“有效运行配置”的测试用它，不放宽正式预检。"""
    if isinstance(value, dict):
        return {filled(k): filled(v) for k, v in value.items()}
    if isinstance(value, list):
        return [filled(v) for v in value]
    if isinstance(value, str):
        if value in FILLED:
            return FILLED[value]
        if value.startswith("<") and value.endswith(">"):
            return "filled-value"
    return value


def example_with_feishu() -> dict[str, Any]:
    values = json.loads((EXAMPLES / "xiaowei.example.json").read_text(encoding="utf-8"))
    values["feishu"] = json.loads((EXAMPLES / "feishu-group.example.json").read_text("utf-8"))
    return values


def template_paths(value: Any, path: str = "") -> dict[str, str]:
    """测试侧枚举模板里需要替换的字段：形如 ``<……>`` 的整值和飞书标识哨兵。"""
    if isinstance(value, dict):
        found: dict[str, str] = {}
        for key, item in value.items():
            found |= template_paths(item, f"{path}.{key}" if path else key)
        return found
    if isinstance(value, list):
        return {
            key: item
            for index, child in enumerate(value)
            for key, item in template_paths(child, f"{path}.{index}").items()
        }
    if isinstance(value, str) and (value in FILLED or (value[:1], value[-1:]) == ("<", ">")):
        return {path: value}
    return {}


def test_repository_templates_name_every_placeholder_without_values(tmp_path: Path) -> None:
    """未替换的仓库模板语法上可读，但预检按字段路径拒绝模板里的每一个标记，且不回显模板文字。"""
    template = example_with_feishu()
    expected = template_paths(template)
    assert {"model.model", "targets.0.starrocks.user", "feishu.group.chat_id"} <= set(expected)
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.validate_placeholders(_load(tmp_path, template))
    message = str(raised.value)
    for path in expected:
        assert path in message
    assert "<" not in message and "replace" not in message


def test_operations_subject_marker_is_a_placeholder(tmp_path: Path) -> None:
    """OPERATIONS 的开通示例写的是 ``<内部 subject>``：照抄未改时按字段路径拒绝。"""
    operations = (EXAMPLES.parent / "deploy/OPERATIONS.md").read_text(encoding="utf-8")
    assert '"<内部 subject>"' in operations
    values = serve_config(8501)
    values["web"]["operator_id"] = "<内部 subject>"
    values["access"]["grants"] = {"<内部 subject>": sorted(QUERY_TOOLS)}
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.validate_placeholders(_load(tmp_path, values))
    assert "web.operator_id" in str(raised.value) and "<" not in str(raised.value)


def test_operations_install_path_matches_the_minimal_template() -> None:
    """首次安装按 config check → model check → 初始化 → 启动；主模板的非尖括号标记都写明。"""
    operations = (EXAMPLES.parent / "deploy/OPERATIONS.md").read_text(encoding="utf-8")
    install = operations.split("## 首次安装", 1)[1].split("\n## ", 1)[0]
    steps = [
        "xiaowei config check",
        "xiaowei model check",
        "xiaowei storage init",
        "up -d xiaowei --wait",
    ]
    assert [install.index(step) for step in steps] == sorted(install.index(s) for s in steps)
    for marker in template_paths(example_with_feishu()).values():
        if not marker.startswith("<"):
            assert f"`{marker}`" in install


def test_placeholder_errors_never_name_a_feishu_open_id(tmp_path: Path) -> None:
    """单聊名单的键是 open_id：名单里的占位符只报告到 ``feishu.users``，不把键写进路径。"""
    values = serve_config(8501, feishu=feishu_config(users={"ou_canary_open_id": "<内部 subject>"}))
    values["access"]["grants"]["<内部 subject>"] = sorted(QUERY_TOOLS)
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.validate_placeholders(_load(tmp_path, values))
    assert "feishu.users" in str(raised.value)
    assert "ou_canary_open_id" not in str(raised.value)


def test_historical_open_id_marker_is_still_a_placeholder(tmp_path: Path) -> None:
    """6d7c6af 发行的模板单聊名单是 ``{"ou_replace_with_open_id": "feishu-user"}``。

    升级时沿用旧片段并给 ``feishu-user`` 补了 grant，配置一致性能过；仍须按占位符拒绝。
    """
    users = {"ou_replace_with_open_id": "feishu-user"}
    values = serve_config(8501, feishu=feishu_config(users=users))
    values["access"]["grants"]["feishu-user"] = sorted(QUERY_TOOLS)
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.validate_placeholders(_load(tmp_path, values))
    assert "feishu.users" in str(raised.value)
    assert "ou_replace_with_open_id" not in str(raised.value)


def test_pre_vertex_template_markers_are_still_placeholders(tmp_path: Path) -> None:
    """C2 前的主模板是 OpenAI Profile 加第二个目标 archive；沿用旧副本未替换时仍按路径拒绝。"""
    values = serve_config(8501)
    values["model"]["model"] = "<获准的模型 ID>"
    target = copy.deepcopy(values["targets"][0])
    target["description"] = "<另一个集群的用途；未配置审计源，不能列慢查询>"
    target["starrocks"] |= {"target_id": "archive", "host": "<另一个 StarRocks FE 地址>"}
    values["targets"].append(target)
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.validate_placeholders(_load(tmp_path, values))
    message = str(raised.value)
    for path in ("model.model", "targets.1.description", "targets.1.starrocks.host"):
        assert path in message
    assert "<" not in message


def test_filled_templates_pass_the_placeholder_check(tmp_path: Path) -> None:
    config = _load(tmp_path, filled(example_with_feishu()))
    runtime.validate_placeholders(config)
    # 普通配置（各 Provider 的既有格式）不含模板 marker，照常通过。
    runtime.validate_placeholders(_load(tmp_path, serve_config(8501, feishu=feishu_config())))


@pytest.mark.parametrize("description", ["<生产>", "业务 <生产> 集群", "销售库 <sales> 汇总"])
def test_angle_brackets_in_real_values_are_not_mistaken_for_placeholders(
    tmp_path: Path, description: str
) -> None:
    """只认发行模板列出的固定完整值，不按“尖括号里有中文”之类的形状猜测。"""
    values = serve_config(8501)
    values["targets"][0]["description"] = description
    values["targets"][0]["starrocks"]["database"] = description
    runtime.validate_placeholders(_load(tmp_path, values))


ENV_TEMPLATE = EXAMPLES.parent / "deploy/.env.example"


def env_template_values() -> dict[str, str]:
    """deploy/.env.example 中仍需操作者填写的值（含按需启用、默认注释掉的行）。"""
    values: dict[str, str] = {}
    for line in ENV_TEMPLATE.read_text(encoding="utf-8").splitlines():
        name, sep, raw = line.lstrip("# ").partition("=")
        if sep and name.startswith("XW_") and "<" in raw:
            values[name.strip()] = raw.strip().strip("'")
    return values


def test_env_template_lists_every_operator_secret() -> None:
    assert set(env_template_values()) == {
        "XW_POSTGRES_PASSWORD",
        "XW_DATABASE_URL",
        "XW_DIGEST_KEY",
        "XW_MODEL_API_KEY",
        "XW_STARROCKS_PASSWORD",
        "XW_ARCHIVE_STARROCKS_PASSWORD",
        "XW_FEISHU_APP_SECRET",
    }


def real_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {name: f"real-{name.lower()}" for name in (DB_ENV, KEY_ENV, MODEL_ENV, SR_ENV)}
    for name, value in (values | overrides).items():
        monkeypatch.setenv(name, value)


def test_env_template_secrets_are_rejected_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """环境变量已设置但仍是 .env 模板值时，完整预检按字段路径拒绝，不输出该值。"""
    config = _load(tmp_path, serve_config(8501))
    fields = {
        DB_ENV: "storage.database_url_ref",
        KEY_ENV: "storage.digest_key_ref",
        MODEL_ENV: "model.api_key_ref",
        SR_ENV: "targets.0.starrocks.password_ref",
    }
    monkeypatch.delenv("XW_POSTGRES_PASSWORD", raising=False)
    for template in env_template_values().values():
        for name, path in fields.items():
            real_env(monkeypatch, **{name: template})
            with pytest.raises(runtime.ConfigError) as raised:
                runtime.validate_config(config)
            assert path in str(raised.value)
            assert template not in str(raised.value)
    real_env(monkeypatch)
    runtime.validate_config(config)


@pytest.mark.parametrize(
    "secret", ["real<中文>secret", "<生产>", "a<b>c", "postgresql+asyncpg://u:<p>@db:5432/x"]
)
def test_real_secrets_with_angle_brackets_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secret: str
) -> None:
    """环境变量只拒绝固定完整模板值，不对真实值做模糊搜索。"""
    monkeypatch.delenv("XW_POSTGRES_PASSWORD", raising=False)
    real_env(monkeypatch, **{MODEL_ENV: secret, SR_ENV: secret, DB_ENV: secret, KEY_ENV: secret})
    runtime.validate_config(_load(tmp_path, serve_config(8501)))


def test_postgres_password_template_is_rejected_only_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Compose 把 .env 交给小维容器：PG 初始化密码仍是模板时拒绝；原生外部库不设置它也能通过。"""
    config = _load(tmp_path, serve_config(8501))
    real_env(monkeypatch)
    monkeypatch.delenv("XW_POSTGRES_PASSWORD", raising=False)
    runtime.validate_config(config)
    monkeypatch.setenv("XW_POSTGRES_PASSWORD", "real<中文>secret")
    runtime.validate_config(config)

    template = env_template_values()["XW_POSTGRES_PASSWORD"]
    monkeypatch.setenv("XW_POSTGRES_PASSWORD", template)
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.validate_config(config)
    assert "XW_POSTGRES_PASSWORD" in str(raised.value)
    assert template not in str(raised.value)


def test_env_template_activates_only_the_required_secrets() -> None:
    """.env 模板默认只启用主模板实际引用的秘密；第二个目标与飞书的秘密注释为按需。"""
    active = {
        name
        for line in ENV_TEMPLATE.read_text(encoding="utf-8").splitlines()
        if (name := line.partition("=")[0]).startswith("XW_") and "<" in line
    }
    assert active == {
        "XW_POSTGRES_PASSWORD",
        "XW_DATABASE_URL",
        "XW_DIGEST_KEY",
        "XW_MODEL_API_KEY",
        "XW_STARROCKS_PASSWORD",
    }


def test_minimal_template_needs_only_the_required_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """主模板填好后，只设必填秘密即通过完整预检：未启用的第二目标与飞书不要求其秘密。"""
    values = filled(json.loads((EXAMPLES / "xiaowei.example.json").read_text(encoding="utf-8")))
    assert values["feishu"] is None and len(values["targets"]) == 1
    for name in ("XW_ARCHIVE_STARROCKS_PASSWORD", "XW_FEISHU_APP_SECRET", "XW_POSTGRES_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    for name in ("XW_DATABASE_URL", "XW_DIGEST_KEY", "XW_MODEL_API_KEY", "XW_STARROCKS_PASSWORD"):
        monkeypatch.setenv(name, f"real-{name.lower()}")
    runtime.validate_config(_load(tmp_path, values))


async def test_static_access_is_the_single_source_for_entry_and_evidence() -> None:
    access = runtime.StaticAccess(
        runtime.AccessConfig(
            policy_version="p1", grants={"alice": frozenset({"local/list_tables"})}
        ),
        frozenset({"sr-test", "sr-other"}),
    )
    decision = await access.resolve("feishu", "alice")
    assert decision is not None
    assert (decision.target_ids, decision.authorized_tools) == (
        {"sr-test", "sr-other"},
        {"local/list_tables"},
    )
    assert await access.resolve("web", "mallory") is None

    def who(subject: str) -> Identity:
        return Identity(subject_id=subject, session_id="s", turn_id="t", channel="web")

    assert await access.authorize(who("alice"), "sr-test", "local/list_tables")
    assert await access.authorize(who("alice"), "sr-other", "local/list_tables")
    assert not await access.authorize(who("alice"), "other-target", "local/list_tables")
    assert not await access.authorize(who("alice"), "sr-test", "local/run_readonly_query")
    assert not await access.authorize(who("mallory"), "sr-test", "local/list_tables")


# ---- 证据绑定当前数据范围（P2 Task 1）-----------------------------------------------------


def narrowed_starrocks() -> dict[str, Any]:
    """只从 allowlist 移除一个函数：授权表、工具契约与会话绑定都不变。"""
    starrocks = SR.model_dump(mode="json")
    starrocks["policy"]["allowed_functions"] = ["SUM"]
    return starrocks


async def test_history_and_resend_reject_after_scope_narrowing(env: Env) -> None:
    """以收窄数据范围的配置重启后，网页历史读取与飞书重发都不再交付旧事实。"""
    config = env.config(feishu=feishu_config())
    narrowed = env.config(feishu=feishu_config(), starrocks=narrowed_starrocks())
    await failed_feishu_result(env, config)
    async with env.running(config, feishu_channel=FakeChannel()) as served:
        await served.page()
        first = await served.turn(
            env.scripts.add(
                "网页查表", tool_call("list_tables", cluster=SR.target_id, **SEARCH_ALL), cite()
            )
        )
        assert first.status_code == 200 and first.json()["state"] == "completed"
        assert (await served.client.get("/api/turns/r1")).status_code == 200  # 同一范围：可读
        cookies = httpx.Cookies(served.client.cookies)
        assert await served.finish() == 0

    async with env.running(narrowed, feishu_channel=FakeChannel()) as served:
        served.client.cookies.update(cookies)
        assert (await served.client.get("/api/turns/r1")).status_code == 403
        assert await served.finish() == 0

    channel = FakeChannel()
    with pytest.raises(ResultUnavailableError):
        await runtime.resend(
            narrowed,
            subject_id="alice",
            chat_id="oc_alice",
            message_id="om_resend",
            clock=env.clock,
            feishu_channel=channel,
            starrocks_connect=env.connect(narrowed),
        )
    assert channel.sends == []


# ---- 多目标配置与路由（P2.5 Task 1）-------------------------------------------------------


def cluster_config(target_id: str) -> dict[str, Any]:
    """与 SR 同名库表、不同集群 ID 的目标配置。"""
    starrocks = SR.model_dump(mode="json")
    starrocks["target_id"] = target_id
    return target_config(starrocks)


async def test_formal_assembly_routes_each_turn_to_the_named_cluster(env: Env) -> None:
    drivers = {
        "sr-a": driver(Result(("region", "total"), [("east", 1)])),
        "sr-b": driver(Result(("region", "total"), [("east", 2)])),
    }
    config = env.config(targets=[cluster_config("sr-a"), cluster_config("sr-b")])
    async with env.running(config, starrocks_connect=drivers) as served:
        await served.page()
        query = env.scripts.add(
            "看 sr-b 的东区",
            tool_call("run_readonly_query", cluster="sr-b", sql="SELECT region, total FROM sales"),
            cite(),
        )
        body = (await served.turn(query, "query", "r1")).json()
        assert body["state"] == "completed"
        (fact,) = body["delivery"]["facts"]
        assert fact["target_id"] == "sr-b" and fact["rows"] == [{"region": "east", "total": 2}]
        assert drivers["sr-a"].attempts == 0 and len(statements(drivers["sr-b"])) == 1

        # 未知集群在任何 StarRocks I/O 前拒绝，不回退到其他集群；模型只能澄清。
        unknown = env.scripts.add(
            "看 sr-z 的东区",
            tool_call("run_readonly_query", cluster="sr-z", sql="SELECT region, total FROM sales"),
            clarify("没有 sr-z 集群"),
        )
        body = (await served.turn(unknown, "query", "r2")).json()
        assert body["state"] == "completed" and body["delivery"]["facts"] == []
        assert "集群不存在" in json.dumps(env.scripts.calls[unknown][1].input, ensure_ascii=False)
        assert drivers["sr-a"].attempts == 0 and len(statements(drivers["sr-b"])) == 1
        assert await served.finish() == 0


# ---- 阶段任务链（P2.5 Task 8）：三个集群，其中一个在服务中途故障 -------------------------------


async def test_a_cluster_failing_mid_task_changes_nothing_on_the_others(env: Env) -> None:
    """正式装配 + 真实 HTTP（Web）+ 飞书替身：sr-a 搜表并查询；sr-b 随后连接失败，本轮停止且
    不改查别的集群、不给模型第二次机会；sr-c 经飞书照常查询，sr-a 的历史照常读取，同会话随后只取
    sr-a 上一轮实际 SQL 的计划。替身不能证明真实集群的故障形态与恢复，只证明路由与失败隔离。"""
    drivers = {
        t: driver(Result(("region", "total"), [("east", n)]))
        for n, t in enumerate(("sr-a", "sr-b", "sr-c"), start=1)
    }
    config = env.config(targets=[cluster_config(t) for t in drivers], feishu=feishu_config())
    channel = FakeChannel()
    async with env.running(config, starrocks_connect=drivers, feishu_channel=channel) as served:
        await served.page()
        found = env.scripts.add(
            "sr-a 东区销售额",
            tool_call("list_tables", cluster="sr-a", **SEARCH_ALL),
            tool_call("run_readonly_query", cluster="sr-a", sql="SELECT region, total FROM sales"),
            cite(),
        )
        body = (await served.turn(found, "query", "r1")).json()
        assert body["state"] == "completed"
        assert [f["tool_id"] for f in body["delivery"]["facts"]] == [
            "local/list_tables",
            "local/run_readonly_query",
        ]
        assert body["delivery"]["facts"][1]["rows"] == [{"region": "east", "total": 1}]
        (ran,) = statements(drivers["sr-a"])

        # sr-b 中途故障：执行开始后失败，本轮停止，不重跑、不改查 sr-a/sr-c。
        drivers["sr-b"].connect_error = OperationalError(2003, "Can't connect to canary-b")
        before = {t: d.attempts for t, d in drivers.items()}
        broken = env.scripts.add(
            "sr-b 东区销售额",
            tool_call("run_readonly_query", cluster="sr-b", sql="SELECT region, total FROM sales"),
            tool_call("run_readonly_query", cluster="sr-a", sql="SELECT region, total FROM sales"),
            cite(),
        )
        body = (await served.turn(broken, "query", "r2")).json()
        assert body["state"] == "failed" and body["delivery"]["facts"] == []
        assert "canary" not in json.dumps(body, ensure_ascii=False)
        code = "SELECT failure_code FROM xiaowei_request WHERE state = 'failed'"
        assert await env.scalar(code) == "evidence_failed"
        assert len(env.scripts.calls[broken]) == 1
        assert drivers["sr-b"].attempts > before["sr-b"]
        # sr-a 只多了回放 r1 历史证据时的零行权限探测（不计入 ``statements``）；没有业务语句。
        assert statements(drivers["sr-a"]) == [ran] and drivers["sr-c"].attempts == 0

        # sr-c 经飞书照常查询；sr-b 不再被访问。
        attempts_b = drivers["sr-b"].attempts
        other = env.scripts.add(
            "sr-c 东区销售额",
            tool_call("run_readonly_query", cluster="sr-c", sql="SELECT region, total FROM sales"),
            cite(),
        )
        channel.emit_raw_from_sdk_thread(feishu_event(env, other, "om_c"))
        await until(lambda: len(channel.sends) == 1)
        text = channel.sends[0][1]["text"]
        assert "目标 sr-c" in text and "| east | 3 |" in text
        assert len(statements(drivers["sr-c"])) == 1
        assert drivers["sr-b"].attempts == attempts_b

        # sr-a 的历史结果照常读取（按当前权限复核 sr-a，不触及故障的 sr-b）。
        history = (await served.client.get("/api/turns/r1")).json()
        assert history["state"] == "completed" and len(history["delivery"]["facts"]) == 2
        assert drivers["sr-b"].attempts == attempts_b

        # 同会话随后诊断：只取上一轮实际 SQL 的计划，不再执行业务 SQL。
        explained = EXPLAIN_PREFIX + ran
        # ``driver`` 的工厂只在第一条连接带上 ``results``：每条连接各建一次（含回放时的权限探测）。
        drivers["sr-a"].make = lambda: driver(SALES, results={explained: PLAN}).make()
        why = env.scripts.add(
            "刚才 sr-a 那条为什么慢",
            tool_call("explain_query", cluster="sr-a", sql=ran),
            cite(),
        )
        body = (await served.turn(why, "diagnose", "r3")).json()
        assert body["state"] == "completed"
        # 脚本引用模型看到的全部证据：回放的 r1 两条与本轮的计划。
        assert [f["tool_id"] for f in body["delivery"]["facts"]] == [
            "local/list_tables",
            "local/run_readonly_query",
            "local/explain_query",
        ]
        assert statements(drivers["sr-a"]) == [ran, explained]
        assert drivers["sr-b"].attempts == attempts_b
        assert await served.finish() == 0


def test_legacy_single_target_configuration_reports_the_migration(tmp_path: Path) -> None:
    values = serve_config(8501)
    (target,) = values.pop("targets")
    values["starrocks"] = target["starrocks"]
    values["business_context"] = None
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.load_config(file)
    message = str(raised.value)
    assert "targets" in message and "starrocks.target_id" in message
    assert SR.host not in message and SR.user not in message


@pytest.mark.parametrize(
    ("checks", "reason"),
    [
        (None, "budget.max_scope_checks"),
        (0, "budget.max_scope_checks"),
        (100_001, "budget.max_scope_checks"),
        (5 * SR.policy.max_rows - 1, "max_scope_checks"),
    ],
    ids=["缺失", "零", "超过上界", "容不下一页列表"],
)
def test_scope_check_limit_is_required_and_fits_one_listing_turn(
    tmp_path: Path, checks: int | None, reason: str
) -> None:
    """每次复核按批去重后的对象数计数；一轮新列表在记录、写入 Session、最终校验、提交时的校验
    与提交前的整段回放中各复核一次，上限至少为最大一页（``policy.max_rows``）的 5 倍，否则任何
    一页满的列表都必然失败。"""
    budget = {k: v for k, v in BUDGET_CONFIG.items() if k != "max_scope_checks"}
    if checks is not None:
        budget["max_scope_checks"] = checks
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(serve_config(8501, budget=budget)), encoding="utf-8")
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.load_config(file)
    assert reason in str(raised.value)
    budget["max_scope_checks"] = 5 * SR.policy.max_rows
    file.write_text(json.dumps(serve_config(8501, budget=budget)), encoding="utf-8")
    assert runtime.load_config(file).budget.max_scope_checks == 5 * SR.policy.max_rows


@pytest.mark.parametrize(
    ("targets", "reason"),
    [
        ([], "targets"),
        ([cluster_config("sr-a"), cluster_config("sr-a")], "集群 ID 不能重复"),
        ([{**cluster_config("sr-a"), "type": "tidb"}], "targets.0.type"),
        ([cluster_config("Sales DB")], "target_id 必须是"),
        ([{**cluster_config("sr-a"), "host": "10.0.0.1"}], "targets.0.host"),
    ],
    ids=["空", "重复 ID", "未知类型", "非法 ID", "目标外的连接字段"],
)
def test_invalid_targets_are_reported_without_values(
    tmp_path: Path, targets: list[dict[str, Any]], reason: str
) -> None:
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(serve_config(8501, targets=targets)), encoding="utf-8")
    with pytest.raises(runtime.ConfigError) as raised:
        runtime.load_config(file)
    assert reason in str(raised.value) and "10.0.0.1" not in str(raised.value)


# ---- 自动结构快照（P2.5 Task 2）-----------------------------------------------------------


async def test_a_cluster_without_a_schema_snapshot_is_unavailable_but_the_others_serve(
    env: Env,
) -> None:
    """启动时 sr-b 的结构刷新失败：服务照常就绪，sr-a 正常查询；sr-b 的数据工具在任何 I/O 前
    拒绝（只有被拒绝的那次刷新尝试到达过驱动），模型只能如实说明。"""
    healthy = driver(Result(("region", "total"), [("east", 1)]))
    down = driver()
    down.connect_error = ProgrammingError(2003, "Can't connect canary-host")
    config = env.config(targets=[cluster_config("sr-a"), cluster_config("sr-b")])
    async with env.running(config, starrocks_connect={"sr-a": healthy, "sr-b": down}) as served:
        await served.page()
        ok = env.scripts.add(
            "sr-a 东区",
            tool_call("run_readonly_query", cluster="sr-a", sql="SELECT region, total FROM sales"),
            cite(),
        )
        body = (await served.turn(ok, "query", "r1")).json()
        assert body["state"] == "completed" and body["delivery"]["facts"][0]["target_id"] == "sr-a"

        refused = env.scripts.add(
            "sr-b 东区",
            tool_call("run_readonly_query", cluster="sr-b", sql="SELECT region, total FROM sales"),
            clarify("sr-b 暂不可用"),
        )
        body = (await served.turn(refused, "query", "r2")).json()
        assert body["state"] == "completed" and body["delivery"]["facts"] == []
        outputs = json.dumps(env.scripts.calls[refused][1].input, ensure_ascii=False)
        assert "表结构暂不可用" in outputs and "canary" not in outputs
        assert down.attempts == 0  # forget() 之后：拒绝路径没有任何连接
        assert await served.finish() == 0
