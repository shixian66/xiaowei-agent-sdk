"""Model-authored reports: real SDK/governance/storage, synthetic model/platform."""

import asyncio
import json
import logging
from dataclasses import replace
from functools import partial

import httpx2
import pytest
from lark_channel.channel.types import OutboundFile
from markdown_it import MarkdownIt
from sqlalchemy import text
from tests.sdk_core.synthetic_tools import QUERY_TOOL, TARGET, TOTAL_TOOL
from tests.sdk_core.test_app import answer, evidence_in, tool_call
from tests.sdk_core.test_channel_service import Env, app_config
from tests.sdk_core.test_channel_service import env as env
from tests.sdk_core.test_delivery_parts import Platform, transmit
from tests.sdk_core.test_feishu import Feishu, Outbox, config, running
from tests.sdk_core.test_feishu_reports import _delivery
from tests.sdk_core.test_vertex_model import VERTEX, _body

from xiaowei.app import Application
from xiaowei.channel import AccessDeniedError, RequestRef, ResultUnavailableError
from xiaowei.feishu import FeishuGateway, send_delivery
from xiaowei.feishu_report import build_report
from xiaowei.governance import GovernedTools
from xiaowei.model_api import open_model
from xiaowei.models import AgentAnswer, DeliveryText, TextBlock, TurnAnswer

pytestmark = pytest.mark.loopback

PROSE = "# 巡检发现\n\n已检查授权范围。\n\n| 项目 | 建议 |\n| --- | --- |\n| 查询 | 继续观察 |"


@pytest.mark.parametrize(
    "source,notice",
    [
        ("建议改为 ARRAY<INT>", True),
        ("建议改为 MAP<STRING,INT>", False),
        ("建议改为 List<String>", True),
        ("| 说明 |\n| --- |\n| 第一行<br>第二行 |", True),
        ("[文档](https://invalid.example/doc)", True),
        ("<https://invalid.example/doc>", True),
        ("![说明图](https://invalid.example/image.png)", True),
    ],
)
async def test_raw_md_common_markup_keeps_facts_and_resends_original_parts(
    env: Env, monkeypatch: pytest.MonkeyPatch, source: str, notice: bool
) -> None:
    message = env.scripts.add(
        "查询东区订单并制作 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(
            evidence_in(call),
            "# 巡检发现\n\n" + source + "\n\n## 后续建议\n\n继续观察。",
            report_format="md",
        ),
    )
    files = []

    async def upload(report):
        files.append(report.content)
        return "file_md_markup"

    outbox = Outbox()
    first_file = True

    async def send(chat_id, value, **kwargs):
        nonlocal first_file
        if first_file and isinstance(value, OutboundFile):
            first_file = False
            outbox.outcomes.insert(0, "failed")
        return await outbox(chat_id, value, **kwargs)

    gateway = FeishuGateway(env.service, config(), send, clock=env.clock, upload_file=upload)
    fs = Feishu(env, gateway, outbox)
    task = asyncio.create_task(gateway.run())
    try:
        await gateway.receive(fs.event(message, message_id="raw_md_markup"))
        await gateway.idle()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(files) == 1
    assert "报告正文" in files[0].decode() and "证据明细" not in files[0].decode()
    content = files[0].decode()
    assert source in content
    assert ("部分不支持的格式已按原文展示" in content) is notice
    assert "部分不支持的格式已移除" not in content
    tokens = MarkdownIt("commonmark", {"html": True}).enable("table").parse(content)
    forbidden = {"html_inline", "html_block", "link_open", "image"}
    assert not any(
        t.type in forbidden or any(c.type in forbidden for c in t.children or ()) for t in tokens
    )
    assert any(t.type == "inline" and t.content == "巡检发现" for t in tokens)
    assert any(t.type == "inline" and t.content == "后续建议" for t in tokens)
    detail_tokens = MarkdownIt().parse(files[0].decode())
    assert [
        json.loads(t.content) for t in detail_tokens if t.type == "fence" and t.info == "json"
    ] == [
        "east",
        100,
    ]
    assert "数据范围：" in files[0].decode()
    assert "private_note" not in content + files[0].decode()
    ref = RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_alice", request_id="raw_md_markup"
    )
    record = await env.store.get("feishu", "alice", "oc_alice", "raw_md_markup")
    before = await env.store.delivery_parts(record)
    assert record.state == "completed" and record.delivery == "failed"
    assert before[0].upload_state == "uploaded" and before[0].send_state == "failed"

    def cannot_render(*args, **kwargs):
        raise AssertionError("补发必须复用已绑定字节")

    monkeypatch.setattr("xiaowei.feishu_delivery.build_reports", cannot_render)

    assert (
        await env.service.results.send(
            ref,
            partial(
                send_delivery,
                send=send,
                upload_file=upload,
                chat_id="oc_alice",
                max_chars=2000,
                results=env.service.results,
                ref=ref,
                resend=True,
            ),
            resend=True,
        )
        == "sent"
    )
    after = await env.store.delivery_parts(record)
    assert [(p.kind, p.body, p.uuid, p.file_key) for p in after] == [
        (p.kind, p.body, p.uuid, p.file_key) for p in before
    ]
    assert all(p.send_state == "sent" for p in after)
    assert len(files) == 1
    assert (
        "报告正文" in files[0].decode()
        and "证据明细" not in files[0].decode()
        and len(env.adapter.calls) == 1
        and env.model_calls(message) == 2
    )


def test_legacy_html_bytes_match_the_main_baseline() -> None:
    # v0 bound reports recheck exact bytes; new prose CSS must not change their renderer.
    assert build_report(_delivery("html")).digest == (
        "a11be616a8027f2e786fe392b28febdc396401751838d4abc582f558ba6e52bb"
    )


def test_new_answer_has_trusted_report_semantics_without_new_model_fields() -> None:
    answer_ = AgentAnswer(
        blocks=(TextBlock(type="text", text=PROSE),),
        report_format="md",
        awaiting_user=False,
    )
    assert TurnAnswer(answer=answer_, context_evidence=()).answer_version == 4
    assert set(answer_.model_dump()) == {"blocks", "report_format", "awaiting_user"}


def test_native_markdown_uses_real_headings_and_tables() -> None:
    delivery = _delivery("md").model_copy(
        update={"blocks": (DeliveryText(text=PROSE),), "report_native": True}
    )
    content = build_report(delivery).content.decode()
    tokens = MarkdownIt("commonmark").enable("table").parse(content)
    assert any(token.type == "heading_open" and token.tag == "h1" for token in tokens[1:])
    assert not any(token.type == "fence" and PROSE in token.content for token in tokens)
    assert "模型生成，未经系统核实" in content


async def report_request(env: Env) -> tuple[RequestRef, str]:
    message = env.scripts.add(
        "查询东区并制作 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), PROSE, report_format="md"),
    )
    record = await env.run(message, "om_native", channel="feishu", conversation="oc_native")
    assert record.state == "completed", record.failure_code
    return RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_native", request_id="om_native"
    ), message


async def test_formal_report_success_and_original_byte_resend(env: Env) -> None:
    ref, message = await report_request(env)
    platform = Platform()
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "sent"
    assert len(platform.files) == 1
    tokens = MarkdownIt().parse(platform.files[0].decode())
    assert not any(t.type == "fence" and PROSE in t.content for t in tokens)
    assert "报告" in env.scripts.calls[message][-1].instructions
    before = (len(env.adapter.calls), env.model_calls(message))
    record = await env.store.get("feishu", "alice", "oc_native", "om_native")
    parts = await env.store.delivery_parts(record)
    assert parts and any(p.kind == "md" for p in parts)
    assert before == (1, 2)
    originals = tuple((p.kind, p.body, p.file_key) for p in parts)
    assert await env.service.results.send(ref, transmit(env, ref, platform), resend=True) is None
    restored = await env.store.delivery_parts(record)
    assert tuple((p.kind, p.body, p.file_key) for p in restored) == originals
    assert (len(env.adapter.calls), env.model_calls(message)) == before


async def test_revoked_evidence_prevents_report_io(env: Env) -> None:
    ref, message = await report_request(env)
    env.grants.revoke_all()
    platform = Platform()
    with pytest.raises((AccessDeniedError, ResultUnavailableError)):
        await env.service.results.send(ref, transmit(env, ref, platform))
    assert platform.files == [] and platform.sent == []
    assert len(env.adapter.calls) == 1 and env.model_calls(message) == 2


async def test_saved_v2_pending_report_keeps_literal_text(env: Env) -> None:
    ref, _ = await report_request(env)
    record = await env.store.get("feishu", "alice", "oc_native", "om_native")
    assert await env.store.delivery_parts(record) == ()
    async with env.engine.begin() as conn:
        raw = await conn.scalar(text("SELECT answer FROM xiaowei_request"))
        saved = json.loads(raw)
        saved["answer_version"] = 2
        await conn.execute(
            text("UPDATE xiaowei_request SET answer=:body"),
            {"body": json.dumps(saved, ensure_ascii=False)},
        )
    delivery = (await env.service.results.view(ref, first=True)).delivery
    assert delivery is not None
    assert not delivery.report_native
    tokens = MarkdownIt().parse(build_report(delivery).content.decode())
    assert any(t.type == "fence" and PROSE in t.content for t in tokens)


async def test_report_skill_without_starrocks_and_web_exclusion(env: Env) -> None:
    _, message = await report_request(env)
    instructions = env.scripts.calls[message][-1].instructions
    assert "HTML 中显示为文字" in instructions
    web_message = env.scripts.add("只解释", lambda call: answer([], "普通解释"))
    await env.run(web_message, "web_native", channel="web")
    assert "HTML 中显示为文字" not in env.scripts.calls[web_message][-1].instructions


async def test_awaiting_user_keeps_html_as_plain_message_without_file(env: Env) -> None:
    value = '<section style="color:red">请明确环境</section>'
    message = env.scripts.add(
        "报告缺环境",
        lambda call: answer(
            [],
            value,
            report_format="html",
            awaiting_user=True,
        ),
    )
    await env.run(message, "awaiting_native", channel="feishu", conversation="oc_d1a")
    ref = RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_d1a", request_id="awaiting_native"
    )
    delivery = (await env.service.results.view(ref, first=True)).delivery
    assert delivery is not None and not delivery.report_native and delivery.report_format is None
    platform = Platform()
    assert await env.service.results.send(ref, transmit(env, ref, platform)) == "sent"
    assert platform.files == []
    assert "请明确环境" in str(platform.sent)
    assert env.adapter.calls == [] and env.model_calls(message) == 1


@pytest.mark.parametrize("protocol", ["responses", "vertex"])
async def test_raw_feishu_truncation_is_terminal_for_each_provider(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, protocol: str
) -> None:
    caplog.set_level(logging.INFO, logger="xiaowei.app")
    requests = []
    payload = (
        _body([{"text": "partial-model-secret"}], finish="MAX_TOKENS")
        if protocol == "vertex"
        else {
            "id": "resp_limit",
            "object": "response",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
        }
    )

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json=payload)

    profile = VERTEX if protocol == "vertex" else env.binding.profile
    monkeypatch.setenv(profile.api_key_ref.removeprefix("env:"), "synthetic-truncation-key")
    async with open_model(profile, transport=httpx2.MockTransport(respond)) as binding:
        app = Application(
            app_config(),
            model=binding,
            engine=env.engine,
            governance=GovernedTools(env.evidence),
            local_tools={
                (TOTAL_TOOL, TARGET): env.adapter.execute,
                (QUERY_TOOL, TARGET): env.adapter.execute,
            },
            clock=env.clock,
        )
        current = replace(env, binding=binding, app=app)
        async with running(current) as fs:
            await fs.gateway.receive(fs.event("生成 MD 报告", message_id="raw_truncated"))
            await fs.drain()
        record = await current.store.get("feishu", "alice", "oc_alice", "raw_truncated")
        assert record.state == "failed" and record.failure_code == "model_failed"
        assert record.answer is None and await current.store.delivery_parts(record) == ()
    assert len(requests) == 1 and env.adapter.calls == []
    assert "模型未能完成本轮" in str(fs.outbox.sent)
    assert "partial-model-secret" not in str(fs.outbox.sent)
    assert "max_output_tokens" not in str(fs.outbox.sent)
    if protocol == "vertex":
        assert "finish_reason_max_tokens" in caplog.text
        assert requests[0].url.path.endswith(":generateContent")


async def test_previously_failed_unbound_md_can_be_resent_without_new_query(env: Env) -> None:
    message = env.scripts.add(
        "查询东区并制作 MD 报告",
        tool_call("order_total", region="east"),
        lambda call: answer(evidence_in(call), "建议 ARRAY<INT>", report_format="md"),
    )
    await env.run(message, "old_md_failure", channel="feishu", conversation="oc_d1a")
    ref = RequestRef(
        channel="feishu", subject_id="alice", conversation_id="oc_d1a", request_id="old_md_failure"
    )

    async def old_delivery_failure(delivery):
        # a56f07a stopped before binding any data parts; preserve that stored state.
        return "failed"

    assert await env.service.results.send(ref, old_delivery_failure) == "failed"
    record = await env.store.get("feishu", "alice", "oc_d1a", "old_md_failure")
    assert record.answer_version == 4 and await env.store.delivery_parts(record) == ()
    platform = Platform()
    assert (
        await env.service.results.send(ref, transmit(env, ref, platform, resend=True), resend=True)
        == "sent"
    )
    assert len(platform.files) == 1
    assert "ARRAY<INT>" in platform.files[0].decode()
    assert "部分不支持的格式已按原文展示" in platform.files[0].decode()
    assert len(env.adapter.calls) == 1 and env.model_calls(message) == 2


@pytest.mark.parametrize("case", ["md", "html", "unsafe"])
async def test_raw_feishu_entry_native_report_or_bounded_failure(env: Env, case: str) -> None:
    source = (
        "<script>upstream-secret</script>"
        if case == "unsafe"
        else '<h1 style="color:navy" onclick="bad()">巡检发现</h1><p>解释与建议</p>'
        if case == "html"
        else PROSE
    )
    message = env.scripts.add(
        "生成解释报告",
        lambda call: answer([], source, report_format="md" if case == "md" else "html"),
    )
    uploaded = []

    async def upload(report):
        uploaded.append(report.content)
        return "file_native_raw"

    outbox = Outbox()
    gateway = FeishuGateway(env.service, config(), outbox, clock=env.clock, upload_file=upload)
    fs = Feishu(env, gateway, outbox)
    task = asyncio.create_task(gateway.run())
    try:
        await gateway.receive(fs.event(message, message_id="raw_native_report"))
        await gateway.idle()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert env.adapter.calls == [] and env.model_calls(message) == 1
    if case == "unsafe":
        assert uploaded == []
        assert "报告正文" in str(outbox.sent) and "未交付" in str(outbox.sent)
        assert "请简化后重新提出" in str(outbox.sent)
        assert "手动补发" not in str(outbox.sent)
        assert "upstream-secret" not in str(outbox.sent)
    else:
        assert len(uploaded) == 1
        if case == "md":
            tokens = MarkdownIt().parse(uploaded[0].decode())
            assert not any(t.type == "fence" and PROSE in t.content for t in tokens)
        else:
            assert '<h1 style="color:navy">巡检发现</h1>' in uploaded[0].decode()
            assert "部分不支持的格式已移除" in uploaded[0].decode()
            assert "部分不支持的格式已按原文展示" not in uploaded[0].decode()
            assert "onclick" not in uploaded[0].decode()
            assert "style=" not in str(outbox.sent)


def test_html_preparation_is_reused_for_split_files_and_preview(monkeypatch) -> None:
    import nh3

    from xiaowei.feishu_delivery import _data_parts

    calls = []
    original_clean = nh3.clean

    def clean(value, **kwargs):
        calls.append(value)
        return original_clean(value, **kwargs)

    monkeypatch.setattr(nh3, "clean", clean)
    monkeypatch.setattr("xiaowei.feishu_report.MAX_REPORT_BYTES", 8500)
    delivery = _delivery("html")
    fact = delivery.facts[0].model_copy(
        update={
            "result_json": json.dumps(
                {
                    "columns": ["id", "value"],
                    "rows": [{"id": i, "value": "data" * 15} for i in range(120)],
                }
            )
        }
    )
    delivery = delivery.model_copy(
        update={
            "report_native": True,
            "facts": (fact,),
            "blocks": (
                DeliveryText(text='<h1 style="color: navy">结论</h1><p>正文</p>'),
                *delivery.blocks[1:],
            ),
        }
    )
    parts = _data_parts(delivery, 0, 2000, "oc_d1a", None)
    assert sum(p.kind == "html" for p in parts) > 1
    assert len(calls) == 1
    preview = json.loads(parts[-1].body)
    assert "结论" in str(preview) and "style=" not in str(preview)


@pytest.mark.parametrize(
    "content",
    [
        "![image](https://invalid.example/x)",
        "<script>x</script>",
        "[link](https://invalid.example)",
    ],
)
def test_native_markdown_keeps_unsupported_constructs_literal(content: str) -> None:
    delivery = _delivery("md").model_copy(
        update={"blocks": (DeliveryText(text=content),), "report_native": True}
    )
    body = build_report(delivery).content.decode()
    tokens = MarkdownIt("commonmark", {"html": True}).parse(body)
    assert any(t.type == "fence" and content in t.content for t in tokens)
    assert "部分不支持的格式已按原文展示" in body
    assert not any(
        t.type in {"html_block", "html_inline", "link_open", "image"}
        or any(c.type in {"html_inline", "link_open", "image"} for c in t.children or ())
        for t in tokens
    )


def test_native_markdown_closes_model_fence_before_fact() -> None:
    delivery = _delivery("md")
    delivery = delivery.model_copy(
        update={
            "blocks": (DeliveryText(text="~~~sql\nSELECT '<tag>'"), *delivery.blocks[1:]),
            "report_native": True,
        }
    )
    tokens = MarkdownIt().parse(build_report(delivery).content.decode())
    code = next(t for t in tokens if t.type == "fence")
    assert "SELECT '<tag>'" in code.content
    assert "来源" not in code.content


@pytest.mark.parametrize("reference", ["![leak][asset]", "[leak][asset]"])
def test_markdown_cannot_activate_a_reference_across_fact_blocks(reference: str) -> None:
    delivery = _delivery("md")
    delivery = delivery.model_copy(
        update={
            "blocks": (
                DeliveryText(text=reference),
                *delivery.blocks[1:],
                DeliveryText(
                    text="[asset]: https://invalid.example/leak\n"
                    "[asset]: https://invalid.example/duplicate"
                ),
            ),
            "report_native": True,
        }
    )
    body = build_report(delivery).content.decode()
    env_: dict = {}
    tokens = MarkdownIt("commonmark", {"html": True}).parse(body, env_)
    assert not env_.get("references")
    assert not any(c.type in {"image", "link_open"} for t in tokens for c in t.children or ())
    assert "部分不支持的格式已按原文展示" in body and "来源" in body
    assert "https://invalid.example/duplicate" in body


@pytest.mark.parametrize("closing", ["    ~~~", "\t~~~", "~~~\u2028a", "~~~\x0ba", "~~~\u00a0"])
def test_markdown_invalid_fence_closer_cannot_capture_fact_identity(closing: str) -> None:
    delivery = _delivery("md")
    delivery = delivery.model_copy(
        update={
            "blocks": (DeliveryText(text="~~~\n" + closing), *delivery.blocks[1:]),
            "report_native": True,
        }
    )
    tokens = MarkdownIt().parse(build_report(delivery).content.decode())
    assert any(t.type == "inline" and t.content == "2. 结果 1" for t in tokens)
    first_code = next(t for t in tokens if t.type == "fence")
    assert "来源" not in first_code.content and "SQL 原文" not in first_code.content


@pytest.mark.parametrize("legacy", [True, False])
async def test_pending_html_upgrade_retains_literal_or_native_semantics(
    env: Env, legacy: bool
) -> None:
    original = (
        "a<b & <table> 很慢" if legacy else "<h1>发现</h1><p>a&lt;b &amp; &lt;table&gt; 很慢</p>"
    )
    message = env.scripts.add(
        "解释并生成 HTML", lambda call: answer([], original, report_format="html")
    )
    await env.run(message, "upgrade_html", channel="feishu", conversation="oc_upgrade")
    if legacy:
        async with env.engine.begin() as conn:
            raw = await conn.scalar(text("SELECT answer FROM xiaowei_request"))
            saved = json.loads(raw)
            saved["answer_version"] = 2
            await conn.execute(
                text("UPDATE xiaowei_request SET answer=:body"), {"body": json.dumps(saved)}
            )
    ref = RequestRef(
        channel="feishu",
        subject_id="alice",
        conversation_id="oc_upgrade",
        request_id="upgrade_html",
    )
    delivery = (await env.service.results.view(ref, first=True)).delivery
    assert delivery is not None and delivery.report_native is not legacy
    content = build_report(delivery).content.decode()
    assert ("<h1>发现</h1>" in content) is not legacy
    assert "a&lt;b &amp; &lt;table&gt; 很慢" in content
