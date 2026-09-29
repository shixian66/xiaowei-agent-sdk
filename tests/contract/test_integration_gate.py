"""integration gate 不可静默死亡。

DSN 已设置却整组跳过，是 CI 里最危险的一种绿：DSN 拼错、service 没起来、迁移失败
全都长成这个样子，而 M4 的全部退出标准都建立在那些用例真的跑过之上。

判定写成纯函数，因此可以在**不真的制造一次跳过**的前提下测试它自己——否则这条
护栏只能靠"某次 CI 恰好出问题"来验证。
"""

import os
import re
from pathlib import Path

from sqlalchemy.engine import make_url
from tests.integration.conftest import unexpected_integration_skips
from tests.sdk_core.conftest import POSTGRES_URL_ENV
from tests.sdk_core.postgres_harness import ADMIN_DATABASE, ADMIN_USER, LOOPBACK, PORT

_WORKFLOW_TEXT = (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text(
    encoding="utf-8"
)
_ROOT = Path(__file__).resolve().parents[2]
_SDK_COMPOSE_TEXT = (_ROOT / "compose.sdk-test.yml").read_text(encoding="utf-8")

_INSIDE = f"tests{os.sep}integration{os.sep}test_lease_fencing_postgres.py::test_x"
_OUTSIDE = f"tests{os.sep}security{os.sep}test_no_network.py::test_y"


def test_no_dsn_means_skips_are_expected() -> None:
    """没有 DSN 时跳过是正常的，不能报错——否则本机开发一律红。"""
    assert unexpected_integration_skips([_INSIDE], dsn=None) == []


def test_dsn_set_makes_an_integration_skip_an_error() -> None:
    assert unexpected_integration_skips([_INSIDE], dsn="postgresql://x/y") == [_INSIDE]


def test_skips_outside_the_integration_directory_are_ignored() -> None:
    """目录外的跳过与这条 gate 无关，不能误报——误报会让人把整条 gate 关掉。"""
    assert unexpected_integration_skips([_OUTSIDE], dsn="postgresql://x/y") == []


def test_the_check_reports_every_offender_not_just_the_first() -> None:
    other = _INSIDE.replace("test_x", "test_z")
    assert unexpected_integration_skips([_INSIDE, _OUTSIDE, other], dsn="postgresql://x/y") == [
        _INSIDE,
        other,
    ]


# --- CI 必须接入 SDK 自己的 PostgreSQL harness ---------------------------------
#
# 旧 integration 套件在本地无 DSN 时允许跳过；SDK 核心的 PostgreSQL 用例则在地址缺失
# 时直接失败。CI 使用 SDK 的一次性 Compose 实例，因此这里将工作流 env、harness 常量、
# Compose loopback 端口和完整 pytest 命令绑定起来，避免换回旧服务后仍然“通过”。


def _declared_env_names(text: str) -> set[str]:
    """工作流里所有 ``NAME: value`` 形态的 env 键。"""
    return set(re.findall(r"(?m)^\s+([A-Z][A-Z0-9_]*):\s", text))


def test_the_workflow_declares_the_exact_env_var_the_gate_reads() -> None:
    """工作流与 SDK fixture 必须使用同一环境变量。"""
    assert POSTGRES_URL_ENV in _declared_env_names(_WORKFLOW_TEXT), (
        f"ci.yml 没有声明 {POSTGRES_URL_ENV}：SDK PostgreSQL 用例会拒绝启动"
    )


def test_the_env_name_binding_is_discriminating() -> None:
    """反空洞：新 harness 已接管 CI，不应遗留旧数据库服务的环境变量。"""
    names = _declared_env_names(_WORKFLOW_TEXT)
    assert f"{POSTGRES_URL_ENV}_RENAMED" not in names
    assert "SDK_TEST_POSTGRES_URL_TYPO" not in names
    assert "PYTEST_POSTGRES_DSN" not in names


def test_the_workflow_url_points_at_the_local_sdk_compose_port() -> None:
    """SDK DSN 必须与受限的 loopback PostgreSQL Compose 端口一致。"""
    match = re.search(rf"(?m)^\s+{POSTGRES_URL_ENV}:\s*(\S+)$", _WORKFLOW_TEXT)
    assert match, f"ci.yml 里 {POSTGRES_URL_ENV} 没有取值"
    url = make_url(match.group(1))
    assert (url.drivername, url.username, url.password, url.host, url.port, url.database) == (
        "postgresql+asyncpg",
        ADMIN_USER,
        None,
        LOOPBACK,
        PORT,
        ADMIN_DATABASE,
    )
    assert f"{LOOPBACK}:{PORT}:5432" in _SDK_COMPOSE_TEXT


def test_the_integration_job_runs_and_cleans_up_the_sdk_harness() -> None:
    """integration job 必须跑全套测试，并在退出时清理一次性数据库。"""
    match = re.search(r"(?ms)^  integration:\n(.*?)(?=^  [\w-]+:\n|\Z)", _WORKFLOW_TEXT)
    assert match, "ci.yml 缺少 integration job"
    job = match.group(1)

    assert "compose.sdk-test.yml up -d --wait" in job
    assert "python -m pytest -q" in job
    assert "trap cleanup EXIT" in job
    assert "compose.sdk-test.yml down -v" in job
