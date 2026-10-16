"""TraceSink 的显式投递意图、落库结局与结构化日志契约。"""

import json
import logging
from io import StringIO

import pytest
from tests.fakes.sinks import make_event

from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import PipelineStage
from xiaowei_agent.log import configure_logging
from xiaowei_agent.observability.durable_sink import DurableTraceSink
from xiaowei_agent.observability.log_sink import (
    StructuredLogTraceSink,
    worker_log_context,
)
from xiaowei_agent.observability.sink import Delivery, delivery_for_write_exception
from xiaowei_agent.persistence import (
    PersistenceIntegrityCategory,
    PersistenceIntegrityError,
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
    PersistenceWriteOutcome,
)


class _Writer:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.events: list[object] = []

    async def record_audit_event(self, *, event: object) -> int:
        self.events.append(event)
        if self.failure is not None:
            raise self.failure
        return len(self.events)


@pytest.mark.parametrize(
    "delivery",
    [
        Delivery.LOG_AND_DURABLE,
        Delivery.COMMAND_COMMITTED,
        Delivery.COMMAND_ROLLED_BACK,
        Delivery.COMMAND_NOT_CONFIRMED,
    ],
)
async def test_task_scoped_delivery_rejects_an_unscoped_event(
    delivery: Delivery,
) -> None:
    writer = _Writer()
    sink = DurableTraceSink(writer=writer)
    with pytest.raises(ValueError, match="task-scoped event"):
        await sink.emit(
            make_event(stage=PipelineStage.GATEWAY, task_id=None),
            delivery=delivery,
        )
    assert writer.events == []


@pytest.mark.parametrize(
    ("delivery", "state"),
    [
        (Delivery.LOG_ONLY, "not_applicable"),
        (Delivery.COMMAND_COMMITTED, "committed"),
        (Delivery.COMMAND_ROLLED_BACK, "failed"),
        (Delivery.COMMAND_NOT_CONFIRMED, "not_confirmed"),
    ],
)
async def test_terminal_delivery_writes_exactly_one_truthful_log(
    delivery: Delivery,
    state: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = StructuredLogTraceSink()
    with caplog.at_level(logging.INFO):
        await sink.emit(
            make_event(
                stage=PipelineStage.GATEWAY,
                task_id=None if delivery is Delivery.LOG_ONLY else "task-1",
            ),
            delivery=delivery,
        )
    assert [record.delivery_state for record in caplog.records] == [state]


async def test_durable_success_writes_once_then_logs_committed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer = _Writer()
    sink = DurableTraceSink(writer=writer)
    event = make_event(stage=PipelineStage.GATEWAY)
    with caplog.at_level(logging.INFO):
        await sink.emit(event, delivery=Delivery.LOG_AND_DURABLE)
    assert writer.events == [event]
    assert [record.delivery_state for record in caplog.records] == ["committed"]


@pytest.mark.parametrize(
    ("write_outcome", "state"),
    [
        (PersistenceWriteOutcome.ROLLED_BACK, "failed"),
        (PersistenceWriteOutcome.NOT_CONFIRMED, "not_confirmed"),
    ],
)
async def test_durable_failure_is_not_retried_or_misreported(
    write_outcome: PersistenceWriteOutcome,
    state: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure = PersistenceUnavailableError(
        category=PersistenceUnavailableCategory.TRANSIENT,
        write_outcome=write_outcome,
    )
    writer = _Writer(failure)
    sink = DurableTraceSink(writer=writer)
    event = make_event(stage=PipelineStage.GATEWAY)
    with caplog.at_level(logging.INFO), pytest.raises(PersistenceUnavailableError):
        await sink.emit(event, delivery=Delivery.LOG_AND_DURABLE)
    assert writer.events == [event]
    assert [record.delivery_state for record in caplog.records] == [state]


async def test_confirmed_integrity_rollback_is_logged_as_failed() -> None:
    failure = PersistenceIntegrityError(
        category=PersistenceIntegrityCategory.SCHEMA,
        write_outcome=PersistenceWriteOutcome.ROLLED_BACK,
    )
    assert delivery_for_write_exception(failure) is Delivery.COMMAND_ROLLED_BACK


async def test_unexpected_write_failure_is_fail_closed_and_not_confirmed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer = _Writer(RuntimeError("constant"))
    sink = DurableTraceSink(writer=writer)
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="constant"):
        await sink.emit(
            make_event(stage=PipelineStage.GATEWAY),
            delivery=Delivery.LOG_AND_DURABLE,
        )
    assert len(writer.events) == 1
    assert [record.delivery_state for record in caplog.records] == ["not_confirmed"]


async def test_structured_logging_failure_never_becomes_an_audit_failure() -> None:
    class _ExplodingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("handler failed")

    logger = logging.getLogger("xiaowei-test-exploding-handler")
    logger.handlers = [_ExplodingHandler()]
    logger.propagate = False
    try:
        await StructuredLogTraceSink(logger).emit(
            make_event(stage=PipelineStage.GATEWAY),
            delivery=Delivery.COMMAND_COMMITTED,
        )
    finally:
        logger.handlers.clear()
        logger.propagate = True


async def test_structured_log_extra_keys_are_an_exact_closed_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = StructuredLogTraceSink(worker_instance="worker-1")
    with caplog.at_level(logging.INFO):
        await sink.emit(
            make_event(stage=PipelineStage.GATEWAY),
            delivery=Delivery.COMMAND_COMMITTED,
        )
    base = logging.LogRecord("", 0, "", 0, "", (), None).__dict__
    extra = set(caplog.records[0].__dict__) - set(base) - {"message"}
    assert extra == {
        "trace_id",
        "task_id",
        "event_id",
        "stage",
        "outcome",
        "step_id",
        "capability_id",
        "policy_revision",
        "attempt_number",
        "delivery_state",
        "error",
        "detail",
        "worker_instance",
    }
    assert caplog.records[0].worker_instance == "worker-1"


async def test_non_worker_log_still_has_a_null_worker_instance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        await StructuredLogTraceSink().emit(
            make_event(stage=PipelineStage.GATEWAY),
            delivery=Delivery.COMMAND_COMMITTED,
        )
    assert caplog.records[0].worker_instance is None


async def test_worker_event_trace_survives_the_real_logging_filter() -> None:
    stream = StringIO()
    configure_logging(Settings(environment_id="dev"), stream=stream)
    event = make_event(stage=PipelineStage.LIFECYCLE)

    await StructuredLogTraceSink(worker_instance="worker-1").emit(
        event,
        delivery=Delivery.COMMAND_COMMITTED,
    )

    payload = json.loads(stream.getvalue())
    assert payload["trace_id"] == event.trace_id


@pytest.mark.asyncio
async def test_worker_log_context_is_task_local_and_resets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="xiaowei_agent.trace")
    sink = StructuredLogTraceSink()
    event = make_event(task_id="task-1", stage=PipelineStage.LIFECYCLE)

    with worker_log_context("worker-context-1"):
        await sink.emit(event, delivery=Delivery.COMMAND_COMMITTED)
    await sink.emit(
        event.model_copy(update={"event_id": "event-after-worker"}),
        delivery=Delivery.COMMAND_COMMITTED,
    )

    assert [record.worker_instance for record in caplog.records] == [
        "worker-context-1",
        None,
    ]


def test_every_production_emit_is_awaited_and_not_fire_and_forget() -> None:
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "emit"
            ):
                continue
            if not isinstance(parents.get(node), ast.Await):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
            if any(
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Attribute)
                and parent.func.attr == "create_task"
                for parent in _ancestors(node, parents)
            ):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}:create_task")
    assert offenders == []


def _ancestors(node: object, parents: dict[object, object]) -> list[object]:
    found: list[object] = []
    while node in parents:
        node = parents[node]
        found.append(node)
    return found


def test_delivery_is_a_five_member_closed_set() -> None:
    assert {item.value for item in Delivery} == {
        "log_only",
        "log_and_durable",
        "command_committed",
        "command_rolled_back",
        "command_not_confirmed",
    }


def test_writer_protocol_has_only_the_audit_append_shape() -> None:
    from xiaowei_agent.observability.sink import AuditEventWriter

    public = {name for name in vars(AuditEventWriter) if not name.startswith("_")}
    assert public == {"record_audit_event"}
