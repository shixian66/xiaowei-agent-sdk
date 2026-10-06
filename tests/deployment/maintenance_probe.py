"""P3-B 容器验收探针。

此文件只由部署测试只读挂载进运行镜像，不进入发行包或运行镜像。它用产品的真实存储 API 建立并
回读合成个人/群会话与去重请求；Evidence 行只用于验证备份恢复会保留归属与过期时间，不作为
真实外部服务或权限复核证据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from agents.extensions.memory import SQLAlchemySession
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei.channel_store import ChannelStore
from xiaowei.models import AgentAnswer, Owner, TurnAnswer
from xiaowei.storage import (
    Readiness,
    check_digest_key,
    check_storage,
    hold_instance_lock,
    open_engine,
)

_PERSONAL = {
    "channel": "web",
    "subject_id": "local-operator",
    "conversation": "p3_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "request_id": "p3-upgrade-request",
    "mode": "query",
    "message": "读取已保存的升级验收结果",
    "policy_version": "v1",
}
_GROUP_OWNER = Owner(kind="group", id="p3-maintenance-group")
_GROUP = {
    "channel": "feishu",
    "subject_id": "ou_p3_member",
    "conversation": "oc_p3_group",
    "request_id": "om_p3_group",
    "mode": "diagnose",
    "message": "读取群会话升级验收结果",
    "policy_version": "v1",
    "owner": _GROUP_OWNER,
}
_ANSWER = TurnAnswer(
    answer=AgentAnswer(
        evidence_ids=(),
        inferences=[],
        clarification=None,
        advice="升级前保存的合成结果",
    ),
    context_evidence=(),
)


def _now() -> datetime:
    return datetime.now(UTC)


@asynccontextmanager
async def _ready_engine() -> AsyncIterator[AsyncEngine]:
    url = os.environ["XW_DATABASE_URL"]
    key = SecretStr(os.environ["XW_DIGEST_KEY"])
    async with open_engine(SecretStr(url)) as engine:
        await check_storage(engine)
        await check_digest_key(engine, key)
        yield engine


def _store(engine: AsyncEngine, readiness: Readiness) -> ChannelStore:
    return ChannelStore(
        engine,
        key=SecretStr(os.environ["XW_DIGEST_KEY"]),
        clock=_now,
        readiness=readiness,
        request_retention_seconds=86_400,
        session_retention_seconds=172_800,
        evidence_retention_seconds=172_800,
        max_answer_bytes=65_536,
    )


async def _register_session(
    engine: AsyncEngine, *, session_id: str, owner: Owner, channel: str
) -> None:
    now = _now()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO xiaowei_session "
                "(session_id, owner_kind, owner_id, channel, profile_fingerprint, created_at, "
                "expires_at, turns, state) VALUES "
                "(:session_id, :owner_kind, :owner_id, :channel, 'p3-profile', :now, :expires, "
                "1, 'active')"
            ),
            {
                "session_id": session_id,
                "owner_kind": owner.kind,
                "owner_id": owner.id,
                "channel": channel,
                "now": now,
                "expires": now + timedelta(days=2),
            },
        )
    await SQLAlchemySession(session_id, engine=engine).add_items(
        [{"role": "user", "content": "P3-B synthetic history"}]
    )


async def _insert_evidence(
    engine: AsyncEngine,
    *,
    evidence_id: str,
    owner: Owner,
    subject_id: str,
    session_id: str,
    turn_id: str,
    channel: str,
    expired: bool,
) -> None:
    now = _now()
    expires = now - timedelta(seconds=1) if expired else now + timedelta(days=1)
    projection = json.dumps(
        {"source": "p3-maintenance", "owner": owner.kind},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO xiaowei_evidence "
                "(evidence_id, owner_kind, owner_id, subject_id, session_id, turn_id, channel, "
                "target_id, tool_id, call_id, tool_name, arguments_digest, policy_id, "
                "policy_fingerprint, captured_at, recorded_at, expires_at, truncated, "
                "model_content, session_content, web_content, feishu_content, dependencies) "
                "VALUES (:evidence_id, :owner_kind, :owner_id, :subject_id, :session_id, "
                ":turn_id, :channel, 'warehouse', 'local/list_tables', :call_id, "
                "'list_tables', 'p3-arguments', 'p3-policy', 'p3-policy-fingerprint', :now, "
                ":now, :expires, false, :projection, :projection, :projection, :projection, NULL)"
            ),
            {
                "evidence_id": evidence_id,
                "owner_kind": owner.kind,
                "owner_id": owner.id,
                "subject_id": subject_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "channel": channel,
                "call_id": f"call-{evidence_id}",
                "now": now,
                "expires": expires,
                "projection": projection,
            },
        )


async def _seed() -> dict[str, Any]:
    async with _ready_engine() as engine:
        readiness = Readiness()
        async with hold_instance_lock(engine, readiness) as lock:
            store = _store(engine, readiness)
            await store.recover(lock)

            personal = await store.accept(**_PERSONAL)  # type: ignore[arg-type]
            assert personal.created
            personal_record = await store.start(
                personal.record,
                message=_PERSONAL["message"],
                policy_version=_PERSONAL["policy_version"],
            )
            personal_record = await store.complete(personal_record, _ANSWER)

            group = await store.accept(**_GROUP)  # type: ignore[arg-type]
            assert group.created
            group_record = await store.start(
                group.record,
                message=_GROUP["message"],
                policy_version=_GROUP["policy_version"],
            )
            group_record = await store.complete(group_record, _ANSWER)

            await _register_session(
                engine,
                session_id=personal_record.session_id,
                owner=Owner(kind="personal", id=_PERSONAL["subject_id"]),
                channel="web",
            )
            await _register_session(
                engine,
                session_id=group_record.session_id,
                owner=_GROUP_OWNER,
                channel="feishu",
            )
            await _insert_evidence(
                engine,
                evidence_id="ev_p3_personal",
                owner=Owner(kind="personal", id=_PERSONAL["subject_id"]),
                subject_id=_PERSONAL["subject_id"],
                session_id=personal_record.session_id,
                turn_id=personal_record.turn_id,
                channel="web",
                expired=False,
            )
            await _insert_evidence(
                engine,
                evidence_id="ev_p3_group_expired",
                owner=_GROUP_OWNER,
                subject_id=_GROUP["subject_id"],
                session_id=group_record.session_id,
                turn_id=group_record.turn_id,
                channel="feishu",
                expired=True,
            )
        return await _snapshot(engine)


async def _verify() -> dict[str, Any]:
    async with _ready_engine() as engine:
        readiness = Readiness()
        async with hold_instance_lock(engine, readiness) as lock:
            store = _store(engine, readiness)
            await store.recover(lock)
            for values, suffix in ((_PERSONAL, "personal"), (_GROUP, "group")):
                repeated = await store.accept(**values)  # type: ignore[arg-type]
                assert not repeated.created and repeated.record.state == "completed"
                continued_values = {**values, "request_id": f"p3-after-upgrade-{suffix}"}
                continued = await store.accept(**continued_values)  # type: ignore[arg-type]
                assert continued.created
                assert continued.record.session_id == repeated.record.session_id
                await store.fail(continued.record, "busy")
        return await _snapshot(engine)


async def _snapshot(engine: AsyncEngine) -> dict[str, Any]:
    async with engine.connect() as conn:
        database = await conn.scalar(text("SELECT current_database()"))
        post_backup_marker = await conn.scalar(
            text("SELECT to_regclass('public.p3_post_backup_marker') IS NOT NULL")
        )
        version = await conn.scalar(text("SELECT version FROM xiaowei_schema_version"))
        binding = (
            await conn.execute(
                text(
                    "SELECT count(*) AS rows, min(length(digest_key_fingerprint)) AS length "
                    "FROM xiaowei_installation"
                )
            )
        ).one()
        counts = {}
        for table in (
            "agent_sessions",
            "agent_messages",
            "xiaowei_channel_session",
            "xiaowei_session",
            "xiaowei_request",
            "xiaowei_evidence",
        ):
            counts[table] = await conn.scalar(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608 - 表名来自固定元组
            )
        owners: dict[str, list[str]] = {}
        for table in ("xiaowei_session", "xiaowei_request", "xiaowei_evidence"):
            rows = await conn.execute(
                text(
                    f"SELECT DISTINCT owner_kind || ':' || owner_id FROM {table} ORDER BY 1"  # noqa: S608
                )
            )
            owners[table] = list(rows.scalars())
        evidence = await conn.execute(
            text(
                "SELECT evidence_id, owner_kind, expires_at <= now() AS expired "
                "FROM xiaowei_evidence ORDER BY evidence_id"
            )
        )
    return {
        "database": database,
        "post_backup_marker": bool(post_backup_marker),
        "schema_version": version,
        "binding_rows": binding.rows,
        "binding_length": binding.length,
        "counts": counts,
        "owners": owners,
        "evidence": [list(row) for row in evidence],
    }


async def _main(action: str) -> dict[str, Any]:
    if action == "seed":
        return await _seed()
    if action == "verify":
        return await _verify()
    async with _ready_engine() as engine:
        return await _snapshot(engine)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("seed", "verify", "snapshot"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_main(args.action)), ensure_ascii=False, default=str))
