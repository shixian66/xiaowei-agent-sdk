"""CI workflow 的安全策略约束。

**设计取向：闭集白名单，而非 denylist。** denylist 只能挡住已知的具体写法，
`toJSON(github)`、`github['token']`、`curl <外部 API>` 等等价形式会不断绕过。
本文件因此对 Actions 表达式和 `uses:` 引用做闭集校验，并约束权限、凭据、env、
gate 存在与不可绕过；其余 `ci.yml` 改动由 PR 审查负责，不在这里逐字钉死。

用标准库读文本 + 精确匹配，不引入 YAML parser。
"""

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_WF = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
_TEXT = _WF.read_text(encoding="utf-8")
_LINES = _TEXT.splitlines()
_SDK_COMPOSE = _WF.parents[2] / "compose.sdk-test.yml"
_SDK_COMPOSE_TEXT = _SDK_COMPOSE.read_text(encoding="utf-8")


def _declares_marker(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets):
            continue
        candidates = (
            stmt.value.elts if isinstance(stmt.value, ast.List | ast.Tuple) else [stmt.value]
        )
        for item in candidates:
            if (
                isinstance(item, ast.Attribute)
                and item.attr == "security"
                and isinstance(item.value, ast.Attribute)
                and item.value.attr == "mark"
            ):
                return True
    return False


def test_every_module_under_tests_security_declares_the_marker() -> None:
    root = Path(__file__).resolve().parent
    missing = [p.name for p in sorted(root.glob("test_*.py")) if not _declares_marker(p)]
    assert missing == [], f"以下安全测试模块未声明 security marker: {missing}"


def test_marker_detector_is_not_fooled_by_text(tmp_path: Path) -> None:
    faker = tmp_path / "test_faker.py"
    faker.write_text('"""pytestmark = pytest.mark.security"""\n', encoding="utf-8")
    assert not _declares_marker(faker)
    real = tmp_path / "test_real.py"
    real.write_text("import pytest\n\npytestmark = pytest.mark.security\n", encoding="utf-8")
    assert _declares_marker(real)
    listed = tmp_path / "test_listed.py"
    listed.write_text(
        "import pytest\n\npytestmark = [pytest.mark.security, pytest.mark.slow]\n",
        encoding="utf-8",
    )
    assert _declares_marker(listed)


_EXPECTED_JOBS = (
    "tests",
    "integration",
    "p3-release",
    "security-gate",
    "lint",
    "types",
    "deps-audit",
    "secret-scan",
    # P1-B Task 8：wheel 在只含锁定运行依赖的干净环境中安装，两个正式入口都只指向新包。
    "product-entry",
)

# ---- 闭集白名单：改动 ci.yml 必须同步更新此处，否则测试变红 ----------------
_ALLOWED_EXPRESSIONS = {"github.ref"}

_ALLOWED_USES = {
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9",
}


def _job_ids() -> list[str]:
    start = next(i for i, line in enumerate(_LINES) if line.rstrip() == "jobs:")
    ids: list[str] = []
    for line in _LINES[start + 1 :]:
        if line.strip() and not line.startswith(" "):
            break
        match = re.fullmatch(r"  ([A-Za-z_][\w-]*):", line.rstrip())
        if match:
            ids.append(match.group(1))
    return ids


def _job_text(job_id: str) -> str:
    match = re.search(rf"(?ms)^  {re.escape(job_id)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", _TEXT)
    assert match, f"缺少 job: {job_id}"
    return match.group(1)


# ---- 闭集断言 --------------------------------------------------------------


def test_actions_expressions_are_a_closed_set() -> None:
    """只允许 `github.ref`。`toJSON(github)` 会间接带出 `github.token`。"""
    used = set(re.findall(r"\$\{\{\s*(.+?)\s*\}\}", _TEXT))
    unknown = sorted(used - _ALLOWED_EXPRESSIONS)
    assert not unknown, f"出现未批准的表达式: {unknown}"


def test_uses_references_are_a_closed_set_with_exact_shas() -> None:
    used = set(re.findall(r"uses:\s*(\S+)", _TEXT))
    assert used == _ALLOWED_USES, f"uses 集合不符: {sorted(used ^ _ALLOWED_USES)}"
    for ref in used:
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref), f"未钉到 40 位 commit SHA: {ref}"


def test_no_step_level_control_attributes() -> None:
    """`continue-on-error` 会让失败的 gate 仍然显示成功；`if` 可让 gate 被跳过。"""
    for attr in ("continue-on-error", "if:"):
        assert attr not in _TEXT, f"禁止使用 {attr}，会使 gate 形同虚设"


def test_each_checkout_step_binds_persist_credentials() -> None:
    """必须绑定到 checkout 自身，而非全局计数——否则可挪到无关 action 输入下凑数。"""
    steps = 0
    for index, line in enumerate(_LINES):
        if "uses: actions/checkout@" not in line:
            continue
        steps += 1
        indent = len(line) - len(line.lstrip())
        body: list[str] = []
        for follow in _LINES[index + 1 :]:
            if follow.strip() and (len(follow) - len(follow.lstrip())) <= indent:
                break
            body.append(follow.strip())
        assert "persist-credentials: false" in body, f"第 {steps} 个 checkout 未绑定该配置"
    assert steps == len(_EXPECTED_JOBS)


def test_jobs_that_build_historical_revisions_fetch_full_history() -> None:
    for job in ("integration", "p3-release"):
        assert _job_text(job).count("fetch-depth: 0") == 1


def test_job_set_is_exactly_the_approved_set() -> None:
    assert sorted(_job_ids()) == sorted(_EXPECTED_JOBS), f"实际 job 集合 = {_job_ids()}"


# ---- 结构与权限约束 --------------------------------------------------------


def test_no_pull_request_target() -> None:
    assert "pull_request_target" not in _TEXT


def test_no_secrets_key_anywhere_including_inherit() -> None:
    hits = re.findall(r"(?m)^[ \t]*secrets\s*:", _TEXT)
    assert hits == [], f"禁止 workflow/job 级 secrets 键，实际命中 {hits}"


def test_exactly_one_permissions_block_and_it_is_workflow_level() -> None:
    blocks = re.findall(r"(?m)^([ \t]*)permissions:", _TEXT)
    assert blocks == [""], f"只允许一个顶层 permissions 块，实际缩进集合={blocks}"


def test_workflow_permissions_are_read_only() -> None:
    block = re.search(r"(?m)^permissions:\n((?:  .*\n)+)", _TEXT)
    assert block, "缺少顶层 permissions 块"
    assert block.group(1).strip() == "contents: read"


def test_no_write_or_write_all_permission_anywhere() -> None:
    assert "write-all" not in _TEXT
    assert not re.search(r"(?m)^\s+\w[\w-]*:\s*write\s*$", _TEXT)


def test_no_env_block_outside_declared_allowlist() -> None:
    allowed = {
        "GITLEAKS_VERSION",
        "GITLEAKS_SHA256",
        "SDK_TEST_POSTGRES_URL",
    }
    names = set(re.findall(r"(?m)^\s+([A-Z][A-Z0-9_]*):\s", _TEXT))
    assert names <= allowed, f"出现未声明的 env 变量: {sorted(names - allowed)}"


def test_env_values_carry_no_credentials() -> None:
    """env 取值里不得出现任何凭证。

    与 ``test_secret_shaped_literals.py`` 互补：那边扫的是源码，这边扫的是 workflow。
    SDK PostgreSQL URL 是这里唯一一个"长得像连接串"的取值，因此单独钉死其形状——**无密码段**。
    """
    values = re.findall(r"(?m)^\s+[A-Z][A-Z0-9_]*:\s*(.+)$", _TEXT)
    for value in values:
        assert "password" not in value.lower()
        # user:password@host 形态。DSN 里只允许 user@host。
        assert not re.search(r"//[^/\s]+:[^/\s@]+@", value), value


def test_workflow_does_not_declare_service_containers() -> None:
    """CI 复用本地隔离 harness，不额外保留旧 PostgreSQL service。"""
    assert not re.search(r"(?m)^\s*services:\s*$", _TEXT)
    assert not re.search(r"(?m)^\s+image:\s", _TEXT)


def test_sdk_postgres_compose_image_is_digest_pinned() -> None:
    """integration 使用的本地 Compose 镜像必须固定到已审查的内容 digest。"""
    images = re.findall(r"(?m)^\s+image:\s*(\S+)$", _SDK_COMPOSE_TEXT)
    assert images == [
        "postgres:16.15-bookworm@sha256:"
        "efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
    ]


def test_runner_is_pinned_not_latest() -> None:
    assert "ubuntu-latest" not in _TEXT
    assert _TEXT.count("runs-on: ubuntu-24.04") == len(_EXPECTED_JOBS)


def test_all_gates_present_with_stable_names() -> None:
    for job in _EXPECTED_JOBS:
        assert re.search(rf"(?m)^    name: {re.escape(job)}$", _TEXT), f"缺少 check 名 {job}"


def test_gates_run_approved_commands_verbatim() -> None:
    assert "python -m pytest -q --ignore=tests/deployment --durations=30\n" in _job_text(
        "integration"
    )
    for cmd in ("python -m pytest -m security -q", "ruff check .", "mypy src"):
        assert f"- run: {cmd}\n" in _TEXT, f"未原样执行批准的门禁命令: {cmd}"


def test_secret_scan_uses_full_history_and_pinned_checksum() -> None:
    assert "fetch-depth: 0" in _TEXT
    assert re.search(r'GITLEAKS_SHA256:\s*"[0-9a-f]{64}"', _TEXT)
    assert "sha256sum -c -" in _TEXT


def test_gates_are_not_piped() -> None:
    for line in _LINES:
        if line.strip().startswith("- run:"):
            assert "|" not in line or line.strip().endswith("run: |"), (
                f"gate 命令不得接管道，退出码会被吞: {line.strip()}"
            )
