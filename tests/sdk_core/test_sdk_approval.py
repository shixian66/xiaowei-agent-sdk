"""SDK-A：正式群网关 → SDK HITL → 治理 → Action/Evidence/Session，外部接口替身。"""

import json
from dataclasses import replace

import pytest
from tests.sdk_core.synthetic_tools import QUERY_TOOL, TOTAL_TOOL
from tests.sdk_core.test_app import cite, clarify, tool_call, upstream_error
from tests.sdk_core.test_group_actions import SOURCE, WRITE, Actions, request_execution
from tests.sdk_core.test_group_actions import actions as actions
from tests.sdk_core.test_group_gateway import running
from tests.sdk_core.test_group_identity import A, B, group_ref

from xiaowei.feishu import attributed

pytestmark = pytest.mark.loopback
GATE = f"{SOURCE}__request_action_execution"


async def paused(actions: Actions, group, *, investigate=False):
    env = actions.env
    steps = [tool_call("order_total", region="east")] if investigate else []
    script = env.scripts.add(
        attributed(A, "SDK 静默请求"),
        *steps,
        tool_call(f"{SOURCE}__propose_change", value="after"),
        request_execution,
        cite("根据实际执行结果反馈"),
    )
    await group.ask("SDK 静默请求", message_id="om_sdk_origin")
    assert await env.rows(
        "SELECT state, delivery FROM xiaowei_request WHERE reply_message_id='om_sdk_origin'"
    ) == [("paused", "sent")]
    assert actions.adapter.calls == []
    return await actions.one(), script


async def test_native_pause_resume_writes_once_and_commits_paired_history(actions: Actions):
    env = actions.env
    # 发起人有群读取/提出权限，没有写权限；B才是本次写的批准主体。
    env.access._grants = {**env.access._grants, A: env.access._grants[A] - {WRITE}}
    async with running(env, max_reply_chars=3500) as group:
        action_id, script = await paused(actions, group)
        assert env.model_calls(script) == 2
        assert all(GATE in names for names in env.scripts.tools_seen(script))
        assert all(f"{SOURCE}__change" not in names for names in env.scripts.tools_seen(script))
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_sdk_approve")
        assert await env.rows(
            "SELECT state, delivery FROM xiaowei_request WHERE reply_message_id='om_sdk_approve'"
        ) == [("completed", "sent")]
        ((origin_turn,),) = await env.rows(
            "SELECT turn_id FROM xiaowei_request WHERE reply_message_id='om_sdk_origin'"
        )
        ((saved_answer,),) = await env.rows(
            "SELECT answer FROM xiaowei_request WHERE reply_message_id='om_sdk_approve'"
        )
        assert json.loads(saved_answer)["run_turn_id"] == origin_turn
        view = await env.results.view(group_ref("om_sdk_approve", B), first=True)
        assert view.delivery is not None
        outcome = next(
            f for f in view.delivery.facts if f.tool_id.endswith("/request_action_execution")
        )
        assert outcome.requires_complete
        assert outcome.metadata["state"] == "succeeded"
        assert {"action_id", "state", "approver", "result"} <= outcome.metadata.keys()
        assert "动作执行事实" in group.outbox.texts()[-1]
        before = len(actions.adapter.calls)
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_sdk_duplicate")
        assert len(actions.adapter.calls) == before == 1
    assert env.model_calls(script) == 3
    assert await env.rows("SELECT state, approver_id FROM xiaowei_action") == [("succeeded", B)]
    rows = await env.rows("SELECT message_data FROM agent_messages ORDER BY id")
    items = [json.loads(row[0]) for row in rows]
    calls = [item["call_id"] for item in items if item.get("type") == "function_call"]
    outputs = [item["call_id"] for item in items if item.get("type") == "function_call_output"]
    assert len(calls) == 2 and sorted(calls) == sorted(outputs)
    assert all(item.get("type") != "tool_approval_item" for item in items)
    assert GATE in [item.get("name") for item in items]
    assert all(f"/批准 {action_id}" not in str(item) for item in items)


@pytest.mark.parametrize("cause", ["left", "history_revoked"])
async def test_requester_invalid_stops_before_model_and_write_with_exact_receipt(actions, cause):
    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        action_id, script = await paused(actions, group, investigate=cause == "history_revoked")
        before = env.model_calls(script)
        if cause == "left":
            env.members.current.remove(A)
        else:
            env.access._group = replace(
                env.access._group, tools=env.access._group.tools - {TOTAL_TOOL, QUERY_TOOL}
            )
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_sdk_invalid")
    assert env.model_calls(script) == before
    assert actions.adapter.calls == []
    assert await env.rows("SELECT state, failure_code FROM xiaowei_action") == [
        ("rejected", "action_requester_invalid")
    ]
    assert await env.rows(
        "SELECT failure_code FROM xiaowei_request WHERE reply_message_id='om_sdk_invalid'"
    ) == [("action_requester_invalid",)]
    assert await env.rows("SELECT attempt FROM xiaowei_action") == [(None,)]
    assert len(actions.adapter.reads) == 1
    assert "发起人已不在群或权限已变化，未执行，请重新提出" in str(group.outbox.sent[-1])
    assert await env.rows("SELECT COUNT(*) FROM agent_messages") == [(0,)]


async def test_approval_without_native_pause_has_no_legacy_write_fallback(actions):
    env = actions.env
    env.scripts.add(
        attributed(A, "只提出"),
        tool_call(f"{SOURCE}__propose_change", value="after"),
        cite("未请求 SDK 执行"),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("只提出")
        action_id = await actions.one()
        await group.ask(f"/批准 {action_id}", sender=B)
    assert actions.adapter.calls == []


async def test_image_session_offers_no_execution_request_and_proposal_stays_unapprovable(
    actions, monkeypatch
):
    """RunState 会保存整轮输入；原图只留应用表，有图会话不进入 SDK 审批暂停。"""
    from tests.sdk_core.test_feishu_images import _png

    from xiaowei.image_input import SavedImage

    env = actions.env
    digest = "d" * 64

    class Images:
        async def load(self, identity):
            return [SavedImage(digest, _png(), "image/png")]

    monkeypatch.setattr(env.app, "images", Images())
    monkeypatch.setattr(
        env.app._model,
        "_profile",
        env.app._model.profile.model_copy(update={"image_input_enabled": True}),
    )
    script = env.scripts.add(
        attributed(A, "有图提出") + f"\n已获准原图 1，来源摘要 {digest}：\n",
        tool_call(f"{SOURCE}__propose_change", value="after"),
        cite("仅形成提案"),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("有图提出", message_id="om_image_proposal")
        action_id = await actions.one()
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_image_approve")
    seen = env.scripts.tools_seen(script)
    assert seen and all(GATE not in names for names in seen)
    assert f"{SOURCE}__propose_change" in seen[0]
    assert "本会话含图片，不能请求执行" in env.scripts.calls[script][0].instructions
    assert env.model_calls(script) == 2
    assert await env.rows("SELECT state, failure_code FROM xiaowei_action") == [
        ("rejected", "action_unresumable")
    ]
    assert await env.rows("SELECT COUNT(*) FROM xiaowei_action_run") == [(0,)]
    assert await env.rows(
        "SELECT failure_code FROM xiaowei_request WHERE reply_message_id='om_image_approve'"
    ) == [("action_unresumable",)]
    assert actions.adapter.calls == []


async def test_proposal_without_sdk_request_is_terminal_and_cannot_be_approved(actions):
    env = actions.env
    env.scripts.add(
        attributed(A, "只提出"),
        tool_call(f"{SOURCE}__propose_change", value="after"),
        cite("仅形成提案"),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("只提出", message_id="om_orphan")
        action_id = await actions.one()
        assert await env.rows("SELECT state, failure_code FROM xiaowei_action") == [
            ("rejected", "action_unresumable")
        ]
        assert await env.rows(
            "SELECT state FROM xiaowei_request WHERE reply_message_id='om_orphan'"
        ) == [("completed",)]
        assert "待批准动作" not in group.outbox.texts()[-1]
        assert "批准命令" not in group.outbox.texts()[-1]
        view = await env.results.view(group_ref("om_orphan", A), first=True)
        assert view.delivery is not None
        assert f"/批准 {action_id}" not in view.delivery.content
        assert all("approval_command" not in fact.metadata for fact in view.delivery.facts)
        assert all(
            fact.result_json is None or f"/批准 {action_id}" not in fact.result_json
            for fact in view.delivery.facts
        )
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_orphan_approve")
    assert actions.adapter.calls == []
    assert await env.rows(
        "SELECT failure_code FROM xiaowei_request WHERE reply_message_id='om_orphan_approve'"
    ) == [("action_unresumable",)]
    assert "未形成可恢复审批" in group.outbox.texts()[-1]


@pytest.mark.parametrize("tool_limit", [2, 3])
async def test_execution_request_budget_is_decided_before_pause(actions, tool_limit):
    env = actions.env
    env.results._budget = env.results.budget.model_copy(update={"max_tool_calls": tool_limit})
    env.scripts.add(
        attributed(A, "额度临界"),
        tool_call(f"{SOURCE}__propose_change", value="after"),
        request_execution,
        cite("说明本轮结果"),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("额度临界", message_id="om_budget_boundary")
        action_id = await actions.one()
        if tool_limit == 2:
            assert await env.rows("SELECT state, failure_code FROM xiaowei_action") == [
                ("rejected", "action_unresumable")
            ]
            assert "待批准动作" not in group.outbox.texts()[-1]
            assert "批准命令" not in group.outbox.texts()[-1]
            assert await env.rows(
                "SELECT state FROM xiaowei_request WHERE reply_message_id='om_budget_boundary'"
            ) == [("completed",)]
            outputs = [
                json.loads(item["output"])
                for call in env.scripts.calls[attributed(A, "额度临界")]
                for item in call.input
                if item.get("type") == "function_call_output"
            ]
            assert any(
                item.get("status") == "rejected" and item.get("code") == "budget_exhausted"
                for item in outputs
            )
            await group.ask(f"/批准 {action_id}", sender=B, message_id="om_budget_refused")
            assert "未形成可恢复审批" in group.outbox.texts()[-1]
        else:
            assert await env.rows("SELECT state FROM xiaowei_action") == [("pending",)]
            await group.ask(f"/批准 {action_id}", sender=B, message_id="om_budget_approved")
            assert await env.rows("SELECT state FROM xiaowei_action") == [("succeeded",)]
    assert len(actions.adapter.calls) == (0 if tool_limit == 2 else 1)


async def test_duplicate_execution_requests_do_not_leave_an_unusable_candidate(actions):
    from agents.testing import function_call

    env = actions.env

    def request_twice(call):
        action_id = next(
            json.loads(item["output"])["data"]["action_id"]
            for item in call.input
            if item.get("type") == "function_call_output"
            and "action_id" in json.loads(item["output"]).get("data", {})
        )
        return [function_call(GATE, {"action_id": action_id}, call_id=f"dup-{n}") for n in (1, 2)]

    env.scripts.add(
        attributed(A, "重复请求"),
        tool_call(f"{SOURCE}__propose_change", value="after"),
        request_twice,
        cite(),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("重复请求", message_id="om_duplicate_gate")
        action_id = await actions.one()
        assert await env.rows("SELECT state, failure_code FROM xiaowei_action") == [
            ("rejected", "action_unresumable")
        ]
        assert await env.rows(
            "SELECT failure_code FROM xiaowei_request WHERE reply_message_id='om_duplicate_gate'"
        ) == [("action_unresumable",)]
        assert "待批准动作" not in group.outbox.texts()[-1]
        assert "批准命令" not in group.outbox.texts()[-1]
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_duplicate_approve")
    assert actions.adapter.calls == []
    assert "未形成可恢复审批" in group.outbox.texts()[-1]


async def test_unknown_action_id_is_rejected_to_model_without_pausing(actions):
    from uuid import uuid4

    env = actions.env
    script = env.scripts.add(
        attributed(A, "错误动作 ID"),
        tool_call(GATE, action_id=str(uuid4())),
        clarify("请核对 Action ID 后重新提出"),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("错误动作 ID", message_id="om_unknown_gate")
    assert actions.adapter.calls == []
    assert await env.rows(
        "SELECT state FROM xiaowei_request WHERE reply_message_id='om_unknown_gate'"
    ) == [("completed",)]
    outputs = [
        json.loads(item["output"])
        for call in env.scripts.calls[script]
        for item in call.input
        if item.get("type") == "function_call_output"
    ]
    assert any(item.get("status") == "rejected" for item in outputs)


async def test_false_sdk_approval_cannot_reach_write_without_bound_approver(actions):
    from uuid import uuid4

    from tests.sdk_core.test_group_identity import GROUP

    from xiaowei.governance import ToolRejectedError
    from xiaowei.models import Identity, RunContext, ToolRequest

    env = actions.env
    gate = f"{SOURCE}/request_action_execution"
    ctx = RunContext(
        identity=Identity(
            subject_id=A,
            session_id="s_approval_guard",
            turn_id="t_approval_guard",
            owner=GROUP.owner,
            channel="feishu",
        ),
        target_scope=frozenset({SOURCE}),
        tool_scope=frozenset({gate}),
        budget=env.results.budget,
    )
    executed = False

    async def forbidden_write(_request):
        nonlocal executed
        executed = True
        raise AssertionError("未经批准不得调用执行函数")

    with pytest.raises(ToolRejectedError):
        await actions.governance.invoke(
            ctx,
            ToolRequest(
                tool_id=gate,
                target_id=SOURCE,
                call_id="c_approval_guard",
                tool_name=GATE,
                arguments={"action_id": str(uuid4())},
            ),
            forbidden_write,
        )
    assert not executed and actions.adapter.calls == []
    assert actions.governance.checkpoint(ctx.identity).used == 0


async def test_failed_resumed_turn_settles_new_proposal_without_replaying_write(actions):
    env = actions.env
    env.results._budget = env.results.budget.model_copy(update={"max_tool_calls": 6})
    env.scripts.add(
        attributed(A, "续跑后失败"),
        tool_call(f"{SOURCE}__propose_change", value="first"),
        request_execution,
        tool_call(f"{SOURCE}__propose_change", value="second"),
        upstream_error,
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("续跑后失败", message_id="om_resume_fail_origin")
        first = await actions.one()
        await group.ask(f"/批准 {first}", sender=B, message_id="om_resume_fail_approve")
        rows = await env.rows("SELECT state, failure_code, plan, action_id FROM xiaowei_action")
        by_value = {
            json.loads(plan)["plan"]["parameters"]["value"]: (state, failure, aid)
            for state, failure, plan, aid in rows
        }
        assert by_value["first"][:2] == ("succeeded", None)
        assert by_value["second"][:2] == ("rejected", "action_unresumable")
        await group.ask(f"/批准 {by_value['second'][2]}", sender=B)
    assert len(actions.adapter.calls) == 1
    assert "未形成可恢复审批" in group.outbox.texts()[-1]


async def test_pause_keeps_only_the_unique_requested_action(actions):
    env = actions.env
    env.results._budget = env.results.budget.model_copy(update={"max_tool_calls": 5})
    env.scripts.add(
        attributed(A, "两个提案只执行一个"),
        tool_call(f"{SOURCE}__propose_change", value="unused"),
        tool_call(f"{SOURCE}__propose_change", value="selected"),
        request_execution,
        cite(),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("两个提案只执行一个", message_id="om_one_gate")
        rows = await env.rows("SELECT state, failure_code, plan, action_id FROM xiaowei_action")
        by_value = {
            json.loads(plan)["plan"]["parameters"]["value"]: (state, failure, aid)
            for state, failure, plan, aid in rows
        }
        assert by_value["unused"][:2] == ("rejected", "action_unresumable")
        assert by_value["selected"][:2] == ("pending", None)
        card = group.outbox.texts()[-1]
        assert by_value["unused"][2] not in card
        assert f"/批准 {by_value['selected'][2]}" in card
        await group.ask(f"/批准 {by_value['unused'][2]}", sender=B)
        assert "未形成可恢复审批" in group.outbox.texts()[-1]
        await group.ask(f"/批准 {by_value['selected'][2]}", sender=B)
    assert len(actions.adapter.calls) == 1
    assert await env.rows(
        "SELECT state FROM xiaowei_action WHERE action_id=:id", id=by_value["selected"][2]
    ) == [("succeeded",)]


@pytest.mark.parametrize("invalid", ["sealed", "closed", "expired"])
async def test_original_session_invalid_has_terminal_reason_and_no_model_or_write(actions, invalid):
    from datetime import timedelta

    from sqlalchemy import text

    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        action_id, script = await paused(actions, group)
        before = env.model_calls(script)
        async with env.engine.begin() as conn:
            if invalid == "expired":
                await conn.execute(
                    text("UPDATE xiaowei_session SET expires_at=:past"),
                    {"past": env.clock() - timedelta(seconds=1)},
                )
            else:
                await conn.execute(
                    text("UPDATE xiaowei_session SET state=:state"), {"state": invalid}
                )
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_sdk_bad_session")
    assert actions.adapter.calls == [] and env.model_calls(script) == before
    assert await env.rows("SELECT state, failure_code FROM xiaowei_action") == [
        ("rejected", "action_session_invalid")
    ]
    assert "原会话不可继续，未执行" in str(group.outbox.sent[-1])
    assert await env.rows("SELECT COUNT(*) FROM agent_messages") == [(0,)]


@pytest.mark.parametrize("failure", ["changed", "timeout", "model"])
async def test_native_failures_preserve_action_fact_and_never_replay(actions, failure):
    from tests.sdk_core.test_app import upstream_error

    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        action_id, script = await paused(actions, group)
        if failure == "changed":
            actions.adapter.revision = "v2"
        elif failure == "timeout":
            actions.adapter.fail = True
        else:
            env.scripts.by_message[script] = [upstream_error]
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_sdk_failed")
        count = len(actions.adapter.calls)
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_sdk_failed_again")
    state = {"changed": "rejected", "timeout": "unknown", "model": "succeeded"}[failure]
    code = {
        "changed": "action_rejected",
        "timeout": "action_unknown",
        "model": "action_feedback_failed",
    }[failure]
    assert await env.rows("SELECT state FROM xiaowei_action") == [(state,)]
    assert await env.rows(
        "SELECT failure_code FROM xiaowei_request WHERE reply_message_id='om_sdk_failed'"
    ) == [(code,)]
    assert count == len(actions.adapter.calls) == (0 if failure == "changed" else 1)
    assert await env.rows("SELECT COUNT(*) FROM agent_messages") == [(0,)]


async def test_restart_restores_original_budget_and_cannot_call_extra_tool(actions):
    from tests.sdk_core.synthetic_tools import TARGET

    from xiaowei.actions import ActionService
    from xiaowei.app import Application
    from xiaowei.channel import ChannelService
    from xiaowei.governance import GovernedTools

    env = actions.env
    env.results._budget = env.results.budget.model_copy(update={"max_tool_calls": 3})
    async with running(env, max_reply_chars=3500) as group:
        action_id, script = await paused(actions, group)
    old = env.app
    governed = GovernedTools(env.evidence)
    fresh = Application(
        old._config,
        model=old._model,
        engine=env.engine,
        governance=governed,
        local_tools={
            (TOTAL_TOOL, TARGET): env.adapter.execute,
            (QUERY_TOOL, TARGET): env.adapter.execute,
        },
        clock=env.clock,
        actions=ActionService(governed, env.store, env.access, [actions.binding], clock=env.clock),
    )
    env.app = fresh
    env.service = ChannelService(fresh, env.results)
    env.scripts.by_message[script] = [tool_call("order_total", region="east"), cite()]
    async with running(env, max_reply_chars=3500) as group:
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_sdk_restart")
    assert len(actions.adapter.calls) == 1
    assert env.adapter.calls == []
    assert await env.rows(
        "SELECT state FROM xiaowei_request WHERE reply_message_id='om_sdk_restart'"
    ) == [("completed",)]
    last = env.scripts.calls[script][-1]
    rejected = [
        json.loads(i["output"])
        for i in last.input
        if i.get("type") == "function_call_output"
        and json.loads(i["output"]).get("status") == "rejected"
    ]
    assert len(rejected) == 1 and rejected[0]["code"] == "budget_exhausted"


async def test_partial_approvals_save_new_version_and_keep_original_turn(actions):
    from agents.testing import function_call

    env = actions.env
    env.results._budget = env.results.budget.model_copy(update={"max_tool_calls": 6})

    def request_both(call):
        ids = [
            json.loads(i["output"])["data"]["action_id"]
            for i in call.input
            if i.get("type") == "function_call_output"
        ]
        return [
            function_call(GATE, {"action_id": aid}, call_id=f"gate-{n}")
            for n, aid in enumerate(ids)
        ]

    script = env.scripts.add(
        attributed(A, "两个动作"),
        tool_call(f"{SOURCE}__propose_change", value="one"),
        tool_call(f"{SOURCE}__propose_change", value="two"),
        request_both,
        cite(),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("两个动作", message_id="om_two")
        ids = [
            row[0]
            for row in await env.rows("SELECT action_id FROM xiaowei_action ORDER BY call_id")
        ]
        assert len(ids) == 2 and actions.adapter.calls == []
        await group.ask(f"/批准 {ids[0]}", sender=B, message_id="om_first")
        assert len(actions.adapter.calls) == 1 and env.model_calls(script) == 3
        assert await env.rows("SELECT version, consumed FROM xiaowei_action_run") == [(2, False)]
        assert await env.rows("SELECT COUNT(*) FROM agent_messages") == [(0,)]
        view = await env.results.view(group_ref("om_first", B), first=True)
        assert view.delivery is not None
        outcomes = [
            f for f in view.delivery.facts if f.tool_id.endswith("/request_action_execution")
        ]
        assert len(outcomes) == 1 and outcomes[0].requires_complete
        assert outcomes[0].metadata["action_id"] == ids[0]
        assert outcomes[0].metadata["state"] == "succeeded"
        assert {"action_id", "state", "approver", "result"} <= outcomes[0].metadata.keys()
        assert "动作执行事实" in group.outbox.texts()[-1]
        assert ids[0] in group.outbox.texts()[-1] and ids[1] in group.outbox.texts()[-1]
        await group.ask(f"/批准 {ids[0]}", sender=B, message_id="om_first_again")
        assert await env.rows(
            "SELECT state FROM xiaowei_action WHERE action_id=:id", id=ids[1]
        ) == [("pending",)]
        await group.ask(f"/批准 {ids[1]}", sender=B, message_id="om_second")
    assert len(actions.adapter.calls) == 2 and env.model_calls(script) == 4
    assert await env.rows("SELECT state FROM xiaowei_action ORDER BY call_id") == [
        ("succeeded",),
        ("succeeded",),
    ]
    assert await env.rows(
        "SELECT state FROM xiaowei_request WHERE reply_message_id='om_second'"
    ) == [("completed",)]
    assert await env.rows("SELECT turns FROM xiaowei_session") == [(1,)]


async def test_group_rejection_uses_native_reject_and_never_writes(actions):
    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        action_id, script = await paused(actions, group)
        await group.ask(f"/拒绝 {action_id}", sender=B, message_id="om_sdk_reject")
    assert actions.adapter.calls == []
    assert await env.rows("SELECT state, failure_code FROM xiaowei_action") == [
        ("rejected", "action_rejected")
    ]
    assert env.model_calls(script) == 3
    assert await env.rows(
        "SELECT state FROM xiaowei_request WHERE reply_message_id='om_sdk_reject'"
    ) == [("completed",)]
    assert "rejected" in str(group.outbox.sent[-1])
    assert await env.rows("SELECT turns FROM xiaowei_session") == [(1,)]


async def test_concurrent_close_after_preflight_is_atomic_and_does_not_write(actions, monkeypatch):
    import asyncio

    from sqlalchemy import text

    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        action_id, script = await paused(actions, group)
    entered, release = asyncio.Event(), asyncio.Event()
    check = env.store._native_session

    async def held(conn, identity):
        entered.set()
        await release.wait()
        return await check(conn, identity)

    monkeypatch.setattr(env.store, "_native_session", held)
    receipt = await env.service.accept(
        env.group(attributed(B, f"/批准 {action_id}"), "om_close_race", actor=B)
    )
    task = asyncio.create_task(env.service.approve(receipt, action_id))
    await asyncio.wait_for(entered.wait(), 3)
    async with env.engine.begin() as conn:
        await conn.execute(text("UPDATE xiaowei_session SET state='closed'"))
    release.set()
    record = await task
    assert record.failure_code == "action_session_invalid"
    assert actions.adapter.calls == [] and env.model_calls(script) == 2
    assert await env.rows("SELECT attempt FROM xiaowei_action") == [(None,)]
    assert len(actions.adapter.reads) == 1
    assert await env.rows("SELECT state FROM xiaowei_action") == [("rejected",)]


async def test_close_while_write_inflight_keeps_success_and_factual_receipt(actions):
    import asyncio

    from sqlalchemy import text

    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        action_id, _script = await paused(actions, group)
        actions.adapter.entered, actions.adapter.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(
            group.ask(f"/批准 {action_id}", sender=B, message_id="om_inflight_close")
        )
        await asyncio.wait_for(actions.adapter.entered.wait(), 3)
        async with env.engine.begin() as conn:
            await conn.execute(text("UPDATE xiaowei_session SET state='closed'"))
        actions.adapter.release.set()
        await task
    assert len(actions.adapter.calls) == 1
    assert await env.rows("SELECT state FROM xiaowei_action") == [("succeeded",)]
    assert await env.rows(
        "SELECT failure_code FROM xiaowei_request WHERE reply_message_id='om_inflight_close'"
    ) == [("action_feedback_failed",)]
    assert "工具执行失败" not in str(group.outbox.sent[-1])
    assert await env.rows("SELECT COUNT(*) FROM agent_messages") == [(0,)]


async def test_failed_material_can_be_explicitly_resent_before_approval(actions):
    from sqlalchemy import text
    from tests.sdk_core.test_group_identity import GROUP

    from xiaowei.channel import RequestRef

    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        action_id, _script = await paused(actions, group)
        async with env.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE xiaowei_request SET delivery='failed' "
                    "WHERE reply_message_id='om_sdk_origin'"
                )
            )
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_not_sent")
        assert actions.adapter.calls == []
        delivered = []

        async def transmit(delivery):
            delivered.append(delivery)
            return "sent"

        outcome = await env.results.send(
            RequestRef(
                channel="feishu",
                request_id="om_sdk_origin",
                subject_id=A,
                conversation_id=GROUP.chat_id,
                group=GROUP,
            ),
            transmit,
            resend=True,
        )
        assert outcome == "sent" and len(delivered) == 1
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_resent_approve")
    assert len(actions.adapter.calls) == 1


async def test_restart_keeps_pause_and_recovers_unsent_material_without_replaying(actions):
    from sqlalchemy import text

    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        aid, script = await paused(actions, group)
    async with env.engine.begin() as conn:
        await conn.execute(text("UPDATE xiaowei_request SET delivery='pending'"))
    report = await env.store.recover(actions.lock)
    assert report.unsent == 1
    assert await env.rows("SELECT state, delivery FROM xiaowei_request") == [("paused", "failed")]
    assert await env.rows("SELECT consumed FROM xiaowei_action_run") == [(False,)]
    assert actions.adapter.calls == [] and env.model_calls(script) == 2
    async with running(env, max_reply_chars=3500) as group:
        await group.ask(f"/批准 {aid}", sender=B)
    assert actions.adapter.calls == []


async def test_expired_native_snapshot_cleanup_is_bounded_and_preserves_action_cause(actions):
    from datetime import timedelta

    from xiaowei.session import cleanup_expired

    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        aid, script = await paused(actions, group)
        env.clock.now += timedelta(minutes=16)
        await cleanup_expired(env.engine, now=env.clock(), batch_size=1)
        assert await env.rows("SELECT COUNT(*) FROM xiaowei_action_run") == [(0,)]
        assert await env.rows("SELECT state, failure_code FROM xiaowei_action") == [
            ("rejected", "action_expired")
        ]
        await group.ask(f"/批准 {aid}", sender=B)
    assert "候选已过期，未执行" in str(group.outbox.sent[-1])
    assert actions.adapter.calls == [] and env.model_calls(script) == 2


async def test_migrated_old_action_has_dedicated_receipt_and_no_resume_or_write(actions):
    from sqlalchemy import text

    env = actions.env
    script = env.scripts.add(
        attributed(A, "旧提案"), tool_call(f"{SOURCE}__propose_change", value="after"), cite()
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("旧提案")
        aid = await actions.one()
        # 此用例验证迁移产物的入口；迁移的真实v8/v9输入另在test_storage_v2实跑。
        async with env.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE xiaowei_action SET state='rejected', "
                    "failure_code='action_upgrade_invalidated'"
                )
            )
        await group.ask(f"/批准 {aid}", sender=B)
    assert "已因升级作废、未执行，请重新提出" in str(group.outbox.sent[-1])
    assert actions.adapter.calls == [] and env.model_calls(script) == 2


@pytest.mark.parametrize("failure", ["syntax", "unavailable"])
async def test_restart_keeps_monitoring_failures_and_failed_expression_without_replay(
    actions, failure
):
    from tests.sdk_core.synthetic_tools import TARGET
    from tests.sdk_core.test_monitoring_r1a import SOURCE as PROM_SOURCE
    from tests.sdk_core.test_monitoring_r1a import TOOL as PROM_TOOL
    from tests.sdk_core.test_monitoring_r1a import monitoring_config
    from tests.sdk_core.test_runtime import serve_config

    from xiaowei.actions import ActionService
    from xiaowei.app import Application
    from xiaowei.channel import ChannelService, ResultDelivery
    from xiaowei.evidence import EvidenceStore
    from xiaowei.governance import (
        GovernedTools,
        MonitoringReadError,
        PromQLExpressionError,
        ToolCatalog,
    )
    from xiaowei.runtime import ServeConfig, _monitoring_catalog
    from xiaowei.tools import governed_function_tool

    env = actions.env
    old = env.app
    base = env.evidence.catalog
    contracts, policies = _monitoring_catalog(
        ServeConfig.model_validate(
            serve_config(8501, **monitoring_config("http://127.0.0.1:9000/mcp"))
        )
    )
    combined = ToolCatalog(
        (*base.contracts, *contracts), (*(base.policy_for(c) for c in base.contracts), *policies)
    )
    env.access._targets |= {PROM_SOURCE}
    env.access._group = replace(env.access._group, tools=env.access._group.tools | {PROM_TOOL})
    profile = old._model.profile.data_policy_id
    data = old._data_policy.model_copy(
        update={"model_tools": old._data_policy.model_tools | {PROM_TOOL}}
    )
    cfg = old._config.model_copy(
        update={
            "purposes": {
                **old._config.purposes,
                "query": old._config.purposes["query"] | {PROM_TOOL},
            },
            "data_policies": {profile: data},
        }
    )
    calls = []

    async def monitor(req):
        # 这里只替代远端读取；Runner、错误分类、账本、Evidence和Session均走产品路径。
        calls.append(req.arguments["query"])
        if failure == "syntax":
            raise PromQLExpressionError(1, 4)
        raise MonitoringReadError("unavailable")

    def fresh():
        evidence = EvidenceStore(
            env.engine,
            combined,
            authorize=env.access.authorize,
            clock=env.clock,
            retention_seconds=3600,
        )
        governed = GovernedTools(evidence)
        app = Application(
            cfg,
            model=old._model,
            engine=env.engine,
            governance=governed,
            local_tools={
                (TOTAL_TOOL, TARGET): env.adapter.execute,
                (QUERY_TOOL, TARGET): env.adapter.execute,
            },
            clock=env.clock,
            actions=ActionService(
                governed, env.store, env.access, [actions.binding], clock=env.clock
            ),
        )
        app._local[PROM_TOOL] = governed_function_tool(
            f"{PROM_SOURCE}__query", contracts[0], governed, monitor
        )
        env.app, env.evidence = app, evidence
        env.results = ResultDelivery(env.store, evidence, env.access, budget=env.results.budget)
        env.service = ChannelService(app, env.results)

    from tests.sdk_core.test_app import answer

    def final(call):
        ids = [
            e["evidence_id"]
            for i in call.input
            if i.get("type") == "function_call_output"
            and "evidence_id" in (e := json.loads(i["output"]))
        ]
        return answer(ids)

    fresh()
    script = env.scripts.add(
        attributed(A, "故障期间申请"),
        tool_call(f"{PROM_SOURCE}__query", query="bad("),
        tool_call(f"{SOURCE}__propose_change", value="after"),
        request_execution,
        tool_call(f"{PROM_SOURCE}__query", query="bad("),
        final,
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("故障期间申请", message_id="om_fail_origin")
    assert calls == ["bad("] and actions.adapter.calls == []
    aid = await actions.one()
    fresh()  # 新GovernedTools模拟进程重启，不能借旧内存账本通过。
    async with running(env, max_reply_chars=3500) as group:
        await group.ask(f"/批准 {aid}", sender=B, message_id="om_fail_resume")
    assert calls == ["bad("] and len(actions.adapter.calls) == 1
    assert await env.rows(
        "SELECT state FROM xiaowei_request WHERE reply_message_id='om_fail_resume'"
    ) == [("completed",)]
    assert PROM_SOURCE in str(group.outbox.sent[-1]) and "未核实" in str(group.outbox.sent[-1])
    assert env.model_calls(script) == 5


async def test_pause_snapshot_and_ledger_roll_back_when_request_transition_fails(actions):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError

    from xiaowei.channel_store import ChannelStoreUnavailableError

    env = actions.env
    env.scripts.add(
        attributed(A, "暂停保存失败"),
        tool_call(f"{SOURCE}__propose_change", value="after"),
        request_execution,
    )
    receipt = await env.service.accept(env.group(attributed(A, "暂停保存失败"), "om_save_pause"))
    observed = []

    def fail(_conn, _cursor, statement, _params, *_args):
        if "INSERT INTO xiaowei_action_run" in statement:
            observed.append("snapshot_and_ledger_inserted")
        if "UPDATE xiaowei_request SET state='paused'" in statement:
            raise SQLAlchemyError("synthetic request transition failure")

    event.listen(env.engine.sync_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(ChannelStoreUnavailableError):
            await env.service.process(receipt)
    finally:
        event.remove(env.engine.sync_engine, "before_cursor_execute", fail)
    assert observed == ["snapshot_and_ledger_inserted"]
    assert not env.store.readiness.ok
    assert await env.rows("SELECT count(*) FROM xiaowei_action_run") == [(0,)]
    assert await env.rows("SELECT state, answer FROM xiaowei_request") == [("running", None)]
    assert await env.rows("SELECT count(*) FROM agent_messages") == [(0,)]
    assert actions.adapter.calls == []


async def test_pause_save_cancellation_locks_readiness_and_rolls_back(actions, monkeypatch):
    import asyncio

    env = actions.env
    env.scripts.add(
        attributed(A, "暂停保存取消"),
        tool_call(f"{SOURCE}__propose_change", value="after"),
        request_execution,
    )
    receipt = await env.service.accept(env.group(attributed(A, "暂停保存取消"), "om_cancel_pause"))
    entered, never = asyncio.Event(), asyncio.Event()

    async def hold(_conn, _identity):
        entered.set()
        await never.wait()

    monkeypatch.setattr(env.store, "_native_session", hold)
    task = asyncio.create_task(env.service.process(receipt))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not env.store.readiness.ok
    assert await env.rows("SELECT count(*) FROM xiaowei_action_run") == [(0,)]
    assert await env.rows("SELECT state, answer FROM xiaowei_request") == [("running", None)]
    assert actions.adapter.calls == []


@pytest.mark.parametrize("damage", ["binding", "ledger", "sdk_state"])
async def test_corrupt_or_incompatible_snapshot_cannot_resume_model_or_write(actions, damage):
    from sqlalchemy import text

    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        aid, script = await paused(actions, group)
        statement = {
            "binding": "UPDATE xiaowei_action_run SET binding=:invalid",
            "ledger": "UPDATE xiaowei_action_run SET ledger=:invalid",
            "sdk_state": "UPDATE xiaowei_action_run SET run_state=:invalid",
        }[damage]
        async with env.engine.begin() as conn:
            await conn.execute(text(statement), {"invalid": "invalid"})
        await group.ask(f"/批准 {aid}", sender=B)
    assert env.model_calls(script) == 2 and actions.adapter.calls == []
    assert await env.rows("SELECT consumed FROM xiaowei_action_run") == [(False,)]
    assert await env.rows("SELECT count(*) FROM agent_messages") == [(0,)]


async def test_waiting_releases_group_and_resume_commits_after_other_investigation(actions):
    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        aid, original = await paused(actions, group)
        investigation = env.scripts.add(
            attributed(B, "等待时查订单"), tool_call("order_total", region="east"), cite()
        )
        await group.ask("等待时查订单", sender=B, message_id="om_while_paused")
        assert env.model_calls(investigation) == 2 and len(env.adapter.calls) == 1
        assert actions.adapter.calls == []
        assert await env.rows("SELECT turns FROM xiaowei_session") == [(1,)]
        await group.ask(f"/批准 {aid}", sender=B, message_id="om_after_wait")
    assert len(actions.adapter.calls) == 1 and env.model_calls(original) == 3
    assert await env.rows("SELECT turns FROM xiaowei_session") == [(2,)]
    assert "等待时查订单" not in str(env.scripts.calls[original][-1].input)
    assert await env.rows(
        "SELECT state FROM xiaowei_request WHERE reply_message_id='om_after_wait'"
    ) == [("completed",)]
    rows = await env.rows("SELECT message_data FROM agent_messages ORDER BY id")
    assert any("等待时查订单" in str(json.loads(r[0])) for r in rows) and any(
        "SDK 静默请求" in str(json.loads(r[0])) for r in rows
    )


async def test_native_binding_keeps_execution_actor_separate_and_snapshot_contains_no_raw_result(
    actions,
):
    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        aid, _script = await paused(actions, group)
        ((saved_ctx, ledger, state),) = await env.rows(
            "SELECT context, ledger, run_state FROM xiaowei_action_run"
        )
        context = json.loads(saved_ctx)
        assert context["identity"]["subject_id"] == A and context["approved_action"] is None
        assert json.loads(ledger)["used"] == 1
        assert 0 < context["budget"]["timeout_seconds"] < env.results.budget.timeout_seconds
        assert "api_key" not in saved_ctx and "must-not-save" not in state
        await group.ask(f"/批准 {aid}", sender=B)
    assert await env.rows(
        "SELECT subject_id FROM xiaowei_evidence WHERE tool_id=:tool", tool=WRITE
    ) == [(B,)]
    assert await env.rows(
        "SELECT subject_id FROM xiaowei_evidence WHERE tool_id=:tool",
        tool=f"{SOURCE}/request_action_execution",
    ) == [(A,)]


async def test_resume_uses_saved_remaining_active_time_not_new_request_budget(actions):
    import asyncio
    import time

    from sqlalchemy import text

    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        aid, script = await paused(actions, group)
        ((raw,),) = await env.rows("SELECT context FROM xiaowei_action_run")
        saved = json.loads(raw)
        # 留足从 PG 检查点走到 Adapter 的调度余量；仍明显短于新批准请求的 30 秒。
        saved["budget"]["timeout_seconds"] = 2.0
        async with env.engine.begin() as conn:
            await conn.execute(
                text("UPDATE xiaowei_action_run SET context=:ctx"), {"ctx": json.dumps(saved)}
            )
        # 模拟可信检查点只剩 2 秒；新批准请求仍有 30 秒，不能重新发满额。
        actions.adapter.read_entered, actions.adapter.read_release = (
            asyncio.Event(),
            asyncio.Event(),
        )
        began = time.monotonic()
        await asyncio.wait_for(group.ask(f"/批准 {aid}", sender=B), 6)
        assert time.monotonic() - began < 6
    assert actions.adapter.read_entered.is_set()
    assert env.store.readiness.ok
    assert actions.adapter.calls == [] and env.model_calls(script) == 2
    assert await env.rows("SELECT state FROM xiaowei_action") == [("rejected",)]
    assert await env.rows("SELECT consumed FROM xiaowei_action_run") == [(True,)]


async def test_near_history_limit_and_maximum_allowed_signature_survive_native_pause(actions):
    from agents.testing import function_call
    from tests.sdk_core.synthetic_tools import TARGET

    from xiaowei.app import Application
    from xiaowei.channel import ChannelService
    from xiaowei.model_api import open_model

    env, old = actions.env, actions.env.app
    profile = old._model.profile.model_copy(
        update={"max_request_bytes": 512000, "max_response_bytes": 512000}
    )
    limits = old._config.session_limits.model_copy(update={"max_history_bytes": 90000})
    signature = "synthetic-signature-" + "s" * (65536 - len("synthetic-signature-"))

    def signed_proposal(call):
        return [
            function_call(
                f"{SOURCE}__propose_change", {"value": "after"}, call_id="max-signed-proposal"
            ).model_copy(update={"provider_data": {"thought_signature": signature}})
        ]

    async with open_model(profile, transport=env.scripts.transport()) as model:
        env.app = Application(
            old._config.model_copy(update={"session_limits": limits}),
            model=model,
            engine=env.engine,
            governance=actions.governance,
            clock=env.clock,
            actions=old.actions,
            local_tools={
                (TOTAL_TOOL, TARGET): env.adapter.execute,
                (QUERY_TOOL, TARGET): env.adapter.execute,
            },
        )
        env.service = ChannelService(env.app, env.results)
        async with running(env, max_reply_chars=3500) as group:
            for n in range(4):
                from tests.sdk_core.test_app import clarify

                env.scripts.add(attributed(A, f"历史{n}"), clarify("q" * 1900))
                await group.ask(f"历史{n}", message_id=f"om_history{n}")
            task = env.scripts.add(
                attributed(A, "大签名续跑"), signed_proposal, request_execution, cite()
            )
            await group.ask("大签名续跑", message_id="om_signature_pause")
            aid = await actions.one()
            ((saved,),) = await env.rows("SELECT run_state FROM xiaowei_action_run")
            assert (
                signature in saved
                and len(saved.encode())
                < limits.max_history_bytes * 16 + 6 * 256000 + old._data_policy.input.max_bytes * 16
            )
            await group.ask(f"/批准 {aid}", sender=B, message_id="om_signature_resume")
        assert await env.rows(
            "SELECT state FROM xiaowei_request WHERE reply_message_id='om_signature_resume'"
        ) == [("completed",)]
        assert len(actions.adapter.calls) == 1 and env.model_calls(task) == 3
        # 锁版OpenAI Responses会去掉其他供应商provider_data；内部SDK/Session仍须保留。
        assert signature not in str(env.scripts.calls[task][-1].input)
        rows = await env.rows("SELECT message_data FROM agent_messages ORDER BY id")
        calls = [json.loads(r[0]) for r in rows if json.loads(r[0]).get("type") == "function_call"]
        assert (
            next(c for c in calls if c["call_id"] == "max-signed-proposal")["provider_data"][
                "thought_signature"
            ]
            == signature
        )
        assert await env.rows("SELECT turns FROM xiaowei_session") == [(5,)]
        assert (
            len(json.dumps([json.loads(r[0]) for r in rows], ensure_ascii=False).encode()) > 72000
        )


async def test_vertex_signature_is_forwarded_after_native_resume(actions, monkeypatch):
    import httpx2
    from tests.sdk_core.synthetic_tools import TARGET
    from tests.sdk_core.test_vertex_model import VERTEX, _body

    from xiaowei.app import Application
    from xiaowei.channel import ChannelService
    from xiaowei.model_api import open_model

    env, old = actions.env, actions.env.app
    signature = "v" * 65536
    requests = []

    def endpoint(req):
        body = json.loads(req.content)
        requests.append(body)
        if len(requests) == 1:
            parts = [
                {
                    "functionCall": {
                        "name": f"{SOURCE}__propose_change",
                        "args": {"value": "after"},
                    },
                    "thoughtSignature": signature,
                }
            ]
        else:
            envelopes = [
                json.loads(p["functionResponse"]["response"]["output"])
                for item in body["contents"]
                for p in item["parts"]
                if "functionResponse" in p
            ]
            if len(requests) == 2:
                parts = [
                    {
                        "functionCall": {
                            "name": GATE,
                            "args": {"action_id": envelopes[-1]["data"]["action_id"]},
                        },
                        "thoughtSignature": "bmF0aXZl",
                    }
                ]
            else:
                parts = [
                    {
                        "text": json.dumps(
                            {
                                "evidence_ids": [e["evidence_id"] for e in envelopes],
                                "inferences": [],
                                "clarification": None,
                            }
                        )
                    }
                ]
        return httpx2.Response(200, json=_body(parts))

    monkeypatch.setenv(VERTEX.api_key_ref.removeprefix("env:"), "synthetic-sdk-a-key")
    profile = VERTEX.model_copy(
        update={
            "data_policy_id": old._model.profile.data_policy_id,
            "max_request_bytes": 512000,
            "max_response_bytes": 512000,
        }
    )
    config = old._config.model_copy(
        update={
            "session_limits": old._config.session_limits.model_copy(
                update={"max_history_bytes": 150000}
            )
        }
    )
    async with open_model(profile, transport=httpx2.MockTransport(endpoint)) as binding:
        env.app = Application(
            config,
            model=binding,
            engine=env.engine,
            governance=actions.governance,
            clock=env.clock,
            actions=old.actions,
            local_tools={
                (TOTAL_TOOL, TARGET): env.adapter.execute,
                (QUERY_TOOL, TARGET): env.adapter.execute,
            },
        )
        env.service = ChannelService(env.app, env.results)
        async with running(env, max_reply_chars=3500) as group:
            await group.ask("带签名申请", message_id="om_vertex_pause")
            assert len(requests) == 2 and actions.adapter.calls == []
            aid = await actions.one()
            await group.ask(f"/批准 {aid}", sender=B, message_id="om_vertex_resume")
        assert len(requests) == 3 and len(actions.adapter.calls) == 1
        assert await env.rows(
            "SELECT state FROM xiaowei_request WHERE reply_message_id='om_vertex_resume'"
        ) == [("completed",)]
        for body in requests[1:]:
            parts = [p for item in body["contents"] for p in item["parts"] if "functionCall" in p]
            assert (
                next(p for p in parts if p["functionCall"]["name"].endswith("__propose_change"))[
                    "thoughtSignature"
                ]
                == signature
            )


async def test_scope_probe_budget_continues_across_pause_and_refuses_before_probe(
    actions, monkeypatch
):
    from sqlalchemy import text
    from tests.sdk_core.synthetic_tools import TARGET

    from xiaowei.actions import ActionService
    from xiaowei.app import Application
    from xiaowei.channel import ChannelService, ResultDelivery
    from xiaowei.evidence import EvidenceStore
    from xiaowei.governance import GovernedTools, ToolCatalog
    from xiaowei.models import ObjectDependency

    env, old = actions.env, actions.env.app
    # 首次调查读 + 两次提案、审批请求与写入共需 7 次工具额度。
    env.results._budget = env.results.budget.model_copy(update={"max_tool_calls": 7})
    base = env.evidence.catalog
    policy = base.policy_for(base.contract(TOTAL_TOOL, TARGET))
    changed = ToolCatalog(
        base.contracts,
        tuple(
            replace(p, data_scope="sdk-a-scope") if p.policy_id == policy.policy_id else p
            for p in {c.policy_id: base.policy_for(c) for c in base.contracts}.values()
        ),
    )
    probes = []

    async def verify(target, deps):
        probes.append((target, len(deps)))
        return "valid"

    async def scoped(req):
        result = await env.adapter.execute(req)
        return result.model_copy(
            update={
                "dependencies": (
                    ObjectDependency(
                        database="d",
                        name="t",
                        use="listed",
                        type=None,
                        table_id=None,
                        columns=(),
                        replayable=True,
                    ),
                )
            }
        )

    evidence = EvidenceStore(
        env.engine,
        changed,
        authorize=env.access.authorize,
        verify_dependencies=verify,
        clock=env.clock,
        retention_seconds=3600,
    )
    governed = GovernedTools(evidence)
    env.app = Application(
        old._config,
        model=old._model,
        engine=env.engine,
        governance=governed,
        clock=env.clock,
        local_tools={(TOTAL_TOOL, TARGET): scoped, (QUERY_TOOL, TARGET): env.adapter.execute},
        actions=ActionService(governed, env.store, env.access, [actions.binding], clock=env.clock),
    )
    env.evidence = evidence
    env.results = ResultDelivery(env.store, evidence, env.access, budget=env.results.budget)
    env.service = ChannelService(env.app, env.results)
    async with running(env, max_reply_chars=3500) as group:
        captured = []
        save = env.store.save_pause

        async def capture(record, pause, **kwargs):
            captured.append((pause.scope_checks, len(probes)))
            return await save(record, pause, **kwargs)

        monkeypatch.setattr(env.store, "save_pause", capture)
        aid, script = await paused(actions, group, investigate=True)
        assert captured[0][0] == captured[0][1]
        env.scripts.by_message[script] = [
            tool_call(f"{SOURCE}__propose_change", value="second"),
            request_execution,
            cite(),
        ]
        before_resume = len(probes)
        await group.ask(f"/批准 {aid}", sender=B, message_id="om_scope_repause")
        assert len(captured) == 2 and captured[1][0] > captured[0][0]
        # 首次送达在独立计数中复核，不占原调查额度；续跑内部探测必须全部计入。
        assert captured[1][0] - captured[0][0] == captured[1][1] - before_resume
        aid = (await env.rows("SELECT action_id FROM xiaowei_action WHERE state='pending'"))[0][0]
        ((raw, used),) = await env.rows("SELECT context,scope_checks FROM xiaowei_action_run")
        assert used > 0
        saved = json.loads(raw)
        saved["budget"]["max_scope_checks"] = used
        async with env.engine.begin() as conn:
            await conn.execute(
                text("UPDATE xiaowei_action_run SET context=:ctx"), {"ctx": json.dumps(saved)}
            )
        before, model_calls = len(probes), env.model_calls(script)
        await group.ask(f"/批准 {aid}", sender=B)
    assert len(probes) == before  # 预算已满，连许可探测都不能再发；移除计数装载会破坏此断言。
    assert len(actions.adapter.calls) == 1 and env.model_calls(script) == model_calls


async def test_resumed_agent_can_propose_a_second_action_in_the_same_task(actions):
    env = actions.env
    env.results._budget = env.results.budget.model_copy(update={"max_tool_calls": 6})
    script = env.scripts.add(
        attributed(A, "分步两个动作"),
        tool_call(f"{SOURCE}__propose_change", value="first"),
        request_execution,
        tool_call(f"{SOURCE}__propose_change", value="second"),
        request_execution,
        cite(),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("分步两个动作", message_id="om_sequential_origin")
        aid = await actions.one()
        await group.ask(f"/批准 {aid}", sender=B, message_id="om_sequential_first")
        assert await env.rows(
            "SELECT state FROM xiaowei_request WHERE reply_message_id='om_sequential_first'"
        ) == [("paused",)]
        assert await env.rows("SELECT version, consumed FROM xiaowei_action_run") == [(2, False)]
        assert [r.arguments for r in actions.adapter.calls] == [{"value": "first"}]
        ((next_id,),) = await env.rows("SELECT action_id FROM xiaowei_action WHERE state='pending'")
        assert "动作执行事实" in group.outbox.texts()[-1] and aid in group.outbox.texts()[-1]
        await group.ask(f"/批准 {next_id}", sender=B, message_id="om_sequential_second")
    assert env.model_calls(script) == 5
    assert [r.arguments for r in actions.adapter.calls] == [{"value": "first"}, {"value": "second"}]
    assert await env.rows(
        "SELECT DISTINCT requester_id, proposal_turn_id FROM xiaowei_action"
    ) == await env.rows(
        "SELECT subject_id, turn_id FROM xiaowei_request"
        " WHERE reply_message_id='om_sequential_origin'"
    )
    assert await env.rows(
        "SELECT state FROM xiaowei_request WHERE reply_message_id='om_sequential_second'"
    ) == [("completed",)]
    assert await env.rows("SELECT turns FROM xiaowei_session") == [(1,)]


@pytest.mark.parametrize("phase", ["first", "resumed"])
async def test_session_expiry_at_pause_save_has_terminal_request_and_allows_new_session(
    actions, monkeypatch, phase
):
    from datetime import timedelta

    from sqlalchemy import text

    env = actions.env
    if phase == "resumed":
        # 两次提案、审批请求与写入共需 6 次；保留此用例的二次暂停路径。
        env.results._budget = env.results.budget.model_copy(update={"max_tool_calls": 6})
    async with running(env, max_reply_chars=3500) as group:
        if phase == "resumed":
            aid, script = await paused(actions, group)
            env.scripts.by_message[script] = [
                tool_call(f"{SOURCE}__propose_change", value="next"),
                request_execution,
                cite(),
            ]
        else:
            env.scripts.add(
                attributed(A, "将过期"),
                tool_call(f"{SOURCE}__propose_change", value="first"),
                request_execution,
                cite(),
            )
        save = env.store.save_pause

        async def expire(record, pause, **kwargs):
            async with env.engine.begin() as conn:
                await conn.execute(
                    text("UPDATE xiaowei_session SET expires_at=:soon"),
                    {"soon": env.clock() + timedelta(seconds=1)},
                )
            env.clock.now += timedelta(seconds=2)
            return await save(record, pause, **kwargs)

        monkeypatch.setattr(env.store, "save_pause", expire)
        if phase == "resumed":
            await group.ask(f"/批准 {aid}", sender=B, message_id="om_save_expired")
        else:
            await group.ask("将过期", message_id="om_save_expired")
        assert await env.rows(
            "SELECT state, delivery FROM xiaowei_request WHERE reply_message_id='om_save_expired'"
        ) == [("failed", "sent")]
        assert not await env.rows("SELECT 1 FROM xiaowei_request WHERE state='running'")
        assert await env.rows("SELECT COUNT(*) FROM xiaowei_action WHERE state='pending'") == [(0,)]
        assert len(actions.adapter.calls) == (1 if phase == "resumed" else 0)
        if phase == "resumed":
            assert "已执行" in group.outbox.texts()[-1]
        else:
            assert "原会话不可继续" in group.outbox.texts()[-1]
        await group.ask("/新建", message_id="om_after_save_expiry")
        assert "已为本群新建会话" in group.outbox.texts()[-1]
        assert env.store.readiness.ok


async def test_native_execution_receipt_cannot_be_clipped_to_claim_delivery_success(actions):
    from xiaowei.feishu_render import CAPACITY_NOTICE

    env = actions.env
    async with running(env, max_reply_chars=3500) as group:
        aid, script = await paused(actions, group)
        group.gateway._config = group.gateway._config.model_copy(update={"max_reply_chars": 200})
        await group.ask(f"/批准 {aid}", sender=B, message_id="om_small_execution_receipt")
    assert len(actions.adapter.calls) == 1 and env.model_calls(script) == 3
    assert await env.rows("SELECT state FROM xiaowei_action") == [("succeeded",)]
    assert await env.rows(
        "SELECT state,delivery FROM xiaowei_request"
        " WHERE reply_message_id='om_small_execution_receipt'"
    ) == [("completed", "failed")]
    assert group.outbox.texts()[-1] == CAPACITY_NOTICE


async def test_partial_execution_facts_remain_delivered_when_the_other_action_expires(actions):
    from datetime import timedelta

    from agents.testing import function_call

    from xiaowei.session import cleanup_expired

    env = actions.env
    env.results._budget = env.results.budget.model_copy(update={"max_tool_calls": 6})

    def both(call):
        return [
            function_call(
                GATE,
                {"action_id": json.loads(i["output"])["data"]["action_id"]},
                call_id=f"gate-expiry-{n}",
            )
            for n, i in enumerate(call.input)
            if i.get("type") == "function_call_output"
        ]

    script = env.scripts.add(
        attributed(A, "两项等待"),
        tool_call(f"{SOURCE}__propose_change", value="one"),
        tool_call(f"{SOURCE}__propose_change", value="two"),
        both,
        cite(),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("两项等待", message_id="om_partial_expire")
        ids = [
            row[0]
            for row in await env.rows("SELECT action_id FROM xiaowei_action ORDER BY call_id")
        ]
        await group.ask(f"/批准 {ids[0]}", sender=B, message_id="om_partial_fact")
        receipt = group.outbox.texts()[-1]
        assert ids[0] in receipt and "succeeded" in receipt and "动作执行事实" in receipt
        env.clock.now += timedelta(minutes=16)
        await cleanup_expired(env.engine, now=env.clock(), batch_size=1)
        await group.ask(f"/批准 {ids[1]}", sender=B)
        view = await env.results.view(group_ref("om_partial_fact", B), first=True)
        assert view.delivery is not None
        assert any(
            f.metadata.get("action_id") == ids[0] and f.metadata.get("state") == "succeeded"
            for f in view.delivery.facts
        )
    assert len(actions.adapter.calls) == 1 and env.model_calls(script) == 3
    assert await env.rows("SELECT state FROM xiaowei_action ORDER BY call_id") == [
        ("succeeded",),
        ("rejected",),
    ]


@pytest.mark.parametrize("cause", ["oversize", "checkpoint_version"])
async def test_pause_save_deterministic_rejection_settles_request_and_closes_session(
    actions, monkeypatch, cause
):
    env = actions.env
    save = env.store.save_pause

    async def refuse(record, pause, **kwargs):
        if cause == "oversize":
            env.store._max_answer_bytes = 128
        else:
            pause = replace(pause, prior_version=1)
        return await save(record, pause, **kwargs)

    monkeypatch.setattr(env.store, "save_pause", refuse)
    env.scripts.add(
        attributed(A, "保存确定拒绝"),
        tool_call(f"{SOURCE}__propose_change", value="after"),
        request_execution,
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("保存确定拒绝", message_id="om_pause_save_refused")
        assert await env.rows(
            "SELECT state,delivery,failure_code FROM xiaowei_request"
            " WHERE reply_message_id='om_pause_save_refused'"
        ) == [("failed", "sent", "result_not_saved")]
        assert await env.rows("SELECT state FROM xiaowei_session") == [("closed",)]
        await group.ask("/新建", message_id="om_after_save_refusal")
        assert "已为本群新建会话" in group.outbox.texts()[-1]
    assert env.store.readiness.ok and actions.adapter.calls == []
    assert await env.rows("SELECT COUNT(*) FROM xiaowei_action_run") == [(0,)]
    assert await env.rows("SELECT COUNT(*) FROM agent_messages") == [(0,)]


async def test_revoking_unused_read_tool_keeps_partial_approval_resumable(actions):
    from agents.testing import function_call

    env = actions.env
    env.results._budget = env.results.budget.model_copy(update={"max_tool_calls": 6})

    def both(call):
        return [
            function_call(
                GATE,
                {"action_id": json.loads(i["output"])["data"]["action_id"]},
                call_id=f"shrink-{n}",
            )
            for n, i in enumerate(call.input)
            if i.get("type") == "function_call_output"
        ]

    script = env.scripts.add(
        attributed(A, "收窄后两项"),
        tool_call(f"{SOURCE}__propose_change", value="one"),
        tool_call(f"{SOURCE}__propose_change", value="two"),
        both,
        cite(),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("收窄后两项", message_id="om_shrink_origin")
        ((prior_binding,),) = await env.rows("SELECT binding FROM xiaowei_action_run")
        ids = [
            row[0]
            for row in await env.rows("SELECT action_id FROM xiaowei_action ORDER BY call_id")
        ]
        env.access._group = replace(env.access._group, tools=env.access._group.tools - {TOTAL_TOOL})
        await group.ask(f"/批准 {ids[0]}", sender=B, message_id="om_shrink_first")
        assert await env.rows(
            "SELECT state,delivery FROM xiaowei_request WHERE reply_message_id='om_shrink_first'"
        ) == [("paused", "sent")]
        ((version, consumed, context, new_binding),) = await env.rows(
            "SELECT version, consumed, context, binding FROM xiaowei_action_run"
        )
        assert version == 2 and not consumed and new_binding != prior_binding
        assert TOTAL_TOOL not in json.loads(context)["tool_scope"]
        await group.ask(f"/批准 {ids[1]}", sender=B, message_id="om_shrink_second")
    assert await env.rows(
        "SELECT state FROM xiaowei_request WHERE reply_message_id='om_shrink_second'"
    ) == [("completed",)]
    assert env.model_calls(script) == 4 and env.adapter.calls == []
    assert "order_total" not in env.scripts.tools_seen(script)[-1]
    assert len(actions.adapter.calls) == 2
    assert await env.rows("SELECT state FROM xiaowei_action ORDER BY call_id") == [
        ("succeeded",),
        ("succeeded",),
    ]
    assert await env.rows("SELECT turns FROM xiaowei_session") == [(1,)]
