"""Bounded download of one resource belonging to a trusted Lark message.

The SDK's public download method buffers the full response before returning. This small
HTTP adapter uses its documented message-resource and tenant-token endpoints instead;
callers must authorize the event before invoking it. No user-supplied URL is accepted.
"""

import asyncio
import json
import re
from typing import Literal

import httpx2
from lark_channel.core.const import FEISHU_DOMAIN, LARK_DOMAIN

from xiaowei.config import FeishuConfig, SecretRefError, resolve_secret_ref

ResourceKind = Literal["image", "file"]
ResourceErrorCode = Literal[
    "invalid_reference",
    "authentication_failed",
    "resource_unavailable",
    "too_large",
    "unsupported_encoding",
    "timeout",
    "transport_failed",
]

_REFERENCE = re.compile(r"[0-9A-Za-z_-]{1,200}\Z")
_TOTAL_TIMEOUT_SECONDS = 30.0
_READ_TIMEOUT_SECONDS = 5.0
_MAX_RESOURCE_BYTES = 2 * 1024 * 1024
_TOKEN_BYTES = 8192
_CHUNK_BYTES = 64 * 1024


class ResourceDownloadError(Exception):
    """A fixed reason code only; upstream response bodies and credentials stay private."""

    def __init__(self, code: ResourceErrorCode) -> None:
        super().__init__(code)
        self.code = code


class LarkResourceDownloader:
    """Fetch a resource by the message ID and key from an already verified raw event."""

    def __init__(
        self, config: FeishuConfig, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._domain = FEISHU_DOMAIN if config.domain == "feishu" else LARK_DOMAIN
        self._transport = transport

    async def download(
        self, message_id: str, file_key: str, kind: ResourceKind, *, max_bytes: int
    ) -> bytes:
        if (
            _REFERENCE.fullmatch(message_id) is None
            or _REFERENCE.fullmatch(file_key) is None
            or kind not in ("image", "file")
            or not 0 < max_bytes <= _MAX_RESOURCE_BYTES
        ):
            raise ResourceDownloadError("invalid_reference")
        try:
            async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                secret = resolve_secret_ref(self._config.app_secret_ref).get_secret_value()
                async with httpx2.AsyncClient(
                    timeout=httpx2.Timeout(_READ_TIMEOUT_SECONDS),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client:
                    token = await self._token(client, secret)
                    path = (
                        f"{self._domain}/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
                    )
                    async with client.stream(
                        "GET",
                        path,
                        params={"type": kind},
                        headers={
                            "authorization": f"Bearer {token}",
                            "accept-encoding": "identity",
                        },
                    ) as response:
                        if response.status_code != 200:
                            raise ResourceDownloadError("resource_unavailable")
                        content = await _bounded_body(response, max_bytes)
                        if not content:
                            raise ResourceDownloadError("resource_unavailable")
                        return content
        except SecretRefError:
            raise ResourceDownloadError("authentication_failed") from None
        except TimeoutError:
            raise ResourceDownloadError("timeout") from None
        except httpx2.TimeoutException:
            raise ResourceDownloadError("timeout") from None
        except httpx2.HTTPError:
            raise ResourceDownloadError("transport_failed") from None

    async def _token(self, client: httpx2.AsyncClient, secret: str) -> str:
        async with client.stream(
            "POST",
            f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._config.app_id, "app_secret": secret},
            headers={"accept-encoding": "identity"},
        ) as response:
            if response.status_code != 200:
                raise ResourceDownloadError("authentication_failed")
            try:
                payload = json.loads(await _bounded_body(response, _TOKEN_BYTES))
            except (ValueError, ResourceDownloadError):
                raise ResourceDownloadError("authentication_failed") from None
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise ResourceDownloadError("authentication_failed")
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token or len(token) > _TOKEN_BYTES:
            raise ResourceDownloadError("authentication_failed")
        return token


async def _bounded_body(response: httpx2.Response, limit: int) -> bytes:
    encoding = response.headers.get("content-encoding")
    if encoding is not None and encoding.strip().lower() != "identity":
        raise ResourceDownloadError("unsupported_encoding")
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise ResourceDownloadError("resource_unavailable") from None
        if length < 0:
            raise ResourceDownloadError("resource_unavailable")
        if length > limit:
            raise ResourceDownloadError("too_large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_raw(chunk_size=_CHUNK_BYTES):
        size += len(chunk)
        if size > limit:
            raise ResourceDownloadError("too_large")
        chunks.append(chunk)
    return b"".join(chunks)
