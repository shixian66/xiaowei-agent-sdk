"""P1-B Task 8：正式入口在真实 Chrome 中的验收（``127.0.0.1:8501``，正式默认端口）。

- ``test_formal_command_in_chrome``：子进程运行正式命令 ``xiaowei serve``（配置文件 + 环境变量
  引用）。模型端点是本机已关闭的 HTTPS 端口，验证页面、脚本、cookie、模型失败固定回执、刷新恢复、
  新建会话、脚本未加载时的提交与 Host 拒绝。不能证明成功轮次（需要真实模型，Task 9）。
- ``test_formal_assembly_turns_in_chrome``：同一正式装配 ``runtime.serve``（实例锁、启动恢复、真实
  Uvicorn、StarRocks 工具与治理）在进程内运行，只把模型换成 HTTP mock 脚本、StarRocks 换成驱动替身，
  验证诊断、查询表格与模型调用中刷新。CLI 参数、环境变量强制与信号由前一个用例覆盖。

Task 6 的组件 smoke（``create_web_app`` + 测试装配）不能替代这两项。运行::

    SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \\
    SDK_TEST_CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
      uv run --locked --extra dev python -m pytest \\
        tests/sdk_core/test_serve_browser.py -m browser -q
"""

import asyncio
import socket
from pathlib import Path

import pytest
from sqlalchemy.engine import URL
from tests.p1b.test_starrocks_adapter import driver
from tests.sdk_core.browser import Page, launch
from tests.sdk_core.test_app import after, cite, tool_call
from tests.sdk_core.test_cli import CANARY, Deployment, secrets_absent
from tests.sdk_core.test_runtime import PLAN, Env
from tests.sdk_core.test_runtime import env as env  # pytest fixture
from tests.sdk_core.test_web_browser import item, js, send, settled, table_rows

from xiaowei.starrocks_tools import PLAN_NOTE
from xiaowei.web import COOKIE

pytestmark = [pytest.mark.loopback, pytest.mark.browser]

PORT = 8501
ORIGIN = f"http://127.0.0.1:{PORT}"


def require_port() -> None:
    """没有其他进程在监听 8501（以能否连上判断，TIME_WAIT 不影响服务绑定）。"""
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            pytest.fail(f"127.0.0.1:{PORT} 已被占用：验收使用正式默认端口")


async def check_cookie(page: Page) -> str:
    assert await page.evaluate("document.cookie") == ""
    cookies = (await page.send("Network.getCookies", {"urls": [ORIGIN]}))["cookies"]
    (cookie,) = [c for c in cookies if c["name"] == COOKIE]
    assert cookie["httpOnly"] and cookie["sameSite"] == "Strict" and not cookie["secure"]
    value: str = cookie["value"]
    return value


async def test_formal_command_in_chrome(
    postgres_url: URL, tmp_path: Path, chrome_binary: str
) -> None:
    require_port()
    deploy = Deployment(postgres_url, tmp_path, port=PORT)
    assert deploy.run("console", "storage", "init").returncode == 0
    with deploy.serving("console") as process:
        async with launch(chrome_binary) as chrome:
            page = await chrome.page()
            await page.navigate(f"{ORIGIN}/")
            await check_cookie(page)
            assert await page.evaluate("document.getElementById('mode').value") == "diagnose"

            # 正式装配的失败路径：固定回执与请求编号，刷新后经 GET 恢复。
            await send(page, f"正式入口 {CANARY}")
            text = await settled(page, 0, "failed")
            request_id = await page.evaluate(f"{item(0)}.dataset.requestId")
            assert "模型未能完成本轮" in text and f"编号 {request_id}" in text
            await page.reload()
            await settled(page, 0, "failed")

            await page.evaluate("document.getElementById('new-session').click()")
            await page.wait_for(
                "document.getElementById('notice').textContent.includes('已新建会话')"
            )
            assert await page.evaluate("document.querySelectorAll('#turns > li').length") == 0

            # 脚本未加载：提交被 CSP 阻止，消息不进入 URL 或历史。
            blank = await chrome.page()
            await blank.send("Network.setBlockedURLs", {"urls": ["*/assets/app.js"]})
            await blank.navigate(f"{ORIGIN}/")
            await blank.evaluate(
                f"document.getElementById('message').value = {js(CANARY)};"
                " document.getElementById('send').click();"
            )
            await asyncio.sleep(1.0)
            assert await blank.evaluate("location.href") == f"{ORIGIN}/"
            history = await blank.send("Page.getNavigationHistory")
            assert all(CANARY not in entry["url"] for entry in history["entries"])

            # 其他 Host（localhost 别名不放行）：在路由前拒绝，不换发 cookie。
            alias = await chrome.page()
            await alias.navigate(f"http://localhost:{PORT}/")
            body = await alias.evaluate("document.body.textContent")
            assert "Host 不被允许" in body
            cookies = await alias.send("Network.getCookies", {"urls": [f"http://localhost:{PORT}"]})
            assert cookies["cookies"] == []
        process.terminate()
        out, err = process.communicate(timeout=60)
    assert process.returncode == 0
    secrets_absent(out + err)


async def test_formal_assembly_turns_in_chrome(env: Env, chrome_binary: str) -> None:
    require_port()
    env.port = PORT
    config = env.config(
        listen_port=PORT,
        web={**env.config().web.model_dump(mode="json"), "allowed_origins": [ORIGIN]},
    )
    async with env.running(config) as served, launch(chrome_binary) as chrome:
        page = await chrome.page()
        await page.navigate(f"{ORIGIN}/")
        await check_cookie(page)

        diagnose = env.scripts.add("浏览器诊断表结构", tool_call("list_tables"), cite())
        await send(page, diagnose)
        await settled(page, 0, "completed")
        assert all("run_readonly_query" not in seen for seen in env.scripts.tools_seen(diagnose))

        query = env.scripts.add(
            "浏览器查询销售额",
            tool_call("run_readonly_query", sql="SELECT region, total FROM sales"),
            cite(),
        )
        await send(page, query, "query")
        await settled(page, 1, "completed")
        assert any("run_readonly_query" in seen for seen in env.scripts.tools_seen(query))
        assert 2 in await table_rows(page, 1)

        env.drv.make = driver(PLAN).make
        plan = env.scripts.add(
            "浏览器查看执行计划",
            tool_call("explain_query", sql="SELECT region, total FROM sales"),
            cite(),
        )
        await send(page, plan, "diagnose")
        await settled(page, 2, "completed")
        # 本轮还引用了回放中的历史证据；只有计划事实带说明，说明与计划表格在同一个事实框中。
        noted = await page.evaluate(
            f"[...{item(2)}.querySelectorAll('.fact')].filter(f => f.querySelector('.note'))"
            ".map(f => [f.querySelector('.note').textContent,"
            " [...f.querySelectorAll('th')].map(th => th.textContent)])"
        )
        assert noted == [[f"说明：{PLAN_NOTE}", ["plan"]]]

        gate, entered = asyncio.Event(), asyncio.Event()
        slow = env.scripts.add(
            "浏览器慢轮", after(gate, tool_call("list_tables"), entered=entered), cite()
        )
        await send(page, slow)
        await asyncio.wait_for(entered.wait(), 20)
        await page.reload()
        gate.set()
        await settled(page, 3, "completed")
        states = await page.evaluate(
            "[...document.querySelectorAll('#turns > li')].map(li => li.className)"
        )
        assert states == ["turn completed"] * 4
        assert await served.finish() == 0
