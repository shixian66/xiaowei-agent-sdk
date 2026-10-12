"""S3 visible success warning through formal serve in real Chrome on a free port."""

import pytest
from tests.sdk_core.browser import launch
from tests.sdk_core.test_runtime import Env, statements
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_session_rejection_entrypoints import limits, query
from tests.sdk_core.test_web_browser import item, send, settled

pytestmark = [pytest.mark.loopback, pytest.mark.browser]


async def test_chrome_success_warning_default_visible_refresh_and_new_session(
    env: Env, chrome_binary: str
) -> None:
    assert env.port != 8501
    async with (
        env.running(env.config(session_limits=limits(turns=2))),
        launch(chrome_binary) as chrome,
    ):
        page = await chrome.page()
        await page.navigate(env.origin + "/")
        await send(page, query(env, "s3-browser-first"), "query")
        await settled(page, 0, "completed")
        assert await page.evaluate(f"{item(0)}.querySelector('.session-notice') === null")
        await send(page, query(env, "s3-browser-last"), "query")
        await settled(page, 1, "completed")
        selector = f"{item(1)}.querySelector('.session-notice')"
        assert await page.evaluate(f"{selector} !== null")
        visible = await page.evaluate(f"{selector}.innerText")
        assert "2/2" in visible and "下轮需要新建" in visible and "本轮结束时" in visible
        assert await page.evaluate(f"{selector}.checkVisibility()")
        await page.navigate(env.origin + "/")
        await settled(page, 1, "completed")
        assert await page.evaluate(f"{selector}.innerText") == visible
        await page.evaluate("document.getElementById('new-session').click()")
        await page.wait_for("document.querySelectorAll('#turns > li').length === 0")
        await send(page, query(env, "s3-browser-new"), "query")
        await settled(page, 0, "completed")
        assert await page.evaluate(f"{item(0)}.querySelector('.session-notice') === null")
        assert len(statements(env.drv)) == 3
