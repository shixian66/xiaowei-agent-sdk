"""本机同源 Web 入口：一个静态页面与四个 JSON API，业务全部经共享 ``ChannelService``。

- 身份：固定操作者（``WebConfig.operator_id``）；会话语境是随机、``HttpOnly``、``SameSite=Strict``
  的 cookie，只由页面换发，数据库只存其带密钥摘要；API 写操作缺少 cookie 时拒绝，不在失败响应里
  换发。客户端不能提交 session、subject 或 target。
- 同源：每个请求的 Host 必须与允许地址逐字相同（不做 DNS 解析或别名归一），非 GET 请求还必须带
  同一来源的 Origin 与 JSON 正文；不开放 CORS。
- 正文：先核对 ``Content-Length``，超限在读取前拒绝；读取时按块累计，伪造长度、缺少长度或分块
  传输在越过上限时停止，不缓冲完整正文。
- 显示：API 只返回 JSON；页面脚本只用 ``textContent`` 与 DOM API，表格只由受信 ``DeliveryFact``
  生成，CSP 禁止内联脚本。

POST 在本进程内等待本轮完成；客户端断开不取消处理，结果由同一 cookie 用 GET 读取。
"""

import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from starlette.exceptions import HTTPException

from xiaowei.app import Mode
from xiaowei.channel import (
    AccessDeniedError,
    ChannelService,
    InboundRequest,
    RequestRef,
    RequestView,
    ResultUnavailableError,
    ResultUnverifiableError,
)
from xiaowei.channel_store import (
    ChannelStoreError,
    ChannelStoreUnavailableError,
    NotReadyError,
    RequestConflictError,
    RequestUnavailableError,
    ResultNotSavedError,
    SessionBusyError,
)
from xiaowei.config import WebConfig
from xiaowei.storage import Readiness

COOKIE = "xiaowei_web"
_COOKIE_BYTES = 32
_COOKIE_VALUE = re.compile(r"[A-Za-z0-9_-]{43}")
_REQUEST_ID = r"^[A-Za-z0-9_-]{1,64}$"
_CONTENT_LENGTH = re.compile(rb"[0-9]{1,16}")
_SAFE_METHODS = frozenset({"GET", "HEAD"})
_STATIC = Path(__file__).with_name("static")
_ASSETS = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
_PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_COMMON_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
)
_STATUS: tuple[tuple[type[ChannelStoreError], int], ...] = (
    (AccessDeniedError, 403),
    (ResultUnverifiableError, 503),  # 子类在前：暂时无法复核，稍后可再读
    (ResultUnavailableError, 403),
    (RequestUnavailableError, 404),
    (RequestConflictError, 409),
    (SessionBusyError, 409),
    (NotReadyError, 503),
    (ChannelStoreUnavailableError, 503),
)
_INVALID_BODY = "请求格式不符合要求"

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class TurnSubmission(_Body):
    """浏览器只能提交的三项：客户端请求编号、显式用途与消息正文。"""

    request_id: Annotated[str, StringConstraints(pattern=_REQUEST_ID)]
    mode: Mode
    message: str = Field(min_length=1)


class NewSession(_Body):
    """新建会话不接受任何参数。"""


_B = TypeVar("_B", bound=_Body)


class _BodyTooLargeError(HTTPException):
    def __init__(self) -> None:
        super().__init__(413, "请求正文过大")


class _Guard:
    """Host/Origin/JSON 与正文上限检查；在路由与正文解析之前执行。"""

    def __init__(self, app: ASGIApp, *, origins: frozenset[str], max_body_bytes: int) -> None:
        self._app = app
        # Host 头的允许值就是允许地址的 netloc；同源要求 Origin 的 netloc 与本次 Host 相同。
        self._origin_for_host = {origin.split("://", 1)[1]: origin for origin in origins}
        self._limit = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        rejection = self._reject(scope)
        if rejection is not None:
            status, message = rejection
            await _error(status, message)(scope, receive, _secured(send))
            return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._limit:
                    raise _BodyTooLargeError
            return message

        await self._app(scope, limited, _secured(send))

    def _reject(self, scope: Scope) -> tuple[int, str] | None:
        headers: list[tuple[bytes, bytes]] = scope["headers"]
        hosts = [value for name, value in headers if name == b"host"]
        host = hosts[0].decode("latin-1") if len(hosts) == 1 else None
        if host is None or host not in self._origin_for_host:
            return 400, "Host 不被允许"
        origins = [value.decode("latin-1") for name, value in headers if name == b"origin"]
        same_origin = origins == [self._origin_for_host[host]]
        if scope["method"] in _SAFE_METHODS:
            if origins and not same_origin:
                return 403, "只接受同源请求"
            return None
        if not same_origin:
            return 403, "只接受同源请求"
        types = [value for name, value in headers if name == b"content-type"]
        if len(types) != 1 or types[0].split(b";", 1)[0].strip().lower() != b"application/json":
            return 415, "请求正文必须是 JSON"
        lengths = [value for name, value in headers if name == b"content-length"]
        if len(lengths) > 1 or (lengths and _CONTENT_LENGTH.fullmatch(lengths[0]) is None):
            return 400, "Content-Length 无效"
        if lengths and int(lengths[0]) > self._limit:
            return 413, "请求正文过大"
        return None


def _secured(send: Send) -> Send:
    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            message["headers"] = [*message.get("headers", ()), *_COMMON_HEADERS]
        await send(message)

    return wrapped


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _view(request_id: str, view: RequestView, readiness: Readiness) -> JSONResponse:
    unfinished = view.state in ("accepted", "running")
    if unfinished and not readiness.ok:
        # 本进程已不能再写这条请求的终态：不以“处理中”继续服务，重启恢复后才有确定结果。
        raise ChannelStoreUnavailableError("服务需要重启，本请求的结果在重启后确认")
    delivery = None
    if view.delivery is not None:
        delivery = view.delivery.model_dump(mode="json", exclude={"channel"})
    status = 202 if unfinished else 200
    body = {"request_id": request_id, "state": view.state, "delivery": delivery}
    return JSONResponse(body, status_code=status)


def _conversation(request: Request) -> str | None:
    """cookie 中的会话语境；缺少或格式不符时为 None。只有页面会换发 cookie。"""
    value = request.cookies.get(COOKIE)
    if value is not None and _COOKIE_VALUE.fullmatch(value):
        return value
    return None


def _required(request: Request) -> str:
    """API 写操作必须带页面换发的 cookie：先建立会话语境，失败响应也不会丢失它。"""
    conversation = _conversation(request)
    if conversation is None:
        raise HTTPException(403, "缺少会话 cookie，请刷新页面")
    return conversation


async def _parse(model: type[_B], request: Request) -> _B:
    try:
        return model.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(400, _INVALID_BODY) from None


def create_web_app(
    service: ChannelService,
    config: WebConfig,
    *,
    components: Callable[[], Mapping[str, str]] | None = None,
) -> FastAPI:
    """装配 Web 入口；``service`` 已在启动时核验唯一授权来源与共享 ``EvidenceStore``。

    ``components`` 给出可选能力（如飞书）的安全状态，只出现在 ``/readyz``，不影响 Web 自身是否就绪。
    """
    page = (_STATIC / "index.html").read_bytes()
    assets = {name: (_STATIC / name).read_bytes() for name in _ASSETS}
    readiness = service.results.store.readiness
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_Guard, origins=config.allowed_origins, max_body_bytes=config.max_body_bytes)

    def ref(conversation: str, request_id: str) -> RequestRef:
        return RequestRef(
            channel="web",
            subject_id=config.operator_id,
            conversation_id=conversation,
            request_id=request_id,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return _error(exc.status_code, str(exc.detail))

    @app.exception_handler(ChannelStoreError)
    async def channel_error(request: Request, exc: ChannelStoreError) -> JSONResponse:
        status = next((code for kind, code in _STATUS if isinstance(exc, kind)), 500)
        return _error(status, str(exc) if status != 500 else "请求处理失败")

    @app.get("/")
    async def index(request: Request) -> Response:
        response = Response(page, media_type="text/html; charset=utf-8")
        response.headers["content-security-policy"] = _PAGE_CSP
        if _conversation(request) is None:
            response.set_cookie(
                COOKIE,
                secrets.token_urlsafe(_COOKIE_BYTES),
                httponly=True,
                samesite="strict",
                secure=config.secure_cookie,
                path="/",
            )
        return response

    @app.get("/assets/{name}")
    async def asset(name: str) -> Response:
        if name not in _ASSETS:
            raise HTTPException(404, "不存在")
        return Response(assets[name], media_type=_ASSETS[name])

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "alive"})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        extra = dict(components()) if components is not None else {}
        if not readiness.ok:
            return JSONResponse({**extra, "status": "not_ready"}, status_code=503)
        return JSONResponse({**extra, "status": "ready"})

    @app.post("/api/turns")
    async def submit(request: Request) -> Response:
        conversation = _required(request)
        submission = await _parse(TurnSubmission, request)
        receipt = await service.accept(
            InboundRequest(
                channel="web",
                channel_request_id=submission.request_id,
                subject_id=config.operator_id,
                conversation_id=conversation,
                mode=submission.mode,
                message=submission.message,
                received_at=datetime.now(UTC),
            )
        )
        if receipt.created:
            try:
                await service.process(receipt)
            except ResultNotSavedError:
                pass  # 请求已记为 failed/result_not_saved，下方读取得到固定回执
        # 只有本次提交刚运行完的请求是首次交付；重复提交与之后的读取都是历史读取。
        view = await service.results.view(
            ref(conversation, submission.request_id), first=receipt.created
        )
        return _view(submission.request_id, view, readiness)

    @app.get("/api/turns/{request_id}")
    async def read(request_id: str, request: Request) -> Response:
        conversation = _conversation(request)
        if conversation is None or re.fullmatch(_REQUEST_ID, request_id) is None:
            raise RequestUnavailableError
        view = await service.results.view(ref(conversation, request_id))
        return _view(request_id, view, readiness)

    @app.post("/api/sessions")
    async def new_session(request: Request) -> Response:
        conversation = _required(request)
        await _parse(NewSession, request)
        await service.new_session("web", config.operator_id, conversation)
        return JSONResponse({"status": "created"})

    return app
