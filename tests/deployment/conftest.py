"""P3-A 发行测试必须实际使用 Docker；缺少引擎或构建失败即测试失败。"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def docker(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("docker")
    assert binary is not None, "P3-A 发行检查需要 Docker Engine"
    return subprocess.run(  # noqa: S603 - 只执行 PATH 解析出的 Docker CLI
        [binary, *args], cwd=cwd, capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="session")
def runtime_image() -> Iterator[str]:
    tag = f"xiaowei-p3-a-test:{os.getpid()}"
    result = docker(
        "build",
        "--pull=false",
        "--build-arg",
        "XW_CODE_SHA=" + "d" * 40,
        "-t",
        tag,
        "-f",
        "Dockerfile.runtime",
        ".",
    )
    assert result.returncode == 0, "P3-A 运行镜像构建失败"
    try:
        yield tag
    finally:
        docker("image", "rm", "-f", tag)
