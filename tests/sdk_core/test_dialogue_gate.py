"""U1 实测框架的离线验证：协议替身 + 正式 runtime + PostgreSQL，不证明模型选择。"""

import json

import pytest
from sqlalchemy.engine import URL
from tests.sdk_core.dialogue_gate import SCENARIOS, Scenario, configuration, run_dialogue
from tests.sdk_core.test_app import Scripts, cite, tool_call
from tests.sdk_core.test_model_api import OPENAI as PROFILE
from tests.sdk_core.test_starrocks_tools import tool_outputs

pytestmark = pytest.mark.loopback


@pytest.fixture(autouse=True)
def fake_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROFILE.api_key_ref.removeprefix("env:"), "u1-synthetic-model-key")


def test_fixed_samples_use_deployment_limits_and_independent_targets() -> None:
    cfg = configuration(PROFILE, next(s for s in SCENARIOS if s.multi))
    assert {t.target_id for t in cfg.targets} == {"pre", "fat"}
    for target in cfg.targets:
        assert target.starrocks.max_value_bytes == 4000
        assert target.starrocks.max_result_bytes == 200000
        assert target.starrocks.host == "synthetic.invalid" and target.starrocks.pool_size == 4


async def test_dialogue_harness_runs_success_and_hidden_tool_failure(postgres_url: URL) -> None:
    scripts = Scripts()
    described = scripts.add(
        "结构",
        tool_call(
            "describe_table",
            cluster="pre",
            database="data_center",
            table="quota_virtual_account_quasi_rt",
            cursor=None,
        ),
        cite(),
    )
    forbidden = scripts.add(
        "不可执行",
        tool_call(
            "run_readonly_query",
            cluster="pre",
            sql="SELECT region FROM data_center.quota_virtual_account_quasi_rt",
        ),
    )
    case = Scenario("offline_path", (("query", described), ("diagnose", forbidden)))
    reports = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(case,), repeats=1
    )
    assert reports[0]["state"] == "completed"
    assert reports[0]["facts"][0]["tool"] == "local/describe_table"
    assert reports[1]["state"] == "failed" and reports[1]["no_query_in_diagnosis"]
    assert "query" not in reports[1]["io_counts"]
    encoded = json.dumps(reports, ensure_ascii=False)
    assert described not in encoded and forbidden not in encoded and "ev_" not in encoded
    assert "SELECT" not in encoded and "synthetic.invalid" not in encoded
    assert all(r["manual_review_required"] for r in reports)


async def test_empty_middle_page_is_observed_on_real_governance_path(postgres_url: URL) -> None:
    scripts = Scripts()

    def next_page(call):
        cursor = json.loads(tool_outputs(call)[-1])["data"]["next_cursor"]
        assert cursor is not None
        return tool_call(
            "list_tables",
            cluster="pre",
            database="data_center",
            keyword=None,
            page_size=200,
            cursor=cursor,
        )(call)

    message = scripts.add(
        "三页",
        tool_call(
            "list_tables",
            cluster="pre",
            database="data_center",
            keyword=None,
            page_size=200,
            cursor=None,
        ),
        next_page,
        next_page,
        cite("已续取到游标为空；中间页无当前可读对象"),
    )
    case = Scenario("offline_pages", (("query", message),), pages=True)
    (result,) = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(case,), repeats=1
    )
    assert result["state"] == "completed"
    assert [f["rows"] for f in result["facts"]] == [200, 0, 1]
    assert [f["has_cursor"] for f in result["facts"]] == [True, True, False]
    assert "query" not in result["io_counts"]


async def test_raw_over_limit_does_not_require_another_query(postgres_url: URL) -> None:
    scripts = Scripts()
    message = scripts.add(
        "完整原文",
        tool_call(
            "run_readonly_query",
            cluster="pre",
            sql="SELECT raw FROM data_center.quota_virtual_account_quasi_rt LIMIT 1",
        ),
        cite("单值超过边界，只取得部分原文，不能声称完整"),
    )
    case = Scenario("offline_raw", (("query", message),), raw_chars=4100)
    (result,) = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(case,), repeats=1
    )
    assert result["state"] == "completed" and result["facts"][0]["truncated"]
    assert result["io_counts"]["query"] == 1 and result["raw_not_pretrimmed"]
