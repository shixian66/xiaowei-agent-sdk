"""P3-A 发行归档：固定白名单、不可变镜像引用与可重复内容。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

from tests.deployment.conftest import docker

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/package_release.py"
APP_IMAGE = "registry.example/xiaowei@sha256:" + "a" * 64
POSTGRES_IMAGE = "postgres:16.15-bookworm@sha256:" + "b" * 64
CODE_SHA = "c" * 40
MEMBERS = {
    ".env.example",
    "OPERATIONS.md",
    "compose.yaml",
    "feishu-group.example.json",
    "release.json",
    "xiaowei.example.json",
}


def package(
    output: Path, platform: str = "linux/arm64"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 固定 Python 与仓库脚本
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--app-image",
            APP_IMAGE,
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
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    try:
        assert package(first).returncode == 0
        assert package(second).returncode == 0
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
    compose = extracted["compose.yaml"].decode()
    assert APP_IMAGE in compose and POSTGRES_IMAGE in compose
    metadata = json.loads(extracted["release.json"])
    assert metadata == {
        "format_version": 1,
        "code_sha": CODE_SHA,
        "platform": "linux/arm64",
        "schema_version": 6,
        "direct_start_schema_versions": [6],
        "upgrade_schema_versions": [1, 2, 3, 4, 5],
        "config_migration": "none",
        "images": {"xiaowei": APP_IMAGE, "postgres": POSTGRES_IMAGE},
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


def test_packaged_operations_only_reference_packaged_files() -> None:
    """公司服务器只拿到发行包：OPERATIONS 不能要求源码仓库、README 或 Git 里的文件。"""
    operations = (ROOT / "deploy/OPERATIONS.md").read_text(encoding="utf-8")
    named = set(re.findall(r"[\w.-]+\.example\.json|\.env\.example", operations))
    assert "feishu-group.example.json" in named
    assert named <= MEMBERS
    assert "examples/" not in operations and "README" not in operations


def test_operations_rollback_restores_the_previous_config() -> None:
    """升级前以受限权限留存旧 JSON，回退旧镜像时一并恢复（新版可能已改成旧版读不了的 users={}）。"""
    operations = (ROOT / "deploy/OPERATIONS.md").read_text(encoding="utf-8")
    upgrade = operations.split("## 普通升级与回退", 1)[1].split("\n## ", 1)[0]
    assert 'install -m 600 xiaowei.json "$previous/xiaowei.json"' in upgrade
    assert upgrade.index("install -m 600 xiaowei.json") < upgrade.index("config check")
    rollback = upgrade.split("如果新版启动失败", 1)[1]
    assert 'cp "$previous/xiaowei.json" xiaowei.json' in rollback
    assert rollback.index("xiaowei.json") < rollback.index("--force-recreate")


def test_release_archive_records_linux_amd64(tmp_path: Path) -> None:
    output = tmp_path / "amd64.tar.gz"
    assert package(output, "linux/amd64").returncode == 0

    with tarfile.open(output, "r:gz") as archive:
        metadata = json.load(archive.extractfile("release.json"))
    assert metadata["platform"] == "linux/amd64"


def test_release_rejects_mutable_image_references(tmp_path: Path) -> None:
    output = tmp_path / "bad.tar.gz"
    result = subprocess.run(  # noqa: S603 - 固定 Python 与仓库脚本
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--app-image",
            "registry.example/xiaowei:latest",
            "--postgres-image",
            POSTGRES_IMAGE,
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
