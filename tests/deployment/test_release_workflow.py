"""手动 amd64 发布必须使用固定输入，并把精确镜像 digest 放入发行包。"""

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
        "Authenticate to GHCR",
        "Build, publish and smoke-check the amd64 image",
        "Publish the versioned release archive",
    ]
    checkout = next(step for step in job["steps"] if "uses" in step)
    assert checkout["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert checkout["with"] == {"persist-credentials": "false", "fetch-depth": "0"}


def test_release_builds_and_smoke_checks_the_amd64_image_by_digest() -> None:
    workflow = load_workflow()
    job = workflow["jobs"]["release"]
    build = next(step for step in job["steps"] if step.get("name", "").startswith("Build,"))
    assert build["env"]["XW_PLATFORM"] == "linux/amd64"
    assert build["env"]["XW_APP_IMAGE"] == "ghcr.io/shixian66/xiaowei-agent-sdk"
    assert build["env"]["XW_POSTGRES_IMAGE"] == (
        "postgres:16.15-bookworm@sha256:"
        "efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
    )

    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "Dockerfile.runtime" in commands
    assert "--platform \"$XW_PLATFORM\"" in commands
    assert '--tag "$XW_APP_IMAGE:sha-$GITHUB_SHA"' in commands
    assert "--metadata-file" in commands
    assert "--push" in commands
    assert "containerimage.digest" in commands
    assert "docker pull \"$app_image\"" in commands
    assert "docker run --rm \"$app_image\" --help" in commands
    assert "scripts/package_release.py" in commands
    assert 'archive="$RUNNER_TEMP/xiaowei-$GITHUB_SHA-linux-amd64.tar.gz"' in commands
    assert '--app-image "$app_image"' in commands
    assert '"$XW_POSTGRES_IMAGE"' in commands
    assert '"$GITHUB_SHA"' in commands


def test_release_uses_a_separate_registry_token_and_uploads_the_archive() -> None:
    workflow = load_workflow()
    job = workflow["jobs"]["release"]
    login = next(step for step in job["steps"] if step.get("name") == "Authenticate to GHCR")
    commands = "\n".join(step.get("run", "") for step in job["steps"])

    secret_reference = "${{ secrets." + "GHCR_WRITE_TOKEN }}"
    assert login["env"]["GHCR_WRITE_TOKEN"] == secret_reference
    assert login["env"]["GHCR_USERNAME"] == "${{ vars.GHCR_USERNAME }}"
    assert 'printf \'%s\' "$GHCR_WRITE_TOKEN" | docker login ghcr.io' in login["run"]
    assert "--password-stdin" in login["run"]
    assert "GHCR_WRITE_TOKEN" not in next(
        step for step in job["steps"] if step.get("name", "").startswith("Build,")
    ).get("env", {})
    publish = next(step for step in job["steps"] if step.get("name", "").startswith("Publish"))
    github_token_reference = "${{ github." + "token }}"
    assert publish["env"]["GH_TOKEN"] == github_token_reference
    assert 'tag="sha-$GITHUB_SHA"' in publish["run"]
    assert "--password-stdin" in commands
    assert "--password \"$GHCR_WRITE_TOKEN\"" not in commands
    assert "gh release create" in commands
    assert "gh release upload" in commands
    assert "--clobber" in commands
    assert "org.opencontainers.image.source" not in commands
    dockerfile = (ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")
    assert "org.opencontainers.image.source" not in dockerfile
