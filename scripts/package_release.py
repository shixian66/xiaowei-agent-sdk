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
import zlib
from collections.abc import Collection
from pathlib import Path
from typing import IO, Any, TypeGuard, cast

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
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_GZIP_MAGIC = b"\x1f\x8b"
_LEGACY_CONFIG = re.compile(r"^([0-9a-f]{64})\.json$")
_LEGACY_LAYER = re.compile(r"^[0-9a-f]{64}/layer\.tar$")
_IMAGE_NAME = "io.containerd.image.name"
_REF_NAME = "org.opencontainers.image.ref.name"
_MANIFEST_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
_CONFIG_TYPES = frozenset(
    {"application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json"}
)
# 层的媒体类型 → 是否 gzip。zstd 等其他压缩不在 docker save 的输出里，明确拒绝。
_LAYER_TYPES = {
    "application/vnd.oci.image.layer.v1.tar": False,
    "application/vnd.oci.image.layer.v1.tar+gzip": True,
    "application/vnd.docker.image.rootfs.diff.tar": False,
    "application/vnd.docker.image.rootfs.diff.tar.gzip": True,
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _open_member(archive: tarfile.TarFile, name: str) -> IO[bytes]:
    try:
        stream = archive.extractfile(name)
    except KeyError:
        stream = None
    if stream is None:
        raise ValueError(f"镜像归档缺少 {name}")
    return stream


def _load_json(archive: tarfile.TarFile, name: str) -> object:
    return json.load(_open_member(archive, name))


def _single(value: object, name: str) -> dict[str, Any]:
    if not (isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict)):
        raise ValueError(f"{name} 必须恰好描述一个镜像")
    return value[0]


def _sha256(stream: IO[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1 << 20), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _member_sha256(archive: tarfile.TarFile, name: str) -> str:
    return _sha256(_open_member(archive, name))


class _Hashing:
    """边读边算 SHA-256，让 tar 校验与 DiffID 只读一遍层内容。"""

    def __init__(self, stream: IO[bytes] | io.BufferedIOBase) -> None:
        self._stream = stream
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self.digest.update(data)
        return data


def _layer_diff_id(archive: tarfile.TarFile, name: str, compressed: bool | None) -> str:
    """层解压后的摘要（OCI DiffID），同时确认它是完整的 tar；Docker 解不开的层导入后不可用。

    ``compressed`` 为 OCI 媒体类型声明的压缩方式，须与内容一致；旧格式没有声明，按内容判断。
    """
    stream = _open_member(archive, name)
    gzipped = stream.read(2) == _GZIP_MAGIC
    stream.seek(0)
    _require(compressed is None or compressed == gzipped, "镜像层的压缩方式与媒体类型不符")
    reader = _Hashing(gzip.GzipFile(fileobj=stream) if gzipped else stream)
    # 流式读取逐个跳过成员：头部损坏或内容被截断时 tarfile 报 ReadError。
    with tarfile.open(fileobj=cast("IO[bytes]", reader), mode="r|") as layer:
        for _ in layer:
            pass
    while reader.read(1 << 20):
        pass
    return reader.digest.hexdigest()


def _descriptor(value: object, types: Collection[str], archive: tarfile.TarFile) -> str:
    """OCI 描述符：媒体类型受支持、摘要格式正确、对应 blob 存在且大小相符；返回 blob 路径。"""
    _require(isinstance(value, dict), "OCI 描述符格式不符")
    descriptor = cast("dict[str, Any]", value)
    media_type = descriptor.get("mediaType")
    _require(isinstance(media_type, str) and media_type in types, "不支持的 OCI 媒体类型")
    digest = descriptor.get("digest")
    matched = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    _require(matched is not None, "OCI 描述符的摘要格式不符")
    path = f"blobs/sha256/{cast('re.Match[str]', matched).group(1)}"
    try:
        member = archive.getmember(path)
    except KeyError:
        raise ValueError(f"镜像归档缺少 {path}") from None
    size = descriptor.get("size")
    _require(type(size) is int and member.isfile() and member.size == size, "OCI 描述符的大小不符")
    return path


def _is_schema_v2(document: dict[str, Any]) -> bool:
    """Docker 把 ``schemaVersion`` 解码为整数：``2.0`` 在 Python 里与 2 相等，导入时却报错。"""
    version = document.get("schemaVersion")
    return type(version) is int and version == 2


def _check_oci_index(
    archive: tarfile.TarFile, expected_tag: str, platform: str, config_name: str, layers: list[str]
) -> list[bool]:
    """Docker 25 起的归档另有 OCI 布局，containerd 镜像存储按它导入：标签、配置与层须和
    manifest.json 完全一致。只支持 index.json 直接指向单个镜像 manifest（docker save 的输出）；
    嵌套索引、多平台等其他布局明确拒绝。返回各层媒体类型声明的压缩方式。
    """
    _require(
        _load_json(archive, "oci-layout") == {"imageLayoutVersion": "1.0.0"},
        "oci-layout 版本不受支持",
    )
    index = _load_json(archive, "index.json")
    _require(isinstance(index, dict) and _is_schema_v2(index), "index.json 格式不符")
    entry = _single(cast("dict[str, Any]", index).get("manifests"), "index.json")
    name, reference = expected_tag.rsplit(":", 1)
    annotations = entry.get("annotations")
    _require(
        isinstance(annotations, dict)
        and annotations.get(_IMAGE_NAME) == f"docker.io/library/{name}:{reference}"
        and annotations.get(_REF_NAME) == reference,
        "镜像归档的标签与版本不符",
    )
    declared = entry.get("platform")
    if declared is not None:
        _require(
            isinstance(declared, dict)
            and f"{declared.get('os')}/{declared.get('architecture')}" == platform,
            "镜像平台与声明不符",
        )
    manifest_path = _descriptor(entry, _MANIFEST_TYPES, archive)
    _require(
        _member_sha256(archive, manifest_path) == manifest_path.rsplit("/", 1)[1],
        "OCI manifest 摘要不符",
    )
    manifest = _load_json(archive, manifest_path)
    _require(
        isinstance(manifest, dict)
        and _is_schema_v2(manifest)
        and manifest.get("mediaType", entry["mediaType"]) == entry["mediaType"],
        "OCI manifest 格式不符",
    )
    manifest = cast("dict[str, Any]", manifest)
    _require(
        _descriptor(manifest.get("config"), _CONFIG_TYPES, archive) == config_name,
        "index.json 与 manifest.json 指向的镜像配置不同",
    )
    descriptors = manifest.get("layers")
    if not isinstance(descriptors, list):
        raise ValueError("OCI manifest 的 layers 格式不符")
    paths = [_descriptor(layer, _LAYER_TYPES, archive) for layer in descriptors]
    _require(paths == layers, "index.json 与 manifest.json 指向的镜像层不同")
    return [_LAYER_TYPES[layer["mediaType"]] for layer in descriptors]


def _check_image_archive(path: Path, expected_tag: str, platform: str) -> None:
    """``docker save`` 归档：恰好一个镜像、标签为本次版本、引用内容齐全且摘要相符、平台与声明一致。

    支持两种格式：旧格式只有 ``manifest.json``；Docker 25 起另有 OCI 布局，按
    ``_check_oci_index`` 核对它与 ``manifest.json`` 一致，避免两种镜像存储导入出不同结果。
    内容寻址的文件按文件名核对摘要；每一层再按位置与配置 ``rootfs.diff_ids`` 核对解压后的摘要，
    并确认是完整的 tar。任一不符则服务器 ``docker load`` 会失败、解出错误内容或装上错误平台的
    镜像（解层失败时 ``docker load`` 仍可能退出 0）。
    """
    with tarfile.open(path, "r:") as archive:
        entry = _single(_load_json(archive, "manifest.json"), "manifest.json")
        _require(entry.get("RepoTags") == [expected_tag], "镜像归档的标签与版本不符")
        config_name, layers = entry.get("Config"), entry.get("Layers")
        _require(
            isinstance(config_name, str) and _is_str_list(layers),
            "manifest.json 的 Config/Layers 格式不符",
        )
        config_name, layers = cast("str", config_name), cast("list[str]", layers)
        names = archive.getnames()
        if "index.json" in names:
            compressed: list[bool | None] = list(
                _check_oci_index(archive, expected_tag, platform, config_name, layers)
            )
        else:
            _require(
                "oci-layout" not in names and all(_LEGACY_LAYER.fullmatch(n) for n in layers),
                "不支持的镜像归档格式",
            )
            compressed = [None] * len(layers)
        named = _BLOB.fullmatch(config_name) or _LEGACY_CONFIG.fullmatch(config_name)
        _require(
            named is not None and _member_sha256(archive, config_name) == named.group(1),
            "镜像配置缺失或摘要不符",
        )
        config = _load_json(archive, config_name)
        _require(isinstance(config, dict), "镜像配置格式不符")
        config = cast("dict[str, Any]", config)
        _require(
            f"{config.get('os')}/{config.get('architecture')}" == platform, "镜像平台与声明不符"
        )
        rootfs = config.get("rootfs")
        _require(
            isinstance(rootfs, dict)
            and rootfs.get("type") == "layers"
            and _is_str_list(rootfs.get("diff_ids")),
            "镜像配置的 rootfs 格式不符",
        )
        diff_ids = cast("dict[str, Any]", rootfs)["diff_ids"]
        _require(len(layers) == len(diff_ids), "镜像层数与配置不符")
        for layer, diff_id, gzipped in zip(layers, diff_ids, compressed, strict=True):
            blob = _BLOB.fullmatch(layer)
            _require(
                blob is None or _member_sha256(archive, layer) == blob.group(1),
                "镜像层缺失或摘要不符",
            )
            _require(
                f"sha256:{_layer_diff_id(archive, layer, gzipped)}" == diff_id,
                "镜像层与配置的 diff_ids 不符",
            )


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
    except ValueError as exc:
        parser.error(f"发行归档生成失败：{exc}")
    except (OSError, EOFError, zlib.error, tarfile.TarError):
        parser.error("发行归档生成失败：镜像归档无法读取或不是完整的 tar/gzip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
