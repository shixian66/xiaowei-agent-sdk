"""手动 amd64 发布：从固定输入构建镜像，以离线文件放进发行包，不推送任何镜像仓库。"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/publish-amd64.yml"


def load_workflow():
    # BaseLoader 只构造字典/列表/字符串，且不会把 GitHub 的 `on` 键解析成 YAML 1.1 布尔值。
    return yaml.load(
        WORKFLOW.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,  # noqa: S506 - BaseLoader does not construct arbitrary Python objects
    )


def test_release_is_manual_main_only_and_uses_least_github_permissions() -> None:
    assert WORKFLOW.is_file(), "缺少手动 amd64 发布 workflow"
    workflow = load_workflow()

    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["on"]["workflow_dispatch"] == ""
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "publish-amd64-main",
        "cancel-in-progress": "false",
    }
    job = workflow["jobs"]["release"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["permissions"] == {"contents": "write"}
    assert [step.get("name", step.get("uses")) for step in job["steps"]] == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "Build, smoke-check and package the amd64 image",
        "Publish the versioned release archive",
    ]
    checkout = next(step for step in job["steps"] if "uses" in step)
    assert checkout["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert checkout["with"] == {"persist-credentials": "false", "fetch-depth": "0"}


def test_release_builds_the_image_into_an_offline_file() -> None:
    workflow = load_workflow()
    job = workflow["jobs"]["release"]
    build = next(step for step in job["steps"] if step.get("name", "").startswith("Build,"))
    assert build["env"]["XW_PLATFORM"] == "linux/amd64"
    assert build["env"]["XW_POSTGRES_IMAGE"] == (
        "postgres:16.15-bookworm@sha256:"
        "efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
    )

    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "Dockerfile.runtime" in commands
    assert '--platform "$XW_PLATFORM"' in commands
    assert 'image="xiaowei:$GITHUB_SHA"' in commands
    assert '--tag "$image"' in commands
    assert "--load" in commands
    assert 'docker run --rm "$image" --help' in commands
    assert 'docker save --output "$image_file" "$image"' in commands
    assert "scripts/package_release.py" in commands
    assert '--image-archive "$image_file"' in commands
    # 打包后从文件重新导入并核对平台：Docker 自身确认这份文件能用。
    reload = commands.index('docker image rm "$image"')
    assert commands.index("scripts/package_release.py") < reload
    assert reload < commands.index('docker load --input "$image_file"')
    assert '{{.Os}}/{{.Architecture}}\')" = "$XW_PLATFORM"' in commands
    assert 'archive="$RUNNER_TEMP/xiaowei-$GITHUB_SHA-linux-amd64.tar.gz"' in commands
    assert '"$XW_POSTGRES_IMAGE"' in commands


def test_release_pushes_to_no_registry_and_uploads_only_the_archive() -> None:
    workflow = load_workflow()
    job = workflow["jobs"]["release"]
    text = WORKFLOW.read_text(encoding="utf-8")
    for word in ("--push", "docker login", "ghcr.io", "GHCR_", "packages:"):
        assert word not in text
    publish = next(step for step in job["steps"] if step.get("name", "").startswith("Publish"))
    github_token_reference = "${{ github." + "token }}"
    assert publish["env"]["GH_TOKEN"] == github_token_reference
    assert 'tag="sha-$GITHUB_SHA"' in publish["run"]
    for command in ("gh release create", "gh release upload", "--clobber"):
        assert command in publish["run"]
    dockerfile = (ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")
    assert "org.opencontainers.image.source" not in dockerfile
