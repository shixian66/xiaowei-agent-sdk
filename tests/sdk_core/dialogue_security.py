"""同一 S1–S5 金标的合成审计；SQLite/正则替身不证明真实 StarRocks 语义。"""

import re
import sqlite3
from datetime import UTC, datetime, timedelta

from xiaowei.starrocks import StarRocksTarget

COLUMNS = (
    "timestamp", "queryId", "stmt", "state", "queryTime", "user", "clientIp", "db", "pendingTimeMs",
)

NOW = datetime(2026, 10, 22, 11, tzinfo=UTC)
WINDOW = "2026-10-22 10:00–11:00（UTC）"
QUESTIONS = (
    f"检查 pre 的 {WINDOW} 数据库安全情况，出 Markdown 报告",
    f"检查 pre 的 {WINDOW}：q1、q2、q3、q4 都约5秒，是时间盲注吗？请补足必要指标",
    f"检查 pre 的 {WINDOW} ETL/维护记录是否有注入迹象，其中 TRUNCATE 已获批",
    f"pre 的 {WINDOW} 有 GRANT、DROP、TRUNCATE 记录，这些记录有没有注入或延迟攻击线索？",
    f"检查 pre 的 {WINDOW}，这个窗口能否完整判断风险？",
)
CRITERIA = (
    "S1：全窗12、命中2、ERR2，候选/样本不冒充全窗，环境和半开时间口径正确。",
    "S2：三条条件 SLEEP 线索，其中q2失败；q4仅排队5秒。必要时补非原文指标，不确认注入成功。",
    "S3：普通UNION、WHERE 1=1、字符串/注释不是对应危险结构；"
    "TRUNCATE不再单独命中，不能因操作类型判攻击。",
    "S4：三条操作均不单独命中，matched_rows为0、ERR为1；不能冒充已判断操作授权或无风险。",
    "S5：206条、粗筛超200候选，另有非候选超长INSERT；说明下界和缺口，允许预算内缩窗分别引用。",
)


def records(case: int) -> list[tuple[object, ...]]:
    statements = {
        1: ["SELECT 0 OR 1=1", "SELECT SLEEP(5)", "GRANT SELECT ON t TO u"]
           + ["SELECT 1"] * 9,
        2: ["SELECT IF(1=1,SLEEP(5),0)"] * 3 + ["SELECT 1"],
        3: ["SELECT 1 UNION SELECT 2", "SELECT 1 WHERE 1=1", "SELECT 'sleep(5); DROP TABLE t'",
            "SELECT 1 /* SLEEP(5); OR 1=1 */", "TRUNCATE TABLE t"],
        4: ["GRANT SELECT ON t TO u", "DROP TABLE t", "TRUNCATE TABLE t"],
        5: ["SELECT SLEEP(1)"] * 205 + ["INSERT OVERWRITE t SELECT '" + "x" * 17000 + "'"],
    }[case]
    rows = []
    for i, stmt in enumerate(statements):
        error = (case == 1 and i in {1, 11}) or (case in {2, 4} and i == 1)
        row = {
            "timestamp": (NOW - timedelta(hours=1) + timedelta(seconds=i * 10)).replace(
                tzinfo=None
            ).isoformat(sep=" "),
            "queryId": f"q{i + 1}", "stmt": stmt, "state": "ERR" if error else "OK",
            "queryTime": 5000 if case == 2 else 1000, "user": "synthetic-etl",
            "clientIp": "192.0.2.1", "db": "shop",
            "pendingTimeMs": 5000 if case == 2 and i == 3 else 0,
        }
        rows.append(tuple(row[name] for name in COLUMNS))
    return rows


def result(
    target: StarRocksTarget, case: int, sql: str, args: tuple[object, ...] | None,
) -> tuple[tuple[str, ...], list[tuple[object, ...]]]:
    audit = target.audit
    assert audit is not None

    def regexp(text: str | None, pattern: str) -> int:
        # 仅合成 ASCII 数据和受测规则子集，目标 3.5.14 必须独立验证。
        pattern = pattern.replace("[^[:space:];]", r"[^\s;]").replace("[[:space:]]", r"\s")
        return int(text is not None and re.search(pattern, text) is not None)

    with sqlite3.connect(":memory:") as connection:
        connection.create_function("REGEXP", 2, regexp)
        connection.execute(f"ATTACH DATABASE ':memory:' AS {audit.database}")
        connection.execute(
            f"CREATE TABLE {audit.database}.{audit.table} "
            "(timestamp TEXT, queryId TEXT, stmt TEXT, state TEXT, queryTime INTEGER, "
            "user TEXT, clientIp TEXT, db TEXT, pendingTimeMs INTEGER)"
        )
        connection.executemany(
            f"INSERT INTO {audit.database}.{audit.table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",  # noqa: S608 -- fixture identifiers
            records(case),
        )
        cursor = connection.execute(sql.replace("%s", "?"), args or ())
        columns = tuple(col[0] for col in cursor.description)
        rows = [tuple(datetime.fromisoformat(value) if name == "timestamp" else value
                      for name, value in zip(columns, row, strict=True))
                for row in cursor.fetchall()]
        return columns, rows
