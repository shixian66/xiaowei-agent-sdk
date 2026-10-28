"""应用表的显式初始化与逐版升级、版本双向拒绝、密钥绑定和单实例锁。真实 PostgreSQL。

普通初始化与就绪检查不升级 v1；只有显式 ``upgrade_storage`` 在实例锁与事务中把 v1 升到 v2，
失败整体回滚。``serve`` 用专用连接持有会话级实例锁，初始化与升级必须独占同一把锁。
"""

import asyncio
from importlib.resources import files

import pytest
from agents.extensions.memory import SQLAlchemySession
from pydantic import SecretStr
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from tests.sdk_core.synthetic_tools import (
    TOTAL_TOOL,
    Clock,
    Grants,
    RecordingAdapter,
    context,
    request,
    secret,
    store,
)

from xiaowei import storage as storage_module
from xiaowei.governance import GovernedTools
from xiaowei.storage import (
    APP_SCHEMA_VERSION,
    APP_TABLES,
    SDK_MESSAGES_TABLE,
    SDK_SESSIONS_TABLE,
    InstanceLock,
    Readiness,
    StorageBusyError,
    StorageDigestKeyMismatchError,
    StorageError,
    StorageNotInitializedError,
    StorageUnavailableError,
    StorageVersionMismatchError,
    check_digest_key,
    check_storage,
    hold_instance_lock,
    initialize_storage,
    open_engine,
    upgrade_storage,
)

pytestmark = pytest.mark.loopback

_V2_TABLES = {"xiaowei_channel_session", "xiaowei_request"}
_DIGEST_KEY = SecretStr("storage-test-digest-key")


def _migration(name: str) -> str:
    return files("xiaowei").joinpath(f"migrations/{name}").read_text(encoding="utf-8")


async def _install_v1(engine: AsyncEngine, *, through: str = "001_initial.sql") -> None:
    """模拟旧部署：SDK 表与按顺序执行到 ``through`` 的应用表迁移（默认只有 001，即 P1-A）。"""
    probe = SQLAlchemySession("probe", engine=engine, create_tables=True)
    await probe.get_items(limit=0)
    names = [
        "001_initial.sql",
        "002_p1b_channels.sql",
        "003_delivery_attempt.sql",
        "004_evidence_dependencies.sql",
        "005_group_ownership.sql",
        "006_digest_key_binding.sql",
        "007_monitoring_actions.sql",
        "008_session_state.sql",
        "009_session_images.sql",
        "010_sdk_approval.sql",
        "011_feishu_reports.sql",
        "012_delivery_parts.sql",
        "013_notification_attempt.sql",
        "014_scheduled_reminders.sql",
        "015_scheduled_agent.sql",
        "016_scheduled_result.sql",
    ]
    async with engine.begin() as conn:
        for name in names[: names.index(through) + 1]:
            for chunk in _migration(name).split(";\n"):
                if chunk.strip():
                    await conn.execute(text(chunk.strip()))


async def _version(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT version FROM xiaowei_schema_version"))).one()[0]
        )


async def _tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))


def test_v2_migration_only_touches_application_tables() -> None:
    for name in (
        "002_p1b_channels.sql",
        "003_delivery_attempt.sql",
        "004_evidence_dependencies.sql",
        "005_group_ownership.sql",
        "006_digest_key_binding.sql",
        "007_monitoring_actions.sql",
        "008_session_state.sql",
        "009_session_images.sql",
        "010_sdk_approval.sql",
        "011_feishu_reports.sql",
        "012_delivery_parts.sql",
        "013_notification_attempt.sql",
        "014_scheduled_reminders.sql",
    ):
        assert "agent_" not in _migration(name)
    assert APP_SCHEMA_VERSION == 17
    assert _V2_TABLES <= APP_TABLES
    assert all(name.startswith("xiaowei_") for name in APP_TABLES)


async def test_fresh_initialization_installs_the_current_version(postgres_url: URL) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await initialize_storage(engine, digest_key=_DIGEST_KEY)
        await initialize_storage(engine, digest_key=_DIGEST_KEY)  # 重复执行不重复建表
        await check_storage(engine)
        assert await _version(engine) == APP_SCHEMA_VERSION
        assert await _tables(engine) == {SDK_SESSIONS_TABLE, SDK_MESSAGES_TABLE, *APP_TABLES}
        assert (
            await upgrade_storage(engine, digest_key=_DIGEST_KEY) == APP_SCHEMA_VERSION
        )  # 已是当前版本：显式升级无变化


async def test_fresh_initialization_binds_the_digest_key(postgres_url: URL) -> None:
    key = SecretStr("original-digest-key")
    async with open_engine(secret(postgres_url)) as engine:
        await initialize_storage(engine, digest_key=key)
        await initialize_storage(engine, digest_key=key)

        with pytest.raises(StorageError, match="摘要密钥与数据库不匹配"):
            await initialize_storage(engine, digest_key=SecretStr("different-digest-key"))

        # 错配只拒绝，不能覆盖首次绑定；原密钥仍可通过。
        await initialize_storage(engine, digest_key=key)


async def test_v13_upgrade_keeps_notification_receipt_and_sdk_history(postgres_url: URL) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="013_notification_attempt.sql")
        async with engine.begin() as conn:
            await storage_module._write_digest_key_binding(conn, _DIGEST_KEY)
            await conn.execute(
                text(
                    "INSERT INTO xiaowei_request (channel,request_key,owner_kind,owner_id,"
                    "subject_id,conversation_key,session_id,turn_id,mode,message_digest,state,failure_code,"
                    "delivery,created_at,updated_at,expires_at,reply_chat_id,reply_message_id) "
                    "VALUES ('feishu','old-notify','group','g',"
                    "'alice','c','s','old-turn','query','d','failed','busy','sent',"
                    "now(),now(),now()+interval '1 hour','oc_old','om_old_notify')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO xiaowei_notification_attempt "
                    "(receipt_id,turn_id,bot_id,bot_alias,target_version,"
                    "content_digest,state,reason) "
                    "VALUES ('old-receipt','old-turn','ops','ops_alert_bot',"
                    "'v1','digest','unknown','response_unknown')"
                )
            )
        session = SQLAlchemySession("v13-history", engine=engine)
        await session.add_items([{"role": "user", "content": "retained v13 history"}])
        assert await upgrade_storage(engine, digest_key=_DIGEST_KEY) == APP_SCHEMA_VERSION
        assert await session.get_items() == [{"role": "user", "content": "retained v13 history"}]
        async with engine.connect() as conn:
            receipt = (
                await conn.execute(
                    text("SELECT receipt_id,turn_id,job_id,state FROM xiaowei_notification_attempt")
                )
            ).one()
        assert tuple(receipt) == ("old-receipt", "old-turn", None, "unknown")
        await check_storage(engine)


async def test_direct_v12_upgrade_adds_notification_and_reminder_tables_and_preserves_history(
    postgres_url: URL,
) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="012_delivery_parts.sql")
        async with engine.begin() as conn:
            await storage_module._write_digest_key_binding(conn, _DIGEST_KEY)
        session = SQLAlchemySession("v12-history", engine=engine)
        await session.add_items([{"role": "user", "content": "retained v12 history"}])
        before = await _tables(engine)
        with pytest.raises(StorageVersionMismatchError):
            await check_storage(engine)
        assert await upgrade_storage(engine, digest_key=_DIGEST_KEY) == APP_SCHEMA_VERSION
        assert await _tables(engine) == before | {
            "xiaowei_notification_attempt",
            "xiaowei_scheduled_job",
            "xiaowei_scheduled_run",
        }
        assert await session.get_items() == [{"role": "user", "content": "retained v12 history"}]
        await check_digest_key(engine, _DIGEST_KEY)
        await check_storage(engine)


async def test_existing_v5_needs_an_explicit_first_digest_key_binding(postgres_url: URL) -> None:
    key = SecretStr("legacy-digest-key")
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="005_group_ownership.sql")

        with pytest.raises(StorageError, match="需要明确确认旧摘要密钥"):
            await upgrade_storage(engine, digest_key=key)
        assert await _version(engine) == 5

        assert (
            await upgrade_storage(engine, digest_key=key, bind_existing_digest_key=True)
            == APP_SCHEMA_VERSION
        )
        with pytest.raises(StorageError, match="摘要密钥与数据库不匹配"):
            await upgrade_storage(engine, digest_key=SecretStr("different-digest-key"))
        assert await _version(engine) == APP_SCHEMA_VERSION


@pytest.mark.parametrize(
    "through",
    [
        "006_digest_key_binding.sql",
        "007_monitoring_actions.sql",
        "008_session_state.sql",
        "009_session_images.sql",
    ],
)
async def test_v6_to_v9_upgrade_to_v11_preserves_binding_rows_and_sdk_history(
    postgres_url: URL, through: str
) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through=through)
        async with engine.begin() as conn:
            await storage_module._write_digest_key_binding(conn, _DIGEST_KEY)
            await conn.execute(
                text(
                    "INSERT INTO xiaowei_request (channel, request_key, owner_kind, owner_id, "
                    "subject_id, conversation_key, session_id, turn_id, mode, "
                    "message_digest, state, failure_code,"
                    " delivery, created_at, updated_at, expires_at) VALUES ('web', 'old-key',"
                    " 'personal', 'alice', 'alice', 'c', 's', 't', 'query', 'd', 'failed', 'busy',"
                    " 'sent', now(), now(), now() + interval '1 hour')"
                )
            )
        session = SQLAlchemySession("existing", engine=engine)
        await session.add_items([{"role": "user", "content": "retained history"}])
        grants = Grants()
        grants.grant("alice", TOTAL_TOOL)
        evidence = store(engine, grants, Clock())
        ctx = context()
        result = await GovernedTools(evidence).invoke(ctx, request(), RecordingAdapter().execute)
        original_projection = await evidence.project(result.evidence_id, ctx, "web")
        async with engine.connect() as conn:
            request_rows = text(
                "SELECT channel, request_key, owner_kind, owner_id, subject_id, "
                "conversation_key, session_id, turn_id, mode, message_digest, state, "
                "failure_code, delivery, created_at, updated_at, expires_at "
                "FROM xiaowei_request"
            )
            before = [tuple(row) for row in await conn.execute(request_rows)]
            binding = [
                tuple(row) for row in await conn.execute(text("SELECT * FROM xiaowei_installation"))
            ]
        with pytest.raises(StorageVersionMismatchError):
            await check_storage(engine)
        assert await _version(engine) in (6, 7, 8, 9)
        assert await upgrade_storage(engine, digest_key=_DIGEST_KEY) == APP_SCHEMA_VERSION
        # 升级与新版启动是不同进程；丢弃 ALTER TABLE 前准备的 SELECT * 查询缓存。
        await engine.dispose()
        assert "xiaowei_action" in await _tables(engine)
        async with engine.connect() as conn:
            assert [tuple(row) for row in await conn.execute(request_rows)] == before
            assert await conn.scalar(text("SELECT session_state FROM xiaowei_request")) is None
            assert [
                tuple(row) for row in await conn.execute(text("SELECT * FROM xiaowei_installation"))
            ] == binding
        assert await session.get_items() == [{"role": "user", "content": "retained history"}]
        assert await evidence.project(result.evidence_id, ctx, "web") == original_projection
        await check_storage(engine)


async def test_v10_sdk_approval_upgrade_to_v11_keeps_paused_request_and_action_run(
    postgres_url: URL,
) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="010_sdk_approval.sql")
        async with engine.begin() as conn:
            await storage_module._write_digest_key_binding(conn, _DIGEST_KEY)
            await conn.execute(
                text("""
                INSERT INTO xiaowei_request (
                    channel, request_key, owner_kind, owner_id, subject_id,
                    conversation_key, session_id, turn_id, mode, message_digest,
                    state, answer, delivery, reply_chat_id, reply_message_id,
                    created_at, updated_at, expires_at
                ) VALUES (
                    'feishu', 'request-v10', 'group', 'group-v10', 'alice',
                    'conversation-v10', 'session-v10', 'turn-v10', 'query', 'digest-v10',
                    'paused', '{}', 'sent', 'conversation-v10', 'message-v10',
                    now(), now(), now() + interval '1 hour'
                )
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO xiaowei_action (
                    action_id, owner_id, requester_id, proposal_session_id, proposal_turn_id,
                    call_id, proposal_tool_id, write_tool_id, target_id, binding, plan,
                    state, created_at, updated_at, approval_expires_at, expires_at
                ) VALUES (
                    'action-v10', 'group-v10', 'alice', 'session-v10', 'turn-v10',
                    'call-v10', 'source/propose', 'source/write', 'source', 'binding-v10', '{}',
                    'pending', now(), now(), now() + interval '30 minutes',
                    now() + interval '1 hour'
                )
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO xiaowei_action_run (
                    turn_id, owner_id, context, run_state, ledger, message, binding,
                    scope_checks, version, consumed, material_turn_id, expires_at
                ) VALUES (
                    'turn-v10', 'group-v10', '{}', '{}', '{}', 'question', 'binding-v10',
                    2, 1, false, 'turn-v10', now() + interval '1 hour'
                )
                """)
            )
            request_before = (await conn.execute(text("SELECT * FROM xiaowei_request"))).one()
            action_before = (await conn.execute(text("SELECT * FROM xiaowei_action"))).one()
            run_before = (await conn.execute(text("SELECT * FROM xiaowei_action_run"))).one()
        with pytest.raises(StorageVersionMismatchError):
            await check_storage(engine)
        assert await upgrade_storage(engine, digest_key=_DIGEST_KEY) == APP_SCHEMA_VERSION
        await engine.dispose()
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT * FROM xiaowei_request"))).one() == (
                *request_before,
                0,
            )
            assert (await conn.execute(text("SELECT * FROM xiaowei_action"))).one() == action_before
            assert (
                await conn.execute(text("SELECT * FROM xiaowei_action_run"))
            ).one() == run_before
            assert await conn.scalar(text("SELECT count(*) FROM xiaowei_report")) == 0
        await check_storage(engine)


async def test_direct_v11_upgrade_and_isolated_dump_restore_preserve_unknown_report(
    postgres_url: URL,
) -> None:
    import hashlib
    import shutil
    import subprocess

    from tests.sdk_core.postgres_harness import isolated_database

    body = b"<html>legacy report &amp; original bytes</html>"
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="011_feishu_reports.sql")
        async with engine.begin() as conn:
            await storage_module._write_digest_key_binding(conn, _DIGEST_KEY)
            await conn.execute(
                text("""
                INSERT INTO xiaowei_request (
                    channel,request_key,owner_kind,owner_id,subject_id,conversation_key,
                    session_id,turn_id,mode,message_digest,state,answer,delivery,
                    created_at,updated_at,expires_at
                ) VALUES ('feishu','legacy-key','personal','alice','alice','opaque-chat',
                    'session-v11','turn-v11','query','message-digest','completed','{}','unknown',
                    now(),now(),now()+interval '1 hour')
            """)
            )
            await conn.execute(
                text("""
                INSERT INTO xiaowei_report(turn_id,format,body,digest,upload_state,expires_at)
                VALUES('turn-v11','html',:body,:digest,'unknown',now()+interval '1 hour')
            """),
                {"body": body, "digest": hashlib.sha256(body).hexdigest()},
            )
        assert await upgrade_storage(engine, digest_key=_DIGEST_KEY) == APP_SCHEMA_VERSION
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT delivery,delivery_version FROM xiaowei_request"))
            ).one() == ("unknown", 0)
            assert bytes(await conn.scalar(text("SELECT body FROM xiaowei_report"))) == body
    binary = shutil.which("docker")
    assert binary is not None, "隔离备份恢复检查需要专用测试PostgreSQL容器"
    container = "xiaowei-sdk-test-postgres-1"

    def pg(*args, data=None):
        return subprocess.run(  # noqa: S603 - 固定Docker/PG工具；数据库名仅取自隔离harness。
            [binary, "exec", "-i", container, *args],
            input=data,
            capture_output=True,
            check=True,
            timeout=60,
        )

    dumped = await asyncio.to_thread(
        pg, "pg_dump", "-U", "postgres", "-Fc", "--no-owner", "--no-acl", postgres_url.database
    )
    async with isolated_database() as restored:
        await asyncio.to_thread(
            pg,
            "pg_restore",
            "-U",
            "postgres",
            "--no-owner",
            "--no-acl",
            "--dbname",
            restored.database,
            data=dumped.stdout,
        )
        async with open_engine(secret(restored)) as engine:
            await check_storage(engine)
            async with engine.connect() as conn:
                assert bytes(await conn.scalar(text("SELECT body FROM xiaowei_report"))) == body
                assert (
                    await conn.scalar(text("SELECT upload_state FROM xiaowei_report")) == "unknown"
                )
                assert (
                    await conn.execute(
                        text("SELECT delivery,delivery_version FROM xiaowei_request")
                    )
                ).one() == ("unknown", 0)
                assert await conn.scalar(text("SELECT count(*) FROM xiaowei_delivery_part")) == 0


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("damage", ["different", "missing"])
async def test_v6_upgrade_cannot_rebind_or_repair_digest_key(
    postgres_url: URL, confirm: bool, damage: str
) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="006_digest_key_binding.sql")
        if damage == "different":
            async with engine.begin() as conn:
                await storage_module._write_digest_key_binding(conn, SecretStr("original-key"))
        with pytest.raises(StorageDigestKeyMismatchError):
            await upgrade_storage(engine, digest_key=_DIGEST_KEY, bind_existing_digest_key=confirm)
        assert await _version(engine) == 6
        assert "xiaowei_action" not in await _tables(engine)


@pytest.mark.parametrize("damage", ["missing", "different"])
async def test_missing_or_changed_v6_binding_is_never_repaired(
    postgres_url: URL, damage: str
) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await initialize_storage(engine, digest_key=_DIGEST_KEY)
        async with engine.begin() as conn:
            if damage == "missing":
                await conn.execute(text("DELETE FROM xiaowei_installation"))
            else:
                await conn.execute(
                    text("UPDATE xiaowei_installation SET digest_key_fingerprint = repeat('0', 64)")
                )

        for operation in (initialize_storage, upgrade_storage):
            with pytest.raises(StorageDigestKeyMismatchError):
                await operation(engine, digest_key=_DIGEST_KEY)
        with pytest.raises(StorageDigestKeyMismatchError):
            await check_digest_key(engine, _DIGEST_KEY)

        async with engine.connect() as conn:
            rows = (
                await conn.execute(text("SELECT digest_key_fingerprint FROM xiaowei_installation"))
            ).all()
        assert rows == ([] if damage == "missing" else [("0" * 64,)])


async def test_v1_is_rejected_until_explicit_upgrade(postgres_url: URL) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine)
        with pytest.raises(StorageVersionMismatchError):
            await check_storage(engine)
        # 普通初始化不把 v1 升级：版本与表都不变。
        with pytest.raises(StorageVersionMismatchError):
            await initialize_storage(engine, digest_key=_DIGEST_KEY)
        assert await _version(engine) == 1
        assert not _V2_TABLES & await _tables(engine)

        assert (
            await upgrade_storage(engine, digest_key=_DIGEST_KEY, bind_existing_digest_key=True)
            == APP_SCHEMA_VERSION
        )
        assert await upgrade_storage(engine, digest_key=_DIGEST_KEY) == APP_SCHEMA_VERSION
        await check_storage(engine)
        assert _V2_TABLES <= await _tables(engine)


async def test_unknown_or_missing_versions_are_never_upgraded(postgres_url: URL) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        with pytest.raises(StorageNotInitializedError):
            await upgrade_storage(engine, digest_key=_DIGEST_KEY)
        await initialize_storage(engine, digest_key=_DIGEST_KEY)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE xiaowei_schema_version SET version = 99"))
        for operation in (check_storage, initialize_storage, upgrade_storage):
            with pytest.raises(StorageVersionMismatchError):
                if operation is initialize_storage:
                    await operation(engine, digest_key=_DIGEST_KEY)
                elif operation is upgrade_storage:
                    await operation(engine, digest_key=_DIGEST_KEY)
                else:
                    await operation(engine)
        assert await _version(engine) == 99


async def test_initialization_advances_the_schema_version(postgres_url: URL) -> None:
    """全新初始化推进到当前版本。旧程序只接受各自版本由基线源码保证，这里不复制旧实现。"""
    async with open_engine(secret(postgres_url)) as engine:
        await initialize_storage(engine, digest_key=_DIGEST_KEY)
        assert await _version(engine) == APP_SCHEMA_VERSION == 17


async def test_failed_upgrade_rolls_back_completely(postgres_url: URL) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine)
        # 迁移的第二个表名被占用：第一张表已建立后失败，整个事务回滚。
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE xiaowei_request (x int)"))
        with pytest.raises(StorageUnavailableError):
            await upgrade_storage(engine, digest_key=_DIGEST_KEY, bind_existing_digest_key=True)
        assert await _version(engine) == 1
        assert "xiaowei_channel_session" not in await _tables(engine)


async def test_cancelled_upgrade_rolls_back_and_releases_the_lock(
    postgres_url: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="005_group_ownership.sql")
        entered = asyncio.Event()
        apply = storage_module._apply_migrations

        async def apply_then_wait(conn: AsyncConnection, *, after: int) -> None:
            await apply(conn, after=after)
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(storage_module, "_apply_migrations", apply_then_wait)
        upgrading = asyncio.create_task(
            upgrade_storage(engine, digest_key=_DIGEST_KEY, bind_existing_digest_key=True)
        )
        await asyncio.wait_for(entered.wait(), 5)
        upgrading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await upgrading

        assert await _version(engine) == 5
        assert "xiaowei_installation" not in await _tables(engine)
        monkeypatch.setattr(storage_module, "_apply_migrations", apply)
        assert (
            await upgrade_storage(engine, digest_key=_DIGEST_KEY, bind_existing_digest_key=True)
            == APP_SCHEMA_VERSION
        )


async def test_concurrent_upgrades_run_the_migration_once(postgres_url: URL) -> None:
    async with (
        open_engine(secret(postgres_url)) as first,
        open_engine(secret(postgres_url)) as second,
    ):
        await _install_v1(first)
        results = await asyncio.gather(
            upgrade_storage(first, digest_key=_DIGEST_KEY, bind_existing_digest_key=True),
            upgrade_storage(second, digest_key=_DIGEST_KEY, bind_existing_digest_key=True),
            return_exceptions=True,
        )
        # 实例锁只让一个进程执行；另一个要么看到已完成的升级，要么因锁被占用而拒绝。
        assert any(r == APP_SCHEMA_VERSION for r in results)
        assert all(r == APP_SCHEMA_VERSION or isinstance(r, StorageBusyError) for r in results)
        assert await _version(first) == APP_SCHEMA_VERSION


async def test_v2_upgrade_keeps_requests_and_leaves_legacy_sending_to_recovery(
    postgres_url: URL,
) -> None:
    """v2 → v3 只加投递尝试列与约束：已有请求不变；v2 遗留的 sending 没有尝试标识。"""
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="002_p1b_channels.sql")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO xiaowei_request VALUES ('web', 'k', 'alice', 'c', 's', 't',"
                    " 'query', 'd', 'failed', NULL, 'busy', 'sending', now(), now(), now())"
                )
            )
        with pytest.raises(StorageVersionMismatchError):
            await check_storage(engine)
        assert (
            await upgrade_storage(engine, digest_key=_DIGEST_KEY, bind_existing_digest_key=True)
            == APP_SCHEMA_VERSION
        )
        await check_storage(engine)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT delivery, delivery_attempt, delivery_owner_pid FROM xiaowei_request"
                    )
                )
            ).one()
        assert tuple(row) == ("sending", None, None)


async def test_v3_upgrade_adds_evidence_dependencies_and_the_unverifiable_code(
    postgres_url: URL,
) -> None:
    """v3 → v4：已有证据的依赖为 NULL（StarRocks 旧证据因此不可读）；新失败码可写，未知码仍拒绝。"""
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="003_delivery_attempt.sql")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO xiaowei_evidence VALUES ('ev_1', 'alice', 's', 't', 'web',"
                    " 'sr-test', 'local/run_readonly_query', 'c', 'run_readonly_query', 'a', 'p',"
                    " 'f', now(), now(), now(), false, 'm', 's', 'w', 'f')"
                )
            )
        with pytest.raises(StorageVersionMismatchError):
            await check_storage(engine)
        assert (
            await upgrade_storage(engine, digest_key=_DIGEST_KEY, bind_existing_digest_key=True)
            == APP_SCHEMA_VERSION
        )
        await check_storage(engine)
        insert = (
            "INSERT INTO xiaowei_request (channel, request_key, owner_kind, owner_id, subject_id,"
            " conversation_key, session_id, turn_id, mode, message_digest, state, answer,"
            " failure_code, delivery, created_at, updated_at, expires_at) VALUES ('web', :key,"
            " 'personal', 'alice', 'alice', 'c', 's', 't', 'query', 'd', 'failed', NULL, :code,"
            " 'pending', now(), now(), now())"
        )
        async with engine.begin() as conn:
            assert await conn.scalar(text("SELECT dependencies FROM xiaowei_evidence")) is None
            await conn.execute(text(insert), {"key": "k1", "code": "scope_unverifiable"})
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text(insert), {"key": "k2", "code": "made_up"})


async def test_instance_lock_is_exclusive_with_serve_and_maintenance(postgres_url: URL) -> None:
    async with (
        open_engine(secret(postgres_url)) as engine,
        open_engine(secret(postgres_url)) as other,
    ):
        await initialize_storage(engine, digest_key=_DIGEST_KEY)
        readiness = Readiness()
        async with hold_instance_lock(engine, readiness) as lock:
            await lock.verify()
            # 第二个 serve 与维护命令都不能与持锁实例并存。
            with pytest.raises(StorageBusyError):
                async with hold_instance_lock(other, Readiness()):
                    pass
            with pytest.raises(StorageBusyError):
                await initialize_storage(other, digest_key=_DIGEST_KEY)
            with pytest.raises(StorageBusyError):
                await upgrade_storage(other, digest_key=_DIGEST_KEY)
        # 释放后可再次取得。
        async with hold_instance_lock(other, Readiness()):
            pass
        assert readiness.ok


async def test_instance_lock_close_timeout_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Driver:
        def add_termination_listener(self, listener: object) -> None:
            del listener

        def remove_termination_listener(self, listener: object) -> None:
            del listener

        def is_closed(self) -> bool:
            return False

    class Raw:
        driver_connection = Driver()

    class Result:
        def mappings(self) -> "Result":
            return self

        def one(self) -> dict[str, object]:
            return {"acquired": True, "pid": 1, "started": object()}

    class Connection:
        invalidated = False

        async def execute(self, *args: object, **kwargs: object) -> Result:
            del args, kwargs
            return Result()

        async def commit(self) -> None:
            return None

        async def get_raw_connection(self) -> Raw:
            return Raw()

        async def invalidate(self) -> None:
            self.invalidated = True

        async def close(self) -> None:
            await asyncio.Event().wait()

    connection = Connection()

    class Engine:
        async def connect(self) -> Connection:
            return connection

    monkeypatch.setattr(storage_module, "_LOCK_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def close_lock() -> None:
        with pytest.raises(StorageUnavailableError, match="实例锁连接关闭超时"):
            async with hold_instance_lock(Engine(), Readiness()):  # type: ignore[arg-type]
                pass

    await asyncio.wait_for(close_lock(), 0.2)
    assert connection.invalidated


async def test_engine_dispose_timeout_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    class Engine:
        async def dispose(self) -> None:
            await asyncio.Event().wait()

    engine = Engine()
    monkeypatch.setattr(storage_module, "_ENGINE_DISPOSE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(storage_module, "create_async_engine", lambda *args, **kwargs: engine)

    async def close_engine() -> None:
        with pytest.raises(StorageUnavailableError, match="PostgreSQL 连接池关闭超时"):
            async with open_engine(
                SecretStr("postgresql+asyncpg://test.invalid/xiaowei")
            ) as opened:
                assert opened is engine

    await asyncio.wait_for(close_engine(), 0.2)


async def test_busy_initialization_leaves_an_empty_database_untouched(postgres_url: URL) -> None:
    async with (
        open_engine(secret(postgres_url)) as engine,
        open_engine(secret(postgres_url)) as other,
    ):
        async with hold_instance_lock(engine, Readiness()):
            with pytest.raises(StorageBusyError):
                await initialize_storage(other, digest_key=_DIGEST_KEY)
            # 被拒绝的命令没有建立任何表（含 SDK 表）。
            assert await _tables(engine) == set()
        await initialize_storage(other, digest_key=_DIGEST_KEY)
        await check_storage(other)


async def test_concurrent_first_initializations_have_one_writer(postgres_url: URL) -> None:
    """并发首次初始化：取不到锁的进程直接拒绝（不建表），不会因锁外建表竞争而失败。"""
    async with (
        open_engine(secret(postgres_url)) as first,
        open_engine(secret(postgres_url)) as second,
    ):
        for _ in range(3):
            results = await asyncio.gather(
                initialize_storage(first, digest_key=_DIGEST_KEY),
                initialize_storage(second, digest_key=_DIGEST_KEY),
                return_exceptions=True,
            )
            assert any(r is None for r in results)
            assert all(r is None or isinstance(r, StorageBusyError) for r in results)
        await check_storage(first)
        assert await _tables(first) == {SDK_SESSIONS_TABLE, SDK_MESSAGES_TABLE, *APP_TABLES}


async def test_losing_the_lock_connection_locks_readiness(postgres_url: URL) -> None:
    async with (
        open_engine(secret(postgres_url)) as engine,
        open_engine(secret(postgres_url)) as admin,
    ):
        await initialize_storage(engine, digest_key=_DIGEST_KEY)
        readiness = Readiness()
        async with hold_instance_lock(engine, readiness) as lock:
            async with admin.begin() as conn:
                terminated = await conn.scalar(
                    text(
                        "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
                        " WHERE pid = :pid AND datname = current_database()"
                    ),
                    {"pid": lock.pid},
                )
            assert terminated == 1
            with pytest.raises(StorageUnavailableError):
                await lock.verify()
            assert not readiness.ok
            # 锁已随连接释放：另一个实例此时可以取得，原实例必须停止服务。
            async with hold_instance_lock(admin, Readiness()):
                pass


async def test_lock_loss_is_signalled_without_a_check(postgres_url: URL) -> None:
    """持锁连接被终止时，驱动的终止通知立即锁低 readiness，不需要等下一次 ``verify``。"""
    async with (
        open_engine(secret(postgres_url)) as engine,
        open_engine(secret(postgres_url)) as admin,
    ):
        await initialize_storage(engine, digest_key=_DIGEST_KEY)
        readiness = Readiness()
        async with hold_instance_lock(engine, readiness) as lock:
            assert not lock.lost.is_set()
            async with admin.begin() as conn:
                await conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                        " WHERE pid = :pid AND datname = current_database()"
                    ),
                    {"pid": lock.pid},
                )
            await asyncio.wait_for(lock.lost.wait(), 2)
            assert readiness.reason == "instance_lock_lost"


async def test_releasing_the_lock_is_not_a_loss(postgres_url: URL) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await initialize_storage(engine, digest_key=_DIGEST_KEY)
        readiness = Readiness()
        async with hold_instance_lock(engine, readiness) as lock:
            await lock.verify()
        await asyncio.sleep(0.1)  # 终止通知经事件循环回调，给它运行的机会
        assert readiness.ok and not lock.lost.is_set()


async def test_a_lock_connection_closed_before_watching_is_a_lock_loss(postgres_url: URL) -> None:
    """连接在取得锁之后、注册终止通知之前断开：通知已经错过，注册时必须立即按丢锁处理并拒绝，
    不能等周期核对。"""
    async with (
        open_engine(secret(postgres_url)) as engine,
        open_engine(secret(postgres_url)) as admin,
    ):
        await initialize_storage(engine, digest_key=_DIGEST_KEY)
        readiness = Readiness()
        conn = await engine.connect()
        try:
            row = (
                await conn.execute(
                    text(
                        "SELECT pid, backend_start FROM pg_stat_activity"
                        " WHERE pid = pg_backend_pid()"
                    )
                )
            ).one()
            await conn.commit()
            driver = (await conn.get_raw_connection()).driver_connection
            assert driver is not None
            async with admin.begin() as other:
                await other.execute(text("SELECT pg_terminate_backend(:p)"), {"p": row.pid})
            async with asyncio.timeout(2):
                while not driver.is_closed():
                    await asyncio.sleep(0.01)
            lock = InstanceLock(conn, readiness, row.pid, row.backend_start)
            with pytest.raises(StorageUnavailableError):
                async with lock.watching():
                    pytest.fail("已关闭的持锁连接不能进入服务")
            assert lock.lost.is_set() and readiness.reason == "instance_lock_lost"
        finally:
            await conn.invalidate()
            await conn.close()


@pytest.mark.parametrize("through", ["008_session_state.sql", "009_session_images.sql"])
async def test_v8_v9_upgrade_invalidates_only_pending_and_recovery_does_not_replay(
    postgres_url: URL, through: str
):
    from datetime import UTC, datetime

    from xiaowei.channel_store import ChannelStore

    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through=through)
        async with engine.begin() as conn:
            await storage_module._write_digest_key_binding(conn, _DIGEST_KEY)
            for state in ("pending", "succeeded", "unknown", "executing"):
                await conn.execute(
                    text("""
                    INSERT INTO xiaowei_action VALUES (
                        :state, 'owner', 'requester', 'session', 'turn', :state,
                        'am/propose', 'am/write', 'am', 'binding', '{}', :state,
                        :attempt, :approver, :approval, :result,
                        now(), now(), now()+interval '15 minutes', now()+interval '1 hour'
                    )
                    """),
                    {
                        "state": state,
                        "attempt": None if state == "pending" else "attempt",
                        "approver": None if state == "pending" else "actor",
                        "approval": None if state == "pending" else "approval",
                        "result": '{"value":1}' if state == "succeeded" else None,
                    },
                )
            before = {
                r.action_id: tuple(r)
                for r in await conn.execute(text("SELECT * FROM xiaowei_action"))
            }
        assert await upgrade_storage(engine, digest_key=_DIGEST_KEY) == APP_SCHEMA_VERSION
        await engine.dispose()
        async with engine.connect() as conn:
            after = {
                r.action_id: tuple(r)
                for r in await conn.execute(text("SELECT * FROM xiaowei_action"))
            }
        assert (
            after["pending"][11] == "rejected"
            and after["pending"][-1] == "action_upgrade_invalidated"
        )
        for state in ("succeeded", "unknown", "executing"):
            assert after[state][:-1] == before[state] and after[state][-1] is None
        assert await upgrade_storage(engine, digest_key=_DIGEST_KEY) == APP_SCHEMA_VERSION
        ready = Readiness()
        store = ChannelStore(
            engine,
            key=_DIGEST_KEY,
            clock=lambda: datetime.now(UTC),
            readiness=ready,
            request_retention_seconds=3600,
            session_retention_seconds=7200,
            evidence_retention_seconds=3600,
            max_answer_bytes=10000,
        )
        async with hold_instance_lock(engine, ready) as lock:
            await store.recover(lock)
            async with engine.connect() as conn:
                assert (
                    await conn.execute(
                        text("SELECT state, result FROM xiaowei_action WHERE action_id='executing'")
                    )
                ).one() == ("unknown", None)
                for state in ("succeeded", "unknown"):
                    actual = (
                        await conn.execute(
                            text("SELECT * FROM xiaowei_action WHERE action_id=:id"), {"id": state}
                        )
                    ).one()
                    assert tuple(actual) == after[state]


async def test_v14_upgrade_preserves_fixed_reminder_and_sdk_session(postgres_url: URL) -> None:
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="014_scheduled_reminders.sql")
        async with engine.begin() as conn:
            await storage_module._write_digest_key_binding(conn, _DIGEST_KEY)
            await conn.execute(
                text(
                    "INSERT INTO xiaowei_scheduled_job "
                    "(job_id,owner_id,creator_id,bot_alias,bot_id,"
                    "target_version,run_at,content_expires_at,text,definition_evidence_ids,"
                    "source_context,"
                    "lateness_seconds,state) VALUES "
                    "('old-job','old-group','old-user','old-alias','bot',"
                    "'v1',now()+interval '1 day',now()+interval '2 days',"
                    "'fixed text','[]','{}',3600,'active')"
                )
            )
        session = SQLAlchemySession("v14-history", engine=engine)
        await session.add_items([{"role": "user", "content": "retained v14 history"}])
        assert await upgrade_storage(engine, digest_key=_DIGEST_KEY) == APP_SCHEMA_VERSION
        await check_storage(engine)
        assert await session.get_items() == [{"role": "user", "content": "retained v14 history"}]
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM xiaowei_scheduled_job WHERE job_id='old-job'")
                    )
                )
                .mappings()
                .one()
            )
        assert row["text"] == "fixed text" and row["state"] == "active"
        assert row["kind"] == "fixed_message" and row["agent_spec"] is None
        assert row["next_run"] == row["run_at"]
        assert "run_count" not in row and "revision" not in row
        async with engine.connect() as conn:
            columns = set(
                (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema=current_schema() "
                            "AND table_name='xiaowei_scheduled_run'"
                        )
                    )
                ).scalars()
            )
        assert (
            not {"answer", "answer_expires_at", "usage", "duration_seconds", "skipped_count"}
            & columns
        )
        migration = _migration("015_scheduled_agent.sql")
        assert SDK_SESSIONS_TABLE not in migration and SDK_MESSAGES_TABLE not in migration


async def test_v15_upgrade_preserves_runs_and_adds_nullable_result(postgres_url: URL) -> None:
    """显式升级不能虚构旧结论、丢掉运行记录或改动SDK历史。"""
    async with open_engine(secret(postgres_url)) as engine:
        await _install_v1(engine, through="015_scheduled_agent.sql")
        async with engine.begin() as conn:
            await storage_module._write_digest_key_binding(conn, _DIGEST_KEY)
            await conn.execute(text(
                "INSERT INTO xiaowei_scheduled_job "
                "(job_id,owner_id,creator_id,bot_alias,bot_id,target_version,run_at,"
                "content_expires_at,text,definition_evidence_ids,source_context,"
                "lateness_seconds,state) "
                "VALUES ('retained-job','group','creator','ops','bot','v1',now(),"
                "now()+interval '1 hour','retained text','[]','{}',3600,'completed')"
            ))
            await conn.execute(text(
                "INSERT INTO xiaowei_scheduled_run (run_id,job_id,scheduled_at,session_id,turn_id,"
                "state,reason,queued_at) VALUES ('retained-run','retained-job',now(),"
                "'retained-session','retained-turn','completed','execution_completed',now())"
            ))
        session = SQLAlchemySession("retained-session", engine=engine)
        await session.add_items([{"role": "assistant", "content": "SDK history is not a snapshot"}])
        assert await upgrade_storage(engine, digest_key=_DIGEST_KEY) == 17
        await check_storage(engine)
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT run_id,state,result_snapshot,result_expires_at FROM xiaowei_scheduled_run"
            ))).one()
            assert tuple(row) == ("retained-run", "completed", None, None)
            columns = (await conn.execute(text(
                "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
                "WHERE table_name='xiaowei_scheduled_run' AND column_name LIKE 'result_%' "
                "ORDER BY column_name"
            ))).all()
            assert columns == [("result_expires_at", "timestamp with time zone", "YES"),
                               ("result_snapshot", "text", "YES")]
        assert await session.get_items() == [
            {"role": "assistant", "content": "SDK history is not a snapshot"}
        ]
