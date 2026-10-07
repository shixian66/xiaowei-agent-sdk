"""P3-A 发行归档：固定白名单、不可变镜像引用与可重复内容。"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from tests.deployment.conftest import docker

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/package_release.py"
POSTGRES_IMAGE = "postgres:16.15-bookworm@sha256:" + "b" * 64
CODE_SHA = "c" * 40
APP_IMAGE = f"xiaowei:{CODE_SHA}"
IMAGE_FILE = "xiaowei-image.tar"
MEMBERS = {
    ".env.example",
    "OPERATIONS.md",
    "compose.yaml",
    "feishu-group.example.json",
    "release.json",
    IMAGE_FILE,
    "xiaowei.example.json",
}
# 发行包里操作者看得到的内容不出现代码托管平台或开发框架的名字。
FORBIDDEN_NAMES = ("github", "ghcr", "sdk")


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def image_archive(
    path: Path,
    tags: list[str] | None = None,
    *,
    platform: str = "linux/arm64",
    drop: tuple[str, ...] = (),
    corrupt: tuple[str, ...] = (),
    layers: list[str] | None = None,
    gzip_layers: bool = False,
    swap_layers: bool = False,
    recompress: bool = False,
) -> Path:
    """与 ``docker save`` 同构的最小镜像归档：内容寻址的配置与两层，摘要都真实可校验。

    ``drop`` / ``corrupt`` 取 ``"config"``、``"layer"``（第一层），构造缺失或内容被改的归档；被改的
    配置仍是同平台的合法 JSON，只有摘要能发现它。``layers`` 覆盖 manifest 中的层列表。
    ``gzip_layers`` 以 gzip 存层（文件名是压缩后摘要，``diff_ids`` 是解压后摘要）；``swap_layers``
    只交换 manifest 中两层的顺序，层文件与配置不变；``recompress`` 把第一层换成同内容、不同压缩
    级别的 gzip（``diff_ids`` 仍对，只有文件名摘要不符）。
    """
    raw = [_tar_bytes({f"etc/xiaowei-{name}": name.encode()}) for name in ("a", "b")]
    stored = [gzip.compress(layer, mtime=0) if gzip_layers else layer for layer in raw]
    names = [f"blobs/sha256/{hashlib.sha256(blob).hexdigest()}" for blob in stored]
    os_name, architecture = platform.split("/")
    config = json.dumps(
        {
            "architecture": architecture,
            "os": os_name,
            "rootfs": {
                "type": "layers",
                "diff_ids": [f"sha256:{hashlib.sha256(layer).hexdigest()}" for layer in raw],
            },
        }
    ).encode()
    config_name = f"blobs/sha256/{hashlib.sha256(config).hexdigest()}"
    blobs = {config_name: config, **dict(zip(names, stored, strict=True))}
    if recompress:
        blobs[names[0]] = gzip.compress(raw[0], compresslevel=1, mtime=0)
        assert blobs[names[0]] != stored[0]
    for kind, name in (("config", config_name), ("layer", names[0])):
        if kind in corrupt:
            blobs[name] = (
                config.replace(b"{", b'{"tampered": true, ', 1)
                if kind == "config"
                else blobs[name] + b"x"
            )
        if kind in drop:
            del blobs[name]
    manifest = [
        {
            "Config": config_name,
            "RepoTags": [APP_IMAGE] if tags is None else tags,
            "Layers": layers if layers is not None else names[::-1] if swap_layers else names,
        }
    ]
    path.write_bytes(_tar_bytes({"manifest.json": json.dumps(manifest).encode(), **blobs}))
    return path


def package(
    output: Path, platform: str = "linux/arm64", *, image: Path | None = None
) -> subprocess.CompletedProcess[str]:
    if image is None:
        image = image_archive(output.with_name(f"{output.name}.image.tar"))
    return subprocess.run(  # noqa: S603 - 固定 Python 与仓库脚本
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--image-archive",
            str(image),
            "--postgres-image",
            POSTGRES_IMAGE,
            "--code-sha",
            CODE_SHA,
            "--platform",
            platform,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_archive_has_only_the_deployment_contract(tmp_path: Path) -> None:
    sentinel = ROOT / "p3-release-sentinel.tmp"
    sentinel.write_text("must not be packaged", encoding="utf-8")
    image = image_archive(tmp_path / "image.tar")
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    try:
        assert package(first, image=image).returncode == 0
        assert package(second, image=image).returncode == 0
    finally:
        sentinel.unlink(missing_ok=True)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        assert set(archive.getnames()) == MEMBERS
        extracted = {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile()
        }

    for example in ("xiaowei.example.json", "feishu-group.example.json"):
        assert extracted[example] == (ROOT / "examples" / example).read_bytes()
    assert extracted[IMAGE_FILE] == image.read_bytes()
    compose = yaml.safe_load(extracted["compose.yaml"])
    app = compose["services"]["xiaowei"]
    assert app["image"] == APP_IMAGE and app["pull_policy"] == "never"
    assert compose["services"]["postgres"]["image"] == POSTGRES_IMAGE
    metadata = json.loads(extracted["release.json"])
    assert metadata == {
        "format_version": 2,
        "code_sha": CODE_SHA,
        "platform": "linux/arm64",
        "schema_version": 6,
        "direct_start_schema_versions": [6],
        "upgrade_schema_versions": [1, 2, 3, 4, 5],
        "config_migration": "none",
        "images": {"xiaowei": APP_IMAGE, "postgres": POSTGRES_IMAGE},
        "image_file": IMAGE_FILE,
        "image_file_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
    }
    forbidden = (
        "AGENTS.md",
        "ARCHITECTURE.md",
        "AGENT_HANDOFF.md",
        "README.md",
        "DEVELOPMENT_PLAN.md",
    )
    assert not any(name in extracted for name in forbidden)
    assert "p3-release-sentinel.tmp" not in extracted


def test_operator_visible_files_name_no_hosting_platform_or_framework(tmp_path: Path) -> None:
    output = tmp_path / "release.tar.gz"
    assert package(output).returncode == 0
    with tarfile.open(output, "r:gz") as archive:
        for member in archive.getmembers():
            if member.name == IMAGE_FILE:
                continue
            text = archive.extractfile(member).read().decode("utf-8").lower()
            assert not [word for word in FORBIDDEN_NAMES if word in text], member.name
    assert not [word for word in FORBIDDEN_NAMES if word in output.name.lower()]


@pytest.mark.parametrize(
    "tags",
    [[], ["xiaowei:latest"], [APP_IMAGE, "xiaowei:latest"], ["other/xiaowei:" + CODE_SHA]],
)
def test_release_rejects_an_image_archive_with_other_tags(tmp_path: Path, tags: list[str]) -> None:
    output = tmp_path / "bad.tar.gz"
    result = package(output, image=image_archive(tmp_path / "image.tar", tags))
    assert result.returncode == 2 and not output.exists()


@pytest.mark.parametrize(
    ("drop", "corrupt"),
    [(("config",), ()), (("layer",), ()), ((), ("config",)), ((), ("layer",))],
    ids=["missing config", "missing layer", "corrupt config", "corrupt layer"],
)
def test_release_rejects_an_image_archive_with_missing_or_corrupt_content(
    tmp_path: Path, drop: tuple[str, ...], corrupt: tuple[str, ...]
) -> None:
    """标签正确但内容缺失或被改：``docker load`` 会失败，发行包不能生成。"""
    output = tmp_path / "bad.tar.gz"
    image = image_archive(tmp_path / "image.tar", drop=drop, corrupt=corrupt)
    result = package(output, image=image)
    assert result.returncode == 2 and not output.exists()


@pytest.mark.parametrize("gzip_layers", [False, True], ids=["plain", "gzip"])
def test_release_checks_each_layer_against_its_diff_id(tmp_path: Path, gzip_layers: bool) -> None:
    """两层都完整、各自摘要正确、数量相同，只交换顺序：配置里的 ``diff_ids`` 对不上，必须拒绝。

    gzip 层按解压后的摘要比对（OCI DiffID 的定义）；正确顺序是成功对照。
    """
    good = tmp_path / "good.tar.gz"
    image = image_archive(tmp_path / "good.tar", gzip_layers=gzip_layers)
    assert package(good, image=image).returncode == 0
    swapped = tmp_path / "swapped.tar.gz"
    image = image_archive(tmp_path / "swapped.tar", gzip_layers=gzip_layers, swap_layers=True)
    assert package(swapped, image=image).returncode == 2 and not swapped.exists()


def test_release_checks_the_blob_name_of_a_compressed_layer(tmp_path: Path) -> None:
    """同内容换了压缩方式：DiffID 不变，但文件不再是 manifest 指向的那个 blob。"""
    output = tmp_path / "bad.tar.gz"
    image = image_archive(tmp_path / "image.tar", gzip_layers=True, recompress=True)
    assert package(output, image=image).returncode == 2 and not output.exists()


def test_release_rejects_a_truncated_gzip_layer(tmp_path: Path) -> None:
    output = tmp_path / "bad.tar.gz"
    image = image_archive(tmp_path / "image.tar", gzip_layers=True)
    with tarfile.open(image) as archive:
        members = {m.name: archive.extractfile(m).read() for m in archive.getmembers()}
    manifest = json.loads(members["manifest.json"])
    first = manifest[0]["Layers"][0]
    members[first] = members[first][:-8]
    manifest[0]["Layers"][0] = f"blobs/sha256/{hashlib.sha256(members[first]).hexdigest()}"
    members[manifest[0]["Layers"][0]] = members.pop(first)
    members["manifest.json"] = json.dumps(manifest).encode()
    image.write_bytes(_tar_bytes(members))
    assert package(output, image=image).returncode == 2 and not output.exists()


@pytest.mark.parametrize("layers", [[], ["blobs/sha256/" + "e" * 64] * 3])
def test_release_rejects_layers_that_do_not_match_the_config(
    tmp_path: Path, layers: list[str]
) -> None:
    output = tmp_path / "bad.tar.gz"
    image = image_archive(tmp_path / "image.tar", layers=layers)
    assert package(output, image=image).returncode == 2 and not output.exists()


def test_release_rejects_an_image_built_for_another_platform(tmp_path: Path) -> None:
    output = tmp_path / "bad.tar.gz"
    image = image_archive(tmp_path / "image.tar", platform="linux/arm64")
    assert package(output, "linux/amd64", image=image).returncode == 2
    assert not output.exists()


def test_failed_packaging_keeps_an_existing_release_archive(tmp_path: Path) -> None:
    output = tmp_path / "release.tar.gz"
    output.write_bytes(b"previous release")
    image = image_archive(tmp_path / "image.tar", drop=("layer",))
    assert package(output, image=image).returncode == 2
    assert output.read_bytes() == b"previous release"


def test_release_rejects_a_missing_or_malformed_image_archive(tmp_path: Path) -> None:
    output = tmp_path / "bad.tar.gz"
    assert package(output, image=tmp_path / "missing.tar").returncode == 2
    broken = tmp_path / "broken.tar"
    broken.write_bytes(b"not a tar")
    assert package(output, image=broken).returncode == 2
    assert not output.exists()


def test_packaged_operations_only_reference_packaged_files() -> None:
    """公司服务器只拿到发行包：OPERATIONS 不能要求源码仓库、README 或 Git 里的文件。"""
    operations = (ROOT / "deploy/OPERATIONS.md").read_text(encoding="utf-8")
    named = set(re.findall(r"[\w.-]+\.example\.json|\.env\.example", operations))
    assert "feishu-group.example.json" in named
    assert named <= MEMBERS
    assert "examples/" not in operations and "README" not in operations


def test_operations_rollback_restores_the_previous_config() -> None:
    """升级前以受限权限留存实际配置路径上的旧 JSON；回退先停候选应用，再恢复到同一路径。

    新版可能已改成旧版读不了的 ``users={}``；路径取自 ``.env`` 的 ``XW_CONFIG_FILE``。
    """
    operations = (ROOT / "deploy/OPERATIONS.md").read_text(encoding="utf-8")
    upgrade = operations.split("## 普通升级与回退", 1)[1].split("\n## ", 1)[0]
    backup = 'install -m 600 "$config_file" "$previous/xiaowei.json"'
    assert backup in upgrade
    assert upgrade.index(backup) < upgrade.index("config check")
    rollback = upgrade.split("如果新版启动失败", 1)[1]
    restore = 'cp "$previous/xiaowei.json" "$config_file"'
    stop = "docker compose --env-file .env stop xiaowei &&"
    assert restore in rollback
    assert rollback.index(stop) < rollback.index('cp "$previous/')
    assert rollback.index(restore) < rollback.index("--force-recreate")


def test_release_archive_records_linux_amd64(tmp_path: Path) -> None:
    output = tmp_path / "amd64.tar.gz"
    image = image_archive(tmp_path / "amd64.tar", platform="linux/amd64")
    assert package(output, "linux/amd64", image=image).returncode == 0

    with tarfile.open(output, "r:gz") as archive:
        metadata = json.load(archive.extractfile("release.json"))
    assert metadata["platform"] == "linux/amd64"


def test_release_rejects_a_mutable_postgres_reference(tmp_path: Path) -> None:
    output = tmp_path / "bad.tar.gz"
    result = subprocess.run(  # noqa: S603 - 固定 Python 与仓库脚本
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--image-archive",
            str(image_archive(tmp_path / "image.tar")),
            "--postgres-image",
            "postgres:latest",
            "--code-sha",
            CODE_SHA,
            "--platform",
            "linux/arm64",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2 and not output.exists()


def test_runtime_image_layers_exclude_project_and_development_assets(
    runtime_image: str, tmp_path: Path
) -> None:
    saved = tmp_path / "image.tar"
    result = docker("save", "-o", str(saved), runtime_image)
    assert result.returncode == 0
    forbidden_packages = ("pytest", "ruff", "mypy", "hatchling", "alembic")
    forbidden_project = (
        "AGENTS.md",
        "ARCHITECTURE.md",
        "AGENT_HANDOFF.md",
        "README.md",
        "DEVELOPMENT_PLAN.md",
    )
    with tarfile.open(saved) as image:
        manifest = json.load(image.extractfile("manifest.json"))
        assert len(manifest) == 1
        names: set[str] = set()
        for layer_name in manifest[0]["Layers"]:
            with tarfile.open(fileobj=image.extractfile(layer_name)) as layer:
                names.update(member.name.removeprefix("./") for member in layer)

    assert not any(name in names for name in forbidden_project)
    assert not any(
        name == "tests" or name.startswith(("tests/", "docs/", "xiaowei_agent/")) for name in names
    )
    lowered = {name.lower() for name in names}
    for package_name in forbidden_packages:
        assert not any(
            f"/site-packages/{package_name}/" in f"/{name}/"
            or f"/site-packages/{package_name}-" in f"/{name}"
            for name in lowered
        )

    history = docker("history", "--no-trunc", "--format", "{{.CreatedBy}}", runtime_image)
    assert history.returncode == 0
    assert not any(word in history.stdout for word in (*forbidden_project, "tests/", "docs/"))

    probe = docker(
        "run",
        "--rm",
        "--entrypoint",
        "python",
        runtime_image,
        "-c",
        (
            "import importlib.metadata as m, importlib.resources as r;"
            " names={d.metadata['Name'].lower() for d in m.distributions()};"
            " assert not names & {'pytest','ruff','mypy','hatchling','alembic'};"
            " root=r.files('xiaowei');"
            " assert root.joinpath('migrations/006_digest_key_binding.sql').is_file();"
            " assert root.joinpath('static/index.html').is_file();"
            " assert 'README' not in (m.metadata('xiaowei-agent').get_payload() or '');"
        ),
    )
    assert probe.returncode == 0, "运行镜像缺少资源或夹带开发依赖"


def test_real_docker_save_packages_and_loads_back(runtime_image: str, tmp_path: Path) -> None:
    """真实 ``docker save → 打包 → 解出 → docker load``：导入后的标签与平台和 release.json 一致。"""
    platform = docker(
        "image", "inspect", runtime_image, "--format", "{{.Os}}/{{.Architecture}}"
    ).stdout.strip()
    assert docker("tag", runtime_image, APP_IMAGE).returncode == 0
    saved = tmp_path / "saved.tar"
    try:
        assert docker("save", "--output", str(saved), APP_IMAGE).returncode == 0
        assert docker("image", "rm", APP_IMAGE).returncode == 0
        output = tmp_path / "release.tar.gz"
        wrong = "linux/amd64" if platform == "linux/arm64" else "linux/arm64"
        assert package(output, wrong, image=saved).returncode == 2 and not output.exists()
        assert package(output, platform, image=saved).returncode == 0
        directory = tmp_path / "release"
        with tarfile.open(output, "r:gz") as bundle:
            bundle.extractall(directory, filter="data")
        metadata = json.loads((directory / "release.json").read_text(encoding="utf-8"))
        image_file = directory / metadata["image_file"]
        digest = hashlib.sha256(image_file.read_bytes()).hexdigest()
        assert digest == metadata["image_file_sha256"]
        assert docker("load", "--input", str(image_file)).returncode == 0
        loaded = docker(
            "image",
            "inspect",
            metadata["images"]["xiaowei"],
            "--format",
            "{{.Os}}/{{.Architecture}}",
        )
        assert loaded.returncode == 0 and loaded.stdout.strip() == metadata["platform"] == platform
        # docker load 解包失败时仍可能退出 0：再运行一次入口，与发布流程一致。
        assert docker("run", "--rm", metadata["images"]["xiaowei"], "--help").returncode == 0
    finally:
        docker("image", "rm", "-f", APP_IMAGE)
