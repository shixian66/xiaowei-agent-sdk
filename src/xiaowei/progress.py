"""每次获准执行的易失进度视图；不参与 Agent、存储或正式交付的控制流。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

from agents import Agent, ModelResponse, RunContextWrapper, RunHooks, Tool, TResponseInputItem

from xiaowei.feishu_render import Message
from xiaowei.models import RunContext

Stage = Literal[
    "preparing",
    "model",
    "tools",
    "saving",
    "saved",
    "awaiting",
    "approval",
    "delivery",
    "sent",
    "unsent",
    "failed",
]
_LABELS: dict[Stage, str] = {
    "preparing": "准备工具",
    "model": "模型处理中",
    "tools": "工具请求",
    "saving": "保存结果",
    "saved": "结果已保存，准备交付",
    "awaiting": "等待补充信息",
    "approval": "等待审批",
    "delivery": "准备正式回复",
    "sent": "正式回复已送达",
    "unsent": "正式回复尚未确认送达",
    "failed": "本次调查未完成",
}
logger = logging.getLogger(__name__)


class Progress:
    """hook 只改内存；每次最多一个有界发送任务，失败停用且不自动重试。"""

    def __init__(
        self,
        create: Callable[[Message], Awaitable[str | None]],
        update: Callable[[str, Message], Awaitable[bool]],
        *,
        grouped: bool,
        delay: float | None = None,
        interval: float = 3.0,
        io_timeout: float = 3.0,
        slots: asyncio.Semaphore | None = None,
    ) -> None:
        self._create, self._update = create, update
        self._delay = (5.0 if grouped else 2.0) if delay is None else delay
        self._interval, self._timeout = interval, io_timeout
        self._slots = slots
        self._changed = asyncio.Event()
        self._stage: Stage = "preparing"
        self._observed = datetime.now(UTC)
        self._active: set[str] = set()
        self._finished = self._disabled = self._sending = False
        self._started = time.monotonic()
        self.task: asyncio.Task[None] | None = None
        self.hooks = _Hooks(self)

    @property
    def finished(self) -> bool:
        return self._finished

    def start(self) -> None:
        """只由渠道在授权及原子 start 成功后调用。"""
        if self.task is None and not self._disabled and not self._finished:
            self._started = time.monotonic()
            self.task = asyncio.create_task(self._run(), name="xiaowei-progress")

    def mark(self, stage: Stage, *, call_id: str | None = None, ended: bool = False) -> None:
        try:
            self._record(stage, call_id=call_id, ended=ended)
        except Exception:
            self.stop()

    def _record(self, stage: Stage, *, call_id: str | None, ended: bool) -> None:
        if self._finished or self._disabled:
            return
        if call_id is not None:
            if ended:
                self._active.discard(call_id)
            else:
                self._active.add(call_id)
            stage = "tools" if self._active else "model"
        if stage != self._stage:
            self._stage = stage
            self._observed = datetime.now(UTC)
            self._changed.set()

    def finish(self, stage: Stage) -> None:
        """不等待网络；在途发送取消后不补终态，避免旧更新覆盖收尾。"""
        if self._finished:
            return
        self.mark(stage)
        self._finished = True
        if self._sending:
            self.stop()
        self._changed.set()

    def stop(self) -> None:
        self._disabled = True
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()

    def _card(self) -> Message:
        label = _LABELS[self._stage]
        observed = self._observed.astimezone(UTC).strftime("%H:%M:%S UTC")
        elapsed = max(0, int(time.monotonic() - self._started))
        return {
            "card": {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": "小维 · 任务进度"}},
                "body": {
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "plain_text",
                                "content": (
                                    f"{label}\n已用时 {elapsed} 秒 · 最近观察 {observed}\n"
                                    "进度可能停止更新，最终结果以正式回复为准。"
                                ),
                            },
                        }
                    ]
                },
            }
        }

    async def _run(self) -> None:
        acquired = False
        try:
            deadline = self._started + self._delay
            while not self._finished and time.monotonic() < deadline:
                self._changed.clear()
                try:
                    async with asyncio.timeout(deadline - time.monotonic()):
                        await self._changed.wait()
                except TimeoutError:
                    break
            if self._finished or self._disabled:
                return
            # 已有发送占满时直接放弃本次观察，不排队积压终态或新卡。
            if self._slots is not None:
                if self._slots.locked():
                    return
                await self._slots.acquire()
                acquired = True
            self._changed.clear()
            sent_stage = self._stage
            self._sending = True
            async with asyncio.timeout(self._timeout):
                message_id = await self._create(self._card())
            self._sending = False
            if not message_id:
                return
            sent_at = time.monotonic()
            while not self._disabled:
                if not self._finished:
                    await self._changed.wait()
                    remaining = self._interval - (time.monotonic() - sent_at)
                    if remaining > 0 and not self._finished:
                        self._changed.clear()
                        try:
                            async with asyncio.timeout(remaining):
                                while not self._finished:
                                    await self._changed.wait()
                                    self._changed.clear()
                        except TimeoutError:
                            pass
                self._changed.clear()
                if self._stage != sent_stage:
                    sent_stage = self._stage
                    self._sending = True
                    async with asyncio.timeout(self._timeout):
                        success = await self._update(message_id, self._card())
                    self._sending = False
                    if not success:
                        return
                    sent_at = time.monotonic()
                if self._finished:
                    return
        except asyncio.CancelledError:
            return  # 只取消独立观察任务，不转换业务任务的取消。
        except Exception:
            logger.debug("进度观察停用；正式结果继续原路径")
        finally:
            self._disabled = True
            self._active.clear()
            if acquired and self._slots is not None:
                self._slots.release()


class _Hooks(RunHooks[RunContext]):
    def __init__(self, progress: Progress) -> None:
        self._progress = progress

    async def on_llm_start(
        self,
        context: RunContextWrapper[RunContext],
        agent: Agent[RunContext],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        self._progress.mark("model")

    async def on_llm_end(
        self,
        context: RunContextWrapper[RunContext],
        agent: Agent[RunContext],
        response: ModelResponse,
    ) -> None:
        pass  # 模型结束不等于保存或交付成功。

    async def on_tool_start(
        self,
        context: RunContextWrapper[RunContext],
        agent: Agent[RunContext],
        tool: Tool,
    ) -> None:
        self._progress.mark("tools", call_id=getattr(context, "tool_call_id", ""))

    async def on_tool_end(
        self,
        context: RunContextWrapper[RunContext],
        agent: Agent[RunContext],
        tool: Tool,
        result: object,
    ) -> None:
        self._progress.mark("model", call_id=getattr(context, "tool_call_id", ""), ended=True)
