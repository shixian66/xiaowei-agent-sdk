"""U1 固定模型样例：复用 Gate 0 观测/驱动，只替换 I/O，走正式 runtime 与渠道。

同一文件复制到基线与候选工作树运行，不覆盖产品提示词/工具说明。报告只给计数和判定；
自动检查不判断自然语言质量，合入仍需逐项独立比较，不把脚本成功当作模型验收。
"""

import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
from asyncmy.errors import OperationalError
from sqlalchemy.engine import URL
from tests.sdk_core.gate0 import ObservingTransport, SyntheticStarRocks
from tests.sdk_core.synthetic_tools import TEST_DIGEST_KEY, ready_engine

from xiaowei.channel import InboundRequest, RequestRef
from xiaowei.model_api import ModelProfile
from xiaowei.runtime import ServeConfig, open_runtime
from xiaowei.starrocks import show_create_sql

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


SCENARIOS = (
    Scenario("desc_single", (("query", f"desc {TABLE}"),)),
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
        "raw_within_limit",
        (("query", f"给我 data_center.{TABLE} 的 raw 原文，只查 1 条"),),
        raw_chars=3900,
    ),
    Scenario(
        "raw_over_limit",
        (("query", f"给我 data_center.{TABLE} 的完整 raw 原文，只查 1 条"),),
        raw_chars=4100,
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
        "lost_delivery_and_five_rows",
        (
            ("query", "列出 pre 的全部库"),
            ("query", "我没有收到库列表，再给我看一下"),
            ("query", f"pre 的 data_center.{TABLE} 只查 5 条 region, amount"),
            ("query", "你怎么返回我这么多，不是只返回5条吗？"),
        ),
    ),
)


@dataclass
class DialogueStarRocks(SyntheticStarRocks):
    raw_chars: int = 100
    deny_middle: bool = False

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
            if "RAW" in sql.upper():
                return ("raw",), [("x" * self.raw_chars,)]
            return ("region", "amount"), [("east", i) for i in range(5)]
        if kind == "ddl":
            return ("Table", "Create Table"), [
                (
                    TABLE,
                    f"CREATE TABLE {TABLE} (\n"
                    "  region varchar(32), amount decimal(18,2), order_date date, raw string\n"
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
        other = json.loads(json.dumps(target))
        other["starrocks"]["target_id"] = "fat"
        values["targets"].append(other)
    return ServeConfig.model_validate(values)


async def run_dialogue(
    profile: ModelProfile,
    url: URL,
    network: Callable[[], httpx2.AsyncBaseTransport],
    *,
    scenarios: tuple[Scenario, ...] = SCENARIOS,
    repeats: int = 3,
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
                        tables["data_center", TABLE] = ("region", "amount", "order_date", "raw")
                        tables["data_center", "quota_other"] = ("region",)
                        if scenario.pages:
                            tables = {("data_center", f"t{i:03}"): ("region",) for i in range(401)}
                        drivers[target.target_id] = DialogueStarRocks(
                            target.starrocks,
                            tables=tables,
                            raw_chars=scenario.raw_chars,
                        )
                    observer = ObservingTransport(network())
                    async with open_runtime(
                        config,
                        clock=lambda: NOW,
                        model_transport=observer,
                        starrocks_connect=drivers,
                    ) as runtime:
                        for drv in drivers.values():
                            drv.deny_middle = scenario.pages
                        conversation = f"u1-{secrets.token_hex(8)}"
                        for index, (mode, message) in enumerate(scenario.messages):
                            first = len(observer.observations)
                            before = {k: len(d.statements) for k, d in drivers.items()}
                            started = time.monotonic()
                            receipt = await runtime.service.accept(
                                InboundRequest(
                                    channel="web",
                                    channel_request_id=f"r{index}",
                                    subject_id=OPERATOR,
                                    conversation_id=conversation,
                                    mode=mode,
                                    message=message,
                                    received_at=NOW,
                                )
                            )
                            record = await runtime.service.process(receipt)
                            view = await runtime.service.results.view(
                                RequestRef(
                                    channel="web",
                                    subject_id=OPERATOR,
                                    conversation_id=conversation,
                                    request_id=f"r{index}",
                                ),
                                first=True,
                            )
                            facts = () if view.delivery is None else view.delivery.facts
                            statements = [
                                s
                                for k, drv in drivers.items()
                                for s in drv.statements[before[k] :]
                                if s[0] != "session"
                            ]
                            queries = [sql for kind, sql in statements if kind == "query"]
                            reports.append(
                                {
                                    "scenario": scenario.name,
                                    "repeat": repeat + 1,
                                    "turn": index + 1,
                                    "mode": mode,
                                    "state": record.state,
                                    "failure": record.failure_code,
                                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                                    "requests": [vars(r) for r in observer.observations[first:]],
                                    "io_counts": {
                                        kind: sum(k == kind for k, _ in statements)
                                        for kind in sorted({k for k, _ in statements})
                                    },
                                    "facts": [
                                        {
                                            "tool": f.tool_id,
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
                                    "manual_review_required": True,
                                }
                            )
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return reports
