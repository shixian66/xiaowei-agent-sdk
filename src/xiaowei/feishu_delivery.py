"""D1a 完整载体选择与按原字节手动恢复；权限快照由 ResultDelivery 提供。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING, Literal

from lark_channel.channel.types import MediaSource, OutboundFile, SendResult

from xiaowei.channel_store import DeliveryClaim, DeliveryPart, SendOutcome
from xiaowei.feishu_render import (
    MAX_REQUEST_BYTES,
    Message,
    MessageCapacityError,
    build_feishu_message,
    capacity_notice,
    fact_limit_notices,
    request_bytes,
)
from xiaowei.feishu_report import (
    MAX_REPORT_BYTES,
    ReportCapacityError,
    ReportFile,
    build_reports,
    prepare_prose,
)
from xiaowei.models import Delivery, DeliveryText
from xiaowei.report_content import ReportContentError

if TYPE_CHECKING:
    from xiaowei.channel import RequestRef, ResultDelivery
    from xiaowei.feishu import Send, UploadFile

logger = logging.getLogger(__name__)
# Lark 官方消息 FAQ 对单群发送列出 5 QPS；只约束多文件消息的连续发送。
_GROUP_FILE_SEND_INTERVAL = 0.21


def _message_part(
    sequence: int, kind: Literal["body", "approval"], message: Message
) -> DeliveryPart:
    if kind not in ("body", "approval"):
        raise ValueError("消息类型无效")
    return DeliveryPart(sequence, kind, json.dumps(message, ensure_ascii=False).encode())


def _preview(
    delivery: Delivery,
    max_chars: int,
    chat_id: str,
    reply_to: str | None,
    notice: str,
    *,
    attachment: bool = False,
) -> Message:
    # 复用渲染器的整行预览：回答优先，预览范围与“结果行未展示”出自同一预算，不先截事实。
    # 交付说明是代码说明，放在系统说明区，不与模型文字拼进同一段。
    candidate = delivery.model_copy(
        update={"report_notice": notice + ("\n结果预览。" if delivery.facts else "")}
    )
    try:
        message = build_feishu_message(
            candidate,
            max_chars,
            chat_id,
            reply_to=reply_to,
            platform_limits=True,
            attachment_preview=attachment,
        )
    except (MessageCapacityError, ValueError):
        message = None
    if message is not None and message != capacity_notice(
        delivery.session_notice, notification_notice=delivery.notification_notice
    ):
        return message
    if delivery.section("action"):
        # 动作区域不进入附件，放不进正文就明确失败，不以说明代替。
        raise MessageCapacityError("动作区域无法在本消息容量内完整展示")
    text = notice + "\n预览无法在本消息容量内展示。"
    limits = list(
        dict.fromkeys(
            [n for f in delivery.facts for n in fact_limit_notices(f)]
            + [
                n
                for n in (
                    delivery.monitoring_notice,
                    delivery.session_notice,
                    delivery.notification_notice,
                )
                if n
            ]
        )
    )
    message = {"text": text + ("\n" + "\n".join(limits) if limits else "")}
    if (
        len(message["text"]) > max_chars
        or request_bytes(message, chat_id, reply_to=reply_to) > MAX_REQUEST_BYTES
    ):
        raise MessageCapacityError("容量说明无法完整展示")
    return message


def _data_parts(
    delivery: Delivery, sequence: int, max_chars: int, chat_id: str, reply_to: str | None
) -> tuple[DeliveryPart, ...]:
    if delivery.report_format is None or delivery.awaiting_user:
        try:
            message = build_feishu_message(
                delivery,
                max_chars,
                chat_id,
                reply_to=reply_to,
                allow_preview=False,
                platform_limits=True,
            )
            return (_message_part(sequence, "body", message),)
        except MessageCapacityError:
            if delivery.awaiting_user:
                raise
    prose = prepare_prose(delivery)
    reports = build_reports(
        delivery.model_copy(update={"report_format": delivery.report_format or "md"}),
        expand_brief=delivery.report_format is not None,
        prose=prose,
    )
    preview = delivery
    if prose:
        texts = iter(prose)
        preview = delivery.model_copy(
            update={
                "blocks": tuple(
                    b.model_copy(update={"text": next(texts).text})
                    if isinstance(b, DeliveryText)
                    else b
                    for b in delivery.blocks
                )
            }
        )
    # 用途写入同一绑定正文与文件内标题；不新增持久化 filename/role 字段。
    file_index = "；".join(
        f"文件 {i}：{'证据明细' if report.filename.startswith('xiaowei-details') else '报告正文'}"
        for i, report in enumerate(reports, 1)
    )
    body = _preview(
        preview,
        max_chars,
        chat_id,
        reply_to,
        f"本次完整结果见附件。{file_index}。",
        attachment=True,
    )
    parts = [
        DeliveryPart(sequence + index, report.format, report.content)
        for index, report in enumerate(reports)
    ]
    parts.append(_message_part(sequence + len(reports), "body", body))
    return tuple(parts)


def _check_capacity(
    parts: tuple[DeliveryPart, ...],
    max_chars: int,
    chat_id: str,
    reply_to: str | None,
    *,
    platform_limits: bool,
) -> None:
    for p in parts:
        if p.send_state == "sent":
            continue
        if p.kind in ("md", "html"):
            if len(p.body) > MAX_REPORT_BYTES:
                raise ReportCapacityError("容量不足，不能重发")
        else:
            message = json.loads(p.body)
            # 使用原卡片的容量规则，绝不按新模板重建/逐字节比较。
            from xiaowei.feishu_render import _chars, _elements

            # 数据分项按平台限制：只有纯文本受字符上限；审批材料沿原规则。
            limit_chars = not platform_limits or set(message) == {"text"}
            if (
                (limit_chars and _chars(message) > max_chars)
                or _elements(message) > 200
                or request_bytes(message, chat_id, reply_to=reply_to) > MAX_REQUEST_BYTES
            ):
                raise MessageCapacityError("容量不足，不能重发")


async def send_complete_delivery(
    delivery: Delivery,
    *,
    results: ResultDelivery,
    ref: RequestRef,
    send: Send,
    upload_file: UploadFile | None,
    chat_id: str,
    max_chars: int,
    reply_to: str | None,
    resend: bool,
    notice: Callable[[str], None] | None = None,
) -> SendOutcome:
    from xiaowei.feishu import send_outcome

    store = results.store
    record = await store.get(
        ref.channel,
        ref.subject_id,
        ref.conversation_id,
        ref.request_id,
        owner=ref.group.owner if ref.group else None,
    )
    if delivery.delivery_attempt is None:
        raise ValueError("投递未取得尝试权")
    claim = DeliveryClaim(record, delivery.delivery_attempt, delivery.content_expires_at)
    existing = await store.delivery_parts(record)
    materials = tuple(f for f in delivery.facts if f.requires_complete)
    data = tuple(f for f in delivery.facts if not f.requires_complete)
    material_parts: tuple[DeliveryPart, ...] = ()
    data_parts = tuple(p for p in existing if p.kind != "approval")
    capacity_failed = False
    material_failed = False
    last_group_send_at: float | None = None
    file_count = 0

    def capacity_refusal() -> Literal["failed"]:
        logger.warning("容量不足，不能重发")
        if notice is not None:
            notice("容量不足，不能重发")
        return "failed"

    async def pace_group_send(file_count: int) -> None:
        if ref.group is not None and file_count > 1 and last_group_send_at is not None:
            delay = last_group_send_at + _GROUP_FILE_SEND_INTERVAL - monotonic()
            await asyncio.sleep(max(0, delay))

    if materials and not any(p.kind == "approval" for p in existing):
        material = delivery.model_copy(
            update={
                "facts": materials,
                "blocks": (),
                "report_format": None,
                "report_notice": None,
            }
        )
        try:
            message = build_feishu_message(
                material, max_chars, chat_id, reply_to=reply_to, allow_preview=False
            )
        except MessageCapacityError:
            if resend:
                return capacity_refusal()
            material_failed = True
        else:
            material_parts = (_message_part(0, "approval", message),)
    # 审批材料独立发送后，剩余正文（模型文字、其余结果与系统说明）照常分区渲染；
    # 编号沿用整条交付的展示顺序。
    data_delivery = delivery.model_copy(update={"facts": data, "requires_complete": False})

    async def hint(reason: str, recovery: str = "请联系操作员手动补发；不会重新查询。") -> None:
        # 提醒尽力一次，非必需分项；失锁/到期/readiness关闭不再外发。
        nonlocal last_group_send_at
        await pace_group_send(file_count)
        await store.check_delivery_attempt(claim)
        try:
            hint_delivery = data_delivery
            if not data or data_delivery.report_native:
                # 提醒是代码说明：不带模型文字，只用纯文本或系统说明。
                hint_delivery = data_delivery.model_copy(update={"content": "", "blocks": ()})
            preview = await asyncio.to_thread(
                _preview,
                hint_delivery,
                max_chars,
                chat_id,
                reply_to,
                "完整结果尚未交付。" + reason + f"原请求编号：{ref.request_id}。" + recovery,
            )
            # 线程渲染期间 readiness 可能关闭、请求可能过期：发送前再复核一次。
            await store.check_delivery_attempt(claim)
            if ref.group is not None:
                last_group_send_at = monotonic()
            await send(chat_id, preview, reply_to=reply_to)
        except Exception:
            logger.warning("未完整交付的提醒未送达")

    async def send_part(
        part: DeliveryPart, *, file_index: int = 0, file_count: int = 0
    ) -> SendOutcome:
        nonlocal last_group_send_at
        if part.send_state == "sent":
            return "sent"
        if (
            part.kind == "approval"
            and delivery.request_state == "paused"
            and not any("approval_command" in f.metadata for f in materials)
        ):
            return "failed"
        key = part.file_key
        if part.kind in ("md", "html"):
            filename = (
                f"xiaowei-report-{file_index}-of-{file_count}.{part.kind}"
                if file_count > 1
                else f"xiaowei-report.{part.kind}"
            )
            if part.upload_state in ("unknown", "uploading"):
                return "unknown"
            if part.upload_state == "pending":
                if upload_file is None:
                    return "failed"
                if not await store.begin_part_upload(claim, part):
                    return "unknown"
                report = ReportFile(
                    part.kind,
                    filename,
                    part.body,
                    hashlib.sha256(part.body).hexdigest(),
                )
                try:
                    key = await upload_file(report)
                    if not isinstance(key, str) or not key:
                        raise ValueError("上传未返回文件键")
                except Exception:
                    await store.finish_part_upload(claim, part, None)
                    return "unknown"
                await store.finish_part_upload(claim, part, key)
            outbound: Message | OutboundFile = OutboundFile(
                source=MediaSource(kind="key", key=key), file_name=filename
            )
        else:
            outbound = json.loads(part.body)
        await pace_group_send(file_count)
        if not await store.begin_part_send(claim, part):
            return "unknown"
        if ref.group is not None:
            last_group_send_at = monotonic()
        try:
            receipt = await send(chat_id, outbound, reply_to=reply_to, uuid=part.uuid)
            outcome = send_outcome(receipt)
            message_id = receipt.message_id if isinstance(receipt, SendResult) else None
        except Exception:
            outcome, message_id = "unknown", None
        return await store.finish_part_send(claim, part, outcome, message_id)

    outcomes: list[SendOutcome] = ["failed"] if material_failed else []
    if material_parts:
        existing = await store.bind_delivery_parts(claim, material_parts)
    approvals = tuple(p for p in existing if p.kind == "approval")
    # C7：首次和补发都先独立交付材料；数据容量不能阻断它，材料自身仍须合容量。
    try:
        _check_capacity(approvals, max_chars, chat_id, reply_to, platform_limits=False)
    except (ReportCapacityError, MessageCapacityError):
        return capacity_refusal()
    outcomes.extend([await send_part(p) for p in approvals])
    needs_data = bool(data or delivery.texts or not materials)
    try:
        if needs_data and not data_parts:
            data_parts = await asyncio.to_thread(
                _data_parts, data_delivery, 1 if materials else 0, max_chars, chat_id, reply_to
            )
        _check_capacity(data_parts, max_chars, chat_id, reply_to, platform_limits=True)
    except ReportContentError:
        await hint("报告正文不符合静态格式要求。", recovery="请简化后重新提出。")
        return "failed"
    except (ReportCapacityError, MessageCapacityError):
        capacity_failed = True
        if resend:
            return capacity_refusal()
    except (ValueError, TypeError):
        logger.error("飞书结果未发送：reason=render_failed")
        if not materials:
            return "failed"
        capacity_failed = True
    parts = await store.bind_delivery_parts(claim, () if capacity_failed else data_parts)
    file_count = sum(p.kind in ("md", "html") for p in parts)
    file_index = 0
    for part in parts:
        if part.kind == "approval":
            continue
        if part.kind in ("md", "html"):
            file_index += 1
        if part.kind == "body":
            current = await store.delivery_parts(record)
            if any(p.kind in ("md", "html") and p.send_state != "sent" for p in current):
                continue
        outcomes.append(await send_part(part, file_index=file_index, file_count=file_count))
    if capacity_failed or any(o != "sent" for o in outcomes):
        current = await store.delivery_parts(record)
        failures = [
            f"第{p.sequence + 1}部分"
            + ("发送失败。" if p.send_state == "failed" else "发送结果不明。")
            for p in current
            if p.send_state in ("failed", "unknown")
        ]
        if any(p.upload_state in ("unknown", "uploading") for p in current):
            failures.append("附件上传结果不明，禁止重传。")
        material_notice = (
            "审批材料容量不足。" if delivery.request_state == "paused" else "动作事实消息容量不足。"
        )
        reason = (material_notice if material_failed else "") + (
            "文件容量不足。" if capacity_failed else "".join(failures) or "附件或消息尚未确认送达。"
        )
        await hint(reason)
        return "unknown" if "unknown" in outcomes else "failed"
    return "sent"
