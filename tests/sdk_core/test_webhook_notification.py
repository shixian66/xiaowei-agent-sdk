"""A：真实群入口/SDK/治理/隔离 PG；只替换模型、成员目录、数据库驱动及 HTTP。

脚本模型不证明自然语言选择能力；MockTransport 不证明真实飞书接口或群内到达。
"""

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx2
import pytest
from sqlalchemy import text as sql
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.p1b.test_starrocks_adapter import Result, driver
from tests.sdk_core.test_app import cite, evidence_in, tool_call, upstream_error
from tests.sdk_core.test_feishu import Outbox
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_gateway import BOT, Group, group_config
from tests.sdk_core.test_group_identity import APP, CHAT, GROUP, A, B
from tests.sdk_core.test_runtime import env as env  # pytest fixture
from tests.sdk_core.test_runtime import feishu_config, serve_config

from xiaowei import runtime
from xiaowei.evidence import EvidenceStoreError
from xiaowei.feishu import FeishuGateway, attributed
from xiaowei.starrocks_tools import QUERY_TOOLS
from xiaowei.webhook import resolve_bot

pytestmark = pytest.mark.loopback
TOOL = "notifications/send_webhook_message"
URL = "https://open.feishu.cn/open-apis/bot/v2/hook/synthetic-webhook-token"
TEXT = "今晚 22:00 检查 StarRocks。"


def script(env, message, *steps):
    env.scripts.add(attributed(A, message), *steps)
    return message


def configured(env, *, sender=True, bots=None):
    data = serve_config(
        env.port,
        feishu=feishu_config(
            app_id=APP,
            users={},
            stop_timeout_seconds=7,
            group={
                **group_config().group.model_dump(mode="json"),
                "tools": sorted(QUERY_TOOLS | {TOOL}),
            },
        ),
        webhook_bots=bots
        or [
            {
                "bot_id": "ops",
                "alias": "ops_alert_bot",
                "description": "运维告警机器人",
                "target_version": "v1",
                "url_ref": "env:XW_TEST_WEBHOOK_URL",
                "signing_secret_ref": None,
                "receive_tools": sorted(QUERY_TOOLS),
                "receive_targets": [SR.target_id],
            }
        ],
    )
    data["data_policy"]["model_tools"].append(TOOL)
    data["access"]["grants"][A] = [TOOL] if sender else sorted(QUERY_TOOLS)
    return runtime.ServeConfig.model_validate(data)


@asynccontextmanager
async def running(
    env, monkeypatch, *, sender=True, response=None, member_check=None, bots=None, config=None
):
    monkeypatch.setenv("XW_TEST_WEBHOOK_URL", URL)
    requests = []

    async def post(request):
        requests.append(request)
        if callable(response):
            return await response(request)
        if isinstance(response, Exception):
            raise response
        return response or httpx2.Response(200, json={"code": 0, "msg": "success"})

    async def member(chat, subject):
        if member_check:
            return await member_check(chat, subject)
        return chat == CHAT and subject == A

    cfg = config or configured(env, sender=sender, bots=bots)
    async with runtime.open_runtime(
        cfg,
        clock=env.clock,
        model_transport=env.scripts.transport(),
        starrocks_connect=env.connect(cfg),
        members=member,
        webhook_transport=httpx2.MockTransport(post),
    ) as rt:
        outbox = Outbox()
        gateway = FeishuGateway(
            rt.service,
            cfg.feishu,
            outbox,
            clock=env.clock,
            bot_open_id=lambda: BOT,
        )
        task = asyncio.create_task(gateway.run())
        group = Group(SimpleNamespace(clock=env.clock), gateway, outbox)
        try:
            yield group, requests, rt
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_u1_group_entry_posts_once_and_reports_api_acceptance(env, monkeypatch):
    request = script(
        env,
        "给 ops_alert_bot 发消息：" + TEXT,
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite("Webhook 接口已接受消息。"),
    )
    async with running(env, monkeypatch) as (group, requests, _rt):
        event = await group.ask(request)
        await group.gateway.receive(event)  # 平台重投不新增请求/POST
        await group.gateway.idle()
        assert len(requests) == 1
        assert json.loads(requests[0].content) == {"msg_type": "text", "content": {"text": TEXT}}
        assert "accepted" in message_text(group.outbox.sent[-1][1])


@pytest.mark.parametrize("alias", ["unknown_bot", "http://127.0.0.1/private"])
async def test_u2_unknown_bot_or_url_is_rejected_before_http(env, monkeypatch, alias):
    request = script(
        env,
        "发到未登记目标 " + alias,
        tool_call("send_webhook_message", bot_alias=alias, text=TEXT, evidence_ids=[]),
        cite("该机器人尚未配置或授权，未发送消息。"),
    )
    async with running(env, monkeypatch) as (group, requests, _rt):
        await group.ask(request)
        assert requests == []
        assert "未发送" in message_text(group.outbox.sent[-1][1])


async def test_same_text_with_new_call_id_still_posts_once(env, monkeypatch):
    request = script(
        env,
        "同一条消息发一次",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch) as (group, requests, _rt):
        await group.ask(request)
        assert len(requests) == 1


async def test_u1_multiline_text_is_exact_after_unrelated_query_history(env, monkeypatch):
    query = script(
        env,
        "先查一次销售",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        cite(),
    )
    message = "第一行：22:00 检查\r\n第二行：23:00 复核"
    send = script(
        env,
        "给机器人发两行文字",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=message, evidence_ids=[]),
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=message, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch) as (group, requests, _rt):
        await group.ask(query)
        await group.ask(send)
        assert len(requests) == 1
        assert json.loads(requests[0].content)["content"]["text"] == message.replace("\r\n", "\n")


@pytest.mark.parametrize("origin", ["text", "facts", "model"])
async def test_webhook_text_neutralizes_mentions_before_deduplication(env, monkeypatch, origin):
    tags = '<at user_id="all">所有人</at> <AT user_id="ou_member">成员</AT>'
    safe_tags = '＜at user_id="all">所有人</at> ＜AT user_id="ou_member">成员</AT>'
    text = "第一行：检查\r\n" + tags + "\n第二行：复核"
    if origin == "facts":
        env.drv = driver(Result(("region", "note"), [("east", tags)]))
    if origin != "text":
        sql_text = (
            "SELECT region, "
            """'<at user_id="all">所有人</at> <AT user_id="ou_member">成员</AT>' AS note """
            "FROM shop.sales"
            if origin == "facts"
            else "SELECT region, total FROM shop.sales"
        )
        query = script(
            env,
            "先取得待转发事实",
            tool_call("run_readonly_query", cluster=SR.target_id, sql=sql_text),
            cite(),
        )

    def send(call):
        # 后一次模型调用也看见首个通知回执，但本次始终选择同一份业务事实。
        selected = evidence_in(call)[:1] if origin != "text" else []
        return tool_call(
            "send_webhook_message",
            bot_alias="ops_alert_bot",
            text="巡检结果\n请复核" if origin == "facts" else text,
            evidence_ids=selected,
        )(call)

    request = script(env, "发送一次含特殊文字的消息", send, send, cite())
    async with running(env, monkeypatch) as (group, requests, _rt):
        if origin != "text":
            await group.ask(query)
        await group.ask(request)
        assert len(requests) == 1
        content = json.loads(requests[0].content)["content"]["text"]
        assert safe_tags in content
        assert "<at" not in content.lower()
        if origin == "text":
            assert content == "第一行：检查\n" + safe_tags + "\n第二行：复核"
        elif origin == "model":
            assert "模型说明（未经系统核实）\n第一行：检查\n" + safe_tags in content
        else:
            assert "note: " + safe_tags in content and "巡检结果\n请复核" in content
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 1
        assert (
            await env.scalar("SELECT content_digest FROM xiaowei_notification_attempt")
            == hashlib.sha256(content.encode()).hexdigest()
        )


def test_webhook_action_material_omits_foreign_group_approval_command():
    from tests.sdk_core.test_feishu_render import fact

    from xiaowei.models import Delivery
    from xiaowei.webhook import _notification_text

    pending = fact(
        section="action",
        metadata={
            "action_id": "act_example",
            "state": "pending",
            "impact": "仅合成对象",
            "approval_command": "@小维 /批准 act_example",
        },
    )
    material = Delivery(
        content="",
        channel="feishu",
        evidence_ids=(pending.evidence_id,),
        facts=(pending,),
    )
    content = _notification_text(material, "")
    assert "approval_command" not in content and "/批准" not in content
    assert "action_id: act_example" in content and "state: pending" in content
    assert "impact: 仅合成对象" in content
    assert pending.metadata["approval_command"] == "@小维 /批准 act_example"


async def test_text_notification_does_not_forward_unselected_current_query(env, monkeypatch):
    request = script(
        env,
        "先查销售留在本群，然后给机器人只发一条提醒",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch) as (group, requests, _rt):
        await group.ask(request)
        assert len(requests) == 1
        content = json.loads(requests[0].content)["content"]["text"]
        assert TEXT in content
        assert "east" not in content and "SELECT" not in content
        # 原群的正常结果交付仍完整，通知只改变自己选择外发的内容。
        assert "east" in message_text(group.outbox.sent[-1][1])


async def test_non_sender_can_query_after_shared_notification_but_cannot_send(env, monkeypatch):
    first = script(
        env,
        "发一条提醒",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    second = "继续查询销售"

    def query_without_send_tool(call):
        assert "send_webhook_message" not in call.tools
        return tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        )(call)

    env.scripts.add(attributed(B, second), query_without_send_tool, cite("查询继续完成"))

    async def members(chat, subject):
        return chat == CHAT and subject in {A, B}

    async with running(env, monkeypatch, member_check=members) as (group, requests, rt):
        await group.ask(first)
        await group.ask(second, sender=B)
        reply = message_text(group.outbox.sent[-1][1])
        assert "查询继续完成" in reply and "east" in reply
        assert len(requests) == 1
        # 可读共享回执绝不授予实际发送权限。
        from xiaowei.models import Identity

        actor = Identity(
            subject_id=B, session_id="test", turn_id="test", channel="feishu", owner=GROUP.owner
        )
        assert not await rt.service.results.access.authorize(actor, "notifications", TOOL)


async def test_receipt_read_failure_reports_unverifiable_without_stopping_service(env, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError

    request = script(
        env,
        "消息发完后回执查询暂时失败",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    intercepted = []

    def fail_receipt(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT bot_alias,state,reason FROM xiaowei_notification_attempt"):
            intercepted.append(statement)
            raise SQLAlchemyError("synthetic receipt read failure")

    async with running(env, monkeypatch, response=httpx2.ReadTimeout("lost")) as (
        group,
        requests,
        rt,
    ):
        event.listen(rt.engine.sync_engine, "before_cursor_execute", fail_receipt)
        try:
            await group.ask(request)
        finally:
            event.remove(rt.engine.sync_engine, "before_cursor_execute", fail_receipt)
        assert intercepted and len(requests) == 1
        assert "发送状态暂无法核验" in message_text(group.outbox.sent[-1][1])
        assert "重发可能重复" in message_text(group.outbox.sent[-1][1])
        assert rt.readiness.ok


async def test_revoked_sender_can_still_see_unknown_warning_in_original_group(env, monkeypatch):
    access = []

    def revoke_and_fail(call):
        access[0]._grants = {}  # 模拟后续交付时已撤销个人发送许可
        return upstream_error(call)

    request = script(
        env,
        "结果未知后撤销发送权，不能把安全回执一起隐藏",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        revoke_and_fail,
    )
    async with running(env, monkeypatch, response=httpx2.ReadTimeout("lost")) as (
        group,
        requests,
        rt,
    ):
        access.append(rt.service.results.access)
        await group.ask(request)
        assert len(requests) == 1
        assert "重发可能重复" in message_text(group.outbox.sent[-1][1])


async def test_current_group_membership_is_checked_before_post(env, monkeypatch):
    current_member = [True]

    async def member(chat, subject):
        return current_member[0]

    def send_after_departure(call):
        current_member[0] = False  # 真 Runner 已开始，工具尚未执行
        return tool_call(
            "send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]
        )(call)

    request = script(
        env,
        "开始后离群，不得发送",
        send_after_departure,
        cite(),
    )
    async with running(env, monkeypatch, member_check=member) as (group, requests, _rt):
        await group.ask(request)
        assert env.scripts.calls
        assert requests == []


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (httpx2.Response(200, json={"code": 19021}), "failed"),
        (httpx2.Response(403, json={"code": 19022}), "failed"),
        (httpx2.Response(302, headers={"location": "http://127.0.0.1/private"}), "failed"),
        (httpx2.Response(500), "unknown"),
        (httpx2.Response(200, json={"code": False}), "unknown"),
        (httpx2.Response(200, content=b"broken response"), "unknown"),
        (httpx2.ReadTimeout("synthetic-token-must-not-leak"), "unknown"),
        (httpx2.ConnectError("synthetic-token-must-not-leak"), "failed"),
    ],
)
async def test_u4_outcome_is_truthful_and_does_not_retry(env, monkeypatch, response, status):
    request = script(
        env,
        "发送一条测试消息",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch, response=response) as (group, requests, _rt):
        await group.ask(request)
        assert len(requests) == 1
        reply = message_text(group.outbox.sent[-1][1])
        assert status in reply
        if status == "unknown":
            assert "可能已经发过" in reply


async def test_accepted_is_visible_after_model_failure(env, monkeypatch):
    request = script(
        env,
        "发完后模型故障",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        upstream_error,
    )
    async with running(env, monkeypatch) as (group, requests, _rt):
        await group.ask(request)
        assert len(requests) == 1
        assert "消息已提交" in message_text(group.outbox.sent[-1][1])


async def test_u3_historical_facts_use_code_rendering(env, monkeypatch):
    query = script(
        env,
        "查销售",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        cite(),
    )

    def forward(call):
        return tool_call(
            "send_webhook_message",
            bot_alias="ops_alert_bot",
            text="巡检说明第一行\n第二行结论",
            evidence_ids=evidence_in(call),
        )(call)

    send = script(env, "把刚才结果发到运维机器人", forward, cite())
    async with running(env, monkeypatch) as (group, requests, _rt):
        await group.ask(query)
        await group.ask(send)
        assert len(requests) == 1
        content = json.loads(requests[0].content)["content"]["text"]
        assert "工具结果（系统根据证据生成）" in content
        assert "模型说明（未经系统核实）" in content
        assert "100" in content and "east" in content
        assert "巡检说明第一行\n第二行结论" in content
        assert "ev_" not in content and '"rows"' not in content


async def test_unreferenced_historical_data_still_requires_recipient_permission(env, monkeypatch):
    bots = [configured(env).webhook_bots[0].model_copy(update={"receive_tools": frozenset()})]
    query = script(
        env,
        "查销售历史",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        cite(),
    )
    send = script(
        env,
        "发一段说明",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite("接收范围不允许，未发送消息。"),
    )
    async with running(env, monkeypatch, bots=bots) as (group, requests, _rt):
        await group.ask(query)
        await group.ask(send)
        assert requests == []
        assert "未发送" in message_text(group.outbox.sent[-1][1])


async def test_sender_permission_is_independent_and_hidden(env, monkeypatch):
    def probe(call):
        assert "send_webhook_message" not in call.tools
        return cite(
            "没有发送权限，未发送消息。",
        )(call)

    request = script(env, "帮我发一条", probe)
    async with running(env, monkeypatch, sender=False) as (group, requests, _rt):
        await group.ask(request)
        assert requests == []


async def test_unknown_allows_explicit_new_turn_after_delivered_warning(env, monkeypatch):
    count = []

    async def post(request):
        count.append(request)
        if len(count) == 1:
            raise httpx2.ReadTimeout("synthetic failure")
        return httpx2.Response(200, json={"code": 0})

    first = script(
        env,
        "发测试消息",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    again = script(
        env,
        "再发一次",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch, response=post) as (group, requests, _rt):
        await group.ask(first)
        assert "重发可能重复" in message_text(group.outbox.sent[-1][1])
        await group.ask(again)
        assert len(requests) == 2
        assert "accepted" in message_text(group.outbox.sent[-1][1])


async def test_unknown_with_undelivered_warning_does_not_post_in_next_turn(env, monkeypatch):
    first = script(
        env,
        "发后回执也丢失",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    again = script(
        env,
        "请再发一次",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch, response=httpx2.ReadTimeout("lost")) as (
        group,
        requests,
        _rt,
    ):
        group.outbox.outcomes.append("failed")
        await group.ask(first)
        await group.ask(again)
        assert len(requests) == 1
        assert "本轮未发送" in message_text(group.outbox.sent[-1][1])


async def test_unknown_capacity_failed_parent_preserves_warning_and_blocks_resend(env, monkeypatch):
    from functools import partial

    from xiaowei import feishu_delivery

    monkeypatch.setattr(
        feishu_delivery,
        "build_feishu_message",
        partial(feishu_delivery.build_feishu_message, max_bytes=1000),
    )
    first = script(
        env,
        "发送后只有容量提示能放下",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    again = script(
        env,
        "仍然要再发一次",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )

    async with running(env, monkeypatch, response=httpx2.ReadTimeout("lost")) as (
        group,
        requests,
        _rt,
    ):
        await group.ask(first)
        first_reply = message_text(group.outbox.sent[-1][1])
        assert "预览无法" in first_reply and "重发可能重复" in first_reply
        # 必需文件未交付，预览只能尽力提示；整个父交付仍失败，不能解除风险门槛。
        assert await env.scalar("SELECT delivery FROM xiaowei_request") == "failed"
        await group.ask(again)
        assert (
            await env.scalar(
                "SELECT reason FROM xiaowei_notification_attempt ORDER BY sequence DESC LIMIT 1"
            )
            == "duplicate_warning"
        )
        assert len(requests) == 1  # 已证明父交付失败；原成功交付后的二次 POST 用例另行保持。


async def test_unknown_gate_is_shared_across_group_senders(env, monkeypatch):
    first = script(
        env,
        "A发出但警告未送达",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    second = "B要求发同一条"
    third = "风险已提示，B仍明确要求发送"
    for message in (second, third):
        env.scripts.add(
            attributed(B, message),
            tool_call(
                "send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]
            ),
            cite(),
        )

    async def members(chat, subject):
        return chat == CHAT and subject in {A, B}

    cfg = configured(env)
    cfg = cfg.model_copy(
        update={
            "access": cfg.access.model_copy(
                update={
                    "grants": {**cfg.access.grants, B: frozenset({TOOL})},
                }
            )
        }
    )
    async with running(
        env, monkeypatch, config=cfg, member_check=members, response=httpx2.ReadTimeout("lost")
    ) as (group, requests, _rt):
        group.outbox.outcomes.append("failed")
        await group.ask(first)
        await group.ask(second, sender=B)
        assert len(requests) == 1
        assert "本轮未发送" in message_text(group.outbox.sent[-1][1])
        await group.ask(third, sender=B)
        assert len(requests) == 2


async def test_accepted_survives_evidence_record_failure(env, monkeypatch):
    request = script(
        env,
        "发送后证据落库故障",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
    )
    async with running(env, monkeypatch) as (group, requests, rt):
        evidence = rt.service.results.evidence
        original = evidence.record

        async def fail_record(ctx, request, observation):
            if request.tool_id == TOOL:
                raise EvidenceStoreError("synthetic evidence failure")
            return await original(ctx, request, observation)

        monkeypatch.setattr(evidence, "record", fail_record)
        await group.ask(request)
        assert len(requests) == 1
        assert "消息已提交" in message_text(group.outbox.sent[-1][1])


async def test_accepted_survives_session_commit_failure(env, monkeypatch):
    from xiaowei.session import PolicySession, SessionStoreError

    request = script(
        env,
        "发送后会话提交失败",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )

    async def fail_commit(self, *args, **kwargs):
        raise SessionStoreError("synthetic session commit failure")

    monkeypatch.setattr(PolicySession, "commit_validated", fail_commit)
    async with running(env, monkeypatch) as (group, requests, _rt):
        await group.ask(request)
        assert len(requests) == 1
        assert "消息已提交" in message_text(group.outbox.sent[-1][1])


@pytest.mark.parametrize("failure", ["cancel", "settle"])
async def test_post_started_but_unsettled_recovers_as_unknown_without_retry(
    env, monkeypatch, failure
):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError

    from xiaowei.channel import InboundRequest

    entered = asyncio.Event()
    release = asyncio.Event()

    async def post(request):
        entered.set()
        if failure == "cancel":
            await release.wait()
        return httpx2.Response(200, json={"code": 0})

    def fail_settle(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE xiaowei_notification_attempt SET state="):
            raise SQLAlchemyError("synthetic settlement failure")

    message = script(
        env,
        "发送开始后中断",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch, response=post) as (_group, requests, rt):
        receipt = await rt.service.accept(
            InboundRequest(
                channel="feishu",
                channel_request_id="om_fault",
                subject_id=A,
                conversation_id=CHAT,
                mode="query",
                message=attributed(A, message),
                received_at=env.clock(),
                group=GROUP,
            )
        )
        if failure == "settle":
            event.listen(rt.engine.sync_engine, "before_cursor_execute", fail_settle)
        task = asyncio.create_task(rt.service.process(receipt))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if failure == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                record = await task
                assert record.state == "failed"
        finally:
            if failure == "settle":
                event.remove(rt.engine.sync_engine, "before_cursor_execute", fail_settle)
        assert len(requests) == 1 and not rt.readiness.ok
        async with rt.engine.connect() as conn:
            assert (
                await conn.execute(sql("SELECT state FROM xiaowei_notification_attempt"))
            ).scalar_one() == "sending"
    async with running(env, monkeypatch) as (_group, requests, rt):
        assert requests == []
        async with rt.engine.connect() as conn:
            assert (
                await conn.execute(sql("SELECT state FROM xiaowei_notification_attempt"))
            ).scalar_one() == "unknown"


async def test_inactive_group_request_is_not_sent_without_aborting_tool(env, monkeypatch):
    from agents.tool_context import ToolContext

    from xiaowei.models import Budget, Identity, RunContext

    async with running(env, monkeypatch) as (_group, requests, rt):
        ctx = RunContext(
            identity=Identity(
                subject_id=A,
                session_id="inactive",
                turn_id="inactive",
                channel="feishu",
                owner=GROUP.owner,
            ),
            tool_scope=frozenset({TOOL}),
            target_scope=frozenset({"notifications"}),
            budget=Budget(max_turns=2, max_tool_calls=2, timeout_seconds=5, max_scope_checks=10),
        )
        notifications = rt.service._app.notifications
        assert notifications is not None
        tool = notifications.tool(ctx, lambda: ())
        arguments = json.dumps({"bot_alias": "ops_alert_bot", "text": TEXT, "evidence_ids": []})
        result = json.loads(
            await tool.on_invoke_tool(
                ToolContext(
                    context=ctx,
                    tool_name=tool.name,
                    tool_call_id="inactive",
                    tool_arguments=arguments,
                ),
                arguments,
            )
        )
        assert result["data"]["status"] == "not_sent"
        assert result["data"]["reason"] == "request_not_active"
        assert requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0


async def test_same_target_credential_rotation_does_not_reset_attempts(env, monkeypatch):
    message = script(
        env,
        "轮换前发送",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch, response=httpx2.ReadTimeout("lost")) as (
        group,
        requests,
        rt,
    ):
        await group.ask(message)
        assert len(requests) == 1
        async with rt.engine.connect() as conn:
            before = (
                await conn.execute(
                    sql(
                        "SELECT receipt_id,content_digest,target_version,state "
                        "FROM xiaowei_notification_attempt"
                    )
                )
            ).all()
    rotated = configured(env).webhook_bots[0].model_copy(update={"url_ref": "env:XW_ROTATED_URL"})
    monkeypatch.setenv("XW_ROTATED_URL", URL + "-rotated")
    async with running(env, monkeypatch, bots=[rotated]) as (_group, requests, rt):
        assert requests == []
        async with rt.engine.connect() as conn:
            after = (
                await conn.execute(
                    sql(
                        "SELECT receipt_id,content_digest,target_version,state "
                        "FROM xiaowei_notification_attempt"
                    )
                )
            ).all()
        assert before == after


async def test_credentials_are_absent_from_model_history_logs_and_answer(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    request = script(
        env,
        "发送安全检查消息",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch) as (group, _requests, rt):
        await group.ask(request)
        async with rt.engine.connect() as conn:
            history = (await conn.execute(sql("SELECT message_data FROM agent_messages"))).all()
            evidence = (
                await conn.execute(
                    sql("SELECT model_content,session_content,feishu_content FROM xiaowei_evidence")
                )
            ).all()
        combined = str(history) + str(evidence) + str(env.scripts.calls) + caplog.text
        combined += str(group.outbox.sent)
        assert "synthetic-webhook-token" not in combined
        assert URL not in combined


@pytest.mark.parametrize(
    "url",
    [
        "http://open.feishu.cn/open-apis/bot/v2/hook/token",
        "https://127.0.0.1/open-apis/bot/v2/hook/token",
        "https://open.feishu.cn.evil.test/open-apis/bot/v2/hook/token",
        "https://open.feishu.cn/open-apis/bot/v2/hook/token?redirect=private",
        "https://user@open.feishu.cn/open-apis/bot/v2/hook/token",
    ],
)
def test_ssrf_config_rejected_without_io(env, monkeypatch, url):
    monkeypatch.setenv("XW_TEST_WEBHOOK_URL", url)
    bot = configured(env).webhook_bots[0]
    with pytest.raises(ValueError) as rejected:
        resolve_bot(bot)
    assert url not in str(rejected.value)


async def test_two_explicit_messages_and_concurrent_identical_calls(env, monkeypatch):
    second = "明早检查数据备份。"

    def simultaneous(call):
        return tool_call(
            "send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]
        )(call) + tool_call(
            "send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]
        )(call)

    request = script(
        env,
        "给机器人发两条不同提醒",
        simultaneous,
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=second, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch) as (group, requests, _rt):
        await group.ask(request)
        assert [json.loads(r.content)["content"]["text"] for r in requests] == [TEXT, second]


async def test_unknown_then_changed_text_in_same_turn_never_posts_again(env, monkeypatch):
    request = script(
        env,
        "测试发送失败不能自动重试",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        tool_call(
            "send_webhook_message", bot_alias="ops_alert_bot", text=TEXT + "!", evidence_ids=[]
        ),
        cite(),
    )
    async with running(env, monkeypatch, response=httpx2.ReadTimeout("lost")) as (
        group,
        requests,
        _rt,
    ):
        await group.ask(request)
        assert len(requests) == 1
        assert "unknown" in message_text(group.outbox.sent[-1][1])


async def test_multiple_bots_route_to_distinct_configured_targets(env, monkeypatch):
    first = configured(env).webhook_bots[0]
    second = first.model_copy(
        update={
            "bot_id": "bigdata",
            "alias": "bigdata_bot",
            "description": "大数据群",
            "url_ref": "env:XW_TEST_SECOND_WEBHOOK_URL",
        }
    )
    monkeypatch.setenv("XW_TEST_SECOND_WEBHOOK_URL", URL + "-second")
    request = script(
        env,
        "分别通知运维群和大数据群",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        tool_call("send_webhook_message", bot_alias="bigdata_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch, bots=[first, second]) as (group, requests, _rt):
        await group.ask(request)
        assert [str(r.url) for r in requests] == [URL, URL + "-second"]


async def test_allowed_tool_does_not_allow_an_unapproved_source(env, monkeypatch):
    bot = configured(env).webhook_bots[0].model_copy(update={"receive_targets": frozenset()})
    query = script(
        env,
        "先读合法源",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        cite(),
    )
    request = script(
        env,
        "该机器人未获准此源数据",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch, bots=[bot]) as (group, requests, _rt):
        await group.ask(query)
        await group.ask(request)
        assert requests == []


async def test_signature_uses_official_timestamp_secret_empty_message_contract(env, monkeypatch):
    import base64
    import hmac

    secret = "synthetic-signing-secret"  # noqa: S105 - 公开的合成签名夹具
    monkeypatch.setenv("XW_TEST_SIGNING_SECRET", secret)
    bot = (
        configured(env)
        .webhook_bots[0]
        .model_copy(
            update={
                "signing_secret_ref": "env:XW_TEST_SIGNING_SECRET",
            }
        )
    )
    request = script(
        env,
        "发送签名消息",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch, bots=[bot]) as (group, requests, _rt):
        await group.ask(request)
        body = json.loads(requests[0].content)
        stamp = str(int(env.clock().timestamp()))
        expected = base64.b64encode(
            hmac.digest(f"{stamp}\n{secret}".encode(), b"", "sha256")
        ).decode()
        assert body["timestamp"] == stamp and body["sign"] == expected
        assert secret not in str(env.scripts.calls) + str(group.outbox.sent)


async def test_large_content_is_not_sent_and_can_be_explained(env, monkeypatch):
    request = script(
        env,
        "发一条超大文本",
        tool_call(
            "send_webhook_message", bot_alias="ops_alert_bot", text="大" * 7000, evidence_ids=[]
        ),
        cite("消息超过容量，未发送；请缩短内容。"),
    )
    async with running(env, monkeypatch) as (group, requests, _rt):
        await group.ask(request)
        assert requests == []
        assert "未发送" in message_text(group.outbox.sent[-1][1])


async def test_mention_expansion_is_counted_before_request_size_admission(env, monkeypatch):
    request = script(
        env,
        "中和后的消息也须满足容量上限",
        tool_call(
            "send_webhook_message", bot_alias="ops_alert_bot", text="<at>" * 3400, evidence_ids=[]
        ),
        cite("消息超过容量，未发送；请缩短内容。"),
    )
    async with running(env, monkeypatch) as (group, requests, _rt):
        await group.ask(request)
        assert requests == []
        assert "未发送" in message_text(group.outbox.sent[-1][1])
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0


async def test_attempt_persistence_failure_has_zero_http(env, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError

    request = script(
        env,
        "发送机会不能落库时不得发送",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
    )
    intercepted = []

    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO xiaowei_notification_attempt"):
            intercepted.append(statement)
            raise SQLAlchemyError("synthetic attempt store failure")

    async with running(env, monkeypatch) as (group, requests, rt):
        event.listen(rt.engine.sync_engine, "before_cursor_execute", fail_insert)
        try:
            await group.ask(request)
        finally:
            event.remove(rt.engine.sync_engine, "before_cursor_execute", fail_insert)
        assert intercepted and requests == []


async def test_unknown_receipt_survives_restart_without_http(env, monkeypatch):
    request = script(
        env,
        "响应丢失后重启不得补发",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=TEXT, evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch, response=httpx2.ReadTimeout("lost")) as (
        group,
        requests,
        _rt,
    ):
        await group.ask(request)
        assert len(requests) == 1
    async with running(env, monkeypatch) as (_group, requests, rt):
        assert requests == []
        async with rt.engine.connect() as conn:
            states = (
                await conn.execute(sql("SELECT state FROM xiaowei_notification_attempt"))
            ).all()
        assert states == [("unknown",)]


@pytest.mark.parametrize("channel", ["web", "feishu"])
async def test_notification_governance_rejects_forged_personal_scope(env, monkeypatch, channel):
    from xiaowei.governance import GovernedTools, ToolRejectedError
    from xiaowei.models import Budget, Identity, RunContext, ToolRequest

    async with running(env, monkeypatch) as (_group, requests, rt):
        governed = GovernedTools(rt.service.results.evidence)
        ctx = RunContext(
            identity=Identity(subject_id=A, session_id="forged", turn_id="forged", channel=channel),
            tool_scope=frozenset({TOOL}),
            target_scope=frozenset({"notifications"}),
            budget=Budget(max_turns=2, max_tool_calls=2, timeout_seconds=5, max_scope_checks=10),
        )

        async def forbidden(request):
            pytest.fail("unauthorized I/O boundary was reached")

        with pytest.raises(ToolRejectedError):
            await governed.invoke(
                ctx,
                ToolRequest(
                    tool_id=TOOL,
                    target_id="notifications",
                    call_id="forged",
                    tool_name="send_webhook_message",
                    arguments={"bot_alias": "ops_alert_bot", "text": TEXT, "evidence_ids": []},
                ),
                forbidden,
            )
        assert requests == []
