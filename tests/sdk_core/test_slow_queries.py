"""P2 Task 6：``list_slow_queries`` 经真 Runner 的完整路径。

真 SDK Runner + mock 模型端点 + ``GovernedTools`` + 真 ``StarRocksAdapter``（最底层连接为
recording 替身）+ ``EvidenceStore`` / ``PolicySession`` + 隔离的真实 PostgreSQL。替身不能证明
AuditLoader 的实际写入，也不能证明真实模型会这样选择工具。
"""

import json
from dataclasses import dataclass, replace
from typing import Any

import pytest
from asyncmy.errors import OperationalError, ProgrammingError
from sqlalchemy import text
from tests.p1b.test_starrocks_adapter import NOW, Result, driver, ready_schema
from tests.p1b.test_starrocks_audit import ACCEPTED, AUDIT_TARGET, CANARY, REJECTED, SOURCE, record
from tests.p1b.test_starrocks_audit import target as audit_target
from tests.sdk_core.test_app import PROFILE, ModelCall, cite, clarify, tool_call
from tests.sdk_core.test_starrocks_tools import (
    CAPACITY,
    PLAN,
    Env,
    app_config,
    executed_sql,
    tool_outputs,
)
from tests.sdk_core.test_starrocks_tools import env as env  # pytest fixture

from xiaowei.app import Application, DataPolicy, Mode, TurnError
from xiaowei.evidence import AnswerRejectedError, EvidenceStore, EvidenceUnavailableError
from xiaowei.feishu_render import build_feishu_message, request_bytes
from xiaowei.governance import GovernedTools, Projection, ToolCatalog, ToolRejectedError
from xiaowei.models import AUDIENCES, Budget, Channel, Identity, RunContext, ToolRequest, TurnAnswer
from xiaowei.session import SessionInputPolicy
from xiaowei.starrocks import (
    AUDIT_COLUMNS,
    EXPLAIN_PREFIX,
    StarRocksAdapter,
    StarRocksTarget,
    probe_sql,
)
from xiaowei.starrocks_schema import DependencyCheck, SchemaCache
from xiaowei.starrocks_tools import (
    AUDIT_NOTE,
    AUDIT_TOOLS,
    DIAGNOSE_TOOLS,
    PLAN_NOTE,
    QUERY_TOOLS,
    RESULT_FIELDS,
    RUN_QUERY,
    SLOW_QUERIES,
    data_scope_digest,
    starrocks_tools,
)

pytestmark = pytest.mark.loopback

ALL_TOOLS = QUERY_TOOLS | AUDIT_TOOLS


@dataclass
class Audited:
    """以带审计源的目标装配的目录、证据与应用；共用 env 的 PostgreSQL、模型端点与驱动。"""

    env: Env
    evidence: EvidenceStore
    governed: GovernedTools
    app: Application
    executes: Any

    def ctx(
        self,
        mode: Mode = "diagnose",
        *,
        session: str = "s1",
        turn: str = "t1",
        calls: int = 3,
        channel: Channel = "web",
    ) -> RunContext:
        return RunContext(
            identity=Identity(
                subject_id="alice", session_id=session, turn_id=turn, channel=channel
            ),
            target_scope=frozenset({AUDIT_TARGET.target_id}),
            tool_scope=self.app.scope_for_turn(mode, ALL_TOOLS, self.app.available_tools),
            budget=Budget(
                max_turns=6, max_tool_calls=calls, timeout_seconds=30.0, max_scope_checks=1000
            ),
        )

    async def evidence_rows(self) -> int:
        async with self.env.engine.connect() as conn:
            return (await conn.execute(text("SELECT count(*) FROM xiaowei_evidence"))).scalar_one()


async def audited(env: Env, target: StarRocksTarget = AUDIT_TARGET) -> Audited:
    """装配并刷新一次结构快照；刷新留下的连接记录清掉，用例只统计工具自己的 I/O。"""
    adapter = StarRocksAdapter(target, connect=env.drv, clock=lambda: NOW)
    tools = starrocks_tools(
        adapter, dict.fromkeys(AUDIENCES, CAPACITY), schema=await ready_schema(adapter, env.drv)
    )
    env.grants.grant("alice", *ALL_TOOLS, target=target.target_id)
    evidence = EvidenceStore(
        env.engine,
        ToolCatalog(tools.contracts, tools.policies),
        authorize=env.grants,
        clock=env.clock,
        retention_seconds=3600,
        verify_dependencies=DependencyCheck({target.target_id: adapter}),
    )
    governed = GovernedTools(evidence)
    audit = AUDIT_TOOLS if target.audit is not None else frozenset()
    config = app_config(
        purposes={"query": QUERY_TOOLS | audit, "diagnose": DIAGNOSE_TOOLS | audit},
        data_policies={
            PROFILE.data_policy_id: DataPolicy(
                input=SessionInputPolicy(max_bytes=2000), model_tools=ALL_TOOLS
            )
        },
    )
    app = Application(
        config,
        model=env.binding,
        engine=env.engine,
        governance=governed,
        local_tools=tools.executes,
        clock=env.clock,
    )
    return Audited(env, evidence, governed, app, tools.executes)


def audit_reads(drv: Any) -> list[str]:
    """到达驱动的审计候选读取。"""
    return [
        sql
        for conn in drv.connections
        for sql, _ in conn.executed
        if "starrocks_audit_tbl__" in sql and not sql.startswith("SELECT 1 FROM `")
    ]


def audit_rows(*stmts: str) -> Result:
    return Result(SOURCE, [record(s, query_id=f"q{i}") for i, s in enumerate(stmts)])


def list_slow() -> Any:
    return tool_call(
        "list_slow_queries",
        cluster=AUDIT_TARGET.target_id,
        window_minutes=60,
        order_by="query_time",
        database=None,
    )


# ---- 成功路径：慢查询 → 计划 → 回答 ----------------------------------------------------------


async def test_diagnose_turn_lists_slow_queries_then_explains_one(env: Env) -> None:
    au = await audited(env)
    env.drv.make = driver(audit_rows(ACCEPTED[0], REJECTED[0], ACCEPTED[1])).make

    def explain_first_listed(call: ModelCall) -> Any:
        (listed,) = tool_outputs(call)
        rows = json.loads(listed)["data"]["rows"]
        env.drv.make = driver(PLAN).make
        row = rows[0]
        sql = row["sql"].replace("FROM sales", f"FROM {row['database']}.sales")
        return tool_call("explain_query", cluster=AUDIT_TARGET.target_id, sql=sql)(call)

    message = env.scripts.add(
        "最近一小时最慢的查询为什么慢",
        list_slow(),
        explain_first_listed,
        cite("扫描了整张表"),
    )
    ctx = au.ctx("diagnose")
    delivered = await au.evidence.validate_answer(await au.app.run_turn(ctx, message), ctx)

    seen = env.scripts.tools_seen(message)[0]
    assert "list_slow_queries" in seen and "explain_query" in seen
    assert "run_readonly_query" not in seen
    audit_fact, plan_fact = delivered.facts
    assert audit_fact.tool_id == SLOW_QUERIES and audit_fact.columns == AUDIT_COLUMNS
    # 未通过 SQLGuard 的记录不出现；取计划时按审计行 database 补全原文的物理表。
    assert [row["sql"] for row in audit_fact.rows] == [ACCEPTED[0], ACCEPTED[1]]
    assert audit_fact.metadata["checked_candidates"] == 3
    assert audit_fact.metadata["omitted_sqlguard"] == 1
    assert audit_fact.metadata["omitted_revoked"] == 0
    assert audit_fact.metadata["more_candidates"] is False
    (listed,) = tool_outputs(env.scripts.calls[message][1])
    assert json.loads(listed)["data"]["omitted_sqlguard"] == 1
    async with env.engine.connect() as conn:
        stored = (
            await conn.execute(
                text(
                    "SELECT model_content, session_content, web_content, feishu_content "
                    "FROM xiaowei_evidence WHERE tool_id = :tool"
                ),
                {"tool": SLOW_QUERIES},
            )
        ).one()
    for content in stored:
        assert json.loads(content)["data"]["omitted_sqlguard"] == 1
    assert audit_fact.note == AUDIT_NOTE and plan_fact.note == PLAN_NOTE
    sent = executed_sql(env.drv)
    assert sent[-1].startswith(EXPLAIN_PREFIX) and len(sent) == 2
    assert "secret" not in delivered.content
    assert f"说明：{AUDIT_NOTE}" in delivered.content.split("\n")


async def test_formatted_from_unixtime_audit_row_is_listed_without_executing_it(env: Env) -> None:
    sql = "SELECT region, FROM_UNIXTIME(total, '%Y-%m-%d') AS d FROM sales"
    policy = AUDIT_TARGET.policy.model_copy(
        update={"allowed_functions": AUDIT_TARGET.policy.allowed_functions | {"UNIX_TO_TIME"}}
    )
    target = AUDIT_TARGET.model_copy(update={"policy": policy})
    au = await audited(env, target)
    env.drv.make = driver(audit_rows(sql, ACCEPTED[0])).make

    message = env.scripts.add("列出最近慢查询", list_slow(), cite())
    ctx = au.ctx()
    delivered = await au.evidence.validate_answer(await au.app.run_turn(ctx, message), ctx)

    (fact,) = delivered.facts
    assert [row["sql"] for row in fact.rows] == [sql, ACCEPTED[0]]
    assert len(audit_reads(env.drv)) == 1
    assert sql not in executed_sql(env.drv)


async def test_query_purpose_also_sees_slow_queries(env: Env) -> None:
    au = await audited(env)
    message = env.scripts.add("看看", clarify())
    await au.app.run_turn(au.ctx("query"), message)
    assert {"list_slow_queries", "run_readonly_query"} <= env.scripts.tools_seen(message)[0]


async def test_list_can_be_shorter_than_max_rows_and_is_not_truncated(env: Env) -> None:
    au = await audited(env)
    env.drv.make = driver(audit_rows(*REJECTED[:5], ACCEPTED[0])).make
    message = env.scripts.add("慢查询", list_slow(), cite())
    ctx = au.ctx()
    delivered = await au.evidence.validate_answer(await au.app.run_turn(ctx, message), ctx)
    (fact,) = delivered.facts
    assert len(fact.rows) == 1 and not fact.truncated
    assert fact.metadata["checked_candidates"] == 6
    assert fact.metadata["omitted_sqlguard"] == 5
    assert fact.metadata["unchecked_candidates"] == 0
    assert "只显示能确认引用对象全部获准的查询" in (fact.note or "")


async def test_revoked_object_is_counted_without_showing_its_sql(env: Env) -> None:
    au = await audited(env)
    denied = ProgrammingError(1142, "SELECT denied")
    make = driver(audit_rows(ACCEPTED[0])).make

    def denied_connection() -> Any:
        conn = make()
        conn.results[probe_sql("shop", "sales")] = denied
        return conn

    env.drv.make = denied_connection
    message = env.scripts.add("慢查询", list_slow(), cite())
    ctx = au.ctx()
    delivered = await au.evidence.validate_answer(await au.app.run_turn(ctx, message), ctx)

    (fact,) = delivered.facts
    assert fact.rows == () and fact.metadata["row_count"] == 0
    assert fact.metadata["checked_candidates"] == 1
    assert fact.metadata["omitted_revoked"] == 1
    assert ACCEPTED[0] not in delivered.content
    assert any(
        sql == probe_sql("shop", "sales")
        for conn in env.drv.connections
        for sql, _ in conn.executed
    )


async def test_feishu_fact_shows_omission_counts(env: Env) -> None:
    au = await audited(env)
    env.drv.make = driver(audit_rows(REJECTED[0], ACCEPTED[0])).make
    message = env.scripts.add("慢查询", list_slow(), cite())
    ctx = au.ctx(channel="feishu")
    delivered = await au.evidence.validate_answer(await au.app.run_turn(ctx, message), ctx)
    card = build_feishu_message(delivered, 3500, "oc_test")
    body = json.dumps(card, ensure_ascii=False)

    assert "omitted_sqlguard: 1" in body
    assert "checked_candidates: 2" in body
    assert request_bytes(card, "oc_test") <= 30_000


# ---- 拒绝与失败 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"window_minutes": 0, "order_by": "query_time"},
        {"window_minutes": 1441, "order_by": "query_time"},  # 默认上限 1440
        {"window_minutes": 60, "order_by": "user"},
        {"window_minutes": 60, "order_by": "queryTime DESC; DROP TABLE t"},
        {"window_minutes": True, "order_by": "query_time"},
    ],
)
async def test_window_and_order_are_rejected_before_io(
    env: Env, arguments: dict[str, object]
) -> None:
    au = await audited(env)
    env.drv.make = driver(audit_rows(ACCEPTED[0])).make
    message = env.scripts.add(
        "越界参数",
        tool_call("list_slow_queries", cluster=AUDIT_TARGET.target_id, database=None, **arguments),
        list_slow(),
        cite(),
    )
    ctx = au.ctx(calls=1)
    delivered = await au.evidence.validate_answer(await au.app.run_turn(ctx, message), ctx)
    (rejected,) = tool_outputs(env.scripts.calls[message][1])
    assert "DROP" not in rejected
    # 被拒绝的调用不占预算、不发 I/O：上限为 1 时随后的合法调用仍执行，审计表只读一次（其余
    # 连接只是零行探测与依赖版本读取）。
    assert len(audit_reads(env.drv)) == 1
    assert [f.tool_id for f in delivered.facts] == [SLOW_QUERIES]


async def test_excluded_audit_fields_never_reach_any_projection(env: Env) -> None:
    """审计表返回了身份列（替身模拟模板被改坏）：结果契约失败，本轮停止，什么都不保存。"""
    au = await audited(env)
    env.drv.make = driver(Result((*SOURCE, "user"), [(*record(ACCEPTED[0]), CANARY)])).make
    message = env.scripts.add("慢查询", list_slow(), cite())
    with pytest.raises(TurnError) as failed:
        await au.app.run_turn(au.ctx(), message)
    assert failed.value.reason == "tool_failed"
    assert await au.evidence_rows() == 0 and CANARY not in repr(failed.value)

    # 正常路径：保存的四种投影只含固定输出列。
    env.drv.make = driver(audit_rows(ACCEPTED[0])).make
    ok = env.scripts.add("慢查询", list_slow(), cite())
    await au.app.run_turn(au.ctx(session="s2"), ok)
    async with env.engine.connect() as conn:
        stored = (
            await conn.execute(
                text(
                    "SELECT model_content || session_content || web_content || feishu_content "
                    "FROM xiaowei_evidence"
                )
            )
        ).scalar_one()
    for excluded in ("clientIp", "authorizedUser", '"user"', "feIp", "errorCode", 'stmt"'):
        assert excluded not in stored


@pytest.mark.parametrize("number", [5203, 5502, 5024])
async def test_audit_failures_stop_the_turn(env: Env, number: int) -> None:
    au = await audited(env)
    env.drv.make = driver(OperationalError(number, f"audit failure {CANARY}")).make
    message = env.scripts.add("慢查询", list_slow(), cite())
    with pytest.raises(TurnError) as failed:
        await au.app.run_turn(au.ctx(), message)
    assert failed.value.reason == "tool_failed"
    assert len(env.scripts.calls[message]) == 1  # 模型不续轮，不交给模型继续
    assert env.drv.attempts == 1 and await au.evidence_rows() == 0  # 不重试，不保存


async def test_forced_query_still_rejected_in_a_diagnose_turn(env: Env) -> None:
    au = await audited(env)
    request = ToolRequest(
        tool_id=RUN_QUERY,
        target_id=AUDIT_TARGET.target_id,
        call_id="c1",
        tool_name="run_readonly_query",
        arguments={"cluster": AUDIT_TARGET.target_id, "sql": "SELECT region FROM sales"},
    )
    with pytest.raises(ToolRejectedError):
        await au.governed.invoke(
            au.ctx("diagnose"), request, au.executes[(RUN_QUERY, AUDIT_TARGET.target_id)]
        )
    assert env.drv.attempts == 0


def assembled(target: StarRocksTarget) -> Any:
    """只看登记结果：不刷新结构快照，不产生 I/O。"""
    adapter = StarRocksAdapter(target, connect=driver(), clock=lambda: NOW)
    schema = SchemaCache(adapter, clock=lambda: NOW)
    return starrocks_tools(adapter, dict.fromkeys(AUDIENCES, CAPACITY), schema=schema)


def test_slow_queries_tool_absent_without_audit_source() -> None:
    plain = assembled(AUDIT_TARGET.model_copy(update={"audit": None}))
    assert SLOW_QUERIES not in {c.tool_id for c in plain.contracts}
    assert (SLOW_QUERIES, AUDIT_TARGET.target_id) not in plain.executes
    with_audit = assembled(AUDIT_TARGET)
    (policy,) = [
        p
        for p in with_audit.policies
        if p.policy_id == f"starrocks.{AUDIT_TARGET.target_id}.list_slow_queries"
    ]
    assert policy.fact_note == AUDIT_NOTE and policy.data_scope == data_scope_digest(AUDIT_TARGET)
    assert "omitted_revoked" in policy.required
    assert all("omitted_revoked" in p.fields for p in policy.projections.values())


def test_audit_counts_are_included_in_startup_projection_capacity() -> None:
    capacity = 32_500  # 足以容纳同一目标的普通查询，却不足以容纳审计计数。
    plain = AUDIT_TARGET.model_copy(update={"audit": None})
    for target in (plain, AUDIT_TARGET):
        adapter = StarRocksAdapter(target, connect=driver(), clock=lambda: NOW)
        schema = SchemaCache(adapter, clock=lambda: NOW)
        if target.audit is None:
            starrocks_tools(adapter, dict.fromkeys(AUDIENCES, capacity), schema=schema)
        else:
            with pytest.raises(ValueError, match="投影放不下最坏情况的必需字段"):
                starrocks_tools(adapter, dict.fromkeys(AUDIENCES, capacity), schema=schema)


async def test_old_audit_evidence_is_invalid_after_count_fields_change(env: Env) -> None:
    au = await audited(env)
    catalog = au.evidence._catalog
    key = f"starrocks.{AUDIT_TARGET.target_id}.list_slow_queries"
    current = catalog._policies[key]
    old = replace(
        current,
        projections={
            audience: Projection(RESULT_FIELDS, spec.max_bytes)
            for audience, spec in current.projections.items()
        },
        required=RESULT_FIELDS,
    )
    env.drv.make = driver(audit_rows(ACCEPTED[0])).make
    message = env.scripts.add("慢查询", list_slow(), cite())
    ctx = au.ctx()
    catalog._policies[key] = old
    try:
        answer = await au.app.run_turn(ctx, message)
    finally:
        catalog._policies[key] = current

    (evidence_id,) = answer.answer.evidence_ids
    with pytest.raises(EvidenceUnavailableError):
        await au.evidence.project(evidence_id, ctx, "web")


# ---- 证据随审计源配置失效 -------------------------------------------------------------------

AUDIT_CHANGED = {
    "换审计表": audit_target(audit={"table": "other_audit_tbl"}),
    "换审计时区": audit_target(audit={"time_zone": "Asia/Shanghai"}),
    "缩小 max_rows": audit_target(audit={"max_rows": 2}),
    "缩小 candidate_rows": audit_target(audit={"candidate_rows": 5}),
    "去掉审计源": AUDIT_TARGET.model_copy(update={"audit": None}),
}


@pytest.mark.parametrize("changed", AUDIT_CHANGED.values(), ids=AUDIT_CHANGED.keys())
async def test_audit_scope_change_invalidates_audit_evidence(
    env: Env, changed: StarRocksTarget
) -> None:
    assert data_scope_digest(changed) != data_scope_digest(AUDIT_TARGET)
    au = await audited(env)
    env.drv.make = driver(audit_rows(ACCEPTED[0])).make
    message = env.scripts.add("慢查询", list_slow(), cite())
    ctx = au.ctx()
    answer: TurnAnswer = await au.app.run_turn(ctx, message)
    (evidence_id,) = answer.answer.evidence_ids

    later = await audited(env, changed)
    for audience in ("model", "session", "web"):
        with pytest.raises(EvidenceUnavailableError):
            await later.evidence.project(evidence_id, ctx, audience)
    with pytest.raises(AnswerRejectedError):
        await later.evidence.validate_answer(answer, ctx)


async def test_selected_cluster_query_metadata_reaches_the_model_without_connection_details(env):
    system = await audited(env)
    env.scripts.add("fat 今天最慢三条", clarify())
    await system.app.run_turn(system.ctx(mode="query"), "fat 今天最慢三条")
    specs = {s["name"]: s["description"] for s in env.scripts.tool_specs[0]}
    query = specs["run_readonly_query"]
    assert AUDIT_TARGET.time_zone in query
    assert AUDIT_TARGET.audit.database in query
    assert AUDIT_TARGET.audit.table in query
    assert AUDIT_TARGET.audit.time_zone in query
    assert AUDIT_TARGET.host not in query
    assert AUDIT_TARGET.password_ref not in query
    assert "Asia/Shanghai" in env.scripts.instructions[0]
