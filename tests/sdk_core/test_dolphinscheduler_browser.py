"""DS D1/E10/F1 in real local Chrome; model and DS remain fixtures, not company UAT."""

import pytest
from tests.sdk_core.browser import launch
from tests.sdk_core.test_app import tool_call
from tests.sdk_core.test_dolphinscheduler_runtime import (
    config_values,
    ds_answer,
    ds_api,
    list_failed,
    source,
)
from tests.sdk_core.test_runtime import env as env
from tests.sdk_core.test_web_browser import item, send, settled

from xiaowei.runtime import ServeConfig

pytestmark = [pytest.mark.loopback, pytest.mark.browser]


@pytest.mark.parametrize("auth_failure", [False, True])
async def test_formal_ds_success_or_auth_gap_visible_without_expanding_basis(
    env, chrome_binary, monkeypatch, auth_failure
):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api(fail=auth_failure) as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url)]))
        message = env.scripts.add(
            "查 DS B 昨晚失败任务",
            tool_call("ds_select_clusters", clusters=["ds-b"]),
            list_failed(),
            ds_answer,
        )
        async with env.running(config), launch(chrome_binary) as chrome:
            page = await chrome.page()
            await page.navigate(env.origin + "/")
            await send(page, message, "query")
            await settled(page, 0, "completed")
            visible = await page.evaluate(f"{item(0)}.innerText")
            assert "生产 B" in visible
            assert "synthetic-token" not in visible
            assert ("认证" in visible) is auth_failure
            assert ("orders_failed" in visible) is (not auth_failure)
            assert not await page.evaluate(f"{item(0)}.querySelector('details[open]') !== null")
            assert len(requests) == 1
            await page.reload()
            await settled(page, 0, "completed")
            assert "生产 B" in await page.evaluate(f"{item(0)}.innerText")
            assert len(requests) == 1
