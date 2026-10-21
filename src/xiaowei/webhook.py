"""指定群的受治理纯文本通知；凭据只留在 HTTP 适配器，发送机会先于 I/O 落库。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx2
from agents import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import text as sql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from xiaowei.config import is_secret_ref, resolve_secret_ref
from xiaowei.evidence import RecipientRejectedError
from xiaowei.feishu_render import display_text, fact_limit_notices, mention_safe
from xiaowei.governance import GovernedTools, Projection, ToolPolicy
from xiaowei.models import (
    AUDIENCES,
    MAX_ANSWER_BLOCKS,
    MAX_ANSWER_TEXT_BYTES,
    AgentAnswer,
    Audience,
    Delivery,
    DeliveryFact,
    DeliveryText,
    FactBlock,
    Identity,
    RunContext,
    TextBlock,
    ToolContract,
    ToolObservation,
    ToolRequest,
    TurnAnswer,
)
from xiaowei.storage import InstanceLock, StorageUnavailableError
from xiaowei.tools import governed_function_tool

TOOL = "notifications/send_webhook_message"
TARGET = "notifications"
MAX_BODY_BYTES = 20_000
UNKNOWN_NOTICE = "上一条结果未知，可能已经发过，重发可能重复。"
APPROVAL_NOTICE = "批准后不会自动通知，完成后请另发通知要求。"
REMINDER_APPROVAL_NOTICE = "本轮不立即发送；确认后按计划到期发送一次，不需要另发即时通知要求。"
_URL = re.compile(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]+\Z")


class WebhookBotConfig(BaseModel):
    """可信管理员登记；同群凭据轮换不改 bot_id/target_version，换接收目标须改版本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    bot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    alias: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=200)
    target_version: str = Field(min_length=1, max_length=64)
    url_ref: str
    signing_secret_ref: str | None
    receive_tools: frozenset[str]
    receive_targets: frozenset[str]

    @field_validator("url_ref", "signing_secret_ref")
    @classmethod
    def _ref(cls, value: str | None) -> str | None:
        if value is not None and not is_secret_ref(value):
            raise ValueError("Webhook 凭据必须使用 env:NAME 安全引用")
        return value


class SendArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    bot_alias: str
    text: str
    evidence_ids: list[str]


def catalog(
    bots: Sequence[WebhookBotConfig], limits: Mapping[Audience, int]
) -> tuple[tuple[ToolContract, ...], tuple[ToolPolicy, ...]]:
    if not bots:
        return (), ()
    description = (
        "仅在用户明确要求发送时，向已登记机器人提交一次纯文本消息；bot_alias 只选下列别名，"
        "不接受 URL。text 是明确文字或模型说明；转发业务事实时 evidence_ids 选实际可见证据，"
        "代码生成事实并独立校验接收权限，不能把原始结果抄进 text 绕过。"
        "未引用的全部可见证据同样受接收许可约束。"
        "recipient_not_authorized 表示含机器人不能接收的可见历史；换字不能消除依赖。"
        "可请用户显式 /新建 后重新完整提出纯文字要求，但须说明会切换全群共享上下文；不能自动新建。"
        "text 最多16000 UTF-8字节，复用现有回答容量；完整请求最多20000字节，"
        "超限为 not_sent，请缩短说明、减少事实或重新取得有界摘要，不静默截断。"
        "accepted 仅表示接口接受，failed 为明确失败，unknown 可能已发送，均不可自动重试；"
        "unknown 后仅用户新一轮明确要求重发且已获重复风险提示才可再次发送。"
        "not_sent 表示未发送；修正未知别名或内容容量后须仍属于用户要求，不能换字重试 unknown。"
        "可用机器人：" + "；".join(f"{b.alias}（{b.description}）" for b in bots)
    )
    fields = ("receipt_id", "bot_alias", "status", "reason")
    policy = ToolPolicy(
        policy_id="notification.feishu_text",
        arguments=SendArgs,
        projections={a: Projection(fields, limits[a]) for a in AUDIENCES},
        required=fields,
        effect="notify",
        group_only=True,
    )
    return (
        ToolContract(
            tool_id=TOOL,
            target_id=TARGET,
            input_schema=SendArgs.model_json_schema(),
            policy_id=policy.policy_id,
            description=description,
        ),
    ), (policy,)


@dataclass(frozen=True)
class _Bot:
    config: WebhookBotConfig
    url: SecretStr
    signing: SecretStr | None


def resolve_bot(config: WebhookBotConfig) -> _Bot:
    url = resolve_secret_ref(config.url_ref)
    if not _URL.fullmatch(url.get_secret_value()):
        raise ValueError("Webhook 地址须为飞书官方 HTTPS 自定义机器人端点，不能含查询或跳转")
    signing = resolve_secret_ref(config.signing_secret_ref) if config.signing_secret_ref else None
    return _Bot(config, url, signing)


class _PrivateTransport(httpx2.AsyncBaseTransport):
    """沿现有模型 transport 模式，让 AsyncClient 日志只看到安全的逻辑地址。"""

    def __init__(self, bots: Sequence[_Bot], inner: httpx2.AsyncBaseTransport) -> None:
        self._bots = {b.config.bot_id: b for b in bots}
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        bot = self._bots[request.url.path.lstrip("/")]
        forwarded = httpx2.Request(
            "POST",
            bot.url.get_secret_value(),
            headers={"Content-Type": "application/json"},
            content=await request.aread(),
        )
        return await self._inner.handle_async_request(forwarded)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _body(bot: _Bot, content: str, clock: Callable[[], datetime]) -> bytes:
    payload: dict[str, object] = {"msg_type": "text", "content": {"text": content}}
    if bot.signing is not None:
        stamp = str(int(clock().timestamp()))
        key = f"{stamp}\n{bot.signing.get_secret_value()}".encode()
        payload.update(
            timestamp=stamp, sign=base64.b64encode(hmac.digest(key, b"", "sha256")).decode("ascii")
        )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _message_text(content: str) -> str:
    """预估容量、发送和摘要使用同一份最终纯文本规范。"""
    return mention_safe(content.replace("\r\n", "\n").replace("\r", "\n"))


def _notification_text(delivery: Delivery, text: str) -> str:
    """只渲染已校验的飞书事实；不把 Web 的转义 content 当通知正文。"""
    if not delivery.facts:
        return text

    def fact_lines(fact: DeliveryFact) -> list[str]:
        lines = [
            "工具结果（系统根据证据生成）",
            f"来源 {fact.tool_id} · 目标 {fact.target_id} · 采集于 {fact.captured_at.isoformat()}",
            *([fact.note] if fact.note else []),
            *fact_limit_notices(fact),
        ]
        for key, value in fact.metadata.items():
            if key == "approval_command":  # 目的群不能批准原群动作，不转发无效命令。
                continue
            shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            lines.append(f"{key}: {shown}")
        for index, row in enumerate(fact.rows, 1):
            lines.append(f"第 {index} 行")
            for column in fact.columns:
                value = row.get(column)
                shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                lines.append(f"{column}: {shown}" if column in row else f"{column}: （未提供）")
        return [display_text(line) for line in lines]

    facts = {f.evidence_id: f for f in delivery.facts}
    lines = list(delivery.notices)
    for fact in delivery.section("action"):
        lines.extend(fact_lines(fact))
    for block in delivery.blocks:
        if isinstance(block, DeliveryText):
            lines.extend(("模型说明（未经系统核实）", block.text))
        else:
            lines.extend(fact_lines(facts[block.evidence_id]))
    return "\n".join(lines)


class WebhookNotifications:
    """应用依赖；每轮闭包绑定可信 context 和实时回放，不放进 RunContext。"""

    def __init__(
        self,
        bots: Sequence[WebhookBotConfig],
        governance: GovernedTools,
        engine: AsyncEngine,
        lock: InstanceLock,
        *,
        clock: Callable[[], datetime],
        verify_sender: Callable[[Identity], Awaitable[bool]],
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        resolved = [resolve_bot(b) for b in bots]
        self._bots = {b.config.alias: b for b in resolved}
        self._governance, self._engine, self._lock, self._clock = governance, engine, lock, clock
        self._verify_sender = verify_sender
        self._locks = {b.config.bot_id: asyncio.Lock() for b in resolved}
        self._client = httpx2.AsyncClient(
            transport=_PrivateTransport(
                resolved, transport or httpx2.AsyncHTTPTransport(retries=0, trust_env=False)
            ),
            timeout=10,
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def bot_config(self, alias: str) -> WebhookBotConfig | None:
        bot = self._bots.get(alias)
        return bot.config if bot is not None else None

    def fixed_message_fits(self, alias: str, content: str) -> bool:
        bot = self._bots.get(alias)
        return (
            bot is not None
            and len(_body(bot, _message_text(content), self._clock)) <= MAX_BODY_BYTES
        )

    async def send_fixed(
        self,
        ctx: RunContext,
        alias: str,
        content: str,
        claim: Callable[[str], Awaitable[tuple[str, str, str, bool]]],
        settle: Callable[[str, str, str], Awaitable[None]],
    ) -> None:
        """可信一次性任务交付：同一 notify 治理/HTTP，发送占用由任务事务负责。

        不注册为模型工具；模型不能提供 claim/settle，也不能跳过获批定义检查。
        """
        bot = self._bots.get(alias)
        if bot is None:
            raise ValueError("机器人未配置")
        request = ToolRequest(
            tool_id=TOOL,
            target_id=TARGET,
            call_id=ctx.identity.turn_id,
            tool_name="fixed-reminder",
            arguments={"bot_alias": alias, "text": content, "evidence_ids": []},
        )

        async def execute(_request: ToolRequest) -> ToolObservation:
            async with self._locks[bot.config.bot_id]:
                if not await self._verify_sender(ctx.identity):
                    raise ValueError("当前发送主体不可核验")
                normalized = _message_text(content)
                body = _body(bot, normalized, self._clock)
                if len(body) > MAX_BODY_BYTES:
                    raise ValueError("完整通知超过20000字节")
                receipt, status, reason, claimed = await claim(normalized)
                if claimed:
                    status, reason = await self._post(bot, body)
                    await settle(receipt, status, reason)
                return self._observation(alias, status, reason, receipt)

        await self._governance.invoke(ctx, request, execute)

    async def warning_seen(self, conn: AsyncConnection, owner: str, bot_id: str) -> bool:
        """原群＋稳定机器人共用未知结果门槛，定时/即时占用同一序列。"""
        params = {"owner": owner, "bot": bot_id}
        unknown = await conn.scalar(
            sql(
                "SELECT max(n.sequence) FROM xiaowei_notification_attempt n "
                "LEFT JOIN xiaowei_request r ON r.turn_id=n.turn_id "
                "LEFT JOIN xiaowei_scheduled_job j ON j.job_id=n.job_id "
                "WHERE n.bot_id=:bot AND n.state IN ('unknown','sending') "
                "AND COALESCE(r.owner_id,j.owner_id)=:owner"
            ),
            params,
        )
        if unknown is None:
            return True
        return bool(
            (
                await conn.execute(
                    sql(
                        "SELECT 1 FROM xiaowei_notification_attempt n "
                        "JOIN xiaowei_request r ON r.turn_id=n.turn_id "
                        "WHERE n.bot_id=:bot "
                        "AND r.owner_id=:owner AND r.delivery='sent' "
                        "AND ((n.sequence>=:sequence AND (n.state='"
                        "unknown' OR n.reason='duplicate_warning')) "
                        "OR (n.reason='task_risk_warning' AND "
                        "n.warning_sequence=:sequence)) LIMIT 1"
                    ),
                    {**params, "sequence": unknown},
                )
            ).first()
        )

    async def record_task_warning(self, identity: Identity, receipt: str) -> None:
        """只记录已授权任务状态查询应交付的风险提示；不占用发送机会、不发 HTTP。"""
        async with self._engine.begin() as conn:
            if not self._lock.readiness.ok or not await self._lock.admits(conn):
                raise StorageUnavailableError("实例未就绪，无法记录任务回执提示")
            await conn.execute(
                sql(
                    "INSERT INTO xiaowei_notification_attempt "
                    "(receipt_id,turn_id,bot_id,bot_alias,target_versi"
                    "on,content_digest,state,reason,warning_sequence) "
                    "SELECT :id,r.turn_id,n.bot_id,n.bot_alias,n.target_version,:digest,'not_sent',"
                    "'task_risk_warning',n.sequence FROM xiaowei_notification_attempt n "
                    "JOIN xiaowei_scheduled_job j ON j.job_id=n.job_id "
                    "JOIN xiaowei_request r ON r.turn_id=:turn "
                    "WHERE n.receipt_id=:receipt AND n.state IN ('unknown','sending') "
                    "AND j.owner_id=:owner AND r.session_id=:session AND r.subject_id=:subject "
                    "AND r.owner_kind='group' AND r.owner_id=:owner AND r.channel='feishu' "
                    "AND r.state='running' ON CONFLICT DO NOTHING"
                ),
                dict(
                    id=uuid.uuid4().hex,
                    turn=identity.turn_id,
                    session=identity.session_id,
                    subject=identity.subject_id,
                    owner=identity.owner.id,
                    receipt=receipt,
                    digest=hashlib.sha256(("task-risk:" + receipt).encode()).hexdigest(),
                ),
            )

    async def recover(self) -> None:
        async with self._engine.begin() as conn:
            if not self._lock.readiness.ok or not await self._lock.admits(conn):
                raise StorageUnavailableError("实例锁已失效，未执行通知恢复")
            await conn.execute(
                sql("UPDATE xiaowei_notification_attempt SET state='unknown' WHERE state='sending'")
            )

    def tool(self, ctx: RunContext, replayed: Callable[[], Sequence[str]]) -> FunctionTool:
        contract = self._governance.catalog.contract(TOOL, TARGET)
        assert contract is not None  # noqa: S101 - 装配时已登记

        async def execute(request: ToolRequest) -> ToolObservation:
            args = SendArgs.model_validate(request.arguments)
            bot = self._bots.get(args.bot_alias)
            if bot is None:
                return self._observation(args.bot_alias, "not_sent", "target_not_allowed")
            async with self._locks[bot.config.bot_id]:
                if not await self._verify_sender(ctx.identity):
                    return self._observation(args.bot_alias, "not_sent", "sender_not_authorized")
                visible = tuple(
                    dict.fromkeys(
                        (
                            *replayed(),
                            *(
                                e
                                for t, e in self._governance.turn_runs(ctx.identity).produced
                                if t in ctx.tool_scope
                            ),
                        )
                    )
                )
                if not set(args.evidence_ids) <= set(visible):
                    return self._observation(args.bot_alias, "not_sent", "evidence_not_visible")
                if not args.text.strip() and not args.evidence_ids:
                    return self._observation(args.bot_alias, "not_sent", "empty_content")
                if len(args.text.encode()) > MAX_ANSWER_TEXT_BYTES or (
                    len(set(args.evidence_ids)) + bool(args.text.strip()) > MAX_ANSWER_BLOCKS
                ):
                    return self._observation(args.bot_alias, "not_sent", "content_too_large")
                # 即使纯文字，也复核模型看过但没有引用的全部证据及目的地许可。
                blocks: list[TextBlock | FactBlock] = []
                if args.text.strip():
                    blocks.append(TextBlock(type="text", text=args.text))
                blocks.extend(
                    FactBlock(type="fact", evidence_id=e, view="full")
                    for e in dict.fromkeys(args.evidence_ids)
                )
                turn = TurnAnswer(
                    answer=AgentAnswer(
                        blocks=tuple(blocks),
                        report_format=None,
                        awaiting_user=False,
                    ),
                    context_evidence=visible,
                    material_only=True,
                )
                try:
                    delivery = await self._governance.evidence.validate_answer(
                        turn,
                        ctx,
                        recipient_tools=bot.config.receive_tools | {TOOL},
                        recipient_targets=bot.config.receive_targets | {TARGET},
                        include_remaining_results=False,
                    )
                except RecipientRejectedError:
                    return self._observation(args.bot_alias, "not_sent", "recipient_not_authorized")
                content = _notification_text(delivery, args.text)
                # 纯文本传输边界统一中和 @ 标签，再构造请求并计算防重摘要。
                content = _message_text(content)
                body = _body(bot, content, self._clock)
                if len(body) > MAX_BODY_BYTES:
                    return self._observation(args.bot_alias, "not_sent", "content_too_large")
                attempt, claimed = await self._claim(ctx.identity, bot, content)
                if not claimed:
                    return self._observation(
                        args.bot_alias, attempt["state"], attempt["reason"], attempt["receipt_id"]
                    )
                status, reason = await self._post(bot, body)
                try:
                    async with self._engine.begin() as conn:
                        result = await conn.execute(
                            sql(
                                "UPDATE xiaowei_notification_attempt "
                                "SET state=:state,reason=:reason "
                                "WHERE receipt_id=:id AND state='sending'"
                            ),
                            {"state": status, "reason": reason, "id": attempt["receipt_id"]},
                        )
                        if result.rowcount != 1:
                            raise RuntimeError("发送状态无法落定")
                except BaseException:
                    self._lock.readiness.lock("notification_settle_failed")
                    raise
                return self._observation(args.bot_alias, status, reason, attempt["receipt_id"])

        return governed_function_tool("send_webhook_message", contract, self._governance, execute)

    def _observation(
        self, alias: str, status: str, reason: str, receipt_id: str = "none"
    ) -> ToolObservation:
        return ToolObservation(
            payload={
                "receipt_id": receipt_id,
                "bot_alias": alias,
                "status": status,
                "reason": reason,
            },
            captured_at=self._clock(),
            truncated=False,
        )

    async def _claim(
        self, identity: Identity, bot: _Bot, content: str
    ) -> tuple[dict[str, Any], bool]:
        params = {
            "turn": identity.turn_id,
            "owner": identity.owner.id,
            "session": identity.session_id,
            "subject": identity.subject_id,
            "bot": bot.config.bot_id,
            "version": bot.config.target_version,
            "alias": bot.config.alias,
            "digest": hashlib.sha256(content.encode()).hexdigest(),
            "id": uuid.uuid4().hex,
        }
        async with self._engine.begin() as conn:
            if not self._lock.readiness.ok or not await self._lock.admits(conn):
                raise StorageUnavailableError("实例锁已失效，未占用发送机会")
            # 请求归属来自已运行的服务端请求，不接受模型伪造 turn/session。
            request = (
                await conn.execute(
                    sql(
                        "SELECT turn_id FROM xiaowei_request "
                        "WHERE turn_id=:turn AND session_id=:session "
                        "AND subject_id=:subject AND owner_kind='group' AND owner_id=:owner "
                        "AND channel='feishu' AND state='running' FOR UPDATE"
                    ),
                    params,
                )
            ).first()
            if request is None:
                return {
                    "state": "not_sent",
                    "reason": "request_not_active",
                    "receipt_id": "none",
                }, False
            old = (
                (
                    await conn.execute(
                        sql(
                            "SELECT * FROM xiaowei_notification_attempt "
                            "WHERE turn_id=:turn AND bot_id=:bot "
                            "ORDER BY sequence DESC"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            for row in old:
                if row["content_digest"] == params["digest"]:
                    result = dict(row)
                    if result["state"] == "sending":
                        result.update(state="unknown", reason="response_unknown")
                    return result, False
            if any(r["state"] != "accepted" for r in old):
                return {"state": "not_sent", "reason": "turn_stopped", "receipt_id": "none"}, False
            # 新轮次的主动重发：上次 unknown 风险须已由原渠道回执展示。
            warning_seen = await self.warning_seen(conn, params["owner"], params["bot"])
            params.update(
                state="sending" if warning_seen else "not_sent",
                reason="pending" if warning_seen else "duplicate_warning",
            )
            row = (
                (
                    await conn.execute(
                        sql(
                            "INSERT INTO xiaowei_notification_attempt "
                            "(receipt_id,turn_id,bot_id,bot_alias,target_version,"
                            "content_digest,state,reason) "
                            "VALUES (:id,:turn,:bot,:alias,:version,:digest,:state,:reason) "
                            "RETURNING *"
                        ),
                        params,
                    )
                )
                .mappings()
                .one()
            )
            return dict(row), warning_seen

    async def _post(self, bot: _Bot, body: bytes) -> tuple[str, str]:
        try:
            async with asyncio.timeout(10):
                async with self._client.stream(
                    "POST", f"https://open.feishu.cn/{bot.config.bot_id}", content=body
                ) as response:
                    if 400 <= response.status_code < 500:
                        return "failed", "http_rejected"
                    if 300 <= response.status_code < 400:
                        return "failed", "redirect_rejected"
                    if not 200 <= response.status_code < 300:
                        return "unknown", "http_unconfirmed"
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > 4096:
                            return "unknown", "invalid_response"
                        data.extend(chunk)
                    payload = json.loads(data)
                    code = payload.get("code", payload.get("StatusCode"))
                    if type(code) is not int:
                        return "unknown", "invalid_response"
                    return ("accepted", "api_accepted") if code == 0 else ("failed", "api_rejected")
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            return "failed", "connection_failed"
        except (httpx2.HTTPError, TimeoutError, ValueError, AttributeError):
            return "unknown", "response_unknown"


async def receipt_notice(
    engine: AsyncEngine, identity: Identity, *, tasks_only: bool = False, task_admin: bool = False
) -> str | None:
    """仅在渠道已复核请求归属/当前通知权限后读取；内容不依赖模型回答或 Evidence 落库。"""
    try:
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sql(
                            "SELECT n.bot_alias,n.state,n.reason,s.receipt_id AS source_receipt "
                            "FROM xiaowei_notification_attempt n "
                            "LEFT JOIN xiaowei_notification_attemp"
                            "t s ON s.sequence=n.warning_sequence "
                            "LEFT JOIN xiaowei_scheduled_job j ON j.job_id=s.job_id "
                            "WHERE n.turn_id=:turn AND (NOT :tasks_only OR "
                            "(n.reason='task_risk_warning' AND j.owner_id=:owner "
                            "AND (j.creator_id=:subject OR :admin))) ORDER BY n.sequence"
                        ),
                        {
                            "turn": identity.turn_id,
                            "tasks_only": tasks_only,
                            "owner": identity.owner.id,
                            "subject": identity.subject_id,
                            "admin": task_admin,
                        },
                    )
                )
                .mappings()
                .all()
            )
    except (OSError, SQLAlchemyError):
        return (
            "通知发送状态暂无法核验，不能据此判断已发送或未发送；"
            "消息可能已发出，重发可能重复；不要自动重发。"
        )
    lines = []
    for row in rows:
        state = row["state"]
        if row["reason"] == "task_risk_warning":
            result = (
                f"任务发送回执 {row['source_receipt']}："
                + UNKNOWN_NOTICE
                + "未自动重发（unknown）。"
            )
        elif state == "accepted":
            result = "消息已提交，Webhook 接口返回成功（accepted），不表示成员已阅读。"
        elif state in {"unknown", "sending"}:
            result = UNKNOWN_NOTICE + "未自动重发（unknown）。"
        elif row["reason"] == "duplicate_warning":
            result = UNKNOWN_NOTICE + "本轮未发送；仍需重发时请明确提出。"
        else:
            result = "Webhook 接口明确失败（failed），未自动重发。"
        lines.append(f"{row['bot_alias']}：{result}")
    return "\n".join(lines) or None
