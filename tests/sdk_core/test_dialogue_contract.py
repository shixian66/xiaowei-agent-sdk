"""U1：正式入口的规则送达与可组合工具路径。

脚本端点不能证明真实模型的自主选择；这里只证明实际说明已送达、直取/追问的产品路径
以及显式诊断的零查询保护。真实模型旧/新对照仍是单独的合入门槛。
"""

import pytest
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.test_app import cite, clarify, tool_call
from tests.sdk_core.test_runtime import Env
from tests.sdk_core.test_runtime import env as env  # pytest fixture
from tests.sdk_core.test_starrocks_tools import RecordingScripts, executed_sql

pytestmark = pytest.mark.loopback


async def test_delivered_rules_allow_needed_evidence_and_preserve_intent(env: Env) -> None:
    env.scripts = RecordingScripts()
    message = env.scripts.add("看看怎么用", clarify())
    async with env.running(env.config()) as served:
        await served.page()
        assert (await served.turn(message, "query")).json()["state"] == "completed"
    (rules,) = env.scripts.instructions
    # 少量关键契约；证明规则送达，不把文字断言当作模型行为验收。
    assert "不必按固定顺序" in rules
    assert "需要时间条件时" in rules
    assert "用户请求原文时不要主动截短" in rules
    assert "不要调用 run_readonly_query" in rules
    assert "本轮每次成功执行的 run_readonly_query" in rules


async def test_delivered_rules_distinguish_pages_snapshots_and_delivery(env: Env) -> None:
    env.scripts = RecordingScripts()
    message = env.scripts.add("我没收到刚才的结果", clarify())
    async with env.running(env.config()) as served:
        await served.page()
        await served.turn(message, "query")
    (rules,) = env.scripts.instructions
    assert "续页不是重跑" in rules
    assert "结构采集 SQL 模板" in rules
    assert "不证明已送达或已读" in rules
    assert "在 inferences 中提问" in rules


async def test_describe_tool_accepts_user_supplied_names_without_search(env: Env) -> None:
    env.scripts = RecordingScripts()
    message = env.scripts.add(
        "查看 shop.sales 的结构，再让我选统计口径",
        tool_call(
            "describe_table", cluster=SR.target_id, database="shop", table="sales", cursor=None
        ),
        cite("已取得列信息；你要按哪个口径统计？"),
    )
    async with env.running(env.config()) as served:
        await served.page()
        result = (await served.turn(message, "query")).json()
    assert result["state"] == "completed"
    (fact,) = result["delivery"]["facts"]
    assert fact["tool_id"] == "local/describe_table"
    assert {r["name"] for r in fact["rows"]} >= {"region", "total"}
    assert result["delivery"]["analysis"][0]["text"].endswith("统计？")
    assert not executed_sql(env.drv)
    descriptions = {t["name"]: t["description"] for t in env.scripts.tool_specs[0]}
    assert "用户提供的准确库表名" in descriptions["describe_table"]
    assert "取自 list_tables 的搜索结果" not in descriptions["describe_table"]


async def test_visible_columns_allow_query_and_next_diagnosis_still_hides_it(env: Env) -> None:
    env.scripts = RecordingScripts()
    structure = env.scripts.add(
        "查看 shop.sales 的列",
        tool_call(
            "describe_table", cluster=SR.target_id, database="shop", table="sales", cursor=None
        ),
        cite(""),
    )
    query = env.scripts.add(
        "查这张表的 region 和 total",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        cite(""),
    )
    forbidden = env.scripts.add(
        "只解释这条 SQL，不要执行",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
    )
    async with env.running(env.config()) as served:
        await served.page()
        assert (await served.turn(structure, "query", "s1")).json()["state"] == "completed"
        result = (await served.turn(query, "query", "s2")).json()
        assert result["state"] == "completed"
        fact = next(
            f for f in result["delivery"]["facts"] if f["tool_id"] == "local/run_readonly_query"
        )
        assert fact["rows"] == [{"region": "east", "total": 100}, {"region": "west", "total": 50}]
        before = list(executed_sql(env.drv))
        denied = (await served.turn(forbidden, "diagnose", "s3")).json()
        assert denied["state"] == "failed"
        assert executed_sql(env.drv) == before  # 诊断轮没有新的业务 SQL。
    assert len(before) == 1
    assert "run_readonly_query" not in env.scripts.calls[forbidden][0].tools
