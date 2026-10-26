"""H2：真实定时入口/SDK/治理/隔离PG；只替换模型、成员目录与外部I/O。"""

import json
from datetime import timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.test_app import cite, last_output, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_identity import B
from tests.sdk_core.test_operations_history_tasks import configured, members
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_scheduled_agent import TASK, activate
from tests.sdk_core.test_webhook_notification import running

from xiaowei.feishu import attributed

pytestmark = pytest.mark.loopback
CONCLUSION = "凌晨巡检：销售查询已完成；业务是否恢复仍待验证。"


def background(env):
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(CONCLUSION),
    )


@pytest.mark.parametrize("recipient_denied", [False, True])
async def test_new_session_reads_saved_background_conclusion(env, monkeypatch, recipient_denied):
    """缺少保存点或H1未接入快照时，新会话拿不到原调查结论。"""
    background(env)
    cfg = configured(env)
    cfg = cfg.model_copy(
        update={"storage": cfg.storage.model_copy(update={"evidence_retention_seconds": 7200})}
    )
    async with running(env, monkeypatch, config=cfg, member_check=members) as (
        group,
        requests,
        rt,
    ):
        await activate(env, group)
        if recipient_denied:
            # 在通知接收边界注入收窄后的配置，仍由真实validate_answer作拒绝。
            bot = rt.scheduled_tasks.notifications.bot_config("ops_alert_bot")
            monkeypatch.setattr(
                rt.scheduled_tasks.notifications,
                "bot_config",
                lambda _alias: bot.model_copy(update={"receive_tools": frozenset()}),
            )
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == (
            "failed" if recipient_denied else "completed"
        )
        if recipient_denied:
            assert all(CONCLUSION not in request.content.decode() for request in requests)
        run_id = await env.scalar("SELECT run_id FROM xiaowei_scheduled_run")
        await group.ask("/新建")
        env.drv.connections.clear()

        def finish(call):
            data = last_output(call)["data"]
            record = data["records"][0]
            assert record.get("analysis") == CONCLUSION
            assert "回答缺失" not in record["gaps"]
            assert "east" in str(record["facts"])
            return cite("找到了当时结论，当前状态需重新核查。")(call)

        message = "昨天凌晨巡检发现了什么"
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
        assert CONCLUSION in message_text(group.outbox.sent[-1][1])
        # 当前对象权限探测可以访问元数据，但不能重跑原业务查询。
        assert not any(
            "FROM shop.sales" in sql for conn in env.drv.connections for sql, _ in conn.executed
        )
        assert len(requests) == 1
        raw = await env.scalar("SELECT result_snapshot FROM xiaowei_scheduled_run")
        assert CONCLUSION in raw
        assert "session_state" not in json.loads(raw)
        assert await env.scalar("SELECT result_expires_at FROM xiaowei_scheduled_run") == (
            env.clock() + timedelta(seconds=3600)
        )


async def test_snapshot_write_failure_never_sends_result_body(env, monkeypatch):
    """真实PG写入故障必须阻止正文，不能发送后才发现没有留存结果。"""
    background(env)
    async with running(env, monkeypatch, config=configured(env), member_check=members) as (
        group,
        requests,
        rt,
    ):
        await activate(env, group)
        env.clock.advance(60)

        def fail_snapshot(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().startswith("UPDATE xiaowei_scheduled_run SET result_snapshot"):
                raise SQLAlchemyError("synthetic snapshot write failure")

        event.listen(rt.engine.sync_engine, "before_cursor_execute", fail_snapshot)
        try:
            await rt.scheduled_tasks.tick()
        finally:
            event.remove(rt.engine.sync_engine, "before_cursor_execute", fail_snapshot)
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "failed"
        assert len(env.scripts.calls[TASK]) == 2
        assert await env.scalar("SELECT result_snapshot FROM xiaowei_scheduled_run") is None
        for request in requests:
            body = json.loads(request.content)["content"]["text"]
            assert CONCLUSION not in body and "east" not in body


@pytest.mark.parametrize("revoked", ["none", "before_read", "after_copy"])
async def test_snapshot_text_keeps_original_definition_source_checks(env, monkeypatch, revoked):
    """没有新工具结果的分析仍受定义原数据约束；暂停/取消及机器人变化不算数据撤权。"""
    from tests.sdk_core.test_scheduled_tasks import history

    task = "基于已记录的east=100写历史摘要，不声称当前状态。"
    async with running(env, monkeypatch, config=configured(env), member_check=members) as (
        group,
        requests,
        rt,
    ):
        old = await history(env, group)
        env.scripts.add(task, cite(CONCLUSION))
        job = await activate(env, group, task=task)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        run_id = await env.scalar("SELECT run_id FROM xiaowei_scheduled_run")
        await group.ask("/新建")
        async with rt.engine.begin() as conn:
            await conn.execute(
                text("UPDATE xiaowei_scheduled_job SET state='cancelled' WHERE job_id=:id"),
                {"id": job},
            )
            if revoked == "before_read":
                await conn.execute(
                    text(
                        "UPDATE xiaowei_evidence SET policy_fingerprint='invalid' "
                        "WHERE evidence_id=:id"
                    ),
                    {"id": old},
                )
        rt.scheduled_tasks.definitions.bots.clear()
        env.drv.connections.clear()

        async def finish(call):
            data = last_output(call)["data"]
            if revoked == "before_read":
                assert all("analysis" not in record for record in data["records"])
                assert data["gaps"]["Evidence 不可读"] is True
            else:
                assert data["records"][0]["analysis"] == CONCLUSION
            if revoked == "after_copy":
                async with rt.engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE xiaowei_evidence SET policy_fingerprint='invalid' "
                            "WHERE evidence_id=:id"
                        ),
                        {"id": old},
                    )
                env.drv.connections.clear()
            return cite("仅交付当前仍可核验的历史材料。")(call)

        message = "找回上次巡检的分析"
        env.scripts.add(
            attributed(B, message),
            tool_call("history__read_operation", record_ref=f"run:{run_id}"),
            finish,
        )
        await group.ask(message, sender=B)
        state = await env.scalar(
            "SELECT state FROM xiaowei_request WHERE subject_id=:actor", {"actor": B}
        )
        assert state == ("failed" if revoked == "after_copy" else "completed")
        visible = message_text(group.outbox.sent[-1][1])
        assert (CONCLUSION in visible) == (revoked == "none")
        assert len(requests) == 1
        if revoked != "none":
            assert env.drv.connections == []


@pytest.mark.parametrize("revoked_at", ["before_commit", "before_delivery"])
async def test_text_snapshot_rechecks_definition_source_after_model(env, monkeypatch, revoked_at):
    """无新Evidence时，模型期间或发送前的原源撤权也须阻断相应保存/正文。"""
    from tests.sdk_core.test_scheduled_tasks import history

    task = "只用定义中已记录的数据写结论。"
    async with running(env, monkeypatch, config=configured(env), member_check=members) as (
        group,
        requests,
        rt,
    ):
        old = await history(env, group)

        async def revoke():
            async with rt.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE xiaowei_evidence SET policy_fingerprint='invalid' "
                        "WHERE evidence_id=:id"
                    ),
                    {"id": old},
                )
            env.drv.connections.clear()

        async def finish(call):
            if revoked_at == "before_commit":
                await revoke()
            return cite(CONCLUSION)(call)

        env.scripts.add(task, finish)
        await activate(env, group, task=task)
        if revoked_at == "before_delivery":
            save = rt.scheduled_tasks._save_agent_result

            async def save_then_revoke(*args):
                await save(*args)
                await revoke()

            monkeypatch.setattr(rt.scheduled_tasks, "_save_agent_result", save_then_revoke)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        saved = await env.scalar("SELECT result_snapshot FROM xiaowei_scheduled_run")
        assert (saved is None) == (revoked_at == "before_commit")
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "failed"
        if revoked_at == "before_commit":
            assert await env.scalar(
                "SELECT count(*) FROM agent_messages WHERE session_id IN "
                "(SELECT session_id FROM xiaowei_scheduled_run)"
            ) == 0
        assert all(CONCLUSION not in request.content.decode() for request in requests)
        assert env.drv.connections == []


async def test_definition_expiry_bounds_text_only_snapshot_and_cleanup(env, monkeypatch):
    """纯文本结果的Delivery期限为空时，定义原数据仍限制快照，清理仅删除正文。"""
    from tests.sdk_core.test_scheduled_tasks import history

    from xiaowei.session import cleanup_expired

    task = "基于已记录的数据写摘要，不查询当前状态。"
    async with running(env, monkeypatch, config=configured(env), member_check=members) as (
        group,
        _requests,
        rt,
    ):
        old = await history(env, group)
        expires = env.clock() + timedelta(seconds=120)
        async with rt.engine.begin() as conn:
            await conn.execute(
                text("UPDATE xiaowei_evidence SET expires_at=:expires WHERE evidence_id=:id"),
                {"expires": expires, "id": old},
            )
        env.scripts.add(task, cite(CONCLUSION))
        await activate(env, group, task=task)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        run_id = await env.scalar("SELECT run_id FROM xiaowei_scheduled_run")
        assert await env.scalar("SELECT result_expires_at FROM xiaowei_scheduled_run") == expires
        assert (
            json.loads(await env.scalar("SELECT result_snapshot FROM xiaowei_scheduled_run"))[
                "context_evidence"
            ]
            == []
        )
        await group.ask("/新建")
        env.clock.advance(61)
        env.drv.connections.clear()

        def finish(call):
            data = last_output(call)["data"]
            assert data["records"] == []
            assert data["gaps"]["Evidence 不可读"] is True
            assert "回答缺失" not in data["gaps"]
            return cite("原数据已过期，本次不能读取当时结论。")(call)

        message = "读取已过期的巡检结论"
        env.scripts.add(
            attributed(B, message),
            tool_call("history__read_operation", record_ref=f"run:{run_id}"),
            finish,
        )
        await group.ask(message, sender=B)
        assert env.drv.connections == []
        assert CONCLUSION not in message_text(group.outbox.sent[-1][1])
        await cleanup_expired(rt.engine, now=env.clock(), batch_size=1)
        assert await env.scalar("SELECT result_snapshot FROM xiaowei_scheduled_run") is None
        assert await env.scalar("SELECT count(*) FROM xiaowei_scheduled_run") == 1


async def test_snapshot_and_definition_share_one_object_check_batch(env, monkeypatch):
    """同一对象的运行证据和定义证据分开计费，会让一次历史读取必然超过5倍规则。"""
    from tests.sdk_core.test_operations_history import limited
    from tests.sdk_core.test_scheduled_tasks import history

    cfg = limited(configured(env), rows=1, checks=1000)
    background(env)
    async with running(env, monkeypatch, config=cfg, member_check=members) as (group, _, rt):
        await history(env, group)
        await activate(env, group)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "completed"
        run_id = await env.scalar("SELECT run_id FROM xiaowei_scheduled_run")
        await group.ask("/新建")

    small = limited(cfg, rows=1, checks=5)
    async with running(env, monkeypatch, config=small, member_check=members) as (group, _, _rt):

        def finish(call):
            assert last_output(call)["data"]["records"][0]["analysis"] == CONCLUSION
            return cite("已找回结论，未重新执行原查询。")(call)

        message = "满额对象预算下读取原巡检结论"
        env.scripts.add(
            attributed(B, message),
            tool_call("history__read_operation", record_ref=f"run:{run_id}"),
            finish,
        )
        await group.ask(message, sender=B, message_id="om_h2_budget_read")
        assert (
            await env.scalar(
                "SELECT state FROM xiaowei_request WHERE subject_id=:actor", {"actor": B}
            )
            == "completed"
        )
        assert CONCLUSION in message_text(group.outbox.sent[-1][1])


async def test_definition_objects_count_toward_history_object_limit(env, monkeypatch):
    """只数运行对象会越过对象上限；省略分析不能误报原回答不存在。"""
    from tests.sdk_core.test_operations_history import limited
    from tests.sdk_core.test_scheduled_tasks import history

    cfg = limited(configured(env), rows=1, checks=1000)
    async with running(env, monkeypatch, config=cfg, member_check=members) as (group, _, rt):
        await history(env, group)  # 定义依赖shop.sales。
        env.scripts.add(
            TASK,
            tool_call("run_readonly_query", cluster=SR.target_id, sql="SELECT * FROM shop.regions"),
            cite(CONCLUSION),
        )
        await activate(env, group)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        run_id = await env.scalar("SELECT run_id FROM xiaowei_scheduled_run")
        await group.ask("/新建")

        def finish(call):
            data = last_output(call)["data"]
            assert "analysis" not in data["records"][0]
            assert "shop.regions" in str(data["records"][0]["facts"])
            assert data["gaps"]["truncated"] is True
            assert "回答缺失" not in data["gaps"]
            return cite("只返回本轮仍可读事实，分析因对象上限省略。")(call)

        message = "读取跨对象巡检结论"
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


async def test_oversized_snapshot_fails_before_sending_result(env, monkeypatch):
    """后台结果同样受已有max_answer_bytes限制，不能无界写入或仅发送不留存。"""
    cfg = configured(env)
    cfg = cfg.model_copy(
        update={"storage": cfg.storage.model_copy(update={"max_answer_bytes": 4000})}
    )
    env.scripts.add(
        TASK,
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(CONCLUSION + "x" * 6000),
    )
    async with running(env, monkeypatch, config=cfg, member_check=members) as (group, requests, rt):
        await activate(env, group)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "failed"
        assert await env.scalar("SELECT result_snapshot FROM xiaowei_scheduled_run") is None
        assert len(env.scripts.calls[TASK]) == 2
        assert all(CONCLUSION not in request.content.decode() for request in requests)


async def test_new_evidence_expiry_is_carried_from_session_validation(env, monkeypatch):
    """Session校验已经取得短期证据期限，保存点不能丢掉它后改用更长请求期限。"""
    background(env)
    async with running(env, monkeypatch, config=configured(env), member_check=members) as (
        group,
        requests,
        rt,
    ):
        expires = env.clock() + timedelta(seconds=120)
        await activate(env, group, interval=3600, expires_at=expires)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "completed"
        assert await env.scalar("SELECT result_expires_at FROM xiaowei_scheduled_run") == expires
        assert (
            await env.scalar(
                "SELECT expires_at FROM xiaowei_evidence WHERE turn_id IN "
                "(SELECT turn_id FROM xiaowei_scheduled_run)"
            )
            == expires
        )
        assert len(requests) == 1
