"""E5b 前置协议实验：用本机 HTTP 端点确认 httpx 公开流式接口的字节、期限与编码边界。

这是协议证据，不是飞书附件已经可用的产品测试；正式入口须另行覆盖。
"""

from __future__ import annotations

import asyncio
import base64
import gzip
from collections.abc import AsyncIterator

import httpx2
import pytest

pytestmark = pytest.mark.loopback

_IMAGE_LIMIT = 2 * 1024 * 1024
_CHUNK = 64 * 1024


def test_twelve_full_images_have_separate_request_cost() -> None:
    """锁版编码器的合成请求大小；不代表真实 Vertex 已接受这个请求。"""
    encoded = base64.b64encode(b"x" * _IMAGE_LIMIT).decode("ascii")
    parts = [{"inlineData": {"mimeType": "image/png", "data": encoded}} for _ in range(12)]
    request = httpx2.Request(
        "POST",
        "https://example.invalid",
        json={
            "contents": [{"role": "user", "parts": [{"text": "检查这些截图"}, *parts]}],
            "generationConfig": {"candidateCount": 1},
        },
    )
    image_bytes = 12 * len(encoded)
    assert image_bytes == 33_554_448
    assert len(request.content) == 33_555_158
    assert len(request.content) - image_bytes == 710


class _TooLargeError(Exception):
    pass


class _UnsupportedEncodingError(Exception):
    pass


async def _read_bounded(
    client: httpx2.AsyncClient, url: str, limit: int, *, total_timeout: float = 3
) -> bytes:
    async with asyncio.timeout(total_timeout):
        async with client.stream("GET", url, headers={"accept-encoding": "identity"}) as response:
            response.raise_for_status()
            encoding = response.headers.get("content-encoding")
            if encoding is not None and encoding.strip().lower() != "identity":
                raise _UnsupportedEncodingError
            length = response.headers.get("content-length")
            if length is not None and int(length) > limit:
                raise _TooLargeError
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_raw(chunk_size=_CHUNK):
                size += len(chunk)
                if size > limit:
                    raise _TooLargeError
                chunks.append(chunk)
            return b"".join(chunks)


async def _endpoint(
    payload: bytes,
    *,
    declared: int | None = None,
    slow: bool = False,
    encoding: str | None = None,
    chunk_size: int = _CHUNK,
    chunk_delay: float = 0.02,
    pause_after: int | None = None,
    observed: tuple[asyncio.Event, asyncio.Event] | None = None,
) -> AsyncIterator[str]:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            encoding_header = (
                b"Content-Encoding: " + encoding.encode("ascii") + b"\r\n" if encoding else b""
            )
            if declared is None:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n" + encoding_header + b"\r\n"
                )
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(declared).encode("ascii")
                    + b"\r\n"
                    + encoding_header
                    + b"\r\n"
                )
            await writer.drain()
            for index, start in enumerate(range(0, len(payload), chunk_size), start=1):
                part = payload[start : start + chunk_size]
                if declared is None:
                    writer.write(f"{len(part):x}\r\n".encode("ascii") + part + b"\r\n")
                else:
                    writer.write(part)
                await writer.drain()
                if pause_after == index:
                    assert observed is not None
                    observed[0].set()
                    try:
                        assert await reader.read(1) == b""
                    except ConnectionResetError:
                        pass  # 取消关闭连接在不同平台可能表现为 EOF 或 reset
                    observed[1].set()
                    return
                if slow:
                    await asyncio.sleep(chunk_delay)
            if declared is None:
                writer.write(b"0\r\n\r\n")
                await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/resource"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stream_without_content_length_accepts_only_within_limit() -> None:
    async for url in _endpoint(b"x" * (_IMAGE_LIMIT - 1)):
        async with httpx2.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            assert len(await _read_bounded(client, url, _IMAGE_LIMIT)) == _IMAGE_LIMIT - 1


@pytest.mark.asyncio
async def test_stream_without_content_length_stops_on_overflow() -> None:
    async for url in _endpoint(b"x" * (_IMAGE_LIMIT + 1), slow=True):
        async with httpx2.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            with pytest.raises(_TooLargeError):
                await _read_bounded(client, url, _IMAGE_LIMIT)


@pytest.mark.asyncio
async def test_declared_oversize_is_rejected_before_reading_body() -> None:
    async for url in _endpoint(b"x", declared=_IMAGE_LIMIT + 1):
        async with httpx2.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            with pytest.raises(_TooLargeError):
                await _read_bounded(client, url, _IMAGE_LIMIT)


@pytest.mark.asyncio
async def test_per_read_timeout_closes_request() -> None:
    async for url in _endpoint(b"x" * (2 * _CHUNK), slow=True):
        timeout = httpx2.Timeout(0.005)
        async with httpx2.AsyncClient(
            timeout=timeout, trust_env=False, follow_redirects=False
        ) as client:
            with pytest.raises(httpx2.TimeoutException):
                await _read_bounded(client, url, _IMAGE_LIMIT)


@pytest.mark.asyncio
async def test_drip_feed_exceeds_total_deadline_despite_short_read_intervals() -> None:
    async for url in _endpoint(b"x" * 30, slow=True, chunk_size=1):
        async with httpx2.AsyncClient(
            timeout=0.5, trust_env=False, follow_redirects=False
        ) as client:
            with pytest.raises(TimeoutError):
                await _read_bounded(client, url, _IMAGE_LIMIT, total_timeout=0.1)


@pytest.mark.asyncio
async def test_encoded_response_is_rejected_even_when_wire_bytes_fit() -> None:
    compressed = gzip.compress(b"x" * (_IMAGE_LIMIT + 1))
    assert len(compressed) < _IMAGE_LIMIT
    async for url in _endpoint(compressed, encoding="gzip"):
        async with httpx2.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            with pytest.raises(_UnsupportedEncodingError):
                await _read_bounded(client, url, _IMAGE_LIMIT)


@pytest.mark.asyncio
async def test_explicit_identity_encoding_is_accepted() -> None:
    async for url in _endpoint(b"valid", encoding="IDENTITY"):
        async with httpx2.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            assert await _read_bounded(client, url, _IMAGE_LIMIT) == b"valid"


@pytest.mark.asyncio
async def test_cancelled_stream_does_not_return_partial_resource() -> None:
    partial, disconnected = asyncio.Event(), asyncio.Event()
    async for url in _endpoint(
        b"x" * (4 * _CHUNK), pause_after=2, observed=(partial, disconnected)
    ):
        async with httpx2.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            reading = asyncio.create_task(_read_bounded(client, url, _IMAGE_LIMIT))
            await asyncio.wait_for(partial.wait(), 1)
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading
            await asyncio.wait_for(disconnected.wait(), 1)


@pytest.mark.asyncio
async def test_false_short_length_does_not_return_full_resource() -> None:
    async for url in _endpoint(b"x" * (2 * _CHUNK), declared=1):
        async with httpx2.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            content = await _read_bounded(client, url, _IMAGE_LIMIT)
            assert content != b"x" * (2 * _CHUNK)
            # 格式校验必须拒绝这种被截短的返回，不能仅凭成功 HTTP 状态认可图像。
            assert content == b"x"
