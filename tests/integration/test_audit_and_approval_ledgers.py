"""两张 append-only 台账 —— **PostgreSQL 侧直接读表**。

共享套件断言的是写入返回的 ``seq``，两个实现给出同一个答案。那不够：``seq`` 对了不
代表**落盘的东西**对了。这里只做套件做不到的三件事。

1. **提升列与载荷必须一致**。``stage`` / ``outcome`` / ``occurred_at`` 既在 JSONB 载荷
   里，又被提升成独立列——同一事实存了两遍，是必然的漂移风险。提升列写错时，套件
   全绿，按列查询却查出错误的结果集，而载荷看上去完全正常。
2. **真实的 JSONB / timestamptz 往返**。``test_row_mapping.py`` 证明的是映射无损，
   中间没有数据库；键序、数值表示、时区与微秒要到这里才有答案。
3. **并发下的序号唯一**。``MAX(seq) + 1`` 是读-改-写，正确性完全依赖那把 advisory
   lock。锁一旦失效，单连接用例照样全绿，只有两个真实连接同时写才会撞主键。

**这里不读回 Protocol**（M4 §8.4 拍板）：读的是表，不是新增的 store 方法。审计与审批
的消费路径仍归 M8。
"""

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.fakes.sinks import make_event
from tests.suites.task_store import make_approval

from xiaowei_agent.contracts import (
    ApprovalRequest,
    PipelineStage,
    StageOutcome,
    TraceEvent,
)
from xiaowei_agent.persistence import UnscopedAuditEventError
from xiaowei_agent.persistence.rows import load_contract
from xiaowei_agent.persistence.schema import TASK_APPROVALS, TASK_AUDIT_EVENTS

pytestmark = pytest.mark.security

_TASK = "task-ledger-1"


async def _rows(engine: AsyncEngine, table: sa.Table) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(table).where(table.c.task_id == _TASK).order_by(table.c.seq)
        )
        return [dict(row) for row in result.mappings().all()]


# --- 审计事件 -----------------------------------------------------------------


async def test_promoted_columns_agree_with_the_stored_payload(
    store: Any, clean_database: AsyncEngine
) -> None:
    """提升列不是第二份真相，是载荷的投影。两者不一致时按列查询会给出错误答案。"""
    event = make_event(stage=PipelineStage.ADMISSION, outcome=StageOutcome.REJECTED, task_id=_TASK)
    await store.record_audit_event(event=event)

    (row,) = await _rows(clean_database, TASK_AUDIT_EVENTS)
    payload = load_contract(TraceEvent, row["event"])
    assert row["stage"] == payload.stage.value == "admission"
    assert row["outcome"] == payload.outcome.value == "rejected"
    assert row["occurred_at"] == payload.occurred_at


async def test_audit_payload_survives_a_real_round_trip(
    store: Any, clean_database: AsyncEngine
) -> None:
    """整条事件必须原样回来，包括脱敏后的 ``detail`` 与感知时区的时间戳。"""
    event = make_event(
        stage=PipelineStage.GATEWAY,
        outcome=StageOutcome.FAILED,
        task_id=_TASK,
        detail={"reason": "tool refused"},
    )
    await store.record_audit_event(event=event)

    (row,) = await _rows(clean_database, TASK_AUDIT_EVENTS)
    assert load_contract(TraceEvent, row["event"]) == event
    assert row["occurred_at"].tzinfo is not None
    assert row["occurred_at"].utcoffset() == event.occurred_at.utcoffset()


async def test_a_denied_step_leaves_an_audit_row_without_any_evidence(
    store: Any, evidence_ledger: Any, clean_database: AsyncEngine
) -> None:
    """证据表替代不了审计：被拒的调用不产生证据，但必须留下审计。

    这条把 ``schema.py`` 里那句注释变成可执行断言——否则"审计和证据是两件事"只是
    一句可以被将来某次重构悄悄推翻的散文。
    """
    await store.record_audit_event(
        event=make_event(
            stage=PipelineStage.ADMISSION, outcome=StageOutcome.REJECTED, task_id=_TASK
        )
    )
    assert len(await _rows(clean_database, TASK_AUDIT_EVENTS)) == 1
    assert await evidence_ledger.load(task_id=_TASK) == ()


async def test_audit_rows_are_append_only(store: Any, clean_database: AsyncEngine) -> None:
    """同一任务、同一阶段的第二条事件必须新增一行，而不是覆盖第一行。"""
    for index in range(2):
        await store.record_audit_event(
            event=make_event(stage=PipelineStage.ADMISSION, task_id=_TASK, event_id=f"e{index}")
        )
    rows = await _rows(clean_database, TASK_AUDIT_EVENTS)
    assert [row["seq"] for row in rows] == [1, 2]
    assert [load_contract(TraceEvent, row["event"]).event_id for row in rows] == [
        "e0",
        "e1",
    ]


async def test_a_rejected_audit_event_leaves_no_row(
    store: Any, clean_database: AsyncEngine
) -> None:
    """拒绝必须发生在写入之前——**这是那条顺序唯一能被观察到的地方**。

    内存实现上观察不到：没有 ``task_id`` 就没有桶可污染，"先分配再校验"只会往一个
    占位桶里写。到了真表上就看得见了：一行 ``task_id`` 为占位值的垃圾审计，或者一个
    来自驱动层的 NOT NULL 违例（而调用方期待的是 ``UnscopedAuditEventError``）。
    """
    await store.record_audit_event(event=make_event(stage=PipelineStage.ADMISSION, task_id=_TASK))
    with pytest.raises(UnscopedAuditEventError):
        await store.record_audit_event(event=make_event(stage=PipelineStage.INTENT, task_id=None))
    async with clean_database.connect() as connection:
        total = (
            await connection.execute(sa.select(sa.func.count()).select_from(TASK_AUDIT_EVENTS))
        ).scalar_one()
    assert total == 1, "被拒的事件在表里留下了行"
    assert [row["seq"] for row in await _rows(clean_database, TASK_AUDIT_EVENTS)] == [1]


# --- 审批记录 -----------------------------------------------------------------


async def test_the_same_step_can_be_asked_for_approval_twice(
    store: Any, clean_database: AsyncEngine
) -> None:
    """主键是 ``(task_id, seq)`` 而不是 ``(task_id, step_id)``。

    换成后者时第二次请求会覆盖第一次，而审批历史正是事后追责要看的东西。
    """
    for _ in range(2):
        await store.record_approval(request=make_approval(_TASK, step_id="s1"))
    rows = await _rows(clean_database, TASK_APPROVALS)
    assert [row["seq"] for row in rows] == [1, 2]
    assert {row["step_id"] for row in rows} == {"s1"}


async def test_approval_payload_survives_a_real_round_trip(
    store: Any, clean_database: AsyncEngine
) -> None:
    request = make_approval(_TASK)
    await store.record_approval(request=request)
    (row,) = await _rows(clean_database, TASK_APPROVALS)
    assert load_contract(ApprovalRequest, row["request"]) == request
    assert row["state"] == request.state.value


# --- 并发 ---------------------------------------------------------------------


async def test_concurrent_audit_writes_get_distinct_seqs(
    independent_stores: Any, clean_database: AsyncEngine
) -> None:
    """``MAX(seq) + 1`` 是读-改-写，正确性全靠 advisory lock。

    必须用**各自持有连接**的 store：``asyncio.gather`` 共用一个连接只是同一会话里的
    两次顺序调用，锁去掉了也照样通过。
    """
    writers = independent_stores(4)
    seqs = await asyncio.gather(
        *(
            writer.record_audit_event(
                event=make_event(stage=PipelineStage.ADMISSION, task_id=_TASK, event_id=f"e{index}")
            )
            for index, writer in enumerate(writers)
        )
    )
    assert sorted(seqs) == [1, 2, 3, 4]
    assert [row["seq"] for row in await _rows(clean_database, TASK_AUDIT_EVENTS)] == [
        1,
        2,
        3,
        4,
    ]


async def test_concurrent_approval_writes_get_distinct_seqs(
    independent_stores: Any, clean_database: AsyncEngine
) -> None:
    """审批走同一段分配逻辑，因此必须有同一条并发断言，否则只有一半被证明。"""
    writers = independent_stores(4)
    seqs = await asyncio.gather(
        *(writer.record_approval(request=make_approval(_TASK)) for writer in writers)
    )
    assert sorted(seqs) == [1, 2, 3, 4]
