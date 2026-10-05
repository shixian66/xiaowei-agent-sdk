"""飞书入口：把获准的单聊文本与指定群内 @本机器人 的文本交给共享 ``ChannelService``，并把保存的
结果单次发送回去。

接入 ``lark-channel-sdk`` 1.4.0 的长连接（``FeishuChannel``）。该 SDK 在应用处理前就 ack 事件，
处理器失败也不能让平台重投；用户 2026-10-01 接受这一契约：落库失败时消息不处理、不回复，只记
安全原因码，由操作者处理后用户重发。去重、单次发送与不重跑 Agent/查询的语义不变。

- 入站只用 ``on("raw")``：SDK 已认证的完整事件字典含 app、tenant 与时间。这里逐字段核对事件类型、
  应用、唯一租户、用户发送者、获准 ``open_id``、单聊、纯文本、时效与长度，任一不符即在持久化与
  模型之前丢弃。普通文本是默认用途（``query``）：查询工具按授权可见，由单 Agent 按正文判断是否
  查询；``/查询`` 与普通文本相同。``/诊断`` 只作用于本条消息，隐藏并拒绝实际查询工具；``/新建``
  不进入模型。
- 身份：``open_id`` 经配置映射为内部 subject，再由同一个 ``AccessPolicy`` 授权；会话语境是单聊
  ``chat_id``，请求编号是 ``message_id``。``chat_id`` 只在内存中用于本次回复，不持久化。
- 指定群（可选配置 ``group``）：只接受该群、``chat_type=group``、平台 ``mentions`` 中含本机器人
  ``open_id`` 的消息；本机器人身份只取 SDK 已解析且属于本应用的身份，未解析时群消息一律丢弃，
  不按名称或正文识别。只删除本机器人 mention 对应的 token。发起人（actor）是发送者 ``open_id``，
  会话归属是群；发送者不需要出现在 ``users`` 中。交给模型的正文首行是由发送者计算的有界作者
  标识（不含 ``open_id``），使共享历史区分各轮作者；它不赋予任何权限。群内一切发送都以
  ``reply_to`` 回复原消息、``reply_target_gone="fail"``：原消息不可回复时明确失败，不改发新消息。
- SDK 在自己的后台线程事件循环上调用处理器；``LarkTransport`` 只把事件转交给应用事件循环，
  数据库、``ChannelService`` 与队列都只在应用循环上运行。发送经 SDK 的 ``schedule`` 回到其循环。
- SDK 回调到应用循环的转交最多同时有 ``queue_size`` 个事件在途；超出时在持久化前丢弃并记
  ``intake_full``（与落库失败同属已接受的先 ack 契约），不创建协程。
- 新接受的请求进入有界队列，由固定数量的消费者运行；队列满时记为 failed/busy 并发送一次固定
  回执。指定群同一时刻只运行一条；尚未开始的群请求（含下一条将运行的队头）都按持久接受的顺序
  留在有界的群 FIFO 中，不占消费者；新进入等待的请求得到一次无数据的排队提示（不查成员目录）。
  群空闲且队头的排队提示尝试已结束时，共享队列中才放入一个“群可运行”标记（另占一个保留槽），
  消费者取到标记时在同一步内取出队头开始运行；上一条的结果保存且一次投递落定后才放下一条的标记。
  等待超过上限或期限的请求记为 failed/busy 并回复固定回执，不运行：定时检查每个配置间隔取出
  全部到期、尚未开始的请求，消费者开始前再核对一次；所有到期项按 turn_id 登记，经同一条路径先
  持久化，再等提示尝试结束，最后在有界并发内经 ``ResultDelivery`` 回执（仍按当前成员资格复核），
  登记期间同一请求的重投不查成员目录、不发送。检查间隔只约束发现与状态结算；先后只就本地发送
  尝试而言，平台实际收到的时间与先后另受成员查询、网络、发送期限与不明结果影响。发送只取得一次
  投递权：结果明确成功、明确失败或不明分别记录，任何路径都不自动重发。
- 结果保存后到投递状态落定之间被取消或出现意外异常时锁低 readiness，交给重启恢复：已保存但
  未发送的飞书结果在恢复中记为 failed，只能显式重发。正常停止先 ``drain``：停止接收，在同一期限
  内等待已进入 ``receive`` 的调用（含落库前的与排队提示）、队列与群 FIFO、到期项的结算与回执都
  结束；长连接关闭有绝对期限。停机取消到达后不再交接下一条、不再创建到期任务。
"""

import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, replace
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
    GroupScope,
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
from xiaowei.config import FeishuConfig, FeishuGroupConfig, resolve_secret_ref
from xiaowei.models import Delivery

logger = logging.getLogger(__name__)

_FINISHED = frozenset({"completed", "failed", "interrupted"})

QUEUED = "已收到，前面还有问题在处理，将按顺序回复"
# 到期回执同时在途的上限：每条都要复核当前成员（一次成员目录查询）再发送；任务总数另受群等待
# 容量约束（回执落定前仍占容量）。一条挂起不影响其余，最多占住一个名额直到成员查询或发送期限。
_EXPIRY_RECEIPTS = 4
EMPTY_COMMAND = "命令后需要写明问题，例如：/查询 昨天各地区订单数，或 /诊断 这条 SQL 为什么慢"
NEW_SESSION = "已新建会话，之前的对话不再作为上下文"
NEW_SESSION_BUSY = "当前会话正在处理消息，请稍后再新建会话"
NEW_GROUP_SESSION = "已为本群新建会话：群内之前的对话不再作为任何成员的上下文"
TRUNCATED = "（内容超过飞书单条消息上限，已截断）"
FACTS_TRUNCATED = "（工具结果超过飞书单条上限，已截断）"
# 渲染后的一个转义单位：\uXXXX、反斜杠加一个字符，或单个字符；截断不拆开它。
_ESCAPE_UNIT = re.compile(r"\\u[0-9a-fA-F]{4}|\\.|.", re.DOTALL)

_EVENT_TYPE = "im.message.receive_v1"
_COMMANDS: Mapping[str, Mode | Literal["new"]] = {
    "/查询": "query",
    "/诊断": "diagnose",
    "/新建": "new",
}
_CLOCK_SKEW = timedelta(seconds=60)
_MAX_ID_CHARS = 200  # 与 InboundRequest 的 Label 上限一致
_OPEN_ID = re.compile(r"ou_[0-9A-Za-z_-]{1,64}")
# 交给模型的群消息首行；正文中出现同样的开头时改写，使首行之外不能冒充作者标识。
_AUTHOR = "【群成员 "
_FORGED_AUTHOR = "[群成员 "
# 飞书文本中的 ``<at ...>`` 会被平台渲染为 @（含 ``user_id="all"``）；发出的文本把它改成全角
# 尖括号，使回答或工具结果中的文字不能 @ 任何人。字符数不变，渲染上限照常成立。
_AT_TAG = re.compile(r"<(?=\s*at\b)", re.IGNORECASE)
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
# 平台已拒绝、但锁版 SDK 归为 unknown 的业务码（官方回复接口）：230011 原消息已撤回，230050 原消息
# 对操作者不可见。
_DEFINITE_FAILURE_CODES = frozenset({230011, 230050})


class Send(Protocol):
    """单次文本发送：(chat_id, text) → 明确成功、明确失败或结果不明。群消息另带 ``reply_to``
    （原消息编号），只回复原消息。"""

    def __call__(
        self, chat_id: str, text: str, *, reply_to: str | None = ...
    ) -> Awaitable[SendOutcome]: ...


BotOpenId = Callable[[], str | None]
"""本机器人的 ``open_id``；SDK 尚未解析或不属于本应用时为 None。"""


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
    group: GroupScope | None = None


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
    try:
        text.encode("utf-8")  # JSON 允许孤立的代理码点，它不是合法文本
    except UnicodeEncodeError:
        raise _RejectedError("content") from None
    if len(text) > max_chars:
        raise _RejectedError("too_long")
    return text.strip()


def _parse(event: object, config: FeishuConfig, now: datetime, bot: BotOpenId) -> _Message:
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
    open_id = _string(event, "event", "sender", "sender_id", "open_id")
    chat_id = _string(event, "event", "message", "chat_id")
    chat_type = _field(event, "event", "message", "chat_type")
    group: GroupScope | None = None
    if chat_type == "p2p":
        subject = config.users.get(open_id)
        if subject is None:
            raise _RejectedError("sender")
    elif chat_type == "group" and config.group is not None:
        if chat_id != config.group.chat_id:
            raise _RejectedError("chat")
        if _OPEN_ID.fullmatch(open_id) is None:
            raise _RejectedError("sender")
        subject = open_id
        group = GroupScope(app_id=config.app_id, tenant_key=config.tenant_key, chat_id=chat_id)
    else:
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
    if group is not None:
        text = _addressed(event, text, bot())
    return _Message(
        message_id=_string(event, "event", "message", "message_id"),
        subject_id=subject,
        chat_id=chat_id,
        text=text,
        group=group,
    )


def _addressed(event: object, text: str, bot: str | None) -> str:
    """群消息必须在平台 ``mentions`` 中 @本机器人；删除这些 mention 的 token 后返回正文。

    只按 ``mentions[].id.open_id`` 与 SDK 已解析的本机器人身份比对：名称、正文里的“@小维”或
    其他机器人都不算。其他成员的 mention token 原样保留（不可信正文的一部分）。
    """
    if bot is None:
        raise _RejectedError("bot_identity")
    mentions = _field(event, "event", "message", "mentions")
    if not isinstance(mentions, list):
        raise _RejectedError("mention")
    keys = {
        _string(mention, "key")
        for mention in mentions
        if isinstance(mention, Mapping) and _field(mention, "id", "open_id") == bot
    }
    if not keys:
        raise _RejectedError("mention")
    for key in keys:
        # 只删除完整 token：``@_user_1`` 不能吃掉 ``@_user_10`` 的前缀。
        text = re.sub(re.escape(key) + r"(?![0-9A-Za-z_])", "", text)
    return text.strip()


def attributed(open_id: str, text: str) -> str:
    """群消息交给模型的形式：首行是由发送者 ``open_id`` 计算的有界作者标识，其后是正文。

    标识只用于在共享历史中区分各轮作者，不含 ``open_id`` 本身，也不赋予任何权限；正文中冒充
    首行格式的文字被改写。
    """
    alias = hashlib.sha256(open_id.encode()).hexdigest()[:8]
    return f"{_AUTHOR}{alias}】\n{text.replace(_AUTHOR, _FORGED_AUTHOR)}"


def mention_safe(text: str) -> str:
    """发往飞书的文本：``<at`` 改为全角尖括号，回答与工具结果不能 @ 任何人。"""
    return _AT_TAG.sub("＜", text)


def _command(text: str) -> tuple[Mode | Literal["new"], str]:
    """消息首部的命令与剥离后的正文；普通文本是默认用途（由单 Agent 判断是否查询）。"""
    for name, kind in _COMMANDS.items():
        if text == name or (text.startswith(name) and text[len(name)].isspace()):
            return kind, text[len(name) :].strip()
    return "query", text


def render(delivery: Delivery, max_chars: int) -> str:
    """飞书纯文本，保证只发一条、不超过 ``max_chars``；不超限时与 ``content`` 逐字相同。

    有分段（证据回答）时先放完整的分析，剩余字数按顺序给工具结果：先保留每条事实的来源与
    说明行，再保留结果行，放不下的截掉并注明。分析本身超限时保留标题、从末尾截断。没有分段
    （澄清、固定回执）时从末尾截断。分段由 ``EvidenceStore`` 生成，不在文字中查找标题。
    """
    content = delivery.content
    if len(content) <= max_chars:
        return content
    layout = delivery.layout
    if layout is None:
        # 澄清是“标题 + 一行正文”：正文放不下时保留它的前缀，而不是只剩标题。
        return _cut_tail(content.split("\n"), max_chars, keep=1)
    kept = [layout.facts_header, FACTS_TRUNCATED, *layout.analysis]
    room = max_chars - _joined(kept)
    if room < 0:
        return _cut_tail(kept, max_chars, keep=2 + min(len(layout.analysis), 1))
    heads: list[list[str]] = [[] for _ in layout.facts]
    bodies: list[list[str]] = [[] for _ in layout.facts]
    pending = [(heads[i], line) for i, f in enumerate(layout.facts) for line in f.head]
    pending += [(bodies[i], line) for i, f in enumerate(layout.facts) for line in f.body]
    for target, line in pending:
        if len(line) + 1 > room:
            break  # 不跳行：保留的结果行在每条事实内保持连续
        target.append(line)
        room -= len(line) + 1
    facts = [line for head, body in zip(heads, bodies, strict=True) for line in (*head, *body)]
    return "\n".join([layout.facts_header, *facts, FACTS_TRUNCATED, *layout.analysis])


def _joined(lines: list[str]) -> int:
    return sum(map(len, lines)) + len(lines) - 1


def _cut_tail(lines: list[str], max_chars: int, *, keep: int = 0) -> str:
    """保留能完整放下的前若干行并附截断说明；前 ``keep`` 行之后一行都放不下时截取下一行。"""
    budget = max_chars - len(TRUNCATED) - 1
    shown: list[str] = []
    used = -1
    for line in lines:
        if used + 1 + len(line) > budget:
            break
        shown.append(line)
        used += 1 + len(line)
    if len(shown) <= keep and len(shown) < len(lines):
        prefix = _prefix(lines[len(shown)], budget - used - 1)
        if prefix:
            shown.append(prefix)
    return "\n".join([*shown, TRUNCATED])


def _prefix(line: str, limit: int) -> str:
    """不超过 ``limit`` 个字符、且不拆开任何转义单位的最长前缀。"""
    end = 0
    for unit in _ESCAPE_UNIT.finditer(line):
        if unit.end() > limit:
            break
        end = unit.end()
    return line[:end]


def send_outcome(result: object) -> SendOutcome:
    """把 SDK 的 ``SendResult`` 映射为投递结果；只有已知未发出的错误类别或业务码才算明确失败。

    成功必须带平台返回的 ``message_id``：SDK 1.4.0 把没有业务 ``code`` 的 HTTP 错误响应（如 500
    ``{}``）当作成功返回，这时结果不明。
    """
    if not isinstance(result, SendResult):
        return "unknown"
    if result.success:
        return "sent" if result.message_id else "unknown"
    error = result.error
    if error is not None and (
        error.code in _DEFINITE_FAILURES or error.raw_code in _DEFINITE_FAILURE_CODES
    ):
        return "failed"
    return "unknown"


@dataclass(frozen=True)
class _Job:
    receipt: RequestReceipt
    ref: RequestRef
    chat_id: str
    reply_to: str | None
    # 进入群等待时的排队提示：发送尝试结束（成功、失败、不明或异常）时设置。尝试结束前这条请求
    # 不交给消费者，它的繁忙回执也不发送：本地的发送尝试中提示在前（平台收到的先后另受发送期限与
    # 不明结果影响）。
    notice: asyncio.Event | None = None


class FeishuGateway:
    """飞书渠道：在应用事件循环上接受事件、排队运行并单次发送。

    ``bot_open_id`` 给出本机器人的可信身份，只用于识别群内 @本机器人；单聊不使用它。
    """

    def __init__(
        self,
        service: ChannelService,
        config: FeishuConfig,
        send: Send,
        *,
        clock: Callable[[], datetime],
        bot_open_id: BotOpenId = lambda: None,
    ) -> None:
        if config.consumer_count > service.max_concurrent_turns:
            raise ValueError("consumer_count 不得超过应用的全局并发上限")
        self._service = service
        self._config = config
        self._send = send
        self._clock = clock
        self._bot = bot_open_id
        self._readiness = service.results.store.readiness
        # 尚未开始的群请求（含下一条将运行的队头）都留在群 FIFO 中，始终受等待期限管理。群空闲且
        # 队头的排队提示尝试已结束时，共享队列中放入一个“群可运行”标记（None，另占一个保留槽，
        # 同一时刻最多一个）；消费者取到标记时才在同一步内取出队头并标记运行中。等待提示不占消费者，
        # 其他请求仍以 queue_size 为上限。
        reserved = 0 if config.group is None else 1
        self._queue: asyncio.Queue[_Job | None] = asyncio.Queue(
            maxsize=config.queue_size + reserved
        )
        self._plain = 0
        self._group_lock = asyncio.Lock()
        self._group_running = False
        self._group_signalled = False
        self._group_waiting: deque[_Job] = deque()
        # 已从群 FIFO 取出的到期项按 turn_id 登记：结算、等提示与回执都在 run 的任务组中进行。登记
        # 期间同一请求的重投不查成员目录、不发送；回执落定前仍占等待容量。
        self._tasks: asyncio.TaskGroup | None = None
        self._expiring: set[str] = set()
        self._expired_settled = asyncio.Event()
        self._expired_settled.set()
        self._receipt_slots = asyncio.Semaphore(_EXPIRY_RECEIPTS)
        self._closing = False
        # 已过关闭检查、尚未返回的 receive 调用：落库前的请求还不在队列中，drain 必须等它们。
        self._receiving = 0
        self._received = asyncio.Event()
        self._received.set()
        # 不写请求表的命令（/新建、空命令提示）只在进程内按消息编号去重。
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()

    def inbound(self, event: object) -> InboundRequest | None:
        """可进入共享服务的请求；被拒绝的事件与不进入模型的命令返回 None。"""
        try:
            message = _parse(event, self._config, self._clock(), self._bot)
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
        self._receiving += 1
        self._received.clear()
        try:
            await self._receive(event)
        finally:
            self._receiving -= 1
            if not self._receiving:
                self._received.set()

    async def _receive(self, event: object) -> None:
        try:
            message = _parse(event, self._config, self._clock(), self._bot)
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
        """运行固定数量的消费者（与群等待到期检查），直到被取消；消费者的意外异常锁低 readiness 并
        结束整个渠道。

        取消时仍有已入队或在群 FIFO 中等待的请求，同样锁低 readiness，交给重启恢复标为 interrupted。
        """
        try:
            async with asyncio.TaskGroup() as group:
                self._tasks = group
                for _ in range(self._config.consumer_count):
                    group.create_task(self._consume())
                if self._config.group is not None:
                    group.create_task(self._sweep(self._config.group.wait_check_seconds))
        finally:
            self._tasks = None
            if (
                not self._queue.empty()
                or self._group_waiting
                or self._group_running
                or self._expiring
            ):
                self._readiness.lock("feishu_queue_abandoned")

    async def idle(self) -> None:
        """等待已入队的请求全部处理完毕（不停止接收）。"""
        await self._queue.join()

    async def drain(self, timeout: float) -> bool:
        """正常停止的第一步：停止接收新事件，在同一期限内先等已进入 ``receive`` 的调用结束（它们
        可能仍在落库、新建会话或发送提示），再等队列与群 FIFO 中的请求处理并投递完毕，以及到期项的
        结算与回执。``receive`` 都结束后排队提示也都已结束；群空闲且队头可运行时必有标记在队列中，
        群队头结束时先放下一条的标记再计为完成，``join`` 因此也等待群 FIFO。

        超时返回 False 并锁低 readiness：剩余工作随后被取消，由重启恢复处理。
        """
        self._closing = True
        try:
            async with asyncio.timeout(timeout):
                await self._received.wait()
                await self._queue.join()
                await self._expired_settled.wait()
        except TimeoutError:
            self._readiness.lock("feishu_drain_timeout")
            logger.warning("飞书渠道停止等待超时")
            return False
        return True

    async def _accept(self, message: _Message, mode: Mode, body: str) -> None:
        request = self._request(message, mode, body)
        if message.group is None:
            receipt = await self._service.accept(request)
            job = self._job(receipt, message)
            if receipt.created and self._plain < self._config.queue_size:
                self._plain += 1
                self._queue.put_nowait(job)
                return
            placement = "full" if receipt.created else None
        else:
            # 接受与入队在同一个群临界区内：同群的运行顺序就是持久接受的顺序。接受之后到入队之间
            # 没有 await，不会出现已接受却未入队又未被拒绝的请求。
            async with self._group_lock:
                receipt = await self._service.accept(request)
                job = self._job(receipt, message)
                placement, job = self._place(job) if receipt.created else (None, job)
        if placement is None:
            # 重投不再运行。只有已结束且投递仍为 pending 的记录进入首次发送竞争（交付时复核当前权限
            # 与成员资格）；排队、运行、发送中或投递已落定的记录直接返回，不产生目录查询或发送。
            # 到期任务仍在进行时同样返回：回执只由它在提示门与回执上限内发送一次；它结束后仍为
            # pending（例如成员资格暂时无法确认）的，重投照常进入首次发送竞争。
            record = receipt.record
            if record.turn_id in self._expiring:
                return
            if record.state in _FINISHED and record.delivery == "pending":
                await self._deliver(job)
        elif placement == "waiting" and job.notice is not None:
            # 一次无数据的排队提示：不经 ResultDelivery、不查成员目录、不占最终结果的投递状态。
            # 在群锁之外发送；尝试一结束（含失败与取消）就放行这条请求的运行或回执。
            try:
                await self._notify(message, QUEUED)
            finally:
                job.notice.set()
                self._signal()
        elif placement == "full":
            await self._service.reject_busy(receipt)
            await self._deliver(job)

    def _job(self, receipt: RequestReceipt, message: _Message) -> _Job:
        return _Job(receipt, self._ref(message), message.chat_id, self._reply_to(message))

    def _place(self, job: _Job) -> tuple[str, _Job]:
        """新接受的群请求：群空闲时成为队头（无排队提示），否则在上限内带着排队提示的完成标记排在
        群 FIFO 末尾，超出上限为 full。

        运行中的一条或尚未开始的队头占一个位置，其余等待项与回执尚未落定的到期项共同受
        ``max_waiting`` 约束，使到期任务数也不超过它。
        """
        group = self._group_settings()
        if not self._group_running and not self._group_waiting:
            self._group_waiting.append(job)
            self._signal()
            return "head", job
        occupied = len(self._group_waiting) + self._group_running + len(self._expiring)
        if occupied <= group.max_waiting:
            job = replace(job, notice=asyncio.Event())
            self._group_waiting.append(job)
            return "waiting", job
        return "full", job

    def _signal(self) -> None:
        """群空闲、队头存在且其排队提示尝试已结束时，在共享队列中放入一个标记（最多一个）。"""
        if self._group_running or self._group_signalled or not self._group_waiting:
            return
        notice = self._group_waiting[0].notice
        if notice is None or notice.is_set():
            self._group_signalled = True
            self._queue.put_nowait(None)

    def _start_group(self) -> _Job | None:
        """消费者取到标记：在同一步内取出可运行的队头并标记运行中，之前已到期的转交结算。

        与定时检查、提示放行之间没有 await：同一请求要么开始运行（此后不再受期限管理），要么被
        结算为到期，不会两者都发生。标记已无可运行的队头时什么也不做（提示尝试结束时再放标记）。
        """
        self._group_signalled = False
        while self._group_waiting:
            job = self._group_waiting[0]
            if self._expired(job):
                self._group_waiting.popleft()
                self._retire(job)
                continue
            if job.notice is not None and not job.notice.is_set():
                return None
            self._group_waiting.popleft()
            self._group_running = True
            return job
        return None

    def _expired(self, job: _Job) -> bool:
        waited = self._clock() - job.receipt.record.created_at
        return waited > timedelta(seconds=self._group_settings().max_wait_seconds)

    def _group_settings(self) -> FeishuGroupConfig:
        group = self._config.group
        if group is None:  # 群消息只在配置了指定群时才会解析出来
            raise RuntimeError("未配置指定群")
        return group

    def _retire(self, job: _Job) -> None:
        """已从群 FIFO 取出的到期项（定时检查或消费者开始前发现）按 turn_id 登记，交给独立任务结算
        与回复；调用方不等待任何外部 I/O。只在 run 的消费者与定时检查中调用，取消到达后不再调用。"""
        if self._tasks is None:  # 群 FIFO 只在 run 中推进
            raise RuntimeError("飞书渠道未运行")
        self._expiring.add(job.receipt.record.turn_id)
        self._expired_settled.clear()
        self._tasks.create_task(self._settle_expired(job))

    async def _settle_expired(self, job: _Job) -> None:
        """到期项唯一的结算与回执路径：先持久化为 failed/busy（只写本地存储），再等这条的排队提示
        尝试结束，最后在有界并发内发送回执；不运行、不调用模型或业务数据库。

        发送仍经 ``ResultDelivery``，按当前成员资格、接收权限、Evidence 与回复目的地复核，投递权在
        提示结束后才取得。一条的成员查询或发送阻塞、失败只影响它自己。
        """
        try:
            await self._service.reject_busy(job.receipt)
            if job.notice is not None:
                await job.notice.wait()
            async with self._receipt_slots:
                await self._deliver(job)
        except ChannelStoreError as exc:
            logger.warning("飞书排队请求到期未处理：%s", type(exc).__name__)
        except asyncio.CancelledError:
            # 已结算或正在结算的回执可能未落定：不再开放新工作，交给重启恢复与显式重发。
            self._readiness.lock("feishu_expiry_cancelled")
            raise
        except Exception:
            self._readiness.lock("feishu_expiry_failed")
            raise
        finally:
            self._expiring.discard(job.receipt.record.turn_id)
            if not self._expiring:
                self._expired_settled.set()

    async def _sweep(self, interval: float) -> None:
        """到期检查：每 ``interval`` 取出群 FIFO 中全部已到期、尚未开始的请求（含等消费者的队头与
        提示仍在发送的等待项）并转交结算，不等队头结束，也不等任何回执发送。"""
        while True:
            await asyncio.sleep(interval)
            kept: deque[_Job] = deque()
            expired: list[_Job] = []
            for job in self._group_waiting:
                (expired if self._expired(job) else kept).append(job)
            self._group_waiting = kept
            for job in expired:
                self._retire(job)
            self._signal()

    async def _consume(self) -> None:
        task = asyncio.current_task()
        while True:
            item = await self._queue.get()
            try:
                job = item if item is not None else self._start_group()
                if job is None:
                    continue  # 标记已无可运行的队头
                if item is not None:
                    self._plain -= 1
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
                if task is not None and task.cancelling():
                    # 取消已到达，却被下层转换成了普通失败（本条已按失败处理）：不交接下一条、不创建
                    # 到期任务（任务组正在关闭），也不再取下一项；剩余请求交给重启恢复。
                    self._readiness.lock("feishu_consumer_cancelled")
                    raise asyncio.CancelledError
                if item is None:
                    # 群队头已结束（失败也算）：先放下一条的标记再计为完成，drain 的 join 因此也
                    # 等待群 FIFO。
                    self._group_running = False
                    self._signal()
            finally:
                self._queue.task_done()

    async def _deliver(self, job: _Job) -> None:
        """首次发送：结果已决定不交付时只记录；投递状态未能落定时锁低 readiness 并原样传播。"""

        async def transmit(delivery: Delivery) -> SendOutcome:
            # 已取得投递权：发送异常按结果不明返回，由 ResultDelivery 记为 unknown。
            text = render(delivery, self._config.max_reply_chars)
            try:
                return await self._send(job.chat_id, text, reply_to=job.reply_to)
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
            await self._service.new_session(
                "feishu", message.subject_id, message.chat_id, group=message.group
            )
        except SessionBusyError:
            await self._notify(message, NEW_SESSION_BUSY)
            return
        await self._notify(message, NEW_SESSION if message.group is None else NEW_GROUP_SESSION)

    async def _notify(self, message: _Message, text: str) -> None:
        """不写请求表的固定提示：只发一次，结果只记日志。"""
        try:
            outcome = await self._send(message.chat_id, text, reply_to=self._reply_to(message))
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
            message=body if message.group is None else attributed(message.subject_id, body),
            received_at=self._clock(),
            group=message.group,
        )

    @staticmethod
    def _ref(message: _Message) -> RequestRef:
        return RequestRef(
            channel="feishu",
            subject_id=message.subject_id,
            conversation_id=message.chat_id,
            request_id=message.message_id,
            group=message.group,
        )

    @staticmethod
    def _reply_to(message: _Message) -> str | None:
        """群内的一切发送都回复原消息；单聊直接发往会话。"""
        return None if message.group is None else message.message_id


class LarkChannel(Protocol):
    """``LarkTransport`` 使用的 ``FeishuChannel`` 公开方法。"""

    def on(self, name: str, handler: Callable[..., Any]) -> Callable[[], None]: ...

    async def start_background(self, *, timeout: float) -> None: ...

    def stop(self, *, join_timeout: float) -> None: ...

    def schedule(self, coro: Coroutine[Any, Any, object]) -> concurrent.futures.Future[object]: ...

    async def send(
        self, to: str, message: dict[str, str], opts: dict[str, str] | None = None
    ) -> object: ...

    def get_bot_identity(self) -> object: ...

    async def get_chat_members(
        self, chat_id: str, *, page_size: int, max_pages: int, id_type: str, force: bool
    ) -> list[object]: ...


def lark_channel(config: FeishuConfig) -> FeishuChannel:
    """按单次文本发送装配 SDK：不重试、不分段、只用 raw 入站，关闭合并转发、卡片与媒体的附加拉取。

    SDK 自带的消息管线照常运行但没有消费者；策略设为 disabled，使其尽早丢弃。
    ``resolve_sender_names=False`` 只关闭事后的姓名回填：锁版 SDK 在规范化（早于 policy）时仍会
    为每条非重复消息的发送者请求通讯录接口，只有构造参数 ``name_lookup`` 能关闭：这里传入不做
    I/O 的函数，任何入站消息（含未 @、非指定群）都不消耗通讯录配额或权限。
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
        ),
        name_lookup=_no_name_lookup,
    )


def _no_name_lookup(open_ids: list[str]) -> dict[str, Any]:
    return {}


class LarkTransport:
    """``FeishuChannel`` 与应用事件循环之间的桥：有界转交 raw 事件、单次限时发送、有界关闭。

    SDK 在自己的线程上调用 raw 处理器。在途事件（已转交、应用侧尚未处理完）最多 ``queue_size``
    个，超出时在持久化前丢弃并记 ``intake_full``，不创建协程。关闭调用 SDK 公开的同步 ``stop``：
    它在独立的守护线程中运行，应用循环最多等待 ``stop_timeout_seconds``，超时返回 False。
    """

    def __init__(self, channel: LarkChannel, config: FeishuConfig) -> None:
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
        try:
            await self._channel.start_background(timeout=self._config.connect_timeout_seconds)
        except BaseException:
            self._accepting = False  # 部分启动失败后不再转交回调
            raise

    def _release(self, future: concurrent.futures.Future[None] | None) -> None:
        with self._lock:
            self._in_flight -= 1
        if future is not None:
            _report(future)

    async def send(self, chat_id: str, text: str, *, reply_to: str | None = None) -> SendOutcome:
        """单次发送；``reply_to`` 给出时只回复该原消息，原消息不可回复时明确失败、不改发新消息。"""
        opts = {"receive_id_type": "chat_id"}
        if reply_to is not None:
            opts |= {"reply_to": reply_to, "reply_target_gone": "fail"}
        future = self._channel.schedule(
            self._channel.send(chat_id, {"text": mention_safe(text)}, opts)
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

    def bot_open_id(self) -> str | None:
        """SDK 已解析且属于本应用的机器人 ``open_id``；未解析、出错或应用不符时为 None。"""
        try:
            identity = self._channel.get_bot_identity()
        except Exception:
            return None
        open_id = getattr(identity, "open_id", None)
        if getattr(identity, "app_id", None) != self._config.app_id or not isinstance(open_id, str):
            return None
        return open_id if _OPEN_ID.fullmatch(open_id) else None

    async def is_member(self, chat_id: str, open_id: str) -> bool:
        """当前成员资格：SDK ``get_chat_members(force=True)`` 绕过缓存，按配置的页数与期限查询。

        找到发送者才返回 True（部分名单中找到也是本次成员证据）；名单中没有返回 False，只表示无法
        确认（群规模超过上界时的部分名单不证明其不在群）；查询失败或超时原样抛出（授权方按拒绝
        处理）。超时会取消 SDK 循环上的查询。
        """
        group = self._config.group
        if group is None or chat_id != group.chat_id:
            return False
        future = self._channel.schedule(
            self._channel.get_chat_members(
                chat_id,
                page_size=group.member_page_size,
                max_pages=group.member_max_pages,
                id_type="open_id",
                force=True,
            )
        )
        try:
            async with asyncio.timeout(group.member_timeout_seconds):
                members = await asyncio.wrap_future(future)
        except BaseException:
            future.cancel()
            raise
        if not isinstance(members, list):
            return False
        return any(
            getattr(member, "id", None) == open_id and getattr(member, "id_type", None) == "open_id"
            for member in members
        )

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
