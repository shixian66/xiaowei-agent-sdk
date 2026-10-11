"""S2 fixed session receipt visible in real Chrome, formal serve on a free port."""

import pytest
from tests.sdk_core.browser import launch
from tests.sdk_core.test_runtime import Env, statements
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_session_rejection_entrypoints import limits, query
from tests.sdk_core.test_web_browser import item, send, settled

pytestmark = [pytest.mark.loopback, pytest.mark.browser]


async def test_chrome_session_limit_receipt_visible_after_refresh_and_new_session(
    env: Env,
    chrome_binary: str,
) -> None:
    assert env.port != 8501
    first = query(env, "s2-browser-first")
    fresh = query(env, "s2-browser-fresh")
    async with env.running(env.config(session_limits=limits())), launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate(env.origin + "/")
        await send(page, first, "query")
        await settled(page, 0, "completed")
        await send(page, "s2-browser-no-model", "query")
        await settled(page, 1, "failed")
        visible = await page.evaluate(f"{item(1)}.innerText")
        assert "交互轮数上限" in visible and "新建会话" in visible
        assert "本轮结束时" in visible and "删除保存记录" in visible
        assert await page.evaluate(f"{item(1)}.querySelector('.session-notice').checkVisibility()")
        await page.navigate(env.origin + "/")
        await settled(page, 1, "failed")
        assert await page.evaluate(f"{item(1)}.innerText") == visible
        assert len(statements(env.drv)) == 1
        assert env.scripts.calls.get("s2-browser-no-model", []) == []
        await page.evaluate("document.getElementById('new-session').click()")
        # 新建会话清空可见列表，不复制旧上下文或重新执行失败的轮次。
        await page.wait_for("document.querySelectorAll('#turns > li').length === 0")
        await send(page, fresh, "query")
        await settled(page, 0, "completed")
        assert len(statements(env.drv)) == 2
