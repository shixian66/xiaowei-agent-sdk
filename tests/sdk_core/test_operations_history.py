"""H1：真 Runner/PG，可控模型与录制业务源；不证明真实模型自主性。"""

from datetime import timedelta

import pytest
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.test_app import answer, cite, evidence_in, last_output, tool_call
from tests.sdk_core.test_evidence_scope import CHAT
from tests.sdk_core.test_evidence_scope import env as env
from tests.sdk_core.test_runtime import OPERATOR

from xiaowei import runtime
from xiaowei.channel import InboundRequest

pytestmark = pytest.mark.loopback

SEARCH = "history/search_operations"
READ = "history/read_operation"
HISTORY_TOOLS = {SEARCH, READ}
OLD_ANALYSIS = "上次建议仅用于比较，尚未执行"


def configured(env):
    data = env.config().model_dump(mode="json")
    data["data_policy"]["model_tools"] += sorted(HISTORY_TOOLS)
    for tools in data["access"]["grants"].values():
        tools.extend(sorted(HISTORY_TOOLS))
    return runtime.ServeConfig.model_validate(data)


async def ask(
    env, rt, message, key, *, channel="feishu", subject="alice", conversation=CHAT, mode="query"
):
    receipt = await rt.service.accept(
        InboundRequest(
            channel=channel,
            channel_request_id=key,
            subject_id=subject,
            conversation_id=conversation,
            mode=mode,
            message=message,
            received_at=env.clock(),
        )
    )
    return await rt.service.process(receipt)


def search_step(env):
    return tool_call(
        "history__search_operations",
        start=(env.clock() - timedelta(hours=1)).isoformat(),
        end=(env.clock() + timedelta(seconds=1)).isoformat(),
        target_ids=[],
        job_id=None,
        run_id=None,
    )


@pytest.mark.parametrize(
    "channel,subject,conversation", [("feishu", "alice", CHAT), ("web", OPERATOR, "browser-a")]
)
async def test_new_generation_finds_original_facts_and_reads_analysis_without_old_context(
    env, channel, subject, conversation
):
    config = configured(env)
    env.scripts.add(
        "先查历史",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        cite(OLD_ANALYSIS),
    )
    async with env.opened(config) as rt:
        old = await ask(
            env,
            rt,
            "先查历史",
            "h1-old",
            channel=channel,
            subject=subject,
            conversation=conversation,
        )
        assert old.state == "completed"
        new = await rt.service.new_session(channel, subject, conversation)
        assert new.session_id != old.session_id
        env.db.forget()

        def read_selected(call):
            # 新一代的首个模型请求没有旧事实或诊断；只有显式历史工具才能取回。
            data = last_output(call)["data"]
            assert OLD_ANALYSIS not in str(data)
            assert data["records"][0]["facts"]
            return tool_call(
                "history__read_operation", record_ref=data["records"][0]["record_ref"]
            )(call)

        def finish(call):
            data = last_output(call)["data"]
            assert OLD_ANALYSIS in data["records"][0]["analysis"]
            assert "east" in str(data["records"][0]["facts"])
            return cite("这是历史事实，当前状态尚未核查")(call)

        def first(call):
            assert OLD_ANALYSIS not in str(call.input)
            assert "east" not in str(call.input)
            return search_step(env)(call)

        env.scripts.add("新建后查历史", first, read_selected, finish)
        result = await ask(
            env,
            rt,
            "新建后查历史",
            "h1-new",
            channel=channel,
            subject=subject,
            conversation=conversation,
            mode="diagnose" if channel == "feishu" else "query",
        )
        assert result.state == "completed", result.failure_code
        assert result.session_id == new.session_id
        assert env.db.business() == []


async def test_shared_web_operator_cannot_read_another_browser_history(env):
    config = configured(env)
    env.scripts.add(
        "浏览器A调查",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region, total FROM shop.sales"
        ),
        cite(OLD_ANALYSIS),
    )
    async with env.opened(config) as rt:
        old = await ask(
            env,
            rt,
            "浏览器A调查",
            "web-a",
            channel="web",
            subject=OPERATOR,
            conversation="cookie-a",
        )
        env.db.forget()

        def refuse(call):
            data = last_output(call)["data"]
            assert data["records"] == []
            assert "east" not in str(data) and OLD_ANALYSIS not in str(data)
            return cite("未找到当前可读历史")(call)

        env.scripts.add(
            "浏览器B猜旧编号",
            tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}"),
            refuse,
        )
        result = await ask(
            env,
            rt,
            "浏览器B猜旧编号",
            "web-b",
            channel="web",
            subject=OPERATOR,
            conversation="cookie-b",
        )
        assert result.state == "completed", result.failure_code
        assert env.db.probes() == [] and env.db.business() == []


async def seed(
    env, rt, *, message="原始调查", key="seed", sql="SELECT region, total FROM shop.sales"
):
    env.scripts.add(
        message, tool_call("run_readonly_query", cluster=SR.target_id, sql=sql), cite(OLD_ANALYSIS)
    )
    old = await ask(env, rt, message, key)
    assert old.state == "completed", old.failure_code
    await rt.service.new_session("feishu", "alice", CHAT)
    env.clock.advance(1)
    return old


def limited(config, *, rows, checks):
    data = config.model_dump(mode="json")
    data["targets"][0]["starrocks"]["policy"]["max_rows"] = rows
    data["budget"]["max_scope_checks"] = checks
    return runtime.ServeConfig.model_validate(data)


async def test_single_full_read_counts_all_old_analysis_dependencies_and_commits(env):
    # 两个不同对象均是旧分析依赖；旧策略不变，仅新轮预算取现有5倍静态最小值。
    config = limited(configured(env), rows=2, checks=1000)
    async with env.opened(config) as rt:
        env.scripts.add(
            "前一轮事实",
            tool_call(
                "run_readonly_query",
                cluster=SR.target_id,
                sql="SELECT region,total FROM shop.sales",
            ),
            cite(),
        )
        first = await ask(env, rt, "前一轮事实", "first-context")
        assert first.state == "completed"
        env.scripts.add(
            "只选择新事实",
            tool_call("run_readonly_query", cluster=SR.target_id, sql="SELECT * FROM shop.regions"),
            lambda call: answer(evidence_in(call)[-1:], OLD_ANALYSIS),
        )
        old = await ask(env, rt, "只选择新事实", "last-context")
        assert old.state == "completed" and len(old.context_evidence) == 2
        await rt.service.new_session("feishu", "alice", CHAT)
    async with env.opened(limited(config, rows=2, checks=10)) as rt:

        def finish(call):
            data = last_output(call)["data"]
            assert OLD_ANALYSIS in data["records"][0]["analysis"]
            return cite("历史分析需结合现状验证")(call)

        env.scripts.add(
            "满额历史读取",
            tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}"),
            finish,
        )
        result = await ask(env, rt, "满额历史读取", "full-read")
        assert result.state == "completed", result.failure_code
        assert len(env.db.probes()) == 10
        assert env.db.business() == []


async def test_single_search_deduplicates_shared_object_across_original_turns(env):
    config = limited(configured(env), rows=1, checks=1000)
    async with env.opened(config) as rt:
        for n in range(3):
            await seed(env, rt, message=f"旧调查{n}", key=f"old-{n}")
    async with env.opened(limited(config, rows=1, checks=1000)) as rt:

        def finish(call):
            assert len(last_output(call)["data"]["records"]) == 3
            return cite("三次记录，同一个对象")(call)

        env.scripts.add("单次查三条", search_step(env), finish)
        result = await ask(env, rt, "单次查三条", "shared-object")
        assert result.state == "completed", result.failure_code
        assert len(env.db.probes()) == 5
        assert env.db.business() == []


async def test_earlier_candidate_flag_uses_metadata_without_probing_omitted_object(env):
    config = limited(configured(env), rows=1, checks=1000)
    async with env.opened(config) as rt:
        await seed(env, rt, message="更早对象", key="earlier", sql="SELECT * FROM shop.regions")
        await seed(env, rt, message="较新对象", key="newer")
        env.db.forget()

        def finish(call):
            data = last_output(call)["data"]
            assert len(data["records"]) == 1
            assert data["has_earlier"] is True and data["gaps"]["truncated"] is True
            return cite("还有更早候选，尚未完整读取")(call)

        env.scripts.add("有无更早", search_step(env), finish)
        result = await ask(env, rt, "有无更早", "earlier-flag")
        assert result.state == "completed", result.failure_code
        assert not any("regions" in sql for sql in env.db.probes())


@pytest.mark.parametrize("method", ["search_operations", "read_operation"])
async def test_source_grant_revoked_before_lookup_returns_no_business_content_or_io(env, method):
    config = configured(env)
    async with env.opened(config) as rt:
        old = await seed(env, rt)
        rt.service.results.access._grants["alice"] = frozenset(HISTORY_TOOLS)
        env.db.forget()
        step = (
            search_step(env)
            if method == "search_operations"
            else tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}")
        )

        def finish(call):
            data = last_output(call)["data"]
            assert data["records"] == []
            assert "east" not in str(data) and OLD_ANALYSIS not in str(data)
            return cite("未找到当前可读事实")(call)

        env.scripts.add("撤权后查询", step, finish)
        result = await ask(env, rt, "撤权后查询", "revoked-before")
        assert result.state == "completed", result.failure_code
        assert env.db.business() == [] and env.db.probes() == []


@pytest.mark.parametrize("change", ["grant", "fingerprint"])
async def test_copied_history_rechecks_original_source_before_final_answer(env, change):
    from sqlalchemy import text

    config = configured(env)
    async with env.opened(config) as rt:
        old = await seed(env, rt)

        async def finish(call):
            assert OLD_ANALYSIS in last_output(call)["data"]["records"][0]["analysis"]
            if change == "grant":
                rt.service.results.access._grants["alice"] = frozenset(HISTORY_TOOLS)
            else:
                async with rt.engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE xiaowei_evidence SET policy_fingerprint='invalid' "
                            "WHERE turn_id=:turn"
                        ),
                        {"turn": old.turn_id},
                    )
            env.db.forget()
            return cite("不可交付的旧分析")(call)

        env.scripts.add(
            "复制后变更",
            tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}"),
            finish,
        )
        result = await ask(env, rt, "复制后变更", "revoked-after")
        assert result.state == "failed" and result.answer is None
        assert env.db.business() == [] and env.db.probes() == []


async def test_web_cookie_entry_new_session_and_other_browser_have_distinct_history(env):
    import httpx

    from xiaowei.web import create_web_app

    config = configured(env)
    async with env.opened(config) as rt:
        app = create_web_app(rt.service, config.web)
        origin = next(iter(config.web.allowed_origins))
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(
                transport=transport, base_url=origin, headers={"Origin": origin}
            ) as a,
            httpx.AsyncClient(
                transport=transport, base_url=origin, headers={"Origin": origin}
            ) as b,
        ):
            assert (await a.get("/")).status_code == 200
            assert (await b.get("/")).status_code == 200
            assert dict(a.cookies) != dict(b.cookies)
            env.scripts.add(
                "A入口调查",
                tool_call(
                    "run_readonly_query",
                    cluster=SR.target_id,
                    sql="SELECT region, total FROM shop.sales",
                ),
                cite(OLD_ANALYSIS),
            )
            response = await a.post(
                "/api/turns",
                json={"request_id": "web-original", "mode": "query", "message": "A入口调查"},
            )
            assert response.json()["state"] == "completed"
            turn_id = await env.scalar("SELECT turn_id FROM xiaowei_request")
            assert (await a.post("/api/sessions", json={})).status_code == 200
            env.scripts.add(
                "A新建后读取",
                tool_call("history__read_operation", record_ref=f"turn:{turn_id}"),
                cite("历史结果需要当前核查"),
            )
            response = await a.post(
                "/api/turns",
                json={"request_id": "web-own", "mode": "query", "message": "A新建后读取"},
            )
            body = response.json()
            assert body["state"] == "completed" and "east" in str(body["delivery"])
            result_json = body["delivery"]["facts"][0]["result_json"]
            assert '"sources"' not in result_json and turn_id not in result_json
            env.db.forget()

            def finish(call):
                assert last_output(call)["data"]["records"] == []
                return cite("本浏览器没有当前可读记录")(call)

            env.scripts.add(
                "B入口读取",
                tool_call("history__read_operation", record_ref=f"turn:{turn_id}"),
                finish,
            )
            response = await b.post(
                "/api/turns",
                json={"request_id": "web-other", "mode": "query", "message": "B入口读取"},
            )
            assert response.json()["state"] == "completed" and "east" not in str(
                response.json()["delivery"]
            )
            assert env.db.probes() == [] and env.db.business() == []


async def test_missing_request_keeps_independent_original_facts_and_original_expiry(env):
    from sqlalchemy import text

    config = configured(env)
    async with env.opened(config) as rt:
        old = await seed(env, rt)
        expiry = await env.scalar(
            "SELECT expires_at FROM xiaowei_evidence WHERE turn_id=:turn", turn=old.turn_id
        )
        async with rt.engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM xiaowei_request WHERE turn_id=:turn"), {"turn": old.turn_id}
            )
        env.clock.advance(60)

        def finish(call):
            data = last_output(call)["data"]
            assert data["records"][0]["facts"]
            assert "analysis" not in data["records"][0]
            assert data["gaps"]["回答缺失"] is True
            return cite("仅保留原始事实，调查结论缺失")(call)

        env.scripts.add(
            "请求已清理",
            tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}"),
            finish,
        )
        result = await ask(env, rt, "请求已清理", "cleaned-request")
        assert result.state == "completed", result.failure_code
        assert (
            await env.scalar(
                "SELECT expires_at FROM xiaowei_evidence WHERE turn_id=:turn", turn=result.turn_id
            )
            == expiry
        )
        assert (
            await env.scalar(
                "SELECT expires_at FROM xiaowei_request WHERE turn_id=:turn", turn=result.turn_id
            )
            == expiry
        )


@pytest.mark.parametrize("condition", ["deleted", "expired"])
async def test_cleared_original_evidence_never_recovers_old_analysis_from_request(env, condition):
    from sqlalchemy import text

    config = configured(env)
    async with env.opened(config) as rt:
        old = await seed(env, rt)
        async with rt.engine.begin() as conn:
            if condition == "deleted":
                await conn.execute(
                    text("DELETE FROM xiaowei_evidence WHERE turn_id=:turn"), {"turn": old.turn_id}
                )
            else:
                await conn.execute(
                    text("UPDATE xiaowei_evidence SET expires_at=:past WHERE turn_id=:turn"),
                    {"turn": old.turn_id, "past": env.clock() - timedelta(seconds=1)},
                )
        env.db.forget()

        def finish(call):
            data = last_output(call)["data"]
            assert OLD_ANALYSIS not in str(data) and "east" not in str(data)
            assert data["gaps"]
            return cite("历史证据已缺失，无法恢复调查结论")(call)

        env.scripts.add(
            "旧证据缺失",
            tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}"),
            finish,
        )
        result = await ask(env, rt, "旧证据缺失", "cleaned-evidence")
        assert result.state == "completed", result.failure_code
        assert env.db.probes() == [] and env.db.business() == []


async def test_invalid_history_window_can_be_corrected_by_runner_without_source_io(env):
    config = configured(env)
    async with env.opened(config) as rt:
        old = await seed(env, rt)
        env.db.forget()

        def correct(call):
            rejection = last_output(call)
            assert rejection["status"] == "rejected"
            return tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}")(call)

        env.scripts.add(
            "修正历史参数",
            tool_call(
                "history__search_operations",
                start="昨天",
                end="今天",
                target_ids=[],
                job_id=None,
                run_id=None,
            ),
            correct,
            cite("根据原事实给出历史结论"),
        )
        result = await ask(env, rt, "修正历史参数", "correct-window")
        assert result.state == "completed", result.failure_code
        assert env.db.business() == []


async def test_history_storage_failure_stops_turn_instead_of_returning_empty_history(env):
    from sqlalchemy import text

    config = configured(env)
    async with env.opened(config) as rt:
        old = await seed(env, rt)
        async with rt.engine.begin() as conn:
            await conn.execute(
                text(
                    "ALTER TABLE xiaowei_scheduled_run RENAME COLUMN turn_id TO fixture_broken_turn"
                )
            )
        env.db.forget()
        env.scripts.add(
            "历史库故障", tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}")
        )
        result = await ask(env, rt, "历史库故障", "storage-failed")
        assert result.state == "failed" and result.answer is None
        # execute阶段存储故障沿既有ToolExecutionError终止，不增加治理错误类别。
        assert result.failure_code == "evidence_failed"
        assert env.db.probes() == [] and env.db.business() == []


async def test_large_old_analysis_is_omitted_while_small_independent_facts_survive(env):
    data = configured(env).model_dump(mode="json")
    data["projection_bytes"] = dict.fromkeys(data["projection_bytes"], 8000)
    data["targets"][0]["starrocks"]["policy"]["max_sql_bytes"] = 300
    data["targets"][0]["starrocks"]["max_result_bytes"] = 2000
    data["targets"][0]["starrocks"]["max_value_bytes"] = 1000
    config = runtime.ServeConfig.model_validate(data)
    long_analysis = "未经验证的旧解释" * 500
    env.scripts.add(
        "长结论",
        tool_call(
            "run_readonly_query", cluster=SR.target_id, sql="SELECT region,total FROM shop.sales"
        ),
        cite(long_analysis),
    )
    async with env.opened(config) as rt:
        old = await ask(env, rt, "长结论", "long-old")
        assert old.state == "completed"
        await rt.service.new_session("feishu", "alice", CHAT)

        def finish(call):
            data = last_output(call)["data"]
            assert "east" in str(data["records"][0]["facts"])
            assert "analysis" not in data["records"][0]
            assert data["gaps"]["truncated"] is True
            assert "回答缺失" not in data["gaps"]
            return cite("历史分析超限，仅返回原事实")(call)

        env.scripts.add(
            "读取长结论",
            tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}"),
            finish,
        )
        result = await ask(env, rt, "读取长结论", "long-new")
        assert result.state == "completed", result.failure_code


async def test_search_keeps_valid_original_facts_and_marks_skipped_record_gap(env):
    from sqlalchemy import text

    config = configured(env)
    async with env.opened(config) as rt:
        bad = await seed(env, rt, message="旧失效记录", key="bad-old")
        good = await seed(env, rt, message="旧有效记录", key="good-old")
        async with rt.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE xiaowei_evidence SET policy_fingerprint='invalid' WHERE turn_id=:turn"
                ),
                {"turn": bad.turn_id},
            )
        env.db.forget()

        def finish(call):
            data = last_output(call)["data"]
            assert [r["record_ref"] for r in data["records"]] == [f"turn:{good.turn_id}"]
            assert data["gaps"]["Evidence 不可读"] is True
            return cite("部分原事实仍可读，覆盖有缺口")(call)

        env.scripts.add("查有效部分", search_step(env), finish)
        result = await ask(env, rt, "查有效部分", "partial-valid")
        assert result.state == "completed", result.failure_code
        assert env.db.business() == []


async def test_candidate_scan_limit_is_reported_when_all_scanned_records_are_unreadable(env):
    from sqlalchemy import text

    config = limited(configured(env), rows=200, checks=1000)
    async with env.opened(config) as rt:
        good = await seed(env, rt, message="较早有效", key="earlier-good")
        for n in range(21):
            await seed(env, rt, message=f"新失效{n}", key=f"new-invalid-{n}")
        async with rt.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE xiaowei_evidence SET policy_fingerprint='invalid' WHERE turn_id<>:good"
                ),
                {"good": good.turn_id},
            )

        def finish(call):
            data = last_output(call)["data"]
            assert data["records"] == [] and data["gaps"]["truncated"] is True
            return cite("扫描受限，不能证明更早没有调查")(call)

        env.scripts.add("扫描达到上限", search_step(env), finish)
        result = await ask(env, rt, "扫描达到上限", "scan-cap")
        assert result.state == "completed", result.failure_code


async def test_unmatched_target_filter_can_be_followed_by_known_authorized_target(env):
    config = configured(env)
    async with env.opened(config) as rt:
        await seed(env, rt)
        env.db.forget()

        def correct(call):
            assert last_output(call)["data"]["records"] == []
            assert env.db.probes() == [] and env.db.business() == []
            return tool_call(
                "history__search_operations",
                start=(env.clock() - timedelta(hours=1)).isoformat(),
                end=(env.clock() + timedelta(seconds=1)).isoformat(),
                target_ids=[SR.target_id],
                job_id=None,
                run_id=None,
            )(call)

        env.scripts.add(
            "先按未知筛选，再查明确指定的目标",
            tool_call(
                "history__search_operations",
                start=(env.clock() - timedelta(hours=1)).isoformat(),
                end=(env.clock() + timedelta(seconds=1)).isoformat(),
                target_ids=["unmatched-target"],
                job_id=None,
                run_id=None,
            ),
            correct,
            cite("仅有指定获准目标的历史事实"),
        )
        result = await ask(env, rt, "先按未知筛选，再查明确指定的目标", "filter-correction")
        assert result.state == "completed", result.failure_code
        assert env.db.business() == []


async def test_search_preserves_projection_truncation_and_saved_incomplete_markers(env):
    import json

    from sqlalchemy import text

    config = configured(env)
    async with env.opened(config) as rt:
        old = await seed(env, rt)
        async with rt.engine.begin() as conn:
            projection = json.loads(
                await conn.scalar(
                    text("SELECT model_content FROM xiaowei_evidence WHERE turn_id=:turn"),
                    {"turn": old.turn_id},
                )
            )
            projection["truncated"] = True  # 原始查询未截断，但其获准投影曾省略字段。
            answer = json.loads(
                await conn.scalar(
                    text("SELECT answer FROM xiaowei_request WHERE turn_id=:turn"),
                    {"turn": old.turn_id},
                )
            )
            answer["limit_reached"] = True
            await conn.execute(
                text("UPDATE xiaowei_evidence SET model_content=:body WHERE turn_id=:turn"),
                {"turn": old.turn_id, "body": json.dumps(projection)},
            )
            await conn.execute(
                text("UPDATE xiaowei_request SET answer=:body WHERE turn_id=:turn"),
                {"turn": old.turn_id, "body": json.dumps(answer)},
            )

        def finish(call):
            data = last_output(call)["data"]
            assert data["records"][0]["facts"][0]["truncated"] is True
            assert data["gaps"]["truncated"] is True and data["gaps"]["limit_reached"] is True
            assert OLD_ANALYSIS not in str(data)
            return cite("历史投影及调查均不完整")(call)

        env.scripts.add("查历史完整性", search_step(env), finish)
        result = await ask(env, rt, "查历史完整性", "historical-gaps")
        assert result.state == "completed", result.failure_code


async def test_unreadable_old_analysis_keeps_independently_authorized_facts(env):
    import json

    from sqlalchemy import text

    config = configured(env)
    async with env.opened(config) as rt:
        old = await seed(env, rt)
        async with rt.engine.begin() as conn:
            saved = json.loads(
                await conn.scalar(
                    text("SELECT answer FROM xiaowei_request WHERE turn_id=:turn"),
                    {"turn": old.turn_id},
                )
            )
            # 已退役的失败源会使完整旧分析不可交付；它不影响独立的查询事实。
            saved["monitoring_failures"] = [
                {
                    "tool_id": "retired-monitor/get_metrics",
                    "target_id": "retired-monitor",
                    "reason": "timeout",
                }
            ]
            await conn.execute(
                text("UPDATE xiaowei_request SET answer=:body WHERE turn_id=:turn"),
                {"turn": old.turn_id, "body": json.dumps(saved)},
            )
        env.db.forget()

        def finish(call):
            data = last_output(call)["data"]
            assert data["records"][0]["facts"]
            assert "east" in str(data["records"][0]["facts"])
            assert "analysis" not in data["records"][0]
            assert data["gaps"]["monitoring_failures"] is True
            assert "回答缺失" not in data["gaps"]  # 回答存在，只是其来源当前不可验证。
            assert OLD_ANALYSIS not in str(data) and "retired-monitor" not in str(data)
            return cite("只有独立历史事实可确认，旧分析无法交付")(call)

        env.scripts.add(
            "只保留独立历史事实",
            tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}"),
            finish,
        )
        result = await ask(env, rt, "只保留独立历史事实", "analysis-fallback")
        assert result.state == "completed", result.failure_code
        assert env.db.business() == []


@pytest.mark.parametrize("exhausted", [True, False])
async def test_budget_exhaustion_is_not_reported_as_unreadable_source(env, exhausted):
    from xiaowei.evidence import EvidenceUnverifiableError, scope_checks
    from xiaowei.models import Identity, RunContext, ToolRequest

    config = configured(env)
    async with env.opened(config) as rt:
        old = await seed(env, rt)
        current = await rt.service.new_session("feishu", "alice", CHAT)
        history = rt.service._app.history
        assert history is not None
        ctx = RunContext(
            identity=Identity(
                subject_id="alice",
                session_id=current.session_id,
                turn_id="budget-check",
                channel="feishu",
                owner=old.owner,
            ),
            target_scope=frozenset({"history", SR.target_id}),
            tool_scope=frozenset(config.data_policy.model_tools),
            budget=config.budget,
        )
        request = ToolRequest(
            tool_id=READ,
            target_id="history",
            call_id="read-budget",
            tool_name="history__read_operation",
            arguments={"record_ref": f"turn:{old.turn_id}"},
        )
        env.db.forget()
        env.db.down = not exhausted
        with scope_checks(used=config.budget.max_scope_checks if exhausted else 0):
            if exhausted:
                with pytest.raises(EvidenceUnverifiableError):
                    await history.execute(ctx, request)
                assert env.db.probes() == []  # 耗尽在任何源探测之前终止，不能返回假不可读。
            else:
                result = await history.execute(ctx, request)
                assert result.payload["records"] == []
                assert result.payload["gaps"]["Evidence 不可读"] is True
        assert env.db.business() == []


@pytest.mark.parametrize("read", [False, True])
async def test_many_original_evidence_are_bounded_independently_of_sql_row_limit(env, read):
    from sqlalchemy import text

    data = configured(env).model_dump(mode="json")
    data["budget"].update(max_tool_calls=11, max_turns=13)
    config = runtime.ServeConfig.model_validate(data)
    async with env.opened(config) as rt:
        env.scripts.add(
            "多次取证的原调查",
            *(
                tool_call(
                    "run_readonly_query",
                    cluster=SR.target_id,
                    sql="SELECT region,total FROM shop.sales",
                )
                for _ in range(11)
            ),
            cite(OLD_ANALYSIS),
        )
        old = await ask(env, rt, "多次取证的原调查", "many-originals")
        assert old.state == "completed", old.failure_code
        assert len(old.context_evidence) == 11
        if read:
            # 详情在原回答无法解析时走纯事实路径；正常分析不受10份计数限制。
            async with rt.engine.begin() as conn:
                await conn.execute(
                    text("UPDATE xiaowei_request SET answer='{}' WHERE turn_id=:turn"),
                    {"turn": old.turn_id},
                )
        await rt.service.new_session("feishu", "alice", CHAT)

        def finish(call):
            data = last_output(call)["data"]
            assert len(data["records"][0]["facts"]) == 10
            assert data["gaps"]["truncated"] is True
            assert OLD_ANALYSIS not in str(data)
            assert data["gaps"].get("回答缺失", False) is read
            return cite("原始事实较多，本次只交付有界部分")(call)

        env.scripts.add(
            "读取有界原事实",
            tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}")
            if read
            else search_step(env),
            finish,
        )
        result = await ask(env, rt, "读取有界原事实", "bounded-originals")
        assert result.state == "completed", result.failure_code


@pytest.mark.parametrize("capacity_limited", [False, True])
async def test_last_conclusion_after_four_turns_keeps_complete_replayed_context(
    env, capacity_limited
):
    data = configured(env).model_dump(mode="json")
    data["budget"]["max_tool_calls"] = 4
    if capacity_limited:
        data["projection_bytes"] = dict.fromkeys(data["projection_bytes"], 8000)
        data["targets"][0]["starrocks"]["policy"]["max_sql_bytes"] = 300
        data["targets"][0]["starrocks"]["max_result_bytes"] = 2000
        data["targets"][0]["starrocks"]["max_value_bytes"] = 1000
    config = runtime.ServeConfig.model_validate(data)
    conclusion = "最后一轮的历史结论" * (500 if capacity_limited else 1)
    async with env.opened(config) as rt:
        for n in range(4):
            message = f"同会话第{n + 1}轮"
            env.scripts.add(
                message,
                *(
                    tool_call(
                        "run_readonly_query",
                        cluster=SR.target_id,
                        sql="SELECT region,total FROM shop.sales",
                    )
                    for _ in range(4)
                ),
                cite(conclusion if n == 3 else "先前一轮结论"),
            )
            old = await ask(env, rt, message, f"context-turn-{n}")
            assert old.state == "completed", old.failure_code
        assert len(old.context_evidence) == 16
        await rt.service.new_session("feishu", "alice", CHAT)
        env.db.forget()

        def finish(call):
            data = last_output(call)["data"]
            record = data["records"][0]
            assert "回答缺失" not in data["gaps"]
            if capacity_limited:
                assert "analysis" not in record
                assert len(record["facts"]) == 4  # 超字节后只取原轮次事实，保持来源绑定。
                assert data["gaps"]["truncated"] is True
            else:
                assert record["analysis"] == conclusion
                assert len(record["facts"]) == 16
                assert not data["gaps"].get("truncated", False)
            return cite("历史结论或本次可交付的独立事实")(call)

        env.scripts.add(
            "新建后读最后一轮",
            tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}"),
            finish,
        )
        result = await ask(env, rt, "新建后读最后一轮", "last-context")
        assert result.state == "completed", result.failure_code
        assert env.db.business() == []


async def test_object_limit_omits_analysis_without_claiming_saved_answer_is_missing(env):
    config = limited(configured(env), rows=1, checks=1000)
    async with env.opened(config) as rt:
        for n, sql in enumerate(("SELECT * FROM shop.sales", "SELECT * FROM shop.regions")):
            message = f"调查对象{n}"
            env.scripts.add(
                message,
                tool_call("run_readonly_query", cluster=SR.target_id, sql=sql),
                cite(OLD_ANALYSIS),
            )
            old = await ask(env, rt, message, f"object-turn-{n}")
            assert old.state == "completed", old.failure_code
        assert len(old.context_evidence) == 2
        await rt.service.new_session("feishu", "alice", CHAT)

        def finish(call):
            data = last_output(call)["data"]
            assert "analysis" not in data["records"][0]
            assert data["gaps"]["truncated"] is True
            assert "回答缺失" not in data["gaps"]
            assert "shop.regions" in str(data["records"][0]["facts"])
            assert "shop.sales" not in str(data["records"][0]["facts"])
            return cite("完整分析依赖超过对象上限，仅返回当轮独立事实")(call)

        env.scripts.add(
            "按对象上限读历史",
            tool_call("history__read_operation", record_ref=f"turn:{old.turn_id}"),
            finish,
        )
        result = await ask(env, rt, "按对象上限读历史", "object-limited-history")
        assert result.state == "completed", result.failure_code
