"""Task 2 测试共用的合成工具、可控时钟、可撤销授权与 recording adapter。

只服务测试：两件合成工具读取固定的合成数据，不模拟 StarRocks 或任何真实系统。
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from agents import FunctionTool
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei.evidence import EvidenceStore
from xiaowei.governance import GovernedTools, Projection, ToolCatalog, ToolPolicy
from xiaowei.models import (
    Budget,
    Channel,
    Identity,
    RunContext,
    ToolContract,
    ToolObservation,
    ToolRequest,
)
from xiaowei.storage import initialize_storage, open_engine
from xiaowei.tools import governed_function_tool

TARGET = "synthetic-warehouse"
OTHER_TARGET = "other-warehouse"
TOTAL_TOOL = "local/order_total"
QUERY_TOOL = "local/run_query"
PRIVATE_NOTE = "synthetic-private-note"
START = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)
RETENTION_SECONDS = 3600


class RegionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    region: str


# 四种用途各自的字段与字节上限；private_note 不属于任何用途。
PROJECTIONS = {
    "model": Projection(fields=("total", "rows"), max_bytes=400),
    "session": Projection(fields=("region", "rows"), max_bytes=400),
    "web": Projection(fields=("region", "total", "rows"), max_bytes=2000),
    "feishu": Projection(fields=("region", "total"), max_bytes=300),
}

POLICIES = (
    ToolPolicy(policy_id="synthetic.region", arguments=RegionArgs, projections=PROJECTIONS),
)
CONTRACTS = (
    ToolContract(
        tool_id=TOTAL_TOOL,
        target_id=TARGET,
        input_schema=RegionArgs.model_json_schema(),
        policy_id="synthetic.region",
    ),
    ToolContract(
        tool_id=QUERY_TOOL,
        target_id=TARGET,
        input_schema=RegionArgs.model_json_schema(),
        policy_id="synthetic.region",
    ),
)


def catalog() -> ToolCatalog:
    return ToolCatalog(CONTRACTS, POLICIES)


def payload(region: str, total: int = 100, rows: int = 2, memo: str = "") -> dict[str, object]:
    extra = {"memo": memo} if memo else {}
    return {
        "region": region,
        "total": total,
        "rows": [{"day": f"2026-09-{d:02d}", "orders": 50, **extra} for d in range(1, rows + 1)],
        "private_note": PRIVATE_NOTE,
    }


@dataclass
class Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Grants:
    """应用提供的当前授权查询：(subject, target, tool) 三元组，测试中可随时撤销。"""

    allowed: set[tuple[str, str, str]] = field(default_factory=set)
    checks: list[tuple[str, str, str]] = field(default_factory=list)

    def grant(self, subject: str, *tools: str, target: str = TARGET) -> None:
        self.allowed.update((subject, target, tool) for tool in tools)

    def revoke_all(self) -> None:
        self.allowed.clear()

    async def __call__(self, identity: Identity, target_id: str, tool_id: str) -> bool:
        key = (identity.subject_id, target_id, tool_id)
        self.checks.append(key)
        return key in self.allowed


@dataclass
class RecordingAdapter:
    """记录实际到达 I/O 的请求；拒绝路径必须保持为空。"""

    calls: list[ToolRequest] = field(default_factory=list)
    total: int = 100
    rows: int = 2
    memo: str = ""
    truncated: bool = False

    async def execute(self, request: ToolRequest) -> ToolObservation:
        self.calls.append(request)
        region = str(request.arguments["region"])
        return ToolObservation(
            payload=payload(region, self.total, self.rows, self.memo),
            captured_at=START,
            truncated=self.truncated,
        )


def context(
    subject: str = "alice",
    session: str = "s1",
    turn: str = "t1",
    channel: Channel = "web",
    *,
    tools: frozenset[str] = frozenset({TOTAL_TOOL, QUERY_TOOL}),
    targets: frozenset[str] = frozenset({TARGET}),
    max_tool_calls: int = 5,
) -> RunContext:
    return RunContext(
        identity=Identity(subject_id=subject, session_id=session, turn_id=turn, channel=channel),
        target_scope=targets,
        tool_scope=tools,
        budget=Budget(max_turns=6, max_tool_calls=max_tool_calls, timeout_seconds=30.0),
        evidence_ids=(),
    )


def request(
    tool_id: str = TOTAL_TOOL, region: str = "east", call_id: str = "call-1"
) -> ToolRequest:
    return ToolRequest(
        tool_id=tool_id,
        target_id=TARGET,
        call_id=call_id,
        tool_name=tool_id.split("/", 1)[1],
        arguments={"region": region},
    )


def sdk_tool(governed: GovernedTools, adapter: RecordingAdapter, tool_id: str) -> FunctionTool:
    """产品同一路径的受治理函数工具；execute 由这里绑定而非模型提供。"""
    contract = governed.catalog.contract(tool_id)
    assert contract is not None
    return governed_function_tool(tool_id.split("/", 1)[1], contract, governed, adapter.execute)


def secret(url: URL) -> SecretStr:
    return SecretStr(url.render_as_string(hide_password=False))


@asynccontextmanager
async def ready_engine(url: URL) -> AsyncIterator[AsyncEngine]:
    async with open_engine(secret(url)) as engine:
        await initialize_storage(engine)
        yield engine


def store(
    engine: AsyncEngine, grants: Grants, clock: Clock, tools: ToolCatalog | None = None
) -> EvidenceStore:
    return EvidenceStore(
        engine,
        tools or catalog(),
        authorize=grants,
        clock=clock,
        retention_seconds=RETENTION_SECONDS,
    )


def model_data(content: str) -> dict[str, object]:
    parsed = json.loads(content)
    assert isinstance(parsed, dict)
    return parsed
