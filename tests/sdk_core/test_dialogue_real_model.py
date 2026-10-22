"""真实模型采集命令的离线回归；所有模型 I/O 显式替换，不调用实际端点。"""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from scripts import dialogue_real_model as command
from tests.sdk_core.dialogue_gate import ReviewSample, ReviewVerdict, Scenario
from tests.sdk_core.dialogue_gate import run_dialogue as actual_run
from tests.sdk_core.test_app import Scripts, cite, tool_call
from tests.sdk_core.test_model_api import OPENAI as PROFILE

from xiaowei.model_api import profile_fingerprint
from xiaowei.models import Delivery

pytestmark = pytest.mark.loopback

BASE = "8503099fb403afb2cc5bc4da0eab3b22618606f2"


def _source_tree(path: Path, *, baseline: bool = False, revision: str | None = None) -> str:
    """真实 Git 源码树；产品来自基线/候选，评估框架仍只使用同一份。"""
    path.mkdir()
    git = shutil.which("git")
    assert git is not None
    if baseline or revision is not None:
        archive = subprocess.check_output(  # noqa: S603 -- fixed Git archive; no shell.
            [git, "archive", revision or BASE, "src"], cwd=command.ROOT
        )
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(path, filter="data")
    else:
        shutil.copytree(
            command.ROOT / "src", path / "src", ignore=shutil.ignore_patterns("__pycache__")
        )
    (path / ".gitignore").write_text("__pycache__/\n")
    for args in (
        ("init", "-q"),
        ("add", "."),
        (
            "-c",
            "user.name=Offline test",
            "-c",
            "user.email=offline@example.invalid",
            "commit",
            "-qm",
            "synthetic source",
        ),
    ):
        subprocess.run([git, *args], cwd=path, check=True, capture_output=True)  # noqa: S603
    return subprocess.check_output([git, "rev-parse", "HEAD"], cwd=path, text=True).strip()  # noqa: S603


def _probe(path: Path, program: str) -> dict:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(path / "src"), str(command.ROOT))),
        "PYTHONDONTWRITEBYTECODE": "1",
        PROFILE.api_key_ref.removeprefix("env:"): "offline-key",
    }
    result = subprocess.run(  # noqa: S603 -- current interpreter and fixed offline program.
        [sys.executable, "-c", program],
        cwd=path.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_collector_binds_actual_pythonpath_source_and_sent_instructions(tmp_path: Path) -> None:
    program = """
import asyncio, json
from scripts import dialogue_real_model as command
from tests.sdk_core.dialogue_gate import ReviewVerdict, Scenario, run_dialogue
from tests.sdk_core.test_app import Scripts, cite, tool_call
from tests.sdk_core.test_model_api import OPENAI as PROFILE
command.load_profile = lambda _: PROFILE
command.check_postgres = lambda: None
from agents.testing import assistant_message
from tests.sdk_core.test_app import evidence_in
from xiaowei import models
scripts = Scripts()
def cite_for_source(call):
    # 基线源码早于 S3，只接受旧回答格式；当前源码只接受新格式。
    if hasattr(models, "LegacyAnswer"):
        return cite()(call)
    ids = evidence_in(call)
    body = {"evidence_ids": ids, "inferences": [], "clarification": None}
    return [assistant_message(json.dumps(body))]
message = scripts.add(
    "SOURCE_BINDING_SENTINEL",
    tool_call("list_databases", cluster="pre", cursor=None, page_size=20), cite_for_source,
)
async def offline_run(profile, url, network, **kwargs):
    return await run_dialogue(
        profile, url, scripts.transport,
        scenarios=(Scenario("offline", (("query", message),)),), **kwargs,
    )
async def review(_):
    return ReviewVerdict(evidence_consistent=True)
command.run_dialogue = offline_run
command.review_sample = review
print(json.dumps(asyncio.run(command.collect(None, 1))))
"""
    reports = []
    for baseline in (True, False):
        path = tmp_path / ("baseline" if baseline else "candidate")
        sha = _source_tree(path, baseline=baseline)
        report = _probe(path, program)
        assert report["source_sha"] == sha
        assert report["samples"][0]["state"] == "completed"
        assert "SOURCE_BINDING_SENTINEL" not in json.dumps(report)
        requests = report["samples"][0]["requests"]
        assert len(requests) == 2
        assert all(len(r["instructions_sha256"]) == 64 for r in requests)
        assert all(len(r["tool_descriptions_sha256"]) == 64 for r in requests)
        reports.append(report)
    assert reports[0]["source_sha"] != reports[1]["source_sha"]
    assert reports[0]["harness_sha256"] == reports[1]["harness_sha256"]
    old, new = (r["samples"][0]["requests"][0] for r in reports)
    assert old["instructions_sha256"] != new["instructions_sha256"]
    assert old["tool_descriptions_sha256"] != new["tool_descriptions_sha256"]


@pytest.mark.parametrize("change", ["tracked", "untracked", "no_git"])
def test_dirty_or_unversioned_source_refuses_before_profile_or_io(
    tmp_path: Path, change: str
) -> None:
    path = tmp_path / "product"
    _source_tree(path)
    if change == "no_git":
        shutil.rmtree(path / ".git")
    elif change == "tracked":
        with (path / "src/xiaowei/app.py").open("a") as stream:
            stream.write("\n# DIRTY_SENTINEL\n")
    else:
        (path / "DIRTY_SENTINEL").write_text("untracked")
    report = _probe(
        path,
        """
import asyncio, json
from scripts import dialogue_real_model as command
def forbidden(_):
    raise AssertionError("PROFILE_OR_IO_SENTINEL")
command.load_profile = forbidden
try:
    asyncio.run(command.collect(None, 1))
except command.ConfigurationError as exc:
    print(json.dumps({"error": str(exc)}))
else:
    raise AssertionError("必须拒绝")
""",
    )
    assert report["error"] and "SENTINEL" not in json.dumps(report)


@pytest.mark.parametrize("changed", ["source", "harness"])
def test_version_change_during_collection_does_not_produce_report(
    tmp_path: Path, changed: str
) -> None:
    path = tmp_path / "product"
    _source_tree(path)
    program = """
import asyncio, json
from pathlib import Path
import xiaowei
from scripts import dialogue_real_model as command
from tests.sdk_core.test_model_api import OPENAI as PROFILE
command.load_profile = lambda _: PROFILE
command.check_postgres = lambda: None
async def offline_run(*args, **kwargs):
    CHANGE_VERSION
    return []
command.run_dialogue = offline_run
try:
    asyncio.run(command.collect(None, 1))
except command.ConfigurationError as exc:
    print(json.dumps({"error": str(exc)}))
else:
    raise AssertionError("版本变化后必须拒绝报告")
"""
    mutation = (
        'Path(xiaowei.__file__).with_name("app.py").write_text("# DIRTY_SENTINEL")'
        if changed == "source"
        else 'command.harness_sha256 = lambda: "changed"'
    )
    report = _probe(path, program.replace("CHANGE_VERSION", mutation))
    assert report["error"] and "SENTINEL" not in json.dumps(report)


async def test_interactive_review_has_live_answer_and_only_fixed_verdict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(command.sys, "stdin", io.StringIO("bad\ny\nn\na\na\na\ny\ny\na\n"))
    sample = ReviewSample(
        scenario="offline",
        repeat=1,
        turn=1,
        mode="query",
        message="LIVE_REQUEST_SENTINEL",
        state="completed",
        failure=None,
        delivery=Delivery(content="LIVE_ANSWER_SENTINEL", channel="web", evidence_ids=()),
        statements=(("pre", "query", "SELECT SQL_SENTINEL"),),
    )
    verdict = await command.review_sample(sample)
    assert isinstance(verdict, ReviewVerdict)
    assert verdict.intent_preserved and verdict.execution_allowed is False
    assert verdict.object_selection_correct is None
    shown = capsys.readouterr()
    assert shown.out == ""
    assert all(s in shown.err for s in (sample.message, "LIVE_ANSWER_SENTINEL", "SQL_SENTINEL"))
    assert "SENTINEL" not in json.dumps(verdict.model_dump())


async def test_env_review_shows_selected_environment_and_asks_two_more_items(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(command.sys, "stdin", io.StringIO("y\n" * 8 + "n\na\n"))
    sample = ReviewSample(
        scenario="env_select_followup_switch",
        repeat=1,
        turn=2,
        mode="query",
        message="用 fat",
        state="completed",
        failure=None,
        delivery=None,
        statements=(),
    )
    verdict = await command.review_sample(sample)
    assert verdict.environment_scope_correct is False
    assert verdict.diagnosis_basis_sufficient is None
    assert "本轮用户明确的环境：fat" in capsys.readouterr().err


async def test_incomplete_review_fails_instead_of_fabricating_judgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command.sys, "stdin", io.StringIO("y\n"))
    sample = ReviewSample(
        scenario="offline",
        repeat=1,
        turn=1,
        mode="diagnose",
        message="合成失败",
        state="failed",
        failure="model_failed",
        delivery=None,
        statements=(),
    )
    with pytest.raises(command.ConfigurationError, match="人工核对未完成"):
        await command.review_sample(sample)


async def test_collector_binds_worktree_and_profile_without_content_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "offline-key")
    monkeypatch.setattr(command, "load_profile", lambda path: PROFILE)
    monkeypatch.setattr(command, "check_postgres", lambda: None)
    monkeypatch.chdir(tmp_path)  # 不能把调用目录误写成被测试工作树。
    # 本用例检查内容/配置投影；实际源码版本绑定另用两个干净 Git 树的子进程验证。
    monkeypatch.setattr(command, "source_sha", lambda: "a" * 40)
    scripts = Scripts()
    message = scripts.add(
        "COLLECT_REQUEST_SENTINEL",
        tool_call("list_databases", cluster="pre", cursor=None, page_size=20),
        cite("COLLECT_ANSWER_SENTINEL"),
    )
    samples = []

    async def review(sample):
        samples.append(sample)
        assert sample.delivery is not None
        return ReviewVerdict(evidence_consistent=True)

    async def offline_run(profile, url, network, **kwargs):
        # 刻意不调用 network；底层只使用脚本协议替身，装配/PG/治理仍走正式路径。
        return await actual_run(
            profile,
            url,
            scripts.transport,
            scenarios=(Scenario("offline", (("query", message),)),),
            **kwargs,
        )

    monkeypatch.setattr(command, "run_dialogue", offline_run)
    monkeypatch.setattr(command, "review_sample", review)
    report = await command.collect(tmp_path / "unused-profile.json", 1)
    assert len(samples) == 1 and len(report["source_sha"]) == 40
    assert report["source_sha"] == "a" * 40
    assert len(report["harness_sha256"]) == 64
    assert report["profile_fingerprint"] == profile_fingerprint(PROFILE)
    assert report["samples"][0]["review"]["evidence_consistent"] is True
    encoded = json.dumps(report, ensure_ascii=False)
    assert all(
        s not in encoded for s in (message, "COLLECT_ANSWER_SENTINEL", "SELECT", "ev_", "https:")
    )


@pytest.mark.parametrize("suite", ["u1", "p4", "monitoring-reliability", "security"])
def test_noninteractive_cli_refuses_before_loading_profile_or_calling_model(
    monkeypatch: pytest.MonkeyPatch,
    suite: str,
) -> None:
    monkeypatch.setattr(command.sys, "stdin", io.StringIO())
    monkeypatch.setattr(
        command.sys, "argv", ["dialogue_real_model", "--profile", "unused.json", "--suite", suite]
    )
    monkeypatch.setattr(command, "load_profile", lambda _: pytest.fail("不能触发模型前提读取"))
    assert command.main() == 2


@pytest.mark.parametrize(("suite", "expected"), [("security", 1), ("u1", 3)])
def test_security_suite_defaults_to_one_run_without_changing_old_suites(
    monkeypatch, suite, expected,
):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    seen = []

    async def collect(path, repeats, name):
        seen.append((repeats, name))
        return {"samples": []}

    monkeypatch.setattr(command.sys, "stdin", Terminal())
    monkeypatch.setattr(command.sys, "stderr", Terminal())
    monkeypatch.setattr(command.sys, "argv", [
        "dialogue_real_model", "--profile", "unused.json", "--suite", suite,
    ])
    monkeypatch.setattr(command, "collect", collect)
    assert command.main() == 0
    assert seen == [(expected, suite)]


async def test_monitoring_review_shows_case_criteria_without_injecting_them(monkeypatch, capsys):
    from tests.sdk_core.monitoring_reliability import CASES, SCENARIOS

    assert command.SUITES["monitoring-reliability"] == SCENARIOS
    case = CASES[0]
    monkeypatch.setattr(command.sys, "stdin", io.StringIO("a\n" * 8 + "y\nn\ny\na\n"))
    sample = ReviewSample(
        scenario=case.scenario.name, repeat=1, turn=1, mode="query",
        message=case.scenario.messages[0][1], state="completed", failure=None,
        delivery=None, statements=(),
    )
    verdict = await command.review_sample(sample)
    shown = capsys.readouterr()
    assert shown.out == "" and case.criteria in shown.err
    assert verdict.environment_scope_correct is True
    assert verdict.diagnosis_basis_sufficient is False
    assert verdict.query_constraints_preserved is True
    assert verdict.answer_focused is None
    assert case.criteria not in json.dumps(verdict.model_dump())


async def test_p4_review_shows_actual_channel_material_and_separate_quality_questions(
    monkeypatch, capsys
):
    from tests.sdk_core.dialogue_delivery import ChannelDelivery

    from xiaowei.feishu_report import ReportFile

    captured = ChannelDelivery(0, messages=[{"text": "VISIBLE_SENTINEL"}], state="unknown")
    captured.files.append(ReportFile("md", "p4.md", b"FILE_SENTINEL", "0" * 64))
    sample = ReviewSample(
        scenario="p4_original_dialogue",
        repeat=1,
        turn=1,
        mode="query",
        message="REQUEST_SENTINEL",
        state="completed",
        failure=None,
        delivery=Delivery(content="INTERNAL_SENTINEL", channel="feishu", evidence_ids=()),
        statements=(("fat", "query", "SQL_SENTINEL"),),
        channel_delivery=captured,
        monitoring_calls=(("prometheus-prod", "query", {"query": "PROMQL_SENTINEL"}),),
    )
    monkeypatch.setattr(command.sys, "stdin", io.StringIO("a\n" * 8 + "y\nn\na\n"))
    verdict = await command.review_sample(sample)
    shown = capsys.readouterr()
    assert shown.out == ""
    assert all(
        value in shown.err
        for value in (
            "VISIBLE_SENTINEL",
            "FILE_SENTINEL",
            "SQL_SENTINEL",
            "PROMQL_SENTINEL",
            "unknown",
        )
    )
    assert verdict.query_constraints_preserved is True
    assert verdict.answer_focused is False
    assert verdict.history_reuse_correct is None
    assert "SENTINEL" not in json.dumps(verdict.model_dump())


def test_p4_collector_runs_same_harness_on_repair_baseline_and_candidate(tmp_path):
    program = """
import asyncio, json
from scripts import dialogue_real_model as command
from dataclasses import replace
from tests.sdk_core.dialogue_gate import P4_SCENARIOS, run_dialogue
from tests.sdk_core.test_app import Scripts, answer, evidence_in, tool_call
from tests.sdk_core.test_autonomy_tasks import TOP3_SQL
from tests.sdk_core.test_model_api import OPENAI as PROFILE
command.load_profile = lambda _: PROFILE
command.check_postgres = lambda: None
case = next(s for s in P4_SCENARIOS if s.name == "p4_original_dialogue")
case = replace(case, messages=(case.messages[1],), environments=(case.environments[1],))
scripts = Scripts()
scripts.add(case.messages[0][1],
    tool_call("run_readonly_query", cluster="fat", sql=TOP3_SQL),
    lambda call: answer(evidence_in(call), "HARNESS_SENTINEL", report_format="md"))
async def offline_run(profile, url, network, **kwargs):
    kwargs["scenarios"] = (case,)
    return await run_dialogue(profile, url, scripts.transport, **kwargs)
command.run_dialogue = offline_run
command.review_sample = lambda sample: asyncio.sleep(0, result=command.ReviewVerdict())
print(json.dumps(asyncio.run(command.collect(None, 1, "p4"))))
"""
    reports = []
    for label, revision in (
        ("baseline", "bb0b1e75ead3e12e608e17eeeb83285dd7feb90d"),
        ("candidate", None),
    ):
        path = tmp_path / label
        sha = _source_tree(path, revision=revision)
        report = _probe(path, program)
        sample = report["samples"][0]
        assert report["source_sha"] == sha
        assert sample["state"] == "completed" and sample["delivery"]["state"] == "sent"
        assert sample["delivery"]["file_count"] >= 1
        assert sample["manual_review_required"] is True
        assert "HARNESS_SENTINEL" not in json.dumps(report)
        reports.append(report)
    assert reports[0]["source_sha"] != reports[1]["source_sha"]
    assert reports[0]["harness_sha256"] == reports[1]["harness_sha256"]
