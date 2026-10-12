"""E5b 原图只在当前获准会话中保存、回放。"""

from datetime import timedelta

import pytest
from sqlalchemy import text
from tests.sdk_core.test_channel_service import Env
from tests.sdk_core.test_channel_service import env as env

from xiaowei.image_input import (
    ImageCapacityError,
    ImageUnavailableError,
    SessionImageStore,
    ValidatedImage,
)
from xiaowei.models import Identity
from xiaowei.session import cleanup_expired

pytestmark = pytest.mark.loopback


async def test_image_store_replays_original_in_order_and_limits_session(env: Env) -> None:
    images = SessionImageStore(env.engine, clock=env.clock)
    session = await env.store.current_session("feishu", "alice", "oc_alice")
    first = ValidatedImage(b"PNG-one", "image/png", 1, 1)
    second = ValidatedImage(b"JPG-two", "image/jpeg", 1, 1)
    for n, image in ((1, first), (2, second)):
        receipt = await env.service.accept(
            env.inbound(f"image {n}", f"image-{n}", channel="feishu", conversation="oc_alice")
        )
        await images.save(session, f"source-{n}", image, turn_id=receipt.record.turn_id)
        # 本单元只读请求终态；真实完成与模型回放另由正式入口用例覆盖。
        async with env.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE xiaowei_request SET state = 'completed', "
                    "answer = '{}' WHERE turn_id = :id"
                ),
                {"id": receipt.record.turn_id},
            )
    identity = Identity(
        subject_id="alice", session_id=session.session_id, turn_id="turn", channel="feishu"
    )
    assert [(i.source_digest, i.data) for i in await images.load(identity)] == [
        ("source-1", b"PNG-one"),
        ("source-2", b"JPG-two"),
    ]
    for n in range(3, 13):
        receipt = await env.service.accept(
            env.inbound(f"image {n}", f"image-{n}", channel="feishu", conversation="oc_alice")
        )
        await images.save(session, f"source-{n}", first, turn_id=receipt.record.turn_id)
    assert not await images.has_capacity(session)
    thirteenth = await env.service.accept(
        env.inbound("image 13", "image-13", channel="feishu", conversation="oc_alice")
    )
    with pytest.raises(ImageCapacityError):
        await images.save(session, "thirteenth", second, turn_id=thirteenth.record.turn_id)
    assert len(await images.load(identity)) == 2


async def test_new_session_does_not_inherit_image(env: Env) -> None:
    images = SessionImageStore(env.engine, clock=env.clock)
    old = await env.store.current_session("feishu", "alice", "oc_alice")
    receipt = await env.service.accept(
        env.inbound("image", "image-one", channel="feishu", conversation="oc_alice")
    )
    await images.save(
        old,
        "source-one",
        ValidatedImage(b"one", "image/png", 1, 1),
        turn_id=receipt.record.turn_id,
    )
    await env.store.fail(receipt.record, "busy")
    new = await env.store.new_session("feishu", "alice", "oc_alice")
    identity = Identity(
        subject_id="alice", session_id=new.session_id, turn_id="turn", channel="feishu"
    )
    assert await images.load(identity) == []


async def test_pending_other_turn_and_failed_image_are_not_replayed(env: Env) -> None:
    images = SessionImageStore(env.engine, clock=env.clock)
    first = await env.service.accept(
        env.inbound("first image", "first-image", channel="feishu", conversation="oc_alice")
    )
    second = await env.service.accept(
        env.inbound("second image", "second-image", channel="feishu", conversation="oc_alice")
    )
    session = await env.store.current_session("feishu", "alice", "oc_alice")
    image = ValidatedImage(b"image", "image/png", 1, 1)
    await images.save(session, "first-source", image, turn_id=first.record.turn_id)
    await images.save(session, "second-source", image, turn_id=second.record.turn_id)
    first_identity = Identity(
        subject_id="alice",
        session_id=session.session_id,
        turn_id=first.record.turn_id,
        channel="feishu",
    )
    assert [saved.source_digest for saved in await images.load(first_identity)] == ["first-source"]
    await env.store.fail(first.record, "busy")
    later_identity = first_identity.model_copy(update={"turn_id": "later"})
    assert await images.load(later_identity) == []


async def test_image_cannot_bind_to_a_request_in_another_session(env: Env) -> None:
    images = SessionImageStore(env.engine, clock=env.clock)
    session = await env.store.current_session("feishu", "alice", "oc_alice")
    other = await env.service.accept(
        env.inbound("other image", "other-image", channel="feishu", conversation="oc_other")
    )
    with pytest.raises(ImageUnavailableError):
        await images.save(
            session,
            "other-source",
            ValidatedImage(b"image", "image/png", 1, 1),
            turn_id=other.record.turn_id,
        )


async def test_modified_or_expired_original_cannot_be_replayed(env: Env) -> None:
    images = SessionImageStore(env.engine, clock=env.clock)
    receipt = await env.service.accept(
        env.inbound("image", "image-one", channel="feishu", conversation="oc_alice")
    )
    session = await env.store.current_session("feishu", "alice", "oc_alice")
    await images.save(
        session,
        "source",
        ValidatedImage(b"original", "image/png", 1, 1),
        turn_id=receipt.record.turn_id,
    )
    identity = Identity(
        subject_id="alice",
        session_id=session.session_id,
        turn_id=receipt.record.turn_id,
        channel="feishu",
    )
    async with env.engine.begin() as conn:
        await conn.execute(text("UPDATE xiaowei_image SET original = 'altered'"))
    with pytest.raises(ImageUnavailableError, match="原值无法核实"):
        await images.load(identity)
    async with env.engine.begin() as conn:
        await conn.execute(text("UPDATE xiaowei_image SET original = 'original'"))
    env.clock.now += timedelta(seconds=7201)
    with pytest.raises(ImageUnavailableError, match="归属或期限"):
        await images.load(identity)


async def test_expired_original_is_removed_by_existing_cleanup(env: Env) -> None:
    images = SessionImageStore(env.engine, clock=env.clock)
    receipt = await env.service.accept(
        env.inbound("image", "image-cleanup", channel="feishu", conversation="oc_alice")
    )
    session = await env.store.current_session("feishu", "alice", "oc_alice")
    await images.save(
        session,
        "source-cleanup",
        ValidatedImage(b"original", "image/png", 1, 1),
        turn_id=receipt.record.turn_id,
    )
    async with env.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM xiaowei_image")) == 1
    env.clock.now += timedelta(seconds=7201)
    await cleanup_expired(env.engine, now=env.clock(), batch_size=100)
    async with env.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM xiaowei_image")) == 0
