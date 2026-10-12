"""固定 HTML 报告在本机 Chrome 中只显示字面内容，不请求外部资源。"""

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
