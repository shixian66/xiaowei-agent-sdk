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
import sys
from pathlib import Path

import httpx2
from tests.sdk_core.dialogue_gate import ReviewSample, ReviewVerdict, run_dialogue
from tests.sdk_core.postgres_harness import isolated_database

import xiaowei
from scripts.gate0_real_model import ConfigurationError, check_postgres, load_profile
from xiaowei.model_api import profile_fingerprint

ROOT = Path(__file__).resolve().parents[1]
REVIEW_LABELS = {
    "intent_preserved": "保持用户意图",
    "execution_allowed": "没有违背禁止执行的要求",
    "object_selection_correct": "选择了正确集群与对象",
    "evidence_consistent": "事实与依据一致",
    "clarification_appropriate": "澄清与是否执行合理",
    "limitations_stated": "表达了口径、分页或结果限制",
    "history_delivery_claim_correct": "没有把历史生成说成已发送或已读",
}


async def review_sample(sample: ReviewSample) -> ReviewVerdict:
    """即时查看合成内容；stdout 的机器报告只保留固定判定，不保存这里的正文。"""
    print(f"\n{sample.scenario} / {sample.repeat} / {sample.turn} ({sample.mode})", file=sys.stderr)
    # 同一 harness 必须能在尚无 U2 渲染器的基线运行；终端控制符交给标准 JSON 转义。
    print("合成请求：" + json.dumps(sample.message, ensure_ascii=False), file=sys.stderr)
    print(f"state={sample.state} failure={sample.failure}", file=sys.stderr)
    print("按目标记录的实际 I/O：", file=sys.stderr)
    print(json.dumps(sample.statements, ensure_ascii=False, indent=2), file=sys.stderr)
    print("已校验的交付：", file=sys.stderr)
    print(
        "无交付"
        if sample.delivery is None
        else json.dumps(sample.delivery.model_dump(mode="json"), ensure_ascii=False, indent=2),
        file=sys.stderr,
    )
    values: dict[str, bool | None] = {}
    for field, label in REVIEW_LABELS.items():
        while True:
            print(f"{label} [y=是/n=否/a=不适用]：", end="", file=sys.stderr, flush=True)
            raw = await asyncio.to_thread(sys.stdin.readline)
            if not raw:
                raise ConfigurationError("人工核对未完成，不生成合入证据")
            answer = raw.strip().lower()
            if answer in {"y", "n", "a"}:
                values[field] = {"y": True, "n": False, "a": None}[answer]
                break
    return ReviewVerdict(**values)


async def collect(profile_path: Path, repeats: int) -> dict[str, object]:
    sha = source_sha()
    harness = harness_sha256()
    profile = load_profile(profile_path)
    check_postgres()
    async with isolated_database() as url:
        results = await run_dialogue(
            profile,
            url,
            lambda: httpx2.AsyncHTTPTransport(retries=0, trust_env=False),
            repeats=repeats,
            manual_review=review_sample,
        )
    if source_sha() != sha or harness_sha256() != harness:
        raise ConfigurationError("运行期间产品或评估框架版本发生变化，不生成合入证据")
    return {
        "source_sha": sha,
        "harness_sha256": harness,
        "profile_id": profile.profile_id,
        "profile_fingerprint": profile_fingerprint(profile),
        "repeats": repeats,
        "samples": results,
        "merge_gate": "需要逐项独立比较自然语言、证据与行为；采集成功不是验收通过",
    }


def source_sha() -> str:
    """绑定实际导入的产品树；调用前及采集后都要求精确、无本地修改的 Git 版本。"""
    git = shutil.which("git")
    if git is None:
        raise ConfigurationError("缺少 Git，不能记录精确版本")
    package = Path(xiaowei.__file__).resolve()

    def read(*args: str, cwd: Path) -> str:
        try:
            return subprocess.run(  # noqa: S603 -- resolved Git; fixed read-only arguments, no shell.
                [git, *args], cwd=cwd, check=True, text=True, capture_output=True, timeout=5
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raise ConfigurationError("无法核实产品 Git 版本，不生成合入证据") from None

    root = Path(read("rev-parse", "--show-toplevel", cwd=package.parent))
    if package != root / "src/xiaowei/__init__.py":
        raise ConfigurationError("实际导入的产品不在 Git 源码目录，不生成合入证据")
    if read("status", "--porcelain", "--untracked-files=all", cwd=root):
        raise ConfigurationError("产品源码树有未提交修改，不生成合入证据")
    return read("rev-parse", "HEAD", cwd=root)


def harness_sha256() -> str:
    harness = hashlib.sha256()
    for path in (
        "tests/sdk_core/gate0.py",
        "tests/sdk_core/dialogue_gate.py",
        "scripts/dialogue_real_model.py",
    ):
        harness.update((ROOT / path).read_bytes())
    return harness.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("旧/新每项至少各运行 3 次")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print("U1 gate: 需要交互终端逐条核对；仅 stdout 可重定向为无内容报告", file=sys.stderr)
        return 2
    try:
        report = asyncio.run(collect(args.profile, args.repeats))
    except ConfigurationError as exc:
        print(f"U1 gate: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
