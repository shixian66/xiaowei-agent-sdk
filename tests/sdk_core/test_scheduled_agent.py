"""C：正式群入口、真 SDK/治理/隔离 PG；模型和外部 I/O 为合成替身。"""

import json
from datetime import timedelta

import pytest
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_actions import request_execution
from tests.sdk_core.test_group_identity import A
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_scheduled_tasks import task_config
from tests.sdk_core.test_webhook_notification import running

from xiaowei import runtime
from xiaowei.feishu import attributed

pytestmark = pytest.mark.loopback
QUERY = "local/run_readonly_query"
TASK = "读取销售统计，汇总当前各区域总额。"


def agent_config(env, **options):
    data = task_config(env, **options).model_dump(mode="json")
    data["scheduled_tasks"]["automatic_tools"] = [QUERY]
    data["scheduled_tasks"]["queue_timeout_seconds"] = 900
    data["feishu"]["group"]["tools"].append("scheduled/propose_agent_task")
    data["data_policy"]["model_tools"].append("scheduled/propose_agent_task")
    bot = data["webhook_bots"][0]
    bot["receive_targets"].append("scheduled")
    bot["receive_tools"].extend(
        [
            "scheduled/propose_agent_task",
            "scheduled/request_action_execution",
            "scheduled/activate_reminder",
            "scheduled/get_action_status",
            "scheduled/get_task",
        ]
    )
    return runtime.ServeConfig.model_validate(data)


def draft(env, *, tools=None, interval=None, task=TASK, at="default", expires_at=None):
    message = "一分钟后读取销售统计并通知运维机器人"
    at = env.clock() + timedelta(seconds=60) if at == "default" else at
    env.scripts.add(
        attributed(A, message),
        tool_call(
            "scheduled__propose_agent_task",
            bot_alias="ops_alert_bot",
            task=task,
            target_ids=[SR.target_id],
            tool_ids=tools or [QUERY],
            at=at.isoformat() if at is not None else None,
            interval_seconds=interval,
            expires_at=expires_at.isoformat() if expires_at is not None else None,
        ),
        request_execution,
        cite("等待确认"),
    )
    return message


async def test_one_time_agent_uses_runner_read_tools_and_fact_delivery(env, monkeypatch):
    """抓住跳过 Runner、写死业务执行、事实由模型伪造或未治理直接 POST。"""
    message = draft(env)
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        cite("本次调查完成。"),
    )
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await group.ask(message)
        material = message_text(group.outbox.sent[-1][1])
        assert TASK in material and QUERY in material
        assert requests == []
        action = await env.scalar("SELECT action_id FROM xiaowei_action")
        await group.ask(f"/批准 {action}")
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        sent = json.loads(requests[0].content)["content"]["text"]
        assert "east" in sent and "100" in sent and "west" in sent and "50" in sent
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "completed"
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "completed"
        calls = env.scripts.calls[TASK]
        assert len(calls) == 2
        assert all("send_webhook_message" not in c.tools for c in calls)
        assert all(
            not any("propose" in t or "request_action_execution" in t for t in c.tools)
            for c in calls
        )
        assert await env.scalar("SELECT count(DISTINCT session_id) FROM xiaowei_session") == 2


async def test_automatic_task_cannot_request_unapproved_write_tool(env, monkeypatch):
    """请求范围不是授权：生产写不能被任务确认提升为自动工具。"""
    message = draft(env, tools=["scheduled/activate_reminder"])
    env.scripts.by_message[attributed(A, message)][1:] = [cite("该工具不允许自动执行，未创建。")]
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, _rt):
        await group.ask(message)
        assert requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job") == 0
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_run") == 0
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
        assert "未创建" in message_text(group.outbox.sent[-1][1])


async def activate(env, group, *, interval=None, task=TASK, **schedule):
    await group.ask(draft(env, interval=interval, task=task, **schedule))
    action = await env.scalar(
        "SELECT action_id FROM xiaowei_action ORDER BY created_at DESC LIMIT 1"
    )
    await group.ask(f"/批准 {action}")
    return await env.scalar("SELECT job_id FROM xiaowei_scheduled_job ORDER BY run_at DESC LIMIT 1")


async def test_periodic_runs_use_new_sessions_and_never_replay_previous_findings(env, monkeypatch):
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    )
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group, interval=10800)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        env.clock.advance(10800)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 2
        assert await env.scalar("SELECT count(DISTINCT session_id) FROM xiaowei_scheduled_run") == 2
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"
        for call in (env.scripts.calls[TASK][0], env.scripts.calls[TASK][2]):
            assert not any(item.get("type") == "function_call_output" for item in call.input)


async def test_agent_dependency_ttl_reaches_session_and_evidence(env, monkeypatch):
    from tests.sdk_core.test_scheduled_tasks import history

    from xiaowei.session import cleanup_expired

    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        old = await history(env, group)
        expiry = env.clock() + timedelta(seconds=120)
        async with rt.engine.begin() as conn:
            from sqlalchemy import text

            await conn.execute(
                text("UPDATE xiaowei_evidence SET expires_at=:expiry WHERE evidence_id=:id"),
                {"expiry": expiry, "id": old},
            )
        env.scripts.add(
            TASK,
            tool_call(
                "run_readonly_query",
                cluster=SR.target_id,
                sql="SELECT region,total FROM shop.sales",
            ),
            cite(),
        )
        await activate(env, group)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        run_session = await env.scalar("SELECT session_id FROM xiaowei_scheduled_run")
        assert (
            await env.scalar(
                "SELECT expires_at FROM xiaowei_session WHERE session_id=:id", {"id": run_session}
            )
            == expiry
        )
        assert (
            await env.scalar(
                "SELECT expires_at FROM xiaowei_evidence WHERE session_id=:id", {"id": run_session}
            )
            == expiry
        )
        env.clock.advance(61)
        await cleanup_expired(rt.engine, now=env.clock(), batch_size=100)
        from agents.extensions.memory import SQLAlchemySession

        assert await SQLAlchemySession(run_session, engine=rt.engine).get_items() == []
        assert (
            await env.scalar(
                "SELECT count(*) FROM xiaowei_evidence WHERE session_id=:id", {"id": run_session}
            )
            == 0
        )
        assert await env.scalar("SELECT text FROM xiaowei_scheduled_job") is None


async def test_agent_logs_public_sdk_usage_and_elapsed_time(env, monkeypatch, caplog):
    caplog.set_level("INFO", logger="xiaowei.app")
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    )
    async with running(env, monkeypatch, config=agent_config(env)) as (group, _requests, rt):
        await activate(env, group)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        records = [
            r.message for r in caplog.records if r.message.startswith("scheduled_agent_usage ")
        ]
        assert len(records) == 1
        usage = json.loads(records[0].split(" ", 1)[1])
        assert usage["model_requests"] == 2
        assert usage["tool_calls"] == 1
        assert usage["input_tokens"] is None  # 替身未上报，不把SDK补零当真实用量。
        assert usage["duration_seconds"] >= 0
        assert TASK not in records[0] and "SELECT" not in records[0]


async def test_unfinished_agent_run_recovers_unknown_without_model_or_post_replay(env, monkeypatch):
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group, interval=10800)
        # 真正持久化的运行占用：恢复面对的就是此状态，不模拟恢复函数。
        job_id = await env.scalar("SELECT job_id FROM xiaowei_scheduled_job")
        stored = await rt.scheduled_tasks.definitions.get(job_id)
        env.clock.advance(60)
        job, pending = await rt.scheduled_tasks._schedule_agent(stored)
        assert await rt.scheduled_tasks._start_agent_run(job, pending)
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "running"
        calls = sum(map(len, env.scripts.calls.values()))
        await rt.scheduled_tasks.recover()
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "unknown"
        assert requests == []
        assert sum(map(len, env.scripts.calls.values())) == calls


async def test_long_downtime_skips_history_and_runs_only_current_anchor(env, monkeypatch):
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    )
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group, interval=3600)
        env.clock.advance(60 + 3 * 3600 + 30)
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert await env.scalar(
            "SELECT scheduled_at FROM xiaowei_scheduled_run WHERE state='completed'"
        ) == env.clock() - timedelta(seconds=30)
        assert (
            await env.scalar("SELECT count(*) FROM xiaowei_scheduled_run WHERE state='running'")
            == 0
        )
        assert await env.scalar(
            "SELECT next_run FROM xiaowei_scheduled_job"
        ) == env.clock() + timedelta(seconds=3570)
        assert len(env.scripts.calls[TASK]) == 2
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_run") == 1


async def test_long_task_starts_at_activation_without_configured_end_or_count_limit(
    env, monkeypatch
):
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    )
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group, interval=3600, at=None)
        material = message_text(group.outbox.sent[-2][1])
        assert "长期" in material
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert await env.scalar("SELECT run_at FROM xiaowei_scheduled_job") == env.clock()
        assert await env.scalar(
            "SELECT next_run FROM xiaowei_scheduled_job"
        ) == env.clock() + timedelta(hours=1)
        assert await env.scalar("SELECT content_expires_at FROM xiaowei_scheduled_job") is None


async def test_busy_agent_queues_same_occurrence_then_runs_once(env, monkeypatch):
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    )
    cfg = agent_config(env).model_copy(update={"max_concurrent_turns": 1})
    async with running(env, monkeypatch, config=cfg) as (group, requests, rt):
        await activate(env, group, interval=3600)
        env.clock.advance(60)
        with rt.service._app.background_slot() as occupied:
            assert occupied
            await rt.scheduled_tasks.tick()
            assert requests == []
            assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "queued"
            run_id = await env.scalar("SELECT run_id FROM xiaowei_scheduled_run")
        env.clock.advance(120)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert await env.scalar("SELECT run_id FROM xiaowei_scheduled_run") == run_id
        assert await env.scalar(
            "SELECT next_run FROM xiaowei_scheduled_job"
        ) == env.clock() + timedelta(seconds=3480)


async def test_queue_timeout_skips_without_model_or_new_opportunity(env, monkeypatch):
    cfg = agent_config(env).model_copy(update={"max_concurrent_turns": 1})
    async with running(env, monkeypatch, config=cfg) as (group, requests, rt):
        await activate(env, group, interval=3600)
        calls = sum(map(len, env.scripts.calls.values()))
        env.clock.advance(60)
        with rt.service._app.background_slot():
            await rt.scheduled_tasks.tick()
            env.clock.advance(901)
            await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "skipped"
        assert await env.scalar("SELECT reason FROM xiaowei_scheduled_run") == "queue_timeout"
        assert sum(map(len, env.scripts.calls.values())) == calls
        assert requests == []


async def test_shorter_than_one_hour_interval_is_rejected_before_task_creation(env, monkeypatch):
    message = draft(env, interval=3599)
    env.scripts.by_message[attributed(A, message)][1:] = [cite("周期须至少1小时，未创建。")]
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, _rt):
        await group.ask(message)
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job") == 0
        assert requests == []


def managed_config(env):
    data = agent_config(env).model_dump(mode="json")
    tools = ["scheduled/list_tasks"]
    data["feishu"]["group"]["tools"].extend(tools)
    data["data_policy"]["model_tools"].extend(tools)
    data["webhook_bots"][0]["receive_tools"].extend(tools)
    return runtime.ServeConfig.model_validate(data)


async def test_group_task_list_exposes_only_visible_jobs_and_permission_checked_body(
    env, monkeypatch
):
    from tests.sdk_core.test_app import last_output

    async with running(env, monkeypatch, config=managed_config(env)) as (group, requests, _rt):
        await activate(env, group, interval=3600)
        job_id = await env.scalar("SELECT job_id FROM xiaowei_scheduled_job")
        message = "本群有哪些定时任务"

        def inspect_list(call):
            data = last_output(call)["data"]
            assert len(data["tasks"]) == 1
            item = data["tasks"][0]
            assert item["job_id"] == job_id and item["kind"] == "agent_task"
            assert item["interval_seconds"] == 3600 and item["expires_at"] is None
            assert item["text"] == TASK
            return cite()(call)

        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__list_tasks", limit=10, after_id=None, include_text=True),
            inspect_list,
        )
        await group.ask(message)
        assert requests == []
        assert job_id in message_text(group.outbox.sent[-1][1])


async def test_expiry_between_runs_posts_one_original_group_notice_without_model(env, monkeypatch):
    until = env.clock() + timedelta(seconds=90)
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    )
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        job = await activate(env, group, interval=3600, expires_at=until)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert "本次为到期前最后一次" in json.loads(requests[0].content)["content"]["text"]
        calls = sum(map(len, env.scripts.calls.values()))
        before = len(group.outbox.sent)
        env.clock.advance(30)
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert sum(map(len, env.scripts.calls.values())) == calls
        assert len(group.outbox.sent) == before + 1
        assert f"任务〈{job}〉已于" in message_text(group.outbox.sent[-1][1])
        assert "到期停止" in message_text(group.outbox.sent[-1][1])
        assert await env.scalar("SELECT notice_state FROM xiaowei_scheduled_job") == "sent"


async def test_long_running_notice_is_appended_every_thirty_days_without_extra_message(
    env, monkeypatch
):
    steps = [
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    ]
    env.scripts.add(TASK, *steps, *steps, *steps)
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        job = await activate(env, group, interval=3600, at=None)
        await rt.scheduled_tasks.tick()
        sent = len(group.outbox.sent)
        env.clock.advance(30 * 86400)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 2
        assert (
            f"本任务已长期运行 30 天，不再需要请 @小维 取消任务〈{job}〉"
            in json.loads(requests[1].content)["content"]["text"]
        )
        env.clock.advance(3600)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 3
        assert "本任务已长期运行" not in json.loads(requests[2].content)["content"]["text"]
        assert len(group.outbox.sent) == sent
        assert len(env.scripts.calls[TASK]) == 6


async def test_unverified_member_skips_this_occurrence_and_recovers_next_period(env, monkeypatch):
    allowed = True

    async def member(chat, subject):
        return allowed

    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    )
    async with running(env, monkeypatch, config=agent_config(env), member_check=member) as (
        group,
        requests,
        rt,
    ):
        await activate(env, group, interval=3600)
        allowed = False
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert (
            await env.scalar("SELECT reason FROM xiaowei_scheduled_run")
            == "authorization_unverified"
        )
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_run") == 1
        assert requests == []
        allowed = True
        env.clock.advance(3600)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"


async def test_cancel_queued_agent_prevents_model_and_expiry_notice(env, monkeypatch):
    cfg = agent_config(env).model_copy(update={"max_concurrent_turns": 1})
    async with running(env, monkeypatch, config=cfg) as (group, requests, rt):
        job = await activate(
            env, group, interval=3600, expires_at=env.clock() + timedelta(seconds=90)
        )
        env.clock.advance(60)
        with rt.service._app.background_slot():
            await rt.scheduled_tasks.tick()
        message = "取消这条任务"
        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__control_task", job_id=job, operation="cancel"),
            cite(),
        )
        await group.ask(message)
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "skipped"
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "cancelled"
        sent = len(group.outbox.sent)
        env.clock.advance(3600)
        await rt.scheduled_tasks.tick()
        assert requests == [] and len(group.outbox.sent) == sent


@pytest.mark.parametrize("has_more,expected", [(False, False), (True, None)])
async def test_member_absence_requires_complete_directory(has_more, expected):
    from types import SimpleNamespace

    from tests.sdk_core.test_feishu import config as feishu_config

    from xiaowei.feishu import LarkTransport

    calls = []

    def request(req):
        calls.append(req)
        payload = {"code": 0, "data": {"items": [], "has_more": has_more, "page_token": "next"}}
        return SimpleNamespace(code=0, raw=SimpleNamespace(content=json.dumps(payload).encode()))

    cfg = feishu_config(
        stop_timeout_seconds=8,
        queue_size=10,
        group={
            "chat_id": "oc_group",
            "tools": [QUERY],
            "member_max_pages": 1,
            "member_page_size": 50,
            "member_timeout_seconds": 2,
            "max_waiting": 10,
            "max_wait_seconds": 300,
            "wait_check_seconds": 1,
        },
    )
    channel = SimpleNamespace(client=SimpleNamespace(request=request))
    result = await LarkTransport(channel, cfg).member_status("oc_group", A)
    assert result is expected
    assert len(calls) == 1
    assert calls[0].paths == {"chat_id": "oc_group"}


async def test_definite_creator_departure_stops_and_notifies_original_group(env, monkeypatch):
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group, interval=3600)

        async def absent(chat, subject):
            return False

        object.__setattr__(rt.scheduled_tasks.definitions, "member_status", absent)
        before = len(group.outbox.sent)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert requests == []
        assert await env.scalar("SELECT reason FROM xiaowei_scheduled_job") == "member_absent"
        assert len(group.outbox.sent) == before + 1
        assert "创建者已不在原操作群" in message_text(group.outbox.sent[-1][1])


async def test_due_jobs_do_not_wait_for_unrelated_webhook_to_finish(env, monkeypatch):
    import asyncio

    import httpx2

    first = asyncio.Event()
    second = asyncio.Event()
    release = asyncio.Event()

    async def post(request):
        if not first.is_set():
            first.set()
            await release.wait()
        return httpx2.Response(200, json={"code": 0})

    steps = [
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    ]
    task2 = TASK + "这是第二条独立任务。"

    def second_query(call):
        second.set()
        return steps[0](call)

    env.scripts.add(TASK, *steps)
    env.scripts.add(task2, second_query, cite())
    async with running(
        env,
        monkeypatch,
        config=agent_config(env).model_copy(update={"max_concurrent_turns": 2}),
        response=post,
    ) as (
        group,
        requests,
        rt,
    ):
        await activate(env, group, interval=3600)
        env.clock.advance(1)
        await activate(env, group, interval=3600, task=task2)
        assert (
            await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job WHERE state='active'") == 2
        ), [message_text(m) for _, m, _ in group.outbox.sent]
        env.clock.advance(60)
        tick = asyncio.create_task(rt.scheduled_tasks.tick())
        try:
            await asyncio.wait_for(first.wait(), 5)
            await asyncio.wait_for(second.wait(), 2)
        finally:
            release.set()
            await tick
        assert len(requests) == 2
        assert (
            await env.scalar("SELECT count(*) FROM xiaowei_scheduled_run WHERE state='completed'")
            == 2
        )


@pytest.mark.parametrize("empty_final", [False, True])
async def test_nested_sdk_limit_finish_cannot_exceed_approved_model_call_budget(
    env, monkeypatch, empty_final
):
    cfg = agent_config(env)
    cfg = cfg.model_copy(update={"budget": cfg.budget.model_copy(update={"max_turns": 2})})
    query = tool_call(
        "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
    )
    from tests.sdk_core.test_app import answer

    env.scripts.add(TASK, query, (lambda c: answer([], "")) if empty_final else query, cite())
    async with running(env, monkeypatch, config=cfg) as (group, _requests, rt):
        await activate(env, group)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert len(env.scripts.calls.get(TASK, [])) <= 2
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "failed"
        await rt.scheduled_tasks.tick()
        assert len(env.scripts.calls.get(TASK, [])) <= 2


@pytest.mark.parametrize(
    "tool",
    ["scheduled/activate_reminder", "scheduled/get_task", "notifications/send_webhook_message"],
)
async def test_automatic_tool_configuration_rejects_non_business_read_tools(env, monkeypatch, tool):
    cfg = agent_config(env)
    cfg = cfg.model_copy(
        update={
            "scheduled_tasks": cfg.scheduled_tasks.model_copy(
                update={"automatic_tools": frozenset({tool})}
            )
        }
    )
    with pytest.raises(ValueError, match="automatic_tools"):
        async with running(env, monkeypatch, config=cfg):
            pytest.fail("invalid automatic tool set must fail before serving")


@pytest.mark.parametrize(
    "tool,target",
    [
        ("notifications/send_webhook_message", "notifications"),
        ("scheduled/get_task", "scheduled"),
        ("scheduled/control_task", "scheduled"),
        ("scheduled/propose_agent_task", "scheduled"),
        ("scheduled/activate_reminder", "scheduled"),
    ],
)
async def test_forged_background_call_is_rejected_by_governance_before_execute(
    env, monkeypatch, tool, target
):
    from xiaowei.governance import ToolRejectedError
    from xiaowei.models import RunContext, ToolRequest

    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group)
        original = RunContext.model_validate_json(
            await env.scalar("SELECT source_context FROM xiaowei_scheduled_job")
        )
        ctx = original.model_copy(
            update={
                "automatic": True,
                "target_scope": frozenset({target}),
                "tool_scope": frozenset({tool}),
            }
        )
        executed = []

        async def forbidden(request):
            executed.append(request)
            pytest.fail("background must never enter the adapter")

        with pytest.raises(ToolRejectedError):
            await rt.scheduled_tasks.governance.invoke(
                ctx,
                ToolRequest(
                    tool_id=tool,
                    target_id=target,
                    arguments=(
                        {"job_id": await env.scalar("SELECT job_id FROM xiaowei_scheduled_job")}
                        if target == "scheduled"
                        else {"bot_alias": "ops_alert_bot", "text": "test", "evidence_ids": []}
                    ),
                    call_id="forged",
                    tool_name=tool.replace("/", "__"),
                ),
                forbidden,
            )
        assert executed == [] and requests == []


async def test_agent_unknown_is_not_replayed_and_blocks_unwarned_immediate_send(env, monkeypatch):
    import httpx2

    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    )
    async with running(
        env, monkeypatch, config=agent_config(env), response=httpx2.ReadTimeout("synthetic")
    ) as (group, requests, rt):
        await activate(env, group)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert await env.scalar("SELECT state FROM xiaowei_notification_attempt") == "unknown"
        message = "给 ops_alert_bot 发消息：明确的新通知"
        env.scripts.add(
            attributed(A, message),
            tool_call(
                "send_webhook_message",
                bot_alias="ops_alert_bot",
                text="明确的新通知",
                evidence_ids=[],
            ),
            cite(),
        )
        await group.ask(message)
        assert len(requests) == 1
        assert "重发可能重复" in message_text(group.outbox.sent[-1][1])
        assert (
            await env.scalar(
                "SELECT reason FROM xiaowei_notification_attempt "
                "WHERE turn_id IS NOT NULL ORDER BY sequence DESC LIMIT 1"
            )
            == "duplicate_warning"
        )


async def test_sending_agent_cannot_be_paused_but_can_pause_between_runs(env, monkeypatch):
    import asyncio

    import httpx2

    started, release = asyncio.Event(), asyncio.Event()

    async def post(request):
        started.set()
        await release.wait()
        return httpx2.Response(200, json={"code": 0})

    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    )
    async with running(
        env,
        monkeypatch,
        config=agent_config(env).model_copy(update={"max_concurrent_turns": 2}),
        response=post,
    ) as (group, requests, rt):
        job = await activate(env, group, interval=3600)
        env.clock.advance(60)
        tick = asyncio.create_task(rt.scheduled_tasks.tick())
        try:
            await asyncio.wait_for(started.wait(), 5)
            message = "暂停这条巡检"
            env.scripts.add(
                attributed(A, message),
                tool_call("scheduled__control_task", job_id=job, operation="pause"),
                cite(),
            )
            await group.ask(message)
            assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"
            assert "发送已准入" in message_text(group.outbox.sent[-1][1])
        finally:
            release.set()
            await tick
        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__control_task", job_id=job, operation="pause"),
            cite(),
        )
        await group.ask(message)
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "paused"
        env.clock.advance(3600)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1


async def test_expiry_notice_unknown_is_durable_and_never_retried(env, monkeypatch):
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group, interval=3600, expires_at=env.clock() + timedelta(seconds=90))
        env.clock.advance(90)
        before = len(group.outbox.sent)
        group.outbox.outcomes.append("unknown")
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT notice_state FROM xiaowei_scheduled_job") == "unknown"
        await rt.scheduled_tasks.recover()
        await rt.scheduled_tasks.tick()
        assert requests == [] and len(group.outbox.sent) == before + 1
        job = await env.scalar("SELECT job_id FROM xiaowei_scheduled_job")
        message = "查询停止说明状态"
        env.scripts.add(
            attributed(A, message), tool_call("scheduled__get_task", job_id=job), cite()
        )
        await group.ask(message)
        assert "原群停止说明=unknown" in message_text(group.outbox.sent[-1][1])


async def test_paused_periodic_job_still_gets_one_expiry_notice(env, monkeypatch):
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        job = await activate(
            env, group, interval=3600, expires_at=env.clock() + timedelta(seconds=90)
        )
        message = "先暂停"
        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__control_task", job_id=job, operation="pause"),
            cite(),
        )
        await group.ask(message)
        before = len(group.outbox.sent)
        env.clock.advance(90)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "completed"
        assert len(group.outbox.sent) == before + 1 and requests == []


async def test_thirty_long_jobs_limit_is_checked_when_activating(env, monkeypatch):
    from sqlalchemy import text

    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group, interval=3600)
        # 已授权定义的合成副本建立边界数量；新的第31条仍从正式群审批入口激活。
        async with rt.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO xiaowei_scheduled_job "
                    "(job_id,owner_id,creator_id,bot_alias,bot_id,target_version,run_at,next_run,"
                    "content_expires_at,text,definition_evidence_ids,source_context,"
                    "lateness_seconds,kind,agent_spec,state) "
                    "SELECT md5(j.job_id || n::text),owner_id,creator_id,bot_alias,bot_id,"
                    "target_version,run_at,next_run,"
                    "content_expires_at,text,definition_evidence_ids,source_context,"
                    "lateness_seconds,kind,agent_spec,'active' "
                    "FROM xiaowei_scheduled_job j CROSS JOIN generate_series(1,29) n"
                )
            )
        env.clock.advance(1)
        await activate(env, group, interval=3600)
        assert (
            await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job WHERE state='active'")
            == 30
        )
        assert (
            await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job WHERE state='draft'") == 1
        )
        assert requests == []


async def test_agent_creation_checks_future_receiver_scope_without_history(env, monkeypatch):
    data = agent_config(env).model_dump(mode="json")
    data["webhook_bots"][0]["receive_tools"].remove(QUERY)
    message = draft(env)
    env.scripts.by_message[attributed(A, message)][1:] = [cite("机器人无权接收该源工具，未创建。")]
    async with running(env, monkeypatch, config=runtime.ServeConfig.model_validate(data)) as (
        group,
        requests,
        _rt,
    ):
        await group.ask(message)
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job") == 0
        assert requests == []


async def test_second_readonly_capability_uses_same_scheduler_without_business_dispatch(
    env, monkeypatch
):
    describe = "local/describe_table"
    cfg = agent_config(env)
    cfg = cfg.model_copy(
        update={
            "scheduled_tasks": cfg.scheduled_tasks.model_copy(
                update={"automatic_tools": frozenset({QUERY, describe})}
            )
        }
    )
    task = "检查销售表的列定义并汇总给运维。"
    env.scripts.add(
        task,
        tool_call(
            "describe_table", cluster=SR.target_id, database="shop", table="sales", cursor=None
        ),
        cite(),
    )
    async with running(env, monkeypatch, config=cfg) as (group, requests, rt):
        await activate(env, group, task=task, tools=[describe])
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "completed"
        assert len(requests) == 1 and "region" in json.loads(requests[0].content)["content"]["text"]
        assert all("run_readonly_query" not in c.tools for c in env.scripts.calls[task])


async def test_background_can_correct_query_and_delivers_partial_when_clarification_needed(
    env, monkeypatch
):
    from tests.sdk_core.test_app import answer, clarify, evidence_in

    env.scripts.add(
        TASK,
        tool_call("run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM sales"),
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        clarify("另外一个统计缺少日期，请补充。"),
        lambda c: answer(
            evidence_in(c), analysis="", clarification="另外一个统计缺少日期，请补充。"
        ),
    )
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT reason FROM xiaowei_scheduled_run") == "incomplete"
        assert len(requests) == 1
        body = json.loads(requests[0].content)["content"]["text"]
        assert "本次任务未完成" in body and "100" in body


async def test_poll_loop_admits_later_due_job_while_prior_run_is_waiting(env, monkeypatch):
    import asyncio

    import httpx2

    first_started, second_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def post(request):
        if not first_started.is_set():
            first_started.set()
            await release.wait()
        return httpx2.Response(200, json={"code": 0})

    cfg = agent_config(env, poll_seconds=0.05).model_copy(update={"max_concurrent_turns": 2})
    second_task = TASK + "后到期的独立任务。"
    query = tool_call(
        "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
    )

    def second_query(call):
        second_started.set()
        return query(call)

    env.scripts.add(TASK, query, cite())
    env.scripts.add(second_task, second_query, cite())
    async with running(env, monkeypatch, config=cfg, response=post) as (group, requests, _rt):
        await activate(env, group, interval=3600)
        env.clock.advance(1)
        await activate(
            env, group, task=second_task, interval=3600, at=env.clock() + timedelta(seconds=120)
        )
        env.clock.advance(60)
        try:
            await asyncio.wait_for(first_started.wait(), 5)
            env.clock.advance(60)
            await asyncio.wait_for(second_started.wait(), 2)
        finally:
            release.set()

        async def completed():
            while (
                await env.scalar(
                    "SELECT count(*) FROM xiaowei_scheduled_run WHERE state='completed'"
                )
                != 2
            ):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(completed(), 5)
        assert len(requests) == 2


@pytest.mark.parametrize("user_expiry", [False, True])
async def test_last_result_marks_earlier_definition_expiry(env, monkeypatch, user_expiry):
    from zoneinfo import ZoneInfo

    from sqlalchemy import text
    from tests.sdk_core.test_scheduled_tasks import history

    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        old = await history(env, group)
        expiry = env.clock() + timedelta(seconds=120)
        async with rt.engine.begin() as conn:
            await conn.execute(
                text("UPDATE xiaowei_evidence SET expires_at=:expiry WHERE evidence_id=:id"),
                {"expiry": expiry, "id": old},
            )
        env.scripts.add(
            TASK,
            tool_call(
                "run_readonly_query",
                cluster=SR.target_id,
                sql="SELECT region,total FROM shop.sales",
            ),
            cite(),
        )
        await activate(
            env,
            group,
            interval=3600,
            expires_at=env.clock() + timedelta(days=1) if user_expiry else None,
        )
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        body = json.loads(requests[0].content)["content"]["text"]
        assert "本次为到期前最后一次" in body
        assert expiry.astimezone(ZoneInfo("Asia/Shanghai")).isoformat() in body


async def test_paused_long_job_cannot_starve_new_active_job(env, monkeypatch):
    data = agent_config(env, max_active_jobs=1).model_dump(mode="json")
    data["webhook_bots"][0]["receive_tools"].append("scheduled/control_task")
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
        job = await activate(env, group, interval=3600)
        message = "暂停第一条任务"
        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__control_task", job_id=job, operation="pause"),
            cite(),
        )
        await group.ask(message)
        env.clock.advance(1)
        await activate(env, group, interval=3600)
        assert (
            await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job WHERE state='active'") == 1
        )
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1


@pytest.mark.parametrize("membership", [False, None])
async def test_start_permission_check_preserves_revoked_vs_unverified(env, monkeypatch, membership):
    query = tool_call(
        "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
    )
    env.scripts.add(TASK, query, cite(), query, cite())
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group, interval=3600)
        checks = 0

        async def member_status(chat, subject):
            nonlocal checks
            checks += 1
            return membership if checks == 1 else True

        object.__setattr__(rt.scheduled_tasks.definitions, "member_status", member_status)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert requests == []
        if membership is False:
            assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "disabled"
            assert await env.scalar("SELECT reason FROM xiaowei_scheduled_job") == "member_absent"
        else:
            assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"
            assert await env.scalar("SELECT unverified_count FROM xiaowei_scheduled_job") == 1
        env.clock.advance(3600)
        await rt.scheduled_tasks.tick()
        assert len(requests) == (0 if membership is False else 1)


async def test_consecutive_start_unverified_checks_accumulate_until_confirmed_run(env, monkeypatch):
    query = tool_call(
        "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
    )
    env.scripts.add(TASK, query, cite())
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group, interval=3600)
        checks = 0
        recovered = False

        async def member_status(chat, subject):
            nonlocal checks
            checks += 1
            return True if recovered else None

        object.__setattr__(rt.scheduled_tasks.definitions, "member_status", member_status)
        env.clock.advance(60)
        for count in range(1, 4):
            await rt.scheduled_tasks.tick()
            assert await env.scalar("SELECT unverified_count FROM xiaowei_scheduled_job") == count
            assert requests == []
            assert env.scripts.calls.get(TASK, []) == []
            env.clock.advance(3600)
        recovered = True
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert await env.scalar("SELECT unverified_count FROM xiaowei_scheduled_job") == 0
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_run") == 4


async def test_large_agent_result_sends_capacity_notice_once_then_next_period_runs(
    env, monkeypatch
):
    from tests.p1b.test_starrocks_adapter import Result, driver

    from xiaowei.models import AUDIENCES

    rows = [(f"PRIVATE_RESULT_{i}_" + "x" * 35, i) for i in range(1000)]
    env.drv = driver(Result(("region", "total"), rows))
    data = agent_config(env).model_dump(mode="json")
    data["targets"][0]["starrocks"]["policy"]["max_rows"] = 1000
    data["targets"][0]["starrocks"]["schema_limits"]["max_age_seconds"] = 7200
    data["targets"][0]["starrocks"]["max_result_bytes"] = 300_000
    data["targets"][0]["starrocks"]["max_value_bytes"] = 500
    data["projection_bytes"] = dict.fromkeys(AUDIENCES, 400_000)
    data["budget"]["max_scope_checks"] = 5000
    data["model"]["max_request_bytes"] = 2_000_000
    query = tool_call(
        "run_readonly_query",
        cluster=SR.target_id,
        sql="SELECT region,total FROM shop.sales LIMIT 1000",
    )
    env.scripts.add(TASK, query, cite(), query, cite())
    async with running(env, monkeypatch, config=runtime.ServeConfig.model_validate(data)) as (
        group,
        requests,
        rt,
    ):
        await activate(env, group, interval=3600)
        from sqlalchemy import text

        async with rt.engine.begin() as conn:
            await conn.execute(
                text("UPDATE xiaowei_scheduled_job SET approved_at=:old"),
                {"old": env.clock() - timedelta(days=30)},
            )
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT reason FROM xiaowei_scheduled_run") == "content_too_large"
        assert len(requests) == 1
        sent = json.loads(requests[0].content)["content"]["text"]
        assert "本次结果超过通知容量，未发送正文" in sent
        assert "PRIVATE_RESULT" not in sent
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 1
        assert await env.scalar("SELECT last_long_notice_at FROM xiaowei_scheduled_job") is None
        env.drv.make = driver(Result(("region", "total"), [("next_period", 10)])).make
        env.clock.advance(3600)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 2
        assert "next_period" in json.loads(requests[1].content)["content"]["text"]
        assert "本任务已长期运行 30 天" in json.loads(requests[1].content)["content"]["text"]
        assert (
            await env.scalar("SELECT last_long_notice_at FROM xiaowei_scheduled_job") == env.clock()
        )
        assert (
            await env.scalar("SELECT count(*) FROM xiaowei_scheduled_run WHERE state='completed'")
            == 1
        )


async def test_send_time_member_unverified_preserves_count_then_success_resets(env, monkeypatch):
    from sqlalchemy import text

    query = tool_call(
        "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
    )
    env.scripts.add(TASK, query, cite(), query, cite())
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group, interval=3600)
        async with rt.engine.begin() as conn:
            await conn.execute(text("UPDATE xiaowei_scheduled_job SET unverified_count=2"))
        sender_ok = False

        async def sender(_identity):
            return sender_ok

        monkeypatch.setattr(rt.scheduled_tasks.notifications, "_verify_sender", sender)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert requests == []
        assert (
            await env.scalar("SELECT reason FROM xiaowei_scheduled_run")
            == "authorization_unverified"
        )
        assert await env.scalar("SELECT unverified_count FROM xiaowei_scheduled_job") == 3
        sender_ok = True
        env.clock.advance(3600)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert await env.scalar("SELECT unverified_count FROM xiaowei_scheduled_job") == 0


@pytest.mark.parametrize("readiness_lost", [False, True])
async def test_one_job_scan_error_isolated_unless_readiness_lost(env, monkeypatch, readiness_lost):
    import asyncio

    from sqlalchemy.exc import OperationalError

    started = asyncio.Event()
    release = asyncio.Event()
    task2 = TASK + "独立第二任务。"
    query = tool_call(
        "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
    )

    async def second_query(call):
        started.set()
        await release.wait()
        return query(call)

    env.scripts.add(task2, second_query, cite())
    async with running(
        env, monkeypatch, config=agent_config(env).model_copy(update={"max_concurrent_turns": 2})
    ) as (group, requests, rt):
        first = await activate(env, group, interval=3600)
        env.clock.advance(1)
        await activate(env, group, task=task2, interval=3600)
        original = rt.scheduled_tasks._schedule_agent

        async def schedule(job):
            if job["job_id"] == first:
                await started.wait()
                if readiness_lost:
                    rt.readiness.lock("synthetic_instance_loss")
                else:
                    release.set()
                raise OperationalError("temporary", {}, RuntimeError("synthetic storage error"))
            return await original(job)

        monkeypatch.setattr(rt.scheduled_tasks, "_schedule_agent", schedule)
        env.clock.advance(60)
        if readiness_lost:
            with pytest.raises(OperationalError):
                await asyncio.wait_for(rt.scheduled_tasks.tick(), 5)
        else:
            await asyncio.wait_for(rt.scheduled_tasks.tick(), 5)
        assert len(requests) == (0 if readiness_lost else 1)
        assert rt.readiness.ok is not readiness_lost
        assert rt.scheduled_tasks._running_jobs == {}
        if not readiness_lost:
            assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "completed"


async def test_only_start_and_send_check_membership(env, monkeypatch):
    query = tool_call(
        "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
    )
    env.scripts.add(TASK, query, cite())
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group)
        checks = []

        async def start_member(chat, subject):
            checks.append("start")
            return True

        async def send_member(identity):
            checks.append("send")
            return True

        object.__setattr__(rt.scheduled_tasks.definitions, "member_status", start_member)
        monkeypatch.setattr(rt.scheduled_tasks.notifications, "_verify_sender", send_member)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert checks == ["start", "send"]


async def test_model_and_compatible_policy_change_use_current_config_without_stopping_job(
    env, monkeypatch
):
    cfg = agent_config(env)
    async with running(env, monkeypatch, config=cfg) as (group, _requests, _rt):
        await activate(env, group, interval=3600)
    data = cfg.model_dump(mode="json")
    data["model"]["model"] = "new-synthetic-model"
    data["data_policy"]["input"]["max_bytes"] += 100
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(),
    )
    async with running(env, monkeypatch, config=runtime.ServeConfig.model_validate(data)) as (
        _group,
        requests,
        rt,
    ):
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert "east" in json.loads(requests[0].content)["content"]["text"]
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "completed"


async def test_past_explicit_periodic_start_rejected_before_draft(env, monkeypatch):
    from tests.sdk_core.test_app import last_output

    message = draft(env, interval=3600, at=env.clock() - timedelta(seconds=1))

    def inspect(call):
        assert last_output(call)["hint"] == "agent_task_time"
        return cite("首跑时间已过，请改为未来时间或激活后首跑。")(call)

    env.scripts.by_message[attributed(A, message)][1:] = [inspect]
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, _rt):
        await group.ask(message)
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_job") == 0
        assert await env.scalar("SELECT count(*) FROM xiaowei_action") == 0
        assert requests == []


@pytest.mark.parametrize("operation", ["pause", "cancel"])
async def test_stopped_agent_control_reports_stopped_despite_historical_receipt(
    env, monkeypatch, operation
):
    from tests.sdk_core.test_app import last_output

    env.scripts.add(TASK, cite("已完成本次检查。"))
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        job = await activate(env, group)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        message = "暂停或取消已停止任务"

        def inspect(call):
            assert last_output(call)["data"]["reason"] == "任务已停止"
            return cite()(call)

        env.scripts.add(
            attributed(A, message),
            tool_call("scheduled__control_task", job_id=job, operation=operation),
            inspect,
        )
        await group.ask(message)
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "completed"
        assert len(requests) == 1


async def test_missed_start_updates_job_without_run_or_io(env, monkeypatch):
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group, interval=3600)
        env.clock.advance(121)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT reason FROM xiaowei_scheduled_job") == "start_time_missed"
        assert await env.scalar("SELECT next_run FROM xiaowei_scheduled_job") == (
            env.clock() + timedelta(seconds=3539)
        )
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_run") == 0
        assert requests == [] and TASK not in env.scripts.calls


@pytest.mark.parametrize(
    "revoked,reason,notice",
    [
        ("notify", "authorization_revoked", "创建者当前权限已撤销"),
        ("query_tools", "authorization_revoked", "创建者当前权限已撤销"),
        ("targets", "authorization_revoked", "创建者当前权限已撤销"),
        ("automatic_tools", "authorization_revoked", "创建者当前权限已撤销"),
        ("recipient_version", "recipient_changed", "机器人接收目标已变化"),
    ],
)
async def test_current_config_revocation_stops_before_model_and_business_post(
    env, monkeypatch, revoked, reason, notice
):
    cfg = agent_config(env)
    async with running(env, monkeypatch, config=cfg) as (group, _requests, _rt):
        await activate(env, group, interval=3600)
    data = cfg.model_dump(mode="json")
    if revoked == "notify":
        data["access"]["grants"][A].remove("notifications/send_webhook_message")
    elif revoked == "query_tools":
        data["feishu"]["group"]["tools"].remove(QUERY)
    elif revoked == "targets":
        data["targets"][0]["starrocks"]["target_id"] = "sr-current"
        data["webhook_bots"][0]["receive_targets"].remove(SR.target_id)
    elif revoked == "automatic_tools":
        data["scheduled_tasks"]["automatic_tools"] = []
    else:
        data["webhook_bots"][0]["target_version"] = "changed-recipient"
    async with running(env, monkeypatch, config=runtime.ServeConfig.model_validate(data)) as (
        group,
        requests,
        rt,
    ):
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "disabled"
        assert requests == [] and TASK not in env.scripts.calls
        assert await env.scalar("SELECT reason FROM xiaowei_scheduled_job") == reason
        body = message_text(group.outbox.sent[-1][1])
        assert "已停止：" + notice in body
        assert "定义依赖当前不可用" not in body


async def test_pause_during_model_prevents_history_commit_and_send(env, monkeypatch):
    import asyncio

    from agents.extensions.memory import SQLAlchemySession

    started, release = asyncio.Event(), asyncio.Event()

    async def finish(call):
        started.set()
        await release.wait()
        return cite()(call)

    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        finish,
    )
    cfg = agent_config(env).model_copy(update={"max_concurrent_turns": 2})
    async with running(env, monkeypatch, config=cfg) as (group, requests, rt):
        job = await activate(env, group, interval=3600)
        env.clock.advance(60)
        tick = asyncio.create_task(rt.scheduled_tasks.tick())
        try:
            await asyncio.wait_for(started.wait(), 5)
            message = "暂停正在调查的任务"
            env.scripts.add(
                attributed(A, message),
                tool_call("scheduled__control_task", job_id=job, operation="pause"),
                cite(),
            )
            await group.ask(message)
            assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "paused"
        finally:
            release.set()
            await tick
        run_session = await env.scalar("SELECT session_id FROM xiaowei_scheduled_run")
        assert await SQLAlchemySession(run_session, engine=rt.engine).get_items() == []
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "failed"
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0
        assert requests == []


@pytest.mark.parametrize("dependency_allowed", [False, True])
async def test_definition_dependencies_obey_current_model_policy_before_background_run(
    env, monkeypatch, dependency_allowed
):
    cfg = agent_config(env)
    task = "刚才查到sales表，请读取销售统计并总结。"
    async with running(env, monkeypatch, config=cfg) as (group, _requests, _rt):
        message = "查一下销售表"
        env.scripts.add(
            attributed(A, message),
            tool_call(
                "list_tables",
                cluster=SR.target_id,
                keyword=".",
                database="shop",
                page_size=5,
                cursor=None,
            ),
            cite(),
        )
        await group.ask(message)
        evidence = await env.scalar("SELECT evidence_id FROM xiaowei_evidence")
        await activate(env, group, interval=3600, task=task)
        ids = json.loads(
            await env.scalar("SELECT definition_evidence_ids FROM xiaowei_scheduled_job")
        )
        assert evidence in ids
    data = cfg.model_dump(mode="json")
    if not dependency_allowed:
        data["data_policy"]["model_tools"].remove("local/list_tables")
    else:
        data["data_policy"]["input"]["max_bytes"] += 100
    env.scripts.add(
        task,
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
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert len(requests) == (1 if dependency_allowed else 0)
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == (
            "active" if dependency_allowed else "disabled"
        )
        if not dependency_allowed:
            assert (
                await env.scalar("SELECT reason FROM xiaowei_scheduled_job")
                == "definition_unavailable"
            )
            assert "已停止：定义依赖当前不可用" in message_text(group.outbox.sent[-1][1])
            assert env.scripts.calls.get(task, []) == []
            assert (
                await env.scalar("SELECT count(*) FROM xiaowei_scheduled_run WHERE state='running'")
                == 0
            )


async def test_runtime_sender_timeout_is_unverified_without_replacing_sender_check(
    env, monkeypatch, caplog
):
    from sqlalchemy import text

    blocked = False

    async def members(chat, subject):
        if blocked:
            raise TimeoutError("private_member_failure")
        return True

    async def start_member(chat, subject):
        return True

    query = tool_call(
        "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
    )
    env.scripts.add(TASK, query, cite(), query, cite())
    async with running(env, monkeypatch, config=agent_config(env), member_check=members) as (
        group,
        requests,
        rt,
    ):
        await activate(env, group, interval=3600)
        object.__setattr__(rt.scheduled_tasks.definitions, "member_status", start_member)
        async with rt.engine.begin() as conn:
            await conn.execute(text("UPDATE xiaowei_scheduled_job SET unverified_count=2"))
        blocked = True
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0
        assert (
            await env.scalar("SELECT reason FROM xiaowei_scheduled_run")
            == "authorization_unverified"
        )
        assert await env.scalar("SELECT unverified_count FROM xiaowei_scheduled_job") == 3
        assert "private_member_failure" not in caplog.text
        blocked = False
        env.clock.advance(3600)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert await env.scalar("SELECT unverified_count FROM xiaowei_scheduled_job") == 0
