"""R12 的公开卡片、格式白名单与正式飞书入口回归；不证明真实客户端显示。"""

import dataclasses
import html
import json
from typing import Any

import pytest
from lark_channel.channel import FeishuChannel
from tests.sdk_core.test_app import answer, clarify
from tests.sdk_core.test_channel_service import Env
from tests.sdk_core.test_channel_service import env as env  # pytest fixture
from tests.sdk_core.test_feishu import Outbox, config, request_row, running
from tests.sdk_core.test_feishu_render import AT, delivery, fact, size, tables, text_parts

from xiaowei.feishu import LarkTransport, lark_channel, send_delivery
from xiaowei.feishu_render import TEXT_CLIPPED, build_feishu_message, request_bytes
from xiaowei.models import Delivery

pytestmark = pytest.mark.loopback


def markdowns(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [node for child in value for node in markdowns(child)]
    if not isinstance(value, dict):
        return []
    if value.get("tag") == "markdown":
        return [value]
    return [node for child in value.values() for node in markdowns(child)]


def prose(kind: str, text: str) -> Delivery:
    return Delivery(
        channel="feishu", evidence_ids=(), content=kind + "\n旧转义内容", feishu_text=text
    )


@pytest.mark.parametrize("kind", ["建议", "需要澄清"])
def test_business_prose_is_one_card_with_real_newlines(kind: str) -> None:
    message = build_feishu_message(prose(kind, "第一行\n第二行"), 3500, "oc_test")
    assert set(message) == {"card"}
    shown = "\n".join(text_parts(message))
    assert "第一行\n第二行" in shown and "旧转义内容" not in shown
    assert kind in shown
    if kind == "建议":
        assert "未经系统核实" in shown


def test_approved_headings_bold_and_lists_are_generated_not_copied() -> None:
    text = "### 关键结论\n\n**主键** 清楚。\n\n- 第一点\n- 第二点\n\n1. 检查列\n2. 看布局"
    message = build_feishu_message(delivery(fact(), analysis=text), 3500, "oc_test")
    nodes = markdowns(message)
    assert nodes, "分析应使用受限格式，不再原样显示 Markdown 符号"
    contents = "\n".join(node["content"] for node in nodes)
    assert "**关键结论**" in contents and "**主键**" in contents
    assert "- 第一点" in contents and "1. 检查列" in contents
    shown = "\n".join(text_parts(message, folded=False))
    assert "依据：ev_original" in shown and fact().note in shown
    assert AT.isoformat() in shown
    assert len(tables(message)) == 1


@pytest.mark.parametrize("identifier", ["__name__", "__DORIS_DELETE_SIGN__", "__init__"])
@pytest.mark.parametrize("has_facts", [False, True], ids=["advice", "fact_analysis"])
@pytest.mark.parametrize(
    ("raw_template", "shown_template", "formatted"),
    [
        ("按 {identifier} 过滤", "按 {identifier} 过滤", False),
        ("**重点**：按 {identifier} 过滤", "**重点**：按 {identifier} 过滤", True),
        ("**按 {identifier} 过滤**", "**按 {identifier} 过滤**", True),
        ("- 按 {identifier} 过滤", "- 按 {identifier} 过滤", True),
    ],
    ids=["plain", "alongside_bold", "inside_bold", "list"],
)
def test_double_underscore_identifiers_keep_literal_spelling(
    identifier: str,
    has_facts: bool,
    raw_template: str,
    shown_template: str,
    formatted: bool,
) -> None:
    raw = raw_template.format(identifier=identifier)
    source = delivery(fact(), analysis=raw) if has_facts else prose("建议", raw)
    message = build_feishu_message(source, 3500, "oc_test")
    nodes = markdowns(message)
    assert bool(nodes) is formatted
    if formatted:
        # 解码实体后只允许已有的 ** 加粗标记，双下划线必须仍属于标识符原值。
        shown = html.unescape("\n".join(node["content"] for node in nodes))
        assert shown == shown_template.format(identifier=identifier)
    else:
        expected = f"- {raw}（依据：ev_original）" if has_facts else raw
        assert expected in text_parts(message)


@pytest.mark.parametrize(
    "text",
    [
        '<at id="all"></at> **关键**',
        "[伪装](https://evil.invalid/path) **关键**",
        "![图片](img_key) **关键**",
        '<font color="red">标签</font> **关键**',
        "<script>alert(1)</script> **关键**",
        "https://evil.invalid :DONE: **关键**",
        "&#60;at id=all&#62; &lt;at id=all&gt; **关键**",
    ],
)
def test_non_whitelisted_syntax_is_literal_text(text: str) -> None:
    message = build_feishu_message(prose("建议", text), 3500, "oc_test")
    assert "card" in message
    contents = "\n".join(node["content"] for node in markdowns(message))
    assert "<at" not in contents and "<font" not in contents and "<script" not in contents
    assert "](https" not in contents and "](img_key" not in contents
    assert "https://" not in contents and ":DONE:" not in contents
    # 仅检查允许组件；从未生成链接、图片、@、操作行为或卡片 URL。
    serialized = json.dumps(message, ensure_ascii=False)
    for structural in ('"tag": "at"', '"tag": "img"', '"url":', '"behaviors":', '"card_link":'):
        assert structural not in serialized
    # 从格式组件解码一次推导显示文字，不能拿原样序列化内容证明已转义。
    visible_literal = html.unescape(contents)
    assert markdowns(message), "反例必须到达格式分支，不能只证明纯文本安全"
    assert text.replace("**", "") in visible_literal.replace("**", "")


def test_sql_ddl_and_fact_values_are_not_parsed_as_model_markdown() -> None:
    raw = 'CREATE TABLE t (v STRING COMMENT "```<at id=all>");\n-- [x](https://evil.invalid)'
    ddl = fact(tool_id="local/show_create_table", columns=("ddl",), rows=({"ddl": raw},))
    message = build_feishu_message(delivery(ddl, analysis="**原文已返回**"), 3500, "oc_test")
    assert raw in text_parts(message, folded=False)
    assert all(raw not in n["content"] for n in markdowns(message))
    sql = 'SELECT "```<at id=all>" FROM t LIMIT 5'
    message = build_feishu_message(delivery(fact(metadata={"sql": sql})), 3500, "oc_test")
    assert "实际 SQL:\n" + sql in text_parts(message)


@pytest.mark.parametrize("fence", ["```", "````````", "~~~"])
def test_code_drafts_fall_back_to_plain_text_without_fence_escape(fence: str) -> None:
    code = 'SELECT "<at id=all> **bold** [x](https://evil.invalid)";'
    message = build_feishu_message(
        prose("建议", fence + "sql\n" + code + "\n" + fence), 3500, "oc_test"
    )
    assert "card" in message
    assert code in "\n".join(text_parts(message))
    assert all(code not in n["content"] for n in markdowns(message))


@pytest.mark.parametrize(
    "raw",
    [
        "**草稿**\n`SELECT 'first\nsecond';`",
        "**草稿**\n` SELECT 1 `",
        "**草稿**\n``  SELECT `x`  ``",
        "**SQL草稿**\nSELECT '\\_%' AS pattern",
        "**路径** C:\\tmp\\log",
        "**草稿**\n```sql\nSELECT  1;\n```",
        "**草稿**\n\n    SELECT  1\n",
    ],
)
def test_code_and_backslash_prose_keep_exact_original_as_plain_text(raw: str) -> None:
    message = build_feishu_message(prose("建议", raw), 3500, "oc_test")
    assert markdowns(message) == []
    assert raw in text_parts(message)


@pytest.mark.parametrize("model_text", ["**检查采集端**", "**草稿**\n```sql\nSELECT 1;\n```"])
def test_monitoring_notice_stays_plain_when_model_prose_has_format(model_text: str) -> None:
    notice = "未核实监控源（系统记录）：prometheus-prod（上游认证或授权失败）。"
    source = Delivery(
        channel="feishu",
        evidence_ids=(),
        content="排查建议（监控读取失败；模型生成，未经数据验证）\n旧转义内容",
        feishu_text=notice + "\n" + model_text,
        monitoring_notice=notice,
    )
    message = build_feishu_message(source, 3500, "oc_test")
    elements = message["card"]["body"]["elements"]
    assert {"tag": "div", "text": {"tag": "plain_text", "content": notice}} in elements
    assert all("prometheus" not in node["content"] for node in markdowns(message))
    assert "旧转义内容" not in "\n".join(text_parts(message))


@pytest.mark.parametrize("max_chars", [200, 3500])
def test_monitoring_notice_is_never_clipped_with_model_prose(max_chars: int) -> None:
    notice = "未核实监控源（系统记录）：prometheus-prod（上游认证或授权失败）。"
    source = Delivery(
        channel="feishu",
        evidence_ids=(),
        content="排查建议（监控读取失败；模型生成，未经数据验证）\n旧转义内容",
        feishu_text=notice + "\n" + "**检查采集端**\n" * 1200,
        monitoring_notice=notice,
    )
    message = build_feishu_message(source, max_chars, "oc_test")
    assert notice in text_parts(message) and TEXT_CLIPPED in text_parts(message)
    assert all("prometheus" not in n["content"] for n in markdowns(message))
    assert sum(map(len, text_parts(message))) <= max_chars and size(message) <= 30_000


def test_controls_and_unclosed_syntax_cannot_make_platform_tags() -> None:
    raw = "**没有结束 [x](url)\n\u202e\u200b<at id=all>"
    message = build_feishu_message(prose("需要澄清", raw), 3500, "oc_test")
    shown = json.dumps(message, ensure_ascii=False)
    assert "\u202e" not in shown and "\u200b" not in shown
    assert "\\u202e" in shown and "\\u200b" in shown
    assert all("<at" not in n["content"] for n in markdowns(message))


@pytest.mark.parametrize("reply_to", [None, "om_original"])
def test_prose_card_two_envelope_budgets_and_truncation(reply_to: str | None) -> None:
    source = prose("建议", '**重点** "\\引用"\n' * 1200)
    message = build_feishu_message(source, 200, "oc_test", reply_to=reply_to, max_bytes=1500)
    assert "card" in message
    assert TEXT_CLIPPED in "\n".join(text_parts(message))
    # 文字/格式符和实体都保守计入字符预算，实际外层 JSON 字节独立计算。
    chars = sum(map(len, text_parts(message)))
    assert chars <= 200
    assert size(message, reply=reply_to) <= 1500
    assert request_bytes(message, "oc_test", reply_to=reply_to) == size(message, reply=reply_to)


@pytest.mark.parametrize("kind", ["建议", "需要澄清"])
async def test_gateway_model_business_reply_is_card_and_not_a_query(env: Env, kind: str) -> None:
    text = "**先确认目标**\n请提供集群。"
    step = clarify(text) if kind == "需要澄清" else lambda call: answer((), "", advice=text)
    message = env.scripts.add("只问候或澄清，不查询", step)
    async with running(env) as fs:
        event = fs.event(message)
        await fs.gateway.receive(event)
        await fs.drain()
        await fs.gateway.receive(event)
        await fs.drain()
    assert await request_row(env) == ("completed", "sent")
    assert len(fs.outbox.sent) == 1 and env.model_calls(message) == 1
    assert env.adapter.calls == []
    assert "card" in fs.outbox.sent[0][1]
    assert "**先确认目标**" in "\n".join(n["content"] for n in markdowns(fs.outbox.sent[0][1]))


async def test_gateway_prose_render_error_is_failed_without_send_or_lock(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("RENDER_CANARY https://private-host.invalid")

    monkeypatch.setattr("xiaowei.feishu_delivery.build_feishu_message", broken)
    message = env.scripts.add("需要确认", clarify("**确认集群**"))
    async with running(env) as fs:
        event = fs.event(message)
        await fs.gateway.receive(event)
        await fs.drain()
        await fs.gateway.receive(event)
        await fs.drain()
    assert await request_row(env) == ("completed", "failed")
    assert env.adapter.calls == [] and fs.outbox.sent == [] and env.readiness.ok
    assert env.model_calls(message) == 1 and "reason=render_failed" in caplog.text
    assert "RENDER_CANARY" not in caplog.text and "private-host" not in caplog.text


@pytest.mark.parametrize("reply_to", [None, "om_original"])
@pytest.mark.parametrize("kind", ["建议", "需要澄清", "SQL草稿", "事实"])
async def test_product_sender_and_real_sdk_send_one_bounded_business_card(
    monkeypatch: pytest.MonkeyPatch, reply_to: str | None, kind: str
) -> None:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route
    from tests.sdk_core.mcp_fixture import serve

    captured: list[tuple[str, bytes]] = []

    async def token(request: Request) -> Response:
        return JSONResponse({"code": 0, "tenant_access_token": "t-synthetic", "expire": 7200})

    async def sent(request: Request) -> Response:
        captured.append((request.url.path, await request.body()))
        return JSONResponse({"code": 0, "data": {"message_id": "om_format"}})

    def endpoint(_: Any) -> Any:
        return Starlette(
            routes=[
                Route("/open-apis/auth/v3/tenant_access_token/internal", token, methods=["POST"]),
                Route("/open-apis/im/v1/messages", sent, methods=["POST"]),
                Route("/open-apis/im/v1/messages/{message_id}/reply", sent, methods=["POST"]),
            ]
        )

    source = (
        delivery(fact(), analysis="**关键**\n结论")
        if kind == "事实"
        else prose(
            kind,
            "**关键**\n结论"
            + ('\n\n``````sql\nSELECT "<at id=all> ```";\n``````' if kind == "SQL草稿" else ""),
        )
    )
    monkeypatch.setenv("XW_TEST_FEISHU_SECRET", "not-a-real-secret")
    with serve(endpoint, path="") as http:
        channel = FeishuChannel(
            config=dataclasses.replace(lark_channel(config()).config, domain=http.url)
        )
        try:
            outcome = await send_delivery(
                source,
                send=LarkTransport(channel, config()).send,
                chat_id="oc_alice",
                max_chars=3500,
                reply_to=reply_to,
            )
        finally:
            channel.stop(join_timeout=5)
    assert outcome == "sent"
    ((path, raw),) = captured
    assert path == (
        f"/open-apis/im/v1/messages/{reply_to}/reply" if reply_to else "/open-apis/im/v1/messages"
    )
    body = json.loads(raw)
    assert body["msg_type"] == "interactive"
    message = {"card": json.loads(body["content"])}
    assert message["card"]["schema"] == "2.0"
    assert len(raw) == request_bytes(message, "oc_alice", reply_to=reply_to) <= 30_000
    if kind == "SQL草稿":
        # 按已批准的保真回退，含代码的说明整段原文纯文字，仍只发送一张业务卡片。
        assert markdowns(message) == [] and source.feishu_text in text_parts(message)
    else:
        assert "**关键**" in "\n".join(n["content"] for n in markdowns(message))


@pytest.mark.parametrize(
    "sample",
    [
        "**结论**\n\n*斜体不启用* [x](url) ![x](img) <at id=all>",
        "**结论**\n\n|name|value|\n|---|---|\n|a|b|",
        "**结论**\n\n" + "`" * 30 + "sql\n<at id=all>\n" + "`" * 30,
        "**结论**\n\n" + ("  " * 80) + "- 深层项\n",
        "**结论** " + "[" * 600 + "文本" + "]" * 600,
        "**结论**\n\\**字面粗体\\** &amp;lt;at&amp;gt;",
    ],
)
def test_complex_format_does_not_enable_other_components(sample: str) -> None:
    message = build_feishu_message(prose("建议", sample), 3500, "oc_test")
    assert "card" in message and size(message) <= 30_000
    assert sum(map(len, text_parts(message))) <= 3500
    elements = message["card"]["body"]["elements"]
    assert all(e["tag"] in {"div", "markdown"} for e in elements)
    for node in markdowns(message):
        assert not any(v in node["content"] for v in ("<at", "](url)", "](img)", "|---|", "<font"))


def test_formatting_is_outside_capacity_search_and_input_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xiaowei import feishu_markdown

    parse = feishu_markdown._PARSER.parse
    inputs: list[str] = []

    def observed(text: str, *args: Any, **kwargs: Any) -> Any:
        inputs.append(text)
        tokens = parse(text, *args, **kwargs)
        assert sum(1 + len(t.children or ()) for t in tokens) <= 4 * len(text) + 16
        return tokens

    monkeypatch.setattr(feishu_markdown._PARSER, "parse", observed)
    sources = tuple(
        fact(evidence_id=f"ev_{i}", rows=tuple({"name": n} for n in range(400))) for i in range(5)
    )
    message = build_feishu_message(delivery(*sources, analysis="**结论** " * 200), 3500, "oc_test")
    assert "card" in message
    assert len(inputs) <= 1 and all(len(text) <= 3500 for text in inputs)
    inputs.clear()
    message = build_feishu_message(prose("建议", "**结论** " * 1000), 3500, "oc_test")
    assert "card" in message and len(inputs) == 1 and len(inputs[0]) <= 3500
    # 解析器不处理超出既有展示上限的文字；回退不删改原文。
    inputs.clear()
    raw = "x" * 3501
    assert feishu_markdown.format_prose(raw) == [
        {"tag": "div", "text": {"tag": "plain_text", "content": raw}}
    ]
    assert inputs == []


def test_format_expansion_keeps_plain_card_when_capacity_would_be_exceeded() -> None:
    source = prose("建议", "**结论** [x](https://evil.invalid)")
    formatted = build_feishu_message(source, 3500, "oc_test")
    assert markdowns(formatted)
    # 格式实体膨胀不能挤掉文字、伪造截断或超容量；卡片退回纯文字且仍为单消息。
    plain_chars = (
        len("小维 · 回复")
        + len("建议（未执行查询，模型生成，未经系统核实）")
        + len(source.feishu_text)
    )
    limited = build_feishu_message(source, plain_chars, "oc_test")
    assert "card" in limited and markdowns(limited) == []
    assert source.feishu_text in text_parts(limited) and TEXT_CLIPPED not in text_parts(limited)
    assert sum(map(len, text_parts(limited))) == plain_chars


@pytest.mark.parametrize("outcome", ["failed", "unknown", RuntimeError("SDK_CANARY")])
async def test_prose_delivery_does_not_fallback_or_retry_after_sending(
    env: Env, outcome: Any
) -> None:
    message = env.scripts.add("只澄清", clarify("**确认集群**"))
    outbox = Outbox([outcome])
    async with running(env, outbox) as fs:
        event = fs.event(message)
        await fs.gateway.receive(event)
        await fs.drain()
        await fs.gateway.receive(event)
        await fs.drain()
    assert len(outbox.sent) == 2 and "card" in outbox.sent[0][1]
    assert "完整结果尚未交付" in outbox.texts()[-1]
    assert env.model_calls(message) == 1 and env.adapter.calls == []
    assert await request_row(env) == ("completed", "failed" if outcome == "failed" else "unknown")
