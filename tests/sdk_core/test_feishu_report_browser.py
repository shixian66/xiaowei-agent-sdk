"""固定 HTML 报告在本机 Chrome 中只显示字面内容，不请求外部资源。"""

import base64
import json
from pathlib import Path

import pytest
from markdown_it import MarkdownIt
from tests.sdk_core.browser import launch
from tests.sdk_core.test_feishu_reports import _delivery

from xiaowei.feishu_report import build_report
from xiaowei.models import DeliveryText

pytestmark = pytest.mark.browser


async def test_normalized_md_is_literal_and_network_free_in_commonmark_reader(
    chrome_binary: str, tmp_path: Path
) -> None:
    source = (
        "# 巡检\n\nARRAY<INT>、MAP<STRING,INT>、List<String>\n\n"
        "| 说明 |\n| --- |\n| 第一行<br>第二行 |\n\n"
        "[文档](https://invalid.example/doc)\n\n"
        "![图片](https://invalid.example/image)\n\n"
        "<script>window.reportAttack=true</script>"
    )
    delivery = _delivery("md")
    delivery = delivery.model_copy(
        update={"report_native": True, "blocks": (DeliveryText(text=source), *delivery.blocks[1:])}
    )
    content = build_report(delivery).content.decode()
    rendered = MarkdownIt("commonmark", {"html": True}).enable("table").render(content)
    path = tmp_path / "md-reader.html"
    path.write_text("<!doctype html><meta charset=utf-8><body>" + rendered + "</body>")
    async with launch(chrome_binary) as chrome:
        page = await chrome.page()
        # Catch attempted I/O without permitting a regression to reach external servers.
        await page.send("Network.setBlockedURLs", {"urls": ["https://*", "http://*"]})
        mark = len(chrome.events)
        await page.navigate(path.as_uri())
        assert await page.evaluate("window.reportAttack === undefined") is True
        assert await page.evaluate("document.querySelectorAll('script,img,a,iframe').length") == 0
        visible = await page.evaluate("document.body.innerText")
        for value in ("ARRAY<INT>", "MAP<STRING,INT>", "List<String>", "第一行<br>第二行"):
            assert value in visible
        assert "来源" in visible and "部分不支持的格式已按原文展示" in visible
        assert not [
            event
            for event in page.events("Network.requestWillBeSent", mark)
            if event["request"]["url"] != path.as_uri()
        ]


@pytest.mark.parametrize("width", [1200, 390])
async def test_native_html_is_readable_isolated_and_network_free(
    chrome_binary: str, tmp_path: Path, width: int
) -> None:
    source = (
        '<section id="report-section-2" style="background-color:#14243a;color:#edf6fc;'
        "display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;"
        "position:fixed;margin:-999999px;width:999999999px;"
        'background-image:url(https://invalid.example/resource)">'
        "<h1>集群巡检 · 科技风</h1><p>在获准范围内分析，不代表全量健康。</p>"
        '<p style="font-size:0;color:transparent">必须阅读的报告结论</p>'
        "<pre><code>SELECT a&lt;b &amp; 1; &lt;table&gt;</code></pre>"
        '<img src="file:///private/forbidden"><script>window.reportAttack=true</script>'
        '<iframe src="https://invalid.example/frame">hidden</iframe></section>'
    )
    delivery = _delivery("html")
    delivery = delivery.model_copy(
        update={
            "report_native": True,
            "blocks": (DeliveryText(text=source), *delivery.blocks[1:]),
        }
    )
    path = tmp_path / "native-report.html"
    path.write_bytes(build_report(delivery).content)
    async with launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": 900,
                "deviceScaleFactor": 1,
                "mobile": width == 390,
            },
        )
        mark = len(chrome.events)
        await page.navigate(path.as_uri())
        assert await page.evaluate("window.reportAttack === undefined") is True
        assert (
            await page.evaluate("document.querySelectorAll('script,img,iframe,object,form').length")
            == 0
        )
        assert (
            await page.evaluate(
                "document.querySelectorAll('.model-prose [id],.model-prose [class]').length"
            )
            == 0
        )
        assert (
            await page.evaluate(
                "getComputedStyle(document.querySelector('.model-prose section')).backgroundColor"
            )
            == "rgb(20, 36, 58)"
        )
        assert (
            await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth") is True
        )
        assert (
            await page.evaluate("document.querySelector('.model-prose code').textContent")
            == "SELECT a<b & 1; <table>"
        )
        assert (
            await page.evaluate("document.querySelector('.model-label').offsetHeight > 0") is True
        )
        assert (
            await page.evaluate("""(() => {
          const p = Array.from(document.querySelectorAll('.model-prose p'))
            .find(e => e.textContent === '必须阅读的报告结论');
          const style = getComputedStyle(p);
          return parseFloat(style.fontSize) >= 12 && style.color !== 'rgba(0, 0, 0, 0)';
        })()""")
            is True
        )
        assert (
            await page.evaluate(
                "Array.from(document.querySelectorAll('.card pre code'))"
                ".some(e => e.textContent.includes(\"SELECT '<script>' AS value\"))"
            )
            is True
        )
        assert (
            await page.evaluate("""(() => {
          const link = document.querySelector('.report-nav a');
          link.click();
          const fact = document.getElementById(link.hash.slice(1));
          fact.scrollIntoView();
          const r = fact.getBoundingClientRect();
          return fact.contains(document.elementFromPoint(r.left + 20, Math.max(1, r.top + 20)));
        })()""")
            is True
        )
        await page.send("Emulation.setEmulatedMedia", {"media": "print"})
        assert (
            await page.evaluate(
                "document.querySelector('.model-prose').scrollHeight <= "
                "document.querySelector('.model-prose').clientHeight + 1"
            )
            is True
        )
        assert (
            await page.evaluate(
                "document.body.innerText.includes('来源') && "
                "document.body.innerText.includes('未经系统核实')"
            )
            is True
        )
        outgoing = [
            event["request"]["url"]
            for event in page.events("Network.requestWillBeSent", mark)
            if event["request"]["url"] != path.as_uri()
        ]
        assert outgoing == []
        await page.send("Emulation.setEmulatedMedia", {"media": "screen"})
        await page.evaluate("window.scrollTo(0,0)")
        shot = await page.send(
            "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}
        )
        screenshot = tmp_path / f"native-report-{width}.png"
        screenshot.write_bytes(base64.b64decode(shot["data"]))
        print(f"report_screenshot={screenshot}")


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
