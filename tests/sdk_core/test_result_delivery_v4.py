"""v4 交付贯穿 RequestRecord→ResultDelivery→实际发送，外部发送只用替身。"""

import pytest
from tests.sdk_core.test_app import answer, evidence_in, tool_call
from tests.sdk_core.test_channel_service import Env
from tests.sdk_core.test_channel_service import env as env
from tests.sdk_core.test_delivery_parts import Platform, parts, rejected, transmit

from xiaowei.channel import RequestRef

pytestmark = pytest.mark.loopback


async def test_v4_bound_message_recovery_keeps_bytes_uuid_and_no_reexecution(env: Env) -> None:
    message = env.scripts.add(
        "v4 正常业务回答",
        tool_call("run_query", region="east"),
        lambda c: answer(evidence_in(c), "继续观察"),
    )
    await env.run(message, "new", channel="feishu", conversation="oc_d1a")
    ref = RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_d1a", request_id="new"
    )
    platform = Platform(outcomes=[rejected()])
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "failed"
    (before,) = await parts(env, ref)
    assert b"\xe6\x8a\x80\xe6\x9c\xaf\xe8\xaf\xa6\xe6\x83\x85" in before.body
    assert (
        await env.service.results.send(ref, transmit(env, ref, platform, resend=True), resend=True)
        == "sent"
    )
    (after,) = await parts(env, ref)
    assert (before.body, before.uuid) == (after.body, after.uuid)
    assert len(env.adapter.calls) == 1 and env.model_calls(message) == 2
    assert not platform.files


@pytest.mark.parametrize("format_", [None, "md"], ids=["message", "file-and-message"])
async def test_old_security_v4_bound_parts_keep_bytes_keys_and_uuid(
    postgres_url, monkeypatch, format_,
):
    from dataclasses import replace
    from functools import partial

    from tests.p1b.test_starrocks_audit import AUDIT_TARGET
    from tests.p1b.test_starrocks_security_audit import record
    from tests.sdk_core.test_security_inspection_eval import (
        ARGS,
        audit_reads,
        security_channel,
        security_driver,
    )
    from tests.sdk_core.test_starrocks_tools import assembled

    from xiaowei import starrocks_tools
    from xiaowei.channel import InboundRequest
    from xiaowei.feishu import send_delivery

    original = starrocks_tools.analyze_security_rows
    monkeypatch.setattr(starrocks_tools, "analyze_security_rows", lambda *args, **kwargs: replace(
        original(*args, **kwargs), scanned_through="2026-10-22T10:04:37+00:00",
    ))
    drv = security_driver(record(queryId="q-old-security"))
    async with assembled(postgres_url, monkeypatch, drv, AUDIT_TARGET) as audit:
        service, store = security_channel(audit)
        message = audit.scripts.add(
            "旧巡检交付", tool_call("inspect_audit_security", **ARGS),
            lambda call: answer(evidence_in(call), "旧模型说明不改写", report_format=format_),
        )
        saved = await service.process(await service.accept(InboundRequest(
            channel="feishu", channel_request_id="old-security", subject_id="alice",
            conversation_id="oc_d1a", mode="query", message=message, received_at=audit.clock(),
        )))
        assert saved.state == "completed"
        ref = RequestRef(channel="feishu", subject_id="alice", conversation_id="oc_d1a",
                         request_id="old-security")
        platform = Platform(outcomes=[rejected()])

        def sender(*, resend=False):
            return partial(
                send_delivery, send=platform.send, upload_file=platform.upload,
                chat_id="oc_d1a", max_chars=3500, results=service.results, ref=ref, resend=resend,
            )

        assert await service.results.send(ref, sender()) == "failed"
        before = await store.delivery_parts(saved)
        assert before and (any(p.kind == "md" for p in before) is (format_ == "md"))
        if format_ is None:
            assert "10:04:37" in before[0].body.decode()
        collected = audit_reads(drv)

        def no_rerender(*args, **kwargs):
            raise AssertionError("已绑定交付不得重新渲染")

        monkeypatch.setattr("xiaowei.feishu_delivery._data_parts", no_rerender)
        assert await service.results.send(ref, sender(resend=True), resend=True) == "sent"
        after = await store.delivery_parts(saved)
        assert [(p.body, p.uuid, p.file_key) for p in before] == [
            (p.body, p.uuid, p.file_key) for p in after
        ]
        assert audit_reads(drv) == collected and len(audit.scripts.calls[message]) == 2
