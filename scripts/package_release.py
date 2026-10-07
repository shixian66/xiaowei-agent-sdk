#!/usr/bin/env python3
"""按固定白名单生成可重复的发行归档；应用镜像以 ``docker save`` 文件放进归档。

服务器离线 ``docker load``。

不读取真实配置，也不访问镜像仓库。应用镜像的名字固定为 ``xiaowei:<代码 SHA>``。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
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
IMAGE_FILE = "xiaowei-image.tar"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_PLATFORM = re.compile(r"^linux/(?:amd64|arm64)$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成小维发行归档")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-archive", type=Path, required=True, help="docker save 的输出")
    parser.add_argument("--postgres-image", required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--platform", required=True)
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if _IMAGE.fullmatch(args.postgres_image) is None:
        parser.error("--postgres-image 必须使用 sha256 digest")
    if _SHA.fullmatch(args.code_sha) is None:
        parser.error("--code-sha 必须是 40 位小写 Git SHA")
    if _PLATFORM.fullmatch(args.platform) is None:
        parser.error("--platform 只接受 linux/amd64 或 linux/arm64")


def app_image(code_sha: str) -> str:
    return f"xiaowei:{code_sha}"


_BLOB = re.compile(r"^blobs/sha256/([0-9a-f]{64})$")
_LEGACY_CONFIG = re.compile(r"^([0-9a-f]{64})\.json$")


def _member_sha256(archive: tarfile.TarFile, name: str) -> str:
    try:
        stream = archive.extractfile(name)
    except KeyError:
        stream = None
    if stream is None:
        raise ValueError("镜像归档缺少 manifest 引用的内容")
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1 << 20), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _check_image_archive(path: Path, expected_tag: str, platform: str) -> None:
    """``docker save`` 归档：恰好一个镜像、标签为本次版本、引用内容齐全且摘要相符、平台与声明一致。

    内容寻址的文件（``blobs/sha256/<摘要>``、旧格式 ``<摘要>.json``）按文件名核对；其他层按配置
    ``rootfs.diff_ids`` 核对。任一不符则服务器 ``docker load`` 会失败或装上错误平台的镜像。
    """
    with tarfile.open(path, "r:") as archive:
        member = archive.extractfile("manifest.json")
        if member is None:
            raise ValueError("镜像归档缺少 manifest.json")
        manifest = json.load(member)
        if not (isinstance(manifest, list) and len(manifest) == 1):
            raise ValueError("镜像归档必须恰好含一个镜像")
        entry = manifest[0]
        if entry.get("RepoTags") != [expected_tag]:
            raise ValueError("镜像归档的标签与版本不符")
        config_name, layers = entry["Config"], entry["Layers"]
        named = _BLOB.fullmatch(config_name) or _LEGACY_CONFIG.fullmatch(config_name)
        if named is None or _member_sha256(archive, config_name) != named.group(1):
            raise ValueError("镜像配置缺失或摘要不符")
        config_stream = archive.extractfile(config_name)
        if config_stream is None:
            raise ValueError("镜像配置缺失或摘要不符")
        config = json.load(config_stream)
        if f"{config.get('os')}/{config.get('architecture')}" != platform:
            raise ValueError("镜像平台与声明不符")
        # 层数与配置不符时 strict zip 抛 ValueError，同样拒绝。
        for layer, diff_id in zip(layers, config["rootfs"]["diff_ids"], strict=True):
            blob = _BLOB.fullmatch(layer)
            expected = blob.group(1) if blob else str(diff_id).removeprefix("sha256:")
            if _member_sha256(archive, layer) != expected:
                raise ValueError("镜像层缺失或摘要不符")


def _image_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _members(args: argparse.Namespace) -> dict[str, bytes | Path]:
    image = app_image(args.code_sha)
    _check_image_archive(args.image_archive, image, args.platform)
    compose = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
    if compose.count(_APP_TOKEN) != 1 or compose.count(_POSTGRES_TOKEN) != 1:
        raise ValueError("Compose 镜像占位符不唯一")
    compose = compose.replace(_APP_TOKEN, image).replace(_POSTGRES_TOKEN, args.postgres_image)
    release = {
        "format_version": 2,
        "code_sha": args.code_sha,
        "platform": args.platform,
        "schema_version": 6,
        "direct_start_schema_versions": [6],
        "upgrade_schema_versions": [1, 2, 3, 4, 5],
        "config_migration": "none",
        "images": {"xiaowei": image, "postgres": args.postgres_image},
        "image_file": IMAGE_FILE,
        "image_file_sha256": _image_sha256(args.image_archive),
    }
    return {
        ".env.example": (ROOT / "deploy/.env.example").read_bytes(),
        "OPERATIONS.md": (ROOT / "deploy/OPERATIONS.md").read_bytes(),
        "compose.yaml": compose.encode(),
        "feishu-group.example.json": (ROOT / "examples/feishu-group.example.json").read_bytes(),
        "release.json": (json.dumps(release, ensure_ascii=False, indent=2) + "\n").encode(),
        "xiaowei.example.json": (ROOT / "examples/xiaowei.example.json").read_bytes(),
        IMAGE_FILE: args.image_archive,
    }


def _write(output: Path, members: dict[str, bytes | Path]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    for name in sorted(members):
                        payload = members[name]
                        info = tarfile.TarInfo(name)
                        info.mode = 0o644
                        info.mtime = 0
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        if isinstance(payload, Path):
                            # 镜像文件较大：按流写入，不整体读进内存。
                            info.size = payload.stat().st_size
                            with payload.open("rb") as stream:
                                archive.addfile(info, stream)
                        else:
                            info.size = len(payload)
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
    except (OSError, ValueError, tarfile.TarError, AttributeError, KeyError, TypeError):
        parser.error("发行归档生成失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
