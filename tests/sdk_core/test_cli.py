"""P1-B Task 8：正式命令的子进程验证。

console script ``xiaowei`` 与 ``python -m xiaowei`` 各自在独立进程中运行。

覆盖：两个入口的帮助与入口指向、包导入无副作用、SDK 模型/工具数据日志在外部预置 ``0``/``false``
时仍被强制关闭（canary）、配置错误不回显值、存储命令、正式 ``serve`` 的成功与关键失败路径
（同源 Web、模型失败固定回执、第二实例被拒、信号停止）。

模型端点指向本机一个已关闭的 HTTPS 端口：轮次在模型调用处失败，走完整的正式装配与失败路径，
不连接任何外部服务；StarRocks 不会被调用（该轮没有工具调用）。真实模型、StarRocks 与飞书属于
Task 9。成功轮次与飞书路径在 ``test_runtime.py`` 中经同一装配（替换最底层 I/O）验证。
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import entry_points
from pathlib import Path

import httpx
import pytest
from sqlalchemy.engine import URL
from tests.sdk_core.test_runtime import (
    DB_ENV,
    KEY_ENV,
    MODEL_ENV,
    SR_ENV,
    free_port,
    serve_config,
)

pytestmark = pytest.mark.loopback

ROOT = Path(__file__).resolve().parents[2]
ENTRIES = {
    "console": [str(Path(sys.executable).parent / "xiaowei")],
    "module": [sys.executable, "-m", "xiaowei"],
}
CANARY = "xw-cli-canary-41f9"
ENDPOINT = "xw-endpoint-canary-7c2e"  # 只出现在模型端点路径中
PRESET = {"OPENAI_AGENTS_DONT_LOG_MODEL_DATA": "0", "OPENAI_AGENTS_DONT_LOG_TOOL_DATA": "false"}


def child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("XW_", "OPENAI_"))}
    return {**env, **PRESET, **extra}


def run(
    argv: list[str], env: dict[str, str], timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 固定的本仓库入口
        argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


# ---- 入口与 SDK 日志开关 -------------------------------------------------------------


@pytest.mark.parametrize("entry", ENTRIES)
def test_both_entries_show_the_same_commands(entry: str) -> None:
    result = run([*ENTRIES[entry], "--help"], child_env())
    assert result.returncode == 0
    for command in ("serve", "storage", "requests", "--config"):
        assert command in result.stdout


def test_console_script_points_at_the_new_cli() -> None:
    (script,) = entry_points(group="console_scripts", name="xiaowei")
    assert script.value == "xiaowei.cli:main"


def test_importing_the_package_has_no_side_effects() -> None:
    probe = (
        "import os, sys, xiaowei;"
        " print('agents' in sys.modules, os.environ['OPENAI_AGENTS_DONT_LOG_MODEL_DATA'])"
    )
    result = run([sys.executable, "-c", probe], child_env())
    assert result.stdout.split() == ["False", "0"]


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
@pytest.mark.parametrize("value", ["0", "false"])
def test_entry_forces_sdk_data_logs_off_even_if_preset(value: str, level: str) -> None:
    """数据日志开关强制关闭，且正式日志配置不放行第三方的端点日志（成功响应路径）。"""
    env = child_env(OPENAI_AGENTS_DONT_LOG_MODEL_DATA=value, OPENAI_AGENTS_DONT_LOG_TOOL_DATA=value)
    script = [sys.executable, "-m", "tests.sdk_core.sdk_log_canary"]
    control = run([*script, "control", CANARY, ENDPOINT, level], env)
    assert control.returncode == 0 and "final output received" in control.stdout
    # 对照：预置值让 SDK 记录模型与工具数据；根 logger 的 INFO 让 httpx2 写出完整端点。
    if level == "DEBUG":
        assert CANARY in control.stderr
    assert f"https://model.test/{ENDPOINT}/v1/responses" in control.stderr
    entry = run([*script, "entry", CANARY, ENDPOINT, level], env)
    assert entry.returncode == 0 and "final output received" in entry.stdout
    assert "product event" in entry.stderr
    secrets_absent(entry.stderr)
    assert "model.test" not in entry.stderr and "sk-canary-test" not in entry.stderr


# ---- 配置 ----------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ENTRIES)
def test_configuration_errors_exit_2_without_values(entry: str, tmp_path: Path) -> None:
    config = serve_config(8501)
    config["storage"] = {**config["storage"], "digest_key_ref": f"plain-{CANARY}"}
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(config), encoding="utf-8")
    result = run([*ENTRIES[entry], "--config", str(file), "serve"], child_env())
    assert result.returncode == 2
    assert "storage.digest_key_ref" in result.stderr and CANARY not in result.stderr
    missing = run([*ENTRIES[entry], "--config", str(tmp_path / "none.json"), "serve"], child_env())
    assert missing.returncode == 2 and "配置文件不可读" in missing.stderr


# ---- 存储命令与正式 serve ------------------------------------------------------------


class Deployment:
    """一份配置文件与它引用的环境变量；模型端点是本机已关闭的 HTTPS 端口（路径含端点 canary）。"""

    def __init__(self, url: URL, tmp_path: Path, port: int | None = None) -> None:
        self.port = port or free_port()
        self.closed = closed = free_port()
        model = serve_config(self.port)["model"]
        config = serve_config(
            self.port,
            model={**model, "base_url": f"https://127.0.0.1:{closed}/{ENDPOINT}/v1"},
            lock_check_seconds=1,
        )
        config["starrocks"] = {**config["starrocks"], "host": "127.0.0.1", "port": closed}
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.file = tmp_path / "xiaowei.json"
        self.file.write_text(json.dumps(config), encoding="utf-8")
        self.env = child_env(
            **{
                DB_ENV: url.render_as_string(hide_password=False),
                KEY_ENV: f"digest-{CANARY}",
                MODEL_ENV: f"sk-{CANARY}",
                SR_ENV: f"sr-{CANARY}",
            }
        )
        self.origin = f"http://127.0.0.1:{self.port}"

    def command(self, entry: str, *args: str, level: str = "INFO") -> list[str]:
        return [*ENTRIES[entry], "--config", str(self.file), "--log-level", level, *args]

    def run(self, entry: str, *args: str) -> subprocess.CompletedProcess[str]:
        return run(self.command(entry, *args), self.env)

    @contextmanager
    def serving(self, entry: str, level: str = "DEBUG") -> Iterator[subprocess.Popen[str]]:
        process = subprocess.Popen(  # noqa: S603 - 固定的本仓库入口
            self.command(entry, "serve", level=level),
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    pytest.fail(f"serve 提前退出：{process.communicate()[1][-2000:]}")
                try:
                    if httpx.get(f"{self.origin}/readyz", timeout=2).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    pytest.fail("serve 未在期限内就绪")
                time.sleep(0.1)
            yield process
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def secrets_absent(text: str) -> None:
    assert CANARY not in text, "日志或输出含凭据或消息正文"
    assert ENDPOINT not in text, "日志或输出含模型端点"


@pytest.mark.parametrize("entry", ENTRIES)
def test_formal_serve_from_a_fresh_database(entry: str, postgres_url: URL, tmp_path: Path) -> None:
    deploy = Deployment(postgres_url, tmp_path)
    before = deploy.run(entry, "serve")
    assert before.returncode == 1 and "未初始化" in before.stderr  # 普通启动不建表

    init = deploy.run(entry, "storage", "init")
    assert init.returncode == 0 and init.stdout.strip() == "storage initialized"
    upgrade = deploy.run(entry, "storage", "upgrade")
    assert upgrade.returncode == 0 and upgrade.stdout.strip() == "storage version 2"

    with deploy.serving(entry) as process:
        ready = httpx.get(f"{deploy.origin}/readyz").json()
        assert ready == {"status": "ready", "feishu": "disabled"}
        assert httpx.get(f"{deploy.origin}/healthz").json() == {"status": "alive"}
        with httpx.Client(base_url=deploy.origin, timeout=60) as client:
            page = client.get("/")
            assert page.status_code == 200 and "xiaowei_web" in page.cookies
            origin = {"origin": deploy.origin}
            turn = client.post(
                "/api/turns",
                json={"request_id": "r1", "mode": "diagnose", "message": f"问 {CANARY}"},
                headers=origin,
            )
            body = turn.json()
            assert turn.status_code == 200 and body["state"] == "failed"
            assert "模型未能完成本轮" in body["delivery"]["content"]
            assert client.get("/api/turns/r1").json()["state"] == "failed"
            assert client.post("/api/sessions", json={}, headers=origin).status_code == 200
            # 保护性失败：其他 Host 与跨源写入在路由前拒绝。
            assert client.get("/", headers={"host": "evil.test"}).status_code == 400
            hostile = client.post("/api/sessions", json={}, headers={"origin": "http://evil.test"})
            assert hostile.status_code == 403

        # 第二个实例：另一端口、同一数据库，被实例锁拒绝。
        other = Deployment(postgres_url, tmp_path / "second")
        second = other.run("module" if entry == "console" else "console", "serve")
        assert second.returncode == 1 and "另一个小维进程" in second.stderr

        cleanup = deploy.run(entry, "storage", "cleanup", "--batch-size", "10")
        assert cleanup.returncode == 0 and cleanup.stdout.startswith("cleaned sessions=")

        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=60)
    assert process.returncode == 0
    assert "启动恢复：interrupted=0" in err
    secrets_absent(out + err + before.stderr + second.stderr)


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
@pytest.mark.parametrize("entry", ENTRIES)
def test_formal_logs_keep_product_events_and_drop_dependency_details(
    entry: str, level: str, postgres_url: URL, tmp_path: Path
) -> None:
    """正式入口的 INFO/DEBUG 只放开本产品日志：第三方的端点、连接与请求细节不出现。

    外部预置 ``OPENAI_LOG=debug`` 会让 openai 把自己的 logger 调到 DEBUG（写出端点与请求选项），
    正式入口也不能因此放行。
    """
    deploy = Deployment(postgres_url, tmp_path)
    deploy.env["OPENAI_LOG"] = "debug"
    assert deploy.run(entry, "storage", "init").returncode == 0
    with deploy.serving(entry, level) as process:
        with httpx.Client(base_url=deploy.origin, timeout=60) as client:
            client.get("/")
            turn = client.post(
                "/api/turns",
                json={"request_id": "r1", "mode": "diagnose", "message": f"问 {CANARY}"},
                headers={"origin": deploy.origin},
            )
            assert turn.json()["state"] == "failed"
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=60)
    assert process.returncode == 0
    logs = out + err
    # 产品自身的阶段、状态与错误码日志仍在。
    assert "启动恢复：interrupted=0" in logs
    assert "stage=" in logs and "reason=" in logs
    # 第三方细节：端点（含路径）、模型连接地址、数据库端口、请求行与请求选项。
    for leak in (
        f"127.0.0.1:{deploy.closed}",
        f"port={deploy.closed}",
        f":{postgres_url.port}",
        "/responses",
        "HTTP Request",
        "Request options",
    ):
        assert leak not in logs, leak
    secrets_absent(logs)


def test_cleanup_rejects_an_out_of_range_batch(postgres_url: URL, tmp_path: Path) -> None:
    deploy = Deployment(postgres_url, tmp_path)
    result = deploy.run("module", "storage", "cleanup", "--batch-size", "0")
    assert result.returncode == 2
