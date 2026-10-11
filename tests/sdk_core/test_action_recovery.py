"""审批局部中断：真实存储和治理下保存事实，继续读取，不重放写入。"""

import asyncio

import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from tests.sdk_core.test_action_lifecycle import propose, takeover
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_feishu_render import message_text
from tests.sdk_core.test_group_actions import Actions
from tests.sdk_core.test_group_actions import actions as actions  # pytest fixture
from tests.sdk_core.test_group_gateway import running
from tests.sdk_core.test_group_identity import A

from xiaowei.channel import ChannelService, ResultDelivery
from xiaowei.channel_store import ChannelStoreUnavailableError, NotReadyError
from xiaowei.feishu import attributed

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize(
    "stage,state,writes,code",
    [
        ("preclaim", "pending", 0, "action_not_approvable"),
        ("read", "rejected", 0, "action_rejected"),
        ("write", "unknown", 1, "action_unknown"),
        ("feedback", "succeeded", 1, "action_feedback_failed"),
    ],
)
async def test_cancelled_approval_saves_facts_and_keeps_queries_available(
    actions: Actions,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    state: str,
    writes: int,
    code: str,
) -> None:
    action_id = await propose(actions)
    env, adapter = actions.env, actions.adapter
    entered, release = asyncio.Event(), asyncio.Event()
    if stage == "read":
        adapter.read_entered, adapter.read_release = entered, release
    elif stage == "write":
        adapter.entered, adapter.release = entered, release
    elif stage == "feedback":
        assert env.app.actions is not None
        status = env.app.actions.status

        async def paused_status(ctx, request):
            entered.set()
            await release.wait()
            return await status(ctx, request)

        monkeypatch.setattr(env.app.actions, "status", paused_status)
    else:
        validate = env.evidence.validate_answer

        async def paused_validation(turn, ctx, *, history=False):
            entered.set()
            await release.wait()
            return await validate(turn, ctx, history=history)

        monkeypatch.setattr(env.evidence, "validate_answer", paused_validation)
    history = await env.rows("SELECT message_data FROM agent_messages ORDER BY id")
    receipt = await env.service.accept(env.group(attributed(A, f"/批准 {action_id}"), "om_a"))
    task = asyncio.create_task(env.service.approve(receipt, action_id))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)

    assert env.store.readiness.ok
    assert len(adapter.calls) == writes
    assert await env.rows("SELECT state FROM xiaowei_action") == [(state,)]
    assert await env.rows(
        "SELECT state, failure_code, answer FROM xiaowei_request WHERE reply_message_id='om_a'"
    ) == [("failed", code, None)]
    assert await env.rows("SELECT message_data FROM agent_messages ORDER BY id") == history
    # 解除仅测试使用的读暂停；产品没有补跑已中断的调用。
    release.set()
    asked = env.scripts.add(
        attributed(A, "继续查订单"), tool_call("order_total", region="east"), cite()
    )
    async with running(env, max_reply_chars=3500) as group:
        # 同一平台消息重投只交付保存的失败回执，不重新批准或调用模型。
        await group.ask(f"/批准 {action_id}", message_id="om_a")
        receipt_text = message_text(group.outbox.sent[-1][1])
        assert "工具执行失败" not in receipt_text
        if stage == "write":
            assert "人工核对远端" in receipt_text and "不会自动重试" in receipt_text
        elif stage == "feedback":
            assert "动作已执行" in receipt_text and "不要重复批准" in receipt_text
        else:
            assert "未执行" in receipt_text
        await group.ask("继续查订单", message_id="om_read")
        if stage != "preclaim":
            await group.ask(f"/批准 {action_id}", message_id="om_again")
    assert env.model_calls(asked) == 2
    assert len(env.adapter.calls) == 1
    assert len(adapter.calls) == writes
    assert await env.rows(
        "SELECT state, delivery FROM xiaowei_request WHERE reply_message_id='om_read'"
    ) == [("completed", "sent")]


@pytest.mark.parametrize("storage", ["action", "request"])
async def test_cancel_settlement_storage_failure_still_stops_the_instance(
    actions: Actions, storage: str
) -> None:
    action_id = await propose(actions)
    env, adapter = actions.env, actions.adapter
    adapter.read_entered, adapter.read_release = asyncio.Event(), asyncio.Event()
    receipt = await env.service.accept(env.group(attributed(A, f"/批准 {action_id}"), "om_a"))
    task = asyncio.create_task(env.service.approve(receipt, action_id))
    await asyncio.wait_for(adapter.read_entered.wait(), 5)

    def fail_sql(conn, cursor, statement, parameters, context, executemany):
        prefix = (
            "UPDATE xiaowei_action SET state" if storage == "action" else "UPDATE xiaowei_request"
        )
        if " ".join(statement.split()).startswith(prefix):
            raise SQLAlchemyError("synthetic state storage failure")

    event.listen(env.engine.sync_engine, "before_cursor_execute", fail_sql)
    try:
        task.cancel()
        with pytest.raises(ChannelStoreUnavailableError):
            await asyncio.wait_for(task, 5)
    finally:
        event.remove(env.engine.sync_engine, "before_cursor_execute", fail_sql)
    assert not env.store.readiness.ok
    assert adapter.calls == []
    assert await env.rows("SELECT state FROM xiaowei_action") == [
        ("executing" if storage == "action" else "rejected",)
    ]
    assert await env.rows("SELECT state FROM xiaowei_request WHERE reply_message_id='om_a'") == [
        ("running",)
    ]
    with pytest.raises(NotReadyError):
        await env.service.accept(env.group(attributed(A, "继续查订单"), "om_next"))


@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
async def test_unsettled_action_cannot_release_the_instance(
    actions: Actions, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    from xiaowei import actions as action_module

    action_id = await propose(actions)
    env, adapter = actions.env, actions.adapter
    adapter.read_entered, adapter.read_release = asyncio.Event(), asyncio.Event()
    cleanup_entered = asyncio.Event()
    never = asyncio.Event()

    async def stalled_finish(action, state, result=None):
        cleanup_entered.set()
        await never.wait()

    # 延迟位于关键 SQL 之前：验证收尾本身的期限和完整性，不能只靠 SQL 错误处理。
    monkeypatch.setattr(env.store, "finish_action", stalled_finish)
    monkeypatch.setattr(action_module, "ACTION_SETTLE_TIMEOUT_SECONDS", 0.1)
    receipt = await env.service.accept(env.group(attributed(A, f"/批准 {action_id}"), "om_a"))
    task = asyncio.create_task(env.service.approve(receipt, action_id))
    await asyncio.wait_for(adapter.read_entered.wait(), 5)
    task.cancel()
    await asyncio.wait_for(cleanup_entered.wait(), 5)
    if interruption == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    else:
        await asyncio.wait_for(task, 2)
    assert not env.store.readiness.ok
    assert adapter.calls == []
    assert await env.rows("SELECT state FROM xiaowei_action") == [("executing",)]
    with pytest.raises(NotReadyError):
        await env.service.accept(env.group(attributed(A, "继续查订单"), "om_next"))


async def test_timeout_state_read_is_bounded_and_does_not_prevent_local_failure_receipt(
    actions: Actions, monkeypatch: pytest.MonkeyPatch
) -> None:
    from xiaowei import channel as channel_module

    action_id = await propose(actions)
    env = actions.env
    assert env.app.actions is not None
    env.results = ResultDelivery(
        env.store,
        env.evidence,
        env.access,
        budget=env.results.budget.model_copy(update={"timeout_seconds": 1}),
    )
    env.service = ChannelService(env.app, env.results)
    status, get_action = env.app.actions.status, env.store.get_action
    release = asyncio.Event()
    reads = 0

    async def slow_status(ctx, request):
        await asyncio.sleep(2)
        return await status(ctx, request)

    async def stalled_read(action_id, identity):
        nonlocal reads
        reads += 1
        if reads == 2:
            await release.wait()
        return await get_action(action_id, identity)

    monkeypatch.setattr(env.app.actions, "status", slow_status)
    monkeypatch.setattr(env.store, "get_action", stalled_read)
    monkeypatch.setattr(channel_module, "ACTION_SETTLE_TIMEOUT_SECONDS", 0.1)
    receipt = await env.service.accept(env.group(attributed(A, f"/批准 {action_id}"), "om_a"))
    task = asyncio.create_task(env.service.approve(receipt, action_id))
    try:
        record = await asyncio.wait_for(asyncio.shield(task), 2)
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    assert env.store.readiness.ok
    assert record.state == "failed" and record.failure_code == "action_unknown"
    assert len(actions.adapter.calls) == 1
    assert await env.rows("SELECT state FROM xiaowei_action") == [("succeeded",)]


@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
async def test_unsettled_request_cannot_release_the_instance(
    actions: Actions, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    from xiaowei import channel as channel_module

    action_id = await propose(actions)
    env, adapter = actions.env, actions.adapter
    adapter.read_entered, adapter.read_release = asyncio.Event(), asyncio.Event()
    cleanup_entered, never = asyncio.Event(), asyncio.Event()

    async def stalled_fail(record, code):
        cleanup_entered.set()
        await never.wait()

    monkeypatch.setattr(env.store, "fail", stalled_fail)
    monkeypatch.setattr(channel_module, "ACTION_SETTLE_TIMEOUT_SECONDS", 0.1)
    receipt = await env.service.accept(env.group(attributed(A, f"/批准 {action_id}"), "om_a"))
    task = asyncio.create_task(env.service.approve(receipt, action_id))
    await asyncio.wait_for(adapter.read_entered.wait(), 5)
    task.cancel()
    await asyncio.wait_for(cleanup_entered.wait(), 5)
    if interruption == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    else:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, 2)
    assert not env.store.readiness.ok
    assert adapter.calls == []
    assert await env.rows("SELECT state FROM xiaowei_action") == [("rejected",)]
    assert await env.rows("SELECT state FROM xiaowei_request WHERE reply_message_id='om_a'") == [
        ("running",)
    ]
    with pytest.raises(NotReadyError):
        await env.service.accept(env.group(attributed(A, "继续查订单"), "om_next"))


async def test_cancel_after_instance_takeover_cannot_settle_or_write(actions: Actions) -> None:
    action_id = await propose(actions)
    env, adapter = actions.env, actions.adapter
    adapter.read_entered, adapter.read_release = asyncio.Event(), asyncio.Event()
    receipt = await env.service.accept(env.group(attributed(A, f"/批准 {action_id}"), "om_a"))
    task = asyncio.create_task(env.service.approve(receipt, action_id))
    await asyncio.wait_for(adapter.read_entered.wait(), 5)
    async with takeover(actions) as new:
        assert not env.store.readiness.ok  # 实际终止持锁后端，旧实例已被终止通知锁低。
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not env.store.readiness.ok
        assert adapter.calls == []
        assert await new.rows("SELECT state, result FROM xiaowei_action") == [("unknown", None)]
        assert await new.rows(
            "SELECT state FROM xiaowei_request WHERE reply_message_id='om_a'"
        ) == [("interrupted",)]


async def test_remote_finishing_after_client_cancellation_does_not_replay_or_claim_success(
    actions: Actions,
) -> None:
    action_id = await propose(actions)
    env, adapter = actions.env, actions.adapter
    entered, remote_release, remote_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def remote_write():
        await remote_release.wait()
        remote_finished.set()

    # 合成远端拥有独立执行生命周期，不能把客户端取消当作远端撤销。
    remote_task = asyncio.create_task(remote_write())

    async def execute(request):
        adapter.calls.append(request)
        entered.set()
        await asyncio.shield(remote_task)
        raise AssertionError("cancelled client must not resume or manufacture a result")

    actions.rebind(execute=execute)
    receipt = await env.service.accept(env.group(attributed(A, f"/批准 {action_id}"), "om_a"))
    task = asyncio.create_task(env.service.approve(receipt, action_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert env.store.readiness.ok
        assert not remote_finished.is_set()
        assert await env.rows("SELECT state, result FROM xiaowei_action") == [("unknown", None)]
        env.scripts.add(
            attributed(A, "继续查订单"), tool_call("order_total", region="east"), cite()
        )
        async with running(env, max_reply_chars=3500) as group:
            await group.ask("继续查订单", message_id="om_read")
            assert len(env.adapter.calls) == 1
            remote_release.set()
            await asyncio.wait_for(remote_task, 5)
            await group.ask(f"/批准 {action_id}", message_id="om_again")
        assert remote_finished.is_set()
        assert len(adapter.calls) == 1
        assert await env.rows("SELECT state, result FROM xiaowei_action") == [("unknown", None)]
    finally:
        remote_release.set()
        await asyncio.wait_for(remote_task, 5)


@pytest.mark.parametrize(
    "stage,state,writes,code",
    [
        ("preclaim", "pending", 0, "action_not_approvable"),
        ("read", "rejected", 0, "action_rejected"),
        ("write", "unknown", 1, "action_unknown"),
        ("feedback", "succeeded", 1, "action_feedback_failed"),
    ],
)
async def test_approval_deadline_keeps_group_queue_and_reading_available(
    actions: Actions,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    state: str,
    writes: int,
    code: str,
) -> None:
    action_id = await propose(actions)
    env, adapter = actions.env, actions.adapter
    env.results = ResultDelivery(
        env.store,
        env.evidence,
        env.access,
        budget=env.results.budget.model_copy(update={"timeout_seconds": 1}),
    )
    env.service = ChannelService(env.app, env.results)
    if stage == "preclaim":
        get_action = env.store.get_action
        paused = False

        async def pause_once(action_id, identity):
            nonlocal paused
            if not paused:
                paused = True
                await asyncio.Event().wait()
            return await get_action(action_id, identity)

        monkeypatch.setattr(env.store, "get_action", pause_once)
    elif stage == "feedback":
        assert env.app.actions is not None

        async def pause_feedback(ctx, request):
            await asyncio.Event().wait()

        monkeypatch.setattr(env.app.actions, "status", pause_feedback)
    elif stage == "read":
        adapter.read_release = asyncio.Event()
    else:
        adapter.release = asyncio.Event()
    asked = env.scripts.add(
        attributed(A, "继续查订单"), tool_call("order_total", region="east"), cite()
    )
    async with running(env, max_reply_chars=3500) as group:
        await asyncio.wait_for(group.ask(f"/批准 {action_id}", message_id="om_a"), 5)
        assert env.store.readiness.ok
        assert await env.rows("SELECT state FROM xiaowei_action") == [(state,)]
        assert await env.rows(
            "SELECT state, failure_code, delivery FROM xiaowei_request "
            "WHERE reply_message_id='om_a'"
        ) == [("failed", code, "sent")]
        assert "工具执行失败" not in message_text(group.outbox.sent[-1][1])
        await group.ask("继续查订单", message_id="om_read")
        # 同一批准事件重投只重发固定回执，不重新占用或执行。
        await group.ask(f"/批准 {action_id}", message_id="om_a")
        if stage != "preclaim":
            await group.ask(f"/批准 {action_id}", message_id="om_again")
    assert env.model_calls(asked) == 2
    assert len(adapter.calls) == writes
    assert len(env.adapter.calls) == 1
    assert await env.rows(
        "SELECT state, delivery FROM xiaowei_request WHERE reply_message_id='om_read'"
    ) == [("completed", "sent")]
