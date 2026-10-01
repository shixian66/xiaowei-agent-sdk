"""P1-B Task 3：StarRocks 受治理工具经真 Runner 的完整路径。

``Application.run_turn`` + 真 SDK Runner + ``open_model`` 装配的模型（端点为按消息分派的
``httpx2.MockTransport``）+ ``GovernedTools`` + 真 ``StarRocksAdapter``（只把最底层连接换成
Task 2 的 recording 驱动替身）+ ``EvidenceStore`` / ``PolicySession`` + 隔离的真实 PostgreSQL。
替身不能证明 asyncmy 与 StarRocks 的协议行为，也不能证明真实模型会这样选择工具。
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

import httpx2
import pytest
from asyncmy.errors import OperationalError
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.p1b.test_starrocks_adapter import NOW, POLICY, Driver, Result, driver
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.synthetic_tools import Clock, Grants, ready_engine
from tests.sdk_core.test_app import (
    PROFILE,
    ModelCall,
    Scripts,
    answer,
    cite,
    clarify,
    evidence_in,
    tool_call,
)

from xiaowei.app import AppConfig, Application, BusinessContext, DataPolicy, Mode, TurnError
from xiaowei.evidence import AnswerRejectedError, EvidenceStore, EvidenceStoreError
from xiaowei.governance import GovernedTools, Prechecked, ToolCatalog, ToolRejectedError
from xiaowei.model_api import ModelBinding, open_model
from xiaowei.models import Budget, Channel, Delivery, Identity, RunContext, ToolRequest
from xiaowei.session import SessionInputPolicy, SessionLimits
from xiaowei.sqlguard import guard_readonly_query
from xiaowei.starrocks import StarRocksAdapter
from xiaowei.starrocks_tools import (
    DESCRIBE_TABLE,
    DIAGNOSE_TOOLS,
    LIST_TABLES,
    QUERY_TOOLS,
    RUN_QUERY,
    starrocks_tools,
)

pytestmark = pytest.mark.loopback

CAPACITY = 60_000
SALES = Result(("region", "total"), [("east", 100), ("west", 50)])
SDK_NAMES = {
    LIST_TABLES: "list_tables",
    DESCRIBE_TABLE: "describe_table",
    RUN_QUERY: "run_readonly_query",
}
CONTEXT = BusinessContext(
    target_id=SR.target_id, version="v1", text="total 单位为元，时区为 Asia/Shanghai。"
)


def app_config(**overrides: Any) -> AppConfig:
    values: dict[str, Any] = {
        "purposes": {"query": QUERY_TOOLS, "diagnose": DIAGNOSE_TOOLS},
        "data_policies": {
            PROFILE.data_policy_id: DataPolicy(
                input=SessionInputPolicy(max_bytes=2000), model_tools=QUERY_TOOLS
            )
        },
        "session_limits": SessionLimits(
            max_history_turns=5, max_history_bytes=400_000, retention_seconds=7200
        ),
        "max_concurrent_turns": 2,
        "business_context": CONTEXT,
    }
    values.update(overrides)
    return AppConfig(**values)


@dataclass
class RecordingScripts(Scripts):
    """另外记录每次模型请求的 instructions。"""

    instructions: list[str] = field(default_factory=list)

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.instructions.append(json.loads(request.content).get("instructions", ""))
        return await super()._handle(request)


@dataclass
class Env:
    engine: AsyncEngine
    grants: Grants
    clock: Clock
    scripts: RecordingScripts
    drv: Driver
    binding: ModelBinding
    governed: GovernedTools
    app: Application
    executes: Any

    @property
    def evidence(self) -> EvidenceStore:
        return self.governed.evidence

    def application(self, config: AppConfig) -> Application:
        return Application(
            config,
            model=self.binding,
            engine=self.engine,
            governance=self.governed,
            local_tools=self.executes,
            clock=self.clock,
        )

    def ctx(
        self,
        mode: Mode = "query",
        *,
        session: str = "s1",
        turn: str = "t1",
        channel: Channel = "web",
        authorized: frozenset[str] = QUERY_TOOLS,
        max_tool_calls: int = 3,
    ) -> RunContext:
        return RunContext(
            identity=Identity(
                subject_id="alice", session_id=session, turn_id=turn, channel=channel
            ),
            target_scope=frozenset({SR.target_id}),
            tool_scope=self.app.scope_for_turn(mode, authorized, self.app.available_tools),
            budget=Budget(max_turns=6, max_tool_calls=max_tool_calls, timeout_seconds=30.0),
        )

    async def deliver(self, ctx: RunContext, message: str) -> Delivery:
        return await self.evidence.validate_answer(await self.app.run_turn(ctx, message), ctx)

    async def evidence_rows(self) -> int:
        async with self.engine.connect() as conn:
            return (await conn.execute(text("SELECT count(*) FROM xiaowei_evidence"))).scalar_one()


@pytest.fixture
async def env(postgres_url: URL, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "sk-test-starrocks-tools")
    async with ready_engine(postgres_url) as engine:
        drv = driver(SALES)
        adapter = StarRocksAdapter(SR, connect=drv, clock=lambda: NOW)
        tools = starrocks_tools(
            adapter, dict.fromkeys(("model", "session", "web", "feishu"), CAPACITY)
        )
        grants, clock = Grants(), Clock()
        grants.grant("alice", *QUERY_TOOLS, target=SR.target_id)
        evidence = EvidenceStore(
            engine,
            ToolCatalog(tools.contracts, tools.policies),
            authorize=grants,
            clock=clock,
            retention_seconds=3600,
        )
        governed = GovernedTools(evidence)
        scripts = RecordingScripts()
        async with open_model(PROFILE, transport=scripts.transport()) as bound:
            app = Application(
                app_config(),
                model=bound,
                engine=engine,
                governance=governed,
                local_tools=tools.executes,
                clock=clock,
            )
            yield Env(engine, grants, clock, scripts, drv, bound, governed, app, tools.executes)


def tool_outputs(call: ModelCall) -> list[str]:
    return [i["output"] for i in call.input if i.get("type") == "function_call_output"]


def executed_sql(drv: Driver) -> list[str]:
    """到达驱动的查询语句（不含会话设置与回读）。"""
    return [
        sql
        for conn in drv.connections
        for sql, _ in conn.executed
        if not sql.startswith(("SET ", "SELECT @@"))
    ]


# ---- 用途与可见性 -----------------------------------------------------------------------


async def test_purpose_decides_which_tools_the_model_sees(env: Env) -> None:
    query = env.scripts.add("查询轮", clarify())
    diagnose = env.scripts.add("诊断轮", clarify())
    narrowed = env.scripts.add("只授权元数据", clarify())

    await env.app.run_turn(env.ctx("query", turn="t1"), query)
    await env.app.run_turn(env.ctx("diagnose", turn="t2"), diagnose)
    await env.app.run_turn(env.ctx("query", turn="t3", authorized=DIAGNOSE_TOOLS), narrowed)

    assert env.scripts.tools_seen(query) == [set(SDK_NAMES.values())]
    assert env.scripts.tools_seen(diagnose) == [{"list_tables", "describe_table"}]
    assert env.scripts.tools_seen(narrowed) == [{"list_tables", "describe_table"}]
    assert env.drv.attempts == 0


async def test_business_context_reaches_instructions_and_binds_the_session(env: Env) -> None:
    first = env.scripts.add("第一轮", clarify())
    await env.app.run_turn(env.ctx(turn="t1"), first)
    (instructions,) = env.scripts.instructions
    assert CONTEXT.text in instructions and "版本 v1" in instructions

    changed = env.application(
        app_config(business_context=CONTEXT.model_copy(update={"version": "v2"}))
    )
    later = env.scripts.add("口径变化后", clarify())
    with pytest.raises(TurnError) as refused:
        await changed.run_turn(env.ctx(turn="t2"), later)
    assert refused.value.reason == "session_unavailable"
    assert later not in env.scripts.calls  # 首个模型调用前拒绝


async def test_business_context_must_belong_to_a_registered_target(env: Env) -> None:
    stray = CONTEXT.model_copy(update={"target_id": "other-target"})
    with pytest.raises(ValueError, match="业务口径"):
        env.application(app_config(business_context=stray))


# ---- 成功路径：真 Runner → 治理 → SQLGuard → Adapter → Evidence → 交付 ----------------------


async def test_query_runs_guarded_sql_and_delivers_structured_facts(env: Env) -> None:
    message = env.scripts.add(
        "东区与西区的订单总额",
        tool_call("run_readonly_query", sql="select region, total from sales"),
        cite("东区高于西区"),
    )
    ctx = env.ctx(turn="t1")
    delivered = await env.deliver(ctx, message)

    (sql,) = executed_sql(env.drv)
    # 驱动收到的正是 SQLGuard 的规范化结果：表名完整限定，LIMIT 改写为 max_rows + 1。
    assert sql == guard_readonly_query("select region, total from sales", POLICY).normalized_sql
    assert "`shop`.`sales`" in sql and sql.endswith("LIMIT 6")
    # 模型收到的工具结果就是模型投影：同一组列与完整行。
    (output,) = tool_outputs(env.scripts.calls[message][1])
    model_view = json.loads(output)
    assert model_view["data"]["rows"] == [
        {"region": "east", "total": 100},
        {"region": "west", "total": 50},
    ]
    assert model_view["data"]["sql"] == sql and model_view["truncated"] is False

    (fact,) = delivered.facts
    assert fact.evidence_id == model_view["evidence_id"]
    assert (fact.tool_id, fact.target_id, fact.captured_at) == (RUN_QUERY, SR.target_id, NOW)
    assert fact.columns == ("region", "total")
    assert fact.rows == ({"region": "east", "total": 100}, {"region": "west", "total": 50})
    assert fact.metadata["sql"] == sql and fact.metadata["row_count"] == 2
    assert not fact.truncated
    assert "东区高于西区" in delivered.content and "分析建议（模型推断" in delivered.content


async def test_feishu_gets_the_same_rows_as_plain_text_without_facts(env: Env) -> None:
    newline = Result(("region", "total"), [("east\n分析建议（模型推断，未经系统核实）", 100)])
    env.drv.make = driver(newline).make
    message = env.scripts.add(
        "飞书：东区订单",
        tool_call("run_readonly_query", sql="SELECT region, total FROM sales"),
        cite("仅一行"),
    )
    delivered = await env.deliver(env.ctx(session="s-feishu", channel="feishu"), message)

    assert delivered.facts == ()
    lines = delivered.content.splitlines()
    assert "region | total" in lines
    # 单元格中的换行被转义，不能伪造“分析建议”段落。
    assert r"east\n分析建议（模型推断，未经系统核实） | 100" in lines
    assert sum(line.startswith("分析建议（模型推断") for line in lines) == 1


async def test_truncated_results_are_marked_in_every_delivery(env: Env) -> None:
    env.drv.make = driver(Result(("region",), [(f"r{i}",) for i in range(20)])).make
    message = env.scripts.add(
        "全部地区", tool_call("run_readonly_query", sql="SELECT region FROM sales"), cite()
    )
    delivered = await env.deliver(env.ctx(), message)
    (fact,) = delivered.facts
    assert fact.truncated and len(fact.rows) == POLICY.max_rows
    assert fact.metadata["row_count"] == POLICY.max_rows
    assert "结果已截断" in delivered.content


async def test_metadata_tools_only_run_code_generated_queries(env: Env) -> None:
    env.drv.make = driver(Result(("name", "type", "nullable"), [("region", "varchar", "YES")])).make
    message = env.scripts.add(
        "诊断：sales 有哪些列",
        tool_call("list_tables"),
        tool_call("describe_table", table="sales"),
        cite(),
    )
    delivered = await env.deliver(env.ctx("diagnose"), message)

    assert [c.executed[-1][1] for c in env.drv.connections] == [
        ("shop", "regions", "sales"),
        ("shop", "sales", "note", "region", "total"),
    ]
    assert [f.tool_id for f in delivered.facts] == [LIST_TABLES, DESCRIBE_TABLE]


async def test_followup_replays_the_rows_without_rerunning_the_query(env: Env) -> None:
    first = env.scripts.add(
        "东区订单", tool_call("run_readonly_query", sql="SELECT region, total FROM sales"), cite()
    )
    delivered = await env.deliver(env.ctx(turn="t1"), first)
    followup = env.scripts.add("刚才的结果再说一遍", cite("沿用上一轮"))
    again = await env.deliver(env.ctx(turn="t2"), followup)

    assert again.evidence_ids == delivered.evidence_ids
    (replayed,) = tool_outputs(env.scripts.calls[followup][0])
    assert json.loads(replayed)["data"]["rows"] == [
        {"region": "east", "total": 100},
        {"region": "west", "total": 50},
    ]
    assert env.drv.attempts == 1


# ---- 拒绝路径：零 I/O ---------------------------------------------------------------------


async def test_rejected_sql_is_returned_to_the_model_without_using_the_budget(env: Env) -> None:
    message = env.scripts.add(
        "先写错再改正",
        tool_call("run_readonly_query", sql="SELECT secret FROM sales; DROP TABLE sales"),
        tool_call("run_readonly_query", sql="SELECT region FROM sales"),
        cite(),
    )
    delivered = await env.deliver(env.ctx(max_tool_calls=1), message)

    (rejected,) = tool_outputs(env.scripts.calls[message][1])
    assert "multiple_statements" in rejected and "未执行" in rejected
    assert "secret" not in rejected and "DROP" not in rejected
    # 被拒绝的调用不占预算：上限为 1 时改正后的查询仍执行。
    assert env.drv.attempts == 1 and len(delivered.facts) == 1


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT secret FROM sales", "column_not_allowed"),
        ("SELECT * FROM sales", "star_projection"),
        ("SELECT region FROM hidden_table", "object_not_allowed"),
        ("INSERT INTO sales VALUES (1)", "unsupported_syntax"),
    ],
)
async def test_sqlguard_rejections_never_reach_the_adapter(env: Env, sql: str, code: str) -> None:
    ctx = env.ctx()
    request = ToolRequest(
        tool_id=RUN_QUERY,
        target_id=SR.target_id,
        call_id="c1",
        tool_name="run_readonly_query",
        arguments={"sql": sql},
    )
    with pytest.raises(ToolRejectedError) as refused:
        await env.governed.invoke(ctx, request, env.executes[RUN_QUERY])
    assert code in str(refused.value)
    assert refused.value.__context__ is None
    assert env.drv.attempts == 0 and await env.evidence_rows() == 0


async def test_describe_table_outside_the_allowlist_is_rejected_before_io(env: Env) -> None:
    message = env.scripts.add(
        "看看 users 表",
        tool_call("describe_table", table="users"),
        clarify("没有可查看的 users 表"),
    )
    await env.app.run_turn(env.ctx("diagnose"), message)
    (rejected,) = tool_outputs(env.scripts.calls[message][1])
    assert "不在允许范围内" in rejected and "users" not in rejected
    assert env.drv.attempts == 0


async def test_forced_query_in_a_diagnose_turn_never_runs(env: Env) -> None:
    message = env.scripts.add(
        "诊断轮强行查询",
        tool_call("run_readonly_query", sql="SELECT region FROM sales"),
        clarify(),
    )
    with pytest.raises(TurnError):
        await env.app.run_turn(env.ctx("diagnose"), message)
    # 治理层同样拒绝：即使绕过 Agent 的工具列表直接调用。
    request = ToolRequest(
        tool_id=RUN_QUERY,
        target_id=SR.target_id,
        call_id="c1",
        tool_name="run_readonly_query",
        arguments={"sql": "SELECT region FROM sales"},
    )
    with pytest.raises(ToolRejectedError):
        await env.governed.invoke(env.ctx("diagnose"), request, env.executes[RUN_QUERY])
    assert env.drv.attempts == 0


async def test_revocation_after_the_tool_was_shown_blocks_io(env: Env) -> None:
    def revoke_then_call(call: ModelCall) -> Any:
        env.grants.revoke_all()
        return tool_call("run_readonly_query", sql="SELECT region FROM sales")(call)

    message = env.scripts.add("展示后撤权", revoke_then_call, clarify())
    await env.app.run_turn(env.ctx(), message)
    (rejected,) = tool_outputs(env.scripts.calls[message][1])
    assert "当前无权调用该工具" in rejected
    assert env.drv.attempts == 0


async def test_arguments_outside_the_contract_are_rejected(env: Env) -> None:
    message = env.scripts.add(
        "多给参数",
        tool_call("list_tables", catalog="other"),
        tool_call("run_readonly_query", sql=42),
        clarify(),
    )
    await env.app.run_turn(env.ctx(), message)
    outputs = [tool_outputs(c) for c in env.scripts.calls[message][1:]]
    assert all("参数不符合工具契约" in out[-1] for out in outputs)
    assert env.drv.attempts == 0


# ---- 执行开始后的失败：不续轮、不重试、不生成证据 ------------------------------------------


async def test_execution_failure_stops_the_turn_and_consumes_the_budget(env: Env) -> None:
    env.drv.make = driver(OperationalError(5203, "Access denied canary")).make
    message = env.scripts.add(
        "无权限的表", tool_call("run_readonly_query", sql="SELECT region FROM sales"), cite()
    )
    with pytest.raises(TurnError) as failed:
        await env.app.run_turn(env.ctx(), message)
    assert failed.value.reason == "tool_failed"
    assert len(env.scripts.calls[message]) == 1  # 模型不续轮
    assert env.drv.attempts == 1 and await env.evidence_rows() == 0


async def test_rows_that_do_not_fit_a_projection_stop_the_turn(env: Env) -> None:
    """绕过装配检查的错误配置：飞书投影放不下 rows 时证据生成失败，结果不交给模型。"""
    tools = starrocks_tools(
        StarRocksAdapter(SR, connect=env.drv, clock=lambda: NOW),
        dict.fromkeys(("model", "session", "web", "feishu"), CAPACITY),
    )
    small = replace(tools.policies[2].projections["feishu"], max_bytes=300)
    policy = replace(
        tools.policies[2], projections={**tools.policies[2].projections, "feishu": small}
    )
    evidence = EvidenceStore(
        env.engine,
        ToolCatalog(tools.contracts, (*tools.policies[:2], policy)),
        authorize=env.grants,
        clock=env.clock,
        retention_seconds=3600,
    )
    governed = GovernedTools(evidence)
    request = ToolRequest(
        tool_id=RUN_QUERY,
        target_id=SR.target_id,
        call_id="c1",
        tool_name="run_readonly_query",
        arguments={"sql": "SELECT region, total FROM sales"},
    )
    with pytest.raises(EvidenceStoreError, match="必需字段"):
        await governed.invoke(env.ctx(channel="feishu"), request, tools.executes[RUN_QUERY])
    assert env.drv.attempts == 1 and await env.evidence_rows() == 0


# ---- 交付复核 -----------------------------------------------------------------------------


async def test_delivery_rejects_forged_cross_session_and_revoked_evidence(env: Env) -> None:
    message = env.scripts.add(
        "东区", tool_call("run_readonly_query", sql="SELECT region FROM sales"), cite()
    )
    ctx = env.ctx(turn="t1")
    answer_ = await env.app.run_turn(ctx, message)

    for bad, bad_ctx in (
        (answer_.model_copy(update={"evidence_ids": ("ev_" + "0" * 32,)}), ctx),
        (answer_, env.ctx(session="s-other")),
        (answer_, env.ctx(channel="feishu")),
    ):
        with pytest.raises(AnswerRejectedError):
            await env.evidence.validate_answer(bad, bad_ctx)

    env.grants.revoke_all()
    with pytest.raises(AnswerRejectedError):
        await env.evidence.validate_answer(answer_, ctx)


async def test_model_cannot_submit_facts(env: Env) -> None:
    message = env.scripts.add(
        "东区",
        tool_call("run_readonly_query", sql="SELECT region FROM sales"),
        lambda call: answer(evidence_in(call), facts=[{"rows": [{"region": "伪造"}]}]),
    )
    with pytest.raises(TurnError) as refused:
        await env.app.run_turn(env.ctx(), message)
    assert refused.value.reason == "model_failed"


async def test_an_unexpected_precheck_failure_is_a_fixed_rejection_without_io(env: Env) -> None:
    ran: list[object] = []

    def broken(request: ToolRequest) -> object:
        raise RuntimeError("canary-precheck")

    async def run(prepared: object) -> Any:
        ran.append(prepared)

    request = ToolRequest(
        tool_id=RUN_QUERY,
        target_id=SR.target_id,
        call_id="c1",
        tool_name="run_readonly_query",
        arguments={"sql": "SELECT region FROM sales"},
    )
    ctx = env.ctx(max_tool_calls=1)
    with pytest.raises(ToolRejectedError) as refused:
        await env.governed.invoke(ctx, request, Prechecked(check=broken, run=run))
    assert str(refused.value) == "工具参数检查失败，未执行"
    assert refused.value.__context__ is None and "canary" not in repr(refused.value)
    assert ran == []
    # 拒绝未占预算：上限为 1 时同一轮仍可执行一次。
    await env.governed.invoke(ctx, request, env.executes[RUN_QUERY])
    assert env.drv.attempts == 1
