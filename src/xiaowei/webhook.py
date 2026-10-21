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
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei.config import is_secret_ref, resolve_secret_ref
from xiaowei.evidence import RecipientRejectedError
from xiaowei.governance import GovernedTools, Projection, ToolPolicy
from xiaowei.models import (
    AUDIENCES,
    MAX_ANSWER_BLOCKS,
    MAX_ANSWER_TEXT_BYTES,
    AgentAnswer,
    Audience,
    FactBlock,
    Identity,
    RunContext,
    TextBlock,
    ToolContract,
    ToolObservation,
    ToolRequest,
    TurnAnswer,
)
from xiaowei.storage import InstanceLock
from xiaowei.tools import governed_function_tool

TOOL = "notifications/send_webhook_message"
TARGET = "notifications"
MAX_BODY_BYTES = 20_000
UNKNOWN_NOTICE = "上一条结果未知，可能已经发过，重发可能重复。"
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

    async def recover(self) -> None:
        async with self._engine.begin() as conn:
            await self._lock.admits(conn)
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
                content = delivery.content if delivery.context_tools - {TOOL} else args.text
                content = content.replace("\r\n", "\n").replace("\r", "\n")
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
            await self._lock.admits(conn)
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
                raise RuntimeError("通知请求归属不符")
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
            unknown = (
                await conn.execute(
                    sql(
                        "SELECT max(n.sequence) FROM xiaowei_notification_attempt n "
                        "JOIN xiaowei_request r ON r.turn_id=n.turn_id "
                        "WHERE n.bot_id=:bot AND n.state IN ('unknown','sending') "
                        "AND r.subject_id=:subject AND r.owner_id=:owner"
                    ),
                    params,
                )
            ).scalar_one()
            warning_seen = True
            if unknown is not None:
                warning_seen = bool(
                    (
                        await conn.execute(
                            sql(
                                "SELECT 1 FROM xiaowei_notification_attempt n "
                                "JOIN xiaowei_request r ON r.turn_id=n.turn_id "
                                "WHERE n.bot_id=:bot AND n.sequence>=:sequence "
                                "AND r.subject_id=:subject "
                                "AND r.owner_id=:owner AND r.delivery='sent' "
                                "AND (n.state='unknown' OR n.reason='duplicate_warning') LIMIT 1"
                            ),
                            {**params, "sequence": unknown},
                        )
                    ).first()
                )
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


async def receipt_notice(engine: AsyncEngine, identity: Identity) -> str | None:
    """仅在渠道已复核请求归属/当前通知权限后读取；内容不依赖模型回答或 Evidence 落库。"""
    try:
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sql(
                            "SELECT bot_alias,state,reason FROM xiaowei_notification_attempt "
                            "WHERE turn_id=:turn ORDER BY sequence"
                        ),
                        {"turn": identity.turn_id},
                    )
                )
                .mappings()
                .all()
            )
    except (OSError, SQLAlchemyError):
        return "通知发送状态暂无法核验，不能据此判断已发送或未发送；不要自动重发。"
    lines = []
    for row in rows:
        state = row["state"]
        if state == "accepted":
            result = "消息已提交，Webhook 接口返回成功（accepted），不表示成员已阅读。"
        elif state in {"unknown", "sending"}:
            result = UNKNOWN_NOTICE + "未自动重发（unknown）。"
        elif row["reason"] == "duplicate_warning":
            result = UNKNOWN_NOTICE + "本轮未发送；仍需重发时请明确提出。"
        else:
            result = "Webhook 接口明确失败（failed），未自动重发。"
        lines.append(f"{row['bot_alias']}：{result}")
    return "\n".join(lines) or None
