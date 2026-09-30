"""P1-B Gate 0：一个真实 Profile 的工具续轮到交付，以及经 Session 的同会话追问。

同一套装配与固定样例供两处使用：离线用例（``test_gate0.py``，HTTP mock 模拟供应商协议）与
显式的真实模型命令（``scripts/gate0_real_model.py``）。装配只走产品路径：``open_model`` →
``Application.run_turn`` → 真 SDK Runner → 受治理函数工具 → ``EvidenceStore`` →
``PolicySession``（``SQLAlchemySession`` + 隔离 PostgreSQL）。两件工具只读取固定合成数据。

专用工具策略的四种投影都包含回答所需的全部字段（``total`` 与 ``rows``），模型可达交集不会
丢掉模型回答依赖的数据；不复用 P1-A 中模型看不到 ``total`` 的 ``PROJECTIONS`` fixture。

观测只记录可复核的元数据：每次模型请求的状态、耗时、展示的工具名、可取得的 usage 数值，
以及工具调用项携带供应商签名的计数；不记录消息正文、模型输出、工具结果或凭据。
"""

import json
import secrets
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx2
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei.app import AppConfig, Application, DataPolicy, Mode, TurnError
from xiaowei.evidence import EvidenceStore
from xiaowei.governance import GovernedTools, Projection, ToolCatalog, ToolPolicy
from xiaowei.model_api import ModelProfile, open_model
from xiaowei.models import (
    Budget,
    Identity,
    RunContext,
    ToolContract,
    ToolObservation,
    ToolRequest,
)
from xiaowei.session import SessionInputPolicy, SessionLimits

TARGET = "gate0-synthetic"
SALES_TOOL = "local/sales_total"
REGIONS_TOOL = "local/list_regions"
SUBJECT = "gate0-operator"
SALES_TOTAL = 1234
RETENTION_SECONDS = 3600
# 响应正文只为取 usage 而暂存；超过此上限不再暂存（Profile 的响应上限仍由产品 transport 执行）。
_USAGE_BODY_LIMIT = 1 << 20
# 只记录这些 token 计数；供应商返回的其他键（可能夹带内容）一律丢弃。
USAGE_KEYS = frozenset(
    {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens"}
)
# 模型请求中工具结果信封的字段名只按此白名单记录（都是本文件策略声明的字段）。
OBSERVED_FIELDS = frozenset({"region", "total", "orders", "rows", "regions"})
# 计入 Gate 0 判定、必须各出现一次的样例。
GATE_SAMPLES = frozenset({"query", "followup", "diagnose_hides_query"})


class RegionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    region: str


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def projections(*fields: str) -> dict[str, Projection]:
    # 四种用途字段相同：模型可达交集（模型 ∩ Session ∩ 渠道）完整保留回答所需字段。
    return {
        a: Projection(fields=fields, max_bytes=4000) for a in ("model", "session", "web", "feishu")
    }


POLICIES = (
    ToolPolicy(
        policy_id="gate0.sales",
        arguments=RegionArgs,
        projections=projections("region", "total", "orders", "rows"),
    ),
    ToolPolicy(policy_id="gate0.regions", arguments=NoArgs, projections=projections("regions")),
)
CONTRACTS = (
    ToolContract(
        tool_id=SALES_TOOL,
        target_id=TARGET,
        input_schema=RegionArgs.model_json_schema(),
        policy_id="gate0.sales",
        description="查询某个地区（region，如 east、west）的合成订单总额与明细。会实际执行查询。",
    ),
    ToolContract(
        tool_id=REGIONS_TOOL,
        target_id=TARGET,
        input_schema=NoArgs.model_json_schema(),
        policy_id="gate0.regions",
        description="列出可查询的地区名称。只读元数据，不执行查询。",
    ),
)
SDK_NAMES = {SALES_TOOL: "sales_total", REGIONS_TOOL: "list_regions"}


@dataclass
class SyntheticAdapter:
    """合成数据的本地执行：记录实际到达 I/O 的请求。"""

    clock: Callable[[], datetime]
    calls: list[ToolRequest] = field(default_factory=list)

    async def execute(self, request: ToolRequest) -> ToolObservation:
        self.calls.append(request)
        if request.tool_id == REGIONS_TOOL:
            payload: dict[str, object] = {"regions": ["east", "west"]}
        else:
            region = str(request.arguments["region"])
            rows = [
                {"day": "2026-09-01", "orders": 2, "amount": 600},
                {"day": "2026-09-02", "orders": 1, "amount": SALES_TOTAL - 600},
            ]
            payload = {"region": region, "total": SALES_TOTAL, "orders": 3, "rows": rows}
        return ToolObservation(payload=payload, captured_at=self.clock(), truncated=False)

    def count(self, tool_id: str) -> int:
        return sum(1 for r in self.calls if r.tool_id == tool_id)


async def _authorize(identity: Identity, target_id: str, tool_id: str) -> bool:
    return identity.subject_id == SUBJECT and target_id == TARGET


def app_config(profile: ModelProfile) -> AppConfig:
    both = frozenset({SALES_TOOL, REGIONS_TOOL})
    return AppConfig(
        # 诊断用途只展示元数据工具；实际查询工具只在明确的查询用途出现。
        purposes={"query": both, "diagnose": frozenset({REGIONS_TOOL})},
        data_policies={
            profile.data_policy_id: DataPolicy(
                input=SessionInputPolicy(max_bytes=2000), model_tools=both
            )
        },
        session_limits=SessionLimits(
            max_history_turns=5, max_history_bytes=40_000, retention_seconds=RETENTION_SECONDS
        ),
        max_concurrent_turns=1,
    )


# ---- 模型请求观测 --------------------------------------------------------------------


@dataclass(frozen=True)
class RequestObservation:
    """一次模型 HTTP 请求的元数据；不含正文。签名计数只对 Chat Completions 有意义。"""

    status: int
    elapsed_ms: int
    tools_offered: tuple[str, ...]
    history_tool_calls: int
    history_signatures: int
    turn_tool_calls: int
    turn_signatures: int
    tool_result_fields: tuple[str, ...]
    """请求中（本轮与回放历史）工具结果信封 ``data`` 的字段名，只取 ``OBSERVED_FIELDS``。"""
    usage: dict[str, int] | None


class ObservingTransport(httpx2.AsyncBaseTransport):
    """包在最底层网络 transport 外：记录请求元数据，响应原样透传。

    产品 transport 仍在外层执行请求头重建与字节限额。
    """

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner
        self.observations: list[RequestObservation] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        shape = _request_shape(await request.aread())
        started = time.monotonic()
        response = await self._inner.handle_async_request(request)
        stream = response.stream
        if not isinstance(stream, httpx2.AsyncByteStream):
            return response

        def record(body: bytes) -> None:
            self.observations.append(
                RequestObservation(
                    status=response.status_code,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    usage=usage_counts(body),
                    **shape,
                )
            )

        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_Tee(stream, record),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


class _Tee(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream, done: Callable[[bytes], None]) -> None:
        self._inner = inner
        self._done = done
        self._body = bytearray()
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            if len(self._body) + len(chunk) <= _USAGE_BODY_LIMIT:
                self._body.extend(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()
        if not self._closed:
            self._closed = True
            self._done(bytes(self._body))


def _request_shape(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        data = {}
    tools = tuple(
        str(t.get("name") or t.get("function", {}).get("name"))
        for t in data.get("tools", [])
        if isinstance(t, dict)
    )
    items = data.get("messages") or data.get("input") or []
    last_user = max((i for i, m in enumerate(items) if m.get("role") == "user"), default=-1)
    counts = {"history": [0, 0], "turn": [0, 0]}
    fields: set[str] = set()
    for index, message in enumerate(items):
        if message.get("role") == "tool" or message.get("type") == "function_call_output":
            fields |= _envelope_fields(message.get("content") or message.get("output"))
        bucket = counts["turn" if index > last_user else "history"]
        for call in message.get("tool_calls") or []:
            bucket[0] += 1
            google = (call.get("extra_content") or {}).get("google") or {}
            bucket[1] += 1 if google.get("thought_signature") else 0
        if message.get("type") == "function_call":
            bucket[0] += 1
    return {
        "tools_offered": tools,
        "history_tool_calls": counts["history"][0],
        "history_signatures": counts["history"][1],
        "turn_tool_calls": counts["turn"][0],
        "turn_signatures": counts["turn"][1],
        "tool_result_fields": tuple(sorted(fields)),
    }


def _envelope_fields(output: object) -> set[str]:
    try:
        envelope = json.loads(output) if isinstance(output, str) else None
    except ValueError:
        return set()
    data = envelope.get("data") if isinstance(envelope, dict) else None
    return set(data) & OBSERVED_FIELDS if isinstance(data, dict) else set()


def usage_counts(body: bytes) -> dict[str, int] | None:
    """只取 ``USAGE_KEYS`` 中的整数计数；未知键、嵌套结构与布尔值丢弃。"""
    try:
        usage = json.loads(body).get("usage")
    except (ValueError, AttributeError):
        return None
    if not isinstance(usage, dict):
        return None
    return {key: value for key in sorted(USAGE_KEYS) if type(value := usage.get(key)) is int}


# ---- 装配与样例 ----------------------------------------------------------------------


@dataclass
class Gate0:
    app: Application
    adapter: SyntheticAdapter
    observer: ObservingTransport


@asynccontextmanager
async def gate0_app(
    profile: ModelProfile,
    engine: AsyncEngine,
    *,
    network: httpx2.AsyncBaseTransport,
    clock: Callable[[], datetime],
) -> AsyncIterator[Gate0]:
    """产品装配：``network`` 只替换最底层发送（离线为 mock，真实运行为不重试的网络 transport）。"""
    catalog = ToolCatalog(CONTRACTS, POLICIES)
    evidence = EvidenceStore(
        engine, catalog, authorize=_authorize, clock=clock, retention_seconds=RETENTION_SECONDS
    )
    governed = GovernedTools(evidence)
    adapter = SyntheticAdapter(clock)
    observer = ObservingTransport(network)
    async with open_model(profile, transport=observer) as binding:
        app = Application(
            app_config(profile),
            model=binding,
            engine=engine,
            governance=governed,
            local_tools={SALES_TOOL: adapter.execute, REGIONS_TOOL: adapter.execute},
            clock=clock,
        )
        yield Gate0(app=app, adapter=adapter, observer=observer)


@dataclass(frozen=True)
class Sample:
    name: str
    session: str
    mode: Mode
    message: str
    gate: bool
    """是否计入 Gate 0 判定；否则只记录行为（回答质量不由单个样例判定）。"""


SAMPLES: tuple[Sample, ...] = (
    Sample("query", "s-main", "query", "请查询 east 区的订单总额，并简要说明。", gate=True),
    Sample(
        "followup",
        "s-main",
        "query",
        "上一轮查到的 east 区订单总额是多少？请直接依据上一轮的查询结果回答，不要再次查询。",
        gate=True,
    ),
    Sample(
        "diagnose_hides_query",
        "s-diag",
        "diagnose",
        "请执行查询，告诉我 east 区的订单总额。",
        gate=True,
    ),
    Sample("ambiguous", "s-vague", "query", "那个数字正常吗？", gate=False),
)


@dataclass
class SampleResult:
    name: str
    mode: Mode
    outcome: Literal["delivered", "clarification", "failed"]
    reason: str | None
    evidence_ids: tuple[str, ...]
    sales_calls: int
    regions_calls: int
    elapsed_ms: int
    requests: list[RequestObservation]
    checks: dict[str, bool] = field(default_factory=dict)
    gate: bool = False

    @property
    def passed(self) -> bool:
        # 没有任何检查的样例不算通过：判定缺失不能等同于全部满足。
        return bool(self.checks) and all(self.checks.values())

    def report(self) -> dict[str, Any]:
        """可写入记录的形式：证据标识只给数量，不含任何内容。"""
        return {
            "sample": self.name,
            "mode": self.mode,
            "gate": self.gate,
            "passed": self.passed,
            "checks": self.checks,
            "outcome": self.outcome,
            "reason": self.reason,
            "cited_evidence": len(self.evidence_ids),
            "sales_calls": self.sales_calls,
            "regions_calls": self.regions_calls,
            "elapsed_ms": self.elapsed_ms,
            "requests": [vars(r) for r in self.requests],
        }


def _context(sample: Sample, app: Application, run: str) -> RunContext:
    return RunContext(
        identity=Identity(
            subject_id=SUBJECT,
            session_id=f"{run}-{sample.session}",
            turn_id=f"{run}-{sample.name}",
            channel="web",
        ),
        target_scope=frozenset({TARGET}),
        tool_scope=app.scope_for_turn(
            sample.mode, frozenset({SALES_TOOL, REGIONS_TOOL}), app.available_tools
        ),
        budget=Budget(max_turns=6, max_tool_calls=3, timeout_seconds=120.0),
    )


async def run_samples(gate: Gate0, samples: Sequence[Sample] = SAMPLES) -> list[SampleResult]:
    """按顺序运行样例；同名 ``session`` 的样例共用一个会话。每次运行使用新的会话标识。"""
    run = f"g0-{secrets.token_hex(4)}"
    results: list[SampleResult] = []
    for sample in samples:
        before_sales = gate.adapter.count(SALES_TOOL)
        before_regions = gate.adapter.count(REGIONS_TOOL)
        first_request = len(gate.observer.observations)
        started = time.monotonic()
        outcome: Literal["delivered", "clarification", "failed"]
        reason: str | None = None
        cited: tuple[str, ...] = ()
        try:
            delivery = await gate.app.run_turn(_context(sample, gate.app, run), sample.message)
            cited = delivery.evidence_ids
            outcome = "delivered" if cited else "clarification"
        except TurnError as exc:
            outcome, reason = "failed", exc.reason
        results.append(
            SampleResult(
                name=sample.name,
                mode=sample.mode,
                outcome=outcome,
                reason=reason,
                evidence_ids=cited,
                sales_calls=gate.adapter.count(SALES_TOOL) - before_sales,
                regions_calls=gate.adapter.count(REGIONS_TOOL) - before_regions,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                requests=gate.observer.observations[first_request:],
                gate=sample.gate,
            )
        )
    judge(results)
    return results


def judge(results: list[SampleResult]) -> None:
    """按样例名写入检查项。每项都要求有直接证据：没有模型请求时相关检查不成立。"""
    query = next((r for r in results if r.name == "query"), None)
    wanted = {"total", "rows"}
    for result in results:
        requests = result.requests
        if result.name == "query":
            result.checks = {
                "delivered": result.outcome == "delivered",
                "tool_executed_once": result.sales_calls == 1,
                "cites_evidence": bool(result.evidence_ids),
                "model_saw_total_and_rows": any(
                    wanted <= set(r.tool_result_fields) for r in requests
                ),
            }
        elif result.name == "followup":
            previous = set(query.evidence_ids) if query is not None else set()
            first = requests[0] if requests else None
            result.checks = {
                "delivered": result.outcome == "delivered",
                "history_replayed": first is not None and first.history_tool_calls >= 1,
                "replayed_total_and_rows": first is not None
                and wanted <= set(first.tool_result_fields),
                "no_tool_rerun": result.sales_calls == 0 and result.regions_calls == 0,
                "cites_previous_evidence": bool(previous & set(result.evidence_ids)),
            }
        elif result.name == "diagnose_hides_query":
            result.checks = {
                "model_requested": bool(requests),
                "metadata_tool_offered": bool(requests)
                and all(SDK_NAMES[REGIONS_TOOL] in r.tools_offered for r in requests),
                "query_tool_hidden": all(
                    SDK_NAMES[SALES_TOOL] not in r.tools_offered for r in requests
                ),
                "query_not_executed": result.sales_calls == 0,
            }
        elif result.name == "ambiguous":
            result.checks = {"clarifies": result.outcome == "clarification"}


def gate_passed(results: Sequence[SampleResult]) -> bool:
    """``GATE_SAMPLES`` 中每个样例恰好出现一次且标记为计入判定，并全部通过。"""
    gated = [r for r in results if r.name in GATE_SAMPLES]
    names = [r.name for r in gated]
    return (
        sorted(names) == sorted(GATE_SAMPLES)
        and all(r.gate for r in gated)
        and all(r.passed for r in gated)
    )
