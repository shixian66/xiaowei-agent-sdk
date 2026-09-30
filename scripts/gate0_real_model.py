"""P1-B Gate 0 真实模型命令：对一个获准 Profile 运行固定样例，输出不含内容的 JSON 记录。

在仓库根目录运行（测试 PostgreSQL 由 ``compose.sdk-test.yml`` 启动）::

    SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \\
      uv run --locked --extra dev python -m scripts.gate0_real_model --profile PROFILE.json

``PROFILE.json`` 是不含凭据的 ``ModelProfile``；其 ``api_key_ref`` 指向的环境变量须已在本机
设置。缺任何配置时以退出码 2 失败，不静默跳过；样例未全部通过时退出码为 1。

本命令会把合成数据发送到 Profile 的端点，只在用户授权该端点后运行。装配与样例见
``tests/sdk_core/gate0.py``：产品路径不变，只把最底层 transport 换成不重试、不读环境配置的
网络 transport，并记录每次请求的元数据。输出不含消息、模型文字、工具结果或凭据。
"""

import os

# 必须早于任何 ``agents`` 导入：SDK 在导入时读取这两个开关，外部预置为 0/false 也不生效。
os.environ["OPENAI_AGENTS_DONT_LOG_MODEL_DATA"] = "1"
os.environ["OPENAI_AGENTS_DONT_LOG_TOOL_DATA"] = "1"

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import httpx2
from pydantic import ValidationError
from tests.sdk_core.gate0 import gate0_app, gate_passed, run_samples
from tests.sdk_core.postgres_harness import (
    HarnessMisconfiguredError,
    isolated_database,
    parse_admin_url,
)
from tests.sdk_core.synthetic_tools import ready_engine

from xiaowei.config import SecretRefError, configure_runtime, resolve_secret_ref
from xiaowei.model_api import ModelProfile, profile_fingerprint

POSTGRES_URL_ENV = "SDK_TEST_POSTGRES_URL"
EXIT_FAILED, EXIT_MISCONFIGURED = 1, 2


class ConfigurationError(Exception):
    """运行前提不满足；消息不含凭据或地址。"""


def load_profile(path: Path) -> ModelProfile:
    try:
        profile = ModelProfile.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError:
        raise ConfigurationError("无法读取 Profile 文件") from None
    except ValidationError:
        raise ConfigurationError("Profile 文件不是合法的 ModelProfile") from None
    try:
        resolve_secret_ref(profile.api_key_ref)
    except SecretRefError as exc:
        raise ConfigurationError(str(exc)) from None
    return profile


def check_postgres() -> None:
    raw = os.environ.get(POSTGRES_URL_ENV)
    if not raw:
        raise ConfigurationError(f"{POSTGRES_URL_ENV} 未设置：需要隔离测试 PostgreSQL")
    try:
        parse_admin_url(raw)
    except HarnessMisconfiguredError as exc:
        raise ConfigurationError(f"{POSTGRES_URL_ENV} {exc}") from None


async def run(
    profile: ModelProfile, *, network: httpx2.AsyncBaseTransport | None = None
) -> dict[str, object]:
    """``network`` 只供离线用例注入 mock；缺省为不重试、不读环境配置的网络 transport。"""
    configure_runtime()
    started = datetime.now(UTC)
    network = network or httpx2.AsyncHTTPTransport(retries=0, trust_env=False)
    async with (
        isolated_database() as url,
        ready_engine(url) as engine,
        gate0_app(profile, engine, network=network, clock=lambda: datetime.now(UTC)) as gate,
    ):
        results = await run_samples(gate)
    return {
        "gate": "P1-B Gate 0",
        "started_at": started.isoformat(),
        "sdk": {"openai-agents": version("openai-agents"), "openai": version("openai")},
        "profile": {
            "profile_id": profile.profile_id,
            "provider": profile.provider,
            "host": httpx2.URL(profile.base_url).host,
            "api_mode": profile.api_mode,
            "model": profile.model,
            "output_mode": profile.output_mode,
            "fingerprint": profile_fingerprint(profile),
        },
        "passed": gate_passed(results),
        "samples": [r.report() for r in results],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", type=Path, required=True, help="不含凭据的 ModelProfile JSON")
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
        check_postgres()
    except ConfigurationError as exc:
        print(f"gate0: {exc}", file=sys.stderr)
        return EXIT_MISCONFIGURED
    report = asyncio.run(run(profile))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
