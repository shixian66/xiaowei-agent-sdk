"""P2.5 Task 7：单 Agent 按自然语言决定是否查询；显式诊断仍收窄（离线）。

正式装配 ``runtime.open_runtime`` + ``ChannelService`` + 隔离的真实 PostgreSQL；只替换最底层 I/O
（同 ``test_evidence_scope``）：模型是按消息分派的 HTTP 脚本，StarRocks 是 recording 驱动替身。

脚本模型的选择是预设的，这里证明的是调用链与代码保护：只有一个业务 Agent、没有额外的分类请求；
查询工具按授权可见；模型误调查询时只读账号、SQLGuard、当前权限与资源限额照常约束（通过这些保护
的只读 SQL 会被执行，这是用户接受的残余）；显式诊断在执行前拒绝查询；澄清与未执行建议单独交付，
本轮开始过业务查询时不能以它们收尾。脚本不能证明真实模型会在“只解释/不要执行”时不选查询工具：
那由 ``tests/sdk_core/gate0.py`` 的固定样例在 P3 获准模型上评估。
"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from agents.testing import assistant_message
from asyncmy.errors import OperationalError
from pydantic import SecretStr
from sqlalchemy import text
from tests.sdk_core.test_app import SEARCH_ALL, ModelCall, cite, clarify, tool_call
from tests.sdk_core.test_evidence_scope import CHAT, Database, Env, Outbox, ref
from tests.sdk_core.test_evidence_scope import env as env  # pytest fixture
from tests.sdk_core.test_runtime import PLAN, cluster_config

from xiaowei import runtime
from xiaowei.app import Mode
from xiaowei.channel import InboundRequest
from xiaowei.channel_store import RequestRecord
from xiaowei.starrocks import EXPLAIN_PREFIX
from xiaowei.storage import open_engine

pytestmark = pytest.mark.loopback

ADVICE_HEADER = "建议（本轮未执行查询；模型生成，未经系统核实）"
QUERY = "run_readonly_query"
SALES_SQL = "SELECT region, total FROM sales"


def advise(text: str = "SELECT region, SUM(total) AS total FROM sales GROUP BY region") -> Any:
    body = {"evidence_ids": [], "inferences": [], "clarification": None, "advice": text}
    return lambda call: [assistant_message(json.dumps(body, ensure_ascii=False))]


def run_query(sql: str = SALES_SQL, cluster: str = "sr-test") -> Any:
    return tool_call(QUERY, cluster=cluster, sql=sql)


async def turn(
    env: Env, rt: runtime.Runtime, message: str, request_id: str, mode: Mode = "query"
) -> RequestRecord:
    receipt = await rt.service.accept(
        InboundRequest(
            channel="feishu",
            channel_request_id=request_id,
            subject_id="alice",
            conversation_id=CHAT,
            mode=mode,
            message=message,
            received_at=datetime.now(UTC),
        )
    )
    return await rt.service.process(receipt)


async def delivered(rt: runtime.Runtime, request_id: str) -> str:
    outbox = Outbox()
    await rt.service.results.send(ref(request_id), outbox)
    (delivery,) = outbox.sent
    return delivery.content


def outputs(call: ModelCall) -> list[str]:
    return [i["output"] for i in call.input if i.get("type") == "function_call_output"]


# ---- 调用链：一个业务 Agent，查询工具按授权可见 ------------------------------------------------


async def test_an_ordinary_message_is_one_business_agent_that_may_query(env: Env) -> None:
    async with env.opened(env.config()) as rt:
        message = env.scripts.add("昨天各地区销售额", run_query(), cite())
        record = await turn(env, rt, message, "om_1")
        assert record.state == "completed", record.failure_code
        # 只有业务 Agent 的两次模型请求（调用工具、最终回答），没有前置分类请求。
        assert len(env.scripts.calls[message]) == 2
        assert all(QUERY in seen for seen in env.scripts.tools_seen(message))
        assert {"list_tables", "describe_table", "explain_query"} <= env.scripts.tools_seen(
            message
        )[0]
        assert len(env.db.business()) == 1


async def test_explicit_diagnosis_hides_and_refuses_a_forced_query(env: Env) -> None:
    async with env.opened(env.config()) as rt:
        message = env.scripts.add("/诊断 这条为什么慢", run_query(), cite())
        record = await turn(env, rt, message, "om_1", mode="diagnose")
        # 可信入口收窄：工具不展示，强行调用在 I/O 前失败，业务 SQL 为 0。
        assert all(QUERY not in seen for seen in env.scripts.tools_seen(message))
        assert record.state == "failed" and env.db.business() == []


# ---- 业务正例：跨库与复杂 SQL、可修正拒绝、随后诊断、换集群比较 ---------------------------------

CROSS_DB = (
    "WITH t AS (SELECT region, total FROM shop.sales) "
    "SELECT t.region, r.name, RANK() OVER (ORDER BY t.total DESC) AS place "
    "FROM t JOIN shop.regions AS r ON t.region = r.region"
)
UNION = "SELECT region FROM sales UNION ALL SELECT region FROM regions"


def ranking_cluster(target_id: str) -> dict[str, Any]:
    """合成目标默认只允许 COUNT/SUM；排名用例另外允许 RANK（窗口排名）。"""
    config = cluster_config(target_id)
    policy = config["starrocks"]["policy"]
    policy["allowed_functions"] = [*policy["allowed_functions"], "RANK"]
    return config


async def test_a_business_task_spans_turns_and_clusters(env: Env) -> None:
    other = Database()
    env.drivers = {"sr-b": other.drv}
    config = env.config(targets=[ranking_cluster("sr-test"), cluster_config("sr-b")])
    async with env.opened(config) as rt:
        other.forget()
        first = env.scripts.add(
            "sr-test 昨天各地区销售排名，和地区表关联",
            run_query("SELECT region FROM nowhere"),  # 不在快照中：I/O 前拒绝，可修正
            run_query(CROSS_DB),
            run_query(UNION),
            cite(),
        )
        record = await turn(env, rt, first, "om_1")
        assert record.state == "completed", record.failure_code
        rejected = outputs(env.scripts.calls[first][1])[-1]
        assert "evidence_id" not in rejected and "未执行" in rejected
        ran = env.db.business()
        assert len(ran) == 2 and "RANK() OVER" in ran[0] and "UNION ALL" in ran[1]

        # 随后解释为什么慢：只取上一轮实际 SQL 的计划，不再执行业务 SQL。
        explained = EXPLAIN_PREFIX + ran[0]
        env.db.overrides = {explained: PLAN}
        why = env.scripts.add(
            "刚才的排名为什么慢",
            tool_call("explain_query", cluster="sr-test", sql=ran[0]),
            cite(),
        )
        assert (await turn(env, rt, why, "om_2")).state == "completed"
        sent = [sql for conn in env.db.drv.connections for sql, _ in conn.executed]
        assert explained in sent and len(env.db.business()) == 2

        # 换另一集群比较：只发往指定的 sr-b。
        compare = env.scripts.add("换 sr-b 比较一下", run_query(cluster="sr-b"), cite())
        assert (await turn(env, rt, compare, "om_3")).state == "completed"
        assert len(other.business()) == 1 and len(env.db.business()) == 2


async def test_a_resource_limit_stops_the_turn_without_rerunning(env: Env) -> None:
    env.db.query = OperationalError(1064, "Memory of Query canary-mem exceed limit")  # type: ignore[assignment]
    async with env.opened(env.config()) as rt:
        message = env.scripts.add("全量明细", run_query(), cite(), run_query(), cite())
        record = await turn(env, rt, message, "om_1")
        assert (record.state, record.failure_code) == ("failed", "evidence_failed")
        # 业务 SQL 只执行了一次；模型没有得到第二次机会，本轮不自动重跑。
        assert len(env.db.business()) == 1 and len(env.scripts.calls[message]) == 1
        receipt = await delivered(rt, "om_1")
        assert "缩小" in receipt and "canary-mem" not in receipt


# ---- 反例中的误调：保护照常生效 -----------------------------------------------------------------


async def test_a_forced_query_in_a_do_not_run_message_is_still_guarded(env: Env) -> None:
    async with env.opened(env.config()) as rt:
        # 越权的 SQL：I/O 前拒绝；模型据拒绝原因只给建议，不伪装成查询结果。
        refused = env.scripts.add(
            "不要执行，只看看这条 SQL：SELECT secret FROM sales",
            run_query("SELECT secret FROM sales"),
            advise(),
        )
        record = await turn(env, rt, refused, "om_1")
        assert record.state == "completed", record.failure_code
        assert env.db.business() == []
        content = await delivered(rt, "om_1")
        assert content.startswith(ADVICE_HEADER) and "工具结果" not in content

        # 获准的只读 SQL：通过权限、SQLGuard 与资源检查后会被执行（用户接受的残余，见模块说明）。
        allowed = env.scripts.add("不要执行，解释一下这条：" + SALES_SQL, run_query(), cite())
        record = await turn(env, rt, allowed, "om_2")
        assert record.state == "completed", record.failure_code
        (ran,) = env.db.business()
        assert ran.startswith("SELECT") and "LIMIT" in ran  # 经 SQLGuard 规范化、加了行数上限


async def test_advice_cannot_close_a_turn_that_ran_a_query(env: Env) -> None:
    async with env.opened(env.config()) as rt:
        for request_id, finish in (("om_1", advise()), ("om_2", clarify())):
            message = env.scripts.add(f"查了却不引用 {request_id}", run_query(), finish)
            record = await turn(env, rt, message, request_id)
            # “本轮未执行查询”必须属实：开始过业务查询的轮次不能以建议或澄清收尾。
            assert (record.state, record.failure_code) == ("failed", "evidence_failed")
        # 元数据工具不是业务查询：查过表结构后仍可澄清。
        searched = env.scripts.add(
            "哪张表有地区", tool_call("list_tables", cluster="sr-test", **SEARCH_ALL), clarify()
        )
        assert (await turn(env, rt, searched, "om_3")).state == "completed"


# ---- 回答分支的交付、回放与历史 ----------------------------------------------------------------


async def test_advice_and_clarification_are_delivered_apart_and_replayed(env: Env) -> None:
    async with env.opened(env.config()) as rt:
        write = env.scripts.add("帮我写一条按地区汇总销售额的 SQL，先别执行", advise())
        assert (await turn(env, rt, write, "om_1")).state == "completed"
        vague = env.scripts.add("查一下订单", clarify("请说明集群、时间范围与订单口径"))
        assert (await turn(env, rt, vague, "om_2")).state == "completed"
        assert env.db.business() == []

        advice, question = await delivered(rt, "om_1"), await delivered(rt, "om_2")
        assert advice.splitlines()[0] == ADVICE_HEADER and "GROUP BY region" in advice
        assert question.splitlines()[0] == "需要澄清（本轮未执行查询）"
        # 历史读取得到同样的内容。
        assert (await rt.service.results.view(ref("om_1"))).delivery.content == advice

        # 同会话追问：上一轮的建议作为模型自己的历史回放，不是工具结果，也不授予查询许可。
        follow = env.scripts.add("就按刚才的写法", advise("沿用上一条建议"))
        assert (await turn(env, rt, follow, "om_3")).state == "completed"
        replayed = json.dumps(env.scripts.calls[follow][0].input, ensure_ascii=False)
        assert "GROUP BY region" in replayed and outputs(env.scripts.calls[follow][0]) == []
        assert env.db.business() == []


async def test_answers_saved_before_advice_existed_are_still_delivered(env: Env) -> None:
    async with env.opened(env.config()) as rt:
        vague = env.scripts.add("查一下订单", clarify())
        assert (await turn(env, rt, vague, "om_1")).state == "completed"
        # 模拟此前版本保存的回答：没有 advice 字段。
        old = '{"evidence_ids": [], "inferences": [], "clarification": "请说明地区"}'
        async with open_engine(SecretStr(env.url.render_as_string(hide_password=False))) as e:
            async with e.begin() as conn:
                await conn.execute(text("UPDATE xiaowei_request SET answer = :old"), {"old": old})
        view = await rt.service.results.view(ref("om_1"))
        assert view.delivery.content == "需要澄清（本轮未执行查询）\n请说明地区"
