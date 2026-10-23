"""有限的审计安全线索分析：只消费已读取记录，无 I/O，也不返回被审计 SQL。"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, cast

from sqlglot import Token, TokenType, exp, parse_one
from sqlglot.dialects.starrocks import StarRocks
from sqlglot.errors import ParseError, TokenError

from xiaowei.starrocks import Scalar, SecurityAuditResult

SECURITY_TOOL: Final = "local/inspect_audit_security"
SIGNALS: Final = ("boolean_probe", "stacked_statements", "delay_call", "high_risk_operation")
OPERATIONS: Final = (
    "grant", "revoke", "account", "password", "drop", "truncate", "export",
    "insert_overwrite", "delete_no_where",
)
SECURITY_COLUMNS: Final = (
    "query_id", "timestamp", "state", "query_time_ms", "user", "client_ip", "database",
    "pending_time_ms", "signals", "operations", "requested_delay_seconds",
)
_METADATA: Final = {
    "query_id": "queryId", "timestamp": "timestamp", "state": "state",
    "query_time_ms": "queryTime", "user": "user", "client_ip": "clientIp",
    "database": "db", "pending_time_ms": "pendingTimeMs",
}


@dataclass(frozen=True)
class SecurityInspection:
    """计数按审计行；样本限额不改变计数，分类计数不能相加为独立请求数。"""

    scanned_rows: int
    failed_rows: int
    matched_rows: int
    signal_counts: dict[str, int]
    operation_counts: dict[str, int]
    rows: tuple[dict[str, object], ...]
    scan_limited: bool
    checked_candidate_rows: int
    scanned_through: Scalar
    window_oversize_rows: int
    unclassified_candidate_rows: int
    omitted_samples: int

    @property
    def match_counts_complete(self) -> bool:
        return not self.scan_limited and self.unclassified_candidate_rows == 0

    @property
    def truncated(self) -> bool:
        return self.scan_limited or self.unclassified_candidate_rows > 0 or self.omitted_samples > 0

    def as_payload(self) -> dict[str, object]:
        """只输出固定元数据和特征；源 SQL、分词器注释和异常均不在此投影中。"""
        return {
            "rules_version": "manual-security-v1",
            "scanned_rows": self.scanned_rows,
            "failed_rows": self.failed_rows,
            "matched_rows": self.matched_rows,
            "signal_counts": self.signal_counts,
            "operation_counts": self.operation_counts,
            "columns": list(SECURITY_COLUMNS),
            "rows": [
                {**row, **{key: ", ".join(cast(list[str], row[key]))
                           for key in ("signals", "operations")}}
                for row in self.rows
            ],
            "row_count": len(self.rows),
            "coverage": {
                "scan_limited": self.scan_limited,
                "checked_candidate_rows": self.checked_candidate_rows,
                "scanned_through": self.scanned_through,
                "window_oversize_rows": self.window_oversize_rows,
                "unclassified_candidate_rows": self.unclassified_candidate_rows,
                "match_counts_complete": self.match_counts_complete,
                "omitted_samples": self.omitted_samples,
            },
        }


def analyze_security_rows(
    result: SecurityAuditResult, *, sample_limit: int, deadline: float | None = None
) -> SecurityInspection:
    """分析固定源列的有界结果；调用者负责治理与读取，线程内按同一期限逐行停止。"""
    counts = dict.fromkeys(SIGNALS, 0)
    operations = dict.fromkeys(OPERATIONS, 0)
    matched: list[dict[str, object]] = []
    uninspected = 0
    for row in result.rows:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError
        stmt = row["stmt"]
        if not isinstance(stmt, str) or not stmt:
            uninspected += bool(row["stmt_present"])
            continue
        signals, found, delay, incomplete = _indicators(stmt)
        uninspected += incomplete
        for signal in signals:
            counts[signal] += 1
        for operation in found:
            operations[operation] += 1
        if signals:
            matched.append({
                **{name: row[source] for name, source in _METADATA.items()},
                "signals": [s for s in SIGNALS if s in signals],
                "operations": [o for o in OPERATIONS if o in found],
                "requested_delay_seconds": delay,
            })

    # 每种管理操作优先有代表，再覆盖其余信号；同一审计行可同时代表多个类别。
    representatives: dict[tuple[str, str], int] = {}
    for i, sample in enumerate(matched):
        for kind in ("operations", "signals"):
            for value in cast(list[str], sample[kind]):
                representatives.setdefault((kind, value), i)
    # 保留代表优先的顺序，后续输出字节裁剪才不会被前置的大量同类样本挤占。
    chosen: dict[int, None] = {}
    for kind, values in (("operations", OPERATIONS), ("signals", SIGNALS)):
        for value in values:
            index = representatives.get((kind, value))
            if index is not None and len(chosen) < sample_limit:
                chosen.setdefault(index, None)
    for i in range(len(matched)):
        if len(chosen) >= sample_limit:
            break
        chosen.setdefault(i, None)
    return SecurityInspection(
        scanned_rows=result.window_rows,
        failed_rows=result.window_failed_rows,
        matched_rows=len(matched), signal_counts=counts, operation_counts=operations,
        rows=tuple(matched[i] for i in chosen), scan_limited=result.truncated,
        scanned_through=result.rows[-1]["timestamp"] if result.rows else None,
        checked_candidate_rows=result.row_count, window_oversize_rows=result.window_oversize_rows,
        unclassified_candidate_rows=uninspected, omitted_samples=len(matched) - len(chosen),
    )


def _indicators(sql: str) -> tuple[set[str], set[str], float | None, bool]:
    try:
        # Tokenizer 有可变状态：每条记录独享实例，不跨线程共享。
        tokens = StarRocks.Tokenizer().tokenize(sql)
    except TokenError:
        return set(), set(), None, True
    statements: list[list[Token]] = [[]]
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            statements.append([])
        else:
            statements[-1].append(token)
    statements = [s for s in statements if s]
    signals = {"stacked_statements"} if len(statements) > 1 else set()
    operations: set[str] = set()
    delays: list[float | None] = []
    incomplete = False
    for statement in statements:
        if _boolean_probe(statement):
            signals.add("boolean_probe")
        delays.extend(_delays(statement))
        found, unknown = _operations(statement, sql)
        operations.update(found)
        incomplete |= unknown
    if delays:
        signals.add("delay_call")
    if operations:
        signals.add("high_risk_operation")
    return signals, operations, delays[0] if len(delays) == 1 else None, incomplete


def _boolean_probe(tokens: Sequence[Token]) -> bool:
    for i, token in enumerate(tokens):
        if token.token_type != TokenType.OR:
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].token_type == TokenType.L_PAREN:
            j += 1
        if j + 2 < len(tokens):
            left, equal, right = tokens[j:j + 3]
            if (left.token_type in {TokenType.NUMBER, TokenType.STRING}
                    and equal.token_type == TokenType.EQ
                    and right.token_type == left.token_type and right.text == left.text):
                return True
    return False


def _delays(tokens: Sequence[Token]) -> list[float | None]:
    calls: list[float | None] = []
    for i, token in enumerate(tokens[:-1]):
        if (token.token_type != TokenType.VAR or token.text.upper() != "SLEEP"
                or tokens[i + 1].token_type != TokenType.L_PAREN):
            continue
        value = None
        if (i + 3 < len(tokens) and tokens[i + 2].token_type == TokenType.NUMBER
                and tokens[i + 3].token_type == TokenType.R_PAREN):
            try:
                number = float(tokens[i + 2].text)
                if math.isfinite(number):
                    value = number
            except ValueError:
                pass
        calls.append(value)
    return calls


def _operations(tokens: Sequence[Token], sql: str) -> tuple[set[str], bool]:
    tokens = [token for token in tokens if token.token_type != TokenType.HINT]
    if not tokens:
        return set(), False
    first = tokens[0]
    if first.token_type in {TokenType.STRING, TokenType.IDENTIFIER}:
        return set(), False
    head = first.text.upper()
    second = tokens[1].text.upper() if len(tokens) > 1 else ""
    if head in {"GRANT", "REVOKE", "EXPORT"}:
        return {head.lower()}, False
    if head in {"CREATE", "ALTER", "DROP"} and second in {"USER", "ROLE"}:
        return {"account"}, False
    if head == "SET" and second == "PASSWORD":
        return {"password"}, False
    if head == "DROP" and second in {"TABLE", "DATABASE"}:
        return {"drop"}, False
    if head == "TRUNCATE" and second == "TABLE":
        return {"truncate"}, False
    if head == "INSERT" and second == "OVERWRITE":
        return {"insert_overwrite"}, False
    if head == "DELETE" or (head == "WITH" and any(
        t.token_type == TokenType.DELETE for t in tokens
    )):
        try:
            root = parse_one(sql[first.start:tokens[-1].end + 1], read="starrocks")
        except (ParseError, TokenError, RecursionError):
            return set(), True
        if isinstance(root, exp.Command):
            return set(), True
        if isinstance(root, exp.Delete) and root.args.get("where") is None:
            return {"delete_no_where"}, False
    return set(), False
