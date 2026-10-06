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
    env_template_values,
    example_with_feishu,
    free_port,
    serve_config,
)

from xiaowei import cli as cli_module
from xiaowei import runtime

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


@pytest.mark.parametrize("entry", ENTRIES)
def test_storage_upgrade_exposes_the_one_time_binding_confirmation(entry: str) -> None:
    result = run(
        [*ENTRIES[entry], "--config", "unused", "storage", "upgrade", "--help"],
        child_env(),
    )
    assert result.returncode == 0
    assert "--bind-existing-digest-key" in result.stdout


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


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_dependency_warnings_and_errors_do_not_reach_the_formal_logs(level: str) -> None:
    """真实飞书 SDK 的端点探针失败：其自带 stdout handler 与根 logger 都会写出原始异常。

    正式日志配置下 stdout 与 stderr 都不出现端点、主机、端口或原始异常；本产品的事件与原因码
    仍按级别输出。
    """
    closed = free_port()
    script = [sys.executable, "-m", "tests.sdk_core.lark_log_canary"]
    env = child_env(OPENAI_LOG="debug")
    control = run([*script, "control", ENDPOINT, str(closed), level], env)
    assert control.returncode == 0 and "probe False" in control.stdout
    # 对照：两条输出路径都带出端点、主机与端口。
    assert ENDPOINT in control.stdout and f"port={closed}" in control.stdout
    assert ENDPOINT in control.stderr and "HTTPConnectionPool" in control.stderr
    entry = run([*script, "entry", ENDPOINT, str(closed), level], env)
    assert entry.returncode == 0 and entry.stdout.split() == ["probe", "False"]
    assert "product event" in entry.stderr and "reason=feishu_unavailable" in entry.stderr
    for leak in (ENDPOINT, "127.0.0.1", str(closed), "HTTPConnectionPool", "[Lark]", "secret-"):
        assert leak not in entry.stdout + entry.stderr, leak


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


@pytest.mark.parametrize("entry", ENTRIES)
def test_config_check_is_offline_and_does_not_modify_the_config(entry: str, tmp_path: Path) -> None:
    config = serve_config(18501)
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic-ca", encoding="utf-8")
    target = config["targets"][0]["starrocks"]
    target.update({"tls": True, "tls_ca_file": str(ca)})
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(config), encoding="utf-8")
    before = (file.read_bytes(), file.stat().st_mtime_ns)
    env = child_env(
        **{
            DB_ENV: "postgresql+asyncpg://offline.invalid/xiaowei",
            KEY_ENV: f"digest $ # ' {CANARY}",
            MODEL_ENV: f"model $ # ' {CANARY}",
            SR_ENV: f"starrocks $ # ' {CANARY}",
        }
    )

    result = run([*ENTRIES[entry], "--config", str(file), "config", "check"], env)

    assert result.returncode == 0 and result.stdout.strip() == "configuration valid"
    assert result.stderr == "" and CANARY not in result.stdout
    assert (file.read_bytes(), file.stat().st_mtime_ns) == before


@pytest.mark.parametrize("entry", ENTRIES)
def test_config_check_rejects_missing_environment_and_unreadable_ca_without_values(
    entry: str, tmp_path: Path
) -> None:
    config = serve_config(18501)
    target = config["targets"][0]["starrocks"]
    target.update({"tls": True, "tls_ca_file": str(tmp_path)})
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(config), encoding="utf-8")
    env = child_env(
        **{
            DB_ENV: "postgresql+asyncpg://offline.invalid/xiaowei",
            KEY_ENV: f"digest-{CANARY}",
            MODEL_ENV: "",
            SR_ENV: f"starrocks-{CANARY}",
        }
    )

    missing = run([*ENTRIES[entry], "--config", str(file), "config", "check"], env)
    assert missing.returncode == 2 and "model.api_key_ref" in missing.stderr
    secrets_absent(missing.stdout + missing.stderr)

    env[MODEL_ENV] = f"model-{CANARY}"
    unreadable = run([*ENTRIES[entry], "--config", str(file), "config", "check"], env)
    assert unreadable.returncode == 2 and "targets.0.starrocks.tls_ca_file" in unreadable.stderr
    secrets_absent(unreadable.stdout + unreadable.stderr)


def test_container_entry_is_the_only_cli_path_that_selects_container_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port = 18501
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(serve_config(port)), encoding="utf-8")
    for name, value in {
        DB_ENV: "postgresql+asyncpg://offline.invalid/xiaowei",
        KEY_ENV: "digest-test",
        MODEL_ENV: "model-test",
        SR_ENV: "starrocks-test",
        "XW_WEB_PORT": str(port),
        "XW_STOP_GRACE_SECONDS": "60",
    }.items():
        monkeypatch.setenv(name, value)
    called: list[runtime.ServeConfig] = []

    async def fake_container_serve(config: runtime.ServeConfig, *, stop: object) -> int:
        called.append(config)
        return 0

    monkeypatch.setattr(runtime, "_container_serve", fake_container_serve)

    assert cli_module.container_main(["serve"], config_path=file) == 0
    assert len(called) == 1 and called[0].listen_host == "127.0.0.1"
    assert "container" not in _parser_help()


def _template_file(tmp_path: Path) -> Path:
    """未替换的发行模板（含飞书片段），环境变量全部已设置：只有模板占位符这一个问题。"""
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(example_with_feishu(), ensure_ascii=False), encoding="utf-8")
    return file


def _no_runtime(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    started: list[str] = []

    async def record(*args: object, **kwargs: object) -> int:
        started.append("runtime")
        return 0

    monkeypatch.setattr(cli_module, "_run", record)
    monkeypatch.setattr(runtime, "_container_serve", record)
    return started


@pytest.mark.parametrize("container", [False, True], ids=["native", "container"])
def test_serve_rejects_an_unreplaced_template_before_any_runtime_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    container: bool,
) -> None:
    for name in (
        "XW_DATABASE_URL",
        "XW_DIGEST_KEY",
        "XW_MODEL_API_KEY",
        "XW_STARROCKS_PASSWORD",
        "XW_ARCHIVE_STARROCKS_PASSWORD",
        "XW_FEISHU_APP_SECRET",
    ):
        monkeypatch.setenv(name, f"real-{name.lower()}")
    monkeypatch.setenv("XW_WEB_PORT", "8501")
    monkeypatch.setenv("XW_STOP_GRACE_SECONDS", "600")
    started = _no_runtime(monkeypatch)
    file = _template_file(tmp_path)
    before = (file.read_bytes(), file.stat().st_mtime_ns)

    if container:
        code = cli_module.container_main(["serve"], config_path=file)
    else:
        code = cli_module.main(["--config", str(file), "serve"])

    assert code == 2 and started == []
    err = capsys.readouterr().err
    assert "model.model" in err and "feishu.app_id" in err and "<" not in err
    assert (file.read_bytes(), file.stat().st_mtime_ns) == before


@pytest.mark.parametrize("entry", ENTRIES)
def test_config_check_rejects_template_values_in_the_environment(
    entry: str, tmp_path: Path
) -> None:
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(serve_config(18501)), encoding="utf-8")
    template = env_template_values()["XW_MODEL_API_KEY"]
    env = child_env(
        **{
            DB_ENV: "postgresql+asyncpg://offline.invalid/xiaowei",
            KEY_ENV: f"digest-{CANARY}",
            MODEL_ENV: template,
            SR_ENV: f"starrocks-{CANARY}",
        }
    )
    result = run([*ENTRIES[entry], "--config", str(file), "config", "check"], env)
    assert result.returncode == 2 and "model.api_key_ref" in result.stderr
    assert template not in result.stdout + result.stderr
    secrets_absent(result.stdout + result.stderr)


def test_native_cli_requests_only_the_configured_loopback_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port = 18501
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(serve_config(port)), encoding="utf-8")
    requested: list[tuple[str, int]] = []

    def reject_after_recording(host: str, requested_port: int) -> object:
        requested.append((host, requested_port))
        raise runtime.ListenError(host, requested_port)

    monkeypatch.setattr(runtime, "_bind", reject_after_recording)

    assert cli_module.main(["--config", str(file), "serve"]) == 1
    assert requested == [("127.0.0.1", port)]


def _parser_help() -> str:
    return run([*ENTRIES["module"], "--help"], child_env()).stdout


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
        (target,) = config["targets"]
        target["starrocks"] = {**target["starrocks"], "host": "127.0.0.1", "port": closed}
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
    assert upgrade.returncode == 0 and upgrade.stdout.strip() == "storage version 6"

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
