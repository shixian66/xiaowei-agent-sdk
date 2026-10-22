"""自建代理的明确协议；正式群入口/SDK/治理，外部 HTTP 为合成替身。"""

import json
import logging

import httpx2
import pytest
from pydantic import ValidationError
from sqlalchemy import text as sql
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.p1b.test_starrocks_adapter import Result, driver
from tests.sdk_core.test_app import cite, evidence_in, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_scheduled_agent import TASK, activate, agent_config
from tests.sdk_core.test_scheduled_tasks import reminder, task_config
from tests.sdk_core.test_webhook_notification import configured, running, script

from xiaowei import runtime
from xiaowei.webhook import resolve_bot

pytestmark = pytest.mark.loopback
PROXY = "https://alerts.example.test/send?groupId=ops%2Fbigdata&token=synthetic-proxy-token&x=1&x=2"


def proxy_config(env, config=None):
    data = (config or configured(env)).model_dump(mode="json")
    data["webhook_bots"][0].update(
        protocol="feishu_proxy",
        url_ref="env:XW_TEST_PROXY_URL",
        title='小维巡检："核心指标"',
        open_url="https://dashboard.example.test/d/node?var-job=node",
    )
    return runtime.ServeConfig.model_validate(data)


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, content=b"not-json"),
        httpx2.Response(204),
        httpx2.Response(202, json={"code": 123, "errcode": 456}),
    ],
)
async def test_proxy_formal_entry_preserves_query_json_and_truthful_http_receipt(
    env, monkeypatch, caplog, response
):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    content = 'CPU 观察值 42%\n说明："中文引号" <at user_id="all">所有人</at>'
    request = script(
        env,
        "向运维代理发消息",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text=content, evidence_ids=[]),
        cite(),
    )

    async def respond(_request):
        assert await env.scalar("SELECT state FROM xiaowei_notification_attempt") == "sending"
        return response

    async with running(env, monkeypatch, config=proxy_config(env), response=respond) as (
        group,
        requests,
        rt,
    ):
        await group.ask(request)
        assert len(requests) == 1
        sent = requests[0]
        assert str(sent.url) == PROXY
        assert sent.headers["content-type"] == "application/json; charset=utf-8"
        payload = json.loads(sent.content)
        assert set(payload) == {"title", "message", "openUrl"}
        assert payload["title"] == '小维巡检："核心指标"'
        assert payload["openUrl"] == "https://dashboard.example.test/d/node?var-job=node"
        assert payload["message"] == content.replace("<at", "＜at")
        assert "<at" not in sent.content.decode()
        assert all(isinstance(value, str) for value in payload.values())
        assert await env.scalar("SELECT state FROM xiaowei_notification_attempt") == "accepted"
        assert (
            await env.scalar("SELECT reason FROM xiaowei_notification_attempt")
            == "proxy_http_success"
        )
        reply = message_text(group.outbox.sent[-1][1])
        assert "代理" in reply and "群内投递未确认" in reply
        history = await env.scalar("SELECT string_agg(message_data, '') FROM agent_messages")
        async with rt.engine.connect() as conn:
            evidence = (
                await conn.execute(
                    sql("SELECT model_content,session_content,feishu_content FROM xiaowei_evidence")
                )
            ).all()
        combined = str(history) + str(evidence) + str(env.scripts.calls) + caplog.text + reply
        assert "synthetic-proxy-token" not in combined and PROXY not in combined
        assert "https://dashboard.example.test/d/node?var-job=node" not in str(env.scripts.calls)
        assert '小维巡检："核心指标"' not in str(env.scripts.calls)


@pytest.mark.parametrize(
    "response,status",
    [
        (httpx2.Response(403), "failed"),
        (httpx2.Response(302, headers={"location": "https://evil.example.test/"}), "failed"),
        (httpx2.Response(503), "unknown"),
        (httpx2.ReadTimeout("synthetic-proxy-token"), "unknown"),
        (httpx2.ConnectError("synthetic-proxy-token"), "failed"),
    ],
)
async def test_proxy_http_failure_or_timeout_never_retries(env, monkeypatch, response, status):
    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    request = script(
        env,
        "发代理测试",
        tool_call(
            "send_webhook_message",
            bot_alias="ops_alert_bot",
            text="普通文字\n第二行",
            evidence_ids=[],
        ),
        cite(),
    )
    async with running(env, monkeypatch, config=proxy_config(env), response=response) as (
        group,
        requests,
        _rt,
    ):
        await group.ask(request)
        assert len(requests) == 1
        assert await env.scalar("SELECT state FROM xiaowei_notification_attempt") == status
        assert status in message_text(group.outbox.sent[-1][1])


async def test_proxy_same_intent_deduplicates_after_mention_sanitization(env, monkeypatch):
    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    args = dict(bot_alias="ops_alert_bot", text="提醒 <at id=all></at>", evidence_ids=[])
    request = script(
        env,
        "发一条提醒",
        tool_call("send_webhook_message", **args),
        tool_call("send_webhook_message", **args),
        cite(),
    )
    async with running(env, monkeypatch, config=proxy_config(env)) as (group, requests, _rt):
        await group.ask(request)
        assert len(requests) == 1
        assert json.loads(requests[0].content)["message"] == "提醒 ＜at id=all></at>"
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 1


async def test_proxy_full_payload_limit_includes_static_fields(env, monkeypatch):
    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    cfg = proxy_config(env)
    data = cfg.model_dump(mode="json")
    data["webhook_bots"][0]["title"] = "标题" * 4000
    cfg = runtime.ServeConfig.model_validate(data)
    request = script(
        env,
        "发送短消息",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text="提醒", evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch, config=cfg) as (group, requests, _rt):
        await group.ask(request)
        assert requests == []
        assert "content_too_large" in message_text(group.outbox.sent[-1][1])


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd?groupId=ops",
        "https://alerts.example.test/send",
        "https://alerts.example.test/send?groupId=",
        "https://alerts.example.test/send?groupId=a&groupId=b",
        "https://user:password@alerts.example.test/send?groupId=a",
        "https://alerts.example.test/send?groupId=a#fragment",
    ],
)
def test_proxy_endpoint_rejects_ambiguous_destination_before_io(env, monkeypatch, url):
    monkeypatch.setenv("XW_TEST_PROXY_URL", url)
    bot = proxy_config(env).webhook_bots[0]
    with pytest.raises(ValueError):
        resolve_bot(bot)


@pytest.mark.parametrize(
    "patch", [{"title": None}, {"open_url": None}, {"signing_secret_ref": "env:UNUSED_SIGNING_KEY"}]
)
def test_proxy_requires_declared_strings_and_rejects_official_signing(env, patch):
    data = configured(env).model_dump(mode="json")
    data["webhook_bots"][0].update(
        {
            "protocol": "feishu_proxy",
            "title": "巡检",
            "open_url": "https://dashboard.example.test/",
            **patch,
        }
    )
    with pytest.raises(ValidationError):
        runtime.ServeConfig.model_validate(data)


@pytest.mark.parametrize(
    "open_url", ["javascript:alert(1)", "file:///etc/passwd", "", "ftp://files.test/x"]
)
def test_proxy_open_url_only_accepts_http_https(env, open_url):
    data = proxy_config(env).model_dump(mode="json")
    data["webhook_bots"][0]["open_url"] = open_url
    with pytest.raises(ValidationError):
        runtime.ServeConfig.model_validate(data)


@pytest.mark.parametrize("origin", ["facts", "model"])
async def test_proxy_evidence_and_model_text_neutralize_mentions(env, monkeypatch, origin):
    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    tags = '<at user_id="all">所有人</at> <AT id=all></AT>'
    env.drv = driver(Result(("region", "note"), [("east", tags)]))
    query = script(
        env,
        "取得事实",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, note FROM shop.sales"
        ),
        cite(),
    )

    def send(call):
        return tool_call(
            "send_webhook_message",
            bot_alias="ops_alert_bot",
            text=tags if origin == "model" else "请复核",
            evidence_ids=evidence_in(call)[:1],
        )(call)

    request = script(env, "转发事实", send, send, cite())
    async with running(env, monkeypatch, config=proxy_config(env)) as (group, requests, _):
        await group.ask(query)
        await group.ask(request)
        assert len(requests) == 1
        payload = json.loads(requests[0].content)
        assert "<at" not in requests[0].content.decode().lower()
        assert tags.replace("<at", "＜at").replace("<AT", "＜AT") in payload["message"]
        assert "工具结果（系统根据证据生成）" in payload["message"]
        if origin == "model":
            assert "模型说明（未经系统核实）" in payload["message"]
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 1


async def test_proxy_sender_recheck_prevents_claim_and_http(env, monkeypatch):
    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    request = script(
        env,
        "发送一条消息",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text="提醒", evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch, config=proxy_config(env)) as (group, requests, rt):

        async def sender(_):
            return False

        monkeypatch.setattr(rt.service._app.notifications, "_verify_sender", sender)
        await group.ask(request)
        assert not requests
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0
        assert "sender_not_authorized" in message_text(group.outbox.sent[-1][1])


async def test_proxy_unapproved_receiver_has_zero_http(env, monkeypatch):
    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    data = proxy_config(env).model_dump(mode="json")
    data["webhook_bots"][0]["receive_targets"] = []
    config = runtime.ServeConfig.model_validate(data)
    query = script(
        env,
        "先读合法源",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    )
    request = script(
        env,
        "不能外发此源",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text="提醒", evidence_ids=[]),
        cite(),
    )
    async with running(env, monkeypatch, config=config) as (group, requests, _):
        await group.ask(query)
        await group.ask(request)
        assert not requests
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0
        assert "recipient_not_authorized" in message_text(group.outbox.sent[-1][1])


async def test_proxy_unknown_gate_requires_delivered_warning_before_explicit_resend(
    env, monkeypatch
):
    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    from tests.sdk_core.test_group_identity import CHAT, A, B

    from xiaowei.feishu import attributed

    config = proxy_config(env)
    config = config.model_copy(
        update={
            "access": config.access.model_copy(
                update={
                    "grants": {
                        **config.access.grants,
                        B: frozenset({"notifications/send_webhook_message"}),
                    }
                }
            )
        }
    )
    first = script(
        env,
        "A发消息",
        tool_call("send_webhook_message", bot_alias="ops_alert_bot", text="提醒", evidence_ids=[]),
        cite(),
    )
    for message in ("B要求发消息", "风险已提示仍要重发"):
        env.scripts.add(
            attributed(B, message),
            tool_call(
                "send_webhook_message", bot_alias="ops_alert_bot", text="提醒", evidence_ids=[]
            ),
            cite(),
        )

    async def members(chat, subject):
        return chat == CHAT and subject in {A, B}

    async with running(
        env, monkeypatch, config=config, member_check=members, response=httpx2.ReadTimeout("lost")
    ) as (group, requests, _):
        group.outbox.outcomes.append("failed")
        await group.ask(first)
        await group.ask("B要求发消息", sender=B)
        assert len(requests) == 1
        assert "重发可能重复" in message_text(group.outbox.sent[-1][1])
        assert "本轮未发送" in message_text(group.outbox.sent[-1][1])
        await group.ask("风险已提示仍要重发", sender=B)
        assert len(requests) == 2
        assert (
            await env.scalar(
                "SELECT count(*) FROM xiaowei_notification_attempt WHERE state='unknown'"
            )
            == 2
        )


@pytest.mark.parametrize(
    "response,state,reason",
    [
        (httpx2.Response(204), "accepted", "proxy_http_success"),
        (httpx2.ReadTimeout("lost"), "unknown", "response_unknown"),
    ],
)
async def test_proxy_fixed_reminder_uses_same_once_only_path(
    env, monkeypatch, response, state, reason
):
    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    async with running(
        env, monkeypatch, config=proxy_config(env, task_config(env)), response=response
    ) as (group, requests, rt):
        await reminder(env, group, content="汇报提醒\n<at id=all></at>")
        calls = sum(len(v) for v in env.scripts.calls.values())
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert json.loads(requests[0].content)["message"] == "汇报提醒\n＜at id=all></at>"
        assert "<at" not in requests[0].content.decode().lower()
        assert await env.scalar("SELECT state FROM xiaowei_notification_attempt") == state
        assert await env.scalar("SELECT reason FROM xiaowei_notification_attempt") == reason
        assert sum(len(v) for v in env.scripts.calls.values()) == calls
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "completed"


@pytest.mark.parametrize("oversized", [False, True])
async def test_proxy_scheduled_agent_delivers_facts_or_one_capacity_notice(
    env, monkeypatch, oversized
):
    from xiaowei.models import AUDIENCES

    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    data = proxy_config(env, agent_config(env)).model_dump(mode="json")
    if oversized:
        rows = [(f"PRIVATE_RESULT_{i}_" + "x" * 35, i) for i in range(1000)]
        env.drv = driver(Result(("region", "total"), rows))
        data["targets"][0]["starrocks"]["policy"]["max_rows"] = 1000
        data["targets"][0]["starrocks"]["schema_limits"]["max_age_seconds"] = 7200
        data["targets"][0]["starrocks"]["max_result_bytes"] = 300_000
        data["targets"][0]["starrocks"]["max_value_bytes"] = 500
        data["projection_bytes"] = dict.fromkeys(AUDIENCES, 400_000)
        data["budget"]["max_scope_checks"] = 5000
        data["model"]["max_request_bytes"] = 2_000_000
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query",
            cluster=SR.target_id,
            sql="SELECT region,total FROM shop.sales LIMIT 1000",
        ),
        cite(),
    )
    async with running(env, monkeypatch, config=runtime.ServeConfig.model_validate(data)) as (
        group,
        requests,
        rt,
    ):
        await activate(env, group, interval=3600)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        payload = json.loads(requests[0].content)
        assert set(payload) == {"title", "message", "openUrl"}
        assert "<at" not in requests[0].content.decode().lower()
        assert len(requests[0].content) <= 20000
        if oversized:
            assert "本次结果超过通知容量，未发送正文" in payload["message"]
            assert "PRIVATE_RESULT" not in payload["message"]
            assert (
                await env.scalar("SELECT reason FROM xiaowei_scheduled_run") == "content_too_large"
            )
        else:
            assert "east" in payload["message"] and "100" in payload["message"]
            assert "west" in payload["message"] and "50" in payload["message"]
            assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "completed"
        assert (
            await env.scalar("SELECT reason FROM xiaowei_notification_attempt")
            == "proxy_http_success"
        )


async def test_proxy_static_fields_cannot_make_capacity_notice_exceed_limit(env, monkeypatch):
    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    data = proxy_config(env, agent_config(env)).model_dump(mode="json")
    data["webhook_bots"][0]["title"] = "标题" * 4000
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    )
    async with running(env, monkeypatch, config=runtime.ServeConfig.model_validate(data)) as (
        group,
        requests,
        rt,
    ):
        await activate(env, group)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert not requests
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0
        assert await env.scalar("SELECT reason FROM xiaowei_scheduled_run") == "content_too_large"


async def test_official_and_proxy_bots_keep_distinct_wire_contracts(env, monkeypatch):
    from tests.sdk_core.test_webhook_notification import URL

    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    data = proxy_config(env).model_dump(mode="json")
    official = configured(env).webhook_bots[0].model_dump(mode="json")
    official.update(bot_id="official", alias="official_bot")
    data["webhook_bots"].append(official)
    request = script(
        env,
        "给两个机器人分别发送一条提醒",
        tool_call(
            "send_webhook_message", bot_alias="ops_alert_bot", text="代理提醒", evidence_ids=[]
        ),
        tool_call(
            "send_webhook_message", bot_alias="official_bot", text="官方提醒", evidence_ids=[]
        ),
        cite(),
    )
    async with running(env, monkeypatch, config=runtime.ServeConfig.model_validate(data)) as (
        group,
        requests,
        _,
    ):
        await group.ask(request)
        assert len(requests) == 2
        assert str(requests[0].url) == PROXY
        assert json.loads(requests[0].content)["message"] == "代理提醒"
        assert str(requests[1].url) == URL
        assert json.loads(requests[1].content) == {
            "msg_type": "text",
            "content": {"text": "官方提醒"},
        }
        assert (
            await env.scalar("SELECT reason FROM xiaowei_notification_attempt WHERE bot_id='ops'")
            == "proxy_http_success"
        )
        assert (
            await env.scalar(
                "SELECT reason FROM xiaowei_notification_attempt WHERE bot_id='official'"
            )
            == "api_accepted"
        )


@pytest.mark.parametrize("kind", ["reminder", "agent_task"])
async def test_proxy_task_status_explains_persisted_http_receipt_even_after_config_change(
    env, monkeypatch, kind
):
    from tests.sdk_core.test_group_identity import A
    from tests.sdk_core.test_starrocks_tools import tool_outputs

    from xiaowei.feishu import attributed

    monkeypatch.setenv("XW_TEST_PROXY_URL", PROXY)
    config = proxy_config(env, agent_config(env) if kind == "agent_task" else task_config(env))
    if kind == "agent_task":
        env.scripts.add(
            TASK,
            tool_call(
                "run_readonly_query",
                cluster=SR.target_id,
                sql="SELECT region,total FROM shop.sales",
            ),
            cite(),
        )

    async def check(group, message, job_id, message_id):
        script(
            env, message, tool_call("scheduled__get_task", job_id=job_id), cite("任务状态如下。")
        )
        await group.ask(message, message_id=message_id)
        outputs = tool_outputs(env.scripts.calls[attributed(A, message)][-1])
        assert "群内投递未确认" in str(outputs)
        reply = message_text(group.outbox.sent[-1][1])
        assert "代理返回 HTTP 2xx" in reply and "群内投递未确认" in reply

    async with running(env, monkeypatch, config=config) as (group, requests, rt):
        if kind == "agent_task":
            job_id = await activate(env, group)
        else:
            job_id = await reminder(env, group)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        await check(group, "查询刚才任务的发送状态", job_id, "om_proxy_current_status")
    # 管理员切换协议并更新目标版本后，旧回执仍必须按保存的实际原因解释。
    data = config.model_dump(mode="json")
    data["webhook_bots"][0].update(
        protocol="feishu",
        title=None,
        open_url=None,
        url_ref="env:XW_TEST_WEBHOOK_URL",
        target_version="v2",
    )
    async with running(env, monkeypatch, config=runtime.ServeConfig.model_validate(data)) as (
        group,
        requests,
        _,
    ):
        await group.ask("/新建", message_id="om_proxy_new_session")
        await check(group, "查询旧任务的发送状态", job_id, "om_proxy_old_status")
        assert not requests
        assert (
            await env.scalar("SELECT reason FROM xiaowei_notification_attempt")
            == "proxy_http_success"
        )
