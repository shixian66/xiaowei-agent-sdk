"""H1群结论共享、旧后台事实与两个content hook；只使用本机隔离库/替身。"""

import pytest
from sqlalchemy import text
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.test_app import cite, last_output, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_identity import CHAT, A, B
from tests.sdk_core.test_operations_history import HISTORY_TOOLS, OLD_ANALYSIS, ask
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_scheduled_agent import TASK, activate, agent_config
from tests.sdk_core.test_webhook_notification import running

from xiaowei import runtime
from xiaowei.feishu import attributed

pytestmark = pytest.mark.loopback


def configured(env):
    data = agent_config(env).model_dump(mode="json")
    data["data_policy"]["model_tools"].extend(sorted(HISTORY_TOOLS))
    data["access"]["grants"][data["web"]["operator_id"]].extend(sorted(HISTORY_TOOLS))
    data["access"]["grants"][A].extend(sorted(HISTORY_TOOLS))
    data["feishu"]["group"]["tools"].extend(sorted(HISTORY_TOOLS))
    data["webhook_bots"][0]["receive_targets"].append("history")
    data["webhook_bots"][0]["receive_tools"].extend(sorted(HISTORY_TOOLS))
    return runtime.ServeConfig.model_validate(data)


async def members(chat, subject):
    return chat == CHAT and subject in {A, B}


async def original(env, group):
    message = "记录本群调查"
    env.scripts.add(
        attributed(A, message),
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        cite(OLD_ANALYSIS),
    )
    await group.ask(message, message_id="om_history_original")
    turn_id = await env.scalar("SELECT turn_id FROM xiaowei_request WHERE state='completed'")
    await group.ask("/新建")
    return turn_id


async def test_current_group_member_reads_another_requesters_conclusion(env, monkeypatch):
    async with running(env, monkeypatch, config=configured(env), member_check=members) as (
        group,
        requests,
        _rt,
    ):
        turn_id = await original(env, group)

        def finish(call):
            assert OLD_ANALYSIS in last_output(call)["data"]["records"][0]["analysis"]
            return cite("其他成员也按当前权限读取")(call)

        message = "查询其他成员的上次结论"
        env.scripts.add(
            attributed(B, message),
            tool_call("history__read_operation", record_ref=f"turn:{turn_id}"),
            finish,
        )
        await group.ask(message, sender=B)
        assert (
            await env.scalar(
                "SELECT state FROM xiaowei_request WHERE subject_id=:actor", {"actor": B}
            )
            == "completed"
        )
        assert "east" in message_text(group.outbox.sent[-1][1])
        assert requests == []


@pytest.mark.parametrize("state", ["paused", "cancelled"])
async def test_old_background_facts_survive_task_and_bot_lifecycle_changes(env, monkeypatch, state):
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        cite("后台原分析未保存"),
    )
    async with running(env, monkeypatch, config=configured(env), member_check=members) as (
        group,
        requests,
        rt,
    ):
        job = await activate(env, group, interval=3600)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "completed"
        run_id = await env.scalar("SELECT run_id FROM xiaowei_scheduled_run")
        await group.ask("/新建")
        async with rt.engine.begin() as conn:
            await conn.execute(
                text("UPDATE xiaowei_scheduled_job SET state=:state WHERE job_id=:id"),
                {"state": state, "id": job},
            )
            # 模拟升级前运行：没有快照仍可读原事实，不从SDK消息重建结论。
            await conn.execute(text(
                "UPDATE xiaowei_scheduled_run SET result_snapshot=NULL,result_expires_at=NULL"
            ))
        rt.scheduled_tasks.definitions.bots.clear()

        def finish(call):
            data = last_output(call)["data"]
            assert "east" in str(data["records"][0]["facts"])
            assert "analysis" not in data["records"][0]
            assert data["records"][0]["gaps"]["回答缺失"] is True
            return cite("只找到当时事实，后台结论未保存")(call)

        message = "查上次巡检事实"
        env.scripts.add(
            attributed(B, message),
            tool_call("history__read_operation", record_ref=f"run:{run_id}"),
            finish,
        )
        await group.ask(message, sender=B)
        assert (
            await env.scalar(
                "SELECT state FROM xiaowei_request WHERE subject_id=:actor", {"actor": B}
            )
            == "completed"
        )
        assert "east" in message_text(group.outbox.sent[-1][1])
        assert len(requests) == 1
        if state == "paused":
            # 通知群/其他入口不因此拥有操作群的历史；固定范围说明不判断提问意图。
            for channel, subject, conversation in (
                ("web", configured(env).web.operator_id, "browser"),
                ("feishu", A, "oc_private"),
            ):

                def outside(call):
                    data = last_output(call)["data"]
                    assert data["records"] == [] and "创建任务的操作群" in data["notice"]
                    return cite("当前入口没有可读记录")(call)

                message = "另一个入口" + channel
                env.scripts.add(
                    message,
                    tool_call("history__read_operation", record_ref=f"run:{run_id}"),
                    outside,
                )
                env.drv.connections.clear()
                result = await ask(
                    env,
                    rt,
                    message,
                    "outside-" + channel,
                    channel=channel,
                    subject=subject,
                    conversation=conversation,
                )
                assert result.state == "completed", result.failure_code
                assert env.drv.connections == []


@pytest.mark.parametrize("change", ["none", "definition", "history"])
async def test_existing_definition_and_history_content_checks_both_apply_in_one_turn(
    env, monkeypatch, change
):
    async with running(env, monkeypatch, config=configured(env), member_check=members) as (
        group,
        requests,
        rt,
    ):
        turn_id = await original(env, group)

        async def finish(call):
            assert "action_id" in last_output(call)["data"]
            if change == "definition":
                rt.scheduled_tasks.definitions.bots.clear()
            elif change == "history":
                async with rt.engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE xiaowei_evidence SET policy_fingerprint='invalid' "
                            "WHERE turn_id=:turn"
                        ),
                        {"turn": turn_id},
                    )
            return cite("只是提出建议，尚未执行提醒")(call)

        from datetime import timedelta

        message = "按历史材料提出提醒"
        env.scripts.add(
            attributed(A, message),
            tool_call("history__read_operation", record_ref=f"turn:{turn_id}"),
            tool_call(
                "scheduled__propose_reminder",
                bot_alias="ops_alert_bot",
                text="请复核",
                evidence_ids=[],
                at=(env.clock() + timedelta(seconds=60)).isoformat(),
            ),
            finish,
        )
        await group.ask(message)
        expected = "completed" if change == "none" else "failed"
        assert (
            await env.scalar(
                "SELECT state FROM xiaowei_request WHERE turn_id<>:old "
                "ORDER BY created_at DESC LIMIT 1",
                {"old": turn_id},
            )
            == expected
        )
        assert requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0
