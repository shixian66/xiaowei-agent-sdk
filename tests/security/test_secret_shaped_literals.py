"""源码里不得出现连续的、形如 secret 的字面量。

**这条测试针对的是一次真实事故的根因。** 仓库一直有一个约定：脱敏测试需要
"看起来像 secret"的输入才能证明脱敏有效，因此假字面量一律**拆开写**
（``"hunter" + "2-plain"``、``"gh" + "p_" + "A" * 36``）。但这个约定从未写进
任何文档，也不在 ADR-008 的四条本地命令覆盖范围内——它只由 CI 的 secret-scan
job 兜底。M2 有三处写成了连续形式，本地四条命令全绿、五轮复审全过，直到开 PR
才被 CI 拦下，而那时它们已经进了两个提交，无法在不改写评审锚点 SHA 的前提下
清除。

把约定变成本地 gate 内的机制，下一次同样的写法在 ``pytest -m security`` 就会
报红，而不是等到推送之后。

本测试**不复刻 gitleaks**：它只覆盖实际发生过的那一类形状（关键词 + ``=`` +
连续的高熵串）。CI 的 gitleaks 仍是全量真源，本测试是把最常踩的那一脚前移。
"""

import pathlib
import re

import pytest

pytestmark = pytest.mark.security

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCANNED = (ROOT / "src", ROOT / "tests")

# 关键词紧跟 = 再跟 12 位以上的连续字母数字——正是 gitleaks generic-api-key
# 在本仓库命中的形状。拆开写（"token=" + _FAKE）不会命中，这正是约定的写法。
_PATTERN = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|access[_-]?key|bearer)=[A-Za-z0-9]{12,}"
)


def _offenders() -> list[str]:
    found: list[str] = []
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == pathlib.Path(__file__).resolve():
                continue  # 本文件自带样例，见 detector 自测
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if _PATTERN.search(line):
                    found.append(f"{path.relative_to(ROOT)}:{lineno}")
    return found


def test_no_contiguous_secret_shaped_literal() -> None:
    assert _offenders() == [], (
        "以下位置出现了连续的 secret 形状字面量，会被 CI 的 secret-scan 拦下：\n"
        + "\n".join(_offenders())
        + '\n请按仓库约定拆开写，例如 "token=" + _FAKE。'
    )


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ('x = "token=abc123def456"', True),
        ('x = {"password=hunter2xxxxxxx": 1}', True),
        ('x = "api_key=AAAAAAAAAAAAAAAA"', True),
        ('x = "Bearer=0123456789abcdef"', True),
        # 约定写法：拆开后不再连续
        ('x = "token=" + _FAKE', False),
        ('_FAKE = "abc123" + "def456"', False),
        # 短值不构成 secret 形状，不得误报
        ('x = "token=ab"', False),
        # 散文里提到关键词同样不得误报
        ("# 只脱敏值会把 secret 留在键里", False),
    ],
)
def test_detector_shape(line: str, flagged: bool) -> None:
    """检测器自测。

    正例与反例都必须有：一个恒不命中的正则同样能让上面那条断言通过；一个过宽的
    正则会把合法的拆分写法和注释也判成违规，逼人绕开 gate。
    """
    assert bool(_PATTERN.search(line)) is flagged


def test_detector_actually_scans_files() -> None:
    """反证：扫描范围不能是空的。

    ``_offenders()`` 返回空列表既可能是"确实没有违规"，也可能是"一个文件都没扫
    到"。这里断言扫描确实覆盖了实际存在的源文件。
    """
    scanned = [p for root in SCANNED for p in root.rglob("*.py")]
    assert len(scanned) > 50
