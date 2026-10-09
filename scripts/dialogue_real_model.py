"""获准后才运行的 U1 新旧对照采集；同一 harness 在两个工作树各跑 >=3 次。"""

import os

os.environ["OPENAI_AGENTS_DONT_LOG_MODEL_DATA"] = "1"
os.environ["OPENAI_AGENTS_DONT_LOG_TOOL_DATA"] = "1"

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import httpx2
from tests.sdk_core.dialogue_gate import run_dialogue
from tests.sdk_core.postgres_harness import isolated_database

from scripts.gate0_real_model import ConfigurationError, check_postgres, load_profile


async def collect(profile_path: Path, repeats: int) -> dict[str, object]:
    profile = load_profile(profile_path)
    check_postgres()
    async with isolated_database() as url:
        results = await run_dialogue(
            profile,
            url,
            lambda: httpx2.AsyncHTTPTransport(retries=0, trust_env=False),
            repeats=repeats,
        )
    git = shutil.which("git")
    if git is None:
        raise ConfigurationError("缺少 Git，不能记录精确版本")
    sha = subprocess.run(  # noqa: S603 -- resolved Git with fixed, read-only arguments; no shell.
        [git, "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    harness = hashlib.sha256()
    for path in (
        "tests/sdk_core/gate0.py",
        "tests/sdk_core/dialogue_gate.py",
        "scripts/dialogue_real_model.py",
    ):
        harness.update(Path(path).read_bytes())
    return {
        "source_sha": sha,
        "harness_sha256": harness.hexdigest(),
        "profile_id": profile.profile_id,
        "repeats": repeats,
        "samples": results,
        "merge_gate": "需要逐项独立比较自然语言、证据与行为；采集成功不是验收通过",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("旧/新每项至少各运行 3 次")
    try:
        report = asyncio.run(collect(args.profile, args.repeats))
    except ConfigurationError as exc:
        print(f"U1 gate: {exc}")
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
