"""有界 PNG/JPEG 原图验证；只检查可解码性，不修改上传字节。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import TYPE_CHECKING, Literal

from PIL import Image, UnidentifiedImageError
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei.models import Identity

if TYPE_CHECKING:
    from xiaowei.channel_store import ChannelSession

ImageRejectCode = Literal["too_large", "dimensions", "unreadable"]
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_SIDE = 8192
MAX_IMAGE_PIXELS = 16_777_216


class ImageRejectedError(Exception):
    def __init__(self, code: ImageRejectCode) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    mime_type: Literal["image/png", "image/jpeg"]
    width: int
    height: int


@dataclass(frozen=True)
class SavedImage:
    source_digest: str
    data: bytes
    mime_type: Literal["image/png", "image/jpeg"]


class ImageStoreError(Exception):
    """图片保存或读取不能确认；消息固定，不含原图或数据库信息。"""


class ImageCapacityError(ImageStoreError):
    """当前会话已存 12 张原图。"""


class ImageUnavailableError(ImageStoreError):
    """图片归属、期限或原值无法核实。"""


_SESSION = text(
    "SELECT owner_kind, owner_id, state, expires_at FROM xiaowei_channel_session "
    "WHERE session_id = :session_id AND channel = 'feishu'"
)
_COUNT = text(
    "SELECT count(*) FROM xiaowei_image AS i JOIN xiaowei_request AS r "
    "ON r.turn_id = i.turn_id WHERE i.session_id = :session_id "
    "AND r.state IN ('accepted', 'running', 'completed')"
)
_DELETE_FAILED = text(
    "DELETE FROM xiaowei_image AS i USING xiaowei_request AS r "
    "WHERE i.turn_id = r.turn_id AND i.session_id = :session_id "
    "AND r.state IN ('failed', 'interrupted')"
)
_INSERT = text(
    "INSERT INTO xiaowei_image (session_id, turn_id, source_digest, content_digest, "
    "mime_type, original, expires_at) "
    "SELECT :session_id, r.turn_id, :source_digest, :content_digest, :mime_type, "
    ":original, :expires_at FROM xiaowei_request AS r "
    "WHERE r.turn_id = :turn_id AND r.session_id = :session_id "
    "AND r.channel = 'feishu' AND r.state = 'accepted' AND r.expires_at > :now"
)
_READ = text(
    "SELECT i.source_digest, i.content_digest, i.mime_type, i.original, i.expires_at "
    "FROM xiaowei_image AS i JOIN xiaowei_request AS r ON r.turn_id = i.turn_id "
    "WHERE i.session_id = :session_id AND (r.state = 'completed' OR "
    "(i.turn_id = :turn_id AND r.state IN ('accepted', 'running'))) ORDER BY i.image_id"
)


class SessionImageStore:
    """原图留在应用表；与 SDK Session 的纯文字历史分离。"""

    def __init__(self, engine: AsyncEngine, *, clock: Callable[[], datetime]) -> None:
        self._engine = engine
        self._clock = clock

    async def has_capacity(self, session: ChannelSession) -> bool:
        try:
            async with self._engine.connect() as conn:
                row = (
                    (await conn.execute(_SESSION, {"session_id": session.session_id}))
                    .mappings()
                    .one_or_none()
                )
                self._check(row, session.owner.kind, session.owner.id)
                return int(await conn.scalar(_COUNT, {"session_id": session.session_id}) or 0) < 12
        except SQLAlchemyError:
            raise ImageStoreError("图片容量无法核实") from None

    async def save(
        self, session: ChannelSession, source_digest: str, image: ValidatedImage, *, turn_id: str
    ) -> None:
        if (
            not turn_id
            or not source_digest
            or len(source_digest) > 128
            or not image.data
            or len(image.data) > MAX_IMAGE_BYTES
            or image.mime_type not in ("image/png", "image/jpeg")
        ):
            raise ImageUnavailableError("图片来源或原值无法核实")
        try:
            async with self._engine.begin() as conn:
                row = (
                    (
                        await conn.execute(
                            text(str(_SESSION) + " FOR UPDATE"), {"session_id": session.session_id}
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                row = self._check(row, session.owner.kind, session.owner.id)
                await conn.execute(_DELETE_FAILED, {"session_id": session.session_id})
                count = int(await conn.scalar(_COUNT, {"session_id": session.session_id}) or 0)
                if count >= 12:
                    raise ImageCapacityError("会话图片已达上限")
                inserted = await conn.execute(
                    _INSERT,
                    {
                        "session_id": session.session_id,
                        "turn_id": turn_id,
                        "source_digest": source_digest,
                        "content_digest": hashlib.sha256(image.data).hexdigest(),
                        "mime_type": image.mime_type,
                        "original": image.data,
                        "expires_at": row["expires_at"],
                        "now": self._clock(),
                    },
                )
                if inserted.rowcount != 1:
                    raise ImageUnavailableError("图片请求无法绑定当前会话")
        except SQLAlchemyError:
            raise ImageStoreError("图片保存失败") from None

    async def load(self, identity: Identity) -> list[SavedImage]:
        if identity.channel != "feishu":
            return []
        try:
            async with self._engine.connect() as conn:
                row = (
                    (await conn.execute(_SESSION, {"session_id": identity.session_id}))
                    .mappings()
                    .one_or_none()
                )
                row = self._check(row, identity.owner.kind, identity.owner.id)
                session_expires = row["expires_at"]
                rows = (
                    (
                        await conn.execute(
                            _READ, {"session_id": identity.session_id, "turn_id": identity.turn_id}
                        )
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError:
            raise ImageStoreError("图片读取失败") from None
        if len(rows) > 12:
            raise ImageUnavailableError("会话图片数量无法核实")
        images: list[SavedImage] = []
        for row in rows:
            original = bytes(row["original"])
            if (
                row["expires_at"] <= self._clock()
                or row["expires_at"] != session_expires
                or not original
                or len(original) > MAX_IMAGE_BYTES
                or row["mime_type"] not in ("image/png", "image/jpeg")
                or hashlib.sha256(original).hexdigest() != row["content_digest"]
            ):
                raise ImageUnavailableError("会话图片原值无法核实")
            images.append(SavedImage(row["source_digest"], original, row["mime_type"]))
        return images

    def _check(self, row: RowMapping | None, kind: str, owner_id: str) -> RowMapping:
        if (
            row is None
            or row["owner_kind"] != kind
            or row["owner_id"] != owner_id
            or row["state"] != "current"
            or row["expires_at"] <= self._clock()
        ):
            raise ImageUnavailableError("会话图片归属或期限无法核实")
        return row


def validate_image(raw: bytes) -> ValidatedImage:
    """先限原值和尺寸，再完整解码；调用方在线程中执行。"""
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ImageRejectedError("too_large")
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format not in ("PNG", "JPEG") or getattr(image, "n_frames", 1) != 1:
                raise ImageRejectedError("unreadable")
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_IMAGE_SIDE
                or height > MAX_IMAGE_SIDE
                or width * height > MAX_IMAGE_PIXELS
            ):
                raise ImageRejectedError("dimensions")
            format = image.format
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ImageRejectedError("unreadable") from None
    mime: Literal["image/png", "image/jpeg"] = "image/png" if format == "PNG" else "image/jpeg"
    return ValidatedImage(raw, mime, width, height)
