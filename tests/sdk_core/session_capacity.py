"""S1 capacity observations through serve/Runner/Session; synthetic content stays in memory.

The observers wrap Xiaowei's boundaries, not SDK internals. They never trim history or
change admission. Reports contain counts, sizes and limits, not captured payloads.
"""

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from tests.sdk_core.dialogue_gate import TABLE, DialogueStarRocks, Scenario, configuration
from tests.sdk_core.test_gate0 import (
    VertexLikeEndpoint,
    _vertex_body,
    _vertex_envelopes,
    vertex_call,
)
from tests.sdk_core.test_monitoring_r1a import SOURCE, TOOL, monitoring_config
from tests.sdk_core.test_runtime import DB_ENV, KEY_ENV, OPERATOR, Env
from tests.sdk_core.test_vertex_model import VERTEX

from xiaowei import app as app_module
from xiaowei import evidence as evidence_module
from xiaowei import model_api
from xiaowei.runtime import ServeConfig
from xiaowei.session import PolicySession

PROFILE = VERTEX.model_copy(
    update={"max_output_tokens": 16384, "max_request_bytes": 1000000, "max_response_bytes": 1000000}
)


def capacity_config(env: Env, *, monitoring_url: str | None = None, **changes: Any) -> ServeConfig:
    values = configuration(PROFILE, Scenario("capacity", ())).model_dump(mode="json")
    values["storage"]["database_url_ref"] = f"env:{DB_ENV}"
    values["storage"]["digest_key_ref"] = f"env:{KEY_ENV}"
    values["listen_port"] = env.port
    values["web"]["operator_id"] = OPERATOR
    values["web"]["allowed_origins"] = [env.origin]
    tools = values["data_policy"]["model_tools"]
    if monitoring_url:
        values["mcp_servers"] = monitoring_config(monitoring_url)["mcp_servers"]
        tools.append(TOOL)
    values["access"]["grants"] = {OPERATOR: tools}
    for section, update in changes.items():
        values[section].update(update)
    return ServeConfig.model_validate(values)


@dataclass
class CapacityDriver(DialogueStarRocks):
    rows: int = 5
    value_bytes: int = 4000
    ddl_bytes: int | None = None

    def result(self, kind: Any, sql: str, args: Any) -> Any:
        prefix = '原文\n"\\'
        value = prefix + "x" * (self.value_bytes - len(prefix.encode()))
        if kind == "query" and "raw" in sql.lower():
            return ("raw",), [(value,) for _ in range(self.rows)]
        if kind == "ddl" and self.ddl_bytes is not None:
            ddl = f"CREATE TABLE {TABLE} (region varchar(32)) COMMENT '"
            ddl += "x" * (self.ddl_bytes - len((ddl + "';").encode())) + "';"
            return ("Table", "Create Table"), [(TABLE, ddl)]
        return super().result(kind, sql, args)


def drivers(config: ServeConfig, **changes: Any) -> dict[str, CapacityDriver]:
    return {
        target.target_id: CapacityDriver(
            target.starrocks,
            tables={
                ("data_center", TABLE): ("region", "amount", "order_date", "raw"),
            },
            **changes,
        )
        for target in config.targets
    }


def finish(contents: list[dict[str, Any]]) -> dict[str, Any]:
    # Cite all current query evidence, but historical followups need only the last evidence.
    envelopes = _vertex_envelopes(contents)
    last_user = max(
        i
        for i, c in enumerate(contents)
        if any("text" in p for p in c["parts"]) and c["role"] == "user"
    )
    current = _vertex_envelopes(contents[last_user + 1 :])
    selected = current or envelopes[-1:]
    ids = [e["evidence_id"] for e in selected]
    answer = {
        "evidence_ids": ids,
        "inferences": [{"text": "合成说明：仅解释当前证据。", "evidence_ids": ids}],
        "clarification": None,
    }
    return _vertex_body([{"text": json.dumps(answer, ensure_ascii=False)}])


def parallel(*calls: Any) -> Any:
    def response(contents: list[dict[str, Any]]) -> dict[str, Any]:
        return _vertex_body(
            [p for call in calls for p in call(contents)["candidates"][0]["content"]["parts"]]
        )

    return response


def sequence(turns: int, *, scenario: str = "mixed") -> VertexLikeEndpoint:
    table = {"cluster": "pre", "database": "data_center", "table": TABLE}
    sql = "SELECT region, amount FROM data_center.quota_virtual_account_quasi_rt LIMIT 5"
    raw = "SELECT raw FROM data_center.quota_virtual_account_quasi_rt LIMIT 5"
    choices = [
        parallel(
            vertex_call("describe_table", **table, cursor=None),
            vertex_call("describe_table_layout", **table),
        ),
        vertex_call("run_readonly_query", cluster="pre", sql=sql),
        vertex_call("run_readonly_query", cluster="pre", sql=raw),
        None,  # Explanation based on history, no tool I/O.
        None,  # Short historical followup, no tool I/O.
        vertex_call(f"{SOURCE}__query", query="up"),
    ]
    replies = {}
    for i in range(turns):
        if scenario == "mixed":
            call = choices[i % len(choices)]
        elif scenario == "ddl":
            call = vertex_call("show_create_table", **table)
        elif scenario == "multi_query":
            call = parallel(
                *(
                    vertex_call(
                        "run_readonly_query", cluster="pre", sql=raw.replace("LIMIT 5", "LIMIT 45")
                    )
                    for _ in range(3)
                )
            )
        elif scenario == "monitoring":
            call = choices[-1]
        else:
            assert scenario == "small"
            call = choices[1] if i == 0 else None
        replies[question(i + 1, scenario)] = [call, finish] if call else [finish]
    return VertexLikeEndpoint(replies)


def question(index: int, scenario: str) -> str:
    tasks = (
        "查看 pre 的 data_center.quota_virtual_account_quasi_rt 字段和布局",
        "只查这张表的 5 条数据",
        "查看 raw 的完整原文，不预先截短",
        "解释刚才的结果，不要重新查询",
        "刚才本次返回多少条？沿用已有证据",
        "查看获准监控源的 up 指标并给出摘要",
    )
    task = (
        tasks[(index - 1) % 6]
        if scenario == "mixed"
        else {
            "small": tasks[1] if index == 1 else tasks[4],
            "ddl": "查看 pre 的该表完整 DDL",
            "multi_query": "查询 pre 的该表三个结果，各取原始文本",
            "monitoring": tasks[5],
        }[scenario]
    )
    return f"第 {index} 轮：{task}"


@dataclass
class CapacityObservation:
    """One turn: complete replay, all serialized requests and real admission/I/O counts."""

    replay_bytes: int = 0
    commit_replay_bytes: int = 0
    scope_checks: int = 0
    requests: list[dict[str, int | bool]] = field(default_factory=list)
    elapsed_ms: int = 0
    completed: bool = False


@dataclass
class CapacityRecorder:
    scenario: str = "mixed"
    turns: list[CapacityObservation] = field(default_factory=list)

    @property
    def current(self) -> CapacityObservation:
        return self.turns[-1]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = self

        class ObservedSession(PolicySession):
            async def get_items(self, limit: int | None = None) -> list[Any]:
                replay = await super().get_items(limit)
                recorder.current.replay_bytes = len(json.dumps(replay, ensure_ascii=False).encode())
                return replay

            async def _replay(self, items: Any, **kwargs: Any) -> Any:
                replay = await super()._replay(items, **kwargs)
                if getattr(self, "observing_commit", False):
                    recorder.current.commit_replay_bytes = len(
                        json.dumps(replay, ensure_ascii=False).encode()
                    )
                return replay

            async def commit_validated(self, *args: Any, **kwargs: Any) -> None:
                # Observe the already-required replay; don't call get_items again or probe evidence.
                self.observing_commit = True
                try:
                    await super().commit_validated(*args, **kwargs)
                finally:
                    self.observing_commit = False

        monkeypatch.setattr(app_module, "PolicySession", ObservedSession)
        original_scope = app_module.scope_checks

        @contextmanager
        def observe_scope() -> Any:
            with original_scope():
                try:
                    yield
                finally:
                    checks = evidence_module._SCOPE_CHECKS.get()
                    recorder.current.scope_checks = checks.used if checks else 0

        monkeypatch.setattr(app_module, "scope_checks", observe_scope)
        original = model_api._GuardedTransport.handle_async_request

        async def observe_request(transport: Any, request: httpx2.Request) -> httpx2.Response:
            body = await request.aread()
            data = json.loads(body)
            fixed = {key: value for key, value in data.items() if key != "contents"}
            parts = [p for c in data["contents"] for p in c["parts"]]
            recorder.current.requests.append(
                {
                    "bytes": len(body),
                    # Same HTTP JSON encoder, including separators, Unicode and escaping.
                    "overhead_bytes": len(httpx2.Request("POST", request.url, json=fixed).content),
                    "instructions_bytes": len(
                        httpx2.Request(
                            "POST", request.url, json=data.get("systemInstruction")
                        ).content
                    ),
                    "declarations_bytes": len(
                        httpx2.Request("POST", request.url, json=data.get("tools", [])).content
                    ),
                    "generation_schema_bytes": len(
                        httpx2.Request("POST", request.url, json=data["generationConfig"]).content
                    ),
                    "tool_count": sum(
                        len(t["functionDeclarations"]) for t in data.get("tools", [])
                    ),
                    "tool_calls": sum("functionCall" in p for p in parts),
                    "tool_outputs": sum("functionResponse" in p for p in parts),
                    "signatures": sum("thoughtSignature" in p for p in parts),
                    "sent": False,
                }
            )
            return await original(transport, request)

        monkeypatch.setattr(model_api._GuardedTransport, "handle_async_request", observe_request)

    def network(self, endpoint: VertexLikeEndpoint) -> httpx2.MockTransport:
        async def handle(request: httpx2.Request) -> httpx2.Response:
            self.current.requests[-1]["sent"] = True
            return await endpoint._handle(request)

        return httpx2.MockTransport(handle)

    async def turn(self, served: Any, index: int) -> dict[str, Any]:
        self.turns.append(CapacityObservation())
        start = time.monotonic()
        body = (await served.turn(question(index, self.scenario), "query", f"s1-{index}")).json()
        self.current.elapsed_ms = int((time.monotonic() - start) * 1000)
        self.current.completed = body["state"] == "completed"
        return body

    def report(self, config: ServeConfig, connectors: dict[str, CapacityDriver]) -> dict[str, Any]:
        from dataclasses import asdict

        from tests.sdk_core.dialogue_gate import ROOT

        source = Path(app_module.__file__).parent
        digest = hashlib.sha256()
        for path in sorted(source.rglob("*.py")):
            digest.update(str(path.relative_to(source)).encode())
            digest.update(path.read_bytes())

        return {
            "input_hashes": {
                "product": digest.hexdigest(),
                "template": hashlib.sha256(
                    (ROOT / "examples/xiaowei.example.json").read_bytes()
                ).hexdigest(),
                "harness": hashlib.sha256(
                    Path(__file__).read_bytes()
                    + Path(__file__).with_name("test_session_capacity.py").read_bytes()
                ).hexdigest(),
            },
            "target_count": len(config.targets),
            "limits": {
                **config.session_limits.model_dump(),
                "max_request_bytes": config.model.max_request_bytes,
                "max_scope_checks": config.budget.max_scope_checks,
                "max_value_bytes": config.targets[0].starrocks.max_value_bytes,
                "max_result_bytes": config.targets[0].starrocks.max_result_bytes,
                "model_projection_bytes": config.projection_bytes["model"],
                "session_projection_bytes": config.projection_bytes["session"],
            },
            "turns": [asdict(t) for t in self.turns],
            "io": {
                "query": sum(len(d.sent("query")) for d in connectors.values()),
                "probe": sum(len(d.sent("probe")) for d in connectors.values()),
                "dependency": sum(len(d.sent("dependency")) for d in connectors.values()),
                "ddl": sum(len(d.sent("ddl")) for d in connectors.values()),
            },
        }
