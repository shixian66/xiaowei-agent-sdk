"""P1-B Task 8：正式命令的子进程验证。

console script ``xiaowei`` 与 ``python -m xiaowei`` 各自在独立进程中运行。

覆盖：两个入口的帮助与入口指向、包导入无副作用、SDK 模型/工具数据日志在外部预置 ``0``/``false``
时仍被强制关闭（canary）、配置错误不回显值、存储命令、正式 ``serve`` 的成功与关键失败路径
（同源 Web、模型失败固定回执、第二实例被拒、信号停止）。

模型端点指向本机一个已关闭的 HTTPS 端口：轮次在模型调用处失败，走完整的正式装配与失败路径，
不连接任何外部服务；StarRocks 不会被调用（该轮没有工具调用）。真实模型、StarRocks 与飞书属于
Task 9。成功轮次与飞书路径在 ``test_runtime.py`` 中经同一装配（替换最底层 I/O）验证。
"""

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from pytest_socket import SocketBlockedError
from sqlalchemy.engine import URL
from tests.sdk_core.test_runtime import (
    DB_ENV,
    FEISHU_ENV,
    KEY_ENV,
    MODEL_ENV,
    SR_ENV,
    env_template_values,
    example_with_feishu,
    filled,
    free_port,
    serve_config,
)
from tests.sdk_core.test_vertex_model import VERTEX, Endpoint, _body, _call

from xiaowei import cli as cli_module
from xiaowei import model_api, runtime

pytestmark = pytest.mark.loopback

ROOT = Path(__file__).resolve().parents[2]
ENTRIES = {
    "console": [str(Path(sys.executable).parent / "xiaowei")],
    "module": [sys.executable, "-m", "xiaowei"],
}
CANARY = "xw-cli-canary-41f9"
ENDPOINT = "xw-endpoint-canary-7c2e"  # 只出现在模型端点路径中
PRESET = {"OPENAI_AGENTS_DONT_LOG_MODEL_DATA": "0", "OPENAI_AGENTS_DONT_LOG_TOOL_DATA": "false"}


@pytest.fixture(autouse=True)
def restore_process_logging() -> Iterator[None]:
    """进程内调用 CLI 后恢复日志输出，避免后续测试写入已关闭的 capsys stream。

    正式 CLI 每次只运行一次；测试会在同一进程反复调用 main，须隔离其日志装配副作用。
    不改变被测调用期间的过滤、级别或断言。
    """
    root, own, lark = (logging.getLogger(n) for n in ("", "xiaowei", "Lark"))
    handlers, lark_handlers = list(root.handlers), list(lark.handlers)
    root_level, own_level = root.level, own.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers[:] = handlers
        lark.handlers[:] = lark_handlers
        root.setLevel(root_level)
        own.setLevel(own_level)


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


MAIN_TEMPLATE = ROOT / "examples/xiaowei.example.json"
FIXED = "vertex 的端点与协议由程序固定"
REQUIRED_ENV = ("XW_DATABASE_URL", "XW_DIGEST_KEY", "XW_MODEL_API_KEY", "XW_STARROCKS_PASSWORD")


def _main_template(tmp_path: Path, *, fill: bool = True, **model: object) -> Path:
    values = json.loads(MAIN_TEMPLATE.read_text(encoding="utf-8"))
    values = filled(values) if fill else values
    values["model"] |= model
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    return file


def _required_env(**overrides: str) -> dict[str, str]:
    """只设主模板的必填秘密：不设第二目标、飞书与 PostgreSQL 初始化密码。"""
    values = {name: f"real-{name.lower()}-{CANARY}" for name in REQUIRED_ENV}
    return child_env(**(values | overrides))


@pytest.mark.parametrize("entry", ENTRIES)
def test_filled_minimal_template_passes_config_check(entry: str, tmp_path: Path) -> None:
    """C2 成功场景：主模板只替换文档标为必填的标记、只设必填秘密，正式 config check 即通过。"""
    file = _main_template(tmp_path)
    before = (file.read_bytes(), file.stat().st_mtime_ns)

    result = run([*ENTRIES[entry], "--config", str(file), "config", "check"], _required_env())

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "configuration valid" and result.stderr == ""
    assert (file.read_bytes(), file.stat().st_mtime_ns) == before


@pytest.mark.parametrize(
    ("fill", "model", "env", "field"),
    [
        pytest.param(
            True,
            {"model": "replace-with-approved-vertex-model"},
            {},
            "model.model",
            id="legacy-unreplaced-model",
        ),
        pytest.param(
            True, {"base_url": "https://evil.invalid/v1"}, {}, FIXED, id="vertex-endpoint"
        ),
        pytest.param(True, {"api_mode": "responses"}, {}, FIXED, id="vertex-protocol"),
        pytest.param(True, {"output_mode": "json_object"}, {}, FIXED, id="vertex-output"),
        pytest.param(True, {}, {"XW_MODEL_API_KEY": ""}, "model.api_key_ref", id="model-key"),
        pytest.param(
            True, {}, {"XW_STARROCKS_PASSWORD": ""}, "targets.0.starrocks.password_ref", id="sr-key"
        ),
    ],
)
@pytest.mark.parametrize("entry", ENTRIES)
def test_minimal_template_failures_exit_2(
    entry: str,
    tmp_path: Path,
    fill: bool,
    model: dict[str, str],
    env: dict[str, str],
    field: str,
) -> None:
    """旧标记、Vertex 固定字段被覆盖、模型或目标秘密缺失时退出 2，不回显值。"""
    file = _main_template(tmp_path, fill=fill, **model)
    argv = [*ENTRIES[entry], "--config", str(file), "config", "check"]

    result = run(argv, _required_env(**env))

    assert result.returncode == 2 and result.stdout == ""
    assert field in result.stderr
    assert CANARY not in result.stderr and "evil.invalid" not in result.stderr
    assert "replace-with" not in result.stderr and "<" not in result.stderr


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
        "XW_WEB_BIND_ADDRESS": "127.0.0.1",
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


def test_container_config_check_accepts_any_ip_bind_without_listing_origins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """发布地址只要是 IP 就接受，不要求 ``allowed_origins`` 跟着改；不是 IP 退出 2 且不回显值。"""
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(filled(example_with_feishu()), ensure_ascii=False), encoding="utf-8")
    for address in ("0.0.0.0", "127.0.0.1", "172.20.0.8", "::"):  # noqa: S104 - 内网访问的发行默认值
        _deploy_env(monkeypatch, XW_WEB_BIND_ADDRESS=address)
        assert cli_module.container_main(["config", "check"], config_path=file) == 0
        assert capsys.readouterr().out.strip() == "configuration valid"
    monkeypatch.delenv("XW_WEB_BIND_ADDRESS")
    assert cli_module.container_main(["config", "check"], config_path=file) == 0
    capsys.readouterr()

    _deploy_env(monkeypatch, XW_WEB_BIND_ADDRESS="intranet-host.example")
    assert cli_module.container_main(["config", "check"], config_path=file) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "XW_WEB_BIND_ADDRESS" in captured.err and "intranet-host" not in captured.err


def _template_file(tmp_path: Path) -> Path:
    """带旧模型与飞书占位符的配置，环境变量全部已设置。"""
    file = tmp_path / "xiaowei.json"
    values = example_with_feishu()
    values["model"]["model"] = "replace-with-approved-vertex-model"
    file.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
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
    _deploy_env(monkeypatch)
    started = _no_runtime(monkeypatch)
    file = _template_file(tmp_path)
    before = (file.read_bytes(), file.stat().st_mtime_ns)

    code = _serve(file, container)

    assert code == 2 and started == []
    err = capsys.readouterr().err
    assert "model.model" in err and "feishu.app_id" in err and "<" not in err
    assert (file.read_bytes(), file.stat().st_mtime_ns) == before


TEMPLATE_ENV = (
    "XW_DATABASE_URL",
    "XW_DIGEST_KEY",
    "XW_MODEL_API_KEY",
    "XW_STARROCKS_PASSWORD",
    "XW_ARCHIVE_STARROCKS_PASSWORD",
    "XW_FEISHU_APP_SECRET",
)


def _deploy_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """发行模板引用的变量全部填成真实值（容器入口还需要端口与停止宽限）。"""
    for name in TEMPLATE_ENV:
        monkeypatch.setenv(name, f"real-{name.lower()}")
    monkeypatch.setenv("XW_WEB_PORT", "8501")
    monkeypatch.setenv("XW_WEB_BIND_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("XW_STOP_GRACE_SECONDS", "600")
    monkeypatch.delenv("XW_POSTGRES_PASSWORD", raising=False)
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)


def _serve(file: Path, container: bool) -> int:
    if container:
        return cli_module.container_main(["serve"], config_path=file)
    return cli_module.main(["--config", str(file), "serve"])


@pytest.mark.parametrize("container", [False, True], ids=["native", "container"])
def test_serve_accepts_real_values_that_contain_angle_brackets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, container: bool
) -> None:
    """真实值里出现尖括号不是模板：预检通过并进入运行时（运行时本身被替换，不做外部 I/O）。"""
    _deploy_env(monkeypatch, XW_MODEL_API_KEY="real<中文>secret", XW_POSTGRES_PASSWORD="a<b>c")  # noqa: S106 - 合成值
    started = _no_runtime(monkeypatch)
    values = filled(example_with_feishu())
    values["targets"][0]["description"] = "业务 <生产> 集群"
    values["targets"][0]["starrocks"]["database"] = "<生产>"
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")

    assert _serve(file, container) == 0 and started == ["runtime"]


def test_container_serve_rejects_a_template_postgres_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template = env_template_values()["XW_POSTGRES_PASSWORD"]
    _deploy_env(monkeypatch, XW_POSTGRES_PASSWORD=template)
    started = _no_runtime(monkeypatch)
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(filled(example_with_feishu()), ensure_ascii=False), "utf-8")

    assert _serve(file, container=True) == 2 and started == []
    err = capsys.readouterr().err
    assert "XW_POSTGRES_PASSWORD" in err and template not in err


@pytest.mark.parametrize("entry", ENTRIES)
def test_config_check_rejects_a_template_postgres_password(entry: str, tmp_path: Path) -> None:
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(serve_config(18501)), encoding="utf-8")
    template = env_template_values()["XW_POSTGRES_PASSWORD"]
    env = child_env(
        **{
            DB_ENV: "postgresql+asyncpg://offline.invalid/xiaowei",
            KEY_ENV: f"digest-{CANARY}",
            MODEL_ENV: f"model-{CANARY}",
            SR_ENV: f"starrocks-{CANARY}",
            "XW_POSTGRES_PASSWORD": template,
        }
    )
    result = run([*ENTRIES[entry], "--config", str(file), "config", "check"], env)
    assert result.returncode == 2 and "XW_POSTGRES_PASSWORD" in result.stderr
    assert template not in result.stdout + result.stderr
    secrets_absent(result.stdout + result.stderr)

    argv = [*ENTRIES[entry], "--config", str(file), "config", "check"]
    ok = run(argv, {**env, "XW_POSTGRES_PASSWORD": "a<b>c"})
    assert ok.returncode == 0, ok.stderr


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
            # 内网访问：其他 Host 与 Origin 照常服务；非 JSON 写入仍在路由前拒绝。
            assert client.get("/", headers={"host": "172.20.0.8"}).status_code == 200
            other = client.post("/api/sessions", json={}, headers={"origin": "http://172.20.0.8"})
            assert other.status_code == 200
            assert client.post("/api/sessions", content=b"{}").status_code == 415

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


# ---- model check ---------------------------------------------------------------------

CHECK_TOOL = "model_check_lookup"  # 固定合成工具名：命令与模型之间的唯一契约
CHECK_LEAK = "xw-check-canary-5d1a"  # 只出现在模型返回的正文与工具参数中
VERTEX_KEY_ENV = VERTEX.api_key_ref.removeprefix("env:")
UNRELATED_ENV = (DB_ENV, KEY_ENV, SR_ENV, FEISHU_ENV, *TEMPLATE_ENV, "XW_POSTGRES_PASSWORD")
ADVICE = {"evidence_ids": [], "inferences": [], "clarification": None}


def _check_answer(**overrides: Any) -> dict[str, Any]:
    body = {**ADVICE, "advice": f"合成数值为 42 {CHECK_LEAK}", **overrides}
    return _body([{"text": json.dumps(body, ensure_ascii=False)}])


def _check_call(name: str = CHECK_TOOL) -> dict[str, Any]:
    return _call(name=name, args={"region": CHECK_LEAK})


def _check_entry(container: bool, argv: list[str], file: Path) -> int:
    if container:
        return cli_module.container_main(argv, config_path=file)
    return cli_module.main(["--config", str(file), *argv])


@dataclass
class ModelCheck:
    """只含模型 Key 的环境、一份 CA 不可读的 Vertex 配置，以及记录装配调用的替身。"""

    file: Path
    endpoint: Endpoint
    calls: list[str]

    def run(self, container: bool, *replies: Any) -> int:
        self.endpoint.replies.extend(replies)
        return _check_entry(container, ["model", "check"], self.file)

    def files(self) -> dict[str, tuple[bytes, int]]:
        """工作目录（含配置文件）每个文件的内容与 mtime：改写已有文件也会被发现。"""
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.file.parent.iterdir()}


@pytest.fixture
def model_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, socket_disabled: None
) -> ModelCheck:
    for name in UNRELATED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(VERTEX_KEY_ENV, f"vertex-key-{CHECK_LEAK}")
    # 容器部署参数缺失或非法：模型检查不得读取它们。
    monkeypatch.setenv("XW_WEB_PORT", "not-a-port")
    monkeypatch.setenv("XW_WEB_BIND_ADDRESS", "intranet-host.example")
    monkeypatch.delenv("XW_STOP_GRACE_SECONDS", raising=False)
    config = serve_config(18501, model=VERTEX.model_dump(mode="json"))
    config["targets"][0]["starrocks"].update({"tls": True, "tls_ca_file": str(tmp_path / "no-ca")})
    work = tmp_path / "work"
    work.mkdir()
    file = work / "xiaowei.json"
    file.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.chdir(work)
    endpoint = Endpoint([])
    calls: list[str] = []

    def forbidden(name: str) -> Any:
        def call(*args: object, **kwargs: object) -> Any:
            calls.append(name)
            raise AssertionError(f"model check 不得调用 {name}")

        return call

    monkeypatch.setattr(runtime, "validate_config", forbidden("validate_config"))
    for name in ("open_engine", "open_starrocks", "lark_channel", "_container_serve"):
        monkeypatch.setattr(runtime, name, forbidden(name))

    real_open, real_run_config = runtime.open_model, runtime.safe_run_config

    def open_model(profile: Any, *, transport: Any = None) -> Any:
        calls.append("open_model")
        assert transport is None  # 正式命令不能另配 transport；替身只替换最底层网络发送
        return real_open(profile, transport=endpoint.transport)

    def safe_run_config() -> Any:
        calls.append("safe_run_config")
        return real_run_config()

    monkeypatch.setattr(runtime, "open_model", open_model)
    monkeypatch.setattr(runtime, "safe_run_config", safe_run_config)
    return ModelCheck(file, endpoint, calls)


def _no_leak(text: str) -> None:
    assert CHECK_LEAK not in text and "合成数值" not in text and CHECK_TOOL not in text


@pytest.mark.parametrize("entry", ENTRIES)
def test_model_check_is_listed_by_both_entries(entry: str) -> None:
    assert "model" in run([*ENTRIES[entry], "--help"], child_env()).stdout
    result = run([*ENTRIES[entry], "--config", "unused", "model", "--help"], child_env())
    assert result.returncode == 0 and "check" in result.stdout


@pytest.mark.filterwarnings("ignore:A test tried to use socket:UserWarning")
@pytest.mark.parametrize("container", [False, True], ids=["native", "container"])
def test_model_check_needs_only_the_model_key(
    model_check: ModelCheck, capsys: pytest.CaptureFixture[str], container: bool
) -> None:
    """缺少数据库、摘要、StarRocks、飞书秘密，CA 不可读，容器端口非法：模型检查仍成功。"""
    before = model_check.files()
    with pytest.raises(SocketBlockedError):  # 本用例连 loopback 也不放行：模型替身不经网络
        socket.socket()

    code = model_check.run(container, _check_call(), _check_answer())

    out, err = capsys.readouterr()
    assert code == 0, err
    assert out.strip() == f"model check valid profile={VERTEX.profile_id} model={VERTEX.model}"
    assert err == ""
    _no_leak(out + err)
    assert model_check.calls == ["open_model", "safe_run_config"]
    assert model_check.files() == before
    first, second = model_check.endpoint.bodies()
    (declared,) = first["tools"][0]["functionDeclarations"]
    assert declared["name"] == CHECK_TOOL
    assert first["generationConfig"]["maxOutputTokens"] == VERTEX.max_output_tokens
    assert "responseJsonSchema" in first["generationConfig"]
    (result,) = [
        part["functionResponse"]
        for content in second["contents"]
        for part in content["parts"]
        if "functionResponse" in part
    ]
    assert result["name"] == CHECK_TOOL
    for request in model_check.endpoint.requests:
        assert request.headers["x-goog-api-key"] == f"vertex-key-{CHECK_LEAK}"


@pytest.mark.parametrize("container", [False, True], ids=["native", "container"])
@pytest.mark.parametrize(
    "key", [None, "", "<获准 Model Profile 的密钥>"], ids=["unset", "empty", "template"]
)
def test_model_check_rejects_a_missing_or_template_key_before_any_request(
    model_check: ModelCheck,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    container: bool,
    key: str | None,
) -> None:
    if key is None:
        monkeypatch.delenv(VERTEX_KEY_ENV)
    else:
        monkeypatch.setenv(VERTEX_KEY_ENV, key)

    assert model_check.run(container) == 2

    err = capsys.readouterr().err
    assert "model.api_key_ref" in err and "<" not in err
    assert model_check.calls == [] and model_check.endpoint.requests == []


@pytest.mark.parametrize("container", [False, True], ids=["native", "container"])
def test_model_check_rejects_template_and_invalid_json_before_any_request(
    model_check: ModelCheck, capsys: pytest.CaptureFixture[str], container: bool
) -> None:
    config = json.loads(model_check.file.read_text(encoding="utf-8"))
    original = config["targets"][0]["description"]
    config["targets"][0]["description"] = "<集群用途，交给模型选择集群>"
    model_check.file.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    assert model_check.run(container) == 2
    err = capsys.readouterr().err
    assert "targets.0.description" in err and "<" not in err

    config["targets"][0]["description"] = original
    config["model"]["unexpected"] = True
    model_check.file.write_text(json.dumps(config), encoding="utf-8")
    assert model_check.run(container) == 2
    assert "model.unexpected" in capsys.readouterr().err
    assert model_check.calls == [] and model_check.endpoint.requests == []


def _status(code: int) -> httpx2.Response:
    return httpx2.Response(code, json={"error": {"message": f"upstream {CHECK_LEAK}"}})


async def _timeout(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("synthetic timeout", request=request)


@pytest.mark.parametrize(
    ("replies", "reason"),
    [
        pytest.param([_status(401)], "auth_failed", id="401"),
        pytest.param([_status(403)], "auth_failed", id="403"),
        pytest.param([_status(429)], "rate_limited", id="429"),
        pytest.param([_status(503)], "upstream_error", id="5xx"),
        pytest.param([_timeout], "unreachable", id="timeout"),
        pytest.param([_check_answer()], "tool_not_called", id="no-tool"),
        pytest.param(
            [_check_call(), _check_call(), _check_answer()], "tool_repeated", id="tool-twice"
        ),
        pytest.param(
            [_check_call(), _check_call(), _check_call()], "tool_repeated", id="tool-loop"
        ),
        pytest.param([_check_call(name="run_readonly_query")], "model_failed", id="wrong-tool"),
        pytest.param([_check_call(), _body([{"text": "not json"}])], "model_failed", id="not-json"),
        pytest.param([_check_call(), _check_answer(advice=None)], "answer_invalid", id="no-advice"),
        pytest.param(
            [_check_call(), _check_answer(evidence_ids=["ev-1"])],
            "answer_invalid",
            id="evidence",
        ),
        pytest.param(
            [_check_call(), _check_answer(inferences=[{"text": "推断", "evidence_ids": []}])],
            "answer_invalid",
            id="inference",
        ),
        pytest.param(
            [_check_call(), _check_answer(clarification="请说明")],
            "answer_invalid",
            id="clarification",
        ),
    ],
)
def test_model_check_failures_exit_1_with_a_fixed_reason(
    model_check: ModelCheck,
    capsys: pytest.CaptureFixture[str],
    replies: list[Any],
    reason: str,
) -> None:
    before = model_check.files()

    assert model_check.run(False, *replies) == 1

    out, err = capsys.readouterr()
    assert out == "" and err.strip() == f"model check failed: {reason}"
    _no_leak(err)
    assert model_check.endpoint.replies == []  # 没有额外请求：不重试
    assert model_check.calls == ["open_model", "safe_run_config"]
    assert model_check.files() == before


@pytest.mark.parametrize("container", [False, True], ids=["native", "container"])
def test_model_check_close_failure_is_a_fixed_reason(
    model_check: ModelCheck,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    container: bool,
) -> None:
    """模型客户端关闭超时：不报告通过，按固定类别失败，不输出模型客户端的说明。"""

    async def hang() -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(model_api, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(httpx2.MockTransport, "aclose", lambda self: hang())

    assert model_check.run(container, _check_call(), _check_answer()) == 1

    out, err = capsys.readouterr()
    assert out == "" and err == "model check failed: model_failed\n"


@pytest.mark.parametrize("container", [False, True], ids=["native", "container"])
def test_model_check_open_failure_is_a_fixed_reason(
    model_check: ModelCheck,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    container: bool,
) -> None:
    """模型客户端在进入阶段失败（真实 open_model 内部）：固定类别，不含异常原文，无请求。"""

    def broken(ref: str) -> Any:
        raise RuntimeError(f"open failed {CHECK_LEAK}")

    monkeypatch.setattr(model_api, "resolve_secret_ref", broken)
    before = model_check.files()

    assert model_check.run(container) == 1

    out, err = capsys.readouterr()
    assert out == "" and err == "model check failed: model_failed\n"
    assert model_check.endpoint.requests == [] and model_check.files() == before


def _break_ca(monkeypatch: pytest.MonkeyPatch, file: Path) -> None:
    config = json.loads(file.read_text(encoding="utf-8"))
    config["targets"][0]["starrocks"].update({"tls": True, "tls_ca_file": str(file.parent)})
    file.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(
    "breaks",
    [
        pytest.param(lambda mp, file: mp.delenv("XW_DIGEST_KEY"), id="missing-secret"),
        pytest.param(lambda mp, file: mp.delenv("XW_FEISHU_APP_SECRET"), id="missing-feishu"),
        pytest.param(_break_ca, id="unreadable-ca"),
        pytest.param(lambda mp, file: mp.setenv("XW_WEB_PORT", "not-a-port"), id="port"),
        pytest.param(
            lambda mp, file: mp.setenv("XW_WEB_BIND_ADDRESS", "intranet-host.example"),
            id="bind-address",
        ),
        pytest.param(lambda mp, file: mp.setenv("XW_STOP_GRACE_SECONDS", "1"), id="stop-grace"),
    ],
)
@pytest.mark.parametrize("argv", [["serve"], ["config", "check"]], ids=["serve", "config-check"])
def test_other_container_commands_keep_the_full_precheck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    breaks: Callable[[pytest.MonkeyPatch, Path], None],
) -> None:
    """对照：容器 serve / config check 仍调用完整预检；缺秘密、CA 不可读、端口或宽限非法时拒绝。"""
    _deploy_env(monkeypatch)
    started = _no_runtime(monkeypatch)
    file = tmp_path / "xiaowei.json"
    file.write_text(json.dumps(filled(example_with_feishu()), ensure_ascii=False), "utf-8")
    checked: list[str] = []
    real = runtime.validate_config

    def spy(config: runtime.ServeConfig, **kwargs: Any) -> None:
        checked.append("validate_config")
        real(config, **kwargs)

    monkeypatch.setattr(runtime, "validate_config", spy)
    assert cli_module.container_main(argv, config_path=file) == 0  # 成功对照
    assert checked == ["validate_config"]
    started.clear()

    breaks(monkeypatch, file)

    assert cli_module.container_main(argv, config_path=file) == 2
    assert started == []
