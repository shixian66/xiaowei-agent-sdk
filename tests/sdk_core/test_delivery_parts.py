"""D1a delivery behavior through real Runner/Evidence/PostgreSQL; channel I/O is synthetic."""

import asyncio
import json
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any

import pytest
from lark_channel.channel.errors import FeishuChannelErrorCode, SendError
from lark_channel.channel.types import OutboundFile, SendResult
from sqlalchemy import text
from tests.sdk_core.test_app import answer, evidence_in, tool_call
from tests.sdk_core.test_channel_service import Env
from tests.sdk_core.test_channel_service import env as env
from tests.sdk_core.test_feishu_reports import _delivery

from xiaowei.channel import AccessDeniedError, RequestRef
from xiaowei.channel_store import RequestUnavailableError
from xiaowei.feishu import send_delivery
from xiaowei.feishu_report import ReportFile, build_report
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


def test_explicit_brief_file_contains_authorized_original_values() -> None:
    delivery = _delivery("md")
    fact = delivery.facts[0].model_copy(update={"view": "brief", "summary": "历史摘要"})
    body = build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()
    assert "SELECT" in body
    assert "<at id=all>" in body
    assert "历史摘要" not in body


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

    monkeypatch.setattr("xiaowei.feishu_delivery.build_report", changed)
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
        await conn.execute(text("UPDATE xiaowei_evidence SET expires_at=:end"),
                           {"end": env.clock() + timedelta(seconds=1)})
    platform = Platform()
    async def delayed_upload(report: ReportFile) -> str:
        key = await platform.upload(report)
        env.clock.advance(2)
        return key
    with pytest.raises(RequestUnavailableError):
        await env.service.results.send(ref,
            partial(transmit(env, ref, platform), upload_file=delayed_upload))
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
