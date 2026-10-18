"""CI 必须使用产品的隔离 PostgreSQL harness，并执行完整测试与退出清理。"""

import re
from pathlib import Path

from sqlalchemy.engine import make_url
from tests.sdk_core.conftest import POSTGRES_URL_ENV
from tests.sdk_core.postgres_harness import ADMIN_DATABASE, ADMIN_USER, LOOPBACK, PORT

_WORKFLOW_TEXT = (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text(
    encoding="utf-8"
)
_ROOT = Path(__file__).resolve().parents[2]
_SDK_COMPOSE_TEXT = (_ROOT / "compose.sdk-test.yml").read_text(encoding="utf-8")


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
