"""S3: stored successful snapshots, current authorization, and no model/history side effects."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import text
from tests.sdk_core.synthetic_tools import QUERY_TOOL, TARGET, TOTAL_TOOL
from tests.sdk_core.test_app import cite, clarify, tool_call
from tests.sdk_core.test_channel_service import Env, Outbox, app_config, ref
from tests.sdk_core.test_channel_service import env as env

from xiaowei.app import Application
from xiaowei.channel import ChannelService, ResultUnavailableError
from xiaowei.channel_store import RequestUnavailableError
from xiaowei.governance import GovernedTools
from xiaowei.models import SessionSnapshot
from xiaowei.session import SessionLimits

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize(
    ("turns", "turn_limit", "used", "byte_limit", "seconds", "words", "last"),
    [
        (3, 5, 200, 1000, 7200, (), False),
        (4, 5, 200, 1000, 7200, ("4/5",), False),
        (5, 5, 200, 1000, 7200, ("5/5",), True),
        (1, 1, 200, 1000, 7200, ("1/1",), True),
        (4, 6, 200, 1000, 7200, (), False),
        (5, 6, 200, 1000, 7200, ("5/6",), False),
        (1, 100, 799, 1000, 7200, (), False),
        (1, 100, 800, 1000, 7200, ("历史容量",), False),
        (1, 100, 1000, 1000, 7200, ("历史容量",), True),
        (1, 100, 200, 1000, 3601, (), False),
        (1, 100, 200, 1000, 3600, ("1 小时",), False),
        (1, 100, 200, 1000, 0, ("保留期限",), True),
        (4, 5, 800, 1000, 3600, ("4/5", "历史容量", "1 小时"), False),
    ],
)
async def test_saved_success_thresholds_visible_without_new_execution(
    env: Env,
    turns: int,
    turn_limit: int,
    used: int,
    byte_limit: int,
    seconds: int,
    words: tuple[str, ...],
    last: bool,
) -> None:
    message = env.scripts.add("边界样本", tool_call("order_total", region="east"), cite())
    record = await env.run(message)
    assert record.state == "completed"
    snapshot = SessionSnapshot(
        observed_at=env.clock(),
        phase="after_commit",
        committed_turns=turns,
        turn_limit=turn_limit,
        replay_bytes=used,
        byte_limit=byte_limit,
        expires_at=env.clock() + timedelta(seconds=seconds),
    )
    # This is a historical observation, deliberately independent of the current session balance.
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE xiaowei_request SET session_state=:snapshot WHERE request_key=:key"),
            {"snapshot": snapshot.model_dump_json(), "key": record.request_key},
        )
    calls = len(env.adapter.calls), env.model_calls(message)
    view = await env.results().view(ref(), first=True)
    assert view.delivery is not None and view.delivery.facts
    notice = view.delivery.session_notice
    if not words:
        assert notice is None
    else:
        assert notice is not None
        assert notice.count("本轮结束时") == 1
        assert "新建会话" in notice
        for word in words:
            assert word in notice
        assert ("下轮需要新建" in notice) is last
        assert "保证" not in notice
    assert (len(env.adapter.calls), env.model_calls(message)) == calls


async def test_warning_is_channel_only_and_costs_no_additional_scope_checks(env: Env) -> None:
    for number in range(4):
        message = env.scripts.add(
            f"排障第{number + 1}轮",
            tool_call("order_total", region="east"),
            cite("结论第一行\n第二行"),
        )
        record = await env.run(message, f"r{number + 1}")
        assert record.state == "completed"
    snapshot = record.session_state
    assert snapshot is not None and snapshot.committed_turns == 4 and snapshot.turn_limit == 5
    before = len(env.grants.checks)
    view = await env.results().view(ref("r4"), first=True)
    checks = len(env.grants.checks) - before
    assert view.delivery is not None
    notice = view.delivery.session_notice
    assert notice is not None and "4/5" in notice
    assert "点击" in notice and "新建会话" in notice
    async with env.engine.begin() as conn:
        answer = await conn.scalar(
            text("SELECT answer FROM xiaowei_request WHERE request_key=:key"),
            {"key": record.request_key},
        )
        items = (
            (await conn.execute(text("SELECT message_data FROM agent_messages"))).scalars().all()
        )
        await conn.execute(
            text("UPDATE xiaowei_request SET session_state=NULL WHERE request_key=:key"),
            {"key": record.request_key},
        )
    assert notice not in answer
    assert all("session_notice" not in item and notice not in item for item in items)
    before = len(env.grants.checks)
    plain = await env.results().view(ref("r4"), first=True)
    assert len(env.grants.checks) - before == checks
    assert plain.delivery is not None and plain.delivery.session_notice is None
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE xiaowei_request SET session_state=:snapshot WHERE request_key=:key"),
            {"snapshot": snapshot.model_dump_json(), "key": record.request_key},
        )
    followup = env.scripts.add("继续排查", tool_call("order_total", region="east"), cite())
    assert (await env.run(followup, "r5")).state == "completed"
    for call in env.scripts.calls[followup]:
        inputs = json.dumps(call.input, ensure_ascii=False)
        assert "session_notice" not in inputs and notice not in inputs
    last = await env.results().view(ref("r5"), first=True)
    assert last.delivery is not None and "下轮需要新建" in last.delivery.session_notice


async def test_warning_history_resend_and_revocation_keep_original_snapshot(env: Env) -> None:
    for number in range(4):
        message = env.scripts.add(f"取证{number}", tool_call("order_total", region="east"), cite())
        record = await env.run(message, f"r{number}")
        assert record.state == "completed"
    result = env.results()
    first = await result.view(ref("r3"), first=True)
    assert first.delivery is not None and first.delivery.session_notice is not None
    original = first.delivery.session_notice
    outbox = Outbox(outcome="failed")
    assert await result.send(ref("r3"), outbox) == "failed"
    env.clock.advance(120)
    restarted = env.results(env.channel_store())
    historical = await restarted.view(ref("r3"))
    assert historical.delivery is not None and historical.delivery.session_notice == original
    outbox.outcome = "sent"
    assert await restarted.send(ref("r3"), outbox, resend=True) == "sent"
    assert outbox.sent[-1].session_notice == original
    assert "4/5" in original and len(env.adapter.calls) == 4
    with pytest.raises(RequestUnavailableError):
        await restarted.view(ref("r3", subject="bob"))
    with pytest.raises(RequestUnavailableError):
        await restarted.view(ref("r3", channel="feishu"))
    env.grants.allowed.remove(("alice", TARGET, TOTAL_TOOL))
    with pytest.raises(ResultUnavailableError):
        await restarted.view(ref("r3"))
    assert len(env.adapter.calls) == 4


def service_with_limits(env: Env, turns: int, size: int = 40_000) -> ChannelService:
    app = Application(
        app_config(
            session_limits=SessionLimits(
                max_history_turns=turns, max_history_bytes=size, retention_seconds=7200
            )
        ),
        model=env.binding,
        engine=env.engine,
        governance=GovernedTools(env.evidence),
        local_tools={
            (TOTAL_TOOL, TARGET): env.adapter.execute,
            (QUERY_TOOL, TARGET): env.adapter.execute,
        },
        clock=env.clock,
    )
    return ChannelService(app, env.results())


async def test_actual_committed_bytes_trigger_notice_without_turn_threshold(env: Env) -> None:
    service = service_with_limits(env, 100, 2300)
    message = env.scripts.add(
        "历史容量样本",
        tool_call("order_total", region="east"),
        cite("排障口径：" + "scope; " * 170),
    )
    record = await service.process(await service.accept(env.inbound(message)))
    assert record.state == "completed", record.session_state
    snapshot = record.session_state
    assert snapshot is not None and snapshot.committed_turns == 1
    assert snapshot.replay_bytes is not None
    assert 1840 <= snapshot.replay_bytes < 2300, snapshot
    view = await env.results().view(ref(), first=True)
    assert view.delivery is not None
    assert "历史容量" in view.delivery.session_notice
    assert "会话已用" not in view.delivery.session_notice
    assert len(env.adapter.calls) == 1


async def test_actual_session_expiry_is_used_at_commit_not_delivery_time(env: Env) -> None:
    service = service_with_limits(env, 100)
    first = env.scripts.add("准备排障", clarify("请给出具体地区"))
    assert (await service.process(await service.accept(env.inbound(first)))).state == "completed"
    env.clock.advance(3600)
    next_message = env.scripts.add("继续了解口径", clarify("请给出地区口径"))
    record = await service.process(await service.accept(env.inbound(next_message, "r2")))
    assert record.state == "completed"
    view = await env.results().view(ref("r2"), first=True)
    assert view.delivery is not None and "1 小时" in view.delivery.session_notice
    original = view.delivery.session_notice
    env.clock.advance(30)
    reread = await env.results().view(ref("r2"))
    assert reread.delivery is not None and reread.delivery.session_notice == original
    assert not env.adapter.calls


async def test_first_success_warning_never_persists_as_sdk_content(env: Env) -> None:
    service = service_with_limits(env, 1)
    message = env.scripts.add("唯一合法轮次", tool_call("order_total", region="east"), cite())
    record = await service.process(await service.accept(env.inbound(message)))
    assert record.state == "completed"
    view = await env.results().view(ref(), first=True)
    assert view.delivery is not None and view.delivery.session_notice is not None
    async with env.engine.connect() as conn:
        items = (
            (await conn.execute(text("SELECT message_data FROM agent_messages"))).scalars().all()
        )
        body = await conn.scalar(text("SELECT answer FROM xiaowei_request"))
    assert all("session_notice" not in item and "本轮结束时" not in item for item in items)
    assert "session_notice" not in body and "本轮结束时" not in body
