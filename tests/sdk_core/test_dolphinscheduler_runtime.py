"""D1/失败反馈：正式 Web、Runner、治理、Evidence 和 PostgreSQL；只替换 DS/模型 I/O。"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from tests.sdk_core.test_app import answer, clarify, tool_call
from tests.sdk_core.test_runtime import Env, serve_config
from tests.sdk_core.test_runtime import env as env

from xiaowei.runtime import ServeConfig

pytestmark = pytest.mark.loopback

DS_TOOLS = {
    "local/ds_select_clusters",
    "local/ds_list_projects",
    "local/ds_list_workflow_instances",
    "local/ds_get_workflow_instance",
    "local/ds_list_task_instances",
    "local/ds_read_task_log",
    "local/ds_search_task_log",
}


def source(cluster="ds-b", base_url="http://127.0.0.1:9/b", version="3.1.8"):
    return {
        "cluster_id": cluster,
        "description": "生产 B" if cluster == "ds-b" else "生产 A",
        "version": version,
        "api_base_url": base_url,
        "token_ref": "env:XW_TEST_DS_TOKEN",
        "time_zone": "Asia/Shanghai",
    }


def config_values(port, sources):
    values = serve_config(port)
    values["dolphinscheduler_sources"] = sources
    values["data_policy"]["model_tools"] += sorted(DS_TOOLS)
    values["access"]["grants"]["operator"] += sorted(DS_TOOLS)
    values["budget"].update(max_tool_calls=16, max_turns=12)
    return values


@pytest.mark.parametrize("version", ["3.1.8", "3.4.0"])
def test_ds_optional_config_accepts_versions_without_changing_database_targets(version):
    values = config_values(8501, [source(version=version)])
    config = ServeConfig.model_validate(values)
    assert config.dolphinscheduler_sources[0].version == version
    assert [t.target_id for t in config.targets] == ["sr-test"]
    assert ServeConfig.model_validate(serve_config(8501)).dolphinscheduler_sources == ()


@contextmanager
def ds_api(*, fail=False, respond=None):
    requests = []

    class API(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            requests.append((parsed.path, parse_qs(parsed.query), self.headers.get("token")))
            body = {
                "code": 0,
                "data": {
                    "total": 1,
                    "totalList": [
                        {
                            "id": 701,
                            "name": "orders_failed",
                            "state": "FAILURE",
                            (
                                "workflowDefinitionCode"
                                if "workflow-instances" in parsed.path
                                else "processDefinitionCode"
                            ): 99,
                            (
                                "workflowDefinitionVersion"
                                if "workflow-instances" in parsed.path
                                else "processDefinitionVersion"
                            ): 2,
                            "startTime": "2026-10-27 20:01:00",
                            "endTime": "2026-10-27 20:02:00",
                        },
                    ],
                },
            }
            if respond is not None:
                body = respond(parsed.path, parse_qs(parsed.query))
            self.send_response(401 if fail else 200)
            self.send_header(
                "Content-Type",
                "application/octet-stream" if isinstance(body, bytes) else "application/json",
            )
            self.end_headers()
            self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

        def log_message(self, *_):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), API)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()


def ds_answer(call):
    ids = []
    for item in call.input:
        if item.get("type") == "function_call_output":
            try:
                result = json.loads(item["output"])
            except ValueError:
                continue
            if result.get("evidence_id"):
                ids.append(result["evidence_id"])
    return answer(ids, analysis="按已取得的 DS 实例信息说明结果及缺口")


def list_failed(cluster="ds-b"):
    return tool_call(
        "ds_list_workflow_instances",
        cluster=cluster,
        project_code=11,
        definition_code=99,
        keyword=None,
        state="FAILURE",
        start_time="2026-10-27T20:00:00+08:00",
        end_time="2026-10-28T08:00:00+08:00",
        page_no=1,
        page_size=20,
    )


@pytest.mark.parametrize(
    "version,route,filter_name",
    [
        ("3.1.8", "process-instances", "processDefineCode"),
        ("3.4.0", "workflow-instances", "workflowDefinitionCode"),
    ],
)
async def test_d1_formal_web_selects_b_reads_and_commits_choice(
    env: Env, monkeypatch, version, route, filter_name
):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(
            config_values(
                env.port,
                [
                    source("ds-a", url + "/a", version),
                    source("ds-b", url + "/b", version),
                ],
            )
        )
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "查 DS B 昨晚订单项目 11 的失败工作流",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                list_failed(),
                ds_answer,
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed", body
            assert "orders_failed" in json.dumps(body, ensure_ascii=False)
            assert [r[0] for r in requests] == [f"/b/projects/11/{route}"]
            assert requests[0][1][filter_name] == ["99"]
            assert requests[0][2] == "synthetic-token"
            assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence WHERE target_id='ds-b'") == 1
    assert await env.scalar(
        "SELECT ds_clusters FROM xiaowei_session WHERE ds_clusters IS NOT NULL"
    ) == ["ds-b"]


async def test_d1_formal_web_auth_failure_is_recorded_without_retry(env: Env, monkeypatch):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api(fail=True) as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url + "/b")]))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "查 DS B 的失败工作流",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                list_failed(),
                ds_answer,
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed", body
            assert len(requests) == 1
            rendered = json.dumps(body["delivery"], ensure_ascii=False)
            assert "生产 B" in rendered and "认证" in rendered
            assert "orders_failed" not in rendered and "synthetic-token" not in rendered
            assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_e1_formal_web_unselected_clarifies_with_zero_ds_io(env: Env, monkeypatch):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url + "/b")]))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add("昨晚有哪些失败工作流", clarify("要查哪套 DS 环境？"))
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed"
            assert requests == []
            assert await served.finish() == 0


async def test_selected_scope_rejects_a_without_ds_io(env: Env, monkeypatch):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(
            config_values(
                env.port,
                [
                    source("ds-a", url + "/a"),
                    source("ds-b", url + "/b"),
                ],
            )
        )
        async with env.running(config) as served:
            await served.page()
            bad = tool_call(
                "ds_list_projects", cluster="ds-a", keyword=None, page_no=1, page_size=10
            )
            message = env.scripts.add(
                "查 DS B，目标不清先说明",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                bad,
                clarify("DS A 不在本次选择中，请核实查询对象"),
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed", body
            assert requests == []
            assert await served.finish() == 0
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_only_selection_is_not_an_unfinished_query(env: Env, monkeypatch):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url + "/b")]))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "查 DS B，项目不明确就问我",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                clarify("需要查哪个项目？"),
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed", body
            assert requests == []
            assert await served.finish() == 0
    assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") == ["ds-b"]
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0


async def test_failed_b_turn_keeps_committed_a_choice_for_followup(env: Env, monkeypatch):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(
            config_values(
                env.port,
                [
                    source("ds-a", url + "/a"),
                    source("ds-b", url + "/b"),
                ],
            )
        )
        async with env.running(config) as served:
            await served.page()
            first = env.scripts.add(
                "查 DS A 的失败工作流",
                tool_call("ds_select_clusters", clusters=["ds-a"]),
                list_failed("ds-a"),
                ds_answer,
            )
            assert (await served.turn(first, "diagnose", "a1")).json()["state"] == "completed"
            fail = env.scripts.add(
                "改查 DS B 的失败工作流",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                list_failed(),
                tool_call("not_a_registered_tool"),
            )
            assert (await served.turn(fail, "diagnose", "b1")).json()["state"] == "failed"
            assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") == ["ds-a"]
            follow = env.scripts.add("再查同一批实例", list_failed("ds-a"), ds_answer)
            assert (await served.turn(follow, "diagnose", "a2")).json()["state"] == "completed"
            assert [r[0].split("/")[1] for r in requests] == ["a", "b", "a"]
            assert await served.finish() == 0


def test_ds_turn_ledger_roundtrip_and_legacy_defaults():
    from dataclasses import replace

    from pydantic import TypeAdapter

    from xiaowei.governance import TurnLedger, TurnRuns

    parser = TypeAdapter(TurnLedger)
    runs = replace(
        TurnRuns(),
        ds_clusters=("ds-a", "ds-b"),
        ds_read_scope=("ds-a", "ds-b"),
        ds_selection_changed=True,
    )
    ledger = TurnLedger(3, runs)
    assert parser.validate_json(parser.dump_json(ledger), strict=True) == ledger
    old = json.loads(parser.dump_json(ledger))
    for key in (
        "ds_clusters",
        "ds_read_scope",
        "ds_selection_changed",
        "ds_log_reads",
        "ds_log_bytes",
    ):
        old["runs"].pop(key)
    restored = parser.validate_json(json.dumps(old), strict=True)
    assert restored.runs.ds_clusters is None
    assert restored.runs.ds_read_scope is None
    assert not restored.runs.ds_selection_changed


@pytest.mark.parametrize(
    "reason,stopped",
    [("ds_auth", True), ("ds_connect", True), ("ds_timeout", False), ("ds_unavailable", False)],
)
async def test_ds_failure_stops_only_appropriate_scope_this_turn(postgres_url, reason, stopped):
    from tests.sdk_core.synthetic_tools import Clock, Grants, context, ready_engine, store

    from xiaowei.dolphinscheduler import DSConfig, catalog
    from xiaowei.governance import (
        DolphinSchedulerReadError,
        GovernedTools,
        ToolCatalog,
        ToolRejectedError,
    )
    from xiaowei.models import AUDIENCES, ToolObservation, ToolRequest

    contracts, policies = catalog(
        [DSConfig.model_validate(source())], dict.fromkeys(AUDIENCES, 20_000)
    )
    tools = frozenset(c.tool_id for c in contracts)
    clock, grants = Clock(), Grants()
    ctx = context(tools=tools, targets=frozenset({"ds-b", "dolphinscheduler"}))
    for c in contracts:
        grants.grant("alice", c.tool_id, target=c.target_id)
    calls = []
    req = ToolRequest(
        tool_id="local/ds_list_projects",
        target_id="ds-b",
        call_id="c1",
        tool_name="ds_list_projects",
        arguments={"cluster": "ds-b", "keyword": None, "page_no": 1, "page_size": 10},
    )

    async def fail(request):
        calls.append(request)
        raise DolphinSchedulerReadError(reason)

    async def success(request):
        calls.append(request)
        return ToolObservation(
            payload={
                "environment": "生产 B",
                "cluster": "ds-b",
                "kind": "ds_list_projects",
                "records": [],
                "coverage": {},
            },
            captured_at=clock(),
            truncated=False,
        )

    async with ready_engine(postgres_url) as engine:
        gov = GovernedTools(store(engine, grants, clock, ToolCatalog(contracts, policies)))
        await gov.select_dolphinscheduler(ctx, ("ds-b",))
        with pytest.raises(DolphinSchedulerReadError):
            await gov.invoke(ctx, req, fail)
        if stopped:
            with pytest.raises(ToolRejectedError):
                await gov.invoke(ctx, req, success)
        else:
            await gov.invoke(ctx, req, success)
        assert len(calls) == (1 if stopped else 2)
        gov.end_turn(ctx.identity)
        ctx2 = ctx.model_copy(
            update={"identity": ctx.identity.model_copy(update={"turn_id": "next"})}
        )
        gov.load_dolphinscheduler_selection(ctx2.identity, ("ds-b",))
        await gov.invoke(ctx2, req, success)
        assert len(calls) == (2 if stopped else 3)


async def test_ds_schema_16_upgrade_adds_nullable_selection_without_sdk_changes(postgres_url):
    from sqlalchemy import text
    from tests.sdk_core.synthetic_tools import TEST_DIGEST_KEY, secret
    from tests.sdk_core.test_storage_v2 import _install_v1

    from xiaowei import storage as storage_module
    from xiaowei.storage import open_engine, upgrade_storage

    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="016_scheduled_result.sql")
        async with engine.begin() as conn:
            await storage_module._write_digest_key_binding(conn, TEST_DIGEST_KEY)
        await upgrade_storage(engine, digest_key=TEST_DIGEST_KEY)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT version FROM xiaowei_schema_version")) == 17
            columns = (
                (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='xiaowei_session'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert "ds_clusters" in columns and "ds_selection_turn" in columns
            assert not (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='agent_sessions' AND column_name LIKE 'ds_%'"
                    )
                )
            ).all()


async def test_e6_e7_explicit_set_replaces_then_followup_retains_it(env: Env, monkeypatch):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(
            config_values(env.port, [source("ds-a", url + "/a"), source("ds-b", url + "/b")])
        )
        async with env.running(config) as served:
            await served.page()
            compare = env.scripts.add(
                "比较 DS A 和 B",
                tool_call("ds_select_clusters", clusters=["ds-a", "ds-b"]),
                list_failed("ds-a"),
                list_failed("ds-b"),
                ds_answer,
            )
            assert (await served.turn(compare, "diagnose", "compare")).json()[
                "state"
            ] == "completed"
            follow = env.scripts.add("今天呢", list_failed("ds-a"), list_failed("ds-b"), ds_answer)
            assert (await served.turn(follow, "diagnose", "follow")).json()["state"] == "completed"
            assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") == ["ds-a", "ds-b"]
            just_b = env.scripts.add(
                "B 上也这样吗",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                list_failed("ds-b"),
                ds_answer,
            )
            assert (await served.turn(just_b, "diagnose", "only-b")).json()["state"] == "completed"
            assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") == ["ds-b"]
            assert [r[0].split("/")[1] for r in requests] == ["a", "b", "a", "b", "b"]
            await served.finish()


async def test_scope_remains_frozen_after_pause_restore_and_revocation(postgres_url):
    from dataclasses import replace

    from pydantic import TypeAdapter
    from tests.sdk_core.synthetic_tools import Clock, Grants, context, ready_engine, store

    from xiaowei.dolphinscheduler import DSConfig, catalog
    from xiaowei.governance import GovernedTools, ToolCatalog, ToolRejectedError, TurnLedger
    from xiaowei.models import AUDIENCES, ToolObservation, ToolRequest

    contracts, policies = catalog(
        [DSConfig.model_validate(source(c)) for c in ("ds-a", "ds-b")],
        dict.fromkeys(AUDIENCES, 20_000),
    )
    tools = frozenset(c.tool_id for c in contracts)
    ctx = context(tools=tools, targets=frozenset({"ds-a", "ds-b", "dolphinscheduler"}))
    grants, clock, calls = Grants(), Clock(), []
    for c in contracts:
        grants.grant("alice", c.tool_id, target=c.target_id)
    req = ToolRequest(
        tool_id="local/ds_list_projects",
        target_id="ds-a",
        call_id="c1",
        tool_name="ds_list_projects",
        arguments={"cluster": "ds-a", "keyword": None, "page_no": 1, "page_size": 10},
    )

    async def read(request):
        calls.append(request)
        return ToolObservation(
            payload={
                "environment": "生产 A",
                "cluster": "ds-a",
                "kind": "ds_list_projects",
                "records": [],
                "coverage": {},
            },
            captured_at=clock(),
            truncated=False,
        )

    async with ready_engine(postgres_url) as engine:
        gov = GovernedTools(store(engine, grants, clock, ToolCatalog(contracts, policies)))
        await gov.select_dolphinscheduler(ctx, ("ds-a",))
        await gov.invoke(ctx, req, read)
        parser = TypeAdapter(TurnLedger)
        saved = parser.validate_json(parser.dump_json(gov.checkpoint(ctx.identity)), strict=True)
        gov.end_turn(ctx.identity)
        gov.restore(ctx, saved)
        with pytest.raises(ToolRejectedError):
            await gov.select_dolphinscheduler(ctx, ("ds-b",))
        grants.allowed.discard(("alice", "ds-a", "local/ds_list_projects"))
        before = gov.turn_runs(ctx.identity)
        with pytest.raises(ToolRejectedError):
            await gov.invoke(ctx, req, read)
        assert gov.turn_runs(ctx.identity) == before
        assert len(calls) == 1
        assert replace(saved.runs, rejected=0).ds_read_scope == ("ds-a",)


async def test_ds_configured_database_query_needs_no_ds_selection(env: Env, monkeypatch):
    from tests.sdk_core.test_app import cite

    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url)]))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "查 StarRocks sr-test 销售额",
                tool_call(
                    "run_readonly_query",
                    cluster="sr-test",
                    sql="SELECT region, total FROM shop.sales",
                ),
                cite(),
            )
            body = (await served.turn(message, "query")).json()
            assert body["state"] == "completed", body
            assert body["delivery"]["facts"][0]["rows"] == [
                {"region": "east", "total": 100},
                {"region": "west", "total": 50},
            ]
            assert requests == []
            await served.finish()
    assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") is None


async def test_ds_selection_commit_failure_keeps_session_writing_and_blocks_replay(
    env, monkeypatch
):
    from xiaowei import session as session_module
    from xiaowei.session import PolicySession

    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    execute = PolicySession._execute

    async def fail_selection_commit(self, statement, parameters):
        if statement is session_module._COMMIT_DS:
            return 0
        return await execute(self, statement, parameters)

    monkeypatch.setattr(PolicySession, "_execute", fail_selection_commit)
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url)]))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "查 DS B 后模拟提交失败",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                list_failed(),
                ds_answer,
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "failed", body
            assert await env.scalar("SELECT state FROM xiaowei_session") == "writing"
            assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") is None
            follow = env.scripts.add("再查一次", list_failed(), ds_answer)
            assert (await served.turn(follow, "diagnose", "next")).json()["state"] == "failed"
            assert len(requests) == 1
            assert not env.scripts.calls.get(follow)
            await served.finish()


async def test_d1_two_pages_and_d4_fresh_detail_keep_original_instance(env, monkeypatch):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")

    def respond(path, params):
        rows = [
            {
                "id": 701,
                "name": "orders",
                "state": "FAILURE",
                "processDefinitionCode": 99,
                "startTime": "2026-10-27 20:01:00",
            },
            {
                "id": 702,
                "name": "orders",
                "state": "FAILURE",
                "processDefinitionCode": 99,
                "startTime": "2026-10-28 07:59:00",
            },
            {
                "id": 703,
                "name": "orders",
                "state": "FAILURE",
                "processDefinitionCode": 99,
                "startTime": "2026-10-28 08:00:00",
            },
        ]
        if path.endswith("/701"):
            return {"code": 0, "data": rows[0]}
        page = int(params["pageNo"][0])
        return {"code": 0, "data": {"total": 3, "totalList": rows[(page - 1) * 2 : page * 2]}}

    def page(number):
        return tool_call(
            "ds_list_workflow_instances",
            cluster="ds-b",
            project_code=11,
            definition_code=99,
            state="FAILURE",
            keyword=None,
            start_time="2026-10-27T20:00:00+08:00",
            end_time="2026-10-28T08:00:00+08:00",
            page_no=number,
            page_size=2,
        )

    with ds_api(respond=respond) as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url)]))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "DS B 昨晚启动的失败订单",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                page(1),
                page(2),
                ds_answer,
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed", body
            rendered = json.dumps(body["delivery"], ensure_ascii=False)
            assert "701" in rendered and "702" in rendered and '"703"' not in rendered
            follow = env.scripts.add(
                "刚才的 701 恢复了吗",
                tool_call(
                    "ds_get_workflow_instance",
                    cluster="ds-b",
                    project_code=11,
                    workflow_instance_id=701,
                    task_id=None,
                ),
                ds_answer,
            )
            body = (await served.turn(follow, "diagnose", "fresh")).json()
            assert body["state"] == "completed", body
            assert requests[-1][0].endswith("/701")
            assert len(requests) == 3
            await served.finish()


async def test_d2_formal_web_locates_middle_database_error_and_persists_only_snippets(
    env, monkeypatch
):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    raw = (
        b"[LOG-PATH]: private/path, [HOST]: private-host\n"
        + b"normal task noise\n" * 320_000
        + b"SQLSyntaxErrorException SQLState 42S02 code 1146 ods.order_delta missing\n"
        + b"normal task noise\n" * 320_000
    )

    def respond(path, params):
        if path.endswith("download-log"):
            return raw
        if path.endswith("/701"):
            return {"code": 0, "data": {"id": 701, "processDefinitionCode": 99, "state": "FAILURE"}}
        return {
            "code": 0,
            "data": {
                "total": 1,
                "totalList": [
                    {"id": 801, "processInstanceId": 701, "state": "FAILURE", "retryTimes": 0}
                ],
            },
        }

    with ds_api(respond=respond) as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url)]))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "定位 DS B 701 的失败节点 801 数据库报错",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                tool_call(
                    "ds_search_task_log",
                    cluster="ds-b",
                    project_code=11,
                    workflow_instance_id=701,
                    task_id=801,
                    task_page_no=1,
                    terms=["SQLSyntaxErrorException", "42S02", "1146"],
                    case_sensitive=False,
                    max_snippets=4,
                    context_lines=1,
                ),
                ds_answer,
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed", body
            rendered = json.dumps(body["delivery"], ensure_ascii=False)
            assert all(word in rendered for word in ("ods.order_delta", "42S02", "1146", "生产 B"))
            assert len(requests) == 3
            assert "private-host" not in rendered
            await served.finish()
    assert (
        await env.scalar("SELECT max(octet_length(model_content)) FROM xiaowei_evidence") < 16_384
    )
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1


def test_config_check_validates_ds_secret_reference_before_any_client_or_io(monkeypatch):
    from tests.sdk_core.test_runtime import real_env

    from xiaowei.runtime import ConfigError, validate_config

    real_env(monkeypatch)
    monkeypatch.delenv("XW_TEST_DS_TOKEN", raising=False)
    config = ServeConfig.model_validate(config_values(8501, [source()]))
    with pytest.raises(ConfigError, match=r"dolphinscheduler_sources\.0\.token_ref"):
        validate_config(config)
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    validate_config(config)


@pytest.mark.parametrize(
    "version,state", [("3.1.8", "NOT_A_STATE"), ("3.4.0", "NOT_A_STATE"), ("3.4.0", "STOP")]
)
async def test_task_state_rejected_before_io_and_can_be_corrected(env, monkeypatch, version, state):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    owner = "processInstanceId" if version == "3.1.8" else "workflowInstanceId"

    def respond(path, params):
        assert params["stateType"] == ["KILL"]
        return {
            "code": 0,
            "data": {"total": 1, "totalList": [{"id": 801, owner: 701, "state": "KILL"}]},
        }

    with ds_api(respond=respond) as (url, requests):
        config = ServeConfig.model_validate(
            config_values(env.port, [source(base_url=url, version=version)])
        )

        def tasks(status):
            return tool_call(
                "ds_list_task_instances",
                cluster="ds-b",
                project_code=11,
                workflow_instance_id=701,
                state=status,
                keyword=None,
                page_no=1,
                page_size=10,
            )

        def corrected(call):
            assert requests == []
            assert "rejected" in call.input[-1]["output"]
            return tasks("KILL")(call)

        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "DS B 停止的任务",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                tasks(state),
                corrected,
                ds_answer,
            )
            result = (await served.turn(message)).json()
            assert result["state"] == "completed", result
            assert len(requests) == 1
            await served.finish()
