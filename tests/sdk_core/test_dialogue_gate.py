"""U1 实测框架的离线验证：协议替身 + 正式 runtime + PostgreSQL，不证明模型选择。"""

import json

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import URL
from tests.sdk_core.dialogue_gate import SCENARIOS, Scenario, configuration, run_dialogue
from tests.sdk_core.test_app import Scripts, cite, tool_call
from tests.sdk_core.test_model_api import OPENAI as PROFILE
from tests.sdk_core.test_starrocks_tools import tool_outputs

from xiaowei.storage import open_engine

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


def test_matrix_covers_ambiguous_database_natural_diagnosis_and_raw_formats() -> None:
    assert any(getattr(s, "ambiguous_database", False) for s in SCENARIOS)
    natural = next(s for s in SCENARIOS if s.name == "previous_diagnosis_natural")
    assert natural.messages[1][0] == "query"
    for field in ("raw", "sql_text", "result_json"):
        for length in (3900, 4100):
            assert any(
                getattr(s, "raw_field", None) == field and s.raw_chars == length for s in SCENARIOS
            )


async def test_manual_review_reads_validated_turn_before_cleanup_without_leaking_report(
    postgres_url: URL,
) -> None:
    scripts = Scripts()
    message = scripts.add(
        "REVIEW_REQUEST_SENTINEL",
        tool_call(
            "run_readonly_query",
            cluster="fat",
            sql="SELECT region, amount FROM data_center.quota_virtual_account_quasi_rt LIMIT 5",
        ),
        cite("REVIEW_ANSWER_SENTINEL"),
    )
    case = Scenario("offline_review", (("query", message),), multi=True)
    seen = []

    async def review(sample):
        # 人工拿到的是真正交付前验证过的当前轮结果，且数据库尚未退出/清理。
        from tests.sdk_core.dialogue_gate import ReviewVerdict

        async with open_engine(SecretStr(postgres_url.render_as_string(hide_password=False))) as e:
            async with e.connect() as conn:
                assert await conn.scalar(text("SELECT count(*) FROM xiaowei_request")) == 1
        assert sample.delivery is not None and len(sample.delivery.facts[0].rows) == 5
        assert sample.delivery.analysis[0].text == "REVIEW_ANSWER_SENTINEL"
        assert sample.message == message
        assert [(target, kind) for target, kind, _ in sample.statements if kind == "query"] == [
            ("fat", "query")
        ]
        seen.append(sample)
        return ReviewVerdict(intent_preserved=True, execution_allowed=True)

    (result,) = await run_dialogue(
        PROFILE, postgres_url, scripts.transport, scenarios=(case,), repeats=1, manual_review=review
    )
    assert len(seen) == 1 and not result["manual_review_required"]
    assert result["review"]["intent_preserved"] is True
    assert result["review"]["execution_allowed"] is True
    assert result["review"]["object_selection_correct"] is None
    assert result["io_by_target"]["fat"]["query"] == 1
    assert result["facts"][0]["target"] == "fat"
    encoded = json.dumps(result, ensure_ascii=False)
    assert all(s not in encoded for s in (message, "REVIEW_ANSWER_SENTINEL", "SELECT", "ev_"))


@pytest.mark.parametrize("field", ["sql_text", "result_json"])
@pytest.mark.parametrize("length", [3900, 4100])
async def test_raw_format_samples_reach_real_value_boundary(
    postgres_url: URL,
    field: str,
    length: int,
) -> None:
    scripts = Scripts()
    message = scripts.add(
        "原文格式边界",
        tool_call(
            "run_readonly_query",
            cluster="pre",
            sql={
                "sql_text": (
                    "SELECT sql_text FROM data_center.quota_virtual_account_quasi_rt LIMIT 1"
                ),
                "result_json": (
                    "SELECT result_json FROM data_center.quota_virtual_account_quasi_rt LIMIT 1"
                ),
            }[field],
        ),
        cite("原文或已标注的截断结果"),
    )
    seen = []

    async def review(sample):
        from tests.sdk_core.dialogue_gate import ReviewVerdict

        assert sample.delivery is not None
        fact = sample.delivery.facts[0]
        assert fact.columns == (field,) and len(fact.rows) == 1
        assert fact.truncated == (length > 4000)
        if length <= 4000:
            assert len(fact.rows[0][field]) == length
        seen.append(sample)
        return ReviewVerdict(evidence_consistent=True)

    (result,) = await run_dialogue(
        PROFILE,
        postgres_url,
        scripts.transport,
        repeats=1,
        manual_review=review,
        scenarios=(
            Scenario("offline_format", (("query", message),), raw_field=field, raw_chars=length),
        ),
    )
    assert len(seen) == 1 and result["io_counts"]["query"] == 1
    assert result["raw_not_pretrimmed"]


def test_manual_verdict_rejects_free_text_or_unknown_fields() -> None:
    from pydantic import ValidationError
    from tests.sdk_core.dialogue_gate import ReviewVerdict

    with pytest.raises(ValidationError):
        ReviewVerdict(intent_preserved="REVIEW_TEXT_SENTINEL")
    with pytest.raises(ValidationError):
        ReviewVerdict(comment="REVIEW_TEXT_SENTINEL")
