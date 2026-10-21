"""Transient synthetic channel I/O; binding, authorization and receipts use production code."""

import json
from dataclasses import dataclass, field
from functools import partial
from time import monotonic
from typing import Any

from lark_channel.channel.types import SendResult
from tests.sdk_core.test_feishu_render import text_parts

from xiaowei.channel import RequestRef, ResultDelivery
from xiaowei.feishu import send_delivery
from xiaowei.feishu_report import ReportFile


@dataclass
class ChannelDelivery:
    started: float
    messages: list[dict[str, Any]] = field(default_factory=list)
    files: list[ReportFile] = field(default_factory=list)
    state: str | None = None
    confirmed_parts: int = 0
    receipt_times: dict[str, int] = field(default_factory=dict)
    failure_simulated: bool = False
    first_body_ack_ms: int | None = None
    complete_delivery_ms: int | None = None

    @property
    def visible_text(self) -> str:
        return "\n".join(
            part for message in self.messages for part in text_parts(message, folded=False)
        )

    async def upload(self, report: ReportFile) -> str:
        self.files.append(report)
        return f"p4_file_{len(self.files)}"

    async def send(
        self, chat_id: str, value: object, *, reply_to: str | None = None, uuid: str | None = None
    ) -> SendResult:
        if isinstance(value, dict):
            self.messages.append(value)
        if uuid is not None:
            self.receipt_times[uuid] = int((monotonic() - self.started) * 1000)
        return SendResult.ok(message_id=f"om_p4_{uuid or len(self.messages)}")

    def metrics(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "confirmed_parts": self.confirmed_parts,
            "file_count": len(self.files),
            "file_bytes": sum(len(f.content) for f in self.files),
            "visible_body_bytes": len(self.visible_text.encode()),
            "message_bytes": sum(
                len(json.dumps(m, ensure_ascii=False).encode()) for m in self.messages
            ),
            "first_body_ack_ms": self.first_body_ack_ms,
            "complete_delivery_ms": self.complete_delivery_ms,
            "channel_io_simulated": True,
        }


async def capture_delivery(
    results: ResultDelivery, ref: RequestRef, started: float
) -> ChannelDelivery:
    captured = ChannelDelivery(started)
    captured.state = await results.send(
        ref,
        partial(
            send_delivery,
            send=captured.send,
            upload_file=captured.upload,
            chat_id=ref.conversation_id,
            max_chars=3500,
            results=results,
            ref=ref,
        ),
    )
    record = await results.store.get(
        ref.channel,
        ref.subject_id,
        ref.conversation_id,
        ref.request_id,
        owner=ref.group.owner if ref.group else None,
    )
    parts = await results.store.delivery_parts(record)
    captured.confirmed_parts = sum(p.send_state == "sent" for p in parts)
    body_receipts = [
        captured.receipt_times[p.uuid]
        for p in parts
        if p.kind == "body" and p.send_state == "sent" and p.uuid in captured.receipt_times
    ]
    captured.first_body_ack_ms = min(body_receipts, default=None)
    if captured.state == "sent":
        captured.complete_delivery_ms = int((monotonic() - started) * 1000)
    return captured
