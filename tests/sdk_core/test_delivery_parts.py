"""D1a delivery behavior through real Runner/Evidence/PostgreSQL; channel I/O is synthetic."""

import asyncio
import json
import threading
from dataclasses import dataclass, field, replace
from functools import partial
from time import monotonic
from typing import Any

import pytest
from lark_channel.channel.errors import FeishuChannelErrorCode, SendError
from lark_channel.channel.types import OutboundFile, SendResult
from sqlalchemy import text
from tests.sdk_core.test_app import answer, clarify, evidence_in, tool_call
from tests.sdk_core.test_channel_service import Env
from tests.sdk_core.test_channel_service import env as env
from tests.sdk_core.test_feishu_reports import _delivery

from xiaowei.channel import AccessDeniedError, RequestRef
from xiaowei.channel_store import RequestUnavailableError
from xiaowei.feishu import send_delivery
from xiaowei.feishu_report import ReportCapacityError, ReportFile, build_report, build_reports
from xiaowei.models import DeliveryFactRef, DeliveryText
from xiaowei.storage import Readiness, hold_instance_lock

pytestmark = pytest.mark.loopback


@dataclass
class Platform:
    sent: list[tuple[object, str | None]] = field(default_factory=list)
    files: list[bytes] = field(default_factory=list)
    outcomes: list[object] = field(default_factory=list)
    upload_error: Exception | None = None

    async def upload(self, report: ReportFile) -> str:
        self.files.append(report.content)
        if self.upload_error:
            raise self.upload_error
        return "file_d1a_complete"

    async def send(
        self,
        chat_id: str,
        value: object,
        *,
        reply_to: str | None = None,
        uuid: str | None = None,
    ) -> Any:
        assert chat_id == "oc_d1a"
        self.sent.append((value, uuid))
        # Supports the old call contract so RED names the behavior, not an adapter error.
        if uuid is None:
            return "sent"
        if self.outcomes:
            result = self.outcomes.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return SendResult(success=True, message_id=f"om_d1a_{len(self.sent)}")


async def request(env: Env, *, format_: str | None = "md") -> RequestRef:
    message = env.scripts.add(
        "查询东区订单并交付结果",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), report_format=format_),
    )
    await env.run(message, "om_d1a", channel="feishu", conversation="oc_d1a")
    return RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_d1a", request_id="om_d1a"
    )


@pytest.mark.asyncio
async def test_explicit_html_advice_file_uses_verified_no_query_delivery(env: Env) -> None:
    draft = "SELECT region, SUM(total) FROM shop.sales GROUP BY region"
    message = env.scripts.add(
        "只解释这段 SQL，做一份 HTML 文件，不执行查询",
        lambda call: answer([], analysis="", advice=draft, report_format="html"),
    )
    record = await env.run(message, "om_advice_html", channel="feishu", conversation="oc_d1a")
    assert record.state == "completed", record.failure_code
    assert env.adapter.calls == []
    assert "只为换成文件格式不增加查询" in env.scripts.calls[message][0].instructions
    assert "容量超限由代码分卷" in env.scripts.calls[message][0].instructions
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="oc_d1a",
        request_id="om_advice_html",
    )
    platform = Platform()
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "sent"
    assert len(platform.files) == 1 and len(platform.sent) == 2
    assert b'<div class="model-prose">' in platform.files[0]
    assert draft.encode() in platform.files[0]
    assert "本次完整结果见附件" in str(platform.sent[-1][0])
    assert "结果预览" not in str(platform.sent[-1][0])


@pytest.mark.parametrize("format_", ["md", "html"])
def test_split_report_preserves_every_typed_row_and_labels_each_file(
    monkeypatch: pytest.MonkeyPatch, format_: str
) -> None:
    import re
    from html import unescape

    delivery = _delivery(format_)
    rows = [
        {"index": n, "nullable": None, "literal": "NULL", "huge": 2**60 + n, "text": f"<值{n}>"}
        for n in range(12)
    ]
    result = {
        "sql": "SELECT '<x>'",
        "columns": ["index", "nullable", "literal", "huge", "text"],
        "metadata": {"scope": "昨天", "applied_limit": 1000},
        "rows": rows,
    }
    fact = delivery.facts[0].model_copy(
        update={
            "rows": tuple(rows),
            "truncated": True,
            "result_json": json.dumps(result, ensure_ascii=False),
        }
    )
    delivery = delivery.model_copy(
        update={
            "facts": (fact,),
            "monitoring_notice": "[not-a-link](https://invalid.example) <img src=x>",
        }
    )
    # HTML 文件现在自带离线样式；容量测量仍只按完整序列化字节。
    limit = 5500 if format_ == "html" else 1100
    monkeypatch.setattr("xiaowei.feishu_report.MAX_REPORT_BYTES", limit)
    files = build_reports(delivery)
    assert len(files) > 1
    recovered = []
    for index, report in enumerate(files, 1):
        assert len(report.content) <= limit
        assert report.filename == f"xiaowei-report-{index}-of-{len(files)}.{format_}"
        body = report.content.decode()
        assert f"{index}/{len(files)}" in body
        assert "工具结果已截断" in body
        if format_ == "md":
            assert "> 范围说明：" in body
        else:
            assert "<img src=x>" not in body
        if format_ == "html":
            for table in re.findall(r"<tbody>(.*?)</tbody>", body, re.S):
                assert all(f'<th scope="col">{name}</th>' in body for name in result["columns"])
                for row in re.findall(r"<tr>(.*?)</tr>", table, re.S):
                    cells = re.findall(r'<code class="cell">(.*?)</code>', row, re.S)
                    recovered.append(
                        dict(
                            zip(
                                result["columns"],
                                (json.loads(unescape(c)) for c in cells),
                                strict=True,
                            )
                        )
                    )
        else:
            # MD 表格：按渲染后的单元格还原值与类型（代码片段内为 JSON 文本）。
            from markdown_it import MarkdownIt

            rendered = MarkdownIt("default").enable("table").render(body)
            for table in re.findall(r"<tbody>(.*?)</tbody>", rendered, re.S):
                for row in re.findall(r"<tr>(.*?)</tr>", table, re.S):
                    cells = re.findall(r"<td>(.*?)</td>", row, re.S)
                    values = (
                        json.loads(unescape(c.removeprefix("<code>").removesuffix("</code>")))
                        for c in cells
                    )
                    recovered.append(dict(zip(result["columns"], values, strict=True)))
        if "证据 ev_report" in body:
            assert "scope" in body and "applied_limit" in body
            assert "SELECT" in body
    assert recovered == rows
    assert any("需要复核" in report.content.decode() for report in files)


def test_split_report_rejects_one_indivisible_oversized_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = _delivery("md")
    row = {"value": "x" * 2000}
    fact = delivery.facts[0].model_copy(
        update={"rows": (row,), "result_json": json.dumps({"rows": [row]})}
    )
    monkeypatch.setattr("xiaowei.feishu_report.MAX_REPORT_BYTES", 1100)
    with pytest.raises(ReportCapacityError):
        build_reports(delivery.model_copy(update={"facts": (fact,)}))


def test_report_splits_when_encoded_file_exceeds_boundary_by_one_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = _delivery("md")
    complete = build_report(delivery)
    monkeypatch.setattr("xiaowei.feishu_report.MAX_REPORT_BYTES", len(complete.content))
    assert build_reports(delivery) == (complete,)
    monkeypatch.setattr("xiaowei.feishu_report.MAX_REPORT_BYTES", len(complete.content) - 1)
    pieces = build_reports(delivery)
    assert len(pieces) > 1
    assert all(len(piece.content) <= len(complete.content) - 1 for piece in pieces)


def test_automatic_split_keeps_historical_brief_without_exporting_raw_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re

    delivery = _delivery("md")
    brief = delivery.facts[0].model_copy(
        update={
            "evidence_id": "ev_history",
            "view": "brief",
            "summary": "历史结果摘要",
            "result_json": '{"secret":"HISTORICAL_RAW_CANARY"}',
        }
    )
    rows = [{"index": n, "value": "x" * 140} for n in range(12)]
    current = delivery.facts[0].model_copy(
        update={"rows": tuple(rows), "result_json": json.dumps({"rows": rows})}
    )
    monkeypatch.setattr("xiaowei.feishu_report.MAX_REPORT_BYTES", 1100)
    reports = build_reports(
        delivery.model_copy(update={"facts": (brief, current)}), expand_brief=False
    )
    content = "\n".join(report.content.decode() for report in reports)
    assert len(reports) > 1
    assert "历史结果摘要" in content and "HISTORICAL_RAW_CANARY" not in content
    assert [int(n) for n in re.findall(r"^\| (\d+) \|", content, re.M)] == list(range(12))
    # 140 字超过单元格宽度，表格写“见下方”，原值在表下围栏中逐条完整保留。
    # 拆分文件中的行号与章节标题一致，按全局行号编号，不在每个文件里从 1 重来。
    assert [
        int(n) for n in re.findall(r"^\| \d+ \| 见下方 第 (\d+) 行 \|$", content, re.M)
    ] == list(range(1, 13))
    assert len(re.findall(r"^x{140}$", content, re.M)) == 12


@pytest.mark.asyncio
async def test_explicit_file_is_followed_by_confirmed_body(env: Env) -> None:
    ref = await request(env)
    platform = Platform()
    transmit = partial(
        send_delivery,
        send=platform.send,
        upload_file=platform.upload,
        chat_id="oc_d1a",
        max_chars=2000,
        results=env.service.results,
        ref=ref,
    )
    assert await env.service.results.send(ref, transmit) == "sent"
    assert len(platform.files) == 1
    assert len(platform.sent) == 2
    assert isinstance(platform.sent[0][0], OutboundFile)
    assert "完整结果" in json.dumps(platform.sent[1][0], ensure_ascii=False)
    assert all(uuid for _, uuid in platform.sent)


@pytest.mark.asyncio
async def test_report_render_does_not_block_other_requests(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = await request(env)
    platform = Platform()
    loop = asyncio.get_running_loop()
    real_build_reports = build_reports
    probe_lag: list[float] = []

    def delayed_render(*args, **kwargs):
        started = monotonic()
        loop.call_soon_threadsafe(lambda: probe_lag.append(monotonic() - started))
        threading.Event().wait(0.3)
        return real_build_reports(*args, **kwargs)

    monkeypatch.setattr("xiaowei.feishu_delivery.build_reports", delayed_render)
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "sent"
    assert probe_lag and probe_lag[0] < 0.2
    assert len(platform.files) == 1 and len(platform.sent) == 2


@pytest.mark.asyncio
async def test_cancel_during_report_render_binds_and_uploads_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = await request(env)
    platform = Platform()
    entered, release = threading.Event(), threading.Event()
    real_build_reports = build_reports

    def delayed_render(*args, **kwargs):
        entered.set()
        release.wait(3)
        return real_build_reports(*args, **kwargs)

    monkeypatch.setattr("xiaowei.feishu_delivery.build_reports", delayed_render)
    task = asyncio.create_task(env.service.results.send(ref, transmit(env, ref, platform)))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert await parts(env, ref) == ()
    assert platform.files == [] and platform.sent == []


def test_explicit_brief_file_contains_authorized_original_values() -> None:
    delivery = _delivery("md")
    fact = delivery.facts[0].model_copy(update={"view": "brief", "summary": "历史摘要"})
    body = build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()
    assert "SELECT" in body
    assert "<at id=all>" in body
    assert "历史摘要" not in body


@pytest.mark.parametrize("mixed", [False, True])
def test_automatic_file_keeps_brief_and_complete_analysis_without_expanding_history(mixed):
    from xiaowei.feishu_delivery import _data_parts

    delivery = _delivery("md").model_copy(update={"report_format": None})
    brief = delivery.facts[0].model_copy(
        update={
            "view": "brief",
            "summary": "历史摘要：本次返回一行",
            "result_json": '{"raw":"HISTORICAL_ORIGINAL_CANARY"}',
            "metadata": {"sql": "HISTORICAL_SQL_CANARY"},
        }
    )
    current = delivery.facts[0].model_copy(update={"evidence_id": "ev_report_current"})
    facts = (brief, current) if mixed else (brief,)
    # 新语义按平台限制：分析合计超过 30,000 字节才需要自动附件。
    analyses = [f"完整分析{i}" + "分析内容" * 490 for i in range(6)]
    delivery = delivery.model_copy(
        update={
            "facts": facts,
            "blocks": (
                *(DeliveryText(text=text) for text in analyses),
                *(DeliveryFactRef(evidence_id=f.evidence_id) for f in facts),
            ),
        }
    )
    file, body = _data_parts(delivery, 0, 600, "oc_d1a", None)
    report = file.body.decode()
    assert file.kind == "md" and body.kind == "body"
    assert "历史摘要：本次返回一行" in report and all(text in report for text in analyses)
    assert "HISTORICAL_ORIGINAL_CANARY" not in report
    assert "HISTORICAL_SQL_CANARY" not in report
    if mixed:
        assert "<at id=all>" in report  # 新full事实仍完整保留。


def transmit(
    env: Env, ref: RequestRef, platform: Platform, *, resend: bool = False, max_chars: int = 2000
):
    return partial(
        send_delivery,
        send=platform.send,
        upload_file=platform.upload,
        chat_id="oc_d1a",
        max_chars=max_chars,
        results=env.service.results,
        ref=ref,
        resend=resend,
    )


def rejected() -> SendResult:
    return SendResult.fail(
        SendError(code=FeishuChannelErrorCode.PERMISSION_DENIED, retryable=False)
    )


async def parts(env: Env, ref: RequestRef):
    record = await env.store.get("feishu", "alice", ref.conversation_id, ref.request_id)
    return await env.store.delivery_parts(record)


@pytest.mark.asyncio
async def test_short_complete_reply_has_one_receipt_and_no_attachment(env: Env) -> None:
    ref = await request(env, format_=None)
    platform = Platform()
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "sent"
    assert len(platform.sent) == 1 and platform.files == []
    (p,) = await parts(env, ref)
    assert p.kind == "body" and p.message_id and p.send_state == "sent"


@pytest.mark.asyncio
async def test_plain_clarification_send_failure_has_nonempty_incomplete_hint(env: Env) -> None:
    question = env.scripts.add("需要补充目标", clarify("请说明目标集群"))
    await env.run(question, "om_d1a", channel="feishu", conversation="oc_d1a")
    ref = RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_d1a", request_id="om_d1a"
    )
    platform = Platform(outcomes=[rejected()])
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "failed"
    assert len(platform.sent) == 2 and platform.files == []
    hint = json.dumps(platform.sent[-1][0], ensure_ascii=False)
    assert "完整结果尚未交付" in hint and "操作员" in hint


@pytest.mark.asyncio
async def test_uncited_auxiliary_index_has_only_tool_object_and_never_raw_values(env: Env) -> None:
    message = env.scripts.add(
        "先调查后回答业务问题",
        tool_call("order_total", region="aux_value_do_not_display"),
        tool_call("run_query", region="west"),
        lambda call: answer(evidence_in(call)[-1:]),
    )
    record = await env.run(message, "om_d1a", channel="feishu", conversation="oc_d1a")
    assert record.state == "completed"
    ref = RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_d1a", request_id="om_d1a"
    )
    platform = Platform()
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "sent"
    assert platform.files == [] and len(platform.sent) == 1
    shown = json.dumps(platform.sent[0][0], ensure_ascii=False)
    assert "查看依据" in shown and "local/order_total" in shown
    assert "aux_value_do_not_display" not in shown


@pytest.mark.asyncio
async def test_new_feishu_request_cannot_be_marked_sent_without_part_receipts(env: Env) -> None:
    ref = await request(env, format_=None)

    async def unconfirmed(_delivery):
        return "sent"

    assert await env.service.results.send(ref, unconfirmed) == "failed"
    record = await env.store.get("feishu", "alice", ref.conversation_id, ref.request_id)
    assert record.delivery == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["bind", "receipt"])
async def test_part_storage_failure_never_claims_delivery_or_sends_remaining_parts(env: Env, stage):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError

    from xiaowei.channel_store import ChannelStoreUnavailableError

    ref = await request(env)
    platform = Platform()

    def fail(_conn, _cursor, statement, _parameters, *_args):
        before_upload = stage == "bind" and "INSERT INTO xiaowei_delivery_part" in statement
        after_message = (
            stage == "receipt"
            and "UPDATE xiaowei_delivery_part SET send_state=" in statement
            and "message_id=" in statement
        )
        if before_upload or after_message:
            raise SQLAlchemyError("synthetic delivery storage failure")

    event.listen(env.engine.sync_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(ChannelStoreUnavailableError):
            await env.service.results.send(ref, transmit(env, ref, platform))
    finally:
        event.remove(env.engine.sync_engine, "before_cursor_execute", fail)
    assert not env.store.readiness.ok
    assert len(platform.files) == len(platform.sent) == (0 if stage == "bind" else 1)
    if stage == "receipt":
        file, body = await parts(env, ref)
        assert file.upload_state == "uploaded" and file.send_state == "unknown"
        assert file.message_id is None and body.send_state == "pending"
    record = await env.store.get("feishu", "alice", ref.conversation_id, ref.request_id)
    assert record.delivery == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [rejected(), SendResult.ok(message_id="")])
async def test_resend_only_missing_body_preserves_bytes_and_uuid(
    env: Env,
    first: SendResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = await request(env)
    platform = Platform(outcomes=[SendResult.ok(message_id="om_file"), first])
    expected = "failed" if not first.success else "unknown"
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == expected
    before = await parts(env, ref)
    assert before[0].send_state == "sent" and before[1].send_state == expected
    calls = len(env.adapter.calls)

    def changed(*_args, **_kwargs):
        raise AssertionError("重发不能按新模板生成字节")

    monkeypatch.setattr("xiaowei.feishu_delivery.build_reports", changed)
    assert (
        await env.service.results.send(ref, transmit(env, ref, platform, resend=True), resend=True)
        == "sent"
    )
    after = await parts(env, ref)
    assert [p.body for p in after] == [p.body for p in before]
    assert [p.uuid for p in after] == [p.uuid for p in before]
    assert platform.sent[-1][1] == before[1].uuid
    assert len(platform.files) == 1 and len(env.adapter.calls) == calls


@pytest.mark.asyncio
async def test_unknown_file_message_can_be_manually_resent_using_existing_upload(env: Env) -> None:
    ref = await request(env)
    platform = Platform(outcomes=[SendResult.ok(message_id="")])
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "unknown"
    first = await parts(env, ref)
    assert first[0].upload_state == "uploaded" and first[1].send_state == "pending"
    assert (
        await env.service.results.send(ref, transmit(env, ref, platform, resend=True), resend=True)
        == "sent"
    )
    assert len(platform.files) == 1
    assert platform.sent[0][1] == platform.sent[2][1] == first[0].uuid


@pytest.mark.asyncio
async def test_unknown_then_definite_failure_stays_unknown(env: Env) -> None:
    ref = await request(env, format_=None)
    platform = Platform(outcomes=[SendResult.ok(message_id="")])
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "unknown"
    platform.outcomes.append(rejected())
    assert (
        await env.service.results.send(ref, transmit(env, ref, platform, resend=True), resend=True)
        == "unknown"
    )
    assert (await parts(env, ref))[0].send_state == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("format_", [None, "md"])
async def test_legacy_unknown_then_failed_keeps_unknown(env: Env, format_: str | None) -> None:
    ref = await request(env, format_=format_)
    async with env.engine.begin() as conn:
        await conn.execute(text("UPDATE xiaowei_request SET delivery_version=0"))
    outcomes = ["unknown", "failed"]
    platform = Platform()

    async def legacy_send(*_args, **_kwargs):
        return outcomes.pop(0)

    send = partial(transmit(env, ref, platform), send=legacy_send)
    assert await env.service.results.send(ref, send) == "unknown"
    assert await env.service.results.send(ref, partial(send, resend=True), resend=True) == "unknown"
    record = await env.store.get("feishu", "alice", ref.conversation_id, ref.request_id)
    assert record.delivery == "unknown" and record.delivery_version == 0


@pytest.mark.asyncio
async def test_upload_unknown_never_reuploads_or_announces_delivered_attachment(env: Env) -> None:
    ref = await request(env)
    platform = Platform(upload_error=TimeoutError())
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "unknown"
    assert (
        await env.service.results.send(ref, transmit(env, ref, platform, resend=True), resend=True)
        == "unknown"
    )
    assert len(platform.files) == 1
    assert all(uuid is None for _, uuid in platform.sent)
    assert all(
        "完整结果尚未交付" in json.dumps(message, ensure_ascii=False)
        and "见附件" not in json.dumps(message, ensure_ascii=False)
        for message, _ in platform.sent
    )


@pytest.mark.asyncio
async def test_one_permission_snapshot_per_attempt_and_revocation_blocks_next_attempt(
    env: Env,
) -> None:
    ref = await request(env)
    platform = Platform(outcomes=[SendResult.ok(message_id="file"), rejected()])
    original = platform.upload

    async def revoke(report: ReportFile) -> str:
        key = await original(report)
        env.grants.revoke_all()
        return key

    before = env.access.resolves
    send = partial(transmit(env, ref, platform), upload_file=revoke)
    assert await env.service.results.send(ref, send) == "failed"
    assert env.access.resolves - before == 1
    assert len(platform.sent) == 3
    with pytest.raises(AccessDeniedError):
        await env.service.results.send(ref, transmit(env, ref, platform, resend=True), resend=True)
    assert len(platform.sent) == 3 and len(platform.files) == 1


@pytest.mark.asyncio
async def test_capacity_failure_shows_preview_but_resend_is_local_only(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ref = await request(env)
    # Simulate D1a's incomplete single-file carrier, while using real authorized Evidence.
    monkeypatch.setattr("xiaowei.feishu_report.MAX_REPORT_BYTES", 100)
    platform = Platform()
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "failed"
    assert len(platform.sent) == 1 and platform.files == []
    shown = json.dumps(platform.sent[0][0], ensure_ascii=False)
    assert "完整结果尚未交付" in shown and "预览" in shown and "见附件" not in shown
    assert (
        await env.service.results.send(ref, transmit(env, ref, platform, resend=True), resend=True)
        == "failed"
    )
    assert len(platform.sent) == 1 and platform.files == []
    assert "容量不足，不能重发" in caplog.text


@pytest.mark.asyncio
async def test_wrong_personal_chat_cannot_read_or_send_saved_parts(env: Env) -> None:
    ref = await request(env)
    platform = Platform()
    bad = ref.model_copy(update={"conversation_id": "oc_wrong"})
    with pytest.raises(RequestUnavailableError):
        await env.service.results.send(bad, transmit(env, bad, platform))
    assert platform.sent == [] and platform.files == []


@pytest.mark.asyncio
async def test_parts_do_not_store_personal_chat_and_are_cascade_deleted(env: Env) -> None:
    ref = await request(env)
    platform = Platform()
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "sent"
    async with env.engine.begin() as conn:
        assert "oc_d1a" not in str(
            (await conn.execute(text("SELECT to_jsonb(r) FROM xiaowei_request r"))).all()
        )
        rows = (await conn.execute(text("SELECT * FROM xiaowei_delivery_part"))).all()
        assert "oc_d1a" not in str(rows)
        await conn.execute(text("DELETE FROM xiaowei_request"))
        assert await conn.scalar(text("SELECT count(*) FROM xiaowei_delivery_part")) == 0


@pytest.mark.asyncio
async def test_evidence_expiry_during_upload_stops_remaining_messages(env: Env) -> None:
    from datetime import timedelta

    ref = await request(env)
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE xiaowei_evidence SET expires_at=:end"),
            {"end": env.clock() + timedelta(seconds=1)},
        )
    platform = Platform()

    async def delayed_upload(report: ReportFile) -> str:
        key = await platform.upload(report)
        env.clock.advance(2)
        return key

    with pytest.raises(RequestUnavailableError):
        await env.service.results.send(
            ref, partial(transmit(env, ref, platform), upload_file=delayed_upload)
        )
    assert platform.sent == []


@pytest.mark.asyncio
async def test_recovery_stops_old_upload_and_preserves_unknown(env: Env) -> None:
    ref = await request(env)
    entered, release = asyncio.Event(), asyncio.Event()
    platform = Platform()

    async def upload(report: ReportFile) -> str:
        entered.set()
        await release.wait()
        return "file_stale"

    old = asyncio.create_task(
        env.service.results.send(ref, partial(transmit(env, ref, platform), upload_file=upload))
    )
    await asyncio.wait_for(entered.wait(), 5)
    restarted = env.channel_store(readiness=Readiness())
    async with hold_instance_lock(env.engine, restarted.readiness) as lock:
        await restarted.recover(lock)
    release.set()
    with pytest.raises(RequestUnavailableError):
        await old
    assert platform.sent == []
    assert (await parts(env, ref))[0].upload_state == "unknown"


@pytest.mark.asyncio
async def test_recovery_during_second_file_upload_stops_remaining_messages(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = await request(env)
    report = build_report(_delivery("md"))
    monkeypatch.setattr(
        "xiaowei.feishu_delivery.build_reports", lambda *_args, **_kwargs: (report,) * 2
    )
    entered, release = asyncio.Event(), asyncio.Event()
    platform = Platform()

    async def upload(report: ReportFile) -> str:
        if len(platform.files) == 1:
            entered.set()
            await release.wait()
        return await platform.upload(report)

    old = asyncio.create_task(
        env.service.results.send(ref, partial(transmit(env, ref, platform), upload_file=upload))
    )
    await asyncio.wait_for(entered.wait(), 5)
    restarted = env.channel_store(readiness=Readiness())
    async with hold_instance_lock(env.engine, restarted.readiness) as lock:
        await restarted.recover(lock)
    release.set()
    with pytest.raises(RequestUnavailableError):
        await old
    file_one, file_two, body = await parts(env, ref)
    assert file_one.send_state == "sent" and file_one.message_id
    assert file_two.upload_state == "unknown" and file_two.send_state == "pending"
    assert body.send_state == "pending"
    assert len(platform.sent) == 1 and len(platform.files) == 2


@pytest.mark.asyncio
async def test_real_channel_sdk_passes_saved_uuid_and_keeps_each_part_one_message(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lark_channel.channel import FeishuChannel
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from tests.sdk_core.mcp_fixture import serve
    from tests.sdk_core.test_feishu import config

    from xiaowei.feishu import LarkTransport, lark_channel

    ref = await request(env)
    uploads: list[bytes] = []
    messages: list[dict[str, Any]] = []

    async def token(request):
        return JSONResponse({"code": 0, "tenant_access_token": "t-synthetic", "expire": 7200})

    async def upload(request):
        uploads.append(await request.body())
        return JSONResponse({"code": 0, "data": {"file_key": "file_sdk"}})

    async def sent(request):
        messages.append(await request.json())
        # 缺ID的真实SDK响应不能算确认；随后手动恢复同一正文部分。
        data = {} if len(messages) == 2 else {"message_id": f"om_sdk_{len(messages)}"}
        return JSONResponse({"code": 0, "data": data})

    def endpoint(_):
        return Starlette(
            routes=[
                Route("/open-apis/auth/v3/tenant_access_token/internal", token, methods=["POST"]),
                Route("/open-apis/im/v1/files", upload, methods=["POST"]),
                Route("/open-apis/im/v1/messages", sent, methods=["POST"]),
            ]
        )

    monkeypatch.setenv("XW_TEST_FEISHU_SECRET", "not-a-real-secret")
    with serve(endpoint, path="") as http:
        channel = FeishuChannel(config=replace(lark_channel(config()).config, domain=http.url))
        transport = LarkTransport(channel, config())
        try:
            transmit_sdk = partial(
                send_delivery,
                send=transport.send,
                upload_file=transport.upload_file,
                chat_id="oc_d1a",
                max_chars=2000,
                results=env.service.results,
                ref=ref,
            )
            assert await env.service.results.send(ref, transmit_sdk) == "unknown"
            first = await parts(env, ref)
            assert len(messages) == 3  # 文件、正文、一次非必需提醒；没有SDK隐式分段。
            assert (
                await env.service.results.send(ref, partial(transmit_sdk, resend=True), resend=True)
                == "sent"
            )
        finally:
            channel.stop(join_timeout=5)
    assert len(uploads) == 1 and len(messages) == 4
    assert messages[0]["msg_type"] == "file"
    assert messages[0]["uuid"] == first[0].uuid
    assert messages[1]["uuid"] == messages[3]["uuid"] == first[1].uuid


@pytest.mark.asyncio
@pytest.mark.parametrize("format_", ["md", "html"])
async def test_clarification_with_report_format_sends_only_question_without_upload(
    env: Env, format_: str
) -> None:
    question = "请明确业务时区。"
    message = env.scripts.add(
        "查询昨天的订单，做成文件",
        lambda call: answer([], analysis="", clarification=question, report_format=format_),
    )
    record = await env.run(message, "om_d1a", channel="feishu", conversation="oc_d1a")
    assert record.state == "completed", record.failure_code
    ref = RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_d1a", request_id="om_d1a"
    )
    view = await env.service.results.view(ref, first=True)
    assert view.delivery is not None and view.delivery.report_format is None
    platform = Platform()
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "sent"
    assert platform.files == [] and len(platform.sent) == 1
    shown = json.dumps(platform.sent[0][0], ensure_ascii=False)
    assert question in shown and "附件" not in shown
    assert len(env.scripts.calls[message]) == 1 and env.adapter.calls == []
    (part,) = await parts(env, ref)
    assert part.kind == "body" and part.send_state == "sent"


@pytest.mark.asyncio
async def test_long_clarification_never_changes_into_an_automatic_attachment(env: Env) -> None:
    # 超过 max_chars=200 的澄清卡片按平台限制完整放入正文；仍不改成附件。
    question = "请先明确目标集群和业务时区。" * 40
    message = env.scripts.add(
        "信息不足时先问我",
        lambda call: answer([], analysis="", clarification=question, report_format="html"),
    )
    await env.run(message, "om_d1a", channel="feishu", conversation="oc_d1a")
    ref = RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_d1a", request_id="om_d1a"
    )
    platform = Platform()
    result = await env.service.results.send(
        ref, partial(transmit(env, ref, platform), max_chars=200)
    )
    assert platform.files == [] and len(platform.sent) == 1
    # 字符上限 200 只约束纯文本；澄清卡片在平台限制内完整发送。
    assert result == "sent" and question in json.dumps(platform.sent[0][0], ensure_ascii=False)
    assert all(part.kind == "body" for part in await parts(env, ref))


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["readiness", "expiry"])
async def test_hint_rechecks_attempt_after_thread_render(
    env: Env, monkeypatch: pytest.MonkeyPatch, invalidate: str
) -> None:
    # 独立审查（5fb2b30）：提醒在线程中渲染期间 readiness 关闭或请求过期，不能再外发结果。
    import xiaowei.feishu_delivery as module
    from xiaowei.channel_store import ChannelStoreError

    ref = await request(env)
    platform = Platform(outcomes=[rejected()])
    original = module._preview
    entered, release = threading.Event(), threading.Event()

    def paused_preview(delivery, max_chars, chat_id, reply_to, notice):
        if notice.startswith("完整结果尚未交付。"):
            entered.set()
            assert release.wait(8), "测试屏障超时"
        return original(delivery, max_chars, chat_id, reply_to, notice)

    monkeypatch.setattr(module, "_preview", paused_preview)
    task = asyncio.create_task(env.service.results.send(ref, transmit(env, ref, platform)))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 6), 7)
        sent_before = len(platform.sent)
        if invalidate == "readiness":
            env.readiness.lock("review_simulated_state_failure")
        else:
            env.clock.advance(10_000)
        release.set()
        try:
            await task
        except ChannelStoreError:
            pass  # 失效后的最终状态写入被拒绝是预期的
        assert len(platform.sent) == sent_before
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
