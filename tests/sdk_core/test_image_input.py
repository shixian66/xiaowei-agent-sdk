"""PNG/JPEG 原图准入：只验证，不改写或压缩上传原值。"""

from io import BytesIO

import pytest
from PIL import Image

from xiaowei.image_input import ImageRejectedError, validate_image


def _image(format: str, size: tuple[int, int] = (8, 8)) -> bytes:
    stream = BytesIO()
    Image.new("RGB", size, "red").save(stream, format=format)
    return stream.getvalue()


@pytest.mark.parametrize(("format", "mime"), [("PNG", "image/png"), ("JPEG", "image/jpeg")])
def test_valid_original_is_preserved(format: str, mime: str) -> None:
    raw = _image(format)
    accepted = validate_image(raw)
    assert accepted.mime_type == mime
    assert accepted.data == raw
    assert accepted.width == 8 and accepted.height == 8


@pytest.mark.parametrize("raw", [b"not image", b"\x89PNG\r\n\x1a\n", _image("JPEG")[:-15]])
def test_corrupt_or_truncated_input_is_rejected(raw: bytes) -> None:
    with pytest.raises(ImageRejectedError, match="unreadable"):
        validate_image(raw)


def test_highly_compressible_oversized_dimensions_stop_before_decode() -> None:
    raw = _image("PNG", (8193, 100))
    assert len(raw) < 2 * 1024 * 1024
    with pytest.raises(ImageRejectedError, match="dimensions"):
        validate_image(raw)


def test_raw_byte_limit_is_independent_of_pixels() -> None:
    with pytest.raises(ImageRejectedError, match="too_large"):
        validate_image(_image("PNG") + b"x" * (2 * 1024 * 1024))
