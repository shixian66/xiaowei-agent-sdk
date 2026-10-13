"""E5b: the production downloader uses only trusted Lark endpoints and stops at byte bounds."""

import asyncio
import json

import httpx2
import pytest
from tests.sdk_core.test_feishu import config


class _Body(httpx2.AsyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def __aiter__(self):
        yield self.payload


class _DripBody(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        yield b"a"
        await asyncio.sleep(10)
        yield b"b"

    async def aclose(self) -> None:
        self.closed.set()


def _response(payload: bytes, *, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(200, headers=headers, stream=_Body(payload))


def _downloader(monkeypatch: pytest.MonkeyPatch, handler, *, domain: str = "lark"):
    from xiaowei.feishu_resource import LarkResourceDownloader

    monkeypatch.setenv("XW_TEST_FEISHU_SECRET", "RESOURCE_SECRET_SENTINEL")
    return LarkResourceDownloader(config(domain=domain), transport=httpx2.MockTransport(handler))


@pytest.mark.asyncio
async def test_download_uses_fixed_lark_domain_and_message_resource_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        assert request.url.host == "open.larksuite.com"
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            assert json.loads(request.content) == {
                "app_id": "cli_test",
                "app_secret": "RESOURCE_SECRET_SENTINEL",
            }
            return _response(b'{"code":0,"tenant_access_token":"token"}')
        assert request.url.path == "/open-apis/im/v1/messages/om_test/resources/file_test"
        assert request.url.params["type"] == "file"
        assert request.headers["authorization"] == "Bearer token"
        assert request.headers["accept-encoding"] == "identity"
        return _response(b"diagnostic text")

    downloader = _downloader(monkeypatch, handler)
    content = await downloader.download("om_test", "file_test", "file", max_bytes=100)
    assert content == b"diagnostic text"
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_default_domain_uses_feishu_sdk_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        hosts.append(request.url.host)
        if request.url.path.endswith("/internal"):
            return _response(b'{"code":0,"tenant_access_token":"token"}')
        return _response(b"log")

    downloader = _downloader(monkeypatch, handler, domain="feishu")
    assert await downloader.download("om_test", "file_test", "file", max_bytes=100) == b"log"
    assert hosts == ["open.feishu.cn", "open.feishu.cn"]


@pytest.mark.asyncio
async def test_missing_runtime_credential_has_fixed_failure_without_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xiaowei.feishu_resource import ResourceDownloadError

    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _response(b"unexpected")

    downloader = _downloader(monkeypatch, handler)
    monkeypatch.delenv("XW_TEST_FEISHU_SECRET")
    with pytest.raises(ResourceDownloadError) as rejected:
        await downloader.download("om_test", "img_test", "image", max_bytes=100)
    assert rejected.value.code == "authentication_failed"
    assert requests == []


@pytest.mark.asyncio
async def test_resource_at_exact_byte_limit_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/internal"):
            return _response(b'{"code":0,"tenant_access_token":"token"}')
        return _response(b"x" * 100, headers={"content-length": "100"})

    downloader = _downloader(monkeypatch, handler)
    assert await downloader.download("om_test", "file_test", "file", max_bytes=100) == b"x" * 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_length", "encoding", "payload", "reason"),
    [
        ("101", None, b"x", "too_large"),
        (None, None, b"x" * 101, "too_large"),
        (None, "gzip", b"x", "unsupported_encoding"),
    ],
)
async def test_download_rejects_oversize_and_encoded_resources(
    monkeypatch: pytest.MonkeyPatch,
    content_length: str | None,
    encoding: str | None,
    payload: bytes,
    reason: str,
) -> None:
    from xiaowei.feishu_resource import ResourceDownloadError

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/internal"):
            return _response(b'{"code":0,"tenant_access_token":"token"}')
        headers = {}
        if content_length is not None:
            headers["content-length"] = content_length
        if encoding is not None:
            headers["content-encoding"] = encoding
        return _response(payload, headers=headers)

    downloader = _downloader(monkeypatch, handler)
    with pytest.raises(ResourceDownloadError) as rejected:
        await downloader.download("om_test", "file_test", "file", max_bytes=100)
    assert rejected.value.code == reason
    assert "RESOURCE_SECRET_SENTINEL" not in str(rejected.value)


@pytest.mark.asyncio
async def test_invalid_reference_is_rejected_before_token_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xiaowei.feishu_resource import ResourceDownloadError

    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        raise AssertionError("untrusted key must not reach I/O")

    downloader = _downloader(monkeypatch, handler)
    with pytest.raises(ResourceDownloadError) as rejected:
        await downloader.download("../other", "file_test", "file", max_bytes=100)
    assert rejected.value.code == "invalid_reference"
    assert requests == []


@pytest.mark.asyncio
async def test_total_deadline_covers_token_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xiaowei.feishu_resource import ResourceDownloadError

    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0.1)
        return httpx2.Response(200, json={"code": 0, "tenant_access_token": "token"})

    downloader = _downloader(monkeypatch, handler)
    monkeypatch.setattr("xiaowei.feishu_resource._TOTAL_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(ResourceDownloadError) as rejected:
        await downloader.download("om_test", "file_test", "file", max_bytes=100)
    assert rejected.value.code == "timeout"


@pytest.mark.asyncio
async def test_total_deadline_covers_slow_body_and_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xiaowei.feishu_resource import ResourceDownloadError

    body = _DripBody()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/internal"):
            return _response(b'{"code":0,"tenant_access_token":"token"}')
        return httpx2.Response(200, stream=body)

    downloader = _downloader(monkeypatch, handler)
    monkeypatch.setattr("xiaowei.feishu_resource._TOTAL_TIMEOUT_SECONDS", 0.03)
    with pytest.raises(ResourceDownloadError) as rejected:
        await downloader.download("om_test", "file_test", "file", max_bytes=100)
    assert rejected.value.code == "timeout" and body.started.is_set() and body.closed.is_set()


@pytest.mark.asyncio
async def test_cancellation_midstream_closes_without_returning_partial_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _DripBody()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/internal"):
            return _response(b'{"code":0,"tenant_access_token":"token"}')
        return httpx2.Response(200, stream=body)

    downloader = _downloader(monkeypatch, handler)
    task = asyncio.create_task(downloader.download("om_test", "file_test", "file", max_bytes=100))
    await body.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed.is_set()


@pytest.mark.asyncio
async def test_redirect_and_authentication_failure_do_not_download_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xiaowei.feishu_resource import ResourceDownloadError

    paths = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        return httpx2.Response(302, headers={"location": "https://elsewhere.invalid/token"})

    downloader = _downloader(monkeypatch, handler)
    with pytest.raises(ResourceDownloadError) as rejected:
        await downloader.download("om_test", "file_test", "file", max_bytes=100)
    assert rejected.value.code == "authentication_failed"
    assert paths == ["/open-apis/auth/v3/tenant_access_token/internal"]
