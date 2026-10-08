"""P1-B Task 6：Web 组件在真实 Uvicorn 与无头 Chrome 中的 smoke。

服务端是 ``create_web_app`` + 进程内 Uvicorn（``127.0.0.1:8501``），后面是与 ``test_web.py`` 相同的
测试装配（真 Runner + HTTP mock 脚本模型、受治理合成工具、隔离的真实 PostgreSQL）。这不是正式
``xiaowei serve`` 入口：正式入口的浏览器验收属于 Task 8。

运行（需要测试 PostgreSQL 与本机 Chrome）::

    SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \\
    SDK_TEST_CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
      uv run --locked --extra dev python -m pytest tests/sdk_core/test_web_browser.py -m browser -q
"""

import asyncio
import json
import logging
import re
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import pytest
import uvicorn
from tests.sdk_core import synthetic_tools
from tests.sdk_core.browser import Page, launch
from tests.sdk_core.synthetic_tools import QUERY_TOOL
from tests.sdk_core.test_app import MODEL_SECRET, after, cite, tool_call, upstream_error
from tests.sdk_core.test_channel_service import Env
from tests.sdk_core.test_channel_service import env as env  # pytest fixture
from tests.sdk_core.test_web import HOSTILE, ORIGIN, config

from xiaowei.web import COOKIE, create_web_app

pytestmark = [pytest.mark.loopback, pytest.mark.browser]

HOST, PORT = "127.0.0.1", 8501
QUERY_NAME = QUERY_TOOL.split("/", 1)[1]
CANARY = "xw-nojs-canary-7f3a"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@contextmanager
def access_log() -> Iterator[list[str]]:
    """收集 Uvicorn access log 的每一行；必须在服务启动前挂上处理器。"""
    logger = logging.getLogger("uvicorn.access")
    capture, level = _Capture(), logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    try:
        yield capture.lines
    finally:
        logger.removeHandler(capture)
        logger.setLevel(level)


@asynccontextmanager
async def serving(app: Any, *, port: int = PORT) -> AsyncIterator[None]:
    # 以“能否连上”判断是否有其他进程在监听；前一个用例留下的 TIME_WAIT 不影响 Uvicorn 绑定。
    with socket.socket() as probe:
        if probe.connect_ex((HOST, port)) == 0:
            pytest.fail(f"{HOST}:{port} 已被占用：不能干扰既有服务")
    server = uvicorn.Server(
        uvicorn.Config(
            app, host=HOST, port=port, http="h11", lifespan="off", log_config=None, access_log=True
        )
    )
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.02)
    try:
        yield
    finally:
        server.should_exit = True
        await task


def js(value: str) -> str:
    return json.dumps(value)


async def send(page: Page, message: str, mode: str | None = None) -> None:
    """像用户一样填写并点击发送；``mode`` 为 None 时保留页面当前选择。"""
    choose = f"document.getElementById('mode').value = {js(mode)};" if mode else ""
    await page.evaluate(
        f"{choose} document.getElementById('message').value = {js(message)};"
        " document.getElementById('send').click();"
    )


def item(index: int) -> str:
    return f"document.querySelectorAll('#turns > li')[{index}]"


async def table_rows(page: Page, index: int) -> list[int]:
    rows: list[int] = await page.evaluate(
        f"[...{item(index)}.querySelectorAll('table')].map(t => t.tBodies[0].rows.length)"
    )
    return rows


async def settled(page: Page, index: int, state: str) -> str:
    await page.wait_for(f"{item(index)}?.classList.contains({js(state)})")
    text: str = await page.evaluate(f"{item(index)}.textContent")
    return text


async def test_web_component_in_real_uvicorn_and_chrome(env: Env, chrome_binary: str) -> None:
    app = create_web_app(env.service, config())
    with access_log() as log:
        async with serving(app), launch(chrome_binary) as chrome:
            page = await chrome.page()
            await page.navigate(f"{ORIGIN}/")
            await check_turns(env, page, log)
            await check_without_script(env, await chrome.page(), log)
    assert not any("javascriptDialogOpening" in e["method"] for e in chrome.events)


async def test_send_works_without_secure_context_apis(env: Env, chrome_binary: str) -> None:
    """用 http://内网IP 打开时浏览器不提供 ``crypto.randomUUID``：发送仍须生成编号并完成。"""
    app = create_web_app(env.service, config())
    message = env.scripts.add("内网发送", tool_call("order_total", region="east"), cite())
    async with serving(app), launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "delete Crypto.prototype.randomUUID;"},
        )
        await page.navigate(f"{ORIGIN}/")
        assert await page.evaluate("typeof crypto.randomUUID") == "undefined"
        await send(page, message)
        await settled(page, 0, "completed")
        request_id = await page.evaluate(f"{item(0)}.dataset.requestId")
        assert await page.evaluate("document.getElementById('message').value") == ""
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id)
    assert env.model_calls(message) == 2


async def test_compact_facts_and_multiline_analysis(env: Env, chrome_binary: str) -> None:
    app = create_web_app(env.service, config())
    inference = "第一行\n第二行 <script>alert(1)</script>"
    message = env.scripts.add(
        "只看查询结果", tool_call("order_total", region="east"), cite(inference)
    )
    async with serving(app, port=18501), launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate("http://127.0.0.1:18501/")
        await send(page, message)
        await settled(page, 0, "completed")
        rendered = await page.evaluate(f"{item(0)}.innerText")
        assert "已回复" in rendered and "已完成" not in rendered
        assert "第一行" in rendered and rendered.count("第二行") == 1
        assert '"rows":' not in rendered
        assert await table_rows(page, 0) == [2]
        assert (
            await page.evaluate(f"{item(0)}.querySelector('.analysis .content').textContent")
            == inference
        )
        assert await page.evaluate(f"{item(0)}.querySelector('details').open") is False
        await page.evaluate(f"{item(0)}.querySelector('summary').click()")
        assert "来源 local/order_total" in await page.evaluate(f"{item(0)}.innerText")
        assert await page.evaluate("document.scripts.length") == 1


@pytest.mark.parametrize("nested_rows", [False, True])
async def test_compact_preserves_exact_values(
    env: Env, chrome_binary: str, monkeypatch: pytest.MonkeyPatch, nested_rows: bool
) -> None:
    big = 9007199254740993
    values = {"id": big, "negative": -big, "missing": None, "empty": "", "active": True}
    payload = {
        "region": {"ids": [big], "note": "<script>alert(1)</script>"},
        "total": big,
        "rows": [{"nested": values}] if nested_rows else [values],
        "private_note": "forbidden-value",
    }
    monkeypatch.setattr(synthetic_tools, "payload", lambda *args: payload)
    app = create_web_app(env.service, config())
    message = env.scripts.add("订单号与空值", tool_call("order_total", region="east"), cite(""))
    async with serving(app, port=18501), launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate("http://127.0.0.1:18501/")
        await send(page, message)
        await settled(page, 0, "completed")
        rendered = await page.evaluate(f"{item(0)}.innerText")
        assert f"total: {big}" in rendered
        assert f'"ids": [{big}]' in rendered
        assert "forbidden-value" not in rendered
        assert await page.evaluate("document.scripts.length") == 1
        if nested_rows:
            assert f'"id": {big}' in rendered and '"missing": null' in rendered
            assert '"empty": ""' in rendered
        else:
            cells = await page.evaluate(
                f"[...{item(0)}.querySelectorAll('td')].map(cell => cell.textContent)"
            )
            assert cells == [str(big), str(-big), "NULL", "（空字符串）", "true"]
        original = await page.evaluate(f"{item(0)}.querySelector('pre.original').textContent")
        assert json.loads(original) == {k: v for k, v in payload.items() if k != "private_note"}


async def test_compact_preserves_multiline_ddl_original(
    env: Env, chrome_binary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.p1b.test_starrocks_ddl import DDL

    payload = {"region": "shop.sales", "total": 1, "rows": [{"ddl": DDL}]}
    monkeypatch.setattr(synthetic_tools, "payload", lambda *args: payload)
    app = create_web_app(env.service, config())
    message = env.scripts.add("建表原文换行", tool_call("order_total", region="east"), cite(""))
    async with serving(app, port=18501), launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate("http://127.0.0.1:18501/")
        await send(page, message)
        await settled(page, 0, "completed")
        assert await page.evaluate(f"{item(0)}.querySelector('td').textContent") == DDL
        assert (
            await page.evaluate(f"getComputedStyle({item(0)}.querySelector('td')).whiteSpace")
            == "pre-wrap"
        )
        original = await page.evaluate(f"{item(0)}.querySelector('pre.original').textContent")
        assert json.loads(original)["rows"] == [{"ddl": DDL}]


async def check_turns(env: Env, page: Page, log: list[str]) -> None:
    # cookie 只经 HttpOnly 保存：脚本读不到；默认用途由单 Agent 判断（P2.5 Task 7）。
    assert await page.evaluate("document.cookie") == ""
    assert await page.evaluate("document.getElementById('mode').value") == "query"

    # 1. 显式诊断：查询工具不展示。
    diagnose = env.scripts.add("浏览器诊断", tool_call("order_total", region="east"), cite())
    await send(page, diagnose, "diagnose")
    await settled(page, 0, "completed")
    assert all(QUERY_NAME not in seen for seen in env.scripts.tools_seen(diagnose))

    # 2. 查询：有限表格、截断说明，以及恶意 HTML 与双向控制符只作为文字。
    env.adapter.memo, env.adapter.truncated = HOSTILE, True
    query = env.scripts.add(
        "浏览器查询", tool_call("order_total", region="east"), cite("<b onmouseover=x>分析</b>")
    )
    await send(page, query, "query")
    text = await settled(page, 1, "completed")
    assert all(QUERY_NAME in seen for seen in env.scripts.tools_seen(query))
    assert await page.evaluate("document.getElementById('mode').value") == "query"  # 发送后重置
    assert await table_rows(page, 1) == [2, 2]  # 本轮与回放的上一轮证据各一张有限表格
    assert "结果已截断" in text and "<script>alert(1)</script>" in text and "<b onmouseover" in text
    assert "\\u202e" in text and "‮" not in text
    dom = await page.evaluate(
        "({title: document.title, scripts: document.scripts.length,"
        " img: document.querySelectorAll('img').length, b: document.querySelectorAll('b').length})"
    )
    assert dom == {"title": "小维", "scripts": 1, "img": 0, "b": 0}
    env.adapter.memo, env.adapter.truncated = "", False

    # 3. 失败：固定回执与请求编号，不含上游错误体。
    failed = env.scripts.add("浏览器失败", upstream_error)
    await send(page, failed)
    text = await settled(page, 2, "failed")
    request_id = await page.evaluate(f"{item(2)}.dataset.requestId")
    assert "模型未能完成本轮" in text and f"编号 {request_id}" in text and MODEL_SECRET not in text

    # 4. 模型调用中刷新页面：进行中的 fetch 被中断，该轮仍在服务端完成，刷新后的 GET 恢复全部结果。
    gate, entered = asyncio.Event(), asyncio.Event()
    slow = env.scripts.add(
        "浏览器慢轮", after(gate, tool_call("order_total", region="east"), entered=entered), cite()
    )
    await send(page, slow)
    await asyncio.wait_for(entered.wait(), 10)
    await page.reload()
    gate.set()
    await settled(page, 3, "completed")
    states = await page.evaluate(
        "[...document.querySelectorAll('#turns > li')].map(li => li.className)"
    )
    assert states == ["turn completed", "turn completed", "turn failed", "turn completed"]
    assert await table_rows(page, 1) == [2, 2]
    calls = {m: env.model_calls(m) for m in (diagnose, query, failed, slow)}
    assert calls == {diagnose: 2, query: 2, failed: 1, slow: 2}
    assert len(env.adapter.calls) == 3

    # 5. 新建会话：列表清空，之后的请求落在新会话。
    first_id = await page.evaluate(f"{item(0)}.dataset.requestId")
    await page.evaluate("document.getElementById('new-session').click()")
    await page.wait_for("document.getElementById('notice').textContent.includes('已新建会话')")
    assert await page.evaluate("document.querySelectorAll('#turns > li').length") == 0
    fresh = env.scripts.add("新会话", tool_call("order_total", region="east"), cite())
    await send(page, fresh)
    await settled(page, 0, "completed")
    fresh_id = await page.evaluate(f"{item(0)}.dataset.requestId")
    cookies = (await page.send("Network.getCookies", {"urls": [ORIGIN]}))["cookies"]
    (cookie,) = [c for c in cookies if c["name"] == COOKIE]
    assert cookie["httpOnly"] and cookie["sameSite"] == "Strict" and not cookie["secure"]
    old = await env.store.get("web", "alice", cookie["value"], first_id)
    new = await env.store.get("web", "alice", cookie["value"], fresh_id)
    assert old.session_id != new.session_id

    # 消息正文从不进入 access log（只有方法、路径与状态）。
    assert any('"GET / HTTP/1.1" 200' in line for line in log)
    for message in (diagnose, query, failed, slow, fresh):
        assert all(message not in line for line in log)


async def check_without_script(env: Env, page: Page, log: list[str]) -> None:
    """脚本未加载时提交表单：消息不进入 URL、浏览器历史或 access log，也不产生任何请求。"""
    requests, adapter_calls = await env.requests(), len(env.adapter.calls)
    await page.send("Network.setBlockedURLs", {"urls": ["*/assets/app.js"]})
    await page.navigate(f"{ORIGIN}/")
    failures = page.events("Network.loadingFailed")
    assert any(f.get("blockedReason") for f in failures)  # app.js 确实没有加载
    mark = len(log)
    await page.evaluate(
        f"document.getElementById('message').value = {js(CANARY)};"
        " document.getElementById('send').click();"
    )
    await asyncio.sleep(1.0)
    assert await page.evaluate("location.href") == f"{ORIGIN}/"
    history = await page.send("Page.getNavigationHistory")
    assert all(CANARY not in entry["url"] for entry in history["entries"])
    assert all(CANARY not in line for line in log)
    assert log[mark:] == []  # 提交被 CSP form-action 'none' 阻止，没有发出请求
    assert await env.requests() == requests and len(env.adapter.calls) == adapter_calls
    assert env.model_calls(CANARY) == 0
