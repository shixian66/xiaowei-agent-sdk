"""W5：群审批命令使用真实入口、存储与治理路径，不进入模型。"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import timedelta

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.engine import URL
from tests.sdk_core.synthetic_tools import (
    QUERY_TOOL,
    TARGET,
    TOTAL_TOOL,
    Clock,
    RecordingAdapter,
    catalog,
    ready_engine,
)
from tests.sdk_core.test_app import ModelCall, Scripts, cite, clarify, tool_call
from tests.sdk_core.test_channel_service import FAKE_MODEL_KEY, app_config
from tests.sdk_core.test_group_gateway import running
from tests.sdk_core.test_group_identity import GROUP, TOOLS, A, B, Env, Members
from tests.sdk_core.test_group_identity import env as env  # pytest fixture
from tests.sdk_core.test_model_api import OPENAI as PROFILE

from xiaowei.actions import (
    ActionBinding,
    ActionPlan,
    ActionService,
    action_status_catalog,
    proposal_policy,
)
from xiaowei.app import Application, DataPolicy
from xiaowei.dolphinscheduler import DolphinScheduler, DSConfig
from xiaowei.dolphinscheduler import catalog as ds_catalog
from xiaowei.evidence import EvidenceStore
from xiaowei.feishu import attributed
from xiaowei.governance import GovernedTools, Projection, ToolCatalog, ToolPolicy
from xiaowei.model_api import open_model
from xiaowei.models import (
    AUDIENCES,
    ToolContract,
    ToolObservation,
    ToolRequest,
)
from xiaowei.runtime import AccessConfig, GroupAccess, StaticAccess
from xiaowei.session import SessionInputPolicy
from xiaowei.storage import InstanceLock, hold_instance_lock

pytestmark = pytest.mark.loopback

SOURCE = "am-synthetic"
PROPOSE = f"{SOURCE}/propose_change"
WRITE = f"{SOURCE}/change"
STATUS = f"{SOURCE}/get_action_status"
SHARED = TOOLS | {PROPOSE, STATUS}


def request_execution(call: ModelCall):
    """脚本Agent对实际提出的Action请求SDK批准；准备拒绝则如实收尾。"""
    names = {i["call_id"]: i["name"] for i in call.input if i.get("type") == "function_call"}
    for item in reversed(call.input):
        if item.get("type") != "function_call_output":
            continue
        data = json.loads(item["output"]).get("data", {})
        if "action_id" in data:
            source = names[item["call_id"]].partition("__")[0]
            return tool_call(f"{source}__request_action_execution", action_id=data["action_id"])(
                call
            )
    return cite("没有形成可执行候选")(call)


class ChangeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str


@dataclass
class ActionAdapter:
    clock: Clock
    revision: str = "v1"
    calls: list[ToolRequest] = field(default_factory=list)
    reads: list[ToolRequest] = field(default_factory=list)
    entered: asyncio.Event | None = None
    release: asyncio.Event | None = None
    fail: bool = False
    read_entered: asyncio.Event | None = None
    read_release: asyncio.Event | None = None

    async def prepare(self, req: ToolRequest) -> ActionPlan:
        self.reads.append(req)
        if self.read_entered is not None:
            self.read_entered.set()
        if self.read_release is not None:
            await self.read_release.wait()
        return ActionPlan(
            parameters=req.arguments,
            revision=self.revision,
            change=f"before → {req.arguments['value']}",
            impact="仅合成对象",
            rollback="经新一次批准恢复 before",
        )

    async def execute(self, req: ToolRequest) -> ToolObservation:
        self.calls.append(req)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.fail:
            raise TimeoutError
        return ToolObservation(
            payload={"value": req.arguments["value"], "raw_secret": "must-not-save"},
            captured_at=self.clock(),
            truncated=False,
        )


@dataclass
class Actions:
    env: Env
    adapter: ActionAdapter
    governance: GovernedTools
    lock: InstanceLock
    binding: ActionBinding

    def rebind(self, **changes) -> None:
        self.binding = replace(self.binding, **changes)
        self.env.app.actions = ActionService(
            self.governance,
            self.env.store,
            self.env.access,
            [self.binding],
            clock=self.env.clock,
        )

    async def one(self) -> str:
        ((action_id,),) = await self.env.rows("SELECT action_id FROM xiaowei_action")
        return action_id


@pytest.fixture
async def actions(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[Actions]:
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), FAKE_MODEL_KEY)
    async with ready_engine(postgres_url) as engine:
        clock, members = Clock(), Members()
        stack = ExitStack()
        request.addfinalizer(stack.close)
        ds, ds_contracts, ds_policies = None, (), ()
        ds_tools, ds_targets = frozenset(), frozenset()
        if getattr(request, "param", None) == "ds":
            from tests.sdk_core.test_dolphinscheduler_runtime import DS_TOOLS, ds_api, source

            monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
            url, _ = stack.enter_context(ds_api())
            sources = [DSConfig.model_validate(source(base_url=url))]
            ds = DolphinScheduler(sources, clock)
            ds_contracts, ds_policies = ds_catalog(sources, dict.fromkeys(AUDIENCES, 20_000))
            ds_tools = frozenset(DS_TOOLS)
            ds_targets = frozenset({"ds-b", "dolphinscheduler"})
        shared = SHARED | ds_tools
        access = StaticAccess(
            AccessConfig(policy_version="w5", grants={A: shared | {WRITE}, B: shared | {WRITE}}),
            frozenset({TARGET, SOURCE}) | ds_targets,
            GroupAccess(scope=GROUP, tools=shared, members=members),
            write_tools=frozenset({WRITE}),
        )
        base = catalog()
        if isinstance(feishu_bytes := getattr(request, "param", None), int):
            base = ToolCatalog(
                base.contracts,
                [
                    replace(
                        base.policy_for(c),
                        projections={
                            a: Projection(
                                ("region", "total", "rows")
                                if a == "feishu"
                                else ("region", "total"),
                                feishu_bytes if a == "feishu" else 400,
                            )
                            for a in AUDIENCES
                        },
                    )
                    for c in base.contracts
                ],
            )
        propose = ToolContract(
            tool_id=PROPOSE,
            target_id=SOURCE,
            policy_id="fixture.proposal",
            input_schema=ChangeArgs.model_json_schema(),
            description="提出合成对象修改，不能执行写入",
        )
        write = ToolContract(
            tool_id=WRITE,
            target_id=SOURCE,
            policy_id="fixture.write",
            input_schema=ChangeArgs.model_json_schema(),
        )
        statuses, status_policies = action_status_catalog([SOURCE], 10_000)
        policies = [base.policy_for(c) for c in base.contracts]
        combined = ToolCatalog(
            (*base.contracts, propose, write, *statuses, *ds_contracts),
            (
                *policies,
                proposal_policy(propose.policy_id, ChangeArgs, 10_000),
                ToolPolicy(
                    write.policy_id,
                    ChangeArgs,
                    {a: Projection(("value",), 10_000) for a in AUDIENCES},
                    effect="write",
                ),
                *status_policies,
                *ds_policies,
            ),
        )
        evidence = EvidenceStore(
            engine, combined, authorize=access.authorize, clock=clock, retention_seconds=3600
        )
        governance = GovernedTools(evidence)
        adapter, scripts = RecordingAdapter(), Scripts()
        async with open_model(PROFILE, transport=scripts.transport()) as bound:
            app = Application(
                app_config(
                    purposes={"query": shared | {WRITE}, "diagnose": frozenset({TOTAL_TOOL})},
                    data_policies={
                        PROFILE.data_policy_id: DataPolicy(
                            input=SessionInputPolicy(max_bytes=500), model_tools=shared | {WRITE}
                        )
                    },
                ),
                model=bound,
                engine=engine,
                governance=governance,
                local_tools={
                    (TOTAL_TOOL, TARGET): adapter.execute,
                    (QUERY_TOOL, TARGET): adapter.execute,
                },
                clock=clock,
                dolphinscheduler=ds,
            )
            env = Env(engine, clock, members, access, adapter, scripts, evidence, app)
            action_adapter = ActionAdapter(clock)
            binding = ActionBinding(
                propose,
                write,
                action_adapter.prepare,
                action_adapter.execute,
                target_binding="synthetic-v1",
            )
            app.actions = ActionService(
                governance,
                env.store,
                access,
                [binding],
                clock=clock,
            )
            async with hold_instance_lock(engine, env.store.readiness) as lock:
                await env.store.recover(lock)
                try:
                    yield Actions(env, action_adapter, governance, lock, binding)
                finally:
                    if ds is not None:
                        await ds.aclose()


@pytest.mark.parametrize("verb", ["批准", "拒绝"])
@pytest.mark.parametrize("separator", [" ", ""])
async def test_approval_command_never_becomes_a_model_question(env: Env, verb, separator) -> None:
    command = f"/{verb}{separator}00000000-0000-4000-8000-000000000001"
    asked = env.scripts.add(attributed(A, command), clarify())
    async with running(env) as group:
        assert group.gateway.inbound(group.event(command)) is None
        await group.ask(command)
    assert env.model_calls(asked) == 0
    assert env.adapter.calls == []
    assert len(group.outbox.sent) == 1


@pytest.mark.parametrize("message", ["/批准了吗", "/批准abc", "/拒绝了吗", "/拒绝abc"])
async def test_approval_like_question_remains_a_model_question(env: Env, message) -> None:
    asked = env.scripts.add(attributed(A, message), clarify())
    async with running(env) as group:
        assert group.gateway.inbound(group.event(message)) is not None
        await group.ask(message)
    assert env.model_calls(asked) == 1
    assert env.adapter.calls == []


@pytest.mark.parametrize("verb", ["批准", "拒绝"])
@pytest.mark.parametrize("body", ["", " abc", " 00000000-0000-4000-8000-000000000001 extra"])
async def test_explicit_malformed_approval_returns_format_receipt_without_model(
    env: Env, verb, body,
):
    command = f"/{verb}{body}"
    asked = env.scripts.add(attributed(A, command), clarify())
    async with running(env) as group:
        assert group.gateway.inbound(group.event(command)) is None
        await group.ask(command)
    assert env.model_calls(asked) == 0 and env.adapter.calls == []
    assert "本次未执行" in "\n".join(group.outbox.texts())


async def test_propose_pause_approve_once_and_follow_up_with_native_history(
    actions: Actions,
) -> None:
    env = actions.env
    proposed = env.scripts.add(
        attributed(A, "改为 after"),
        tool_call("am-synthetic__propose_change", value="after"),
        request_execution,
        cite("等待群批准"),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("改为 after", message_id="om_proposal")
        assert actions.adapter.calls == []
        action_id = await actions.one()
        before = await env.rows("SELECT message_data FROM agent_messages ORDER BY id")
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_approval")
        assert [r.arguments for r in actions.adapter.calls] == [{"value": "after"}]
        assert before == []
        assert await env.rows("SELECT message_data FROM agent_messages ORDER BY id") != before
        assert await env.rows("SELECT state, approver_id, result FROM xiaowei_action") == [
            ("succeeded", B, '{"value": "after"}')
        ]
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_approval")
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_again")
        assert len(actions.adapter.calls) == 1
        follow = env.scripts.add(
            attributed(A, "动作怎样了"),
            tool_call("am-synthetic__get_action_status", action_id=action_id),
            cite(),
        )
        await group.ask("动作怎样了", message_id="om_status")
    assert env.model_calls(proposed) == 3 and env.model_calls(follow) == 2
    for call in env.scripts.calls[proposed]:
        assert "提出工具只准备材料、尚未写入" in call.instructions
        assert "有权群成员明确批准" in call.instructions
    assert all("am-synthetic__change" not in names for names in env.scripts.tools_seen(proposed))
    rows = await env.rows(
        "SELECT state, delivery FROM xiaowei_request WHERE reply_message_id=:id", id="om_approval"
    )
    assert rows == [("completed", "sent")]


@pytest.mark.parametrize("failure", ["not_sent", "not_approver", "left", "expired", "changed"])
async def test_invalid_approval_has_zero_writes(actions: Actions, failure: str) -> None:
    env = actions.env
    env.scripts.add(
        attributed(A, "提出"),
        tool_call("am-synthetic__propose_change", value="after"),
        request_execution,
        cite(),
    )
    async with running(env, max_reply_chars=3500) as group:
        await group.ask("提出", message_id="om_p")
        action_id = await actions.one()
        if failure == "not_sent":
            async with env.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE xiaowei_request SET delivery='failed', delivery_version=0 "
                        "WHERE reply_message_id='om_p'"
                    )
                )
        elif failure == "not_approver":
            env.access._grants = {A: SHARED}  # 当前唯一授权表撤权。
        elif failure == "left":
            env.members.current.remove(A)
        elif failure == "expired":
            env.clock.now += timedelta(minutes=16)
        elif failure == "changed":
            actions.adapter.revision = "v2"
        await group.ask(f"/批准 {action_id}", message_id="om_a")
    assert actions.adapter.calls == []
    assert await env.rows("SELECT state FROM xiaowei_request WHERE reply_message_id='om_a'") == [
        ("failed",)
    ]


@pytest.mark.parametrize("actions", ["ds"], indirect=True)
@pytest.mark.parametrize("fail_after_resume", [False, True])
async def test_ds_selection_survives_native_pause_and_commits_only_after_resume(
    actions, fail_after_resume
):
    from tests.sdk_core.test_dolphinscheduler_runtime import ds_answer, list_failed

    env = actions.env
    env.scripts.add(
        attributed(A, "查 DS B 后提出监控动作"),
        tool_call("ds_select_clusters", clusters=["ds-b"]),
        list_failed(),
        tool_call("am-synthetic__propose_change", value="after"),
        request_execution,
        tool_call("missing_tool_after_resume") if fail_after_resume else ds_answer,
    )
    async with running(env) as group:
        await group.ask("查 DS B 后提出监控动作", message_id="om_ds_pause")
        assert await env.rows("SELECT ds_clusters FROM xiaowei_session") == [(None,)]
        action_id = await actions.one()
        assert actions.adapter.calls == []
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_ds_resume")
        assert len(actions.adapter.calls) == 1
        assert await env.rows("SELECT ds_clusters FROM xiaowei_session") == [
            (None if fail_after_resume else ["ds-b"],)
        ]
        assert await env.rows("SELECT state FROM xiaowei_session") == [("active",)]
