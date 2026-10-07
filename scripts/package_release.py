#!/usr/bin/env python3
"""按固定白名单生成可重复的 P3 发行归档；不读取真实配置或访问镜像仓库。"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_APP_TOKEN = "@XW_APP_IMAGE@"  # noqa: S105 - image placeholder, not a secret
_POSTGRES_TOKEN = "@XW_POSTGRES_IMAGE@"  # noqa: S105 - image placeholder, not a secret
_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_PLATFORM = re.compile(r"^linux/(?:amd64|arm64)$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成小维 P3 发行归档")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--app-image", required=True)
    parser.add_argument("--postgres-image", required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--platform", required=True)
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if _IMAGE.fullmatch(args.app_image) is None:
        parser.error("--app-image 必须使用 sha256 digest")
    if _IMAGE.fullmatch(args.postgres_image) is None:
        parser.error("--postgres-image 必须使用 sha256 digest")
    if _SHA.fullmatch(args.code_sha) is None:
        parser.error("--code-sha 必须是 40 位小写 Git SHA")
    if _PLATFORM.fullmatch(args.platform) is None:
        parser.error("--platform 只接受 linux/amd64 或 linux/arm64")


def _members(args: argparse.Namespace) -> dict[str, bytes]:
    compose = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
    if compose.count(_APP_TOKEN) != 1 or compose.count(_POSTGRES_TOKEN) != 1:
        raise ValueError("Compose 镜像占位符不唯一")
    compose = compose.replace(_APP_TOKEN, args.app_image).replace(
        _POSTGRES_TOKEN, args.postgres_image
    )
    release = {
        "format_version": 1,
        "code_sha": args.code_sha,
        "platform": args.platform,
        "schema_version": 6,
        "direct_start_schema_versions": [6],
        "upgrade_schema_versions": [1, 2, 3, 4, 5],
        "config_migration": "none",
        "images": {"xiaowei": args.app_image, "postgres": args.postgres_image},
    }
    return {
        ".env.example": (ROOT / "deploy/.env.example").read_bytes(),
        "OPERATIONS.md": (ROOT / "deploy/OPERATIONS.md").read_bytes(),
        "compose.yaml": compose.encode(),
        "feishu-group.example.json": (ROOT / "examples/feishu-group.example.json").read_bytes(),
        "release.json": (json.dumps(release, ensure_ascii=False, indent=2) + "\n").encode(),
        "xiaowei.example.json": (ROOT / "examples/xiaowei.example.json").read_bytes(),
    }


def _write(output: Path, members: dict[str, bytes]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    for name in sorted(members):
                        payload = members[name]
                        info = tarfile.TarInfo(name)
                        info.size = len(payload)
                        info.mode = 0o644
                        info.mtime = 0
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        archive.addfile(info, io.BytesIO(payload))
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    _validate(parser, args)
    try:
        _write(args.output, _members(args))
    except (OSError, ValueError):
        parser.error("发行归档生成失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
