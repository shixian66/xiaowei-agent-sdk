"""浏览器 smoke 用的最小 Chrome DevTools 协议客户端。

经 ``--remote-debugging-pipe`` 连接：Chrome 从 fd 3 读、向 fd 4 写以 ``\\0`` 结尾的 JSON 消息，
不开放调试端口，也不需要额外依赖。只实现 smoke 用到的命令、事件与页面求值。
"""

import asyncio
import contextlib
import json
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

_TIMEOUT = 15.0


class CDPError(RuntimeError):
    pass


@dataclass
class Chrome:
    process: asyncio.subprocess.Process
    reader: asyncio.StreamReader
    write_fd: int
    events: list[dict[str, Any]] = field(default_factory=list)
    _pending: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    _next_id: int = 0

    async def _dispatch(self) -> None:
        while True:
            try:
                raw = await self.reader.readuntil(b"\0")
            except asyncio.IncompleteReadError:
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(CDPError("Chrome 已退出"))
                return
            message = json.loads(raw[:-1])
            if "id" in message:
                self._pending.pop(message["id"]).set_result(message)
            else:
                self.events.append(message)

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session: str | None = None
    ) -> dict[str, Any]:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session is not None:
            message["sessionId"] = session
        future = asyncio.get_running_loop().create_future()
        self._pending[self._next_id] = future
        os.write(self.write_fd, json.dumps(message).encode() + b"\0")
        reply = await asyncio.wait_for(future, _TIMEOUT)
        if "error" in reply:
            raise CDPError(f"{method}: {reply['error']}")
        result: dict[str, Any] = reply["result"]
        return result

    async def page(self) -> "Page":
        target = await self.send("Target.createTarget", {"url": "about:blank"})
        attached = await self.send(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )
        page = Page(self, attached["sessionId"])
        for domain in ("Page", "Runtime", "Network"):
            await page.send(f"{domain}.enable")
        return page


@dataclass
class Page:
    chrome: Chrome
    session: str

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.chrome.send(method, params, self.session)

    def events(self, method: str, since: int = 0) -> list[dict[str, Any]]:
        return [
            event["params"]
            for event in self.chrome.events[since:]
            if event.get("sessionId") == self.session and event["method"] == method
        ]

    async def _loaded(self, action: str, params: dict[str, Any]) -> None:
        mark = len(self.chrome.events)
        await self.send(action, params)
        async with asyncio.timeout(_TIMEOUT):
            while not self.events("Page.loadEventFired", mark):
                await asyncio.sleep(0.05)

    async def navigate(self, url: str) -> None:
        await self._loaded("Page.navigate", {"url": url})

    async def reload(self) -> None:
        await self._loaded("Page.reload", {})

    async def evaluate(self, expression: str) -> Any:
        reply = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in reply:
            raise CDPError(f"页面求值失败：{reply['exceptionDetails'].get('text')}")
        return reply["result"].get("value")

    async def wait_for(self, expression: str, timeout: float = _TIMEOUT) -> Any:
        async with asyncio.timeout(timeout):
            while not (value := await self.evaluate(expression)):
                await asyncio.sleep(0.05)
        return value


@contextlib.asynccontextmanager
async def launch(binary: str) -> AsyncIterator[Chrome]:
    """启动无头 Chrome（独立临时 profile）；退出时关闭浏览器并回收进程。"""
    to_chrome_r, to_chrome_w = os.pipe()
    from_chrome_r, from_chrome_w = os.pipe()
    # 子进程里用 shell 把两端重定向到 3/4；原编号必须避开 3/4，否则重定向会互相覆盖。
    if min(to_chrome_r, from_chrome_w) <= 4:
        raise CDPError("管道编号与 Chrome 约定的 fd 3/4 冲突")
    with tempfile.TemporaryDirectory() as profile:
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            f'exec "$0" "$@" 3<&{to_chrome_r} 4>&{from_chrome_w}',
            binary,
            "--headless",
            "--remote-debugging-pipe",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--disable-extensions",
            "--no-proxy-server",
            "--use-mock-keychain",
            "--password-store=basic",
            "about:blank",
            pass_fds=(to_chrome_r, from_chrome_w),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(from_chrome_r, "rb", 0)
        )
        chrome = Chrome(process, reader, to_chrome_w)
        dispatcher = asyncio.create_task(chrome._dispatch())
        try:
            yield chrome
        finally:
            with contextlib.suppress(CDPError, TimeoutError):
                await chrome.send("Browser.close")
            try:
                await asyncio.wait_for(process.wait(), _TIMEOUT)
            except TimeoutError:
                process.kill()
                await process.wait()
            os.close(to_chrome_w)
            transport.close()
            await dispatcher
