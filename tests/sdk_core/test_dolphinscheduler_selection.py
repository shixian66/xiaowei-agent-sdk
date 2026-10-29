"""生产重复选择回归：正式应用/SDK/PG，仅模型与 DS HTTP 使用合成应答。"""

import asyncio
import hashlib
import json
import logging

import httpx2
import pytest
from tests.sdk_core.synthetic_tools import Clock, Grants, context, ready_engine, store
from tests.sdk_core.test_app import answer_body, tool_call
from tests.sdk_core.test_dolphinscheduler_runtime import (
    config_values,
    ds_answer,
    ds_api,
    list_failed,
    source,
)
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_vertex_model import VERTEX, _body, _call_part

from xiaowei.dolphinscheduler import DSConfig, catalog
from xiaowei.governance import GovernedTools, ToolCatalog, ToolRejectedError
from xiaowei.models import AUDIENCES
from xiaowei.runtime import ServeConfig

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize("limit", [32, 2])
async def test_vertex_100_parallel_identical_selections_leave_budget_for_projects(
    env, monkeypatch, caplog, limit
):
    """同一响应 100 次选择不能挤掉下一次项目读取；2 次预算也足够。"""
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    monkeypatch.setenv(VERTEX.api_key_ref.removeprefix("env:"), "fake-vertex-key")
    caplog.set_level(logging.INFO, logger="xiaowei")
    requests_to_model = []
    feedback = []

    def endpoint(request):
        body = json.loads(request.content)
        requests_to_model.append(body)
        contents = body["contents"]
        if len(requests_to_model) == 1:
            return httpx2.Response(
                200,
                json=_body([{"text": json.dumps(answer_body([], clarification="查哪套 DS？"))}]),
            )
        if len(requests_to_model) == 2:
            parts = [
                _call_part("ds_select_clusters", {"clusters": ["ds-b"]}, call_id=f"selection-{i}")
                for i in range(100)
            ]
            return httpx2.Response(200, json=_body(parts))
        outputs = [
            json.loads(part["functionResponse"]["response"]["output"])
            for content in contents
            for part in content["parts"]
            if "functionResponse" in part
        ]
        if len(requests_to_model) == 3:
            feedback.extend(outputs)
            return httpx2.Response(
                200,
                json=_body(
                    [
                        _call_part(
                            "ds_list_projects",
                            {"cluster": "ds-b", "keyword": None, "page_no": 1, "page_size": 10},
                            call_id="projects",
                        )
                    ]
                ),
            )
        assert len(requests_to_model) == 4
        ids = [o["evidence_id"] for o in outputs if "evidence_id" in o]
        return httpx2.Response(
            200,
            json=_body(
                [
                    {
                        "text": json.dumps(
                            answer_body(ids, analysis="按已取得事实回答；无事实时说明工具额度限制")
                        )
                    }
                ]
            ),
        )

    def projects(path, params):
        return {"code": 0, "data": {"total": 1, "totalList": [{"code": 11, "name": "orders"}]}}

    with ds_api(respond=projects) as (url, requests):
        values = config_values(env.port, [source(base_url=url)])
        values["model"] = VERTEX.model_dump(mode="json")
        values["model"]["max_request_bytes"] = 262_144
        values["budget"]["max_tool_calls"] = limit
        config = ServeConfig.model_validate(values)
        async with env.running(config, model_transport=httpx2.MockTransport(endpoint)) as served:
            await served.page()
            assert (await served.turn("DS 有哪些项目", "query", "clarify")).json()[
                "state"
            ] == "completed"
            assert requests == []
            result = (await served.turn("查 DS B 的项目", "query", "projects")).json()
            assert result["state"] == "completed", result
            assert "orders" in json.dumps(result["delivery"])
            assert result["delivery"]["facts"][0]["tool_id"] == "local/ds_list_projects"
            assert await served.finish() == 0
    assert len(feedback) == 100
    assert all(o == {"status": "selected", "clusters": ["ds-b"]} for o in feedback)
    assert len(requests) == 1

    def offered(body):
        return {f["name"] for t in body.get("tools", []) for f in t["functionDeclarations"]}

    assert "ds_list_projects" not in offered(requests_to_model[1])
    assert "ds_list_projects" in offered(requests_to_model[2])
    assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") == ["ds-b"]
    assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 1
    selection_logs = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("ds_selection ")
    ]
    assert sum("status=selected" in s for s in selection_logs) == 1
    assert sum("status=unchanged" in s for s in selection_logs) == 99
    assert all("turn=" in s and "tool_id=local/ds_select_clusters" in s for s in selection_logs)
    assert any(
        "call_ref=" + hashlib.sha256(b"selection-0").hexdigest()[:16] in s for s in selection_logs
    )
    assert all(
        "clusters" not in s.replace("ds_select_clusters", "") and "selection-0" not in s
        for s in selection_logs
    )


@pytest.fixture
async def selection(postgres_url):
    configs = [DSConfig.model_validate(source(c)) for c in ("ds-a", "ds-b")]
    contracts, policies = catalog(configs, dict.fromkeys(AUDIENCES, 20_000))
    grants, clock = Grants(), Clock()
    for c in contracts:
        grants.grant("alice", c.tool_id, target=c.target_id)
    ctx = context(
        tools=frozenset(c.tool_id for c in contracts),
        targets=frozenset({"ds-a", "ds-b", "dolphinscheduler"}),
        max_tool_calls=2,
    )

    async def authorize(identity, target, tool):
        await asyncio.sleep(0)  # 实际授权可挂起；比较与更新必须在它之后原子完成。
        return await grants(identity, target, tool)

    async with ready_engine(postgres_url) as engine:
        gov = GovernedTools(store(engine, authorize, clock, ToolCatalog(contracts, policies)))
        yield gov, ctx, grants


async def test_concurrent_selection_normalizes_but_charges_different_order(selection):
    gov, ctx, _ = selection
    await asyncio.gather(
        *[gov.select_dolphinscheduler(ctx, ("ds-a", "ds-b", "ds-a")) for _ in range(100)]
    )
    assert gov.checkpoint(ctx.identity).used == 1
    before = gov.turn_runs(ctx.identity)
    await gov.select_dolphinscheduler(ctx, ("ds-a", "ds-b"))
    assert gov.turn_runs(ctx.identity) is before
    await gov.select_dolphinscheduler(ctx, ("ds-b", "ds-a"))
    assert gov.checkpoint(ctx.identity).used == 2
    await gov.select_dolphinscheduler(ctx, ("ds-b", "ds-a"))  # 额度用完仍可幂等成功。
    with pytest.raises(ToolRejectedError) as caught:
        await gov.select_dolphinscheduler(ctx, ("ds-a",))
    assert caught.value.code == "budget_exhausted"
    assert gov.turn_runs(ctx.identity).ds_clusters == ("ds-b", "ds-a")


@pytest.mark.parametrize("revoked", ["dolphinscheduler", "ds-b"])
async def test_inherited_selection_is_already_effective_but_still_rechecks_authority(
    selection, revoked
):
    gov, ctx, grants = selection
    gov.load_dolphinscheduler_selection(ctx.identity, ("ds-b",))
    before = gov.turn_runs(ctx.identity)
    await gov.select_dolphinscheduler(ctx, ("ds-b", "ds-b"))
    assert gov.checkpoint(ctx.identity).used == 0
    assert gov.turn_runs(ctx.identity) is before
    grants.allowed = {grant for grant in grants.allowed if grant[1] != revoked}
    with pytest.raises(ToolRejectedError) as caught:
        await gov.select_dolphinscheduler(ctx, ("ds-b",))
    assert caught.value.code == "not_allowed"
    assert gov.checkpoint(ctx.identity).used == 0
    assert gov.turn_runs(ctx.identity) is before


async def test_selection_rejections_and_ds_read_failures_have_data_free_logs(
    env, monkeypatch, caplog
):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    caplog.set_level(logging.INFO, logger="xiaowei")
    with ds_api(fail=True) as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url)]))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "查 B，不能查就说明缺口",
                tool_call("ds_select_clusters", clusters=["nonexistent-private-target"]),
                tool_call("ds_select_clusters", clusters=[]),
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                list_failed(),
                ds_answer,
            )
            result = (await served.turn(message, "query")).json()
            assert result["state"] == "completed", result
            assert result["delivery"]["facts"] == []
            assert "认证" in json.dumps(result["delivery"], ensure_ascii=False)
            assert len(requests) == 1
            assert await served.finish() == 0
    logs = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith(("ds_selection ", "monitoring_read "))
    ]
    assert any("status=rejected code=not_allowed" in s for s in logs)
    assert any("status=rejected code=invalid_arguments" in s for s in logs)
    assert any(
        "tool_id=local/ds_list_workflow_instances target_id=ds-b status=ds_auth" in s for s in logs
    )
    assert all("turn=" in s and "call_ref=" in s for s in logs)
    assert all(
        "synthetic-token" not in s
        and "nonexistent-private-target" not in s
        and "clusters=" not in s
        for s in logs
    )


@pytest.mark.parametrize("case", ["catalog", "scope", "arguments"])
async def test_identical_selection_does_not_bypass_existing_validation(selection, case):
    gov, ctx, _ = selection
    chosen = ("unknown",) if case == "catalog" else ("ds-b",)
    gov.load_dolphinscheduler_selection(ctx.identity, chosen)
    before = gov.turn_runs(ctx.identity)
    if case == "scope":
        ctx = ctx.model_copy(update={"tool_scope": frozenset()})
    with pytest.raises(ToolRejectedError) as caught:
        await gov.select_dolphinscheduler(ctx, () if case == "arguments" else chosen)
    assert caught.value.code == ("invalid_arguments" if case == "arguments" else "not_allowed")
    assert gov.checkpoint(ctx.identity).used == 0
    assert gov.turn_runs(ctx.identity) is before
