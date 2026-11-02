"""S3 独立审查修复的回归：新模型输出预算对同样内容仍然生效。"""


import pytest
from pydantic import ValidationError
from tests.sdk_core.test_channel_service import env as env

from xiaowei.models import (
    AgentAnswer,
    FactBlock,
    TextBlock,
)

pytestmark = pytest.mark.loopback

CHAT = "oc_d1a"
# 存储上限明确容纳原记录：失败只能来自回答适配，不能来自请求表容量。
STORE_BYTES = 64_000

SHAPES = {
    # 一条事实 + 三段各 2,000 汉字的关联分析：模型文字累计 18,000 字节。
    "18000_bytes": ["甲" * 2000, "乙" * 2000, "丙" * 2000],
    # 一条事实 + 64 段短分析：按块计共 65 块。
    "65_blocks": [f"第{i}段关联分析" for i in range(64)],
}


# ---- 修复一：旧回答不被新模型输出预算拒绝 ---------------------------------------------------


@pytest.mark.parametrize("texts", SHAPES.values(), ids=SHAPES.keys())
def test_new_output_budget_still_rejects_the_same_content(texts: list[str]) -> None:
    """新模型输出的 16,000 字节 / 64 块上限不变：同样内容作为新回答仍被拒绝（可改写）。"""
    blocks = (
        FactBlock(type="fact", evidence_id="e1", view="full"),
        *(TextBlock(type="text", text=t) for t in texts),
    )
    with pytest.raises(ValidationError):
        AgentAnswer(blocks=blocks, report_format=None, awaiting_user=False)




