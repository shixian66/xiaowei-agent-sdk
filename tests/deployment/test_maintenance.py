"""P3-B：不同镜像普通升级、失败回退、成对备份恢复与历史 schema 迁移。"""

from __future__ import annotations

import hashlib
import json
import os
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
    _runtime_config(active, port)
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
        restore_check = _compose(
            active, "run", "--rm", "--no-deps", "xiaowei", "config", "check"
        )
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
        original_check = _compose(
            active, "run", "--rm", "--no-deps", "xiaowei", "config", "check"
        )
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
    _runtime_config(directory, port)
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
