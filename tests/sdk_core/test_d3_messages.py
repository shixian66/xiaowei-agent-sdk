"""D3 用户可见回答、真实 SDK post/降级协议；不证明真实客户端或模型能力。"""

import dataclasses
import json
from typing import Any

import pytest
from lark_channel.channel import FeishuChannel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from tests.sdk_core.mcp_fixture import serve
from tests.sdk_core.test_app import answer
from tests.sdk_core.test_channel_service import Env
from tests.sdk_core.test_channel_service import env as env
from tests.sdk_core.test_feishu import config, request_row, running
from tests.sdk_core.test_feishu_format import prose
from tests.sdk_core.test_feishu_render import delivery, fact, text_parts

from xiaowei.feishu import LarkTransport, lark_channel
from xiaowei.feishu_render import build_feishu_message, request_bytes

pytestmark = pytest.mark.loopback


def post_text(message: dict[str, Any]) -> str:
    return "\n".join(
        "".join(node["text"] for node in row) for row in message["post"]["zh_cn"]["content"]
    )


def test_answer_precedes_results_and_sources_are_folded() -> None:
    source = fact()
    message = build_feishu_message(delivery(source, analysis="先核对业务口径。"), 3500, "oc_test")
    visible = "\n".join(text_parts(message, folded=False))
    assert visible.index("先核对业务口径。") < visible.index('"orders"')
    assert source.note in visible  # 未分类限制不能因简化排版而隐藏。
    assert source.evidence_id not in visible and source.captured_at.isoformat() not in visible
    assert "查看依据" in visible and "模型推断" in visible
    full = "\n".join(text_parts(message))
    assert source.tool_id in full and source.evidence_id in full
    assert source.captured_at.isoformat() in full and source.metadata["sql"] in full


def test_prose_uses_safe_structured_post_without_active_external_nodes() -> None:
    raw = (
        "### 关键结论\n\n**检查** __name__ 和 [链接](https://invalid.example) "
        "<script>原样说明</script>。\n\n- 第一步\n- 第二步"
    )
    message = build_feishu_message(prose("建议", raw), 3500, "oc_test", allow_preview=False)
    nodes = [n for r in message["post"]["zh_cn"]["content"] for n in r]
    assert {n["tag"] for n in nodes} == {"text"}
    assert any(n.get("style") == ["bold"] and n["text"] == "检查" for n in nodes)
    shown = post_text(message)
    assert "__name__ 和 [链接](https://invalid.example) <script>原样说明</script>。" in shown
    assert "第一步" in shown and "第二步" in shown


@pytest.mark.parametrize("result", ["ok", "format_error", "missing_id"])
async def test_gateway_real_sdk_post_and_fallback_keep_saved_content_and_reply(
    env: Env, monkeypatch: pytest.MonkeyPatch, result: str
) -> None:
    captured: list[tuple[str, bytes]] = []

    async def token(_: Request) -> Response:
        return JSONResponse({"code": 0, "tenant_access_token": "t-synthetic", "expire": 7200})

    async def sent(request: Request) -> Response:
        raw = await request.body()
        captured.append((request.url.path, raw))
        if result == "format_error" and len(captured) == 1:
            return JSONResponse({"code": 230001, "msg": "invalid message content"})
        return JSONResponse(
            {"code": 0, "data": {} if result == "missing_id" else {"message_id": "om_post"}}
        )

    def endpoint(_: Any) -> Starlette:
        return Starlette(
            routes=[
                Route("/open-apis/auth/v3/tenant_access_token/internal", token, methods=["POST"]),
                Route("/open-apis/im/v1/messages", sent, methods=["POST"]),
            ]
        )

    text = "### 排查建议\n\n**先看配置** __name__\n\n- 核对目标\n- 明确时间"
    message = env.scripts.add("只解释，不查询", lambda call: answer((), "", advice=text))
    monkeypatch.setenv("XW_TEST_FEISHU_SECRET", "not-a-real-secret")
    with serve(endpoint, path="") as http:
        channel = FeishuChannel(
            config=dataclasses.replace(lark_channel(config()).config, domain=http.url)
        )
        transport = LarkTransport(channel, config())
        try:
            async with running(env) as fs:
                fs.gateway._send = transport.send
                # 正式入口装配使用的字段由 running fixture 公开，替换渠道 I/O，不绕业务处理。
                event = fs.event(message)
                await fs.gateway.receive(event)
                await fs.drain()
        finally:
            channel.stop(join_timeout=5)
    assert env.adapter.calls == [] and env.model_calls(message) == 1
    bodies = [json.loads(raw) for _, raw in captured]
    assert bodies[0]["msg_type"] == "post"
    post = {"post": json.loads(bodies[0]["content"])}
    assert len(captured[0][1]) == request_bytes(post, "oc_alice", reply_to=None)
    if result == "format_error":
        assert len(captured) == 2 and bodies[1]["msg_type"] == "text"
        assert json.loads(bodies[1]["content"])["text"] == post_text(post).strip()
        assert bodies[1]["uuid"] == bodies[0]["uuid"]
    elif result == "ok":
        assert len(captured) == 1
    assert all(path == "/open-apis/im/v1/messages" for path, _ in captured)
    assert all(body["receive_id"] == "oc_alice" for body in bodies)
    assert await request_row(env) == ("completed", "unknown" if result == "missing_id" else "sent")


async def test_registered_note_summary_keeps_business_limits_visible(postgres_url: Any) -> None:
    from tests.sdk_core.synthetic_tools import (
        CONTRACTS,
        POLICIES,
        TOTAL_TOOL,
        Clock,
        Grants,
        cited,
        context,
        ready_engine,
        store,
    )
    from tests.sdk_core.test_evidence import _answer, _record

    from xiaowei.governance import ToolCatalog

    full_note = "采集时间为读取时刻；默认窗不是指标执行窗。未返回单位/阈值。"
    summary = "仪表盘配置摘要；未查询实时指标，未返回单位/阈值。"
    policy = dataclasses.replace(POLICIES[0], fact_note=full_note, fact_note_summary=summary)
    grants, clock = Grants(), Clock()
    grants.grant("alice", TOTAL_TOOL)
    ctx = context(channel="feishu")
    async with ready_engine(postgres_url) as engine:
        evidence = store(engine, grants, clock, ToolCatalog(CONTRACTS, (policy,)))
        eid = await _record(evidence, ctx)
        delivered = await evidence.validate_answer(cited(_answer((eid,))), ctx)
        message = build_feishu_message(delivered, 3500, "oc_test", allow_preview=False)
    visible = "\n".join(text_parts(message, folded=False))
    assert summary in visible and full_note not in visible
    assert full_note in "\n".join(text_parts(message))


async def test_manual_resend_keeps_original_post_bytes_uuid_and_no_model_rerun(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.sdk_core.test_delivery_parts import Platform, parts, rejected, transmit

    from xiaowei.channel import RequestRef

    text = "**检查配置**\n\n- 核对已授权来源\n- 明确时间口径"
    message = env.scripts.add("只说明排查方法", lambda call: answer((), "", advice=text))
    await env.run(message, "om_d3_post", channel="feishu", conversation="oc_d1a")
    ref = RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_d1a", request_id="om_d3_post"
    )
    platform = Platform(outcomes=[rejected()])
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "failed"
    (bound,) = await parts(env, ref)
    original, uuid = bound.body, bound.uuid
    assert "post" in json.loads(original)

    def changed_renderer(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("已绑定消息不能重渲染")

    monkeypatch.setattr("xiaowei.feishu_delivery.build_feishu_message", changed_renderer)
    before = len(platform.sent)
    assert (
        await env.service.results.send(ref, transmit(env, ref, platform, resend=True), resend=True)
        == "sent"
    )
    assert len(platform.sent) == before + 1
    assert platform.sent[-1] == (json.loads(original), uuid)
    assert platform.files == [] and env.adapter.calls == [] and env.model_calls(message) == 1


@pytest.mark.parametrize("reply_to", [None, "om_parent"])
def test_post_capacity_uses_converted_ast_and_keeps_component_limit(reply_to: str | None) -> None:
    from xiaowei.feishu_render import MessageCapacityError

    source = prose("建议", "**原样标识符** __name__\n- " + '😀"&' * 15)
    message = build_feishu_message(source, 3500, "oc_test", reply_to=reply_to, allow_preview=False)
    assert "post" in message
    # 独立重建公开请求，测量 UTF-8 和两层 JSON 转义；不借产品计数得出期望。
    body = {
        "content": json.dumps(message["post"], ensure_ascii=False),
        "msg_type": "post",
        "uuid": "0" * 36,
    }
    if reply_to is None:
        body["receive_id"] = "oc_test"
    needed = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
    assert request_bytes(message, "oc_test", reply_to=reply_to) == needed
    assert (
        build_feishu_message(
            source, 3500, "oc_test", reply_to=reply_to, max_bytes=needed, allow_preview=False
        )
        == message
    )
    # 小于 post 且更小于卡片时必须换载体，不能依赖 SDK 隐式分段。
    with pytest.raises(MessageCapacityError):
        build_feishu_message(
            source, 3500, "oc_test", reply_to=reply_to, max_bytes=180, allow_preview=False
        )
    crowded = prose("建议", "**甲**乙" * 110)
    card = build_feishu_message(crowded, 3500, "oc_test", allow_preview=False)
    assert "card" in card  # 转换为 220 个 post 节点时改用完整卡片，不削内容。
    assert "甲" in "\n".join(text_parts(card)) and "乙" in "\n".join(text_parts(card))


def test_independent_approval_card_keeps_existing_material_and_labels() -> None:
    material = fact(
        requires_complete=True, metadata={"action_id": "ac_1", "approval_command": "/批准 ac_1"}
    )
    card = build_feishu_message(delivery(material), 3500, "oc_test", allow_preview=False)
    assert card["card"]["header"]["title"]["content"] == "小维 · 工具结果"
    visible = text_parts(card, folded=False)
    assert (
        f"[{material.evidence_id}] 来源 {material.tool_id} · 目标 {material.target_id}\n"
        f"采集于 {material.captured_at.isoformat()}" in visible
    )
    assert "说明：" + material.note in visible
    assert "批准命令：/批准 ac_1" in visible
    assert "查看依据" not in "\n".join(visible)


def test_grafana_summary_registration_and_visible_configuration_label() -> None:
    from tests.sdk_core.test_grafana_g3 import g3_config
    from tests.sdk_core.test_runtime import serve_config

    from xiaowei.runtime import ServeConfig, _monitoring_catalog

    cfg = ServeConfig.model_validate(serve_config(8501, **g3_config("http://127.0.0.1:12345/mcp")))
    _, policies = _monitoring_catalog(cfg)
    policy = next(p for p in policies if p.policy_id == "grafana.get_dashboard_summary")
    source = fact(
        tool_id="grafana-prod/get_dashboard_summary",
        target_id="grafana-prod",
        columns=(),
        rows=(),
        metadata={
            "title": "Node Exporter 基础监控",
            "panelCount": 24,
            "timeRange": '{"start":"now-30m","end":"now"}',
        },
        note=policy.fact_note,
        note_summary=policy.fact_note_summary,
    )
    message = build_feishu_message(
        delivery(source, analysis="找到这个看板。"), 3500, "oc_test", allow_preview=False
    )
    visible = "\n".join(text_parts(message, folded=False))
    assert "仪表盘配置摘要" in visible and "不是已执行的指标查询" in visible
    assert "未返回单位/阈值" in visible and "24" in visible
    assert source.tool_id not in visible and policy.fact_note not in visible
    assert policy.fact_note in "\n".join(text_parts(message))
