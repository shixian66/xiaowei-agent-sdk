"""原始 DDL 经真实 SDK、治理和 PostgreSQL：原文、权限、范围和不完整结果。"""

import json
from typing import Any

import pytest
from sqlalchemy.engine import URL
from tests.p1b.test_starrocks_adapter import (
    NOW,
    Driver,
    FakeConnection,
    Result,
    adapter,
    driver,
    schema_results,
)
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.p1b.test_starrocks_ddl import DDL, IDENTITY_SQL, SHOW_SQL
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_starrocks_tools import CAPACITY, assembled, tool_outputs

from xiaowei.evidence import EvidenceUnavailableError
from xiaowei.governance import ToolExecutionError, ToolRejectedError
from xiaowei.models import AUDIENCES, Audience, ToolRequest
from xiaowei.starrocks_schema import SchemaCache
from xiaowei.starrocks_tools import QUERY_TOOLS, data_scope_digest, starrocks_tools

pytestmark = pytest.mark.loopback
SHOW_CREATE = "local/show_create_table"


def scripted(*, view: bool = False, replaced: bool = False, engine: str = "OLAP") -> Driver:
    drv = driver()
    make = drv.make
    checks = 0

    def identity(args: tuple[object, ...]) -> Result:
        nonlocal checks
        checks += 1
        return Result(
            ("id", "engine", "type"),
            [(102 if replaced and checks > 1 else 101, engine, "BASE TABLE")],
        )

    def connection() -> FakeConnection:
        conn = make()
        conn.results.update(
            {
                **schema_results(views=frozenset({("shop", "sales")}) if view else frozenset()),
                IDENTITY_SQL: identity,
                SHOW_SQL: Result(("Table", "Create Table"), [("sales", DDL)]),
            }
        )
        return conn

    drv.make = connection
    return drv


def request(**arguments: Any) -> ToolRequest:
    return ToolRequest(
        tool_id=SHOW_CREATE,
        target_id=SR.target_id,
        call_id="ddl-call",
        tool_name="show_create_table",
        arguments={"cluster": SR.target_id, "database": "shop", "table": "sales", **arguments},
    )


async def test_raw_ddl_reaches_model_session_and_web_exactly(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assembled(
        postgres_url, monkeypatch, scripted(), SR.model_copy(update={"max_ddl_bytes": 800})
    ) as env:
        assert (SHOW_CREATE, SR.target_id) in env.executes
        message = env.scripts.add(
            "给我内部表原始建表语句",
            tool_call("show_create_table", cluster=SR.target_id, database="shop", table="sales"),
            cite(),
        )
        delivered = await env.deliver(env.ctx("diagnose"), message)
        (fact,) = delivered.facts
        assert fact.rows == ({"ddl": DDL},) and not fact.truncated
        assert json.loads(fact.result_json)["rows"] == [{"ddl": DDL}]
        (output,) = tool_outputs(env.scripts.calls[message][1])
        assert json.loads(output)["data"]["rows"] == [{"ddl": DDL}]
        assert "服务器" in fact.note
        again = env.scripts.add("刚才的原文再展示", cite())
        replayed = await env.deliver(env.ctx("diagnose", turn="t2"), again)
        assert replayed.facts[0].rows == ({"ddl": DDL},)
        assert sum(sql == SHOW_SQL for conn in env.drv.connections for sql, _ in conn.executed) == 1


async def test_ddl_capacity_failure_is_an_explicit_fact_without_partial_ddl(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assembled(postgres_url, monkeypatch, scripted()) as env:
        message = env.scripts.add(
            "完整 DDL",
            tool_call("show_create_table", cluster=SR.target_id, database="shop", table="sales"),
            cite(),
        )
        delivered = await env.deliver(env.ctx(), message)
        (fact,) = delivered.facts
        assert fact.truncated and fact.rows == ()
        assert "超过" in fact.metadata["message"] and "原文未返回" in fact.metadata["message"]
        assert DDL not in delivered.model_dump_json()


@pytest.mark.parametrize(
    "denial", ["grant", "scope", "target", "view", "missing", "sql_length", "no_snapshot"]
)
async def test_ddl_denied_before_io(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch, denial: str
) -> None:
    target = (
        SR.model_copy(update={"policy": SR.policy.model_copy(update={"max_sql_bytes": 4})})
        if denial == "sql_length"
        else SR
    )
    async with assembled(postgres_url, monkeypatch, scripted(view=denial == "view"), target) as env:
        assert (SHOW_CREATE, SR.target_id) in env.executes
        ctx = env.ctx()
        call = request(table="missing" if denial == "missing" else "sales")
        if denial == "grant":
            env.grants.allowed.discard(("alice", SR.target_id, SHOW_CREATE))
        if denial == "scope":
            ctx = ctx.model_copy(update={"tool_scope": QUERY_TOOLS - {SHOW_CREATE}})
        if denial == "target":
            ctx = ctx.model_copy(update={"target_scope": frozenset({"another-cluster"})})
        execute = env.executes[(SHOW_CREATE, SR.target_id)]
        if denial == "no_snapshot":
            ada = adapter(env.drv, target)
            execute = starrocks_tools(
                ada, dict.fromkeys(AUDIENCES, CAPACITY), schema=SchemaCache(ada, clock=lambda: NOW)
            ).executes[(SHOW_CREATE, SR.target_id)]
        with pytest.raises(ToolRejectedError):
            await env.governed.invoke(ctx, call, execute)
        assert env.drv.attempts == 0 and await env.evidence_rows() == 0


async def test_replacement_during_ddl_never_enters_evidence(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assembled(
        postgres_url,
        monkeypatch,
        scripted(replaced=True),
        SR.model_copy(update={"max_ddl_bytes": 800}),
    ) as env:
        with pytest.raises(ToolExecutionError):
            await env.governed.invoke(
                env.ctx(), request(), env.executes[(SHOW_CREATE, SR.target_id)]
            )
        assert await env.evidence_rows() == 0


async def test_ddl_history_is_unavailable_after_revocation(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assembled(
        postgres_url, monkeypatch, scripted(), SR.model_copy(update={"max_ddl_bytes": 800})
    ) as env:
        ctx = env.ctx()
        result = await env.governed.invoke(
            ctx, request(), env.executes[(SHOW_CREATE, SR.target_id)]
        )
        env.grants.revoke_all()
        with pytest.raises(EvidenceUnavailableError):
            await env.evidence.project(result.evidence_id, ctx, "web")


async def test_external_ddl_is_rejected_without_probing_the_external_table(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assembled(postgres_url, monkeypatch, scripted(engine="MYSQL")) as env:
        with pytest.raises(ToolExecutionError):
            await env.governed.invoke(
                env.ctx(), request(), env.executes[(SHOW_CREATE, SR.target_id)]
            )
        sqls = [sql for conn in env.drv.connections for sql, _ in conn.executed]
        assert IDENTITY_SQL in sqls and SHOW_SQL not in sqls
        assert not any(sql.startswith("SELECT 1 FROM") for sql in sqls)
        assert await env.evidence_rows() == 0


def test_ddl_capacity_changes_evidence_scope() -> None:
    assert data_scope_digest(SR) != data_scope_digest(SR.model_copy(update={"max_ddl_bytes": 800}))


def test_existing_projection_capacity_still_assembles_with_ddl_registered() -> None:
    # 原目标（未配置 max_ddl_bytes）在 e33279c 的容量门槛为 49437；新工具的实际两种结果
    # 均更小，不能因虚构“最大查询行 + DDL 超限说明”的组合拒绝旧配置。
    ada = adapter(driver())
    tools = starrocks_tools(
        ada, dict.fromkeys(AUDIENCES, 49_500), schema=SchemaCache(ada, clock=lambda: NOW)
    )
    assert any(contract.tool_id == SHOW_CREATE for contract in tools.contracts)


@pytest.mark.parametrize("audience", AUDIENCES)
def test_insufficient_projection_capacity_still_rejects_each_boundary(audience: Audience) -> None:
    ada = adapter(driver())
    capacities = dict.fromkeys(AUDIENCES, CAPACITY)
    capacities[audience] = 49_000
    with pytest.raises(ValueError, match="投影放不下"):
        starrocks_tools(ada, capacities, schema=SchemaCache(ada, clock=lambda: NOW))
