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
