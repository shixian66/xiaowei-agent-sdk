"""U1/R7 固定模型样例：复用 Gate 0 观测/驱动，只替换 I/O，走正式 runtime 与渠道。

同一文件复制到基线与候选工作树运行，不覆盖产品提示词/工具说明。报告只给计数和判定；
自动检查不判断自然语言质量，合入仍需逐项独立比较，不把脚本成功当作模型验收。
"""

import json
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx2
from asyncmy.errors import OperationalError
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy.engine import URL
from tests.sdk_core.gate0 import ObservingTransport, SyntheticStarRocks
from tests.sdk_core.synthetic_tools import TEST_DIGEST_KEY, ready_engine

from xiaowei.channel import GroupScope, InboundRequest, RequestRef
from xiaowei.model_api import ModelProfile
from xiaowei.models import Delivery
from xiaowei.runtime import ServeConfig, open_runtime
from xiaowei.starrocks import StarRocksError, StarRocksErrorCode, show_create_sql
from xiaowei.starrocks_tools import QUERY_TOOLS

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
OPERATOR = "u1-synthetic-operator"
DB_ENV, KEY_ENV = "XW_U1_TEST_DATABASE_URL", "XW_U1_TEST_DIGEST_KEY"
TABLE = "quota_virtual_account_quasi_rt"
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Scenario:
    name: str
    messages: tuple[tuple[str, str], ...]
    multi: bool = False
    known_time: bool = True
    raw_chars: int = 100
    pages: bool = False
    tool_budget: int = 16
    ambiguous_database: bool = False
    raw_field: str = "raw"
    only_match: str | None = None
    unavailable: str | None = None
    discovery_pages: bool = False
    busy_target: str | None = None
    actors: tuple[str, ...] = ()
    new_session_at: frozenset[int] = frozenset()


class ReviewVerdict(BaseModel):
    """人工只记录固定是/否/不适用，禁止把模型正文或任意评论写进报告。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    intent_preserved: StrictBool | None = None
    execution_allowed: StrictBool | None = None
    object_selection_correct: StrictBool | None = None
    evidence_consistent: StrictBool | None = None
    clarification_appropriate: StrictBool | None = None
    limitations_stated: StrictBool | None = None
    history_delivery_claim_correct: StrictBool | None = None


@dataclass(frozen=True)
class ReviewSample:
    """隔离测试尚在运行时交给人工的样本；内容只用于即时查看，不进入统计报告。"""

    scenario: str
    repeat: int
    turn: int
    mode: str
    message: str
    state: str
    failure: str | None
    delivery: Delivery | None
    statements: tuple[tuple[str, str, str], ...]


SCENARIOS = (
    Scenario("desc_single", (("query", f"desc {TABLE}"),)),
    Scenario("desc_ambiguous_database", (("query", f"desc {TABLE}"),), ambiguous_database=True),
    Scenario("desc_ambiguous_targets", (("query", f"desc {TABLE}"),), multi=True),
    Scenario("desc_qualified", (("query", f"查看 pre 中 data_center.{TABLE} 的结构"),), multi=True),
    Scenario("count_without_time", (("query", f"pre 的 data_center.{TABLE} 总共多少行？"),)),
    Scenario("yesterday_known", (("query", f"pre 的 data_center.{TABLE} 昨天订单数"),)),
    Scenario(
        "yesterday_missing", (("query", f"data_center.{TABLE} 昨天订单数"),), known_time=False
    ),
    Scenario("sql_only", (("query", f"只写 data_center.{TABLE} 的计数 SQL，不要执行"),)),
    Scenario(
        "previous_diagnosis",
        (
            ("query", f"查询 pre 的 data_center.{TABLE} 前 5 条 region, amount"),
            ("diagnose", "分析刚才那条 SQL 为什么慢，不要执行它"),
        ),
    ),
    Scenario(
        "previous_diagnosis_natural",
        (
            ("query", f"查询 pre 的 data_center.{TABLE} 前 5 条 region, amount"),
            ("query", "分析刚才那条 SQL 为什么慢，不要执行它"),
        ),
    ),
    Scenario(
        "raw_within_limit",
        (("query", f"给我 data_center.{TABLE} 的 raw 原文，只查 1 条"),),
        raw_chars=3900,
    ),
    Scenario(
        "raw_over_limit",
        (("query", f"给我 data_center.{TABLE} 的完整 raw 原文，只查 1 条"),),
        raw_chars=4100,
    ),
    *(
        Scenario(
            f"raw_{field}_{boundary}",
            (("query", f"给我 data_center.{TABLE} 的 {field} 完整原文，只查 1 条"),),
            raw_chars=length,
            raw_field=field,
        )
        for field in ("sql_text", "result_json")
        for boundary, length in (("within_limit", 3900), ("over_limit", 4100))
    ),
    Scenario(
        "ddl_history_and_refresh",
        (
            ("query", f"给我 pre 中 data_center.{TABLE} 的完整原始 DDL"),
            ("query", "刚才那份 DDL 再展示一下"),
            ("query", "重新查询最新 DDL"),
        ),
    ),
    Scenario(
        "candidate_choice", (("query", "在 pre 搜索 quota 表，把候选给我选择，不要执行查询"),)
    ),
    Scenario("pages_with_empty_middle", (("query", "列出 pre 的 data_center 全部表"),), pages=True),
    Scenario(
        "page_budget_partial",
        (("query", "列出 pre 的 data_center 全部表"),),
        pages=True,
        tool_budget=1,
    ),
    Scenario(
        "history_claim_and_five_rows",
        (
            ("query", "列出 pre 的全部库"),
            ("query", "我没有收到库列表，再给我看一下"),
            ("query", f"pre 的 data_center.{TABLE} 只查 5 条 region, amount"),
            ("query", "你怎么返回我这么多，不是只返回5条吗？"),
        ),
    ),
)


# R7 fixed tasks: scripted endpoints test paths; real-model runs require separate authorization.
R7_SCENARIOS = (
    Scenario(
        "r7_explicit_target", (("query", f"查看 fat data_center.{TABLE} 的字段"),), multi=True
    ),
    Scenario(
        "r7_unique_readable",
        (("query", f"desc data_center.{TABLE}"),),
        multi=True,
        only_match="pre",
    ),
    Scenario("r7_multiple_matches", (("query", f"desc data_center.{TABLE}"),), multi=True),
    Scenario(
        "r7_discovery_incomplete",
        (("query", f"desc data_center.{TABLE}"),),
        multi=True,
        discovery_pages=True,
    ),
    Scenario(
        "r7_target_unavailable",
        (("query", f"查看 fat data_center.{TABLE} 的字段"),),
        multi=True,
        unavailable="fat",
    ),
    Scenario(
        "r7_group_reference",
        (
            ("query", f"查看 fat data_center.{TABLE} 的字段"),
            ("query", "沿用A明确指定的集群和表，看看它的布局"),
        ),
        multi=True,
        actors=("ou_r7_a", "ou_r7_b"),
    ),
    Scenario(
        "r7_group_unclear",
        (
            ("query", f"查看 fat data_center.{TABLE} 的字段"),
            ("query", f"pre 也有 data_center.{TABLE} 吗？"),
            ("query", "看看那个表的布局"),
        ),
        multi=True,
        actors=("ou_r7_a", "ou_r7_b", "ou_r7_a"),
    ),
    Scenario(
        "r7_new_session",
        (
            ("query", f"查看 fat data_center.{TABLE} 的字段"),
            ("query", f"desc data_center.{TABLE}"),
        ),
        multi=True,
        new_session_at=frozenset({1}),
    ),
    Scenario("r7_busy", (("query", "看看pre有哪些库；如果繁忙可再试一次"),), busy_target="pre"),
    Scenario("r7_budget_exhausted", (("query", "列出 pre 所有数据库和各库全部表"),), tool_budget=1),
    Scenario(
        "r7_only_sql", (("query", f"写fat的data_center.{TABLE}查询SQL，不要执行"),), multi=True
    ),
    Scenario(
        "r7_after_discovery",
        (
            ("query", "列出fat、fat1和pre各集群的可读库"),
            ("query", f"desc data_center.{TABLE}"),
        ),
        multi=True,
    ),
    Scenario(
        "r7_explicit_followup",
        (
            ("query", f"看fat的data_center.{TABLE}字段"),
            ("query", "沿用刚才明确的fat集群和表，看看它的布局"),
            ("query", f"切到pre，查看data_center.{TABLE}字段"),
        ),
        multi=True,
    ),
    Scenario(
        "r7_compare_targets",
        (("query", f"比较fat、fat1和pre的data_center.{TABLE}字段"),),
        multi=True,
    ),
    Scenario(
        "r7_discovery_error",
        (("query", f"desc data_center.{TABLE}"),),
        multi=True,
        unavailable="fat1",
    ),
    *(s for s in SCENARIOS if s.name in {"desc_single", "desc_ambiguous_database"}),
    Scenario(
        "r7_unknown_target", (("query", f"看unknown集群data_center.{TABLE}的字段"),), multi=True
    ),
)


def tool_choices(body: bytes, targets: frozenset[str]) -> list[tuple[str, str]]:
    """Only retain known tool/target labels; never copy arbitrary identifiers or arguments."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        return []
    if not isinstance(data, dict):
        return []

    def objects(value: Any) -> list[dict[str, Any]]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    calls = []
    if "candidates" in data:
        for candidate in objects(data["candidates"]):
            content = candidate.get("content")
            if isinstance(content, dict):
                calls.extend(
                    p["functionCall"]
                    for p in objects(content.get("parts"))
                    if isinstance(p.get("functionCall"), dict)
                )
    elif "choices" in data:
        for choice in objects(data["choices"]):
            message = choice.get("message")
            if isinstance(message, dict):
                calls.extend(
                    c["function"]
                    for c in objects(message.get("tool_calls"))
                    if isinstance(c.get("function"), dict)
                )
    else:
        calls = [c for c in objects(data.get("output")) if c.get("type") == "function_call"]
    selected = []
    known_tools = {t.partition("/")[2] for t in QUERY_TOOLS}
    for call in calls:
        args = call.get("args", call.get("arguments"))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        target = args.get("cluster") if isinstance(args, dict) else None
        selected.append(
            (
                call["name"]
                if isinstance(call.get("name"), str) and call["name"] in known_tools
                else "other-tool",
                target if isinstance(target, str) and target in targets else "other-target",
            )
        )
    return selected


def _record_choices(choices: list[tuple[str, str]], targets: frozenset[str], body: bytes) -> None:
    choices.extend(tool_choices(body, targets))


async def _synthetic_members(actors: frozenset[str], checks: list[str], _: str, actor: str) -> bool:
    checks.append(actor)
    return actor in actors  # No Lark API; each actor is checked independently.


@dataclass
class DialogueStarRocks(SyntheticStarRocks):
    raw_chars: int = 100
    deny_middle: bool = False
    unavailable: bool = False
    busy: bool = False
    slot_timeouts: int = 0
    raw_field: str = "raw"

    async def __call__(self, **kwargs: Any) -> Any:
        if self.busy:
            # Public connector failure seam. Real semaphore waiting is tested separately.
            self.slot_timeouts += 1
            raise StarRocksError(StarRocksErrorCode.POOL_TIMEOUT, slot_wait=True)
        if self.unavailable:
            raise OperationalError(2003, "synthetic source unavailable")
        return await super().__call__(**kwargs)

    def classify(self, sql: str, args: tuple[object, ...] | None) -> Any:
        if sql in {show_create_sql(db, name) for db, name in self.tables}:
            return "ddl"
        return super().classify(sql, args)

    def result(self, kind: Any, sql: str, args: Any) -> Any:
        if (
            kind == "probe"
            and self.deny_middle
            and any(f"`t{i:03}`" in sql for i in range(200, 400))
        ):
            raise OperationalError(1142, "SELECT command denied on synthetic object")
        if kind == "query":
            if "COUNT(" in sql.upper():
                return ("count",), [(10,)]
            if self.raw_field.upper() in sql.upper():
                if self.raw_field == "sql_text":
                    prefix, suffix = "SELECT region FROM orders /*", "*/"
                    value = prefix + "x" * (self.raw_chars - len(prefix + suffix)) + suffix
                elif self.raw_field == "result_json":
                    value = '{"payload":"' + "x" * (self.raw_chars - 14) + '"}'
                else:
                    value = "x" * self.raw_chars
                return (self.raw_field,), [(value,)]
            return ("region", "amount"), [("east", i) for i in range(5)]
        if kind == "ddl":
            return ("Table", "Create Table"), [
                (
                    TABLE,
                    f"CREATE TABLE {TABLE} (\n"
                    "  region varchar(32), amount decimal(18,2), order_date date, raw string,\n"
                    "  sql_text string, result_json string\n"
                    ') ENGINE=OLAP\nDUPLICATE KEY(region)\nPROPERTIES ("replication_num"="1");',
                )
            ]
        return super().result(kind, sql, args)


def configuration(profile: ModelProfile, scenario: Scenario) -> ServeConfig:
    """只沿用部署数值限额，所有身份、目标与环境引用都替换为隔离样例。"""
    values = json.loads((ROOT / "examples/xiaowei.example.json").read_text())
    values["storage"]["database_url_ref"] = f"env:{DB_ENV}"
    values["storage"]["digest_key_ref"] = f"env:{KEY_ENV}"
    values["model"] = profile.model_dump(mode="json")
    values["web"] = {
        "operator_id": OPERATOR,
        "allowed_origins": ["http://127.0.0.1:18501"],
        "secure_cookie": False,
        "max_body_bytes": 65536,
    }
    values["listen_port"], values["feishu"], values["mcp_servers"] = 18501, None, []
    values["budget"]["max_tool_calls"] = scenario.tool_budget
    values["access"]["grants"] = {OPERATOR: values["data_policy"]["model_tools"]}
    target = values["targets"][0]
    target["description"] = "合成订单库，仅供 U1 模型对照，禁止操作公司集群"
    target["business_context"] = (
        {
            "version": "u1-synthetic",
            "text": "业务时区 Asia/Shanghai；订单日期列 order_date；"
            "一行是一笔订单，region 是地区，amount 单位为元，raw 为原始文本。",
        }
        if scenario.known_time
        else None
    )
    sr = target["starrocks"]
    sr.update(
        target_id="pre", host="synthetic.invalid", database="data_center", time_zone="Asia/Shanghai"
    )
    sr["password_ref"] = "env:U1_UNUSED_PASSWORD"  # noqa: S105 -- env reference; synthetic driver.
    # 固定部署边界；不要借 400KB 投影掩盖原始单值/总结果容量。
    sr.update(max_value_bytes=4000, max_result_bytes=200000, pool_size=4)
    values["targets"] = [target]
    if scenario.multi:
        for target_id in ("fat", "fat1"):
            other = json.loads(json.dumps(target))
            other["starrocks"]["target_id"] = target_id
            values["targets"].append(other)
    if scenario.actors:
        values["feishu"] = {
            "app_id": "cli_r7synthetic",
            "app_secret_ref": "env:XW_R7_UNUSED_FEISHU_SECRET",
            "tenant_key": "r7-synthetic-tenant",
            "users": {},
            "max_event_age_seconds": 300,
            "max_message_chars": 2000,
            "max_reply_chars": 3500,
            "queue_size": 4,
            "consumer_count": 1,
            "send_timeout_seconds": 5,
            "connect_timeout_seconds": 5,
            "stop_timeout_seconds": 10,
            "group": {
                "chat_id": "oc_r7_synthetic",
                "tools": sorted(QUERY_TOOLS),
                "member_page_size": 50,
                "member_max_pages": 2,
                "member_timeout_seconds": 2,
                "max_waiting": 2,
                "max_wait_seconds": 60,
                "wait_check_seconds": 1,
            },
        }
    return ServeConfig.model_validate(values)


async def run_dialogue(
    profile: ModelProfile,
    url: URL,
    network: Callable[[], httpx2.AsyncBaseTransport],
    *,
    scenarios: tuple[Scenario, ...] = SCENARIOS,
    repeats: int = 3,
    manual_review: Callable[[ReviewSample], Awaitable[ReviewVerdict]] | None = None,
) -> list[dict[str, Any]]:
    """报告用于新旧逐项比较；成功不自动批准合入，所有失败都保留。"""
    previous = {name: os.environ.get(name) for name in (DB_ENV, KEY_ENV)}
    os.environ[DB_ENV] = url.render_as_string(hide_password=False)
    os.environ[KEY_ENV] = TEST_DIGEST_KEY.get_secret_value()
    reports: list[dict[str, Any]] = []
    try:
        async with ready_engine(url):
            for repeat in range(repeats):
                for scenario in scenarios:
                    config = configuration(profile, scenario)
                    drivers: dict[str, DialogueStarRocks] = {}
                    for target in config.targets:
                        tables = {(f"db{i:02}", "orders"): ("region",) for i in range(14)}
                        tables["data_center", TABLE] = (
                            "region",
                            "amount",
                            "order_date",
                            "raw",
                            "sql_text",
                            "result_json",
                        )
                        if scenario.name.startswith("r7_"):
                            tables["data_center", TABLE] += (f"r7_{target.target_id}",)
                        if (
                            scenario.only_match is not None
                            and target.target_id != scenario.only_match
                        ):
                            del tables["data_center", TABLE]
                        if scenario.discovery_pages:
                            tables.update(
                                {
                                    ("data_center", f"{TABLE}_bak_{i:03}"): ("region",)
                                    for i in range(401)
                                }
                            )
                        if scenario.ambiguous_database:
                            tables["other_center", TABLE] = tables["data_center", TABLE]
                        tables["data_center", "quota_other"] = ("region",)
                        if scenario.pages:
                            tables = {("data_center", f"t{i:03}"): ("region",) for i in range(401)}
                        drivers[target.target_id] = DialogueStarRocks(
                            target.starrocks,
                            tables=tables,
                            raw_chars=scenario.raw_chars,
                            raw_field=scenario.raw_field,
                            unavailable=target.target_id == scenario.unavailable,
                        )
                    choices: list[tuple[str, str]] = []
                    observer = ObservingTransport(
                        network(),
                        observe_response=partial(_record_choices, choices, frozenset(drivers)),
                    )

                    member_checks: list[str] = []

                    members = partial(_synthetic_members, frozenset(scenario.actors), member_checks)

                    group = (
                        GroupScope(
                            app_id="cli_r7synthetic",
                            tenant_key="r7-synthetic-tenant",
                            chat_id="oc_r7_synthetic",
                        )
                        if scenario.actors
                        else None
                    )
                    channel = "feishu" if group is not None else "web"

                    async with open_runtime(
                        config,
                        clock=lambda: NOW,
                        model_transport=observer,
                        starrocks_connect=drivers,
                        members=members,
                    ) as runtime:
                        for drv in drivers.values():
                            drv.deny_middle = scenario.pages
                            drv.busy = drv.target.target_id == scenario.busy_target
                        conversation = (
                            group.chat_id if group is not None else f"u1-{secrets.token_hex(8)}"
                        )
                        if group is not None:
                            await runtime.service.new_session(
                                channel, scenario.actors[0], conversation, group=group
                            )
                        for index, (mode, message) in enumerate(scenario.messages):
                            subject = scenario.actors[index] if scenario.actors else OPERATOR
                            if index in scenario.new_session_at:
                                await runtime.service.new_session(
                                    channel, subject, conversation, group=group
                                )
                            first = len(observer.observations)
                            first_choice = len(choices)
                            first_members = len(member_checks)
                            before_connections = {k: d.attempts for k, d in drivers.items()}
                            before_timeouts = {k: d.slot_timeouts for k, d in drivers.items()}
                            before = {k: len(d.statements) for k, d in drivers.items()}
                            started = time.monotonic()
                            receipt = await runtime.service.accept(
                                InboundRequest(
                                    channel=channel,
                                    channel_request_id=f"r{index}",
                                    subject_id=subject,
                                    conversation_id=conversation,
                                    group=group,
                                    mode=mode,
                                    message=message,
                                    received_at=NOW,
                                )
                            )
                            record = await runtime.service.process(receipt)
                            view = await runtime.service.results.view(
                                RequestRef(
                                    channel=channel,
                                    subject_id=subject,
                                    conversation_id=conversation,
                                    group=group,
                                    request_id=f"r{index}",
                                ),
                                first=True,
                            )
                            facts = () if view.delivery is None else view.delivery.facts
                            statements = [
                                (k, kind, sql)
                                for k, drv in drivers.items()
                                for kind, sql in drv.statements[before[k] :]
                                if kind != "session"
                            ]
                            queries = [sql for _, kind, sql in statements if kind == "query"]
                            elapsed_ms = int((time.monotonic() - started) * 1000)
                            verdict = (
                                None
                                if manual_review is None
                                else await manual_review(
                                    ReviewSample(
                                        scenario=scenario.name,
                                        repeat=repeat + 1,
                                        turn=index + 1,
                                        mode=mode,
                                        message=message,
                                        state=record.state,
                                        failure=record.failure_code,
                                        delivery=view.delivery,
                                        statements=tuple(statements),
                                    )
                                )
                            )
                            reports.append(
                                {
                                    "scenario": scenario.name,
                                    "repeat": repeat + 1,
                                    "turn": index + 1,
                                    "mode": mode,
                                    "state": record.state,
                                    "failure": record.failure_code,
                                    "elapsed_ms": elapsed_ms,
                                    "requests": [vars(r) for r in observer.observations[first:]],
                                    "tool_choices_by_target": {
                                        target: {
                                            tool: sum(
                                                t == target and name == tool
                                                for name, t in choices[first_choice:]
                                            )
                                            for tool in sorted(
                                                {
                                                    name
                                                    for name, t in choices[first_choice:]
                                                    if t == target
                                                }
                                            )
                                        }
                                        for target in sorted({t for _, t in choices[first_choice:]})
                                    },
                                    "facts_by_tool_target": {
                                        target: {
                                            tool: sum(
                                                f.target_id == target and f.tool_id == tool
                                                for f in facts
                                            )
                                            for tool in sorted(
                                                {f.tool_id for f in facts if f.target_id == target}
                                            )
                                        }
                                        for target in sorted({f.target_id for f in facts})
                                    },
                                    "io_counts": {
                                        kind: sum(k == kind for _, k, _ in statements)
                                        for kind in sorted({k for _, k, _ in statements})
                                    },
                                    "membership_checks_for_actor": member_checks[
                                        first_members:
                                    ].count(subject),
                                    "connection_attempts_by_target": {
                                        k: d.attempts - before_connections[k]
                                        for k, d in drivers.items()
                                    },
                                    "synthetic_slot_timeouts_by_target": {
                                        k: d.slot_timeouts - before_timeouts[k]
                                        for k, d in drivers.items()
                                    },
                                    "io_by_target": {
                                        target: {
                                            kind: sum(
                                                t == target and k == kind for t, k, _ in statements
                                            )
                                            for kind in sorted(
                                                {k for t, k, _ in statements if t == target}
                                            )
                                        }
                                        for target in drivers
                                    },
                                    "facts": [
                                        {
                                            "tool": f.tool_id,
                                            "target": f.target_id,
                                            "rows": len(f.rows),
                                            "truncated": f.truncated,
                                            "has_cursor": f.metadata.get("next_cursor") is not None,
                                        }
                                        for f in facts
                                    ],
                                    "no_query_in_diagnosis": mode != "diagnose" or not queries,
                                    "raw_not_pretrimmed": scenario.raw_chars == 100
                                    or not any(
                                        token in sql.upper()
                                        for sql in queries
                                        for token in ("SUBSTRING(", "LEFT(", "SUBSTR(")
                                    ),
                                    "review": None if verdict is None else verdict.model_dump(),
                                    "manual_review_required": verdict is None,
                                    "delivery_failure_simulated": False,
                                }
                            )
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return reports
