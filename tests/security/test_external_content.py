"""外部文本恒为不可信，摘要不可伪造。"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import ExternalContent, ExternalSource, TrustLevel

pytestmark = pytest.mark.security

_AT = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)
_HELLO_SHA256 = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source": ExternalSource.TOOL,
        "trust": TrustLevel.UNTRUSTED,
        "content": "hello",
        "digest": _HELLO_SHA256,
        "captured_at": _AT,
    }
    return base | overrides


def test_capture_computes_digest_and_pins_trust() -> None:
    got = ExternalContent.capture(source=ExternalSource.TOOL, content="hello", captured_at=_AT)
    assert got.digest == _HELLO_SHA256
    assert got.trust is TrustLevel.UNTRUSTED


def test_trust_cannot_be_set_to_anything_else() -> None:
    with pytest.raises(ValidationError):
        ExternalContent(**_payload(trust="trusted"))


def test_forged_digest_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExternalContent(**_payload(digest="0" * 64))


def test_undeclared_field_is_rejected() -> None:
    """外部文本不得夹带 policy / 权限 / 计划字段。"""
    with pytest.raises(ValidationError):
        ExternalContent(**_payload(policy_revision="r1"))


def test_capture_rejects_undeclared_keyword() -> None:
    """capture 是关键字受限的类方法，多传参数抛 TypeError（而非 ValidationError）。"""
    with pytest.raises(TypeError):
        ExternalContent.capture(  # type: ignore[call-arg]
            source=ExternalSource.WEB,
            content="x",
            captured_at=_AT,
            policy_revision="r1",
        )


def test_copy_cannot_forge_the_digest() -> None:
    """改内容不改摘要，必须在 copy 时就被抓住。"""
    original = ExternalContent.capture(source=ExternalSource.TOOL, content="hello", captured_at=_AT)
    with pytest.raises(ValidationError):
        original.model_copy(update={"content": "tampered"})
