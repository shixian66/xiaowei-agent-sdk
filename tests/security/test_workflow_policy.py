"""CI workflow 的安全策略约束。

**设计取向：闭集白名单，而非 denylist。** denylist 只能挡住已知的具体写法，
`toJSON(github)`、`github['token']`、`curl <外部 API>` 等等价形式会不断绕过。
本文件因此对 Actions 表达式、`uses:` 引用和全部 `run` 命令做**精确闭集校验**：
任何新增或改动都会使测试变红，必须显式更新白名单，从而强制人工审查。

用标准库读文本 + 精确匹配，不引入 YAML parser。
"""

import hashlib
import re
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_WF = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
_TEXT = _WF.read_text(encoding="utf-8")
_LINES = _TEXT.splitlines()
_SDK_COMPOSE = _WF.parents[2] / "compose.sdk-test.yml"
_SDK_COMPOSE_TEXT = _SDK_COMPOSE.read_text(encoding="utf-8")

_EXPECTED_JOBS = (
    "tests",
    "integration",
    "compose-smoke",
    "security-gate",
    "lint",
    "types",
    "deps-audit",
    "secret-scan",
)

# ---- 承重锚：整个 ci.yml 的 SHA-256 --------------------------------------
# 契约是「任何 ci.yml 改动都必须转红并接受人工审查」。集合式白名单只能挡住
# 「多出的东西」，挡不住删除必需命令、重复摘要顶替、把配置挪到无关 action 下、
# 或加 `continue-on-error` 让 gate 形同虚设。整文件摘要是唯一能覆盖全部
# 增/删/改/移位的锚点；合法修改 workflow 时必须显式更新此常量。
_WORKFLOW_SHA256 = "b371b1108b059ad8b81fd1fca875fbf50815ce2b20486d732309866ba8f86cea"

# ---- 闭集白名单：改动 ci.yml 必须同步更新此处，否则测试变红 ----------------
_ALLOWED_EXPRESSIONS = {"github.ref"}

_ALLOWED_USES = {
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9",
}

# 多行 run block 的规范化 SHA-256；改一个字符即变红。
_ALLOWED_RUN_BLOCK_DIGESTS = {
    "14dccc18ea3ff5aa544415f4682995d6076e600dd7708d760aebcb0373229e62",  # install gitleaks
    "1542f514fb26fe1fec603de711f032493d5f46f74c7edfb1f7ef4340209f2ca5",  # scanner self-test
    # allowlist narrowness self-test
    "f7e4098014484c0b069b5769ea455b97cf25e34fa331636174cd3ab84ed2c475",
    "28ac15963a97048c3ef875b67c01f1d1525928d052c4ea96b7d3aa4797494467",  # SDK test harness
}


def _run_commands_and_block_digests() -> tuple[list[str], list[str]]:
    """提取全部 run 步骤：单行命令原文，多行 block 归一化后取摘要。"""
    single: list[str] = []
    digests: list[str] = []
    index = 0
    while index < len(_LINES):
        match = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", _LINES[index])
        if not match:
            index += 1
            continue
        indent, rest = match.group(1), match.group(2).strip()
        if rest == "|":
            body: list[str] = []
            index += 1
            while index < len(_LINES) and (
                not _LINES[index].strip() or _LINES[index].startswith(indent + "  ")
            ):
                body.append(_LINES[index].strip())
                index += 1
            normalized = "\n".join(line for line in body if line)
            digests.append(hashlib.sha256(normalized.encode()).hexdigest())
            continue
        single.append(rest)
        index += 1
    return single, digests


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


def test_uses_occurrence_counts_are_exact() -> None:
    """每个 job 恰好一次 checkout；仅 secret-scan 不使用 setup-uv。"""
    assert _TEXT.count("uses: actions/checkout@") == len(_EXPECTED_JOBS)
    assert _TEXT.count("uses: astral-sh/setup-uv@") == len(_EXPECTED_JOBS) - 1


def test_workflow_file_digest_is_pinned() -> None:
    """整文件摘要：覆盖新增、删除、重复顶替、移位和控制属性等全部改动形态。

    合法修改 `ci.yml` 时必须同步更新 `_WORKFLOW_SHA256`，从而强制人工审查。
    下面的语义测试只用于给出可读的失败原因，不能替代本断言。
    """
    actual = hashlib.sha256(_WF.read_bytes()).hexdigest()
    assert actual == _WORKFLOW_SHA256, (
        f"ci.yml 已改动：实际摘要 {actual}，白名单为 {_WORKFLOW_SHA256}。"
        "确认改动经过审查后再更新常量。"
    )


def test_single_line_run_commands_match_exactly() -> None:
    """精确多重集：既拒绝多余命令，也拒绝删除必需命令。"""
    single, _ = _run_commands_and_block_digests()
    expected = Counter(
        {
            "uv sync --extra dev --frozen": 7,
            # 旧套件维持默认跳过；SDK PostgreSQL 全套在 integration 的 digest-pinned block 中运行。
            "python -m pytest -q --ignore=tests/sdk_core": 1,
            "python -m pytest -m security -q": 1,
            "python -m scripts.compose_smoke": 1,
            "ruff check .": 1,
            "mypy src": 1,
            "uv export --frozen --no-emit-project --extra dev -o requirements-audit.txt": 1,
            "pip-audit --strict -r requirements-audit.txt": 1,
            './gitleaks git --log-opts="--all" --redact --no-banner .': 1,
        }
    )
    assert Counter(single) == expected, f"run 命令多重集不符: {Counter(single)}"


def test_multiline_run_blocks_match_exactly() -> None:
    """精确多重集：防止用一个合法 block 重复顶替另一个必需 block。"""
    _, digests = _run_commands_and_block_digests()
    assert Counter(digests) == Counter(dict.fromkeys(_ALLOWED_RUN_BLOCK_DIGESTS, 1)), (
        f"多行 run block 多重集不符: {Counter(digests)}"
    )


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


def test_job_set_is_exactly_the_approved_eight() -> None:
    assert sorted(_job_ids()) == sorted(_EXPECTED_JOBS), f"实际 job 集合 = {_job_ids()}"


# ---- 结构与权限约束 --------------------------------------------------------


def test_workflow_exists() -> None:
    assert _WF.is_file()


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
        "bb3e1a57e5407e0a5280b4211980a5e537f4abd234a87014ac979849a78dd825"
    ]


def test_the_integration_job_runs_no_extra_command() -> None:
    """unit 与全套路径各有一条精确 pytest 命令；integration 包含全套路径。"""
    assert _TEXT.count("- run: python -m pytest -q --ignore=tests/sdk_core\n") == 1
    assert _job_text("integration").count("python -m pytest -q\n") == 1
    assert "compose.sdk-test.yml up -d --wait" in _job_text("integration")
    assert "compose.sdk-test.yml down -v" in _job_text("integration")


def test_runner_is_pinned_not_latest() -> None:
    assert "ubuntu-latest" not in _TEXT
    assert _TEXT.count("runs-on: ubuntu-24.04") == len(_EXPECTED_JOBS)


def test_all_eight_gates_present_with_stable_names() -> None:
    for job in _EXPECTED_JOBS:
        assert re.search(rf"(?m)^    name: {re.escape(job)}$", _TEXT), f"缺少 check 名 {job}"


def test_gates_run_adr008_commands_verbatim() -> None:
    assert "python -m pytest -q\n" in _job_text("integration")
    for cmd in ("python -m pytest -m security -q", "ruff check .", "mypy src"):
        assert f"- run: {cmd}\n" in _TEXT, f"未原样执行 ADR-008 命令: {cmd}"


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
