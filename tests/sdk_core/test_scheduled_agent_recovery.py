"""A：正式 SDK/治理/隔离 PG；Vertex、StarRocks 与发送接口均为替身。"""

import json
from typing import get_args

import pytest
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.test_app import MODEL_SECRET, tool_call, upstream_error
from tests.sdk_core.test_gate0 import VertexLikeEndpoint, vertex_call
from tests.sdk_core.test_runtime import _vertex_cite_seen
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_scheduled_agent import TASK, activate, agent_config
from tests.sdk_core.test_vertex_model import VERTEX
from tests.sdk_core.test_webhook_notification import running

from xiaowei import runtime
from xiaowei.app import TurnError, TurnReason
from xiaowei.image_input import SessionImageStore

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize("reason", get_args(TurnReason))
def test_every_turn_reason_has_a_default_message(reason):
    error = TurnError(reason)
    assert error.reason == reason
    assert str(error).strip()


async def test_scheduled_text_task_with_real_image_profile_uses_runner(env, monkeypatch):
    config = agent_config(env)
    async with running(env, monkeypatch, config=config) as (group, _, _):
        await activate(env, group)
    data = config.model_dump(mode="json")
    data["model"] = VERTEX.model_copy(
        update={
            "image_input_enabled": True,
            "data_policy_id": config.model.data_policy_id,
            "max_request_bytes": 256_000,
        }
    ).model_dump(mode="json")
    data["feishu"]["image_input_enabled"] = True
    data["storage"]["request_retention_seconds"] = 7200
    data["storage"]["evidence_retention_seconds"] = 7200
    monkeypatch.setenv("XIAOWEI_TEST_VERTEX_KEY", "synthetic")
    endpoint = VertexLikeEndpoint(
        replies={
            TASK: [
                vertex_call(
                    "run_readonly_query",
                    cluster=SR.target_id,
                    sql="SELECT region,total FROM shop.sales",
                ),
                _vertex_cite_seen,
            ]
        }
    )
    config = runtime.ServeConfig.model_validate(data)

    async def download(*args, **kwargs):
        raise AssertionError("定时文本任务不应下载用户图片")

    async with running(
        env,
        monkeypatch,
        config=config,
        model_transport=endpoint.transport(),
        download_resource=download,
    ) as (_, requests, rt):
        assert isinstance(rt.scheduled_tasks.application.images, SessionImageStore)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_run") == "completed"
        assert len(requests) == 1
        assert "east" in json.loads(requests[0].content)["content"]["text"]
        assert not endpoint.replies[TASK]


@pytest.mark.parametrize("after_query", [False, True])
async def test_each_failed_occurrence_sends_one_safe_status(env, monkeypatch, caplog, after_query):
    query = tool_call(
        "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
    )
    steps = [query, upstream_error] if after_query else [upstream_error]
    env.scripts.add(TASK, *steps, *steps)
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group, interval=3600)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        env.clock.advance(3600)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 2
        assert (
            await env.scalar(
                "SELECT count(*) FROM xiaowei_scheduled_run "
                "WHERE state='failed' AND reason='model_failed'"
            )
            == 2
        )
        assert await env.scalar("SELECT state FROM xiaowei_scheduled_job") == "active"
        for request in requests:
            text = json.loads(request.content)["content"]["text"]
            assert "未完成" in text and "model_failed" in text
            assert "任务获准目标" in text and SR.target_id in text
            assert "实际查询时间窗：未取得" in text
            assert MODEL_SECRET not in text and "SELECT" not in text
        assert "stage=run_turn" in caplog.text
        assert "reason=model_failed" in caplog.text
        assert MODEL_SECRET not in caplog.text


@pytest.mark.parametrize(
    "error,reason", [(RuntimeError, "execution_failed"), (TimeoutError, "timeout")]
)
async def test_prepare_failure_reports_zero_model_requests(env, monkeypatch, caplog, error, reason):
    async with running(env, monkeypatch, config=agent_config(env)) as (group, requests, rt):
        await activate(env, group)

        async def fail_prepare(*args, **kwargs):
            raise error("private_prepare_sql_sentinel")

        monkeypatch.setattr(rt.scheduled_tasks.application, "prepare_tools", fail_prepare)
        env.clock.advance(60)
        await rt.scheduled_tasks.tick()
        assert len(requests) == 1
        assert TASK not in env.scripts.calls
        assert await env.scalar("SELECT reason FROM xiaowei_scheduled_run") == reason
        assert "stage=prepare_tools" in caplog.text
        assert "model_requests=0" in caplog.text
        assert "private_prepare_sql_sentinel" not in caplog.text
        assert "private_prepare_sql_sentinel" not in requests[0].content.decode()


@pytest.mark.parametrize("fault", ["revoked", "locked", "cancelled"])
async def test_failure_status_never_bypasses_current_sender_or_instance(env, monkeypatch, fault):
    import asyncio

    allowed = True

    async def member(chat, subject):
        return allowed

    async with running(env, monkeypatch, config=agent_config(env), member_check=member) as (
        group,
        requests,
        rt,
    ):
        await activate(env, group)

        def fail(call):
            nonlocal allowed
            if fault == "revoked":
                allowed = False
            elif fault == "locked":
                rt.readiness.lock("synthetic_failure")
            else:
                raise asyncio.CancelledError
            return upstream_error(call)

        env.scripts.add(TASK, fail)
        env.clock.advance(60)
        if fault == "locked":
            with pytest.raises(TurnError):
                await rt.scheduled_tasks.tick()
        else:
            await rt.scheduled_tasks.tick()
        assert requests == []
        assert await env.scalar("SELECT count(*) FROM xiaowei_notification_attempt") == 0


@pytest.mark.parametrize("after_send", [False, True])
async def test_settlement_failure_never_sends_an_additional_status(env, monkeypatch, after_send):
    from tests.sdk_core.test_app import cite

    from xiaowei.scheduled_tasks import ScheduledTasks
    from xiaowei.storage import StorageUnavailableError

    unverified = False

    async def member(chat, subject):
        if unverified:
            raise TimeoutError("synthetic_member_unverified")
        return True

    async with running(env, monkeypatch, config=agent_config(env), member_check=member) as (
        group,
        requests,
        rt,
    ):
        await activate(env, group)

        async def fail_settle(self, *args, **kwargs):
            rt.readiness.lock("synthetic_settlement_failure")
            raise StorageUnavailableError

        monkeypatch.setattr(ScheduledTasks, "_finish_agent", fail_settle)
        if after_send:
            env.scripts.add(TASK, cite("调查完成。"))
        else:
            unverified = True
        env.clock.advance(60)
        with pytest.raises(StorageUnavailableError):
            await rt.scheduled_tasks.tick()
        assert len(requests) == int(after_send)
        assert not rt.readiness.ok
