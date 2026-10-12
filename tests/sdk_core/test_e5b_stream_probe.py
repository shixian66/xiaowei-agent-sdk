"""E5b 前置协议实验：用本机 HTTP 端点确认 httpx 公开流式接口能在读入上限处关闭。

这是协议证据，不是飞书附件已经可用的产品测试；正式入口须另行覆盖。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx2
import pytest

pytestmark = pytest.mark.loopback

_IMAGE_LIMIT = 2 * 1024 * 1024
_CHUNK = 64 * 1024


class _TooLargeError(Exception):
    pass


async def _read_bounded(client: httpx2.AsyncClient, url: str, limit: int) -> bytes:
    async with client.stream("GET", url, headers={"accept-encoding": "identity"}) as response:
        response.raise_for_status()
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
    payload: bytes, *, declared: int | None = None, slow: bool = False
) -> AsyncIterator[str]:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            if declared is None:
                writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(declared).encode("ascii")
                    + b"\r\n\r\n"
                )
            await writer.drain()
            for start in range(0, len(payload), _CHUNK):
                part = payload[start : start + _CHUNK]
                if declared is None:
                    writer.write(f"{len(part):x}\r\n".encode("ascii") + part + b"\r\n")
                else:
                    writer.write(part)
                await writer.drain()
                if slow:
                    await asyncio.sleep(0.02)
            if declared is None:
                writer.write(b"0\r\n\r\n")
                await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

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
async def test_stream_timeout_closes_request() -> None:
    async for url in _endpoint(b"x" * (2 * _CHUNK), slow=True):
        timeout = httpx2.Timeout(0.005)
        async with httpx2.AsyncClient(
            timeout=timeout, trust_env=False, follow_redirects=False
        ) as client:
            with pytest.raises(httpx2.TimeoutException):
                await _read_bounded(client, url, _IMAGE_LIMIT)


@pytest.mark.asyncio
async def test_cancelled_stream_does_not_return_partial_resource() -> None:
    async for url in _endpoint(b"x" * (4 * _CHUNK), slow=True):
        async with httpx2.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            reading = asyncio.create_task(_read_bounded(client, url, _IMAGE_LIMIT))
            await asyncio.sleep(0.005)
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading


@pytest.mark.asyncio
async def test_false_short_length_does_not_return_full_resource() -> None:
    async for url in _endpoint(b"x" * (2 * _CHUNK), declared=1):
        async with httpx2.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            content = await _read_bounded(client, url, _IMAGE_LIMIT)
            assert content != b"x" * (2 * _CHUNK)
            # 格式校验必须拒绝这种被截短的返回，不能仅凭成功 HTTP 状态认可图像。
            assert content == b"x"
