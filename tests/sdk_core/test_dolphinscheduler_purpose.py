"""DS 用途边界：正式装配与入口，模型/DS/飞书只在底层 I/O 使用离线替身。"""

import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from tests.p1b.test_starrocks_adapter import driver
from tests.sdk_core.synthetic_tools import Clock, Grants, context, ready_engine, s3_answer, store
from tests.sdk_core.test_app import cite, clarify, last_output, tool_call
from tests.sdk_core.test_dolphinscheduler_runtime import (
    DS_TOOLS,
    config_values,
    ds_answer,
    ds_api,
    list_failed,
    source,
)
from tests.sdk_core.test_feishu import FakeChannel
from tests.sdk_core.test_feishu_images import _png
from tests.sdk_core.test_gate0 import _vertex_body
from tests.sdk_core.test_group_gateway import GroupChannel, raw_group_event, runtime_group
from tests.sdk_core.test_runtime import PLAN, SR, Env, feishu_config, feishu_event
from tests.sdk_core.test_runtime import env as env

from xiaowei.dolphinscheduler import DSConfig, catalog
from xiaowei.governance import GovernedTools, ToolCatalog, ToolRejectedError
from xiaowei.models import AUDIENCES, ToolRequest
from xiaowei.operations_history import TOOLS as HISTORY_TOOLS
from xiaowei.runtime import ServeConfig, _app_config

pytestmark = pytest.mark.loopback
DS_NAMES = {tool.split("/", 1)[1] for tool in DS_TOOLS}


async def test_query_diagnose_query_preserves_ds_choice_and_database_diagnosis(
    env: Env, monkeypatch
):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    env.drv = driver(PLAN)
    with ds_api() as (url, requests):
        values = config_values(env.port, [source(base_url=url)])
        values["data_policy"]["model_tools"] += sorted(HISTORY_TOOLS)
        values["access"]["grants"]["operator"] += sorted(HISTORY_TOOLS)
        async with env.running(ServeConfig.model_validate(values)) as served:
            await served.page()
            first = env.scripts.add(
                "查 DS B 的失败工作流",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                list_failed(),
                ds_answer,
            )
            assert (await served.turn(first, "query", "first")).json()["state"] == "completed"
            assert len(requests) == 1
            for call in env.scripts.calls[first]:
                queries = call.instructions.split("以下是查询类工具：", 1)[1].split("。", 1)[0]
                assert "ds_select_clusters" not in queries
                assert "ds_list_workflow_instances" in queries
                assert "DS 工作方法" in call.instructions
            old_turn = await env.scalar("SELECT turn_id FROM xiaowei_request")

            def plan_after_history(call):
                assert "orders_failed" in str(last_output(call)["data"])
                return tool_call(
                    "explain_query", cluster=SR.target_id, sql="SELECT region FROM shop.sales"
                )(call)

            message = env.scripts.add(
                "仅诊断这条 SQL，并复核已保存的 DS 结果，不继续查 DS",
                tool_call("history__read_operation", record_ref=f"turn:{old_turn}"),
                plan_after_history,
                cite("根据执行计划分析，DS 信息是历史事实"),
            )
            result = (await served.turn(message, "diagnose", "plan")).json()
            assert result["state"] == "completed", result
            assert any(f["tool_id"] == "local/explain_query" for f in result["delivery"]["facts"])
            for offered in env.scripts.tools_seen(message):
                assert offered.isdisjoint(DS_NAMES)
                assert "explain_query" in offered and "run_readonly_query" not in offered
                assert {t.replace("/", "__") for t in HISTORY_TOOLS} <= offered
            assert len(requests) == 1
            assert all(
                "DS 工作方法" not in call.instructions for call in env.scripts.calls[message]
            )
            follow = env.scripts.add("普通对话继续查 DS B 今天的失败", list_failed(), ds_answer)
            assert (await served.turn(follow, "query", "follow")).json()["state"] == "completed"
            assert len(requests) == 2  # 沿用已提交选择，不重复选择或额外查询。
            assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") == ["ds-b"]
            assert await served.finish() == 0


@pytest.mark.parametrize("forced", ["select", "read"])
async def test_diagnose_forced_ds_call_is_rejected_without_io(env: Env, monkeypatch, forced):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url)]))
        async with env.running(config) as served:
            await served.page()
            first = env.scripts.add(
                "选 DS B", tool_call("ds_select_clusters", clusters=["ds-b"]), clarify("已选 B")
            )
            assert (await served.turn(first, "query", "select")).json()["state"] == "completed"
            message = env.scripts.add(
                "仅诊断，模型却试图使用 DS",
                tool_call("ds_select_clusters", clusters=["ds-b"])
                if forced == "select"
                else list_failed(),
                ds_answer,
            )
            result = (await served.turn(message, "diagnose", "forced")).json()
            assert result["state"] == "failed", result
            assert len(env.scripts.calls[message]) == 1
            assert requests == []
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 0
            assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") == ["ds-b"]
            assert await served.finish() == 0


async def test_diagnose_governance_rejects_all_ds_tools_before_adapter(postgres_url):
    """即使绕过 SDK 可见工具集合，可信用途 scope 仍在选择/读取入口拒绝。"""
    settings = ServeConfig.model_validate(config_values(18501, [source()]))
    contracts, policies = catalog(
        [DSConfig.model_validate(source())], dict.fromkeys(AUDIENCES, 20_000)
    )
    grants, clock = Grants(), Clock()
    for contract in contracts:
        grants.grant("alice", contract.tool_id, target=contract.target_id)
    ctx = context(
        tools=_app_config(settings).purposes["diagnose"],
        targets=frozenset({"ds-b", "dolphinscheduler"}),
    )
    calls = []

    async def execute(request):
        calls.append(request)
        raise AssertionError("diagnose 不得到达 DS Adapter")

    async with ready_engine(postgres_url) as engine:
        gov = GovernedTools(store(engine, grants, clock, ToolCatalog(contracts, policies)))
        gov.load_dolphinscheduler_selection(ctx.identity, ("ds-b",))
        before = gov.checkpoint(ctx.identity)
        with pytest.raises(ToolRejectedError) as denied:
            await gov.select_dolphinscheduler(ctx, ("ds-b",))
        assert denied.value.code == "not_allowed"
        for name in sorted(DS_TOOLS - {"local/ds_select_clusters"}):
            request = ToolRequest(
                tool_id=name,
                target_id="ds-b",
                tool_name=name.split("/", 1)[1],
                call_id=name,
                arguments={"cluster": "ds-b"},
            )
            with pytest.raises(ToolRejectedError) as denied:
                await gov.invoke(ctx, request, execute)
            assert denied.value.code == "not_allowed"
        assert calls == [] and gov.checkpoint(ctx.identity) == before


@pytest.mark.parametrize("entry", ["command", "txt", "md", "image", "image-post"])
async def test_formal_feishu_diagnose_entries_hide_ds(env: Env, monkeypatch, entry):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    calls, downloads = [], []

    def endpoint(request):
        calls.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json=_vertex_body(
                [{"text": json.dumps(s3_answer(text="请说明要分析的错误。", awaiting_user=True))}]
            ),
        )

    async def download(mid, key, kind, *, max_bytes):
        downloads.append((mid, key, kind))
        return _png() if kind == "image" else b"SQLState 42S02"

    monkeypatch.setattr(
        "xiaowei.feishu_resource.LarkResourceDownloader",
        lambda _settings: SimpleNamespace(download=download),
    )
    with ds_api() as (url, requests):
        values = config_values(env.port, [source(base_url=url)])
        values["access"]["grants"]["alice"] += sorted(DS_TOOLS | HISTORY_TOOLS)
        values["data_policy"]["model_tools"] += sorted(HISTORY_TOOLS)
        values["model"].update(
            provider="vertex",
            model="gemini-3-flash-preview",
            base_url=None,
            api_mode=None,
            output_mode=None,
            image_input_enabled=True,
        )
        values["storage"].update(request_retention_seconds=7200, evidence_retention_seconds=7200)
        values["feishu"] = feishu_config(image_input_enabled=True, document_input_enabled=True)
        if entry == "image-post":
            values["feishu"] = runtime_group(tools=values["data_policy"]["model_tools"])
            values["feishu"].update(image_input_enabled=True, document_input_enabled=True)
            event = raw_group_event(env, "看看这个错误", "om_purpose")
        else:
            event = feishu_event(env, "/诊断 查看错误", "om_purpose")
        message = event["event"]["message"]
        if entry in {"txt", "md"}:
            message.update(
                message_type="file",
                content=json.dumps(
                    {
                        "file_key": "file_test",
                        "file_name": f"error.{entry}",
                    }
                ),
            )
        elif entry == "image":
            message.update(message_type="image", content=json.dumps({"image_key": "img_test"}))
        elif entry == "image-post":
            message.update(
                message_type="post",
                content=json.dumps(
                    {
                        "zh_cn": {
                            "content": [
                                [
                                    {"tag": "text", "text": "看看这个错误"},
                                    {"tag": "img", "image_key": "img_test"},
                                ]
                            ]
                        }
                    }
                ),
            )
        channel = GroupChannel() if entry == "image-post" else FakeChannel()
        async with env.running(
            ServeConfig.model_validate(values),
            feishu_channel=channel,
            model_transport=httpx2.MockTransport(endpoint),
        ) as served:
            channel.emit_raw_from_sdk_thread(event)
            async with asyncio.timeout(20):
                while await env.scalar("SELECT delivery FROM xiaowei_request") != "sent":
                    await asyncio.sleep(0.02)
            assert await env.scalar("SELECT mode FROM xiaowei_request") == "diagnose"
            assert await env.scalar("SELECT state FROM xiaowei_request") == "completed"
            assert len(calls) == 1
            assert "DS 工作方法" not in str(calls[0]["systemInstruction"])
            offered = {d["name"] for t in calls[0]["tools"] for d in t["functionDeclarations"]}
            assert offered.isdisjoint(DS_NAMES)
            assert "explain_query" in offered and "run_readonly_query" not in offered
            assert {t.replace("/", "__") for t in HISTORY_TOOLS} <= offered
            assert requests == [] and env.drv.attempts == 0
            assert len(downloads) == (0 if entry == "command" else 1)
            assert await served.finish() == 0
