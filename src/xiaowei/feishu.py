"""飞书单聊入口：把获准的 p2p 文本交给共享 ``ChannelService``，并把保存的结果单次发送回去。

接入 ``lark-channel-sdk`` 1.4.0 的长连接（``FeishuChannel``）。该 SDK 在应用处理前就 ack 事件，
处理器失败也不能让平台重投；用户 2026-10-01 接受这一契约：落库失败时消息不处理、不回复，只记
安全原因码，由操作者处理后用户重发。去重、单次发送与不重跑 Agent/查询的语义不变。

- 入站只用 ``on("raw")``：SDK 已认证的完整事件字典含 app、tenant 与时间。这里逐字段核对事件类型、
  应用、唯一租户、用户发送者、获准 ``open_id``、单聊、纯文本、时效与长度，任一不符即在持久化与
  模型之前丢弃。``/查询``、``/诊断`` 只作用于本条消息，普通文本为诊断；``/新建`` 不进入模型。
- 身份：``open_id`` 经配置映射为内部 subject，再由同一个 ``AccessPolicy`` 授权；会话语境是单聊
  ``chat_id``，请求编号是 ``message_id``。``chat_id`` 只在内存中用于本次回复，不持久化。
- SDK 在自己的后台线程事件循环上调用处理器；``LarkTransport`` 只把事件转交给应用事件循环，
  数据库、``ChannelService`` 与队列都只在应用循环上运行。发送经 SDK 的 ``schedule`` 回到其循环。
- SDK 回调到应用循环的转交最多同时有 ``queue_size`` 个事件在途；超出时在持久化前丢弃并记
  ``intake_full``（与落库失败同属已接受的先 ack 契约），不创建协程。
- 新接受的请求进入有界队列，由固定数量的消费者运行；队列满时记为 failed/busy 并发送一次固定
  回执。发送只取得一次投递权：结果明确成功、明确失败或不明分别记录，任何路径都不自动重发。
- 结果保存后到投递状态落定之间被取消或出现意外异常时锁低 readiness，交给重启恢复：已保存但
  未发送的飞书结果在恢复中记为 failed，只能显式重发。正常停止先 ``drain`` 停止接收并有界等待，
  长连接关闭有绝对期限。
"""

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from lark_channel.channel import (
    ChannelConfig,
    FeishuChannel,
    InboundConfig,
    MediaCapabilities,
    OutboundConfig,
    PolicyConfig,
    RetryConfig,
)
from lark_channel.channel.config import TransportConfig
from lark_channel.channel.errors import FeishuChannelErrorCode
from lark_channel.channel.types import SendResult
from lark_channel.core.enum import LogLevel
from lark_channel.ws import client as ws_client

from xiaowei.app import Mode
from xiaowei.channel import (
    AccessDeniedError,
    ChannelService,
    InboundRequest,
    RequestReceipt,
    RequestRef,
    ResultUnavailableError,
)
from xiaowei.channel_store import (
    ChannelStoreError,
    RequestUnavailableError,
    ResultNotSavedError,
    SendOutcome,
    SessionBusyError,
)
from xiaowei.config import FeishuConfig, resolve_secret_ref
from xiaowei.models import Delivery

logger = logging.getLogger(__name__)

EMPTY_COMMAND = "命令后需要写明问题，例如：/查询 昨天各地区订单数"
NEW_SESSION = "已新建会话，之前的对话不再作为上下文"
NEW_SESSION_BUSY = "当前会话正在处理消息，请稍后再新建会话"
TRUNCATED = "（内容超过飞书单条消息上限，已截断）"

_EVENT_TYPE = "im.message.receive_v1"
_COMMANDS: Mapping[str, Mode | Literal["new"]] = {
    "/查询": "query",
    "/诊断": "diagnose",
    "/新建": "new",
}
_CLOCK_SKEW = timedelta(seconds=60)
_MAX_ID_CHARS = 200  # 与 InboundRequest 的 Label 上限一致
# 序列化正文的容器上限：JSON 转义最多把一个字符写成 12 个（代理对 ``\\ud83d\\ude00``），再留出
# ``{"text": ""}`` 与空白的余量。超过时在解析前拒绝，解析后仍按实际文本长度做最终限制。
_JSON_ESCAPE_FACTOR = 12
_CONTENT_OVERHEAD = 64
# 结果已按当前身份、权限或期限决定不交付：投递状态已落定（不可交付时记为 failed），或者本就不能
# 由这个身份读取，不需要锁低 readiness。
_WITHHELD = (ResultUnavailableError, AccessDeniedError, RequestUnavailableError)
_SEEN_COMMANDS = 1024
# SDK 已确定未发出的错误类别；其余（含 unknown、超时、未连接）按结果不明处理。
_DEFINITE_FAILURES = frozenset(
    {
        FeishuChannelErrorCode.FORMAT_ERROR,
        FeishuChannelErrorCode.TARGET_REVOKED,
        FeishuChannelErrorCode.RATE_LIMITED,
        FeishuChannelErrorCode.PERMISSION_DENIED,
        FeishuChannelErrorCode.SSRF_BLOCKED,
    }
)

Send = Callable[[str, str], Awaitable[SendOutcome]]
"""单次文本发送：(chat_id, text) → 明确成功、明确失败或结果不明。"""


class _RejectedError(Exception):
    """事件不能进入渠道：``code`` 是写入日志的安全原因码，不含消息内容或标识。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Message:
    message_id: str
    subject_id: str
    chat_id: str
    text: str


def _field(node: object, *path: str) -> object:
    for name in path:
        if not isinstance(node, Mapping):
            raise _RejectedError("malformed")
        node = node.get(name)
    return node


def _string(node: object, *path: str, max_chars: int = _MAX_ID_CHARS) -> str:
    value = _field(node, *path)
    if not isinstance(value, str) or not 0 < len(value) <= max_chars:
        raise _RejectedError("malformed")
    return value


def _text(event: object, max_chars: int) -> str:
    """序列化正文先按容器上限拒绝，再解析，最后按实际文本长度限制。"""
    raw = _field(event, "event", "message", "content")
    if not isinstance(raw, str):
        raise _RejectedError("content")
    if len(raw) > max_chars * _JSON_ESCAPE_FACTOR + _CONTENT_OVERHEAD:
        raise _RejectedError("content_too_large")
    try:
        content = json.loads(raw)
    except json.JSONDecodeError:
        raise _RejectedError("content") from None
    text = content.get("text") if isinstance(content, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise _RejectedError("content")
    if len(text) > max_chars:
        raise _RejectedError("too_long")
    return text.strip()


def _parse(event: object, config: FeishuConfig, now: datetime) -> _Message:
    """按可信配置逐字段核对事件；任何不符都以安全原因码拒绝。"""
    if _field(event, "header", "event_type") != _EVENT_TYPE:
        raise _RejectedError("event_type")
    if _field(event, "header", "app_id") != config.app_id:
        raise _RejectedError("app")
    if (
        _field(event, "header", "tenant_key") != config.tenant_key
        or _field(event, "event", "sender", "tenant_key") != config.tenant_key
    ):
        raise _RejectedError("tenant")
    if _field(event, "event", "sender", "sender_type") != "user":
        raise _RejectedError("sender_type")
    subject = config.users.get(_string(event, "event", "sender", "sender_id", "open_id"))
    if subject is None:
        raise _RejectedError("sender")
    if _field(event, "event", "message", "chat_type") != "p2p":
        raise _RejectedError("chat_type")
    if _field(event, "event", "message", "message_type") != "text":
        raise _RejectedError("message_type")
    try:
        created = datetime.fromtimestamp(
            int(_string(event, "event", "message", "create_time")) / 1000, UTC
        )
    except (ValueError, OverflowError, OSError):
        raise _RejectedError("malformed") from None
    max_age = timedelta(seconds=config.max_event_age_seconds)
    if now - created > max_age or created - now > _CLOCK_SKEW:
        raise _RejectedError("stale")
    text = _text(event, config.max_message_chars)
    return _Message(
        message_id=_string(event, "event", "message", "message_id"),
        subject_id=subject,
        chat_id=_string(event, "event", "message", "chat_id"),
        text=text,
    )


def _command(text: str) -> tuple[Mode | Literal["new"], str]:
    """消息首部的命令与剥离后的正文；普通文本是诊断。"""
    for name, kind in _COMMANDS.items():
        if text == name or (text.startswith(name) and text[len(name)].isspace()):
            return kind, text[len(name) :].strip()
    return "diagnose", text


def render(content: str, max_chars: int) -> str:
    """飞书纯文本：超过上限时在最后一个完整行处截断并附固定说明，保证只发一条消息。"""
    if len(content) <= max_chars:
        return content
    budget = max_chars - len(TRUNCATED) - 1
    cut = content.rfind("\n", 0, budget + 1)
    body = content[: cut if cut > 0 else budget]
    return f"{body}\n{TRUNCATED}"


def send_outcome(result: object) -> SendOutcome:
    """把 SDK 的 ``SendResult`` 映射为投递结果；只有已知未发出的错误类别才算明确失败。"""
    if not isinstance(result, SendResult):
        return "unknown"
    if result.success:
        return "sent"
    error = result.error
    if error is not None and error.code in _DEFINITE_FAILURES:
        return "failed"
    return "unknown"


@dataclass(frozen=True)
class _Job:
    receipt: RequestReceipt
    ref: RequestRef
    chat_id: str


class FeishuGateway:
    """飞书单聊渠道：在应用事件循环上接受事件、排队运行并单次发送。"""

    def __init__(
        self,
        service: ChannelService,
        config: FeishuConfig,
        send: Send,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if config.consumer_count > service.max_concurrent_turns:
            raise ValueError("consumer_count 不得超过应用的全局并发上限")
        self._service = service
        self._config = config
        self._send = send
        self._clock = clock
        self._readiness = service.results.store.readiness
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=config.queue_size)
        self._closing = False
        # 不写请求表的命令（/新建、空命令提示）只在进程内按消息编号去重。
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()

    def inbound(self, event: object) -> InboundRequest | None:
        """可进入共享服务的请求；被拒绝的事件与不进入模型的命令返回 None。"""
        try:
            message = _parse(event, self._config, self._clock())
        except _RejectedError:
            return None
        kind, body = _command(message.text)
        if kind == "new" or not body:
            return None
        return self._request(message, kind, body)

    async def receive(self, event: object) -> None:
        """处理一条 raw 事件。SDK 已经 ack：这里的任何拒绝或失败都只记录安全原因码。"""
        if self._closing:
            logger.info("飞书事件已丢弃：%s", "closing")
            return
        try:
            message = _parse(event, self._config, self._clock())
        except _RejectedError as rejected:
            logger.info("飞书事件已丢弃：%s", rejected.code)
            return
        kind, body = _command(message.text)
        try:
            if kind == "new":
                await self._new_session(message)
            elif not body:
                if self._first(message):
                    await self._notify(message, EMPTY_COMMAND)
            else:
                await self._accept(message, kind, body)
        except ChannelStoreError as exc:
            logger.warning("飞书请求未处理：%s", type(exc).__name__)

    async def run(self) -> None:
        """运行固定数量的消费者，直到被取消；消费者的意外异常锁低 readiness 并结束整个渠道。

        取消时仍有已入队未运行的请求，同样锁低 readiness，交给重启恢复标为 interrupted。
        """
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(self._config.consumer_count):
                    group.create_task(self._consume())
        finally:
            if not self._queue.empty():
                self._readiness.lock("feishu_queue_abandoned")

    async def idle(self) -> None:
        """等待已入队的请求全部处理完毕（不停止接收）。"""
        await self._queue.join()

    async def drain(self, timeout: float) -> bool:
        """正常停止的第一步：停止接收新事件，有界等待已入队请求处理并投递完毕。

        超时返回 False 并锁低 readiness：剩余请求随后被取消，由重启恢复处理。
        """
        self._closing = True
        try:
            async with asyncio.timeout(timeout):
                await self._queue.join()
        except TimeoutError:
            self._readiness.lock("feishu_drain_timeout")
            logger.warning("飞书渠道停止等待超时")
            return False
        return True

    async def _accept(self, message: _Message, mode: Mode, body: str) -> None:
        receipt = await self._service.accept(self._request(message, mode, body))
        job = _Job(receipt, self._ref(message), message.chat_id)
        if not receipt.created:
            # 重投：只对已结束且投递仍为 pending 的记录尝试首次发送，不再运行。
            await self._deliver(job)
            return
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            await self._service.reject_busy(receipt)
            await self._deliver(job)

    async def _consume(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                try:
                    await self._service.process(job.receipt)
                except ResultNotSavedError:
                    pass  # 已记为 failed/result_not_saved，下面发送固定回执
                await self._deliver(job)
            except asyncio.CancelledError:
                # 本条可能已保存却未落定投递：不再开放新工作，交给重启恢复。
                self._readiness.lock("feishu_consumer_cancelled")
                raise
            except ChannelStoreError as exc:
                logger.warning("飞书请求处理失败：%s", type(exc).__name__)
            except Exception:
                self._readiness.lock("feishu_consumer_failed")
                raise
            finally:
                self._queue.task_done()

    async def _deliver(self, job: _Job) -> None:
        """首次发送：结果已决定不交付时只记录；投递状态未能落定时锁低 readiness 并原样传播。"""

        async def transmit(delivery: Delivery) -> SendOutcome:
            # 已取得投递权：发送异常按结果不明返回，由 ResultDelivery 记为 unknown。
            text = render(delivery.content, self._config.max_reply_chars)
            try:
                return await self._send(job.chat_id, text)
            except Exception as exc:
                logger.error("飞书发送异常，按结果不明记录：%s", type(exc).__name__)
                return "unknown"

        try:
            outcome = await self._service.results.send(job.ref, transmit)
        except _WITHHELD as exc:
            logger.warning("飞书结果不交付：%s", type(exc).__name__)
            return
        except BaseException:
            # 结果可能仍是 completed/pending，且回复目的地只在本进程内：不能以正常状态继续接活。
            self._readiness.lock("feishu_delivery_unsettled")
            raise
        if outcome is not None and outcome != "sent":
            logger.warning("飞书结果发送未成功：%s", outcome)

    async def _new_session(self, message: _Message) -> None:
        if not self._first(message):
            return
        try:
            await self._service.new_session("feishu", message.subject_id, message.chat_id)
        except SessionBusyError:
            await self._notify(message, NEW_SESSION_BUSY)
            return
        await self._notify(message, NEW_SESSION)

    async def _notify(self, message: _Message, text: str) -> None:
        """不写请求表的固定提示：只发一次，结果只记日志。"""
        try:
            outcome = await self._send(message.chat_id, text)
        except Exception as exc:
            logger.error("飞书提示发送异常，结果不明：%s", type(exc).__name__)
            return
        if outcome != "sent":
            logger.warning("飞书提示发送未成功：%s", outcome)

    def _first(self, message: _Message) -> bool:
        key = (message.subject_id, message.message_id)
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > _SEEN_COMMANDS:
            self._seen.popitem(last=False)
        return True

    def _request(self, message: _Message, mode: Mode, body: str) -> InboundRequest:
        return InboundRequest(
            channel="feishu",
            channel_request_id=message.message_id,
            subject_id=message.subject_id,
            conversation_id=message.chat_id,
            mode=mode,
            message=body,
            received_at=self._clock(),
        )

    @staticmethod
    def _ref(message: _Message) -> RequestRef:
        return RequestRef(
            channel="feishu",
            subject_id=message.subject_id,
            conversation_id=message.chat_id,
            request_id=message.message_id,
        )


class _Channel(Protocol):
    """``LarkTransport`` 使用的 ``FeishuChannel`` 公开方法。"""

    def on(self, name: str, handler: Callable[..., Any]) -> Callable[[], None]: ...

    async def start_background(self, *, timeout: float) -> None: ...

    def stop(self, *, join_timeout: float) -> None: ...

    def schedule(self, coro: Coroutine[Any, Any, object]) -> concurrent.futures.Future[object]: ...

    async def send(
        self, to: str, message: dict[str, str], opts: dict[str, str] | None = None
    ) -> object: ...


def lark_channel(config: FeishuConfig) -> FeishuChannel:
    """按单次文本发送装配 SDK：不重试、不分段、只用 raw 入站，关闭入站附加 API 调用。

    SDK 自带的消息管线照常运行但没有消费者；策略设为 disabled，使其尽早丢弃。
    """
    secret = resolve_secret_ref(config.app_secret_ref)
    return FeishuChannel(
        config=ChannelConfig(
            app_id=config.app_id,
            app_secret=secret.get_secret_value(),
            log_level=LogLevel.WARNING,
            transport=TransportConfig(handshake_timeout_seconds=config.connect_timeout_seconds),
            policy=PolicyConfig(dm_policy="disabled", group_policy="disabled"),
            inbound=InboundConfig(
                expand_merge_forward=False,
                fetch_interactive_card=False,
                media_capabilities=MediaCapabilities(
                    image=False, audio=False, video=False, file=False, sticker=False
                ),
                include_raw=False,
                emit_raw_events=True,
            ),
            outbound=OutboundConfig(
                text_chunk_limit=config.max_reply_chars, retry=RetryConfig(max_attempts=1)
            ),
            resolve_sender_names=False,
        )
    )


class LarkTransport:
    """``FeishuChannel`` 与应用事件循环之间的桥：有界转交 raw 事件、单次限时发送、有界关闭。

    SDK 在自己的线程上调用 raw 处理器。在途事件（已转交、应用侧尚未处理完）最多 ``queue_size``
    个，超出时在持久化前丢弃并记 ``intake_full``，不创建协程。关闭调用 SDK 公开的同步 ``stop``：
    它在独立的守护线程中运行，应用循环最多等待 ``stop_timeout_seconds``，超时返回 False。
    """

    def __init__(self, channel: _Channel, config: FeishuConfig) -> None:
        self._channel = channel
        self._config = config
        self._lock = threading.Lock()
        self._in_flight = 0
        self._accepting = False
        self._stopped: bool | None = None

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    async def start(self, receive: Callable[[dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        loop = asyncio.get_running_loop()
        # SDK 的长连接在导入时绑定模块级事件循环，并在自己的线程里运行它；它不能是应用循环。
        if ws_client.loop is loop:
            raise RuntimeError("飞书 SDK 必须在应用事件循环启动前导入")

        def on_raw(payload: dict[str, Any]) -> None:
            with self._lock:
                reason = None
                if not self._accepting:
                    reason = "closing"
                elif self._in_flight >= self._config.queue_size:
                    reason = "intake_full"
                else:
                    self._in_flight += 1
            if reason is not None:
                logger.warning("飞书事件已丢弃：%s", reason)
                return
            coro = receive(payload)
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError:  # 应用循环已关闭
                coro.close()
                self._release(None)
                logger.warning("飞书事件已丢弃：%s", "closing")
                return
            future.add_done_callback(self._release)

        self._channel.on("raw", on_raw)
        self._accepting = True
        await self._channel.start_background(timeout=self._config.connect_timeout_seconds)

    def _release(self, future: concurrent.futures.Future[None] | None) -> None:
        with self._lock:
            self._in_flight -= 1
        if future is not None:
            _report(future)

    async def send(self, chat_id: str, text: str) -> SendOutcome:
        future = self._channel.schedule(
            self._channel.send(chat_id, {"text": text}, {"receive_id_type": "chat_id"})
        )
        try:
            async with asyncio.timeout(self._config.send_timeout_seconds):
                result = await asyncio.wrap_future(future)
        except TimeoutError:
            logger.warning("飞书发送超时，结果不明")
            return "unknown"
        except Exception as exc:
            logger.warning("飞书发送异常，结果不明：%s", type(exc).__name__)
            return "unknown"
        return send_outcome(result)

    async def stop(self) -> bool:
        """停止接收并关闭 SDK；在期限内完成返回 True。重复调用返回第一次的结果，不再关闭。

        超时后不再等待：守护线程不阻止进程退出，残余线程与真实长连接的关闭需在实测中核对。
        SDK 关闭抛出的异常原样传播。
        """
        self._accepting = False
        if self._stopped is not None:
            return self._stopped
        timeout = self._config.stop_timeout_seconds
        done: concurrent.futures.Future[None] = concurrent.futures.Future()

        def close() -> None:
            try:
                self._channel.stop(join_timeout=timeout)
            except BaseException as exc:
                with contextlib.suppress(concurrent.futures.InvalidStateError):
                    done.set_exception(exc)
            else:
                with contextlib.suppress(concurrent.futures.InvalidStateError):
                    done.set_result(None)

        self._stopped = False
        threading.Thread(target=close, name="xiaowei-feishu-stop", daemon=True).start()
        try:
            async with asyncio.timeout(timeout):
                await asyncio.wrap_future(done)
        except TimeoutError:
            logger.error("飞书长连接关闭超时（%s 秒），不再等待", timeout)
            return False
        self._stopped = True
        return True


def _report(future: concurrent.futures.Future[None]) -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        logger.error("飞书事件处理异常：%s", type(exc).__name__)
