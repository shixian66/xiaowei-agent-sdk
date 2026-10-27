"""E6 报告文件：获准事实的单文件交付与纯文本保真边界。"""

import asyncio
import dataclasses
import hashlib
import json
from datetime import UTC, datetime
from functools import partial
from typing import Any

import pytest
from lark_channel.channel import FeishuChannel
from lark_channel.channel.types import OutboundFile
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from tests.sdk_core.mcp_fixture import serve
from tests.sdk_core.test_app import answer, evidence_in, tool_call
from tests.sdk_core.test_channel_service import Env
from tests.sdk_core.test_channel_service import env as env  # pytest fixture
from tests.sdk_core.test_feishu import CHATS, Feishu, Outbox, config
from tests.sdk_core.test_group_actions import SOURCE, Actions, request_execution
from tests.sdk_core.test_group_actions import actions as actions  # pytest fixture
from tests.sdk_core.test_group_gateway import BOT, Group, group_config
from tests.sdk_core.test_group_identity import CHAT, A, B, group_ref

from xiaowei import feishu, feishu_report
from xiaowei.channel import AccessDeniedError, RequestRef
from xiaowei.channel_store import ChannelStore, ChannelStoreUnavailableError, ReportMismatchError
from xiaowei.feishu import FeishuGateway, LarkTransport, attributed, lark_channel, send_delivery
from xiaowei.feishu_report import build_report
from xiaowei.models import Delivery, DeliveryFact, DeliveryFactRef, DeliveryText
from xiaowei.storage import Readiness, hold_instance_lock

pytestmark = pytest.mark.loopback


def _delivery(format_: str) -> Delivery:
    fact = DeliveryFact(
        evidence_id="ev_report",
        tool_id="local/run_readonly_query",
        target_id="fat",
        captured_at=datetime(2026, 10, 12, tzinfo=UTC),
        truncated=False,
        columns=("value",),
        rows=({"value": "<at id=all> [click](https://invalid.example) ```"},),
        metadata={"sql": "SELECT '<script>' AS value"},
        result_json=(
            '{"sql":"SELECT \'<script>\' AS value",'
            '"rows":[{"value":"<at id=all> [click](https://invalid.example) ```"}]}'
        ),
    )
    return Delivery(
        content="工具结果",
        evidence_ids=("ev_report",),
        channel="feishu",
        facts=(fact,),
        blocks=(DeliveryText(text="需要复核"), DeliveryFactRef(evidence_id="ev_report")),
    ).model_copy(update={"report_format": format_})


@pytest.mark.parametrize("format_", ["md", "html"])
def test_report_preserves_evidence_and_has_no_active_content(format_: str) -> None:
    from xiaowei.feishu_report import build_report

    report = build_report(_delivery(format_))
    assert report.filename == f"xiaowei-report.{format_}"
    assert report.digest and len(report.digest) == 64
    body = report.content.decode("utf-8")
    assert "结果 1" in body and "模型说明" in body and "fat" in body
    assert "ev_report" not in body
    assert "SELECT" in body and "需要复核" in body
    if format_ == "html":
        assert "<script>" not in body and "https://invalid.example" in body
        assert "&lt;at id=all&gt;" in body
        assert "<a " not in body and "<img " not in body
    else:
        assert "<at id=all>" in body and "[click](https://invalid.example)" in body
        assert "~~~" in body or "```" in body


def test_report_does_not_interpret_source_labels_as_markdown() -> None:
    delivery = _delivery("md")
    changed = delivery.facts[0].model_copy(
        update={"target_id": "<at id=all> [link](https://x.test)"}
    )
    report = build_report(delivery.model_copy(update={"facts": (changed,)}))
    body = report.content.decode()
    assert "## 证据 ev_report · 来源" not in body
    assert "<at id=all> [link](https://x.test)" in body


@pytest.mark.parametrize("format_", ["md", "html"])
def test_report_displays_invisible_controls_as_literals(format_: str) -> None:
    delivery = _delivery(format_)
    fact = delivery.facts[0].model_copy(
        update={
            "result_json": json.dumps({"rows": [{"value": "方向\u202e反转"}]}, ensure_ascii=False)
        }
    )
    body = build_report(
        delivery.model_copy(
            update={
                "facts": (fact,),
                "blocks": (
                    DeliveryText(text="方向\u202e反转"),
                    DeliveryFactRef(evidence_id="ev_report"),
                ),
            }
        )
    ).content.decode()
    assert "\u202e" not in body
    assert "\\u202e" in body


def test_partial_monitoring_report_keeps_unverified_notice() -> None:
    delivery = _delivery("md").model_copy(update={"monitoring_notice": "CPU 来源未核实"})
    body = build_report(delivery).content.decode()
    assert "结果 1" in body and "ev_report" not in body
    assert "未核实来源" in body and "CPU 来源未核实" in body


def test_report_distinguishes_next_page_from_lost_values() -> None:
    delivery = _delivery("md")
    fact = delivery.facts[0].model_copy(
        update={
            "tool_id": "local/list_tables",
            "truncated": True,
            "metadata": {"next_cursor": "opaque_cursor"},
        }
    )
    paged = build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()
    assert "还有后续页" in paged
    assert "工具结果已截断" not in paged

    lost = fact.model_copy(update={"tool_id": "local/run_readonly_query"})
    clipped = build_report(delivery.model_copy(update={"facts": (lost,)})).content.decode()
    assert "还有后续页" in clipped and "工具结果已截断" in clipped


def test_explicit_brief_report_expands_authorized_original() -> None:
    delivery = _delivery("md")
    brief = delivery.facts[0].model_copy(update={"view": "brief", "summary": "只显示行数摘要"})
    body = build_report(delivery.model_copy(update={"facts": (brief,)})).content.decode()
    assert "只显示行数摘要" not in body and "获准原文未展示" not in body
    assert "SELECT '<script>'" in body


def test_file_limit_is_serialized_bytes_without_an_extra_line_limit() -> None:
    delivery = _delivery("md")
    fact = delivery.facts[0].model_copy(update={"result_json": "\n" * 10_001})
    report = build_report(delivery.model_copy(update={"facts": (fact,)}))
    assert len(report.content) < 400_000
    assert report.content.count(b"\n") > 10_000


@pytest.fixture(autouse=True)
def legacy_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """本模块回归升级前 E6 记录；新分项契约在 test_delivery_parts/正式入口独立验收。"""
    accept = ChannelStore.accept

    async def old_accept(self, **kwargs):
        result = await accept(self, **kwargs)
        async with self._engine.begin() as conn:
            await conn.execute(
                text("UPDATE xiaowei_request SET delivery_version=0 WHERE turn_id=:turn"),
                {"turn": result.record.turn_id},
            )
        return dataclasses.replace(
            result, record=dataclasses.replace(result.record, delivery_version=0)
        )

    monkeypatch.setattr(ChannelStore, "accept", old_accept)


def test_report_format_is_not_exposed_in_web_serialization() -> None:
    view = (
        _delivery("md")
        .model_copy(update={"request_state": "paused", "report_notice": "本轮尚未完成"})
        .model_dump(exclude={"channel"})
    )
    assert "report_format" not in view
    assert "request_state" not in view
    # S3：载体说明是代码生成的系统说明，Web 也须展示（如非飞书渠道的文件请求）。
    assert view["report_notice"] == "本轮尚未完成"


@pytest.mark.asyncio
async def test_agent_report_choice_reaches_verified_feishu_delivery(env: Env) -> None:
    message = env.scripts.add(
        "请查东区订单并生成 MD 巡检报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    record = await env.run(message, "om_report", channel="feishu", conversation="oc_report")
    assert record.state == "completed", record.failure_code
    view = await env.service.results.view(
        RequestRef(
            channel="feishu",
            subject_id="alice",
            conversation_id="oc_report",
            request_id="om_report",
        ),
        first=True,
    )
    assert view.delivery is not None and view.delivery.report_format == "md"
    assert view.delivery.facts and view.delivery.facts[0].evidence_id in view.delivery.evidence_ids


@pytest.mark.asyncio
async def test_raw_feishu_event_runs_agent_and_sends_single_report_file(env: Env) -> None:
    message = env.scripts.add(
        "请查东区订单并生成 MD 巡检报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    uploaded: list[bytes] = []

    async def upload(report: object) -> str:
        uploaded.append(report.content)  # type: ignore[attr-defined]
        return "file_e6_gateway"

    outbox = Outbox()
    gateway = FeishuGateway(env.service, config(), outbox, clock=env.clock, upload_file=upload)
    fs = Feishu(env, gateway, outbox)
    task = asyncio.create_task(gateway.run())
    try:
        await gateway.receive(fs.event(message, message_id="om_e6_gateway"))
        await gateway.idle()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(uploaded) == len(outbox.sent) == 1
    assert isinstance(outbox.sent[0][1], OutboundFile)
    assert outbox.sent[0][1].source.key == "file_e6_gateway"
    assert outbox.replies == [None]


@pytest.mark.asyncio
async def test_report_bytes_and_upload_key_bind_to_original_request(env: Env) -> None:
    message = env.scripts.add(
        "生成东区订单 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    record = await env.run(message, "om_bound", channel="feishu", conversation="oc_bound")
    ref = RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_bound", request_id="om_bound"
    )
    delivery = (await env.service.results.view(ref, first=True)).delivery
    assert delivery is not None
    report = build_report(delivery)
    claim = await env.store.claim_send(record)
    assert claim is not None
    state, key, attempt = await env.store.bind_report(record, report)
    assert (state, key) == ("pending", None) and attempt
    assert await env.store.begin_report_upload(record, attempt)
    await env.store.complete_report_upload(record, attempt, "file_key_1")
    assert await env.store.bind_report(record, report) == ("uploaded", "file_key_1", attempt)
    changed = report.__class__(
        format=report.format,
        filename=report.filename,
        content=report.content + b"changed",
        digest=report.digest,
    )
    with pytest.raises(ReportMismatchError):
        await env.store.bind_report(record, changed)
    changed_content = report.content + b"different"
    recomputed = dataclasses.replace(
        report,
        content=changed_content,
        digest=hashlib.sha256(changed_content).hexdigest(),
    )
    with pytest.raises(ReportMismatchError):
        await env.store.bind_report(record, recomputed)


@pytest.mark.asyncio
async def test_report_file_is_deleted_with_original_request(env: Env) -> None:
    message = env.scripts.add(
        "生成东区订单 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    record = await env.run(message, "om_report_retention", channel="feishu")
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="cookie-1",
        request_id="om_report_retention",
    )
    delivery = (await env.service.results.view(ref, first=True)).delivery
    assert delivery is not None
    assert await env.store.claim_send(record) is not None
    await env.store.bind_report(record, build_report(delivery))
    async with env.engine.begin() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM xiaowei_report")) == 1
        await conn.execute(
            text("DELETE FROM xiaowei_request WHERE turn_id = :turn_id"),
            {"turn_id": record.turn_id},
        )
        assert await conn.scalar(text("SELECT count(*) FROM xiaowei_report")) == 0


@pytest.mark.asyncio
async def test_report_uses_one_upload_and_one_file_message_on_delivery(env: Env) -> None:
    message = env.scripts.add(
        "生成东区订单 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    await env.run(message, "om_send_report", channel="feishu", conversation="oc_report_send")
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="oc_report_send",
        request_id="om_send_report",
    )
    uploaded: list[bytes] = []
    sent: list[OutboundFile] = []

    async def upload(report: object) -> str:
        uploaded.append(report.content)  # type: ignore[attr-defined]
        return "file_key_1"

    async def send(chat_id: str, value: object, *, reply_to: str | None = None) -> str:
        assert chat_id == "oc_report_send" and reply_to is None
        assert isinstance(value, OutboundFile)
        sent.append(value)
        return "sent"

    transmit = partial(
        send_delivery,
        send=send,
        chat_id="oc_report_send",
        max_chars=2000,
        results=env.service.results,
        ref=ref,
        upload_file=upload,
    )
    assert await env.service.results.send(ref, transmit) == "sent"
    assert len(uploaded) == len(sent) == 1
    assert sent[0].source is not None and sent[0].source.key == "file_key_1"
    assert sent[0].file_name == "xiaowei-report.md"
    assert await env.service.results.send(ref, transmit, resend=True) is None


@pytest.mark.asyncio
async def test_unknown_report_upload_cannot_be_retried_or_sent(
    env: Env, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = env.scripts.add(
        "生成东区订单 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    await env.run(message, "om_upload_unknown", channel="feishu", conversation="oc_upload_unknown")
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="oc_upload_unknown",
        request_id="om_upload_unknown",
    )
    calls: list[str] = []

    async def upload(report: object) -> str:
        calls.append("upload")
        raise TimeoutError("sentinel upstream message")

    async def send(chat_id: str, value: object, *, reply_to: str | None = None) -> str:
        calls.append("send")
        return "sent"

    transmit = partial(
        send_delivery,
        send=send,
        chat_id="oc_upload_unknown",
        max_chars=2000,
        results=env.service.results,
        ref=ref,
        upload_file=upload,
    )
    assert await env.service.results.send(ref, transmit) == "unknown"
    # 即使新版容量规则变小，旧请求已有结果不明的上传记录，也不能改走卡片发送。
    monkeypatch.setattr(feishu_report, "MAX_REPORT_BYTES", 100)
    assert (
        await env.service.results.send(ref, partial(transmit, resend=True), resend=True)
        == "unknown"
    )
    assert calls == ["upload"]
    assert "sentinel upstream message" not in caplog.text
    assert "reason=report_upload_unknown" in caplog.text


@pytest.mark.asyncio
async def test_cancelled_upload_remains_unknown_and_cannot_resend(env: Env) -> None:
    message = env.scripts.add(
        "生成东区订单 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    await env.run(message, "om_cancel_upload", channel="feishu", conversation="oc_cancel_upload")
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="oc_cancel_upload",
        request_id="om_cancel_upload",
    )
    entered = asyncio.Event()
    uploads = 0
    sends = 0

    async def upload(report: object) -> str:
        nonlocal uploads
        uploads += 1
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    async def send(chat_id: str, value: object, *, reply_to: str | None = None) -> str:
        nonlocal sends
        sends += 1
        return "sent"

    transmit = partial(
        send_delivery,
        send=send,
        chat_id="oc_cancel_upload",
        max_chars=2000,
        results=env.service.results,
        ref=ref,
        upload_file=upload,
    )
    task = asyncio.create_task(env.service.results.send(ref, transmit))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (
        await env.service.results.send(ref, partial(transmit, resend=True), resend=True)
        == "unknown"
    )
    assert uploads == 1 and sends == 0


@pytest.mark.asyncio
async def test_recovery_during_upload_prevents_stale_file_message(env: Env) -> None:
    message = env.scripts.add(
        "生成东区订单 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    await env.run(message, "om_report_recover", channel="feishu", conversation="oc_report_recover")
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="oc_report_recover",
        request_id="om_report_recover",
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    uploads = 0
    sends = 0

    async def upload(report: object) -> str:
        nonlocal uploads
        uploads += 1
        entered.set()
        await release.wait()
        return "file_stale_attempt"

    async def send(chat_id: str, value: object, *, reply_to: str | None = None) -> str:
        nonlocal sends
        sends += 1
        return "sent"

    transmit = partial(
        send_delivery,
        send=send,
        chat_id="oc_report_recover",
        max_chars=2000,
        results=env.service.results,
        ref=ref,
        upload_file=upload,
    )
    old = asyncio.create_task(env.service.results.send(ref, transmit))
    await asyncio.wait_for(entered.wait(), 5)
    restarted = env.channel_store(readiness=Readiness())
    async with hold_instance_lock(env.engine, restarted.readiness) as lock:
        assert (await restarted.recover(lock)).unknown == 1
        release.set()
        with pytest.raises(ChannelStoreUnavailableError):
            await old
        newer = env.results(restarted)
        resent = partial(
            send_delivery,
            send=send,
            chat_id="oc_report_recover",
            max_chars=2000,
            results=newer,
            ref=ref,
            upload_file=upload,
            resend=True,
        )
        assert await newer.send(ref, resent, resend=True) == "unknown"
    assert uploads == 1 and sends == 0


@pytest.mark.asyncio
async def test_web_report_request_is_delivered_with_a_code_notice(env: Env) -> None:
    """S3：非飞书渠道要文件不再拒绝或改写；正文照常交付，由代码说明当前渠道不支持文件。"""
    message = env.scripts.add(
        "生成东区订单 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    record = await env.run(message, "web_report", channel="web", conversation="web_report")
    assert record.state == "completed"
    assert len(env.scripts.calls[message]) == 2


@pytest.mark.asyncio
async def test_rejected_evidence_never_uploads_report(env: Env) -> None:
    message = env.scripts.add(
        "生成东区订单 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(["ev_not_issued"], report_format="md"),
        lambda call: answer(["ev_not_issued"], report_format="md"),  # 改写一次仍伪造
    )
    record = await env.run(
        message, "om_bad_evidence_report", channel="feishu", conversation="oc_bad_evidence"
    )
    assert record.state == "failed" and record.failure_code == "evidence_failed"
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="oc_bad_evidence",
        request_id="om_bad_evidence_report",
    )
    uploads = 0
    sends = 0

    async def upload(report: object) -> str:
        nonlocal uploads
        uploads += 1
        return "unreachable"

    async def send(chat_id: str, value: object, *, reply_to: str | None = None) -> str:
        nonlocal sends
        sends += 1
        assert not isinstance(value, OutboundFile)
        return "sent"

    transmit = partial(
        send_delivery,
        send=send,
        chat_id="oc_bad_evidence",
        max_chars=2000,
        results=env.service.results,
        ref=ref,
        upload_file=upload,
    )
    assert await env.service.results.send(ref, transmit) == "sent"
    assert uploads == 0 and sends == 1


@pytest.mark.asyncio
async def test_legacy_report_uses_starting_permission_snapshot(env: Env) -> None:
    message = env.scripts.add(
        "生成东区订单 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    await env.run(message, "om_revoked_report", channel="feishu", conversation="oc_report_revoke")
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="oc_report_revoke",
        request_id="om_revoked_report",
    )
    calls: list[str] = []

    async def upload(report: object) -> str:
        calls.append("upload")
        env.grants.revoke_all()
        return "file_key_1"

    async def send(chat_id: str, value: object, *, reply_to: str | None = None) -> str:
        calls.append("send")
        return "sent"

    transmit = partial(
        send_delivery,
        send=send,
        chat_id="oc_report_revoke",
        max_chars=2000,
        results=env.service.results,
        ref=ref,
        upload_file=upload,
    )
    assert await env.service.results.send(ref, transmit) == "sent"
    assert calls == ["upload", "send"]


@pytest.mark.asyncio
async def test_report_capacity_falls_back_to_card_and_resend_without_upload(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = env.scripts.add(
        "生成东区订单 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    await env.run(message, "om_report_capacity", channel="feishu", conversation="oc_capacity")
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="oc_capacity",
        request_id="om_report_capacity",
    )
    uploads: list[str] = []
    outbox = Outbox(outcomes=["failed", "sent"])

    async def upload(report: object) -> str:
        uploads.append("upload")
        return "file_key_1"

    monkeypatch.setattr(feishu_report, "MAX_REPORT_BYTES", 100)
    transmit = partial(
        send_delivery,
        send=outbox,
        chat_id="oc_capacity",
        max_chars=2000,
        results=env.service.results,
        ref=ref,
        upload_file=upload,
    )
    assert await env.service.results.send(ref, transmit) == "failed"
    resent = await env.service.results.send(ref, partial(transmit, resend=True), resend=True)
    assert resent == "sent"
    assert uploads == []
    assert len(outbox.sent) == 2
    assert outbox.texts()[0] == outbox.texts()[1]
    assert "报告超过文件容量，已改用消息展示" in outbox.texts()[0]
    assert "数据范围：" in outbox.texts()[0]


@pytest.mark.asyncio
async def test_action_report_card_capacity_uses_existing_receipt_without_upload() -> None:
    delivery = _delivery("md").model_copy(
        update={"requires_complete": True, "request_state": "completed"}
    )
    outbox = Outbox()
    uploads: list[str] = []

    async def upload(report: object) -> str:
        uploads.append("upload")
        return "file_should_not_exist"

    assert (
        await send_delivery(
            delivery,
            send=outbox,
            chat_id="oc_report_capacity_receipt",
            max_chars=100,
            upload_file=upload,
        )
        == "failed"
    )
    assert uploads == []
    assert len(outbox.sent) == 1
    assert "容量" in outbox.texts()[0]


@pytest.mark.asyncio
async def test_action_proposal_with_report_request_sends_card_and_resends(
    actions: Actions,
) -> None:
    env = actions.env
    question = "提出修改建议，并给 MD 巡检报告"
    env.scripts.add(
        attributed(A, question),
        tool_call("order_total", region="east"),
        tool_call(f"{SOURCE}__propose_change", value="after"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    uploads: list[str] = []

    async def upload(report: object) -> str:
        uploads.append("upload")
        return "file_should_not_exist"

    outbox = Outbox(outcomes=["failed", "sent"])
    gateway = FeishuGateway(
        env.service,
        group_config(max_reply_chars=3500),
        outbox,
        clock=env.clock,
        bot_open_id=lambda: BOT,
        upload_file=upload,
    )
    group = Group(env, gateway, outbox)
    task = asyncio.create_task(gateway.run())
    try:
        await group.ask(question, sender=A, message_id="om_e6_action_proposal")
        ref = group_ref("om_e6_action_proposal", A)
        transmit = partial(
            send_delivery,
            send=outbox,
            chat_id=CHAT,
            reply_to="om_e6_action_proposal",
            max_chars=3500,
            results=env.service.results,
            ref=ref,
            upload_file=upload,
            resend=True,
        )
        assert await env.service.results.send(ref, transmit, resend=True) == "sent"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert uploads == []
    assert len(outbox.sent) == 2
    assert outbox.texts()[0] == outbox.texts()[1]
    assert "含需完整展示的动作事实" in outbox.texts()[0]
    assert "本轮未形成可恢复审批" in outbox.texts()[0]
    assert "before → after" in outbox.texts()[0] and "/批准" not in outbox.texts()[0]
    assert actions.adapter.calls == []


@pytest.mark.asyncio
async def test_action_result_with_report_request_sends_card_and_resends_same_card(
    actions: Actions,
) -> None:
    env = actions.env
    question = "巡检后提出动作并给 MD 报告"
    env.scripts.add(
        attributed(A, question),
        tool_call("order_total", region="east"),
        tool_call(f"{SOURCE}__propose_change", value="after"),
        request_execution,
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    uploaded: list[str] = []

    async def upload(report: object) -> str:
        uploaded.append("upload")
        return "file_should_not_exist"

    outbox = Outbox(outcomes=["sent", "failed", "sent"])
    gateway = FeishuGateway(
        env.service,
        group_config(max_reply_chars=3500),
        outbox,
        clock=env.clock,
        bot_open_id=lambda: BOT,
        upload_file=upload,
    )
    group = Group(env, gateway, outbox)
    task = asyncio.create_task(gateway.run())
    try:
        await group.ask(question, sender=A, message_id="om_e6_action_origin")
        action_id = await actions.one()
        await group.ask(f"/批准 {action_id}", sender=B, message_id="om_e6_action_approve")
        assert await env.rows(
            "SELECT state, delivery FROM xiaowei_request "
            "WHERE reply_message_id='om_e6_action_approve'"
        ) == [("completed", "failed")]
        ref = group_ref("om_e6_action_approve", B)
        transmit = partial(
            send_delivery,
            send=outbox,
            chat_id=CHAT,
            reply_to="om_e6_action_approve",
            max_chars=3500,
            results=env.service.results,
            ref=ref,
            upload_file=upload,
            resend=True,
        )
        assert await env.service.results.send(ref, transmit, resend=True) == "sent"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert uploaded == []
    assert len(outbox.sent) == 3
    assert outbox.texts()[1] == outbox.texts()[2]
    assert "含需完整展示的动作事实" in outbox.texts()[1]
    assert "动作执行事实" in outbox.texts()[1]


@pytest.mark.asyncio
async def test_paused_saved_answer_with_report_format_uses_card_before_upload(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    question = "巡检东区订单并生成 MD 报告"
    env.scripts.add(
        question,
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    original_process = env.service.process

    async def pause_before_send(receipt: object) -> object:
        record = await original_process(receipt)  # type: ignore[arg-type]
        # 防御性状态组合：SDK-A 当前的暂停材料不会选择报告，但已保存回答可能来自兼容版本。
        async with env.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE xiaowei_request SET state='paused', session_state=NULL "
                    "WHERE turn_id=:turn"
                ),
                {"turn": record.turn_id},
            )
        return record

    monkeypatch.setattr(env.service, "process", pause_before_send)
    uploads: list[str] = []

    async def upload(report: object) -> str:
        uploads.append("upload")
        return "file_should_not_exist"

    outbox = Outbox(outcomes=["failed", "sent"])
    gateway = FeishuGateway(env.service, config(), outbox, clock=env.clock, upload_file=upload)
    fs = Feishu(env, gateway, outbox)
    task = asyncio.create_task(gateway.run())
    try:
        await gateway.receive(fs.event(question, message_id="om_e6_paused"))
        await gateway.idle()
        ref = RequestRef(
            channel="feishu",
            subject_id="alice",
            conversation_id=CHATS["ou_alice"],
            request_id="om_e6_paused",
        )
        transmit = partial(
            send_delivery,
            send=outbox,
            chat_id=CHATS["ou_alice"],
            max_chars=2000,
            results=env.service.results,
            ref=ref,
            upload_file=upload,
            resend=True,
        )
        assert await env.service.results.send(ref, transmit, resend=True) == "sent"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert uploads == []
    assert len(outbox.sent) == 2
    assert outbox.texts()[0] == outbox.texts()[1]
    assert "本轮尚未完成" in outbox.texts()[0]


@pytest.mark.asyncio
async def test_failed_file_message_resend_reuses_saved_upload_key(env: Env) -> None:
    message = env.scripts.add(
        "生成东区订单 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="md"),
    )
    await env.run(message, "om_file_resend", channel="feishu", conversation="oc_file_resend")
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="oc_file_resend",
        request_id="om_file_resend",
    )
    uploaded: list[bytes] = []
    sent: list[str] = []

    async def upload(report: object) -> str:
        uploaded.append(report.content)  # type: ignore[attr-defined]
        return "file_key_1"

    async def send(chat_id: str, value: object, *, reply_to: str | None = None) -> str:
        assert isinstance(value, OutboundFile) and value.source is not None
        sent.append(value.source.key or "")
        return "failed" if len(sent) == 1 else "sent"

    partial_send = partial(
        send_delivery,
        send=send,
        chat_id="oc_file_resend",
        max_chars=2000,
        results=env.service.results,
        ref=ref,
        upload_file=upload,
    )
    assert await env.service.results.send(ref, partial_send) == "failed"
    assert (
        await env.service.results.send(ref, partial(partial_send, resend=True), resend=True)
        == "sent"
    )
    assert len(uploaded) == 1 and sent == ["file_key_1", "file_key_1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_to", [None, "om_origin"])
async def test_real_sdk_sends_one_file_after_one_upload_to_local_endpoint(
    env: Env, monkeypatch: pytest.MonkeyPatch, reply_to: str | None
) -> None:
    message = env.scripts.add(
        "生成东区订单 HTML 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format="html"),
    )
    request_id = "om_sdk_report" if reply_to is None else "om_sdk_reply"
    await env.run(message, request_id, channel="feishu", conversation="oc_sdk_report")
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="oc_sdk_report",
        request_id=request_id,
    )
    uploads: list[bytes] = []
    messages: list[tuple[str, dict[str, Any]]] = []

    async def token(request: Request) -> Response:
        return JSONResponse({"code": 0, "tenant_access_token": "t-synthetic", "expire": 7200})

    async def upload(request: Request) -> Response:
        uploads.append(await request.body())
        return JSONResponse({"code": 0, "data": {"file_key": "file_synthetic"}})

    async def sent(request: Request) -> Response:
        messages.append((request.url.path, json.loads(await request.body())))
        return JSONResponse({"code": 0, "data": {"message_id": "om_report_sent"}})

    def endpoint(_: Any) -> Starlette:
        return Starlette(
            routes=[
                Route("/open-apis/auth/v3/tenant_access_token/internal", token, methods=["POST"]),
                Route("/open-apis/im/v1/files", upload, methods=["POST"]),
                Route("/open-apis/im/v1/messages", sent, methods=["POST"]),
                Route("/open-apis/im/v1/messages/{message_id}/reply", sent, methods=["POST"]),
            ]
        )

    monkeypatch.setenv("XW_TEST_FEISHU_SECRET", "not-a-real-secret")
    with serve(endpoint, path="") as http:
        channel = FeishuChannel(
            config=dataclasses.replace(lark_channel(config()).config, domain=http.url)
        )
        transport = LarkTransport(channel, config())
        try:
            transmit = partial(
                send_delivery,
                send=transport.send,
                chat_id="oc_sdk_report",
                reply_to=reply_to,
                max_chars=2000,
                results=env.service.results,
                ref=ref,
                upload_file=transport.upload_file,
            )
            assert await env.service.results.send(ref, transmit) == "sent"
        finally:
            channel.stop(join_timeout=5)
    assert len(uploads) == len(messages) == 1
    assert b"xiaowei-report.html" in uploads[0]
    assert "数据范围：".encode() in uploads[0] and b"ev_" not in uploads[0]
    path, sent_body = messages[0]
    assert path == (
        "/open-apis/im/v1/messages/om_origin/reply" if reply_to else "/open-apis/im/v1/messages"
    )
    assert sent_body["msg_type"] == "file"
    assert json.loads(sent_body["content"]) == {"file_key": "file_synthetic"}


@pytest.mark.asyncio
@pytest.mark.parametrize("format_", ["md", "html"])
@pytest.mark.parametrize("initial", ["failed", "unknown", "upload_unknown", "revoked"])
async def test_legacy_resend_keeps_bound_file_across_renderer_upgrade(
    env: Env, monkeypatch: pytest.MonkeyPatch, format_: str, initial: str
) -> None:
    """任意模板升级都不能改写已绑定的 v0 文件，真实 SDK 仅访问本机协议替身。"""
    question = env.scripts.add(
        "查东区订单并生成文件",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format=format_),
    )
    await env.run(question, "om_legacy_upgrade", channel="feishu", conversation="oc_legacy")
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="oc_legacy",
        request_id="om_legacy_upgrade",
    )
    uploads: list[bytes] = []
    messages: list[dict[str, Any]] = []

    async def token(request: Request) -> Response:
        return JSONResponse({"code": 0, "tenant_access_token": "t-synthetic", "expire": 7200})

    async def upload(request: Request) -> Response:
        uploads.append(await request.body())
        if initial == "upload_unknown":
            return JSONResponse({}, status_code=500)
        return JSONResponse({"code": 0, "data": {"file_key": "file_original"}})

    async def sent(request: Request) -> Response:
        messages.append(json.loads(await request.body()))
        if len(messages) == 1:
            if initial == "unknown":
                return JSONResponse({}, status_code=500)
            return JSONResponse({"code": 230050, "msg": "synthetic rejection"})
        return JSONResponse({"code": 0, "data": {"message_id": "om_restored"}})

    def endpoint(_: Any) -> Starlette:
        return Starlette(
            routes=[
                Route("/open-apis/auth/v3/tenant_access_token/internal", token, methods=["POST"]),
                Route("/open-apis/im/v1/files", upload, methods=["POST"]),
                Route("/open-apis/im/v1/messages", sent, methods=["POST"]),
            ]
        )

    # 更换模板后，旧实现会把新字节交给 bind_report，导致 report_mismatch。
    original_builder = feishu.build_report

    def upgraded_builder(delivery: Delivery):
        report = original_builder(delivery)
        content = report.content + b"\nchanged template\n"
        return dataclasses.replace(
            report, content=content, digest=hashlib.sha256(content).hexdigest()
        )

    monkeypatch.setenv("XW_TEST_FEISHU_SECRET", "not-a-real-secret")
    with serve(endpoint, path="") as http:
        channel = FeishuChannel(
            config=dataclasses.replace(lark_channel(config()).config, domain=http.url)
        )
        transport = LarkTransport(channel, config())
        transmit = partial(
            send_delivery,
            send=transport.send,
            upload_file=transport.upload_file,
            chat_id="oc_legacy",
            max_chars=2000,
            results=env.service.results,
            ref=ref,
        )
        try:
            expected = "unknown" if initial in ("unknown", "upload_unknown") else "failed"
            assert await env.service.results.send(ref, transmit) == expected
            async with env.engine.connect() as conn:
                saved = (
                    await conn.execute(
                        text(
                            "SELECT format, body, digest, file_key, upload_state "
                            "FROM xiaowei_report"
                        )
                    )
                ).one()
            assert saved.format == format_
            assert saved.body in uploads[0]
            assert hashlib.sha256(saved.body).hexdigest() == saved.digest
            if initial != "upload_unknown":
                assert saved.file_key == "file_original" and saved.upload_state == "uploaded"
            model_calls, query_calls = (
                sum(len(calls) for calls in env.scripts.calls.values()),
                len(env.adapter.calls),
            )
            monkeypatch.setattr(feishu, "build_report", upgraded_builder)
            resend = partial(transmit, resend=True)
            if initial == "revoked":
                env.grants.revoke_all()
                with pytest.raises(AccessDeniedError):
                    await env.service.results.send(ref, resend, resend=True)
            else:
                expected = "unknown" if initial == "upload_unknown" else "sent"
                assert await env.service.results.send(ref, resend, resend=True) == expected
            assert len(uploads) == 1  # 补发没有重新上传或查询。
            assert len(messages) == (
                2 if initial in ("failed", "unknown") else 0 if initial == "upload_unknown" else 1
            )
            for message in messages:
                assert json.loads(message["content"]) == {"file_key": "file_original"}
            assert (
                sum(len(calls) for calls in env.scripts.calls.values()),
                len(env.adapter.calls),
            ) == (model_calls, query_calls)
            async with env.engine.connect() as conn:
                assert (
                    await conn.execute(
                        text(
                            "SELECT format, body, digest, file_key, upload_state "
                            "FROM xiaowei_report"
                        )
                    )
                ).one() == saved
        finally:
            channel.stop(join_timeout=5)
