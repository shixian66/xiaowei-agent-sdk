"""StarRocks 结构快照：自动发现只读账号实际可 SELECT 的对象与列，有界、定时刷新、到期关闭。

一次刷新（``SchemaCache.refresh``）在 ``refresh_timeout_seconds`` 内完成全部步骤：经 Adapter 的代码
模板读取全部用户库的对象、列与表 ID → 去掉系统库、审计源表与无法安全引用的名字 → 对其余每个
对象做零行 SELECT 探测，只保留数据库确认可读的对象。任一步失败、超时或超过容量上限，都不发布
新快照（不发布半份结构），已有快照继续可用到它自己的期限；期限从刷新开始时计，不因后台重试
延长。没有快照或快照已到期时，``current`` 拒绝，该目标的数据工具在任何 I/O 前不可用。

快照记录采集时间、到期时间、对象类型、注释、``(TABLE_ID, CREATE_TIME)`` 版本标识与列（名字、类型、
是否可空、注释），以及据此给出的 SQLGuard 范围（默认库中的可读对象与它们的全部列；4.1.4 不支持
列级授权）。快照中的“可读”只是采集时的结论：交付表结构前仍须按当前权限重新探测，新查询由数据库
自己的权限检查把关。

审计源表即使可读也不进入快照：审计原文只经 ``list_slow_queries`` 按语句检查后交付，不能被业务
查询直接读取。

每个目标一份缓存，各自刷新、互不影响；同一目标同时只有一次刷新在进行（single-flight），刷新任务
不随某个等待者的取消而中止，``aclose`` 时取消。缓存只在应用进程内存中，不含凭据，不进入
RunContext。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from xiaowei.sqlguard import QueryPolicy
from xiaowei.starrocks import (
    Scalar,
    SchemaRows,
    StarRocksAdapter,
    StarRocksError,
    safe_identifier,
)

logger = logging.getLogger(__name__)

SCHEMA_UNAVAILABLE: Final = "该集群的表结构暂不可用（尚未采集成功或已过期），未执行；请稍后重试"


class SchemaUnavailableError(Exception):
    """目标当前没有可用的结构快照；消息是固定说明。"""

    def __init__(self) -> None:
        super().__init__(SCHEMA_UNAVAILABLE)


class _SchemaContractError(Exception):
    pass


class _LimitExceededError(Exception):
    pass


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    nullable: str
    comment: str | None


@dataclass(frozen=True)
class ObjectInfo:
    """一个可读对象；``table_id`` 与 ``created_at`` 是对象版本标识（视图的 ``table_id`` 为 0）。"""

    database: str
    name: str
    type: str
    comment: str | None
    created_at: str | None
    table_id: int | None
    columns: tuple[ColumnInfo, ...]

    @property
    def column_names(self) -> frozenset[str]:
        return frozenset(c.name for c in self.columns)


@dataclass(frozen=True, eq=False)
class SchemaSnapshot:
    """一次成功刷新的完整结构；构造后不再修改，刷新时整体替换。"""

    target_id: str
    collected_at: datetime
    expires_at: datetime
    objects: Mapping[tuple[str, str], ObjectInfo]
    query_policy: QueryPolicy

    def object(self, database: str, name: str) -> ObjectInfo | None:
        return self.objects.get((database, name))


class SchemaCache:
    """一个目标的结构快照；``clock`` 给出带时区的当前时间（到期判断与采集时间）。"""

    def __init__(self, adapter: StarRocksAdapter, *, clock: Callable[[], datetime]) -> None:
        self._adapter = adapter
        self._clock = clock
        target = adapter.target
        self._limits = target.schema_limits
        audit = target.audit
        self._excluded = frozenset() if audit is None else {(audit.database, audit.table)}
        self._snapshot: SchemaSnapshot | None = None
        self._inflight: asyncio.Task[bool] | None = None

    @property
    def target_id(self) -> str:
        return self._adapter.target.target_id

    @property
    def adapter(self) -> StarRocksAdapter:
        return self._adapter

    def current(self) -> SchemaSnapshot:
        """未到期的完整快照；没有或已到期时抛出 ``SchemaUnavailableError``。"""
        snapshot = self._snapshot
        if snapshot is None or self._clock() >= snapshot.expires_at:
            raise SchemaUnavailableError
        return snapshot

    async def refresh(self) -> bool:
        """刷新一次并返回是否成功；并发调用共享同一次刷新。失败只记录原因代码，不抛出。"""
        task = self._inflight
        if task is None or task.done():
            task = asyncio.create_task(self._refresh(), name=f"xiaowei-schema-{self.target_id}")
            self._inflight = task
        return await asyncio.shield(task)

    async def run(self) -> None:
        """后台刷新循环：每 ``refresh_seconds`` 刷新一次，直到被取消。"""
        while True:
            await asyncio.sleep(self._limits.refresh_seconds)
            await self.refresh()

    async def aclose(self) -> None:
        """取消进行中的刷新（连接由 Adapter 断开），等待它结束。"""
        task = self._inflight
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})

    async def _refresh(self) -> bool:
        started = self._clock()
        reason = ""
        try:
            async with asyncio.timeout(self._limits.refresh_timeout_seconds):
                rows = await self._adapter.read_schema()
                if rows.truncated:
                    raise _LimitExceededError
                candidates = self._candidates(rows)
                verdicts = await self._adapter.probe(sorted(candidates))
        except StarRocksError as exc:
            reason = exc.code.value
        except TimeoutError:
            reason = "timeout"
        except _LimitExceededError:
            reason = "limit_exceeded"
        except _SchemaContractError:
            reason = "result_contract"
        except Exception as exc:  # 后台刷新：意外错误同样不发布快照，只记录类型，目标保持关闭
            reason = f"unexpected:{type(exc).__name__}"
        if reason:
            logger.warning("结构快照刷新失败：target=%s reason=%s", self.target_id, reason)
            return False
        readable = {key: obj for key, obj in candidates.items() if verdicts.get(key) is True}
        self._snapshot = SchemaSnapshot(
            target_id=self.target_id,
            collected_at=started,
            expires_at=started + timedelta(seconds=self._limits.max_age_seconds),
            objects=readable,
            query_policy=self._query_policy(readable),
        )
        logger.info(
            "结构快照已刷新：target=%s objects=%d readable=%d",
            self.target_id,
            len(candidates),
            len(readable),
        )
        return True

    def _candidates(self, rows: SchemaRows) -> dict[tuple[str, str], ObjectInfo]:
        """对象、列与 ID 合并为待探测对象；无法安全引用、属于审计源或没有列的对象不进入。"""
        columns: dict[tuple[str, str], list[ColumnInfo]] = {}
        for row in rows.columns:
            key = (_text(row["db"]), _text(row["name"]))
            columns.setdefault(key, []).append(
                ColumnInfo(
                    name=_text(row["col"]),
                    type=_text(row["type"]),
                    nullable=_text(row["nullable"]),
                    comment=_optional_text(row["comment"]),
                )
            )
        ids: dict[tuple[str, str], int] = {}
        for row in rows.ids:
            table_id = row["id"]
            if isinstance(table_id, bool) or not isinstance(table_id, int):
                raise _SchemaContractError
            ids[(_text(row["db"]), _text(row["name"]))] = table_id
        candidates: dict[tuple[str, str], ObjectInfo] = {}
        for row in rows.objects:
            database, name = _text(row["db"]), _text(row["name"])
            key = (database, name)
            if key in self._excluded or not (safe_identifier(database) and safe_identifier(name)):
                continue
            if not columns.get(key):
                continue
            candidates[key] = ObjectInfo(
                database=database,
                name=name,
                type=_text(row["type"]),
                comment=_optional_text(row["comment"]),
                created_at=_optional_text(row["created"]),
                table_id=ids.get(key),
                columns=tuple(columns[key]),
            )
        return candidates

    def _query_policy(self, readable: Mapping[tuple[str, str], ObjectInfo]) -> QueryPolicy:
        target = self._adapter.target
        local = {name: obj for (db, name), obj in readable.items() if db == target.database}
        return QueryPolicy(
            target_id=target.target_id,
            default_database=target.database,
            allowed_objects=frozenset(local),
            allowed_columns={name: obj.column_names for name, obj in local.items()},
            allowed_functions=target.policy.allowed_functions,
            max_rows=target.policy.max_rows,
            max_sql_bytes=target.policy.max_sql_bytes,
        )


def _text(value: Scalar) -> str:
    if not isinstance(value, str) or not value:
        raise _SchemaContractError
    return value


def _optional_text(value: Scalar) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _SchemaContractError
    return value
