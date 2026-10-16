"""三档字符串语义的行为边界。

上一个文件用 AST 保证"每个字段都选了一档";这个文件保证"三档确实不同",否则
分档退化成命名习惯。每一档都同时有正例与反例。
"""

import datetime as _dt

import pytest
from pydantic import ValidationError
from tests.conftest import make_envelope

from xiaowei_agent.contracts import (
    AgentError,
    ApprovalRequest,
    ApprovalState,
    ErrorCategory,
    ExternalContent,
    ExternalSource,
    RenderPayload,
    RenderSection,
    TaskStatus,
)

pytestmark = pytest.mark.security

NOW = _dt.datetime(2026, 9, 2, tzinfo=_dt.UTC)
_HEX = "0" * 64

_APPROVAL = {
    "task_id": "t1",
    "step_id": "s1",
    "plan_hash": _HEX,
    "target_fingerprint": _HEX,
    "policy_revision": "policy-2026-09-01",
    "subject": "approver",
    "expires_at": NOW,
    "state": ApprovalState.PENDING,
}

_ERR = {
    "code": "tool.timeout",
    "category": ErrorCategory.TIMEOUT,
    "retryable": True,
    "message_key": "err.tool.timeout",
}


# --- StrictStr:标识符与引用 -------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", " ref-1", "ref-1 ", "\tref-1\n"])
def test_reference_fields_reject_empty_and_padded(bad: str) -> None:
    """空串与带空白的引用都必须拒绝。

    ``cause_ref=""`` 是最危险的一个:它在结构上"存在"(不是 None),却在所有
    ``if err.cause_ref:`` 判断里"不存在"。这种二义性让"有原因但拿不到"和"没有
    原因"无法区分,正是 fail-closed 要消灭的状态。
    """
    with pytest.raises(ValidationError):
        AgentError(**_ERR, cause_ref=bad)


def test_reference_fields_still_accept_none_and_clean_values() -> None:
    assert AgentError(**_ERR, cause_ref=None).cause_ref is None
    assert AgentError(**_ERR, cause_ref="ref-1").cause_ref == "ref-1"


def test_padded_reference_is_not_silently_stripped() -> None:
    """必须拒绝,不能悄悄 strip。

    如果改成 strip,``" r1 "`` 与 ``"r1"`` 会静默合流成同一个引用——攻击者可以
    用任意空白变体绕开"这个 ref 已用过"之类的去重检查。
    """
    with pytest.raises(ValidationError):
        AgentError(**_ERR, cause_ref="  r1  ")


# --- NonEmptyText:本系统生成的文本 -----------------------------------------


def test_generated_text_rejects_empty() -> None:
    # 先证明同样的构造在 body 合法时能成功,否则"拒绝"可能来自别的缺失字段
    assert RenderSection(title="t", body="ok", refs=()).body == "ok"
    with pytest.raises(ValidationError):
        RenderSection(title="t", body="", refs=())
    assert make_envelope(text="ok").text == "ok"
    with pytest.raises(ValidationError):
        make_envelope(text="")


def test_generated_text_preserves_padding() -> None:
    """与 StrictStr 的关键差别:不做 strip 校验。

    正文的前后空白属于内容本身(代码块缩进、对齐)。用 StrictStr 会把合法正文
    判成非法——这就是不能一刀切的原因。
    """
    assert RenderSection(title="t", body="  缩进正文  ", refs=()).body == "  缩进正文  "
    assert make_envelope(text="  查询为什么慢  ").text == "  查询为什么慢  "


def test_generated_text_tuples_reject_empty_entries() -> None:
    ok = {"answer": "a", "sections": (), "status": TaskStatus.SUCCEEDED, "refs": ()}
    assert RenderPayload(**ok, next_steps=("下一步",)).next_steps == ("下一步",)
    with pytest.raises(ValidationError):
        RenderPayload(**ok, next_steps=("",))


# --- FreeText:外部不可信文本逐字捕获 ---------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", "  含空白的工具输出  ", "\n\n"])
def test_external_content_is_captured_verbatim(raw: str) -> None:
    """外部文本一个字节都不许改。

    空串与纯空白都是合法的工具输出。任何收紧(非空、strip)都会篡改被捕获的原文,
    并让 digest 与真实来源对不上——取证链就断在这里。
    """
    captured = ExternalContent.capture(source=ExternalSource.TOOL, content=raw, captured_at=NOW)
    assert captured.content == raw


@pytest.mark.parametrize("bad", [b"\x00binary", 123, None, ["list"], 1.5])
def test_capture_rejects_non_text_fail_closed(bad: object) -> None:
    """必须是 ValidationError,不是 AttributeError。

    根因是"先派生后校验":``content_digest`` 直接调 ``content.encode()``,在字段
    校验之前就炸。``AttributeError`` 是解释器级意外,调用方无法与"真的算不出摘要"
    区分;更糟的是 Gateway 的异常处理器自身会调用 ``capture``,在那里冒出
    AttributeError 会直接逃出刚加固过的 except 分支。
    """
    with pytest.raises(ValidationError):
        ExternalContent.capture(
            source=ExternalSource.TOOL,
            content=bad,  # type: ignore[arg-type]
            captured_at=NOW,
        )


def test_capture_does_not_raise_attribute_error_for_bytes() -> None:
    """把修复前的确切症状钉死,防止回归成裸 AttributeError。"""
    with pytest.raises(ValidationError):
        ExternalContent.capture(
            source=ExternalSource.TOOL,
            content=b"payload",  # type: ignore[arg-type]
            captured_at=NOW,
        )


# --- Sha256Hex:摘要形状 ------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "different",  # 旧标注下能构造成功
        "A" * 64,  # 大写
        "0" * 63,  # 短一位
        "0" * 65,  # 长一位
        "g" * 64,  # 非十六进制
        "0" * 64 + "\n",  # 尾随换行:锚点写成 $ 时会漏
    ],
)
def test_digest_shaped_fields_reject_malformed(bad: str) -> None:
    """形状必须在**构造**时拒绝。

    ``plan_hash="different"`` 旧标注下能构造成功,只在后续比对时表现为"不匹配"
    ——与"计划确实变了"是同一个信号,排查时分不开。

    **必须在 ``ApprovalRequest`` 上测,不能在 ``ExternalContent.digest`` 上测**:
    后者有 ``_digest_must_match`` 重算比对,任何畸形值都会先被那条校验器拦下,
    ``Sha256Hex`` 是否承重完全看不出来。变异测试正是这样抓到本条测试原本空转的
    (拆掉 Sha256Hex 后测试仍全绿)。
    """
    with pytest.raises(ValidationError):
        ApprovalRequest(**{**_APPROVAL, "plan_hash": bad})
    with pytest.raises(ValidationError):
        ApprovalRequest(**{**_APPROVAL, "target_fingerprint": bad})


def test_digest_shaped_fields_accept_a_real_digest() -> None:
    """正例:证明上一条的拒绝来自形状,而不是别的必填字段缺失。"""
    assert ApprovalRequest(**_APPROVAL).plan_hash == _HEX


def test_external_content_digest_mismatch_is_still_caught() -> None:
    """形状合法但内容不符,仍必须被重算比对拒绝——两道防线都要在。"""
    with pytest.raises(ValidationError):
        ExternalContent(source=ExternalSource.TOOL, content="x", digest=_HEX, captured_at=NOW)
