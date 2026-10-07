"""P1-B Task 6：本机同源 Web。FastAPI 应用经 ASGI 调用，后面是与 Task 5 相同的真实路径：
ChannelService → Application（真 Runner + HTTP mock 脚本模型）→ 受治理合成工具 → 隔离的真实
PostgreSQL。

ASGI 调用不经过浏览器与 Uvicorn：cookie 策略、CSP 与 DOM 渲染的浏览器行为不能由这里证明。
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.sdk_core.synthetic_tools import TARGET
from tests.sdk_core.test_app import MODEL_SECRET, after, cite, tool_call, upstream_error
from tests.sdk_core.test_channel_service import Env
from tests.sdk_core.test_channel_service import env as env  # pytest fixture

from xiaowei.config import WebConfig
from xiaowei.web import COOKIE, _Guard, create_web_app

pytestmark = pytest.mark.loopback

ORIGIN = "http://127.0.0.1:8501"
OTHER_ALLOWED = "http://localhost:8501"
LIMIT = 4096
STATIC = Path(__file__).resolve().parents[2] / "src" / "xiaowei" / "static"


def config(**overrides: Any) -> WebConfig:
    values: dict[str, Any] = {
        "operator_id": "alice",
        "allowed_origins": frozenset({ORIGIN, OTHER_ALLOWED}),
        "secure_cookie": False,
        "max_body_bytes": LIMIT,
    }
    values.update(overrides)
    return WebConfig(**values)


class Web:
    def __init__(self, env: Env) -> None:
        self.env = env
        self.app = create_web_app(env.service, config())

    @asynccontextmanager
    async def client(self, *, page: bool = True) -> AsyncIterator[httpx.AsyncClient]:
        """同源客户端；默认先打开页面取得 cookie，与浏览器的使用顺序相同。"""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
        ) as client:
            if page:
                assert (await client.get("/")).status_code == 200
            yield client

    @staticmethod
    async def submit(
        client: httpx.AsyncClient, message: str, request_id: str = "r1", mode: str = "query"
    ) -> httpx.Response:
        return await client.post(
            "/api/turns", json={"request_id": request_id, "mode": mode, "message": message}
        )


@pytest.fixture
async def web(env: Env) -> AsyncIterator[Web]:
    yield Web(env)


async def raw(
    app: Any,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]],
    chunks: tuple[bytes, ...] = (b"",),
) -> tuple[int, bytes, int]:
    """直接以 ASGI 调用：可构造重复头、伪造长度与分块正文，并统计 receive 调用次数。"""
    pending = list(chunks)
    calls = 0
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8501),
    }
    await app(scope, receive, send)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], body, calls


def same_origin(*extra: tuple[bytes, bytes]) -> list[tuple[bytes, bytes]]:
    return [
        (b"host", b"127.0.0.1:8501"),
        (b"origin", ORIGIN.encode()),
        (b"content-type", b"application/json"),
        (b"cookie", f"{COOKIE}={'c' * 43}".encode()),
        *extra,
    ]


# ---- 正式入口的成功与失败路径 --------------------------------------------------------------


async def test_page_issues_cookie_and_query_turn_round_trips(web: Web) -> None:
    message = web.env.scripts.add("东区订单？", tool_call("order_total", region="east"), cite())
    async with web.client(page=False) as client:
        page = await client.get("/")
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        cookie = page.headers["set-cookie"]
        assert re.match(rf"{COOKIE}=[A-Za-z0-9_-]{{43}};", cookie)
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" not in cookie
        posted = await web.submit(client, message)
        assert posted.status_code == 200 and posted.headers["content-type"] == "application/json"
        body = posted.json()
        assert (body["request_id"], body["state"]) == ("r1", "completed")
        (fact,) = body["delivery"]["facts"]
        assert fact["tool_id"] == "local/order_total" and fact["target_id"] == TARGET
        assert fact["rows"][0] == {"day": "2026-09-01", "orders": 50}
        assert "工具结果（系统根据证据生成）" in body["delivery"]["content"]
        # 刷新后 GET：重新验证得到同一交付，不再调用模型或工具。
        again = await client.get("/api/turns/r1")
        assert again.status_code == 200 and again.json() == body
    assert web.env.model_calls(message) == 2 and len(web.env.adapter.calls) == 1
    # 身份来自配置的固定操作者，会话来自 cookie。
    record = await web.env.store.get("web", "alice", cookie.split(";")[0].split("=", 1)[1], "r1")
    assert record.state == "completed"


async def test_failed_turn_returns_fixed_receipt_without_upstream_detail(web: Web) -> None:
    message = web.env.scripts.add("上游失败", upstream_error)
    async with web.client() as client:
        posted = await web.submit(client, message, mode="diagnose")
    body = posted.json()
    assert posted.status_code == 200
    assert (body["request_id"], body["state"]) == ("r1", "failed")
    assert body["delivery"]["content"].startswith("模型未能完成本轮")
    assert MODEL_SECRET not in posted.text and body["delivery"]["facts"] == []


@pytest.mark.parametrize("cookie", [None, "short", "x" * 42 + "!"])
async def test_api_writes_require_the_page_cookie(web: Web, cookie: str | None) -> None:
    message = web.env.scripts.add("没有 cookie", tool_call("order_total", region="east"), cite())
    async with web.client(page=False) as client:
        if cookie is not None:
            client.cookies.set(COOKIE, cookie, domain="127.0.0.1")
        posted = await web.submit(client, message)
        renewed = await client.post("/api/sessions", json={})
    for response in (posted, renewed):
        assert response.status_code == 403 and "set-cookie" not in response.headers
    assert await web.env.requests() == 0 and web.env.model_calls(message) == 0


# ---- 请求格式 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "content_type", "status"),
    [
        ('{"request_id": "r1", "message": "缺用途"}', "application/json", 400),
        ('{"request_id": "r1", "mode": "write", "message": "x"}', "application/json", 400),
        ('{"request_id": "r1", "mode": "query", "message": ""}', "application/json", 400),
        ('{"request_id": "r 1", "mode": "query", "message": "x"}', "application/json", 400),
        ('{"request_id": "r1", "mode": "query", "message": "x", "session_id": "s"}', None, 400),
        ('{"request_id": "r1", "mode": "query", "message": "x", "subject_id": "b"}', None, 400),
        ('{"request_id": "r1", "mode": "query", "message": "x", "target_id": "t"}', None, 400),
        ('["r1", "query", "x"]', "application/json", 400),
        ("{not json", "application/json", 400),
        ('{"request_id": "r1", "mode": "query", "message": "x"}', "text/plain", 415),
        ("request_id=r1&mode=query&message=x", "application/x-www-form-urlencoded", 415),
        ('{"request_id": "r1", "mode": "query", "message": "x"}', "", 415),
    ],
)
async def test_turn_body_must_be_strict_json(
    web: Web, body: str, content_type: str | None, status: int
) -> None:
    headers = {"Content-Type": "application/json" if content_type is None else content_type}
    async with web.client() as client:
        response = await client.post("/api/turns", content=body.encode(), headers=headers)
    assert response.status_code == status and "error" in response.json()
    assert await web.env.requests() == 0 and web.env.adapter.calls == []


# ---- Host / Origin ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "rebind.example:8501",  # 外部域名解析到 loopback
        "localhost.evil:8501",
        "127.0.0.1:9999",
        "127.0.0.1",
        "127.0.0.1.:8501",
        "localhost.:8501",
        "LOCALHOST:8501",
        "user@127.0.0.1:8501",
        "[::ffff:127.0.0.1]:8501",
        "[0:0:0:0:0:0:0:1]:8501",
        "127.1:8501",
        "2130706433:8501",
        "",
    ],
)
async def test_host_must_match_allowlist_literally(web: Web, host: str) -> None:
    for method, path in (("GET", "/healthz"), ("GET", "/"), ("POST", "/api/turns")):
        headers = [(b"host", host.encode()), (b"origin", ORIGIN.encode())]
        status, _, calls = await raw(web.app, method, path, headers)
        assert (status, calls) == (400, 0)
    assert await web.env.requests() == 0


async def test_duplicate_or_missing_host_is_rejected(web: Web) -> None:
    for headers in ([], [(b"host", b"127.0.0.1:8501"), (b"host", b"127.0.0.1:8501")]):
        status, _, _ = await raw(web.app, "GET", "/healthz", headers)
        assert status == 400


async def test_allowed_hosts_include_ipv6_and_named_loopback(env: Env) -> None:
    app = create_web_app(
        env.service, config(allowed_origins=frozenset({"http://[::1]:8501", OTHER_ALLOWED}))
    )
    for host in (b"[::1]:8501", b"localhost:8501"):
        status, _, _ = await raw(app, "GET", "/healthz", [(b"host", host)])
        assert status == 200


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "http://rebind.example:8501",
        "http://127.0.0.1:9999",
        "https://127.0.0.1:8501",
        "http://127.0.0.1:8501/",
        "http://127.0.0.1.:8501",
        OTHER_ALLOWED,  # 允许的地址，但与本次请求的 Host 不同源
    ],
)
async def test_post_requires_same_origin(web: Web, origin: str | None) -> None:
    message = web.env.scripts.add("跨站提交", tool_call("order_total", region="east"), cite())
    body = json.dumps({"request_id": "r1", "mode": "query", "message": message}).encode()
    # 带有效 cookie：拒绝只能来自来源检查。
    headers = [h for h in same_origin() if h[0] != b"origin"]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    for path in ("/api/turns", "/api/sessions"):
        status, _, calls = await raw(web.app, "POST", path, headers, (body,))
        assert (status, calls) == (403, 0)
    assert await web.env.requests() == 0
    assert web.env.model_calls(message) == 0 and web.env.adapter.calls == []
    # 对照：同一请求换成同源 Origin 后被接受并运行。
    status, _, _ = await raw(web.app, "POST", "/api/turns", same_origin(), (body,))
    assert status == 200 and len(web.env.adapter.calls) == 1


async def test_cross_origin_read_is_rejected_and_no_cors_headers(web: Web) -> None:
    async with web.client() as client:
        hostile = await client.get("/api/turns/r1", headers={"Origin": "http://rebind.example"})
        preflight = await client.options(
            "/api/turns",
            headers={
                "Origin": "http://rebind.example",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert hostile.status_code == 403
    for response in (hostile, preflight):
        assert not any(h.startswith("access-control-") for h in response.headers)


# ---- 请求体上限 --------------------------------------------------------------------------


async def test_declared_oversize_body_is_rejected_before_receive(web: Web) -> None:
    headers = same_origin((b"content-length", str(LIMIT + 1).encode()))
    status, _, calls = await raw(web.app, "POST", "/api/turns", headers, (b"x" * (LIMIT + 1),))
    assert (status, calls) == (413, 0)


@pytest.mark.parametrize("length", [b"abc", b"-1", b"1e3", b" 10", b"10, 10"])
async def test_malformed_content_length_is_rejected(web: Web, length: bytes) -> None:
    status, _, calls = await raw(
        web.app, "POST", "/api/turns", same_origin((b"content-length", length))
    )
    assert (status, calls) == (400, 0)


async def test_repeated_content_length_is_rejected(web: Web) -> None:
    headers = same_origin((b"content-length", b"2"), (b"content-length", b"2"))
    status, _, calls = await raw(web.app, "POST", "/api/turns", headers, (b"{}",))
    assert (status, calls) == (400, 0)


@pytest.mark.parametrize(
    "declared",
    [(b"content-length", b"10"), (b"transfer-encoding", b"chunked"), None],
)
async def test_body_reading_stops_at_limit(web: Web, declared: tuple[bytes, bytes] | None) -> None:
    chunk = b"x" * 1024
    chunks = (chunk,) * 20
    headers = same_origin(*([declared] if declared else []))
    status, _, calls = await raw(web.app, "POST", "/api/turns", headers, chunks)
    # 第 5 个 1 KiB 块使累计超过 4 KiB：在此停止，不读完 20 KiB。
    assert (status, calls) == (413, LIMIT // len(chunk) + 1)
    assert await web.env.requests() == 0


# ---- 身份、跨会话读取与冲突 ----------------------------------------------------------------


async def test_other_cookie_cannot_read_or_collide(web: Web) -> None:
    message = web.env.scripts.add("我的结果", tool_call("order_total", region="east"), cite())
    async with web.client() as owner, web.client() as other, web.client(page=False) as anonymous:
        assert (await web.submit(owner, message)).json()["state"] == "completed"
        missing = await owner.get("/api/turns/never")
        foreign = await other.get("/api/turns/r1")
        no_cookie = await anonymous.get("/api/turns/r1")
        # 其他 cookie 使用同一请求编号是它自己的请求，不与他人冲突，也不返回他人的结果。
        mine = web.env.scripts.add("另一条", tool_call("order_total", region="west"), cite())
        reused = await web.submit(other, mine)
    for response in (missing, foreign, no_cookie):
        assert response.status_code == 404
        assert response.json() == missing.json()
    assert reused.json()["state"] == "completed"
    assert reused.json()["delivery"]["facts"][0]["metadata"]["region"] == "west"


async def test_forged_cookie_values_are_replaced(web: Web) -> None:
    async with web.client(page=False) as client:
        client.cookies.set(COOKIE, "short", domain="127.0.0.1")
        page = await client.get("/")
    assert re.match(rf"{COOKIE}=[A-Za-z0-9_-]{{43}};", page.headers["set-cookie"])


async def test_same_request_id_with_other_content_conflicts(web: Web) -> None:
    first = web.env.scripts.add("原内容", tool_call("order_total", region="east"), cite())
    async with web.client() as client:
        await web.submit(client, first)
        conflict = await web.submit(client, "改过的内容")
        mode = await web.submit(client, first, mode="diagnose")
    assert conflict.status_code == mode.status_code == 409
    assert web.env.model_calls("改过的内容") == 0


async def test_revoked_operator_is_denied_without_io(web: Web) -> None:
    web.env.grants.revoke_all()
    message = web.env.scripts.add("撤权后", tool_call("order_total", region="east"), cite())
    async with web.client() as client:
        response = await web.submit(client, message)
    assert response.status_code == 403
    assert await web.env.requests() == 0 and web.env.model_calls(message) == 0


async def test_completed_result_becomes_unavailable_after_revocation(web: Web) -> None:
    message = web.env.scripts.add("先查后撤", tool_call("order_total", region="east"), cite())
    async with web.client() as client:
        await web.submit(client, message)
        web.env.grants.allowed.discard(("alice", TARGET, "local/order_total"))
        response = await client.get("/api/turns/r1")
    assert response.status_code == 403
    assert "rows" not in response.text and "2026-09-01" not in response.text


# ---- 并发、重复与新建会话 ------------------------------------------------------------------


async def test_concurrency_duplicates_and_new_session(web: Web) -> None:
    gate, entered = asyncio.Event(), asyncio.Event()
    slow = web.env.scripts.add(
        "慢查询", after(gate, tool_call("order_total", region="east"), entered=entered), cite()
    )
    other = web.env.scripts.add("同会话第二条", tool_call("order_total", region="east"), cite())
    async with web.client() as client:
        first = asyncio.ensure_future(web.submit(client, slow))
        await entered.wait()
        duplicate = await web.submit(client, slow)
        busy = await web.submit(client, other, "r2")
        blocked = await client.post("/api/sessions", json={})
        gate.set()
        done = await first
        repeat = await web.submit(client, slow)
        renewed = await client.post("/api/sessions", json={})
        after_new = await web.submit(client, other, "r3")
    assert (duplicate.status_code, duplicate.json()["state"]) == (202, "running")
    assert duplicate.json()["delivery"] is None
    assert busy.json()["state"] == "failed"
    assert busy.json()["delivery"]["content"].startswith("系统繁忙")
    assert blocked.status_code == 409
    assert done.json()["state"] == "completed" and repeat.json() == done.json()
    assert renewed.status_code == 200 and after_new.json()["state"] == "completed"
    assert web.env.model_calls(slow) == 2 and web.env.model_calls(other) == 2
    record = await web.env.store.get("web", "alice", client_cookie(client), "r3")
    assert (
        record.session_id
        != (await web.env.store.get("web", "alice", client_cookie(client), "r1")).session_id
    )


def client_cookie(client: httpx.AsyncClient) -> str:
    value = client.cookies.get(COOKIE)
    assert value is not None
    return value


async def test_new_session_requires_json_object(web: Web) -> None:
    async with web.client() as client:
        for content in (b'{"session_id": "s"}', b"[]", b""):
            response = await client.post(
                "/api/sessions", content=content, headers={"Content-Type": "application/json"}
            )
            assert response.status_code == 400


# ---- readiness -------------------------------------------------------------------------


async def test_readiness_gates_new_work_but_not_liveness(web: Web) -> None:
    message = web.env.scripts.add("就绪前", tool_call("order_total", region="east"), cite())
    async with web.client() as client:
        assert (await client.get("/readyz")).status_code == 200
        await web.submit(client, message)
        web.env.readiness.lock("test")
        health = await client.get("/healthz")
        ready = await client.get("/readyz")
        turn = await web.submit(client, "就绪后", "r2")
        session = await client.post("/api/sessions", json={})
        old = await client.get("/api/turns/r1")
    assert health.status_code == 200
    assert ready.status_code == 503 and "test" not in ready.text
    assert turn.status_code == session.status_code == 503
    assert old.status_code == 200 and old.json()["state"] == "completed"
    assert web.env.model_calls("就绪后") == 0


async def test_result_not_saved_returns_fixed_receipt(web: Web) -> None:
    message = web.env.scripts.add("保存失败", tool_call("order_total", region="east"), cite())
    await web.env.fail_request_writes("NEW.state = 'completed'")
    async with web.client() as client:
        response = await web.submit(client, message)
        await web.env.heal_request_writes()
        again = await client.get("/api/turns/r1")
    assert response.status_code == 200 and response.json()["state"] == "failed"
    assert response.json()["delivery"]["content"] == "结果保存失败，请新建会话后重试"
    assert again.json() == response.json() and "2026-09-01" not in response.text


async def test_unwritable_terminal_state_locks_readiness(web: Web) -> None:
    message = web.env.scripts.add("终态写不了", tool_call("order_total", region="east"), cite())
    await web.env.fail_request_writes("NEW.state IN ('completed', 'failed')")
    async with web.client() as client:
        response = await web.submit(client, message)
        await web.env.heal_request_writes()
        ready = await client.get("/readyz")
        pending = await client.get("/api/turns/r1")
    assert response.status_code == 503 and "error" in response.json()
    assert ready.status_code == 503
    # 本进程不能再写该请求的终态：不再显示“处理中”，交给重启恢复标为 interrupted。
    assert pending.status_code == 503 and "state" not in pending.json()


# ---- 显示安全 ----------------------------------------------------------------------------

HOSTILE = '<script>alert(1)</script><img src=x onerror="alert(2)">‮😀'


async def test_hostile_values_stay_json_data(web: Web) -> None:
    web.env.adapter.memo, web.env.adapter.truncated = HOSTILE, True
    message = web.env.scripts.add(
        "危险内容", tool_call("order_total", region="east"), cite(f"<b onmouseover=x>{'长' * 150}")
    )
    async with web.client() as client:
        response = await web.submit(client, message)
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-type-options"] == "nosniff"
    body = response.json()
    assert body["delivery"]["facts"][0]["rows"][0]["memo"] == HOSTILE
    assert body["delivery"]["facts"][0]["truncated"] is True
    assert "<b onmouseover=x>" in body["delivery"]["content"]


async def test_page_is_static_and_script_never_parses_html(web: Web) -> None:
    async with web.client() as client:
        page = await client.get("/")
        script = await client.get("/assets/app.js")
        style = await client.get("/assets/app.css")
        missing = await client.get("/assets/../web.py")
    csp = page.headers["content-security-policy"]
    directives = {part.strip() for part in csp.split(";")}
    for directive in (
        "default-src 'none'",
        "script-src 'self'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ):
        assert directive in directives
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    # 脚本未加载时表单也不会把消息放进 URL：显式 POST 到 JSON 接口（非 JSON 正文得到 415），
    # 且 CSP 的 form-action 'none' 阻止提交本身；浏览器行为由 test_web_browser.py 证明。
    assert re.findall(r"<form[^>]*>", page.text) == [
        '<form id="composer" method="post" action="/api/turns">'
    ]
    assert script.headers["content-type"].startswith("text/javascript")
    assert style.headers["content-type"].startswith("text/css")
    assert missing.status_code == 404
    # 页面没有内联脚本或事件属性；脚本只用 textContent 与 DOM API。
    assert re.findall(r"<script[^>]*>", page.text) == ['<script src="/assets/app.js" defer>']
    assert not re.search(r"\son[a-z]+\s*=", page.text)
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "Function(",
        'setTimeout("',
        "srcdoc",
        "DOMParser",
        "createContextualFragment",
    ):
        assert forbidden not in script.text
    assert script.text == (STATIC / "app.js").read_text(encoding="utf-8")


def test_web_config_accepts_canonical_loopback_and_private_ipv4_origins() -> None:
    for origin in (
        "http://127.0.0.1:8501",
        "http://[::1]:8501",
        "http://localhost:8501",
        "https://localhost:8443",
        "http://127.0.0.1:443",  # 不是 http 的默认端口，浏览器保留它
        "https://127.0.0.1:80",
        "http://172.20.0.8:8501",
    ):
        config(allowed_origins=frozenset({origin}))
    for origin in (
        "http://127.0.0.1",
        "http://LOCALHOST:8501",
        "http://localhost.:8501",
        "http://u@127.0.0.1:8501",
        "http://127.0.0.1:08501",
        "http://127.0.0.1:8501/",
        "http://rebind.example:8501",
        "http://[0:0:0:0:0:0:0:1]:8501",
        "http://127.1:8501",
        "http://8.8.8.8:8501",
        "http://0.0.0.0:8501",
        "http://172.20.0.8:08501",
        "http://172.020.0.8:8501",
        "ftp://127.0.0.1:8501",
    ):
        with pytest.raises(ValueError):
            config(allowed_origins=frozenset({origin}))
    with pytest.raises(ValueError):
        config(allowed_origins=frozenset({ORIGIN, "https://127.0.0.1:8501"}))


async def test_private_origin_guard_accepts_only_configured_same_origin_requests() -> None:
    accepted: list[str] = []

    async def endpoint(scope: Any, receive: Any, send: Any) -> None:
        del receive
        accepted.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    private_origin = "http://172.20.0.8:8501"
    guard = _Guard(
        endpoint,
        origins=frozenset({ORIGIN, private_origin}),
        max_body_bytes=LIMIT,
    )
    private_host = b"172.20.0.8:8501"

    status, _, _ = await raw(guard, "GET", "/", [(b"host", private_host)])
    assert status == 200
    status, _, _ = await raw(
        guard,
        "POST",
        "/api/turns",
        [
            (b"host", private_host),
            (b"origin", private_origin.encode()),
            (b"content-type", b"application/json"),
        ],
        (b"{}",),
    )
    assert status == 200 and accepted == ["/", "/api/turns"]

    status, _, _ = await raw(
        guard,
        "POST",
        "/api/turns",
        [
            (b"host", private_host),
            (b"origin", ORIGIN.encode()),
            (b"content-type", b"application/json"),
        ],
        (b"{}",),
    )
    assert status == 403 and accepted == ["/", "/api/turns"]
    status, _, _ = await raw(
        guard,
        "GET",
        "/",
        [(b"host", b"8.8.8.8:8501"), (b"origin", b"http://8.8.8.8:8501")],
    )
    assert status == 400 and accepted == ["/", "/api/turns"]


# 浏览器在 Host 与 Origin 中省略 scheme 的默认端口，端口 0 没有稳定的 origin：按字面匹配时
# 这些配置下的每个请求都会被拒绝，因此在配置阶段失败。
UNUSABLE_ORIGINS = ("http://127.0.0.1:0", "http://127.0.0.1:80", "https://localhost:443")


@pytest.mark.parametrize("origin", UNUSABLE_ORIGINS)
async def test_web_config_rejects_ports_a_browser_cannot_send(env: Env, origin: str) -> None:
    message = env.scripts.add("端口配置", tool_call("order_total", region="east"), cite())
    with pytest.raises(ValueError, match="端口"):
        config(allowed_origins=frozenset({origin}))
    # 同一组里混入可用地址也不放行。
    with pytest.raises(ValueError, match="端口"):
        config(allowed_origins=frozenset({ORIGIN, origin}))
    assert await env.requests() == 0 and env.model_calls(message) == 0
    assert env.adapter.calls == []


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        (ORIGIN, b"127.0.0.1:8501"),
        ("http://[::1]:8501", b"[::1]:8501"),
        (OTHER_ALLOWED, b"localhost:8501"),
        ("http://172.20.0.8:8501", b"172.20.0.8:8501"),
    ],
)
async def test_allowed_origin_serves_a_same_origin_turn(env: Env, origin: str, host: bytes) -> None:
    message = env.scripts.add("同源对照", tool_call("order_total", region="east"), cite())
    app = create_web_app(env.service, config(allowed_origins=frozenset({origin})))
    headers = [(b"host", host)]
    status, page, _ = await raw(app, "GET", "/", headers)
    assert status == 200 and b"<form" in page
    body = json.dumps({"request_id": "r1", "mode": "query", "message": message}).encode()
    post = [
        (b"host", host),
        (b"origin", origin.encode()),
        (b"content-type", b"application/json"),
        (b"cookie", f"{COOKIE}={'c' * 43}".encode()),
    ]
    status, response, _ = await raw(app, "POST", "/api/turns", post, (body,))
    assert status == 200 and json.loads(response)["state"] == "completed"
    assert env.model_calls(message) == 2 and len(env.adapter.calls) == 1


async def test_secure_cookie_setting_marks_the_cookie_secure(env: Env) -> None:
    app = create_web_app(env.service, config(secure_cookie=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        page = await client.get("/")
    cookie = page.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
