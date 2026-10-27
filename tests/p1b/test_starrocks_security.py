"""安全巡检的合成金标；不连接数据库，不执行被审计 SQL。"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from xiaowei.starrocks import SecurityAuditResult

NOW = datetime(2026, 10, 23, 10, tzinfo=UTC)


def batch(*statements, truncated=False, changes=None, totals=None):
    rows = []
    for i, stmt in enumerate(statements):
        row = {
            "queryId": f"q{i}",
            "timestamp": (NOW + timedelta(seconds=i)).isoformat(),
            "stmt": stmt,
            "stmt_present": stmt is not None and stmt != "",
            "state": "OK",
            "queryTime": 5000,
            "pendingTimeMs": 0,
            "user": "etl",
            "clientIp": "192.0.2.1",
            "db": "shop",
        }
        row.update((changes or {}).get(i, {}))
        rows.append(row)
    return SecurityAuditResult(
        target_id="pre", sql="trusted collection template", columns=tuple(rows[0]) if rows else (),
        rows=tuple(rows), row_count=len(rows), truncated=truncated, collected_at=NOW,
        elapsed_ms=1,
        **(totals or {
        "window_rows": len(rows),
        "window_failed_rows": sum(row["state"] == "ERR" for row in rows),
        "window_oversize_rows": sum(row["stmt_present"] and row["stmt"] is None for row in rows),
        }),
    )


def analyze(result, **kwargs):
    from xiaowei.starrocks_security import analyze_security_rows

    return analyze_security_rows(result, sample_limit=kwargs.pop("sample_limit", 20), **kwargs)


SIGNAL_CASES = [
    ("SELECT * FROM shop.t WHERE id=1 OR 1=1 -- probe", "boolean_probe"),
    ("SELECT * FROM shop.t WHERE id=1 OR ('x'='x')", "boolean_probe"),
    ("SELECT IF(1=1,SLEEP(5),0)", "delay_call"),
    ("SELECT 1; DROP TABLE shop.t", "stacked_statements"),
    ("INSERT OVERWRITE t SELECT 0 OR 1=1", "boolean_probe"),
    ("DELETE FROM t WHERE 0=1 OR 1=1", "boolean_probe"),
]
OPERATION_CASES = [
    "GRANT SELECT ON shop.t TO someone",
    "REVOKE SELECT ON shop.t FROM someone",
    "CREATE USER someone IDENTIFIED BY 'dummy'",
    "ALTER USER someone IDENTIFIED BY 'dummy'",
    "DROP ROLE someone", "SET PASSWORD FOR someone = 'dummy'",
    "DROP TABLE shop.t", "DROP DATABASE shop", "TRUNCATE TABLE shop.t",
    "EXPORT TABLE shop.t TO 'dummy'", "INSERT OVERWRITE shop.t SELECT 1",
    "INSERT /*+ SET_VAR(a=1) */ OVERWRITE t SELECT 1",
    "/* ordinary comment */ INSERT OVERWRITE shop.t SELECT 1",
    "DELETE FROM shop.t",
    "WITH x AS (SELECT id FROM shop.t WHERE id>0) DELETE FROM shop.t",
]


@pytest.mark.parametrize(("sql", "signal"), SIGNAL_CASES)
def test_explainable_signals(sql, signal):
    result = analyze(batch(sql))
    assert result.matched_rows == 1
    assert result.rows[0]["signals"] == [signal]
    assert "operations" not in result.rows[0]
    assert result.unclassified_candidate_rows == 0 and result.match_counts_complete


@pytest.mark.parametrize("sql", OPERATION_CASES)
def test_operation_type_alone_is_not_a_security_signal(sql):
    result = analyze(batch(sql))
    payload = result.as_payload()
    assert result.matched_rows == 0 and result.rows == ()
    assert payload["rules_version"] == "manual-security-v2"
    assert payload["operation_counts"] == {}
    assert "high_risk_operation" not in payload["signal_counts"]
    assert "operations" not in payload["columns"]
    assert result.match_counts_complete


@pytest.mark.parametrize("sql", [
    "SELECT 1 UNION ALL SELECT 2", "SELECT * FROM shop.t WHERE 1=1",
    "SELECT 'sleep(5); DROP TABLE shop.t; OR 1=1'",
    "SELECT 1 /* sleep(5); DROP TABLE shop.t; OR 1=1 */",
    "/*+ SET_VAR(a=1) */",
    "-- sleep(5); DROP TABLE shop.t; OR 1=1\nSELECT 1",
    "SELECT 1;", "SELECT ';'", "SELECT `SLEEP` FROM shop.t",
    "DELETE FROM shop.t WHERE id=1", "INSERT INTO shop.t SELECT 1",
    "SELECT * FROM shop.t WHERE a=1 OR b=1",
])
def test_normal_business_counterexamples(sql):
    result = analyze(batch(sql))
    assert result.matched_rows == 0
    assert result.unclassified_candidate_rows == 0


def test_s1_audit_row_counts_are_not_sample_counts():
    result = analyze(batch(
        "SELECT * FROM shop.t WHERE id=1 OR 1=1", "SELECT SLEEP(5)",
        "GRANT SELECT ON shop.t TO someone",
        changes={1: {"state": "ERR"}},
        totals={"window_rows": 12, "window_failed_rows": 2, "window_oversize_rows": 0},
    ), sample_limit=1)
    assert (result.scanned_rows, result.matched_rows, result.failed_rows) == (12, 2, 2)
    assert result.signal_counts == {
        "boolean_probe": 1, "stacked_statements": 0, "delay_call": 1,
    }
    assert len(result.rows) == 1 and result.omitted_samples == 1
    assert result.truncated
    assert result.checked_candidate_rows == 3
    assert result.match_counts_complete  # 展示省略不改变分类计数。


def test_s2_failure_and_pending_are_preserved_without_inventing_condition_evidence():
    result = analyze(batch("SELECT IF(1=1,SLEEP(5),0)", "SELECT * FROM shop.t",
                           changes={0: {"state": "ERR"}, 1: {"pendingTimeMs": 5000}}))
    assert result.matched_rows == 1
    assert result.rows[0]["state"] == "ERR"
    assert result.rows[0]["requested_delay_seconds"] == 5
    assert "has_condition_keyword" not in result.rows[0]


@pytest.mark.parametrize(("sql", "expected"), [
    ("SELECT SLEEP(0.5)", 0.5), ("SELECT SLEEP(2+3)", None),
    ("SELECT SLEEP(2), SLEEP(3)", None), ("SELECT SLEEP(1e9999)", None),
])
def test_delay_argument_is_only_a_single_finite_constant(sql, expected):
    result = analyze(batch(sql))
    assert result.rows[0]["requested_delay_seconds"] == expected


def test_attack_representatives_precede_auxiliary_rows_with_two_sample_slots():
    result = analyze(batch(*(["SHOW VARIABLES; SELECT 1"] * 25),
                           "SELECT 0 OR 1=1", "SELECT SLEEP(1)"), sample_limit=2)
    assert result.matched_rows == 27
    assert result.signal_counts == {
        "boolean_probe": 1, "delay_call": 1, "stacked_statements": 25,
    }
    assert {row["query_id"] for row in result.rows} == {"q25", "q26"}
    assert result.omitted_samples == 25 and result.match_counts_complete


def test_multiple_operations_in_one_audit_row_are_only_auxiliary():
    result = analyze(batch("GRANT SELECT ON shop.t TO someone; DROP TABLE shop.t; "
                           "DROP TABLE shop.other"))
    assert result.matched_rows == 1
    assert result.rows[0]["signals"] == ["stacked_statements"]
    assert result.signal_counts == {
        "boolean_probe": 0, "delay_call": 0, "stacked_statements": 1,
    }
    assert result.as_payload()["operation_counts"] == {}


def test_s5_distinguishes_absent_text_from_unread_sql_and_reports_scan_end():
    result = analyze(batch(None, None, "SELECT 'unterminated", truncated=True,
                           changes={1: {"stmt_present": True}}))
    assert result.scanned_rows == 3
    assert result.window_oversize_rows == 1
    assert result.unclassified_candidate_rows == 2
    assert result.scan_limited and result.truncated
    assert not result.match_counts_complete
    assert result.scanned_through is None


@pytest.mark.parametrize("truncated", [False, True])
def test_candidate_time_does_not_claim_a_scan_watermark(truncated):
    result = analyze(batch("SELECT SLEEP(5)", truncated=truncated, totals={
        "window_rows": 29232, "window_failed_rows": 6, "window_oversize_rows": 3,
    }))
    assert result.as_payload()["coverage"]["scanned_through"] is None
    assert (result.scanned_rows, result.failed_rows, result.matched_rows) == (29232, 6, 1)
    assert result.match_counts_complete is not truncated


def test_empty_window_has_no_invented_scan_end():
    result = analyze(batch())
    assert (result.scanned_rows, result.failed_rows, result.matched_rows) == (0, 0, 0)
    assert result.scanned_through is None and not result.truncated


def test_raw_sql_and_parser_error_text_are_not_output_or_product_logs(caplog):
    result = analyze(batch("SET PASSWORD FOR someone = 'secret-canary-42'",
                           "SELECT 'secret-canary-43"))
    encoded = json.dumps(result.as_payload(), ensure_ascii=False)
    assert "secret-canary" not in encoded
    assert "statement_shape" not in encoded and "missing_fields" not in encoded
    assert all("secret-canary" not in r.getMessage() for r in caplog.records
               if r.name.startswith("xiaowei"))
    assert result.unclassified_candidate_rows == 1


def test_non_candidate_oversize_rows_do_not_report_truncation_or_incomplete_matches():
    result = analyze(batch("SELECT SLEEP(5)", totals={
        "window_rows": 237398, "window_failed_rows": 3, "window_oversize_rows": 135,
    }))
    assert not result.truncated
    assert result.match_counts_complete
    coverage = result.as_payload()["coverage"]
    assert coverage["window_oversize_rows"] == 135
    assert coverage["unclassified_candidate_rows"] == 0
    assert "uninspected_sql_rows" not in coverage


def test_no_candidates_retains_nonempty_window_totals_and_has_no_scan_watermark():
    result = analyze(batch(totals={
        "window_rows": 237398, "window_failed_rows": 3, "window_oversize_rows": 135,
    }))
    assert (result.scanned_rows, result.failed_rows, result.matched_rows) == (237398, 3, 0)
    assert result.scanned_through is None
    assert result.checked_candidate_rows == 0
    assert result.match_counts_complete and not result.truncated


def test_unread_candidates_make_match_counts_lower_bounds_even_when_read_texts_are_valid():
    result = analyze(batch("SELECT SLEEP(5)", truncated=True))
    assert result.matched_rows == 1 and result.unclassified_candidate_rows == 0
    assert not result.match_counts_complete and result.truncated


def test_exhausted_processing_deadline_cannot_return_a_success():
    with pytest.raises(TimeoutError):
        analyze(batch("SELECT SLEEP(5)"), deadline=0)


def test_removed_delete_ast_check_does_not_mark_delay_candidates_unclassified():
    nested = "DELETE FROM t WHERE x = " + "(" * 150 + "SLEEP(1)" + ")" * 150
    result = analyze(batch("SELECT SLEEP(1)", nested))
    assert result.matched_rows == 2 and result.signal_counts["delay_call"] == 2
    assert result.unclassified_candidate_rows == 0
    assert result.match_counts_complete and not result.truncated
