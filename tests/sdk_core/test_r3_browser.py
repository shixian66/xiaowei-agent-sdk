"""R3 visible/folded behavior in real Chrome and formal startup on a free port."""

import pytest
from tests.p1b.test_starrocks_adapter import TARGET as SR
from tests.sdk_core.browser import launch
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_r3_entrypoints import brief
from tests.sdk_core.test_runtime import Env, statements
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_web_browser import item, send, settled

pytestmark = [pytest.mark.loopback, pytest.mark.browser]


async def test_real_chrome_brief_and_explicit_full_history(env: Env, chrome_binary: str):
    assert env.port != 8501
    full = env.scripts.add(
        "Read five sales",
        tool_call(
            "run_readonly_query",
            cluster=SR.target_id,
            sql="SELECT region, total FROM shop.sales LIMIT 5",
        ),
        cite(""),
    )
    short = env.scripts.add("How many came back?", brief)
    original = env.scripts.add("Show the original rows again", cite(""))
    async with env.running(env.config()), launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate(env.origin + "/")
        await send(page, full, "query")
        await settled(page, 0, "completed")
        assert (
            await page.evaluate(f"{item(0)}.querySelector('.table-wrap').getClientRects().length")
            == 1
        )
        await send(page, short, "query")
        await settled(page, 1, "completed")
        assert (
            await page.evaluate(f"{item(1)}.querySelector('.fact-summary').textContent")
            == "\u672c\u6b21\u8fd4\u56de\u884c\u6570\uff1a2\u3002"
        )
        visibility = await page.evaluate(
            f"(() => {{ const n = {item(1)}.querySelector('.table-wrap'); "
            "return {rects: n.getClientRects().length, visible: n.checkVisibility(), "
            "closed: !n.closest('details').open, text: n.closest('.fact').innerText}; })()"
        )
        assert visibility["visible"] is False and visibility["closed"] is True, visibility
        assert await page.evaluate(f"{item(1)}.querySelector('.evidence').open") is False
        assert (
            await page.evaluate(f"{item(1)}.querySelector('.fact').innerText.includes('east')")
            is False
        )
        await page.evaluate(f"{item(1)}.querySelector('.evidence').open = true")
        assert (
            await page.evaluate(f"{item(1)}.querySelector('.table-wrap').getClientRects().length")
            == 1
        )
        assert (
            await page.evaluate(f"{item(1)}.querySelector('.original').innerText.includes('east')")
            is True
        )
        await send(page, original, "query")
        await settled(page, 2, "completed")
        assert (
            await page.evaluate(f"{item(2)}.querySelector('.table-wrap').getClientRects().length")
            == 1
        )
        assert len(statements(env.drv)) == 1


@pytest.mark.parametrize("view", ["full", "brief"])
async def test_html_report_hides_short_metadata_and_keeps_every_original(chrome_binary, view):
    from urllib.parse import quote

    from tests.sdk_core.test_feishu_reports import _delivery

    from xiaowei.feishu_report import build_reports

    delivery = _delivery("html")
    source = delivery.facts[0].model_copy(
        update={
            "view": view,
            "summary": None,
            "columns": (),
            "rows": (),
            "metadata": {
                "result": "RAW_METRICS_CANARY",
                "start_time": "1792440000",
                "warnings": None,
            },
            "result_json": (
                '{"result":"RAW_METRICS_CANARY","start_time":"1792440000","warnings":null}'
            ),
        }
    )
    (report,) = build_reports(delivery.model_copy(update={"facts": (source,)}))
    async with launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate("data:text/html;charset=utf-8," + quote(report.content.decode()))
        visible = await page.evaluate("document.body.innerText")
        assert "需要复核" in visible
        assert "RAW_METRICS_CANARY" not in visible and "start_time" not in visible
        assert await page.evaluate("document.querySelector('details').open") is False
        await page.evaluate("document.querySelector('details').open = true")
        complete = await page.evaluate("document.body.innerText")
        assert "RAW_METRICS_CANARY" in complete and "1792440000" in complete


@pytest.mark.parametrize("kind", ["audit", "action"])
async def test_html_review_notices_and_action_fields_are_visible(chrome_binary, kind):
    from urllib.parse import quote

    from tests.sdk_core.test_answer_review import reviewed_delivery

    from xiaowei.feishu_report import build_reports

    value = reviewed_delivery("html", action=kind != "audit", complete=False)
    (report,) = build_reports(value)
    async with launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate("data:text/html;charset=utf-8," + quote(report.content.decode()))
        visible = await page.evaluate("document.body.innerText")
        if kind == "audit":
            assert "规则或权限过滤 17 条" in visible
            assert "omitted_sqlguard" not in visible
        else:
            assert "ACTION_CANARY" not in visible and "ac_review" not in visible
            assert "ACTION_CANARY" not in report.content.decode()


async def test_web_filtered_candidates_and_historical_warning_are_visible(env, chrome_binary):
    from tests.sdk_core.gate0 import SyntheticStarRocks
    from tests.sdk_core.test_diagnosis import SLOW_SQL, UNAPPROVED_SQL
    from tests.sdk_core.test_runtime import audit_config

    from xiaowei.runtime import ServeConfig

    settings = audit_config(env.port)
    settings["targets"][0]["starrocks"].update(max_result_bytes=200_000, max_value_bytes=4000)
    settings["projection_bytes"] = dict.fromkeys(("model", "session", "web", "feishu"), 400_000)
    config = ServeConfig.model_validate(settings)
    env.drv = SyntheticStarRocks(
        config.targets[0].starrocks,
        audit=(SLOW_SQL,) * 3 + (UNAPPROVED_SQL,) * 17,
        tables={("shop", "sales"): ("region", "total", "note")},
    )
    query = env.scripts.add(
        "查询慢候选",
        tool_call(
            "list_slow_queries",
            cluster=SR.target_id,
            window_minutes=60,
            order_by="query_time",
            database=None,
        ),
        cite("仅供参考"),
    )
    followup = env.scripts.add("刚才返回几条", brief)
    async with env.running(config), launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate(env.origin + "/")
        await send(page, query, "query")
        await settled(page, 0, "completed")
        visible = await page.evaluate(f"{item(0)}.innerText")
        assert "规则或权限过滤 17 条" in visible and "仅列获准候选" in visible
        assert "omitted_sqlguard" not in visible and "结果已截断" not in visible
        await page.evaluate(f"{item(0)}.querySelector('details').open = true")
        assert "omitted_sqlguard" in await page.evaluate(f"{item(0)}.innerText")
        await send(page, followup, "query")
        await settled(page, 1, "completed")
        assert "采集时结果，不代表当前状态" in await page.evaluate(f"{item(1)}.innerText")
    assert not env.drv.sent("query")


@pytest.mark.parametrize(
    "summary",
    [
        "仅列获准候选，为空不代表没有慢查询。",
        "执行计划为估算，未实际运行。",
        "仪表盘配置快照，不是实时指标。",
        "采集时结果，不代表当前状态。",
    ],
)
async def test_html_fixed_caveats_remain_visible_with_original_in_details(chrome_binary, summary):
    from urllib.parse import quote

    from tests.sdk_core.test_answer_review import reviewed_delivery

    from xiaowei.feishu_report import build_reports

    value = reviewed_delivery("html")
    fact = value.facts[0].model_copy(update={"note_summary": summary, "note": "FULL_NOTE_CANARY"})
    (report,) = build_reports(value.model_copy(update={"facts": (fact,)}))
    async with launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate("data:text/html;charset=utf-8," + quote(report.content.decode()))
        visible = await page.evaluate("document.body.innerText")
        assert summary in visible and "FULL_NOTE_CANARY" not in visible
        await page.evaluate("document.querySelector('details').open = true")
        assert "FULL_NOTE_CANARY" in await page.evaluate("document.body.innerText")


@pytest.mark.parametrize("kind", ["action", "multiline"])
async def test_web_view_shows_status_projection_limits_and_note_linebreaks(chrome_binary, kind):
    """Web component: actual status projection and _view response, browser fetch is synthetic."""
    import json
    from pathlib import Path
    from urllib.parse import quote

    from tests.sdk_core.synthetic_tools import Clock
    from tests.sdk_core.test_answer_review import reviewed_delivery

    from xiaowei.actions import action_status_catalog
    from xiaowei.channel import RequestView
    from xiaowei.evidence import _project
    from xiaowei.models import ToolObservation
    from xiaowei.storage import Readiness
    from xiaowei.web import _view

    value = reviewed_delivery(action=kind == "action", complete=False)
    fact = value.facts[0]
    if kind == "action":
        contracts, policies = action_status_catalog(["monitoring"], 10_000)
        contract = next(c for c in contracts if c.tool_id.endswith("/get_action_status"))
        policy = next(p for p in policies if p.policy_id == contract.policy_id)
        spec = policy.projections["web"]
        projected, truncated = _project(
            fact.evidence_id,
            ToolObservation(
                payload={
                    "action_id": "00000000-0000-4000-8000-000000000001",
                    "state": "succeeded",
                    "result": "r" * 9000,
                    "parameters": "OMITTED_STATUS_PARAMETERS_CANARY" * 100,
                },
                captured_at=Clock().now,
                truncated=False,
            ),
            spec.fields,
            spec.max_bytes,
            policy.required,
        )
        assert truncated and "parameters" not in json.loads(projected)["data"]
        fact = fact.model_copy(
            update={
                "tool_id": contract.tool_id,
                "metadata": json.loads(projected)["data"],
                "truncated": truncated,
                "note": policy.fact_note,
                "result_json": json.dumps(json.loads(projected)["data"]),
            }
        )
    else:
        fact = fact.model_copy(update={"truncated": True})
    value = value.model_copy(update={"facts": (fact,), "channel": "web", "report_format": None})
    response = _view("component-result", RequestView("completed", value), Readiness())
    assets = Path(__file__).resolve().parents[2] / "src/xiaowei/static"
    markup = (assets / "index.html").read_text()
    markup = markup.replace('<script src="/assets/app.js" defer></script>', "")
    markup = markup.replace(
        '<link rel="stylesheet" href="/assets/app.css">',
        "<style>" + (assets / "app.css").read_text() + "</style>",
    )
    async with launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate("data:text/html;charset=utf-8," + quote(markup))
        await page.evaluate(
            "window.fetch = async () => ({status: 200, json: async () => ("
            + response.body.decode()
            + ")});"
        )
        await page.evaluate((assets / "app.js").read_text())
        await send(page, "查看已获准结果", "query")
        await settled(page, 0, "completed")
        visible = await page.evaluate(f"{item(0)}.innerText")
        if kind == "action":
            assert "工具结果已截断，不能据此声称完整。" in visible
            assert "OMITTED_STATUS_PARAMETERS_CANARY" not in visible
            assert await page.evaluate(
                f"{item(0)}.querySelector('.action').innerText.includes('succeeded')"
            )
        else:
            assert "工具结果已截断" in visible and "规则或权限过滤 17 条" in visible
            assert await page.evaluate(
                f"{item(0)}.querySelector('.note').innerText.includes(String.fromCharCode(10))"
            )


async def test_v4_task_status_times_are_visible_in_web_body(chrome_binary: str) -> None:
    """同一已验证任务事实的实际 DOM；fetch 为组件替身，不证明 Web 任务授权。"""
    from pathlib import Path
    from urllib.parse import quote

    from tests.sdk_core.test_presentation_v4 import task_status_delivery

    from xiaowei.channel import RequestView
    from xiaowei.storage import Readiness
    from xiaowei.web import _view

    value = task_status_delivery().model_copy(update={"channel": "web"})
    response = _view("task-status", RequestView("completed", value), Readiness())
    assets = Path(__file__).resolve().parents[2] / "src/xiaowei/static"
    markup = (assets / "index.html").read_text()
    markup = markup.replace('<script src="/assets/app.js" defer></script>', "")
    markup = markup.replace(
        '<link rel="stylesheet" href="/assets/app.css">',
        "<style>" + (assets / "app.css").read_text() + "</style>",
    )
    async with launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate("data:text/html;charset=utf-8," + quote(markup))
        await page.evaluate(
            "window.fetch = async () => ({status: 200, json: async () => ("
            + response.body.decode()
            + ")});"
        )
        await page.evaluate((assets / "app.js").read_text())
        await send(page, "这个任务上次何时成功？", "query")
        await settled(page, 0, "completed")
        assert await page.evaluate(f"{item(0)}.querySelectorAll('details').length") == 1
        assert await page.evaluate(f"{item(0)}.querySelector('details').open") is False
        visible = await page.evaluate(f"{item(0)}.innerText")
        assert "2026-10-09T10:05:00+00:00" in visible
        assert "2026-10-09T10:06:00+00:00" in visible
        assert "nr_internal" not in visible
