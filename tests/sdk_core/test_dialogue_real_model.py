"""真实模型采集命令的离线回归；所有模型 I/O 显式替换，不调用实际端点。"""

import io
import json
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


async def test_interactive_review_has_live_answer_and_only_fixed_verdict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(command.sys, "stdin", io.StringIO("bad\ny\nn\na\na\ny\ny\na\n"))
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
    assert len(report["harness_sha256"]) == 64
    assert report["profile_fingerprint"] == profile_fingerprint(PROFILE)
    assert report["samples"][0]["review"]["evidence_consistent"] is True
    encoded = json.dumps(report, ensure_ascii=False)
    assert all(
        s not in encoded for s in (message, "COLLECT_ANSWER_SENTINEL", "SELECT", "ev_", "https:")
    )


def test_noninteractive_cli_refuses_before_loading_profile_or_calling_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command.sys, "stdin", io.StringIO())
    monkeypatch.setattr(command.sys, "argv", ["dialogue_real_model", "--profile", "unused.json"])
    monkeypatch.setattr(command, "load_profile", lambda _: pytest.fail("不能触发模型前提读取"))
    assert command.main() == 2
