"""有限的审计安全线索分析：只消费已读取记录，无 I/O，也不返回被审计 SQL。"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, cast

from sqlglot import Token, TokenType
from sqlglot.dialects.starrocks import StarRocks
from sqlglot.errors import TokenError

from xiaowei.starrocks import Scalar, SecurityAuditResult

SECURITY_TOOL: Final = "local/inspect_audit_security"
SECURITY_FAILURE_REASONS: Final = {
    "object_not_readable": "已刷新复核，当前可读结构仍缺审计源",
    "schema_unavailable": "结构暂不可用或刷新失败，未能复核审计源",
    "unsupported": "审计源缺少安全巡检必需字段",
    "busy": "审计连接槽位繁忙，未发出审计 SQL",
    "budget_exhausted": "本轮工具预算已用完",
    "connect_failed": "审计源连接失败",
    "auth_failed": "审计源身份认证失败",
    "session_setup_failed": "只读采集会话设置失败",
    "permission_denied": "审计源读取权限被拒绝",
    "object_missing": "读取时审计源对象缺失",
    "object_unreadable": "读取时审计源对象不可读",
    "query_failed": "审计读取失败，未取得可交付巡检结果",
    "server_timeout": "服务端因超时终止审计读取",
    "timeout": "读取或分析超时，未取得可交付巡检结果",
    "connection_lost": "连接中断，读取结果未知",
}
RULES_VERSION: Final = "manual-security-v2"
SIGNALS: Final = ("boolean_probe", "delay_call", "stacked_statements")
SECURITY_COLUMNS: Final = (
    "query_id", "timestamp", "state", "query_time_ms", "user", "client_ip", "database",
    "pending_time_ms", "signals", "requested_delay_seconds",
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
            "rules_version": RULES_VERSION,
            "scanned_rows": self.scanned_rows,
            "failed_rows": self.failed_rows,
            "matched_rows": self.matched_rows,
            "signal_counts": self.signal_counts,
            "operation_counts": {},
            "columns": list(SECURITY_COLUMNS),
            "rows": [
                {**row, "signals": ", ".join(cast(list[str], row["signals"]))}
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
    matched: list[dict[str, object]] = []
    uninspected = 0
    for row in result.rows:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError
        stmt = row["stmt"]
        if not isinstance(stmt, str) or not stmt:
            uninspected += bool(row["stmt_present"])
            continue
        signals, delay, incomplete = _indicators(stmt)
        uninspected += incomplete
        for signal in signals:
            counts[signal] += 1
        if signals:
            matched.append({
                **{name: row[source] for name, source in _METADATA.items()},
                "signals": [s for s in SIGNALS if s in signals],
                "requested_delay_seconds": delay,
            })

    # 每类先保留代表；样本名额不足时优先布尔/延迟，再列辅助多语句。
    representatives: dict[str, int] = {}
    for i, sample in enumerate(matched):
        for value in cast(list[str], sample["signals"]):
            representatives.setdefault(value, i)
    chosen: dict[int, None] = {}
    for value in SIGNALS:
        index = representatives.get(value)
        if index is not None and len(chosen) < sample_limit:
            chosen.setdefault(index, None)
    for i in range(len(matched)):
        if len(chosen) >= sample_limit:
            break
        chosen.setdefault(i, None)
    return SecurityInspection(
        scanned_rows=result.window_rows,
        failed_rows=result.window_failed_rows,
        matched_rows=len(matched), signal_counts=counts,
        rows=tuple(matched[i] for i in chosen), scan_limited=result.truncated,
        scanned_through=None,
        checked_candidate_rows=result.row_count, window_oversize_rows=result.window_oversize_rows,
        unclassified_candidate_rows=uninspected, omitted_samples=len(matched) - len(chosen),
    )


def _indicators(sql: str) -> tuple[set[str], float | None, bool]:
    try:
        # Tokenizer 有可变状态：每条记录独享实例，不跨线程共享。
        tokens = StarRocks.Tokenizer().tokenize(sql)
    except TokenError:
        return set(), None, True
    statements: list[list[Token]] = [[]]
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            statements.append([])
        else:
            statements[-1].append(token)
    statements = [s for s in statements if s]
    signals = {"stacked_statements"} if len(statements) > 1 else set()
    delays: list[float | None] = []
    for statement in statements:
        if _boolean_probe(statement):
            signals.add("boolean_probe")
        delays.extend(_delays(statement))
    if delays:
        signals.add("delay_call")
    return signals, delays[0] if len(delays) == 1 else None, False


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
