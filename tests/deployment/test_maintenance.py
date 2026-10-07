"""P3-B：不同镜像普通升级、失败回退、成对备份恢复与历史 schema 迁移。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.deployment.conftest import docker
from tests.deployment.test_compose import (
    LEGACY_MODEL,
    POSTGRES_RUNTIME_IMAGE,
    _compose,
    _compose_command,
    _free_port,
    _http,
    _pg,
    _runtime_config,
    _wait_stopped,
    _write_env,
)

ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name("maintenance_probe.py")
P3_A_SHA = "9f69beba19736d293c22d50a59b50c90ac7f0d4e"
V4_SHA = "a938a0f1b481f1cb23507dc170380c73028b8e54"
V5_SHA = "fb67cd243ed3ab2beebae3970b909535042259f6"
# 允许 ``feishu.users={}`` 之前的最后一个 main：它要求单聊名单至少一人。
USERS_REQUIRED_SHA = "6d7c6af0a5688ba14c6c7a86a59a6d2f7c3f1369"
_UV_IMAGE = (
    "ghcr.io/astral-sh/uv@sha256:4f5d923c9dcea037f57bda425dd209f3ec643da2f0b74227f68d09dab0b3bb36"
)
_PYTHON_IMAGE = "python@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b"
_HISTORICAL_DOCKERFILE = f"""# syntax=docker/dockerfile:1.7
FROM {_UV_IMAGE} AS build
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_NO_CACHE=1
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY src/xiaowei ./src/xiaowei
RUN uv sync --frozen --no-dev --no-install-project
RUN uv build --wheel --out-dir /tmp/dist \\
    && uv pip install --python /opt/venv/bin/python --no-deps /tmp/dist/*.whl
FROM {_PYTHON_IMAGE}
ARG XW_CODE_SHA
LABEL org.opencontainers.image.title="xiaowei-history-fixture" \\
      org.opencontainers.image.revision="${{XW_CODE_SHA}}"
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/nonexistent
COPY --from=build /opt/venv /opt/venv
USER 65532:65532
ENTRYPOINT ["xiaowei"]
"""


def _extract_tree(sha: str, destination: Path) -> None:
    destination.mkdir(parents=True)
    archive = destination.with_suffix(".tar")
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(  # noqa: S603 - 固定 Git 与受审提交
        [git, "archive", "--format=tar", "--output", str(archive), sha],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")
    archive.unlink()


def _build_tree_image(sha: str, tag: str, *, historical: bool = False) -> tuple[str, Path]:
    context = ROOT / ".pytest_cache" / f"p3-image-{sha[:8]}-{uuid.uuid4().hex}"
    _extract_tree(sha, context)
    dockerfile = "Dockerfile.runtime"
    if historical:
        dockerfile = "Dockerfile.history"
        (context / dockerfile).write_text(_HISTORICAL_DOCKERFILE, encoding="utf-8")
    result = docker(
        "build",
        "--pull=false",
        "--build-arg",
        f"XW_CODE_SHA={sha}",
        "-t",
        tag,
        "-f",
        dockerfile,
        ".",
        cwd=context,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return tag, context


@pytest.fixture(scope="session")
def p3_a_image() -> Iterator[str]:
    tag = f"xiaowei-p3-a-history:{os.getpid()}"
    image, context = _build_tree_image(P3_A_SHA, tag)
    try:
        yield image
    finally:
        docker("image", "rm", "-f", image)
        shutil.rmtree(context, ignore_errors=True)


@pytest.fixture(scope="session")
def users_required_image() -> Iterator[str]:
    tag = f"xiaowei-users-required-history:{os.getpid()}"
    image, context = _build_tree_image(USERS_REQUIRED_SHA, tag)
    try:
        yield image
    finally:
        docker("image", "rm", "-f", image)
        shutil.rmtree(context, ignore_errors=True)


@pytest.fixture(scope="session")
def historical_images() -> Iterator[tuple[str, str]]:
    v4_tag = f"xiaowei-p3-v4-fixture:{os.getpid()}"
    v5_tag = f"xiaowei-p3-v5-fixture:{os.getpid()}"
    v4_image, v4_context = _build_tree_image(V4_SHA, v4_tag, historical=True)
    v5_image, v5_context = _build_tree_image(V5_SHA, v5_tag, historical=True)
    try:
        yield v4_image, v5_image
    finally:
        docker("image", "rm", "-f", v4_image, v5_image)
        shutil.rmtree(v4_context, ignore_errors=True)
        shutil.rmtree(v5_context, ignore_errors=True)


def _image_id(image: str) -> str:
    result = docker("image", "inspect", image, "--format", "{{.Id}}")
    assert result.returncode == 0
    value = result.stdout.strip()
    assert value.startswith("sha256:")
    return value


def _image_platform(image: str) -> str:
    result = docker("image", "inspect", image, "--format", "{{.Os}}/{{.Architecture}}")
    assert result.returncode == 0
    assert result.stdout.strip() in {"linux/amd64", "linux/arm64"}
    return result.stdout.strip()


def _release_from_tree(source: Path, destination: Path, image: str, sha: str) -> dict[str, Any]:
    app_ref = f"test.invalid/xiaowei@{_image_id(image)}"
    postgres_ref = (
        "postgres:16.15-bookworm@sha256:"
        "efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
    )
    archive = destination.with_suffix(".tar.gz")
    packaged = subprocess.run(  # noqa: S603 - 固定 Python 与受审发行脚本
        [
            sys.executable,
            str(source / "scripts/package_release.py"),
            "--output",
            str(archive),
            "--app-image",
            app_ref,
            "--postgres-image",
            postgres_ref,
            "--code-sha",
            sha,
            "--platform",
            _image_platform(image),
        ],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    assert packaged.returncode == 0, packaged.stderr
    destination.mkdir()
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(destination, filter="data")
    archive.unlink()
    metadata = json.loads((destination / "release.json").read_text(encoding="utf-8"))
    compose = (destination / "compose.yaml").read_text(encoding="utf-8")
    compose = compose.replace(app_ref, image).replace(postgres_ref, POSTGRES_RUNTIME_IMAGE)
    (destination / "compose.yaml").write_text(compose, encoding="utf-8")
    return metadata


def _copy_operator(source: Path, destination: Path) -> None:
    shutil.copyfile(source / ".env", destination / ".env")
    shutil.copyfile(source / "xiaowei.json", destination / "xiaowei.json")
    shutil.copytree(source / "certs", destination / "certs", dirs_exist_ok=True)


def _replace_app_image(directory: Path, before: str, after: str) -> None:
    compose_path = directory / "compose.yaml"
    compose = compose_path.read_text(encoding="utf-8")
    assert compose.count(before) == 1
    compose_path.write_text(compose.replace(before, after), encoding="utf-8")


def _historical_cli(directory: Path, *command: str) -> subprocess.CompletedProcess[str]:
    return _compose(
        directory,
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "xiaowei",
        "xiaowei",
        "--config",
        "/etc/xiaowei/xiaowei.json",
        *command,
        timeout=120,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _install_private_directory(directory: Path) -> None:
    binary = shutil.which("install")
    assert binary is not None
    result = subprocess.run(  # noqa: S603 - 固定系统工具与测试目录
        [binary, "-d", "-m", "700", str(directory)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def _copy_preserving_mode(source: Path, destination: Path) -> None:
    binary = shutil.which("cp")
    assert binary is not None
    result = subprocess.run(  # noqa: S603 - 固定系统工具与测试文件
        [binary, "-p", str(source), str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _replace_env_value(path: Path, name: str, value: str) -> None:
    prefix = f"{name}="
    lines = path.read_text(encoding="utf-8").splitlines()
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    replacement = f"{name}='{escaped}'"
    matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    assert len(matches) == 1
    lines[matches[0]] = replacement
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _compose_app_database_url(directory: Path) -> str:
    rendered = _compose(directory, "config", "--format", "json")
    assert rendered.returncode == 0, rendered.stderr
    environment = json.loads(rendered.stdout)["services"]["xiaowei"]["environment"]
    assert isinstance(environment, dict)
    value = environment["XW_DATABASE_URL"]
    assert isinstance(value, str)
    return value


def _serve_databases(container_id: str) -> list[str]:
    result = _pg(
        container_id,
        "SELECT DISTINCT datname FROM pg_stat_activity "
        "WHERE usename = 'xiaowei' AND client_addr IS NOT NULL ORDER BY 1",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def _probe(directory: Path, action: str) -> dict[str, Any]:
    result = _compose(
        directory,
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "python",
        "-v",
        f"{PROBE}:/opt/p3-maintenance-probe.py:ro",
        "xiaowei",
        "/opt/p3-maintenance-probe.py",
        action,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _post_saved_turn(port: int) -> dict[str, Any]:
    payload = json.dumps(
        {
            "request_id": "p3-upgrade-request",
            "mode": "query",
            "message": "读取已保存的升级验收结果",
        },
        ensure_ascii=False,
    ).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/turns",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Cookie": "xiaowei_web=p3_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "Origin": f"http://127.0.0.1:{port}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - 固定 loopback
        assert response.status == 200
        return json.load(response)


def _safe_dump(
    directory: Path, destination: Path, *, database: str | None = None
) -> subprocess.CompletedProcess[bytes]:
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.unlink(missing_ok=True)
    destination.unlink(missing_ok=True)
    command = 'exec pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
    if database is not None:
        assert database.isidentifier()
        command = f'exec pg_dump -Fc -U "$POSTGRES_USER" -d {database}'
    environment = {key: value for key, value in os.environ.items() if not key.startswith("XW_")}
    with temporary.open("wb") as output:
        result = subprocess.run(  # noqa: S603 - 参数只由本测试构造
            [
                *_compose_command(),
                "--env-file",
                ".env",
                "exec",
                "-T",
                "postgres",
                "sh",
                "-c",
                command,
            ],
            cwd=directory,
            env=environment,
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    if result.returncode == 0:
        os.replace(temporary, destination)
    else:
        temporary.unlink(missing_ok=True)
    return result


def _restore(directory: Path, archive: Path, database: str) -> subprocess.CompletedProcess[bytes]:
    assert database.isidentifier()
    created = _compose(
        directory,
        "exec",
        "-T",
        "postgres",
        "sh",
        "-c",
        f'exec createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" {database}',
    )
    assert created.returncode == 0, created.stderr
    environment = {key: value for key, value in os.environ.items() if not key.startswith("XW_")}
    with archive.open("rb") as source:
        return subprocess.run(  # noqa: S603 - 参数只由本测试构造
            [
                *_compose_command(),
                "--env-file",
                ".env",
                "exec",
                "-T",
                "postgres",
                "sh",
                "-c",
                f'exec pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" -d {database}',
            ],
            cwd=directory,
            env=environment,
            stdin=source,
            capture_output=True,
            check=False,
            timeout=120,
        )


def test_operations_use_staged_upgrade_and_atomic_paired_backup() -> None:
    operations = (ROOT / "deploy/OPERATIONS.md").read_text(encoding="utf-8")
    for required in (
        "compose.next.yaml",
        "release.next.json",
        "--no-deps --force-recreate xiaowei --wait",
        "direct_start_schema_versions",
        "upgrade_schema_versions",
        'tmp="$pair/.xiaowei.dump.tmp"',
        'mv "$tmp" "$pair/xiaowei.dump"',
        "pg_restore --exit-on-error",
        ".env.before-restore",
        "storage upgrade --bind-existing-digest-key",
        "on-failure:3",
    ):
        assert required in operations
    assert ".env.restore" not in operations
    assert "P3-A 尚未完成" not in operations
    assert "docker compose --env-file .env down -v" not in operations


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_different_image_upgrade_rollback_and_paired_restore(
    p3_a_image: str, runtime_image: str, tmp_path: Path
) -> None:
    del tmp_path
    host_tmp = ROOT / ".pytest_cache" / f"p3-maintenance-{uuid.uuid4().hex}"
    host_tmp.mkdir(parents=True)
    active = host_tmp / "active"
    candidate = host_tmp / "candidate"
    rollback = host_tmp / "rollback"
    a_source = host_tmp / "a-source"
    _extract_tree(P3_A_SHA, a_source)
    a_metadata = _release_from_tree(a_source, active, p3_a_image, P3_A_SHA)
    git = shutil.which("git")
    assert git is not None
    head = subprocess.run(  # noqa: S603 - 固定 Git 与当前受审工作树
        [git, "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    b_metadata = _release_from_tree(ROOT, candidate, runtime_image, head)
    assert a_metadata["images"]["xiaowei"] != b_metadata["images"]["xiaowei"]
    assert _image_id(p3_a_image) != _image_id(runtime_image)

    port = _free_port()
    suffix = uuid.uuid4().hex[:10]
    project = f"xiaowei-p3b-{suffix}"
    volume = f"xiaowei-p3b-{suffix}-pg"
    values = _write_env(
        active,
        XW_WEB_PORT=str(port),
        XW_STOP_GRACE_SECONDS="60",
        XW_PROJECT_NAME=project,
        XW_PG_VOLUME=volume,
    )
    _runtime_config(active, port, model=LEGACY_MODEL)
    (active / ".env").chmod(0o600)
    _copy_operator(active, candidate)
    operator_hashes = {
        name: _sha256(active / name) for name in (".env", "xiaowei.json", "certs/ca.pem")
    }
    original_env = (active / ".env").read_bytes()

    try:
        assert _compose(active, "up", "-d", "postgres", "--wait").returncode == 0
        initialized = _compose(active, "run", "--rm", "--no-deps", "xiaowei", "storage", "init")
        assert initialized.returncode == 0, initialized.stderr
        started = _compose(active, "up", "-d", "xiaowei", "--wait")
        assert started.returncode == 0, started.stderr
        a_app_id = _compose(active, "ps", "-q", "xiaowei").stdout.strip()
        pg_id = _compose(active, "ps", "-q", "postgres").stdout.strip()
        assert a_app_id and pg_id and _http(port, "/readyz")[0] == 200

        assert _compose(active, "stop", "xiaowei").returncode == 0
        seeded = _probe(active, "seed")
        assert seeded["schema_version"] == 6
        assert seeded["counts"]["xiaowei_request"] == 2
        assert _compose(active, "up", "-d", "xiaowei", "--wait").returncode == 0

        pull_failed = docker(
            "pull",
            "127.0.0.1:1/xiaowei@sha256:" + "0" * 64,
        )
        assert pull_failed.returncode != 0
        assert _compose(active, "ps", "-q", "xiaowei").stdout.strip() == a_app_id
        assert _http(port, "/readyz")[0] == 200

        _write_env(
            candidate,
            XW_WEB_PORT=str(port + 1),
            XW_STOP_GRACE_SECONDS="60",
            XW_PROJECT_NAME=project,
            XW_PG_VOLUME=volume,
        )
        failed_check = _compose(candidate, "run", "--rm", "--no-deps", "xiaowei", "config", "check")
        assert failed_check.returncode == 2
        assert _compose(active, "ps", "-q", "xiaowei").stdout.strip() == a_app_id
        assert _http(port, "/readyz")[0] == 200
        _copy_operator(active, candidate)
        shutil.copyfile(candidate / "compose.yaml", active / "compose.next.yaml")
        shutil.copyfile(candidate / "release.json", active / "release.next.json")
        checked = _compose(
            active,
            "-f",
            "compose.next.yaml",
            "run",
            "--rm",
            "--no-deps",
            "xiaowei",
            "config",
            "check",
        )
        assert checked.returncode == 0 and checked.stdout == "configuration valid\n"

        rollback.mkdir()
        for name in ("compose.yaml", "release.json"):
            shutil.copyfile(active / name, rollback / name)
        (active / "compose.next.yaml").replace(active / "compose.yaml")
        (active / "release.next.json").replace(active / "release.json")
        upgraded = _compose(
            active,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "xiaowei",
            "--wait",
        )
        assert upgraded.returncode == 0, upgraded.stderr
        b_app_id = _compose(active, "ps", "-q", "xiaowei").stdout.strip()
        assert b_app_id != a_app_id
        assert _compose(active, "ps", "-q", "postgres").stdout.strip() == pg_id
        inspected_image = docker("inspect", b_app_id, "--format", "{{.Image}}")
        assert _image_id(runtime_image) in inspected_image.stdout
        assert {
            name: _sha256(active / name) for name in (".env", "xiaowei.json", "certs/ca.pem")
        } == operator_hashes
        app = json.loads(docker("inspect", b_app_id).stdout)[0]
        assert app["HostConfig"]["PortBindings"][f"{port}/tcp"] == [
            {"HostIp": "127.0.0.1", "HostPort": str(port)}
        ]
        assert app["HostConfig"]["LogConfig"]["Config"] == {
            "max-file": "4",
            "max-size": "7m",
        }
        replay = _post_saved_turn(port)
        assert replay["state"] == "completed"
        assert "升级前保存的合成结果" in replay["delivery"]["content"]

        assert _compose(active, "stop", "xiaowei").returncode == 0
        verified = _probe(active, "verify")
        assert verified["binding_rows"] == 1 and verified["binding_length"] == 64
        assert verified["owners"]["xiaowei_request"] == [
            "group:p3-maintenance-group",
            "personal:local-operator",
        ]

        pair = host_tmp / "backup-pair"
        _install_private_directory(pair)
        _copy_preserving_mode(active / ".env", pair / "xiaowei.env")
        assert stat.S_IMODE((pair / "xiaowei.env").stat().st_mode) == 0o600
        backup = pair / "xiaowei.dump"
        assert _safe_dump(active, backup, database="missing_database").returncode != 0
        assert not backup.exists() and not (pair / ".xiaowei.dump.tmp").exists()
        assert _safe_dump(active, backup).returncode == 0
        assert backup.is_file() and backup.stat().st_size > 0
        marker = _pg(
            pg_id,
            "CREATE TABLE p3_post_backup_marker (id integer PRIMARY KEY); "
            "INSERT INTO p3_post_backup_marker VALUES (1)",
        )
        assert marker.returncode == 0, marker.stderr
        restored = _restore(active, backup, "xiaowei_restore")
        assert restored.returncode == 0, restored.stderr.decode(errors="replace")

        restored_url = values["XW_DATABASE_URL"].rsplit("/", 1)[0] + "/xiaowei_restore"
        before_restore = active / ".env.before-restore"
        _copy_preserving_mode(active / ".env", before_restore)
        _copy_preserving_mode(pair / "xiaowei.env", active / ".env")
        _replace_env_value(active / ".env", "XW_DATABASE_URL", restored_url)
        restored_env = (active / ".env").read_bytes()
        assert _compose_app_database_url(active) == restored_url
        restore_check = _compose(active, "run", "--rm", "--no-deps", "xiaowei", "config", "check")
        assert restore_check.returncode == 0 and restore_check.stdout == "configuration valid\n"
        assert _compose(active, "up", "-d", "xiaowei", "--wait").returncode == 0
        assert _http(port, "/readyz")[0] == 200
        assert _serve_databases(pg_id) == ["xiaowei_restore"]
        assert _compose(active, "stop", "xiaowei").returncode == 0
        assert _probe(active, "snapshot") == {
            **verified,
            "database": "xiaowei_restore",
            "post_backup_marker": False,
        }

        _replace_env_value(active / ".env", "XW_DIGEST_KEY", "wrong-p3-restore-key")
        assert _compose(active, "up", "-d", "--force-recreate", "xiaowei").returncode == 0
        failed_id = _compose(active, "ps", "-a", "-q", "xiaowei").stdout.strip()
        failed_state = _wait_stopped(failed_id)
        assert failed_state["ExitCode"] == 1
        assert json.loads(docker("inspect", failed_id).stdout)[0]["RestartCount"] == 3
        assert "wrong-p3-restore-key" not in docker("logs", failed_id).stderr

        (active / ".env").write_bytes(restored_env)
        assert _compose(active, "up", "-d", "--force-recreate", "xiaowei", "--wait").returncode == 0
        assert _http(port, "/readyz")[0] == 200
        assert _compose(active, "stop", "xiaowei").returncode == 0

        _copy_preserving_mode(before_restore, active / ".env")
        assert (active / ".env").read_bytes() == original_env
        assert _compose_app_database_url(active) == values["XW_DATABASE_URL"]
        original_check = _compose(active, "run", "--rm", "--no-deps", "xiaowei", "config", "check")
        assert original_check.returncode == 0 and original_check.stdout == "configuration valid\n"
        assert _compose(active, "up", "-d", "--force-recreate", "xiaowei", "--wait").returncode == 0
        assert _serve_databases(pg_id) == ["xiaowei"]
        assert _compose(active, "stop", "xiaowei").returncode == 0
        original_snapshot = _probe(active, "snapshot")
        assert original_snapshot == {
            **verified,
            "database": "xiaowei",
            "post_backup_marker": True,
        }

        for name in ("compose.yaml", "release.json"):
            shutil.copyfile(rollback / name, active / name)
        rolled_back = _compose(
            active,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "xiaowei",
            "--wait",
        )
        assert rolled_back.returncode == 0, rolled_back.stderr
        rollback_id = _compose(active, "ps", "-q", "xiaowei").stdout.strip()
        assert (
            _image_id(p3_a_image) in docker("inspect", rollback_id, "--format", "{{.Image}}").stdout
        )
        assert _http(port, "/readyz")[0] == 200
        assert _compose(active, "stop", "xiaowei").returncode == 0
        rolled_back_snapshot = _probe(active, "snapshot")
        assert rolled_back_snapshot == original_snapshot
        assert {
            name: _sha256(active / name) for name in (".env", "xiaowei.json", "certs/ca.pem")
        } == operator_hashes
    finally:
        _compose(active, "down", "-v", "--remove-orphans", timeout=60)
        docker("volume", "rm", "-f", volume)
        shutil.rmtree(host_tmp, ignore_errors=True)


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_container_migration_v4_to_v5_to_v6_and_old_program_refusal(
    historical_images: tuple[str, str], runtime_image: str, tmp_path: Path
) -> None:
    del tmp_path
    v4_image, v5_image = historical_images
    host_tmp = ROOT / ".pytest_cache" / f"p3-migration-{uuid.uuid4().hex}"
    host_tmp.mkdir(parents=True)
    directory = host_tmp / "release"
    head = subprocess.run(  # noqa: S603 - 固定 Git 与当前受审工作树
        [shutil.which("git") or "git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _release_from_tree(ROOT, directory, runtime_image, head)
    _replace_app_image(directory, runtime_image, v4_image)
    port = _free_port()
    suffix = uuid.uuid4().hex[:10]
    project = f"xiaowei-p3b-migrate-{suffix}"
    volume = f"xiaowei-p3b-migrate-{suffix}-pg"
    values = _write_env(
        directory,
        XW_WEB_PORT=str(port),
        XW_STOP_GRACE_SECONDS="60",
        XW_PROJECT_NAME=project,
        XW_PG_VOLUME=volume,
    )
    _runtime_config(directory, port, model=LEGACY_MODEL)
    original_env = (directory / ".env").read_bytes()

    try:
        assert _compose(directory, "up", "-d", "postgres", "--wait").returncode == 0
        pg_id = _compose(directory, "ps", "-q", "postgres").stdout.strip()
        initialized = _historical_cli(directory, "storage", "init")
        assert initialized.returncode == 0, initialized.stderr
        assert _pg(pg_id, "SELECT version FROM xiaowei_schema_version").stdout.strip() == "4"

        personal = _pg(
            pg_id,
            "INSERT INTO xiaowei_channel_session "
            "(channel, subject_id, conversation_key, generation, session_id, state, created_at, "
            "expires_at) VALUES ('web', 'alice', 'v4-conversation', 1, 'v4-session', "
            "'current', now(), now() + interval '1 day'); "
            "INSERT INTO xiaowei_session "
            "(session_id, subject_id, channel, profile_fingerprint, created_at, expires_at, "
            "turns, state) VALUES ('v4-session', 'alice', 'web', 'v4-profile', now(), "
            "now() + interval '1 day', 1, 'active'); "
            "INSERT INTO xiaowei_evidence "
            "(evidence_id, subject_id, session_id, turn_id, channel, target_id, tool_id, call_id, "
            "tool_name, arguments_digest, policy_id, policy_fingerprint, captured_at, recorded_at, "
            "expires_at, truncated, model_content, session_content, web_content, feishu_content, "
            "dependencies) VALUES ('ev_v4_personal', 'alice', 'v4-session', 'v4-turn', 'web', "
            "'warehouse', 'local/list_tables', 'v4-call', 'list_tables', 'v4-args', 'v4-policy', "
            "'v4-fingerprint', now(), now(), now() + interval '1 day', false, '{}', '{}', '{}', "
            "'{}', NULL); "
            "INSERT INTO xiaowei_request "
            "(channel, request_key, subject_id, conversation_key, session_id, turn_id, mode, "
            "message_digest, state, answer, failure_code, delivery, created_at, updated_at, "
            "expires_at) VALUES ('web', 'v4-request-key', 'alice', 'v4-conversation', "
            "'v4-session', 'v4-turn', 'query', 'v4-message-digest', 'failed', NULL, 'busy', "
            "'failed', now(), now(), now() + interval '1 day')",
        )
        assert personal.returncode == 0, personal.stderr

        _replace_app_image(directory, v4_image, v5_image)
        migrated_v5 = _historical_cli(directory, "storage", "upgrade")
        assert migrated_v5.returncode == 0 and "storage version 5" in migrated_v5.stdout
        assert (
            _pg(
                pg_id,
                "SELECT version || ':' || owner_kind || ':' || owner_id "
                "FROM xiaowei_schema_version CROSS JOIN xiaowei_session "
                "WHERE session_id = 'v4-session'",
            ).stdout.strip()
            == "5:personal:alice"
        )
        assert (
            _pg(
                pg_id,
                "SELECT owner_kind || ':' || owner_id FROM xiaowei_evidence "
                "WHERE evidence_id = 'ev_v4_personal'",
            ).stdout.strip()
            == "personal:alice"
        )

        group = _pg(
            pg_id,
            "INSERT INTO xiaowei_channel_session "
            "(channel, owner_kind, owner_id, conversation_key, generation, session_id, state, "
            "created_at, expires_at) VALUES ('feishu', 'group', 'p3-v5-group', 'v5-group-key', "
            "1, 'v5-group-session', 'current', now(), now() + interval '1 day'); "
            "INSERT INTO xiaowei_session "
            "(session_id, owner_kind, owner_id, channel, profile_fingerprint, created_at, "
            "expires_at, turns, state) VALUES ('v5-group-session', 'group', 'p3-v5-group', "
            "'feishu', 'v5-profile', now(), now() + interval '1 day', 1, 'active'); "
            "INSERT INTO xiaowei_evidence "
            "(evidence_id, owner_kind, owner_id, subject_id, session_id, turn_id, channel, "
            "target_id, tool_id, call_id, tool_name, arguments_digest, policy_id, "
            "policy_fingerprint, captured_at, recorded_at, expires_at, truncated, model_content, "
            "session_content, web_content, feishu_content, dependencies) VALUES "
            "('ev_v5_group', 'group', 'p3-v5-group', 'ou_v5_member', 'v5-group-session', "
            "'v5-group-turn', 'feishu', 'warehouse', 'local/list_tables', 'v5-call', "
            "'list_tables', 'v5-args', 'v5-policy', 'v5-fingerprint', now(), now(), "
            "now() + interval '1 day', false, '{}', '{}', '{}', '{}', NULL); "
            "INSERT INTO xiaowei_request "
            "(channel, request_key, owner_kind, owner_id, subject_id, conversation_key, "
            "session_id, turn_id, mode, message_digest, state, answer, failure_code, delivery, "
            "reply_chat_id, reply_message_id, created_at, updated_at, expires_at) VALUES "
            "('feishu', 'v5-group-request', 'group', 'p3-v5-group', 'ou_v5_member', "
            "'v5-group-key', 'v5-group-session', 'v5-group-turn', 'diagnose', "
            "'v5-message-digest', 'failed', NULL, 'busy', 'failed', 'oc_v5_group', "
            "'om_v5_group', now(), now(), now() + interval '1 day')",
        )
        assert group.returncode == 0, group.stderr
        pre_v6 = host_tmp / "pre-v6.dump"
        assert _safe_dump(directory, pre_v6).returncode == 0

        _replace_app_image(directory, v5_image, runtime_image)
        wrong_route = _compose(directory, "up", "-d", "--no-deps", "--force-recreate", "xiaowei")
        assert wrong_route.returncode == 0, wrong_route.stderr
        failed_id = _compose(directory, "ps", "-a", "-q", "xiaowei").stdout.strip()
        failed_state = _wait_stopped(failed_id)
        assert failed_state["ExitCode"] == 1
        failed_inspect = json.loads(docker("inspect", failed_id).stdout)[0]
        assert failed_inspect["RestartCount"] == 3
        assert _pg(pg_id, "SELECT version FROM xiaowei_schema_version").stdout.strip() == "5"

        missing_confirmation = _compose(
            directory, "run", "--rm", "--no-deps", "xiaowei", "storage", "upgrade"
        )
        assert missing_confirmation.returncode == 1
        assert _pg(pg_id, "SELECT version FROM xiaowei_schema_version").stdout.strip() == "5"
        migrated_v6 = _compose(
            directory,
            "run",
            "--rm",
            "--no-deps",
            "xiaowei",
            "storage",
            "upgrade",
            "--bind-existing-digest-key",
        )
        assert migrated_v6.returncode == 0 and migrated_v6.stdout.strip() == "storage version 6"
        preserved = _pg(
            pg_id,
            "SELECT version || ':' || "
            "(SELECT count(*) FROM xiaowei_installation) || ':' || "
            "(SELECT count(*) FROM xiaowei_session WHERE owner_kind = 'personal') || ':' || "
            "(SELECT count(*) FROM xiaowei_session WHERE owner_kind = 'group') || ':' || "
            "(SELECT count(*) FROM xiaowei_evidence) FROM xiaowei_schema_version",
        )
        assert preserved.returncode == 0 and preserved.stdout.strip() == "6:1:1:1:2"

        _replace_app_image(directory, runtime_image, v5_image)
        old_upgrade = _historical_cli(directory, "storage", "upgrade")
        old_serve = _historical_cli(directory, "serve")
        assert old_upgrade.returncode == old_serve.returncode == 1
        assert _pg(pg_id, "SELECT version FROM xiaowei_schema_version").stdout.strip() == "6"

        restored = _restore(directory, pre_v6, "xiaowei_v5_rollback")
        assert restored.returncode == 0, restored.stderr.decode(errors="replace")
        rollback_url = values["XW_DATABASE_URL"].rsplit("/", 1)[0] + "/xiaowei_v5_rollback"
        _write_env(
            directory,
            XW_WEB_PORT=str(port),
            XW_STOP_GRACE_SECONDS="60",
            XW_PROJECT_NAME=project,
            XW_PG_VOLUME=volume,
            XW_DATABASE_URL=rollback_url,
        )
        old_rollback = _historical_cli(directory, "storage", "upgrade")
        assert old_rollback.returncode == 0 and "storage version 5" in old_rollback.stdout
        (directory / ".env").write_bytes(original_env)

        _replace_app_image(directory, v5_image, runtime_image)
        assert _pg(pg_id, "UPDATE xiaowei_schema_version SET version = 999").returncode == 0
        unknown = _compose(
            directory,
            "run",
            "--rm",
            "--no-deps",
            "xiaowei",
            "storage",
            "upgrade",
            "--bind-existing-digest-key",
        )
        assert unknown.returncode == 1
        assert _pg(pg_id, "SELECT version FROM xiaowei_schema_version").stdout.strip() == "999"
        assert _pg(pg_id, "UPDATE xiaowei_schema_version SET version = 6").returncode == 0
    finally:
        _compose(directory, "down", "-v", "--remove-orphans", timeout=60)
        docker("volume", "rm", "-f", volume)
        shutil.rmtree(host_tmp, ignore_errors=True)


# 演练里启用飞书：默认网络设为 internal，容器无外网也无外部 DNS；飞书连接在本机失败，
# 不触达真实服务。
_ISOLATION = "networks:\n  default:\n    internal: true\n"


def _isolated(directory: Path, compose_file: str, *args: str) -> subprocess.CompletedProcess[str]:
    return _compose(directory, "-f", compose_file, "-f", "isolation.yaml", *args, timeout=240)


def _feishu_section(package: Path, users: dict[str, str]) -> dict[str, Any]:
    """只用发行包里的飞书模板，按 OPERATIONS 替换待填写标记。"""
    section = json.loads((package / "feishu-group.example.json").read_text(encoding="utf-8"))
    section.update(
        app_id="cli_drillapp",
        tenant_key="drill-tenant",
        users=users,
        connect_timeout_seconds=1,
    )
    section["group"]["chat_id"] = "oc_drill_group"
    return section


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_feishu_users_upgrade_rollback_restores_the_config_the_old_image_reads(
    users_required_image: str, runtime_image: str
) -> None:
    """旧版要求单聊名单非空，新版允许 ``{}``。

    逐字执行 OPERATIONS 的留存、切换与回退命令块，只替换目录和 ``config_file``：配置放在非默认的
    嵌套路径，默认位置的 ``xiaowei.json`` 是内容不同的诱饵。预检失败不停旧服务；回退先停止候选
    应用，再把升级前的 JSON 恢复到同一路径，旧镜像重新读取配置并就绪。
    """
    host_tmp = ROOT / ".pytest_cache" / f"p3-users-rollback-{uuid.uuid4().hex}"
    host_tmp.mkdir(parents=True)
    active = host_tmp / "active"
    candidate = host_tmp / "candidate"
    previous = host_tmp / "previous"
    old_source = host_tmp / "old-source"
    _extract_tree(USERS_REQUIRED_SHA, old_source)
    _release_from_tree(old_source, active, users_required_image, USERS_REQUIRED_SHA)
    git = shutil.which("git")
    assert git is not None
    head = subprocess.run(  # noqa: S603 - 固定 Git 与当前受审工作树
        [git, "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    _release_from_tree(ROOT, candidate, runtime_image, head)
    assert not (active / "feishu-group.example.json").exists()

    port = _free_port()
    suffix = uuid.uuid4().hex[:10]
    volume = f"xiaowei-users-{suffix}-pg"
    _write_env(
        active,
        XW_WEB_PORT=str(port),
        XW_STOP_GRACE_SECONDS="120",  # 启用飞书与指定群后停止上界更长
        XW_CONFIG_FILE=_DRILL_CONFIG_FILE,
        XW_PROJECT_NAME=f"xiaowei-users-{suffix}",
        XW_PG_VOLUME=volume,
        XW_FEISHU_APP_SECRET="feishu $ # space ' quote",  # noqa: S106 - 合成值
    )
    (active / ".env").chmod(0o600)
    _runtime_config(active, port, model=LEGACY_MODEL)
    config = json.loads((active / "xiaowei.json").read_text(encoding="utf-8"))
    # 旧版合法、新版拒绝：已登记用户的 subject 没有授权。
    config["feishu"] = _feishu_section(candidate, {"ou_drill_user": "drill-user"})
    custom = active / _DRILL_CONFIG_FILE
    custom.parent.mkdir()
    custom.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    decoy = active / "xiaowei.json"
    decoy.write_text('{"decoy": "默认位置的诱饵，不应被预检、留存或恢复"}', encoding="utf-8")
    (active / "isolation.yaml").write_text(_ISOLATION, encoding="utf-8")
    old_json = custom.read_bytes()
    old_mode = stat.S_IMODE(custom.stat().st_mode)
    decoy_json = decoy.read_bytes()
    env_before = (active / ".env").read_bytes()
    blocks = {
        name: _localize_block(block, active, previous, candidate)
        for name, block in _upgrade_blocks().items()
    }
    shims = host_tmp / "bin"
    _install_drill_shims(shims)
    log = host_tmp / "drill.log"

    try:
        assert _isolated(active, "compose.yaml", "up", "-d", "postgres", "--wait").returncode == 0
        init = _isolated(
            active, "compose.yaml", "run", "--rm", "--no-deps", "xiaowei", "storage", "init"
        )
        assert init.returncode == 0, init.stderr
        started = _isolated(active, "compose.yaml", "up", "-d", "xiaowei", "--wait")
        assert started.returncode == 0, started.stderr
        old_id = _isolated(active, "compose.yaml", "ps", "-q", "xiaowei").stdout.strip()
        postgres_id = _isolated(active, "compose.yaml", "ps", "-q", "postgres").stdout.strip()
        volume_created = docker("volume", "inspect", volume, "--format", "{{.CreatedAt}}").stdout

        # 留存旧控制文件与实际配置路径上的旧 JSON（600），再预检新版。
        backup = _run_block(blocks["backup"], shims, log)
        assert backup.returncode == 0, backup.stderr
        assert (previous / "xiaowei.json").read_bytes() == old_json
        assert stat.S_IMODE((previous / "xiaowei.json").stat().st_mode) == 0o600
        for name in ("compose.yaml", "release.json"):
            assert (previous / name).read_bytes() == (active / name).read_bytes()
        shutil.copyfile(candidate / "compose.yaml", active / "compose.next.yaml")
        shutil.copyfile(candidate / "release.json", active / "release.next.json")
        check = ("run", "--rm", "--no-deps", "xiaowei", "config", "check")
        failed = _isolated(active, "compose.next.yaml", *check)
        assert failed.returncode == 2 and "drill-user" in failed.stderr  # 读的是自定义路径
        assert "ou_drill_user" not in failed.stdout + failed.stderr
        assert _isolated(active, "compose.yaml", "ps", "-q", "xiaowei").stdout.strip() == old_id
        assert _health(old_id) == "healthy"

        config["access"]["grants"]["drill-user"] = ["local/list_tables"]
        custom.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        passed = _isolated(active, "compose.next.yaml", *check)
        assert passed.returncode == 0, passed.stderr

        switched = _run_block(blocks["switch"], shims, log)
        assert switched.returncode == 0, switched.stderr

        # 新版允许空单聊名单（群仍可用）；旧镜像读不了这份配置，所以回退必须恢复旧 JSON。
        del config["access"]["grants"]["drill-user"]
        config["feishu"]["users"] = {}
        custom.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        recreate = ("up", "-d", "--no-deps", "--force-recreate", "xiaowei", "--wait")
        assert _isolated(active, "compose.yaml", *check).returncode == 0
        assert _isolated(active, "compose.yaml", *recreate).returncode == 0
        new_id = _isolated(active, "compose.yaml", "ps", "-q", "xiaowei").stdout.strip()
        new_image = docker("inspect", new_id, "--format", "{{.Image}}").stdout
        assert _image_id(runtime_image) in new_image
        assert _health(new_id) == "healthy"
        old_compose = str(previous / "compose.yaml")
        unreadable = _isolated(active, old_compose, "--project-directory", str(active), *check)
        assert unreadable.returncode == 2 and "feishu.users" in unreadable.stderr

        log.write_text("", encoding="utf-8")
        rolled_back = _run_block(blocks["rollback"], shims, log, candidate=new_id)
        assert rolled_back.returncode == 0, rolled_back.stderr
        steps = log.read_text(encoding="utf-8").splitlines()
        copies = [index for index, step in enumerate(steps) if step.startswith("cp ")]
        assert steps[0] == "compose stop xiaowei", steps
        assert len(copies) == 3 and copies[0] > 0, steps
        assert all(steps[index].endswith(" candidate=exited") for index in copies), steps
        assert custom.read_bytes() == old_json
        assert stat.S_IMODE(custom.stat().st_mode) == old_mode
        assert decoy.read_bytes() == decoy_json
        rollback_id = _isolated(active, "compose.yaml", "ps", "-q", "xiaowei").stdout.strip()
        image = docker("inspect", rollback_id, "--format", "{{.Image}}").stdout
        assert _image_id(users_required_image) in image
        assert _health(rollback_id) == "healthy"
        # 回退只换应用：PostgreSQL 容器、卷、.env 与摘要密钥原样保留。
        assert _isolated(active, "compose.yaml", "ps", "-q", "postgres").stdout.strip() == (
            postgres_id
        )
        assert docker("volume", "inspect", volume, "--format", "{{.CreatedAt}}").stdout == (
            volume_created
        )
        assert (active / ".env").read_bytes() == env_before
    finally:
        _isolated(active, "compose.yaml", "down", "-v", "--remove-orphans")
        shutil.rmtree(host_tmp, ignore_errors=True)


def test_rollback_overwrites_nothing_when_the_candidate_does_not_stop(tmp_path: Path) -> None:
    """停止候选应用失败时，回退命令块不得继续覆盖控制文件或配置。"""
    active = tmp_path / "active"
    previous = tmp_path / "previous"
    for directory, label in ((active, "candidate"), (previous, "previous")):
        directory.mkdir()
        for name in ("compose.yaml", "release.json"):
            (directory / name).write_text(f"{label} {name}\n", encoding="utf-8")
    (active / "config").mkdir()
    (active / _DRILL_CONFIG_FILE).write_text("candidate config\n", encoding="utf-8")
    (previous / "xiaowei.json").write_text("previous config\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in active.rglob("*") if path.is_file()}
    shims = tmp_path / "bin"
    shims.mkdir()
    failing = shims / "docker"
    failing.write_text('#!/bin/sh\necho "docker $*" >> "$DRILL_LOG"\nexit 1\n', encoding="utf-8")
    failing.chmod(0o755)
    log = tmp_path / "drill.log"
    script = _localize_block(_upgrade_blocks()["rollback"], active, previous, tmp_path / "new")

    result = _run_block(script, shims, log)

    assert result.returncode != 0
    assert log.read_text(encoding="utf-8") == "docker compose --env-file .env stop xiaowei\n"
    assert {path: path.read_bytes() for path in active.rglob("*") if path.is_file()} == before


def test_upgrade_blocks_name_the_operator_config_file_in_every_block() -> None:
    """每个可单独复制的命令块都自己定义 ``config_file``，默认值与 .env 模板一致。"""
    env_example = (ROOT / "deploy/.env.example").read_text(encoding="utf-8")
    assert f"XW_CONFIG_FILE={_DEFAULT_CONFIG_FILE}\n" in env_example
    blocks = _upgrade_blocks()
    for name in ("backup", "rollback"):
        assert '"$config_file"' in blocks[name], name
    for block in blocks.values():
        if "$config_file" in block:
            definition = f"config_file='{_DEFAULT_CONFIG_FILE}'  # 与 .env 的 XW_CONFIG_FILE 相同"
            assert definition in block.splitlines(), block


_DEFAULT_CONFIG_FILE = "./xiaowei.json"
_DRILL_CONFIG_FILE = "config/prod.json"
_UPGRADE_SECTION = "## 普通升级与回退（schema 不变）"
_BLOCK_MARKERS = {
    "backup": "install -m 600",
    "switch": "mv compose.next.yaml compose.yaml",
    "rollback": '"$previous/compose.yaml"',
}


def _upgrade_blocks() -> dict[str, str]:
    """OPERATIONS 普通升级一节的可复制命令块，按内容认出留存、切换与回退三块。"""
    operations = (ROOT / "deploy/OPERATIONS.md").read_text(encoding="utf-8")
    section = operations.split(_UPGRADE_SECTION, 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```sh\n(.*?)```", section, flags=re.DOTALL)
    found = {}
    for name, marker in _BLOCK_MARKERS.items():
        matches = [block for block in blocks if marker in block]
        assert len(matches) == 1, name
        found[name] = matches[0]
    return found


def _localize_block(block: str, active: Path, previous: Path, candidate: Path) -> str:
    """只替换文档里的目录与 ``config_file`` 取值，命令本身逐字执行。"""
    script = block.replace("/opt/xiaowei/releases/<旧 SHA>", str(previous))
    script = script.replace("/opt/xiaowei/releases/<新 SHA>", str(candidate))
    script = script.replace("/opt/xiaowei/current", str(active))
    script = script.replace(
        f"config_file='{_DEFAULT_CONFIG_FILE}'", f"config_file='./{_DRILL_CONFIG_FILE}'"
    )
    assert "<" not in script, script
    return script


# 演练替身：文档写 ``docker compose --env-file .env [-f 文件] …``，交给本机 Compose 并叠加
# internal 网络覆盖文件；其他 docker 命令原样执行。cp 先记下候选容器当时的状态再复制。
_DOCKER_SHIM = """#!@PYTHON@
import os, sys
args = sys.argv[1:]
if args[:1] != ["compose"]:
    os.execv(@DOCKER@, [@DOCKER@, *args])
args = args[1:]
assert args[:2] == ["--env-file", ".env"], args
args, compose_file = args[2:], "compose.yaml"
if args[:1] == ["-f"]:
    compose_file, args = args[1], args[2:]
with open(os.environ["DRILL_LOG"], "a", encoding="utf-8") as log:
    log.write("compose " + " ".join(args) + "\\n")
command = [*@COMPOSE@, "--env-file", ".env", "-f", compose_file, "-f", "isolation.yaml", *args]
os.execv(command[0], command)
"""
_CP_SHIM = """#!@PYTHON@
import os, subprocess, sys
state = subprocess.run(
    [@DOCKER@, "inspect", "--format", "{{.State.Status}}", os.environ["DRILL_CANDIDATE"]],
    capture_output=True, text=True, check=False,
).stdout.strip() or "none"
with open(os.environ["DRILL_LOG"], "a", encoding="utf-8") as log:
    log.write("cp " + " ".join(sys.argv[1:]) + " candidate=" + state + "\\n")
os.execv(@CP@, [@CP@, *sys.argv[1:]])
"""


def _install_drill_shims(directory: Path) -> None:
    real_docker, real_cp = shutil.which("docker"), shutil.which("cp")
    assert real_docker is not None and real_cp is not None
    directory.mkdir()
    for name, template in (("docker", _DOCKER_SHIM), ("cp", _CP_SHIM)):
        shim = directory / name
        shim.write_text(
            template.replace("@PYTHON@", sys.executable)
            .replace("@DOCKER@", repr(real_docker))
            .replace("@CP@", repr(real_cp))
            .replace("@COMPOSE@", repr(_compose_command())),
            encoding="utf-8",
        )
        shim.chmod(0o755)


def _run_block(
    script: str, shims: Path, log: Path, *, candidate: str = "none"
) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("sh")
    assert shell is not None
    environment = {key: value for key, value in os.environ.items() if not key.startswith("XW_")}
    environment.update(
        PATH=f"{shims}{os.pathsep}{environment['PATH']}",
        DRILL_LOG=str(log),
        DRILL_CANDIDATE=candidate,
    )
    return subprocess.run(  # noqa: S603 - 只执行仓库文档里的命令块与测试替身
        [shell, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


def _health(container_id: str) -> str:
    return docker("inspect", container_id, "--format", "{{.State.Health.Status}}").stdout.strip()
