"""固定 HTML 报告在本机 Chrome 中只显示字面内容，不请求外部资源。"""

import json
from pathlib import Path

import pytest
from tests.sdk_core.browser import launch
from tests.sdk_core.test_feishu_reports import _delivery

from xiaowei.feishu_report import build_report

pytestmark = pytest.mark.browser


async def test_static_html_report_has_no_active_tags_or_external_request(
    chrome_binary: str, tmp_path: Path
) -> None:
    report = build_report(_delivery("html"))
    path = tmp_path / report.filename
    path.write_bytes(report.content)
    async with launch(chrome_binary) as chrome:
        page = await chrome.page()
        mark = len(chrome.events)
        await page.navigate(path.as_uri())
        assert await page.evaluate("document.body.innerText.includes('SELECT')") is True
        assert await page.evaluate("document.body.innerText.includes('<at id=all>')") is True
        active = await page.evaluate("document.querySelectorAll('script,img,link,iframe,a').length")
        assert active == 0
        outgoing = [
            event["request"]["url"]
            for event in page.events("Network.requestWillBeSent", mark)
            if event["request"]["url"].startswith(("http:", "https:"))
        ]
        assert outgoing == []


async def test_generic_html_report_is_readable_on_phone_and_copies_ddl_exactly(
    chrome_binary: str, tmp_path: Path
) -> None:
    delivery = _delivery("html")
    ddl = "CREATE TABLE `orders` (\n  `note` STRING\n) ENGINE=OLAP;\n-- <img src=x>"
    payload = {
        "sql": "SHOW CREATE TABLE orders",
        "columns": ["ddl", "nullable", "huge"],
        "rows": [{"ddl": ddl, "nullable": None, "huge": 2**60}],
        "scope": "生产 · 昨天",
    }
    fact = delivery.facts[0].model_copy(
        update={"result_json": json.dumps(payload, ensure_ascii=False)}
    )
    report = build_report(delivery.model_copy(update={"facts": (fact,)}))
    path = tmp_path / report.filename
    path.write_bytes(report.content)
    async with launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
        )
        mark = len(chrome.events)
        await page.navigate(path.as_uri())
        assert await page.evaluate("document.querySelector('table') !== null") is True
        assert (
            await page.evaluate("getComputedStyle(document.querySelector('.card')).borderRadius")
            == "12px"
        )
        assert (
            await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth") is True
        )
        copied = await page.evaluate(
            "Array.from(document.querySelectorAll('pre.sql-code code')).map(el => el.textContent)"
        )
        assert ddl in copied
        assert (
            await page.evaluate("document.body.innerText.includes('1152921504606846976')") is True
        )
        assert await page.evaluate("document.querySelectorAll('script,img,a,iframe').length") == 0
        outgoing = [
            event["request"]["url"]
            for event in page.events("Network.requestWillBeSent", mark)
            if event["request"]["url"].startswith(("http:", "https:"))
        ]
        assert outgoing == []
