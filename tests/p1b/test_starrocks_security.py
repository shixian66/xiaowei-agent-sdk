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
    ("SELECT * FROM shop.t WHERE id=1 OR 1=1 -- probe", "boolean_probe", set()),
    ("SELECT * FROM shop.t WHERE id=1 OR ('x'='x')", "boolean_probe", set()),
    ("SELECT IF(1=1,SLEEP(5),0)", "delay_call", set()),
    ("SELECT 1; DROP TABLE shop.t", "stacked_statements", {"drop"}),
    ("GRANT SELECT ON shop.t TO someone", "high_risk_operation", {"grant"}),
    ("REVOKE SELECT ON shop.t FROM someone", "high_risk_operation", {"revoke"}),
    ("CREATE USER someone IDENTIFIED BY 'dummy'", "high_risk_operation", {"account"}),
    ("ALTER USER someone IDENTIFIED BY 'dummy'", "high_risk_operation", {"account"}),
    ("DROP ROLE someone", "high_risk_operation", {"account"}),
    ("SET PASSWORD FOR someone = 'dummy'", "high_risk_operation", {"password"}),
    ("TRUNCATE TABLE shop.t", "high_risk_operation", {"truncate"}),
    ("EXPORT TABLE shop.t TO 'dummy'", "high_risk_operation", {"export"}),
    ("INSERT OVERWRITE shop.t SELECT 1", "high_risk_operation", {"insert_overwrite"}),
    ("DELETE FROM shop.t", "high_risk_operation", {"delete_no_where"}),
    ("WITH x AS (SELECT id FROM shop.t WHERE id>0) DELETE FROM shop.t",
     "high_risk_operation", {"delete_no_where"}),
]


@pytest.mark.parametrize(("sql", "signal", "operations"), SIGNAL_CASES)
def test_explainable_signals(sql, signal, operations):
    result = analyze(batch(sql))
    assert result.matched_rows == 1
    assert signal in result.rows[0]["signals"]
    assert set(result.rows[0]["operations"]) == operations


@pytest.mark.parametrize("sql", [
    "SELECT 1 UNION ALL SELECT 2", "SELECT * FROM shop.t WHERE 1=1",
    "SELECT 'sleep(5); DROP TABLE shop.t; OR 1=1'",
    "SELECT 1 /* sleep(5); DROP TABLE shop.t; OR 1=1 */",
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
    ), sample_limit=2)
    assert (result.scanned_rows, result.matched_rows, result.failed_rows) == (12, 3, 2)
    assert result.signal_counts == {
        "boolean_probe": 1, "stacked_statements": 0, "delay_call": 1,
        "high_risk_operation": 1,
    }
    assert len(result.rows) == 2 and result.omitted_samples == 1
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


def test_s4_grant_is_not_hidden_by_earlier_etl_truncates():
    result = analyze(batch(*(["TRUNCATE TABLE shop.t"] * 25),
                           "GRANT SELECT ON shop.t TO someone"))
    assert result.matched_rows == 26
    assert result.operation_counts["truncate"] == 25
    assert result.operation_counts["grant"] == 1
    assert "q25" in {row["query_id"] for row in result.rows}
    assert len(result.rows) == 20 and result.omitted_samples == 6


def test_multiple_operations_in_one_audit_row_are_not_lost_or_double_counted():
    result = analyze(batch("GRANT SELECT ON shop.t TO someone; DROP TABLE shop.t; "
                           "DROP TABLE shop.other"))
    assert result.matched_rows == 1
    assert result.signal_counts["stacked_statements"] == 1
    assert result.signal_counts["high_risk_operation"] == 1
    assert result.operation_counts["grant"] == result.operation_counts["drop"] == 1
    assert set(result.rows[0]["operations"]) == {"grant", "drop"}


def test_s5_distinguishes_absent_text_from_unread_sql_and_reports_scan_end():
    result = analyze(batch(None, None, "SELECT 'unterminated", truncated=True,
                           changes={1: {"stmt_present": True}}))
    assert result.scanned_rows == 3
    assert result.window_oversize_rows == 1
    assert result.unclassified_candidate_rows == 2
    assert result.scan_limited and result.truncated
    assert not result.match_counts_complete
    assert result.scanned_through == "2026-10-23T10:00:02+00:00"


def test_empty_window_has_no_invented_scan_end():
    result = analyze(batch())
    assert (result.scanned_rows, result.failed_rows, result.matched_rows) == (0, 0, 0)
    assert result.scanned_through is None and not result.truncated


def test_raw_sql_and_parser_error_text_are_not_output_or_product_logs(caplog):
    result = analyze(batch("SET PASSWORD FOR someone = 'secret-canary-42'",
                           "DELETE FROM 'secret-canary-43' WHERE"))
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


def test_deep_delete_parse_failure_preserves_other_signals_and_rows():
    nested = "DELETE FROM t WHERE x = " + "(" * 150 + "SLEEP(1)" + ")" * 150
    result = analyze(batch("SELECT SLEEP(1)", nested))
    assert result.matched_rows == 2 and result.signal_counts["delay_call"] == 2
    assert result.unclassified_candidate_rows == 1
    assert not result.match_counts_complete and result.truncated
