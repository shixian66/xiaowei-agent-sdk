"""P3-A Compose 静态契约；真实构建与启动用例在同文件后续覆盖。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import certifi
import pytest
import yaml
from tests.deployment.conftest import docker
from tests.deployment.test_release import APP_IMAGE, POSTGRES_IMAGE, ROOT, package

POSTGRES_RUNTIME_IMAGE = (
    "postgres:16.15-bookworm@sha256:"
    "efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
)


def _release(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    archive = tmp_path / "release.tar.gz"
    assert package(archive).returncode == 0
    directory = tmp_path / "release"
    directory.mkdir()
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(directory, filter="data")
    return directory, yaml.safe_load((directory / "compose.yaml").read_text(encoding="utf-8"))


def _compose_command() -> list[str]:
    docker = shutil.which("docker")
    if docker is not None:
        probe = subprocess.run(  # noqa: S603 - 只探测 PATH 中的 Docker CLI
            [docker, "compose", "version"], capture_output=True, text=True, check=False
        )
        if probe.returncode == 0:
            return [docker, "compose"]
    standalone = shutil.which("docker-compose")
    assert standalone is not None, "P3-A 发行检查需要 Docker Compose"
    return [standalone]


def _write_env(directory: Path, **overrides: str) -> dict[str, str]:
    postgres_password = "pg $ # space ' quote"  # noqa: S105 - 合成特殊字符测试值
    values = {
        "XW_WEB_PORT": "18501",
        # 本机测试只绑 loopback；发行默认 0.0.0.0 由静态与渲染用例分别验证。
        "XW_WEB_BIND_ADDRESS": "127.0.0.1",
        "XW_STOP_GRACE_SECONDS": "90",
        "XW_CONFIG_FILE": "./xiaowei.json",
        "XW_CERTS_DIR": "./certs",
        "XW_LOG_MAX_SIZE": "7m",
        "XW_LOG_MAX_FILES": "4",
        "XW_PROJECT_NAME": "xiaowei-p3-test",
        "XW_PG_VOLUME": "xiaowei-p3-test-pg",
        "XW_POSTGRES_DB": "xiaowei",
        "XW_POSTGRES_USER": "xiaowei",
        "XW_POSTGRES_PASSWORD": postgres_password,
        "XW_DATABASE_URL": (
            "postgresql+asyncpg://xiaowei:"
            f"{quote(postgres_password, safe='')}@postgres:5432/xiaowei"
        ),
        "XW_DIGEST_KEY": "digest $ # space ' quote",
        "XW_MODEL_API_KEY": "model $ # space ' quote",
        "XW_STARROCKS_PASSWORD": "sr $ # space ' quote",
        "XW_ARCHIVE_STARROCKS_PASSWORD": "archive $ # space ' quote",
    }
    values.update(overrides)
    lines = []
    for name, value in values.items():
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"{name}='{escaped}'")
    (directory / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = directory / "xiaowei.json"
    if not config.exists():
        config.write_text("{}", encoding="utf-8")
    (directory / "certs").mkdir(exist_ok=True)
    return values


def _localize_images(directory: Path, runtime_image: str) -> None:
    compose = (directory / "compose.yaml").read_text(encoding="utf-8")
    compose = compose.replace(APP_IMAGE, runtime_image).replace(
        POSTGRES_IMAGE, POSTGRES_RUNTIME_IMAGE
    )
    (directory / "compose.yaml").write_text(compose, encoding="utf-8")


def _compose(directory: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("XW_")}
    return subprocess.run(  # noqa: S603 - 参数只由本测试构造
        [*_compose_command(), "--env-file", ".env", *args],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _free_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as server:
        return int(server.getsockname()[1])


# C2 之前发行模板的模型段（OpenAI Responses）。旧镜像不认识 ``provider: vertex``；升级与回退演练中
# 操作者保留的是旧版本发行的这种配置，新旧镜像都能读取。
LEGACY_MODEL = {
    "profile_id": "example-openai",
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_mode": "responses",
    "model": "synthetic-model",
    "api_key_ref": "env:XW_MODEL_API_KEY",
    "output_mode": "json_schema",
    "request_timeout_seconds": 60,
    "max_output_tokens": 4096,
    "max_request_bytes": 1000000,
    "max_response_bytes": 1000000,
    "data_policy_id": "example-policy",
    "reasoning_effort": None,
}


def _runtime_config(
    directory: Path,
    port: int,
    *,
    model: dict[str, Any] | None = None,
    template: Path | None = None,
) -> None:
    """按该发行版本的模板写出可运行配置；旧镜像演练可指定历史模板。"""
    config = json.loads(
        (template or directory / "xiaowei.example.json").read_text(encoding="utf-8")
    )
    config["targets"] = config["targets"][:1]
    # 模板占位符必须替换；正式预检会拒绝未填写的模板。
    config["model"]["model"] = "synthetic-model"
    if model is not None:
        config["model"] = model
    config["targets"][0]["description"] = "合成集群"
    target = config["targets"][0]["starrocks"]
    target.update(
        {
            "host": "127.0.0.1",
            "port": 1,
            "database": "synthetic",
            "user": "readonly",
            "connect_timeout_seconds": 1,
            "query_timeout_seconds": 1,
            "client_timeout_seconds": 2,
            "schema_limits": {
                **target["schema_limits"],
                "refresh_seconds": 60,
                "refresh_timeout_seconds": 2,
            },
            "tls": True,
            "tls_ca_file": "/etc/xiaowei/certs/ca.pem",
        }
    )
    config["listen_port"] = port
    config["web"]["allowed_origins"] = [f"http://127.0.0.1:{port}"]
    config["shutdown_timeout_seconds"] = 1
    config["lock_check_seconds"] = 1
    (directory / "xiaowei.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    shutil.copyfile(certifi.where(), directory / "certs/ca.pem")


def _container_state(container_id: str) -> dict[str, Any]:
    result = docker("inspect", "--format", "{{json .State}}", container_id)
    assert result.returncode == 0
    return json.loads(result.stdout)


def _wait_stopped(container_id: str, *, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _container_state(container_id)
        if not state["Running"]:
            return state
        time.sleep(0.2)
    raise AssertionError("容器未在期限内停止")


def _pg(container_id: str, sql: str) -> subprocess.CompletedProcess[str]:
    return docker(
        "exec",
        container_id,
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-Atq",
        "-U",
        "xiaowei",
        "-d",
        "xiaowei",
        "-c",
        sql,
    )


def _wait_pg_value(container_id: str, sql: str, expected: str, *, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _pg(container_id, sql)
        if result.returncode == 0 and result.stdout.strip() == expected:
            return
        time.sleep(0.2)
    raise AssertionError("PostgreSQL 状态未在期限内达到预期")


def _http(port: int, path: str = "/", **headers: str) -> tuple[int, bytes, Any]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - 固定 HTTP loopback
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def test_compose_has_two_services_and_keeps_operator_settings_external(tmp_path: Path) -> None:
    _, compose = _release(tmp_path)
    assert set(compose["services"]) == {"xiaowei", "postgres"}
    app = compose["services"]["xiaowei"]
    postgres = compose["services"]["postgres"]

    assert app["image"] == APP_IMAGE and postgres["image"] == POSTGRES_IMAGE
    assert app["init"] is True and app["restart"] == "on-failure:3"
    assert app["healthcheck"]["timeout"] == "20s"
    assert postgres.get("ports") is None
    assert set(postgres["environment"]) == {
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    }
    assert app["ports"] == [
        {
            "target": "${XW_WEB_PORT:-8501}",
            "published": "${XW_WEB_PORT:-8501}",
            "host_ip": "${XW_WEB_BIND_ADDRESS:-0.0.0.0}",
            "protocol": "tcp",
        }
    ]
    # 健康检查每次冷启动 Python；公司服务器约 10 秒，超时须留余量。
    assert app["healthcheck"]["timeout"] == "20s"
    mounts = {mount["target"]: mount for mount in app["volumes"]}
    for target in ("/etc/xiaowei/xiaowei.json", "/etc/xiaowei/certs"):
        assert mounts[target]["read_only"] is True
        assert mounts[target]["bind"]["create_host_path"] is False
    assert compose["name"] == "${XW_PROJECT_NAME:-xiaowei}"
    assert compose["volumes"]["pgdata"]["name"] == "${XW_PG_VOLUME:-xiaowei-pgdata}"


def test_compose_resolves_non_default_parameters_and_literal_secrets(tmp_path: Path) -> None:
    directory, _ = _release(tmp_path)
    expected = _write_env(directory, XW_WEB_BIND_ADDRESS="172.20.0.8")
    result = subprocess.run(  # noqa: S603 - 只执行探测后的 Compose CLI
        [*_compose_command(), "config", "--format", "json"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "docker compose config failed"
    rendered = json.loads(result.stdout)
    app = rendered["services"]["xiaowei"]
    postgres = rendered["services"]["postgres"]
    assert rendered["name"] == expected["XW_PROJECT_NAME"]
    assert app["ports"][0]["host_ip"] == expected["XW_WEB_BIND_ADDRESS"]
    assert app["ports"][0]["published"] == expected["XW_WEB_PORT"]
    assert app["environment"]["XW_WEB_BIND_ADDRESS"] == expected["XW_WEB_BIND_ADDRESS"]
    assert app["stop_grace_period"] == "1m30s"
    for name in ("XW_DIGEST_KEY", "XW_MODEL_API_KEY", "XW_STARROCKS_PASSWORD"):
        # Compose config 用 $$ 表示传给容器的字面 $；容器实值由正式启动用例另验。
        assert app["environment"][name] == expected[name].replace("$", "$$")
    assert set(postgres["environment"]) == {
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    }
    assert postgres["environment"]["POSTGRES_PASSWORD"] == expected["XW_POSTGRES_PASSWORD"].replace(
        "$", "$$"
    )


def test_compose_publishes_the_web_port_to_all_addresses_by_default(tmp_path: Path) -> None:
    """不设 ``XW_WEB_BIND_ADDRESS`` 时内网其他机器可以访问；设成 127.0.0.1 可收回到本机。"""
    directory, _ = _release(tmp_path)
    _write_env(directory)
    env_file = directory / ".env"
    lines = env_file.read_text(encoding="utf-8").splitlines()
    env_file.write_text(
        "\n".join(line for line in lines if not line.startswith("XW_WEB_BIND_ADDRESS=")) + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 - 只执行探测后的 Compose CLI
        [*_compose_command(), "config", "--format", "json"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "docker compose config failed"
    app = json.loads(result.stdout)["services"]["xiaowei"]
    assert app["ports"][0]["host_ip"] == "0.0.0.0"  # noqa: S104 - 内网访问的发行默认值
    assert app["environment"]["XW_WEB_BIND_ADDRESS"] == "0.0.0.0"  # noqa: S104


def test_missing_config_bind_fails_without_creating_a_host_directory(
    runtime_image: str, tmp_path: Path
) -> None:
    del tmp_path  # Docker Desktop/Colima 不一定共享系统临时目录；仓库缓存目录始终可挂载。
    host_tmp = ROOT / ".pytest_cache" / f"p3-compose-{uuid.uuid4().hex}"
    host_tmp.mkdir(parents=True)
    try:
        directory, _ = _release(host_tmp)
        _localize_images(directory, runtime_image)
        missing = directory / "missing.json"
        _write_env(directory, XW_CONFIG_FILE="./missing.json")

        result = _compose(directory, "run", "--rm", "--no-deps", "xiaowei", "config", "check")

        assert result.returncode != 0
        assert not missing.exists()
    finally:
        shutil.rmtree(host_tmp, ignore_errors=True)


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_formal_compose_entry_success_failure_network_and_signal(
    runtime_image: str, tmp_path: Path
) -> None:
    del tmp_path
    host_tmp = ROOT / ".pytest_cache" / f"p3-compose-{uuid.uuid4().hex}"
    host_tmp.mkdir(parents=True)
    directory, _ = _release(host_tmp)
    _localize_images(directory, runtime_image)
    port = _free_port()
    suffix = uuid.uuid4().hex[:10]
    project = f"xiaowei-p3-{suffix}"
    volume = f"xiaowei-p3-{suffix}-pg"
    expected = _write_env(
        directory,
        XW_WEB_PORT=str(port),
        XW_STOP_GRACE_SECONDS="60",
        XW_PROJECT_NAME=project,
        XW_PG_VOLUME=volume,
    )
    _runtime_config(directory, port)
    (directory / ".env").chmod(0o600)
    upgrade_process: subprocess.Popen[str] | None = None

    try:
        assert _compose(directory, "config", "--quiet").returncode == 0
        checked = _compose(directory, "run", "--rm", "--no-deps", "xiaowei", "config", "check")
        assert checked.returncode == 0 and checked.stdout == "configuration valid\n"

        postgres = _compose(directory, "up", "-d", "postgres", "--wait")
        assert postgres.returncode == 0, postgres.stderr
        first_init = _compose(directory, "run", "--rm", "--no-deps", "xiaowei", "storage", "init")
        second_init = _compose(directory, "run", "--rm", "--no-deps", "xiaowei", "storage", "init")
        assert first_init.returncode == second_init.returncode == 0

        started = _compose(directory, "up", "-d", "xiaowei", "--wait")
        assert started.returncode == 0, started.stderr
        app_id = _compose(directory, "ps", "-q", "xiaowei").stdout.strip()
        pg_id = _compose(directory, "ps", "-q", "postgres").stdout.strip()
        assert app_id and pg_id

        status, body, headers = _http(port)
        assert status == 200 and b"<!doctype html>" in body.lower()
        assert "xiaowei_web=" in headers["set-cookie"]
        ready_status, ready_body, _ = _http(port, "/readyz")
        assert ready_status == 200 and json.loads(ready_body) == {
            "feishu": "disabled",
            "status": "ready",
        }
        # 内网访问：其他 Host / Origin 不再拒绝。
        assert _http(port, Host="172.20.0.8")[0] == 200
        assert _http(port, Origin="http://172.20.0.8")[0] == 200

        app_inspect = json.loads(docker("inspect", app_id).stdout)[0]
        pg_inspect = json.loads(docker("inspect", pg_id).stdout)[0]
        bindings = app_inspect["NetworkSettings"]["Ports"][f"{port}/tcp"]
        assert bindings == [{"HostIp": "127.0.0.1", "HostPort": str(port)}]
        assert not any(pg_inspect["NetworkSettings"]["Ports"].values())
        assert set(item.split("=", 1)[0] for item in pg_inspect["Config"]["Env"]).isdisjoint(
            {"XW_DIGEST_KEY", "XW_MODEL_API_KEY", "XW_STARROCKS_PASSWORD"}
        )

        environment = docker(
            "exec",
            app_id,
            "python",
            "-c",
            (
                "import json,os;"
                "print(json.dumps({k:os.environ[k] for k in "
                "('XW_DIGEST_KEY','XW_MODEL_API_KEY','XW_STARROCKS_PASSWORD')}))"
            ),
        )
        assert environment.returncode == 0
        actual = json.loads(environment.stdout)
        for name in actual:
            assert actual[name] == expected[name]

        assert docker("kill", "--signal", "TERM", app_id).returncode == 0
        stopped = _wait_stopped(app_id)
        assert stopped["ExitCode"] == 0

        downgraded = _pg(
            pg_id,
            # 本机演练重建真实 v5 形状，不能只改版本而留下后来新增的表。
            "BEGIN; DROP TABLE xiaowei_action; DROP TABLE xiaowei_installation; "
            "UPDATE xiaowei_schema_version SET version = 5; COMMIT",
        )
        assert downgraded.returncode == 0
        lock = docker(
            "exec",
            "-d",
            "-e",
            "PGAPPNAME=p3-upgrade-lock",
            pg_id,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "xiaowei",
            "-d",
            "xiaowei",
            "-c",
            "BEGIN; LOCK TABLE xiaowei_schema_version IN ACCESS EXCLUSIVE MODE; "
            "SELECT pg_sleep(60); COMMIT",
        )
        assert lock.returncode == 0
        _wait_pg_value(
            pg_id,
            "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
            "WHERE a.application_name = 'p3-upgrade-lock' AND l.granted "
            "AND l.relation = 'xiaowei_schema_version'::regclass",
            "1",
        )
        upgrade_name = f"{project}-upgrade-signal"
        clean_env = {key: value for key, value in os.environ.items() if not key.startswith("XW_")}
        upgrade_process = subprocess.Popen(  # noqa: S603 - 参数只由本测试构造
            [
                *_compose_command(),
                "--env-file",
                ".env",
                "run",
                "--name",
                upgrade_name,
                "--no-deps",
                "xiaowei",
                "storage",
                "upgrade",
                "--bind-existing-digest-key",
            ],
            cwd=directory,
            env=clean_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_pg_value(
            pg_id,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND mode = 'ExclusiveLock' AND granted",
            "1",
        )
        assert docker("kill", "--signal", "TERM", upgrade_name).returncode == 0
        upgrade_process.communicate(timeout=10)
        assert upgrade_process.returncode != 0
        terminated = _pg(
            pg_id,
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE application_name = 'p3-upgrade-lock'",
        )
        assert terminated.returncode == 0
        _wait_pg_value(
            pg_id,
            "SELECT version || ':' || coalesce(to_regclass('xiaowei_installation')::text, '') "
            "FROM xiaowei_schema_version",
            "5:",
        )
        assert docker("rm", "-f", upgrade_name).returncode == 0
        restored = _compose(
            directory,
            "run",
            "--rm",
            "--no-deps",
            "xiaowei",
            "storage",
            "upgrade",
            "--bind-existing-digest-key",
        )
        assert restored.returncode == 0 and restored.stdout.strip() == "storage version 7"
        upgrade_process = None

        wrong_key = "wrong digest $ # space ' quote"
        _write_env(
            directory,
            XW_WEB_PORT=str(port),
            XW_STOP_GRACE_SECONDS="60",
            XW_PROJECT_NAME=project,
            XW_PG_VOLUME=volume,
            XW_DIGEST_KEY=wrong_key,
        )
        failed = _compose(directory, "up", "-d", "--force-recreate", "xiaowei")
        assert failed.returncode == 0, failed.stderr
        failed_id = _compose(directory, "ps", "-a", "-q", "xiaowei").stdout.strip()
        failure_state = _wait_stopped(failed_id)
        assert failure_state["ExitCode"] == 1
        assert failure_state["Restarting"] is False
        failed_inspect = json.loads(docker("inspect", failed_id).stdout)[0]
        assert failed_inspect["RestartCount"] == 3
        logs = docker("logs", failed_id)
        assert "摘要密钥与数据库不匹配" in logs.stderr
        assert wrong_key not in logs.stderr
    finally:
        _compose(directory, "down", "-v", "--remove-orphans", timeout=60)
        if upgrade_process is not None and upgrade_process.poll() is None:
            upgrade_process.kill()
            upgrade_process.communicate(timeout=5)
        docker("volume", "rm", "-f", volume)
        shutil.rmtree(host_tmp, ignore_errors=True)
