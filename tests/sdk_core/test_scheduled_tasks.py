"""一次性提醒：真 SDK / PostgreSQL；模型与外部发送使用离线替身。"""

import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import text
from tests.sdk_core.test_app import cite, evidence_in, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_actions import Actions, request_execution
from tests.sdk_core.test_group_actions import actions as actions
from tests.sdk_core.test_group_gateway import running
from tests.sdk_core.test_group_identity import A, B
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_webhook_notification import configured
from tests.sdk_core.test_webhook_notification import running as webhook_running

from xiaowei import runtime
from xiaowei.feishu import attributed
from xiaowei.models import ToolObservation

pytestmark = pytest.mark.loopback


def task_config(env, *, enabled=True, approve=True, admins=(), **options):
    data = configured(env).model_dump(mode="json")
    tools = {
        "scheduled/propose_reminder",
        "scheduled/get_action_status",
        "scheduled/get_task",
        "scheduled/control_task",
    }
    data["scheduled_tasks"] = {"enabled": enabled, "poll_seconds": 60, **options}
    data["feishu"]["group"]["tools"].extend(sorted(tools))
    data["data_policy"]["model_tools"].extend(sorted(tools | {"scheduled/activate_reminder"}))
    if approve:
        data["access"]["grants"][A].append("scheduled/activate_reminder")
    for subject in admins:
        data["access"]["grants"][subject] = ["scheduled/control_task"]
    return runtime.ServeConfig.model_validate(data)


def documented_task_config(env):
    """读取公开示例的接收策略；保留合成凭据与源，不自动补任何许可。"""
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    section = readme.split('<a id="webhook-notifications"></a>', 1)[1].split("**开发验证**", 1)[0]
    config = task_config(env)
    data = config.model_dump(mode="json")
    for block in re.findall(r"```json\n(.*?)\n```", section, re.S):
        values = json.loads("{" + block + "}")
        receiver = values.get("webhook_bots", [{}])[0] if "webhook_bots" in values else values
        for key in ("receive_tools", "receive_targets"):
            if key in receiver:
                data["webhook_bots"][0][key] = receiver[key]
    data["webhook_bots"][0]["receive_targets"] = [
        config.targets[0].target_id if target == "fat" else target
        for target in data["webhook_bots"][0]["receive_targets"]
    ]
    return runtime.ServeConfig.model_validate(data)


async def reminder(env, group, *, seconds=60, confirm=True, content="汇报提醒", select=False):
    at = env.clock() + timedelta(seconds=seconds)
    message = "设置一次提醒"

    def propose(call):
        return tool_call(
            "scheduled__propose_reminder",
            bot_alias="ops_alert_bot",
            text=content,
            evidence_ids=evidence_in(call) if select else [],
            at=at.isoformat(),
        )(call)

    env.scripts.add(attributed(A, message), propose, request_execution, cite("已记录：" + content))
    await group.ask(message)
    job_id = await env.scalar("SELECT job_id FROM xiaowei_scheduled_job")
    if confirm:
        action = await env.scalar("SELECT action_id FROM xiaowei_action")
        await group.ask(f"/批准 {action}")
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"
    return job_id


async def history(env, group):
    from tests.p1b.test_starrocks_adapter import TARGET as SR

    message = "先查销售"
    env.scripts.add(
        attributed(A, message),
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        cite(),
    )
    await group.ask(message)
    return await env.scalar("SELECT evidence_id FROM xiaowei_evidence")


async def test_native_action_can_activate_local_definition_once_independent_of_session(
    actions: Actions,
) -> None:
    """B 的接入实验：只用公开 ActionBinding，批准只负责当次激活。

    这里的临时表是隔离测试对象，不是正式 scheduled_job schema；生产实现另测。
    """
    env = actions.env
    async with env.engine.begin() as conn:
        await conn.execute(text("CREATE TABLE fixture_job (id text PRIMARY KEY, enabled boolean)"))
        await conn.execute(text("INSERT INTO fixture_job VALUES ('reminder-v1', false)"))

    async def activate(request):
        async with env.engine.begin() as conn:
            result = await conn.execute(
                text("UPDATE fixture_job SET enabled=true WHERE id=:id AND NOT enabled"),
                {"id": request.arguments["value"]},
            )
            assert result.rowcount == 1
        return ToolObservation(
            payload={"value": request.arguments["value"]},
            captured_at=env.clock(),
            truncated=False,
        )

    actions.rebind(execute=activate)
    env.scripts.add(
        attributed(A, "明天上午10点提醒汇报"),
        tool_call("am-synthetic__propose_change", value="reminder-v1"),
        request_execution,
        cite(),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("明天上午10点提醒汇报", message_id="om_reminder")
        assert await env.rows("SELECT enabled FROM fixture_job") == [(False,)]
        action_id = await actions.one()
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_confirm")
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_repeat")
    assert await env.rows("SELECT enabled FROM fixture_job") == [(True,)]
    assert await env.rows("SELECT state FROM xiaowei_action") == [("succeeded",)]
    env.clock.advance(2 * 86400)
    # 后续任务授权是自己的持久化记录，不以原 Action 或 Session 仍有效为前提。
    assert await env.rows("SELECT enabled FROM fixture_job") == [(True,)]


async def test_u5_formal_entry_confirms_then_sends_once_without_model_at_due_time(
    env, monkeypatch
) -> None:
    config = task_config(env)
    at = (env.clock() + timedelta(days=1)).replace(hour=2, minute=0, second=0, microsecond=0)
    message = "明天上午10点把汇报提醒发到运维告警群"
    env.scripts.add(
        attributed(A, message),
        tool_call(
            "scheduled__propose_reminder",
            bot_alias="ops_alert_bot",
            text="上午10点需要做汇报。",
            evidence_ids=[],
            at=at.isoformat(),
        ),
        request_execution,
        cite(),
    )
    async with webhook_running(env, monkeypatch, config=config) as (group, requests, rt):
        await group.ask(message)
        assert requests == []
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "draft"
        material = message_text(group.outbox.sent[-1][1])
        assert "Asia/Shanghai" in material and "1小时" in material
        assert "上午10点需要做汇报。" in material
        assert "批准后不会自动通知" not in material
        assert "确认后按计划到期发送一次" in material
        action_id = await env.scalar("SELECT action_id FROM xiaowei_action")
        await group.ask(f"/批准 {action_id}", sender=A)
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"
        assert await env.scalar("SELECT approval_id FROM xiaowei_scheduled_job") == action_id
        assert await env.scalar("SELECT approver_id FROM xiaowei_scheduled_job") == A
        assert requests == []
        calls = len(env.scripts.calls)
        env.clock.advance((at - env.clock()).total_seconds())
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert json.loads(requests[0].content)["content"]["text"] == "上午10点需要做汇报。"
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "completed"
        assert len(env.scripts.calls) == calls


async def test_history_is_frozen_even_without_explicit_fact_reference(env, monkeypatch):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        evidence = await history(env, group)
        await reminder(env, group, confirm=False)
        async with rt.engine.connect() as conn:
            ids, expiry = (
                await conn.execute(
                    text(
                        "SELECT definition_evidence_ids,content_expires_at "
                        "FROM xiaowei_scheduled_job"
                    )
                )
            ).one()
            original = await conn.scalar(
                text("SELECT expires_at FROM xiaowei_evidence WHERE evidence_id=:id"),
                {"id": evidence},
            )
        assert json.loads(ids) == [evidence]
        assert expiry <= original
        assert "本任务将于" in message_text(group.outbox.sent[-1][1])
        assert "/新建" in message_text(group.outbox.sent[-1][1])
        assert requests == []


async def test_reminder_beyond_business_data_expiry_is_not_created(env, monkeypatch):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, _rt):
        await history(env, group)
        assert await reminder(env, group, seconds=10 * 86400, confirm=False) is None
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
        assert requests == []


async def test_task_derived_storage_cannot_extend_old_data_ttl(env, monkeypatch):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        evidence = await history(env, group)
        async with rt.engine.connect() as conn:
            expiry = await conn.scalar(
                text("SELECT expires_at FROM xiaowei_evidence WHERE evidence_id=:id"),
                {"id": evidence},
            )
        env.clock.advance(3500)
        await reminder(env, group, seconds=30, confirm=False, content="旧结果的说明")
        async with rt.engine.connect() as conn:
            for table in ("xiaowei_action", "xiaowei_action_run", "xiaowei_session"):
                values = (
                    (await conn.execute(text(f"SELECT expires_at FROM {table}"))).scalars().all()  # noqa: S608 - 固定表名
                )
                assert values and all(value <= expiry for value in values), table
            task_expiry = await conn.scalar(
                text("SELECT expires_at FROM xiaowei_request ORDER BY created_at DESC LIMIT 1")
            )
            assert task_expiry <= expiry
        assert requests == []


@pytest.mark.parametrize("offset,state,posts", [(120, "completed", 1), (3700, "missed", 0)])
async def test_late_reminder_only_catches_up_inside_window(env, monkeypatch, offset, state, posts):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        await reminder(env, group)
        env.clock.advance(offset)
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert len(requests) == posts
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == state


@pytest.mark.parametrize("operation,expected", [("pause", "paused"), ("cancel", "cancelled")])
async def test_creator_can_directly_stop_without_action_approval(
    env, monkeypatch, operation, expected
):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        job_id = await reminder(env, group)
        message = "停掉这个提醒"
        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__control_task", job_id=job_id, operation=operation),
            cite(),
        )
        await group.ask(message)
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == expected
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 1
        env.clock.advance(100)
        await rt.scheduled_tasks.tick()
        assert requests == []


@pytest.mark.parametrize(
    "response,status",
    [
        (httpx2.ReadTimeout("synthetic"), "unknown"),
        (httpx2.Response(200, json={"code": 100}), "failed"),
    ],
)
async def test_fixed_send_failure_never_repeats_post(env, monkeypatch, response, status):
    async with webhook_running(env, monkeypatch, config=task_config(env), response=response) as (
        group,
        requests,
        rt,
    ):
        await reminder(env, group)
        env.clock.advance(100)
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.recover()
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert await env.scalar("SELECT state FROM xiaowei_notification_attempt") == status


@pytest.mark.parametrize("failure", [False, TimeoutError("synthetic membership")])
async def test_temporary_membership_failure_skips_then_automatically_rechecks(
    env, monkeypatch, failure
):
    member = True

    async def members(_chat, subject):
        if isinstance(member, Exception):
            raise member
        return member and subject == A

    async with webhook_running(env, monkeypatch, config=task_config(env), member_check=members) as (
        group,
        requests,
        rt,
    ):
        await reminder(env, group)
        member = failure
        env.clock.advance(100)
        await rt.scheduled_tasks.tick()
        assert requests == []
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"
        assert await env.scalar("SELECT unverified_count FROM xiaowei_scheduled_job") == 1
        member = True
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert await env.scalar("SELECT unverified_count FROM xiaowei_scheduled_job") == 0


@pytest.mark.parametrize(
    "actor,admin,member,expected",
    [(B, False, True, "active"), (B, True, True, "cancelled"), (B, True, False, "active")],
)
async def test_only_current_member_creator_or_personal_admin_can_stop(
    env, monkeypatch, actor, admin, member, expected
):
    async def members(_chat, subject):
        return subject == A or (subject == B and member)

    async with webhook_running(
        env, monkeypatch, config=task_config(env, admins=[B] if admin else []), member_check=members
    ) as (group, requests, _rt):
        job = await reminder(env, group)
        message = "取消这个提醒"
        env.scripts.add(
            attributed(actor, message),
            tool_call("scheduled__control_task", job_id=job, operation="cancel"),
            cite(),
        )
        await group.ask(message, sender=actor)
        if member:
            assert len(env.scripts.calls[attributed(actor, message)]) == 2
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == expected
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 1
        assert requests == []


@pytest.mark.parametrize("operation", ["resume", "create", "restart_server"])
async def test_control_effect_cannot_expand_authorization(env, monkeypatch, operation):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, _rt):
        job = await reminder(env, group)
        message = "非法控制操作"
        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__control_task", job_id=job, operation=operation),
            cite(),
        )
        await group.ask(message)
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"
        assert requests == []


async def test_disabled_scheduler_keeps_status_and_control_but_never_claims(env, monkeypatch):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, _rt):
        job = await reminder(env, group)
        assert requests == []
    env.clock.advance(100)
    async with webhook_running(env, monkeypatch, config=task_config(env, enabled=False)) as (
        group,
        requests,
        rt,
    ):
        await rt.scheduled_tasks.tick()
        assert requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0
        message = "暂停已有提醒"
        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__control_task", job_id=job, operation="pause"),
            cite(),
        )
        await group.ask(message, message_id="om_stop_after_restart")
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "paused"


async def test_restart_catches_up_only_an_unstarted_occurrence(env, monkeypatch):
    config = task_config(env)
    async with webhook_running(env, monkeypatch, config=config) as (group, requests, _rt):
        await reminder(env, group)
        assert requests == []
    env.clock.advance(100)
    async with webhook_running(env, monkeypatch, config=config) as (_group, requests, rt):
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
    async with webhook_running(env, monkeypatch, config=config) as (_group, requests, rt):
        await rt.scheduled_tasks.tick()
        assert requests == []
        assert await env.scalar("SELECT state FROM xiaowei_notification_attempt") == "accepted"


@pytest.mark.parametrize("target_changed", [False, True])
async def test_same_target_secret_rotation_preserves_job_but_target_change_disables(
    env, monkeypatch, target_changed
):
    config = task_config(env)
    async with webhook_running(env, monkeypatch, config=config) as (group, requests, _rt):
        await reminder(env, group)
        assert requests == []
    data = config.model_dump(mode="json")
    data["webhook_bots"][0]["url_ref"] = "env:XW_TEST_ROTATED_WEBHOOK"
    if target_changed:
        data["webhook_bots"][0]["target_version"] = "new-receiver"
    monkeypatch.setenv(
        "XW_TEST_ROTATED_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/rotated-synthetic"
    )
    env.clock.advance(100)
    async with webhook_running(
        env, monkeypatch, config=runtime.ServeConfig.model_validate(data)
    ) as (_group, requests, rt):
        await rt.scheduled_tasks.tick()
        assert len(requests) == (0 if target_changed else 1)
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == (
            "disabled" if target_changed else "completed"
        )


async def test_confirmed_job_stops_after_creator_notification_grant_revoked(env, monkeypatch):
    config = task_config(env)
    async with webhook_running(env, monkeypatch, config=config) as (group, requests, _rt):
        await reminder(env, group)
    data = config.model_dump(mode="json")
    data["access"]["grants"][A].remove("notifications/send_webhook_message")
    env.clock.advance(100)
    async with webhook_running(
        env, monkeypatch, config=runtime.ServeConfig.model_validate(data)
    ) as (_group, requests, rt):
        await rt.scheduled_tasks.tick()
        assert requests == []
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "disabled"


async def test_parallel_due_checks_share_single_occurrence(env, monkeypatch):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        await reminder(env, group)
        env.clock.advance(100)
        await asyncio.gather(rt.scheduled_tasks.tick(), rt.scheduled_tasks.tick())
        assert len(requests) == 1


async def test_no_evidence_reminder_survives_expired_source_session(env, monkeypatch):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        await reminder(env, group, seconds=86400)
        await group.ask("/新建")
        env.clock.advance(86400)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1


async def test_cleanup_removes_expired_task_body_and_current_session_copies(env, monkeypatch):
    from xiaowei.session import cleanup_expired

    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        await history(env, group)
        env.clock.advance(3500)
        await reminder(env, group, seconds=30, content="派生结果提醒")
        assert await env.scalar("SELECT count(*) FROM agent_messages") > 0
        assert (
            await env.scalar(
                "SELECT count(*) FROM xiaowei_request WHERE answer LIKE '%派生结果提醒%'"
            )
            > 0
        )
        env.clock.advance(101)
        await cleanup_expired(rt.engine, now=env.clock(), batch_size=100)
        assert await env.scalar("SELECT text FROM xiaowei_scheduled_job") is None
        assert await env.scalar("SELECT count(*) FROM agent_messages") == 0
        assert (
            await env.scalar(
                "SELECT count(*) FROM xiaowei_request WHERE answer LIKE '%派生结果提醒%'"
            )
            == 0
        )
        assert await env.scalar("SELECT state FROM xiaowei_session") == "closed"
        assert requests == []


async def test_busy_does_not_start_or_consume_occurrence(env, monkeypatch):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        await reminder(env, group)
        env.clock.advance(100)
        app = rt.service._app
        from contextlib import ExitStack

        with ExitStack() as slots:
            for _ in range(app.max_concurrent_turns):
                assert slots.enter_context(app.background_slot())
            await rt.scheduled_tasks.tick()
            assert requests == []
            assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0
            assert await env.scalar("SELECT reason FROM xiaowei_scheduled_job") == "busy"
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1


async def test_pause_before_atomic_claim_prevents_http(env, monkeypatch):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        job = await reminder(env, group)
        entered, release = asyncio.Event(), asyncio.Event()
        claim = rt.scheduled_tasks._claim

        async def wait_before_claim(*args):
            entered.set()
            await release.wait()
            return await claim(*args)

        monkeypatch.setattr(rt.scheduled_tasks, "_claim", wait_before_claim)
        env.clock.advance(100)
        due = asyncio.create_task(rt.scheduled_tasks.tick())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            message = "暂停这个提醒"
            env.scripts.add(
                attributed(A, message),
                tool_call("scheduled__control_task", job_id=job, operation="pause"),
                cite(),
            )
            await group.ask(message)
        finally:
            release.set()
            await due
        assert requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "paused"


async def test_fixed_text_mentions_and_digest_use_the_same_final_body(env, monkeypatch):
    import hashlib

    content = '第一行\r\n<at user_id="all">所有人</at>\n第二行'
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        await reminder(env, group, content=content)
        env.clock.advance(100)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        sent = json.loads(requests[0].content)["content"]["text"]
        assert sent == content.replace("\r\n", "\n").replace("<at", "＜at")
        assert (
            await env.scalar("SELECT content_digest FROM xiaowei_notification_attempt")
            == hashlib.sha256(sent.encode()).hexdigest()
        )


@pytest.mark.parametrize("source_allowed", [False, True])
async def test_task_material_forwarding_keeps_transitive_recipient_scope(
    env, monkeypatch, source_allowed
):
    data = task_config(env).model_dump(mode="json")
    bot = dict(data["webhook_bots"][0])
    bot.update(
        bot_id="other",
        alias="other_bot",
        receive_tools=["scheduled/get_task"],
        receive_targets=["scheduled"],
    )
    if source_allowed:
        bot["receive_tools"].extend(data["webhook_bots"][0]["receive_tools"])
        bot["receive_targets"].extend(data["webhook_bots"][0]["receive_targets"])
    data["webhook_bots"].append(bot)
    async with webhook_running(
        env, monkeypatch, config=runtime.ServeConfig.model_validate(data)
    ) as (group, requests, _rt):
        await history(env, group)
        job = await reminder(env, group, seconds=60, select=True)
        await group.ask("/新建")
        message = "查看任务材料"
        env.scripts.add(
            attributed(A, message), tool_call("scheduled__get_task", job_id=job), cite()
        )
        await group.ask(message)
        message = "把任务材料发到 other_bot"

        def forward(call):
            return tool_call(
                "send_webhook_message",
                bot_alias="other_bot",
                text="材料说明",
                evidence_ids=evidence_in(call),
            )(call)

        env.scripts.add(attributed(A, message), forward, cite())
        await group.ask(message)
        assert len(requests) == (1 if source_allowed else 0)


async def test_lost_lock_at_fixed_claim_has_zero_post_and_attempt(env, monkeypatch):
    from xiaowei.governance import ToolExecutionError

    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        await reminder(env, group)
        claim = rt.scheduled_tasks._claim

        async def lose_before_claim(*args):
            await rt.lock.connection.execute(text("SELECT pg_advisory_unlock_all()"))
            await rt.lock.connection.commit()
            return await claim(*args)

        monkeypatch.setattr(rt.scheduled_tasks, "_claim", lose_before_claim)
        env.clock.advance(100)
        with pytest.raises(ToolExecutionError):
            await rt.scheduled_tasks.tick()
        assert not rt.readiness.ok
        assert requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0


@pytest.mark.parametrize("revoke", [False, True])
async def test_formal_resend_rechecks_task_definition_without_opening_model_or_webhook(
    env, monkeypatch, revoke
):
    from tests.sdk_core.test_group_gateway import GroupChannel

    from xiaowei.channel import ResultUnavailableError

    config = task_config(env)
    async with webhook_running(env, monkeypatch, config=config) as (group, requests, _rt):
        await history(env, group)
        job = await reminder(env, group, select=True)
        await group.ask("/新建")
        message = "查看这个提醒的内容"
        env.scripts.add(
            attributed(A, message), tool_call("scheduled__get_task", job_id=job), cite()
        )
        group.outbox.outcomes.append("failed")
        await group.ask(message, message_id="om_task_material")
        assert (
            await env.scalar(
                "SELECT delivery FROM xiaowei_request WHERE reply_message_id='om_task_material'"
            )
            == "failed"
        )
        assert requests == []
    data = config.model_dump(mode="json")
    if revoke:
        data["feishu"]["group"]["tools"].remove("local/run_readonly_query")
    changed = runtime.ServeConfig.model_validate(data)
    channel = GroupChannel(members=[A])
    calls = sum(map(len, env.scripts.calls.values()))
    # 维护入口无需解析 Webhook 凭据，也不能启动模型。
    monkeypatch.delenv("XW_TEST_WEBHOOK_URL")
    args = dict(
        subject_id=A,
        message_id="om_task_material",
        group=True,
        clock=env.clock,
        feishu_channel=channel,
        starrocks_connect=env.connect(changed),
    )
    if revoke:
        with pytest.raises(ResultUnavailableError):
            await runtime.resend(changed, **args)
        assert channel.sends == []
    else:
        assert await runtime.resend(changed, **args) == "sent"
        assert len(channel.sends) == 1
    assert sum(map(len, env.scripts.calls.values())) == calls


async def test_activation_receipt_does_not_copy_business_body(env, monkeypatch):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (
        group,
        _requests,
        _rt,
    ):
        await history(env, group)
        await reminder(env, group, select=True)
        result = json.loads(await env.scalar("SELECT result FROM xiaowei_action"))
        assert result["text"] is None
        assert result["state"] == "active"


async def test_started_occurrence_recovers_as_unknown_and_blocks_unwarned_immediate_send(
    env, monkeypatch
):
    from xiaowei.webhook import WebhookNotifications

    config = task_config(env)
    entered = asyncio.Event()

    async def interrupted_post(*_args):
        entered.set()
        await asyncio.Event().wait()

    async def crashed_before_settlement(*_args):
        pass  # 故障注入：模拟进程消失，取消清理没有执行机会。

    async with webhook_running(env, monkeypatch, config=config) as (group, requests, rt):
        await reminder(env, group)
        with monkeypatch.context() as patch:
            patch.setattr(WebhookNotifications, "_post", interrupted_post)
            patch.setattr(
                rt.scheduled_tasks, "_interrupt", crashed_before_settlement, raising=False
            )
            env.clock.advance(100)
            due = asyncio.create_task(rt.scheduled_tasks.tick())
            await asyncio.wait_for(entered.wait(), 5)
            due.cancel()
            with pytest.raises(asyncio.CancelledError):
                await due
        assert requests == []
        assert await env.scalar("SELECT state FROM xiaowei_notification_attempt") == "sending"
    async with webhook_running(env, monkeypatch, config=config) as (group, requests, rt):
        await rt.scheduled_tasks.tick()
        assert requests == []
        assert await env.scalar("SELECT state FROM xiaowei_notification_attempt") == "unknown"
        assert (
            await env.scalar("SELECT reason FROM xiaowei_notification_attempt")
            == "response_unknown"
        )
        assert await env.scalar("SELECT reason FROM xiaowei_scheduled_job") == "response_unknown"
        await group.ask("/新建", message_id="om_after_crash_new")
        for number in (1, 2):
            message = "再发一次汇报提醒" + str(number)
            env.scripts.add(
                attributed(A, message),
                tool_call(
                    "send_webhook_message",
                    bot_alias="ops_alert_bot",
                    text="汇报提醒",
                    evidence_ids=[],
                ),
                cite(),
            )
            await group.ask(message, message_id=f"om_after_crash_send_{number}")
            assert len(requests) == number - 1  # 先送达重复风险提示，用户下一轮明确重发才准入。


@pytest.mark.parametrize("alias", ["missing", "http://127.0.0.1/private"])
async def test_unconfigured_scheduled_recipient_has_zero_job_and_http(env, monkeypatch, alias):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, _rt):
        message = "设置未经授权的提醒"
        env.scripts.add(
            attributed(A, message),
            tool_call(
                "scheduled__propose_reminder",
                bot_alias=alias,
                text="提醒",
                evidence_ids=[],
                at=(env.clock() + timedelta(days=1)).isoformat(),
            ),
            cite(),
        )
        await group.ask(message)
        assert requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job") == 0


async def test_missing_personal_confirmation_grant_cannot_activate(env, monkeypatch):
    async with webhook_running(env, monkeypatch, config=task_config(env, approve=False)) as (
        group,
        requests,
        _rt,
    ):
        await reminder(env, group, confirm=False)
        action = await env.scalar("SELECT action_id FROM xiaowei_action")
        await group.ask(f"/批准 {action}")
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "draft"
        assert requests == []


async def test_capacity_preview_uses_the_normalized_final_http_text(env, monkeypatch):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (_group, requests, rt):
        content = "a" * 19800 + "\r\n" * 40
        assert rt.service._app.notifications.fixed_message_fits("ops_alert_bot", content)
        assert requests == []


async def test_no_notification_grant_hides_creation_but_keeps_stop_and_status(env, monkeypatch):
    data = task_config(env).model_dump(mode="json")
    data["access"]["grants"][A].remove("notifications/send_webhook_message")
    config = runtime.ServeConfig.model_validate(data)
    message = "设置提醒"

    def probe(call):
        assert "scheduled__propose_reminder" not in call.tools
        assert "scheduled__request_action_execution" not in call.tools
        assert "scheduled__control_task" in call.tools
        assert "scheduled__get_task" in call.tools
        return cite("缺少独立通知许可，未创建任务。")(call)

    env.scripts.add(attributed(A, message), probe)
    async with webhook_running(env, monkeypatch, config=config) as (group, requests, _rt):
        await group.ask(message)
        assert requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job") == 0


async def test_runner_can_correct_datetime_without_unnecessary_clarification(env, monkeypatch):
    data = task_config(env).model_dump(mode="json")
    data["budget"]["max_tool_calls"] = 5
    config = runtime.ServeConfig.model_validate(data)
    message = "明天10点给 ops_alert_bot 发汇报提醒"
    at = (env.clock() + timedelta(days=1)).replace(hour=2, minute=0, second=0, microsecond=0)

    def corrected(call):
        rejected = [
            json.loads(item["output"])
            for item in call.input
            if item.get("type") == "function_call_output"
        ]
        assert any(
            item.get("status") == "rejected"
            and item.get("retry_this_turn") is True
            and "时区" in item["reason"]
            for item in rejected
        )
        return tool_call(
            "scheduled__propose_reminder",
            bot_alias="ops_alert_bot",
            text="汇报提醒",
            evidence_ids=[],
            at=at.isoformat(),
        )(call)

    env.scripts.add(
        attributed(A, message),
        tool_call(
            "scheduled__propose_reminder",
            bot_alias="ops_alert_bot",
            text="汇报提醒",
            evidence_ids=[],
            at=at.replace(tzinfo=None).isoformat(),
        ),
        corrected,
        request_execution,
        cite(),
    )
    async with webhook_running(env, monkeypatch, config=config) as (group, requests, _rt):
        await group.ask(message)
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job") == 1
        action = await env.scalar("SELECT action_id FROM xiaowei_action")
        await group.ask(f"/批准 {action}")
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"
        assert len(env.scripts.calls[attributed(A, message)]) == 4
        assert requests == []


async def test_budget_timeout_after_claim_settles_unknown_without_restart_or_repost(
    env, monkeypatch
):
    from xiaowei.webhook import WebhookNotifications

    calls = []

    async def delayed_post(*_args):
        calls.append(1)
        await asyncio.Event().wait()

    config = task_config(env)
    async with webhook_running(env, monkeypatch, config=config) as (group, _requests, _rt):
        await reminder(env, group)
    data = config.model_dump(mode="json")
    data["budget"]["timeout_seconds"] = 1
    monkeypatch.setattr(WebhookNotifications, "_post", delayed_post)
    env.clock.advance(100)
    async with webhook_running(
        env, monkeypatch, config=runtime.ServeConfig.model_validate(data)
    ) as (_group, _requests, rt):
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_notification_attempt") == "unknown"
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "completed"
        assert rt.lock.readiness.ok
        await rt.scheduled_tasks.tick()
        assert len(calls) == 1


async def test_cleanup_marks_unstarted_late_reminder_missed(env, monkeypatch):
    from xiaowei.session import cleanup_expired

    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        await reminder(env, group)
        env.clock.advance(3661)
        await cleanup_expired(rt.engine, now=env.clock(), batch_size=100)
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "missed"
        assert await env.scalar("SELECT reason FROM xiaowei_scheduled_job") == "lateness_exceeded"
        await rt.scheduled_tasks.tick()
        assert requests == []


@pytest.mark.parametrize("delivered", [True, False])
async def test_task_status_delivered_risk_notice_controls_explicit_resend(
    env, monkeypatch, delivered
):
    from tests.sdk_core.test_feishu_render import message_text

    data = task_config(env).model_dump(mode="json")
    data["webhook_bots"][0]["receive_tools"].append("scheduled/get_task")
    data["webhook_bots"][0]["receive_targets"].append("scheduled")

    async with webhook_running(
        env,
        monkeypatch,
        config=runtime.ServeConfig.model_validate(data),
        response=httpx2.ReadTimeout("synthetic"),
    ) as (group, requests, rt):
        job = await reminder(env, group)
        env.clock.advance(100)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        await group.ask("/新建")
        message = "查询这个提醒状态"
        env.scripts.add(
            attributed(A, message), tool_call("scheduled__get_task", job_id=job), cite()
        )
        if not delivered:
            group.outbox.outcomes.append("failed")
        await group.ask(message)
        text = message_text(group.outbox.sent[-1][1])
        assert "可能已" in text and "重发可能重复" in text
        message = "再发一次汇报提醒"
        env.scripts.add(
            attributed(A, message),
            tool_call(
                "send_webhook_message", bot_alias="ops_alert_bot", text="汇报提醒", evidence_ids=[]
            ),
            cite(),
        )
        await group.ask(message)
        assert len(requests) == (2 if delivered else 1)


async def test_old_task_risk_notice_cannot_acknowledge_a_later_unknown_send(env, monkeypatch):
    data = task_config(env).model_dump(mode="json")
    data["webhook_bots"][0]["receive_tools"].append("scheduled/get_task")
    data["webhook_bots"][0]["receive_targets"].append("scheduled")
    async with webhook_running(
        env,
        monkeypatch,
        config=runtime.ServeConfig.model_validate(data),
        response=httpx2.ReadTimeout("synthetic"),
    ) as (group, requests, rt):
        job = await reminder(env, group)
        env.clock.advance(100)
        await rt.scheduled_tasks.tick()
        await group.ask("/新建")
        for i in (1, 2):
            query = f"查询旧提醒状态{i}"
            env.scripts.add(
                attributed(A, query), tool_call("scheduled__get_task", job_id=job), cite()
            )
            await group.ask(query)
            message = f"再发一次汇报提醒{i}"
            env.scripts.add(
                attributed(A, message),
                tool_call(
                    "send_webhook_message",
                    bot_alias="ops_alert_bot",
                    text="汇报提醒",
                    evidence_ids=[],
                ),
                cite(),
            )
            if i == 1:
                group.outbox.outcomes.append("failed")  # 新 unknown 的提示未送达。
            await group.ask(message)
        assert len(requests) == 2  # 旧回执提示不确认后来出现的新 unknown。


@pytest.mark.parametrize("control", [False, True])
async def test_revoked_definition_returns_safe_status_and_control_outcome(
    env, monkeypatch, control
):
    config = task_config(env)
    async with webhook_running(env, monkeypatch, config=config) as (group, _requests, _rt):
        await history(env, group)
        job = await reminder(env, group, content="模型解释保密正文")
        await group.ask("/新建")
    data = config.model_dump(mode="json")
    data["feishu"]["group"]["tools"].remove("local/run_readonly_query")
    async with webhook_running(
        env, monkeypatch, config=runtime.ServeConfig.model_validate(data)
    ) as (group, requests, _rt):
        message = "管理已撤权任务"

        def checked(call):
            values = [
                json.loads(i["output"])
                for i in call.input
                if i.get("type") == "function_call_output"
            ]
            assert any(item["data"].get("text") is None for item in values)
            assert "模型解释保密正文" not in json.dumps(call.input, ensure_ascii=False)
            return cite()(call)

        env.scripts.add(
            attributed(A, message),
            tool_call(
                "scheduled__control_task" if control else "scheduled__get_task",
                job_id=job,
                **({"operation": "pause"} if control else {}),
            ),
            checked,
        )
        await group.ask(message, message_id="om_revoked_task")
        assert len(env.scripts.calls[attributed(A, message)]) == 2
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == (
            "paused" if control else "active"
        )
        assert requests == []


@pytest.mark.parametrize("already_warned", [False, True])
async def test_status_does_not_consume_the_send_turn(env, monkeypatch, already_warned):
    data = task_config(env).model_dump(mode="json")
    data["webhook_bots"][0]["receive_tools"].append("scheduled/get_task")
    data["webhook_bots"][0]["receive_targets"].append("scheduled")
    async with webhook_running(
        env,
        monkeypatch,
        config=runtime.ServeConfig.model_validate(data),
        response=httpx2.ReadTimeout("synthetic"),
    ) as (group, requests, rt):
        job = await reminder(env, group)
        env.clock.advance(100)
        await rt.scheduled_tasks.tick()
        await group.ask("/新建")
        if already_warned:
            message = "先查发送状态"
            env.scripts.add(
                attributed(A, message), tool_call("scheduled__get_task", job_id=job), cite()
            )
            await group.ask(message)
        message = "查这个任务状态并明确再发一次"
        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__get_task", job_id=job),
            tool_call(
                "send_webhook_message", bot_alias="ops_alert_bot", text="汇报提醒", evidence_ids=[]
            ),
            cite(),
        )
        await group.ask(message)
        assert len(env.scripts.calls[attributed(A, message)]) == 3
        assert len(requests) == (2 if already_warned else 1)


async def test_plain_reminder_deadline_does_not_shorten_shared_session(env, monkeypatch):
    from xiaowei.session import cleanup_expired

    data = task_config(env).model_dump(mode="json")
    data["session_limits"]["retention_seconds"] = 86400
    data["storage"]["request_retention_seconds"] = 86400
    data["storage"]["evidence_retention_seconds"] = 86400
    async with webhook_running(
        env, monkeypatch, config=runtime.ServeConfig.model_validate(data)
    ) as (group, requests, rt):
        await reminder(env, group)
        assert await env.scalar(
            "SELECT expires_at FROM xiaowei_session"
        ) == env.clock() + timedelta(days=1)
        env.clock.advance(3661)
        await cleanup_expired(rt.engine, now=env.clock(), batch_size=100)
        message = "你好"
        env.scripts.add(attributed(A, message), cite("你好"))
        await group.ask(message)
        assert len(env.scripts.calls[attributed(A, message)]) == 1
        assert "你好" in message_text(group.outbox.sent[-1][1])
        assert requests == []


async def test_reminder_send_deadline_preserves_valid_history_and_data_ttl(env, monkeypatch):
    from xiaowei.session import cleanup_expired

    data = task_config(env).model_dump(mode="json")
    data["session_limits"]["retention_seconds"] = 604800
    data["storage"]["request_retention_seconds"] = 604800
    data["storage"]["evidence_retention_seconds"] = 604800
    async with webhook_running(
        env, monkeypatch, config=runtime.ServeConfig.model_validate(data)
    ) as (group, requests, rt):
        evidence = await history(env, group)
        expiry = await env.scalar("SELECT expires_at FROM xiaowei_evidence")
        await reminder(env, group, seconds=600)
        env.clock.advance(4201)  # 发送窗口已过，但历史数据仍有效。
        await cleanup_expired(rt.engine, now=env.clock(), batch_size=100)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "missed"
        assert await env.scalar("SELECT expires_at FROM xiaowei_session") == expiry
        assert await env.scalar("SELECT content_expires_at FROM xiaowei_scheduled_job") == expiry
        assert await env.scalar("SELECT text FROM xiaowei_scheduled_job") == "汇报提醒"
        assert not await env.scalar("SELECT task_content_purged FROM xiaowei_session")
        message = "继续查看刚才的销售"
        env.scripts.add(attributed(A, message), cite())
        await group.ask(message)
        assert evidence in evidence_in(env.scripts.calls[attributed(A, message)][0])
        assert "会话历史不能安全继续" not in message_text(group.outbox.sent[-1][1])
        env.clock.advance((expiry - env.clock()).total_seconds() + 1)
        await cleanup_expired(rt.engine, now=env.clock(), batch_size=100)
        assert await env.scalar("SELECT text FROM xiaowei_scheduled_job") is None
        assert await env.scalar("SELECT state FROM xiaowei_session") == "closed"
        assert requests == []


async def test_send_deadline_is_rechecked_at_atomic_claim(env, monkeypatch):
    data = task_config(env).model_dump(mode="json")
    data["storage"]["evidence_retention_seconds"] = 604800
    data["session_limits"]["retention_seconds"] = 604800
    data["storage"]["request_retention_seconds"] = 604800
    async with webhook_running(
        env, monkeypatch, config=runtime.ServeConfig.model_validate(data)
    ) as (group, requests, rt):
        await history(env, group)
        await reminder(env, group)
        claim = rt.scheduled_tasks._claim

        async def delayed_claim(*args):
            env.clock.advance(3600)
            return await claim(*args)

        monkeypatch.setattr(rt.scheduled_tasks, "_claim", delayed_claim)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "missed"
        assert await env.scalar("SELECT reason FROM xiaowei_scheduled_job") == "lateness_exceeded"
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0
        assert requests == []


async def test_documented_receiver_supports_consecutive_reminders_and_immediate_send(
    env, monkeypatch
):
    async with webhook_running(env, monkeypatch, config=documented_task_config(env)) as (
        group,
        requests,
        rt,
    ):
        jobs = []
        for i in (1, 2):
            message = f"同群设置第{i}条提醒"
            env.scripts.add(
                attributed(A, message),
                tool_call(
                    "scheduled__propose_reminder",
                    bot_alias="ops_alert_bot",
                    text=f"提醒{i}",
                    evidence_ids=[],
                    at=(env.clock() + timedelta(seconds=60 * i)).isoformat(),
                ),
                request_execution,
                cite(),
            )
            await group.ask(message)
            async with rt.engine.connect() as conn:
                job, action = (
                    await conn.execute(
                        text(
                            "SELECT j.job_id,a.action_id FROM xiaowei_scheduled_job j "
                            "JOIN xiaowei_action a ON "
                            "a.plan::jsonb #>> '{plan,parameters,job_id}'=j.job_id "
                            "WHERE j.text=:body"
                        ),
                        {"body": f"提醒{i}"},
                    )
                ).one()
            await group.ask(f"/批准 {action}")
            jobs.append(job)
            if i == 1:
                session = await env.scalar("SELECT session_id FROM xiaowei_channel_session")
                message = "马上发今晚22点检查"
                env.scripts.add(
                    attributed(A, message),
                    tool_call(
                        "send_webhook_message",
                        bot_alias="ops_alert_bot",
                        text="今晚22点检查",
                        evidence_ids=[],
                    ),
                    cite(),
                )
                await group.ask(message)
                assert len(requests) == 1
        assert (
            await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job WHERE state='active'") == 2
        )
        for _ in (1, 2):
            env.clock.advance(60)
            await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert [json.loads(r.content)["content"]["text"] for r in requests] == [
            "今晚22点检查",
            "提醒1",
            "提醒2",
        ]
        assert await env.scalar("SELECT session_id FROM xiaowei_channel_session") == session
        async with rt.engine.connect() as conn:
            ids = await conn.scalar(
                text("SELECT definition_evidence_ids FROM xiaowei_scheduled_job WHERE job_id=:id"),
                {"id": jobs[1]},
            )
        assert json.loads(ids)  # 第二份定义保留真实的历史依赖，没有过滤任务记录。


async def test_missing_task_receiver_permission_explains_configuration_without_http(
    env, monkeypatch
):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, _rt):
        await reminder(env, group)
        message = "先发一条检查消息"
        env.scripts.add(
            attributed(A, message),
            tool_call(
                "send_webhook_message", bot_alias="ops_alert_bot", text="检查", evidence_ids=[]
            ),
            cite(),
        )
        await group.ask(message)
        shown = message_text(group.outbox.sent[-1][1])
        assert "管理员" in shown and "receive_tools" in shown and "scheduled" in shown
        message = "再设一条提醒"

        def check_feedback(call):
            rejected = [
                json.loads(i["output"])
                for i in call.input
                if i.get("type") == "function_call_output"
            ]
            assert any(
                i.get("status") == "rejected"
                and "管理员" in i["reason"]
                and "receive_tools" in i["reason"]
                and "scheduled" in i["reason"]
                for i in rejected
            )
            return cite("未创建提醒")(call)

        env.scripts.add(
            attributed(A, message),
            tool_call(
                "scheduled__propose_reminder",
                bot_alias="ops_alert_bot",
                text="提醒2",
                evidence_ids=[],
                at=(env.clock() + timedelta(seconds=120)).isoformat(),
            ),
            check_feedback,
        )
        await group.ask(message)
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job") == 1
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0
        assert requests == []


async def test_task_receiver_permission_never_inherits_business_source_permission(env, monkeypatch):
    data = documented_task_config(env).model_dump(mode="json")
    other = dict(data["webhook_bots"][0])
    other.update(
        bot_id="other",
        alias="other_bot",
        receive_targets=["scheduled"],
        receive_tools=[t for t in other["receive_tools"] if t.startswith("scheduled/")],
    )
    data["webhook_bots"].append(other)
    async with webhook_running(
        env, monkeypatch, config=runtime.ServeConfig.model_validate(data)
    ) as (
        group,
        requests,
        _rt,
    ):
        await history(env, group)
        await reminder(env, group, select=True)
        message = "把结果提醒发给另一个机器人"
        env.scripts.add(
            attributed(A, message),
            tool_call(
                "send_webhook_message", bot_alias="other_bot", text="结果提醒", evidence_ids=[]
            ),
            cite(),
        )
        await group.ask(message)
        assert requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0


async def test_approved_reminders_ignore_other_unknown_but_immediate_send_still_warns(
    env, monkeypatch
):
    async def response(_request):
        if len(requests) == 1:
            raise httpx2.ReadTimeout("synthetic")
        return httpx2.Response(200, json={"code": 0})

    async with webhook_running(
        env, monkeypatch, config=documented_task_config(env), response=response
    ) as (
        group,
        requests,
        rt,
    ):
        jobs = []
        for i in (1, 2):
            message = f"设置第{i}条提醒"
            env.scripts.add(
                attributed(A, message),
                tool_call(
                    "scheduled__propose_reminder",
                    bot_alias="ops_alert_bot",
                    text=f"提醒{i}",
                    evidence_ids=[],
                    at=(env.clock() + timedelta(seconds=60 * i)).isoformat(),
                ),
                request_execution,
                cite(),
            )
            await group.ask(message)
            async with rt.engine.connect() as conn:
                job_id, action = (
                    await conn.execute(
                        text(
                            "SELECT j.job_id,a.action_id FROM xiaowei_scheduled_job j "
                            "JOIN xiaowei_action a ON "
                            "a.plan::jsonb #>> '{plan,parameters,job_id}'=j.job_id "
                            "WHERE j.text=:body"
                        ),
                        {"body": f"提醒{i}"},
                    )
                ).one()
            await group.ask(f"/批准 {action}")
            jobs.append(job_id)
        assert (
            await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job WHERE state='active'") == 2
        )
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert (
            await env.scalar("SELECT state FROM xiaowei_notification_attempt WHERE state='unknown'")
            == "unknown"
        )
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert len(requests) == 2
        assert [json.loads(r.content)["content"]["text"] for r in requests] == ["提醒1", "提醒2"]
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 2
        message = "立即发送另一条提醒"
        env.scripts.add(
            attributed(A, message),
            tool_call(
                "send_webhook_message",
                bot_alias="ops_alert_bot",
                text="另一条提醒",
                evidence_ids=[],
            ),
            cite(),
        )
        await group.ask(message)
        assert len(requests) == 2
        assert "重发可能重复" in message_text(group.outbox.sent[-1][1])
        async with rt.engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT state FROM xiaowei_notification_attempt WHERE job_id=:id"),
                    {"id": jobs[0]},
                )
                == "unknown"
            )


@pytest.mark.parametrize("offset", [60, 61, 300])
async def test_approval_at_or_after_due_time_rejects_activation(env, monkeypatch, offset):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        await reminder(env, group, confirm=False)
        material = message_text(group.outbox.sent[-1][1])
        action = await env.scalar("SELECT action_id FROM xiaowei_action")
        env.clock.advance(offset)
        await group.ask(f"/批准 {action}")
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "draft"
        assert await env.scalar("SELECT state FROM xiaowei_action") == "rejected"
        assert await env.scalar("SELECT failure_code FROM xiaowei_action") == "reminder_time_passed"
        assert "提醒时间已过，未激活，请重新创建" in message_text(group.outbox.sent[-1][1])
        assert "提醒时间之前批准" in material
        assert "已激活" in material
        message = "查询过时批准的结果"
        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__get_action_status", action_id=action),
            cite(),
        )
        await group.ask(message)
        assert "提醒时间已过，未激活，请重新创建" in message_text(group.outbox.sent[-1][1])
        await rt.scheduled_tasks.tick()
        assert requests == []


@pytest.mark.parametrize("operation", ["pause", "cancel"])
async def test_control_after_claim_reports_send_already_admitted(env, monkeypatch, operation):
    entered, release = asyncio.Event(), asyncio.Event()

    async def response(_request):
        entered.set()
        await release.wait()
        return httpx2.Response(200, json={"code": 0})

    async with webhook_running(env, monkeypatch, config=task_config(env), response=response) as (
        group,
        requests,
        rt,
    ):
        job = await reminder(env, group)
        env.clock.advance(100)
        due = asyncio.create_task(rt.scheduled_tasks.tick())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            message = f"现在{operation}这个提醒"
            env.scripts.add(
                attributed(A, message),
                tool_call("scheduled__control_task", job_id=job, operation=operation),
                cite(),
            )
            await group.ask(message)
            assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"
            assert "发送已准入，未暂停/取消" in message_text(group.outbox.sent[-1][1])
        finally:
            release.set()
            await due
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "completed"
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1


async def test_control_waits_for_claim_commit_before_deciding(env, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncConnection
    from tests.sdk_core.test_channel_store import waiting_on_locks

    entered, release = asyncio.Event(), asyncio.Event()
    execute = AsyncConnection.execute

    async def hold_claim_commit(conn, statement, *args, **kwargs):
        result = await execute(conn, statement, *args, **kwargs)
        if str(statement).startswith("INSERT INTO xiaowei_notification_attempt "):
            entered.set()
            await release.wait()
        return result

    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        job = await reminder(env, group)
        env.clock.advance(100)
        monkeypatch.setattr(AsyncConnection, "execute", hold_claim_commit)
        due = asyncio.create_task(rt.scheduled_tasks.tick())
        control = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            message = "占用事务尚未提交时暂停"
            env.scripts.add(
                attributed(A, message),
                tool_call("scheduled__control_task", job_id=job, operation="pause"),
                cite(),
            )
            control = asyncio.create_task(group.ask(message))
            assert await waiting_on_locks(rt.engine, 1)
        finally:
            release.set()
            await due
            if control is not None:
                await control
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "completed"
        assert "发送已准入，未暂停/取消" in message_text(group.outbox.sent[-1][1])
        assert len(requests) == 1


async def test_activation_rechecks_time_after_waiting_for_admission_lock(env, monkeypatch):
    from tests.sdk_core.test_channel_store import waiting_on_locks

    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        await reminder(env, group, confirm=False)
        action = await env.scalar("SELECT action_id FROM xiaowei_action")
        approval = None
        async with rt.engine.begin() as conn:
            await conn.execute(text("LOCK TABLE xiaowei_scheduled_job IN SHARE ROW EXCLUSIVE MODE"))
            try:
                approval = asyncio.create_task(group.ask(f"/批准 {action}"))
                assert await waiting_on_locks(rt.engine, 1)
                env.clock.advance(60)
            finally:
                await conn.commit()
                if approval is not None:
                    await approval
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "draft"
        assert "提醒时间已过，未激活，请重新创建" in message_text(group.outbox.sent[-1][1])
        await rt.scheduled_tasks.tick()
        assert requests == []


@pytest.mark.parametrize("delivered", [False, True])
async def test_control_only_risk_notice_requires_actual_delivery(env, monkeypatch, delivered):
    data = task_config(env).model_dump(mode="json")
    data["webhook_bots"][0]["receive_tools"].append("scheduled/control_task")
    data["webhook_bots"][0]["receive_targets"].append("scheduled")
    async with webhook_running(
        env,
        monkeypatch,
        config=runtime.ServeConfig.model_validate(data),
        response=httpx2.ReadTimeout("synthetic"),
    ) as (group, _requests, rt):
        job = await reminder(env, group)
        env.clock.advance(100)
        await rt.scheduled_tasks.tick()
        await group.ask("/新建")
    data["feishu"]["group"]["tools"].remove("notifications/send_webhook_message")
    data["feishu"]["group"]["tools"].remove("scheduled/get_task")
    data["scheduled_tasks"]["enabled"] = False
    async with webhook_running(
        env, monkeypatch, config=runtime.ServeConfig.model_validate(data)
    ) as (group, requests, _rt):
        message = "取消这个未知结果任务"
        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__control_task", job_id=job, operation="cancel"),
            cite(),
        )
        if not delivered:
            group.outbox.outcomes.append("failed")
        await group.ask(message, message_id="om_control_only_risk")
        assert "重发可能重复" in message_text(group.outbox.sent[-1][1])
        assert requests == []
    data["feishu"]["group"]["tools"].append("notifications/send_webhook_message")
    async with webhook_running(
        env, monkeypatch, config=runtime.ServeConfig.model_validate(data)
    ) as (group, requests, _rt):
        message = "明确再发一次汇报提醒"
        env.scripts.add(
            attributed(A, message),
            tool_call(
                "send_webhook_message", bot_alias="ops_alert_bot", text="汇报提醒", evidence_ids=[]
            ),
            cite(),
        )
        await group.ask(message, message_id="om_after_control_risk")
        assert len(requests) == (1 if delivered else 0)


async def test_completed_fixed_reminder_keeps_admitted_control_reason(env, monkeypatch):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        job = await reminder(env, group)
        env.clock.advance(100)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "completed"
        message = "暂停已发出的一次性提醒"
        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__control_task", job_id=job, operation="pause"),
            cite(),
        )
        await group.ask(message)
        assert "发送已准入，未暂停/取消" in message_text(group.outbox.sent[-1][1])
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "completed"
        assert len(requests) == 1


async def test_cancelled_fixed_reminder_repeat_cancel_does_not_claim_send_admission(
    env, monkeypatch
):
    async with webhook_running(env, monkeypatch, config=task_config(env)) as (group, requests, rt):
        job = await reminder(env, group, seconds=600)
        message = "取消这条提醒"
        for index in range(2):
            env.scripts.add(
                attributed(A, message),
                tool_call("scheduled__control_task", job_id=job, operation="cancel"),
                cite("任务已取消。"),
            )
            await group.ask(message, message_id=f"om_repeat_cancel_{index}")
            assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "cancelled"
            body = message_text(group.outbox.sent[-1][1])
            assert "发送已准入" not in body
            assert "cancelled" in body
            assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0
            assert requests == []
        env.clock.advance(600)
        await rt.scheduled_tasks.tick()
        assert requests == []
