"""B：正式 SDK/治理/Session/隔离 PG；审计、模型与元数据使用合成替身。"""

import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from tests.p1b.test_starrocks_security_audit import record
from tests.sdk_core import test_starrocks_tools as fixtures
from tests.sdk_core.test_app import clarify, tool_call
from tests.sdk_core.test_security_inspection_eval import (
    ARGS,
    TOOLS,
    audit_reads,
    enabled,
    security_driver,
)

from xiaowei.starrocks import StarRocksError, StarRocksErrorCode

pytestmark = pytest.mark.loopback


def cite_results(text="已取得的结果如下"):
    from tests.sdk_core.test_app import answer

    def finish(call):
        outputs = [json.loads(value) for value in fixtures.tool_outputs(call)]
        return answer([item["evidence_id"] for item in outputs if "evidence_id" in item], text)

    return finish


@asynccontextmanager
async def opened(postgres_url, monkeypatch, *, missing=()):
    caches = []
    original = fixtures.ready_schema

    async def capture(*args, **kwargs):
        cache = await original(*args, **kwargs)
        caches.append(cache)
        return cache

    monkeypatch.setattr(fixtures, "ready_schema", capture)
    drv = security_driver(record(), missing=missing)
    from tests.p1b.test_starrocks_audit import AUDIT_TARGET

    async with fixtures.assembled(postgres_url, monkeypatch, drv, AUDIT_TARGET) as env:
        enabled(env)
        yield env, caches[0]


def remove_audit(cache):
    from tests.p1b.test_starrocks_audit import AUDIT

    first = cache.current()
    cache._snapshot = replace(
        first,
        objects={
            key: obj for key, obj in first.objects.items() if key != (AUDIT.database, AUDIT.table)
        },
    )


async def test_missing_audit_recovers_once_before_collection(postgres_url, monkeypatch):

    async with opened(postgres_url, monkeypatch) as (env, cache):
        remove_audit(cache)
        refresh = cache.refresh
        calls = 0

        async def counted():
            nonlocal calls
            calls += 1
            return await refresh()

        monkeypatch.setattr(cache, "refresh", counted)
        message = env.scripts.add(
            "旧快照缺审计源，底层已经恢复",
            tool_call("inspect_audit_security", **ARGS),
            cite_results(),
        )
        delivery = await env.deliver(env.ctx(authorized=TOOLS), message)
        assert len(delivery.facts) == 1 and calls == 1
        assert len(audit_reads(env.drv)) == 2
        assert delivery.inspection_notice is None


@pytest.mark.parametrize(
    "refresh_ok,code", [(True, "object_not_readable"), (False, "schema_unavailable")]
)
async def test_missing_audit_after_refresh_has_trusted_failure(
    postgres_url,
    monkeypatch,
    refresh_ok,
    code,
):
    async with opened(postgres_url, monkeypatch) as (env, cache):
        remove_audit(cache)

        async def refresh():
            return refresh_ok

        monkeypatch.setattr(cache, "refresh", refresh)
        message = env.scripts.add(
            "检查本窗",
            tool_call("inspect_audit_security", **ARGS),
            clarify("该集群未完成巡检"),
        )
        ctx = env.ctx(authorized=TOOLS)
        turn = await env.app.run_turn(ctx, message)
        assert len(turn.inspection_failures) == 1
        failure = turn.inspection_failures[0]
        assert failure.reason == code and failure.counted and not failure.sql_started
        delivery = await env.evidence.validate_answer(turn, ctx)
        assert code in delivery.inspection_notice
        assert not audit_reads(env.drv) and await env.evidence_rows() == 0
        output = json.loads(fixtures.tool_outputs(env.scripts.calls[message][1])[0])
        assert output["code"] == code and "window" in output


async def test_known_source_failure_returns_fixed_reason_and_commits(postgres_url, monkeypatch):
    async with opened(postgres_url, monkeypatch) as (env, _):

        async def failed(*args):
            raise StarRocksError(StarRocksErrorCode.SERVER_TIMEOUT)

        monkeypatch.setattr(env.adapter, "security_audit", failed)
        message = env.scripts.add(
            "本源读取超时",
            tool_call("inspect_audit_security", **ARGS),
            clarify("未取得巡检结果"),
        )
        ctx = env.ctx(authorized=TOOLS)
        turn = await env.app.run_turn(ctx, message)
        delivery = await env.evidence.validate_answer(turn, ctx)
        assert "server_timeout" in delivery.inspection_notice
        assert delivery.facts == () and await env.evidence_rows() == 0


@pytest.mark.parametrize("phase", ["prepare", "execute"])
async def test_inspection_failure_logs_only_trusted_identifiers(
    postgres_url, monkeypatch, caplog, phase
):
    import hashlib
    import logging

    sentinel = "SELECT secret_payload FROM password_sentinel"
    async with opened(
        postgres_url, monkeypatch, missing=("stmt",) if phase == "prepare" else ()
    ) as (env, _):
        if phase == "execute":

            async def failed(*args):
                exc = StarRocksError(StarRocksErrorCode.SERVER_TIMEOUT)
                exc.args = (sentinel,)
                raise exc

            monkeypatch.setattr(env.adapter, "security_audit", failed)
        caplog.set_level(logging.INFO, logger="xiaowei.governance")
        message = env.scripts.add(
            "巡检失败日志不包含数据或异常正文",
            tool_call("inspect_audit_security", **ARGS),
            clarify("未取得巡检结果"),
        )
        ctx = env.ctx(authorized=TOOLS)
        turn = await env.app.run_turn(ctx, message)
        failure = turn.inspection_failures[0]
        entries = [
            r.getMessage() for r in caplog.records if r.msg.startswith("security_inspection ")
        ]
        assert entries == [
            f"security_inspection turn={ctx.identity.turn_id} "
            f"call_ref={hashlib.sha256(failure.call_id.encode()).hexdigest()[:16]} "
            f"target={failure.target_id} reason={failure.reason} "
            f"counted={failure.counted} sql_started={failure.sql_started}"
        ]
        assert "SELECT" not in caplog.text and sentinel not in caplog.text


@asynccontextmanager
async def multi_opened(postgres_url, monkeypatch):
    from xiaowei.app import TargetInfo
    from xiaowei.evidence import EvidenceStore
    from xiaowei.governance import GovernedTools, ToolCatalog
    from xiaowei.models import AUDIENCES
    from xiaowei.starrocks import StarRocksAdapter
    from xiaowei.starrocks_schema import DependencyCheck
    from xiaowei.starrocks_tools import starrocks_tools

    async with opened(postgres_url, monkeypatch) as (env, first):
        adapters = {env.adapter.target.target_id: env.adapter}
        caches = {env.adapter.target.target_id: first}
        for key in ("fat1", "fat2"):
            base = env.adapter.target
            target = base.model_copy(
                update={
                    "target_id": key,
                    "audit": base.audit.model_copy(update={"time_zone": "Asia/Shanghai"}),
                    "policy": base.policy.model_copy(update={"target_id": key}),
                }
            )
            drv = security_driver(record())
            adapter = adapters[key] = StarRocksAdapter(target, connect=drv, clock=env.clock)
            caches[key] = await fixtures.ready_schema(adapter, drv)
        parts = [
            starrocks_tools(a, dict.fromkeys(AUDIENCES, fixtures.CAPACITY), schema=caches[k])
            for k, a in adapters.items()
        ]
        bundle = parts[0] + parts[1] + parts[2]
        for key in adapters:
            env.grants.grant("alice", *TOOLS, target=key)
        evidence = EvidenceStore(
            env.engine,
            ToolCatalog(bundle.contracts, bundle.policies),
            authorize=env.grants,
            clock=env.clock,
            retention_seconds=3600,
            verify_dependencies=DependencyCheck(adapters),
        )
        env.governed = GovernedTools(evidence)
        env.executes = bundle.executes
        enabled(env)
        env.app = env.application(
            env.app._config.model_copy(
                update={
                    "targets": tuple(
                        TargetInfo(target_id=k, description="合成审计源", business_context=None)
                        for k in adapters
                    ),
                }
            )
        )
        try:
            yield env, adapters, caches
        finally:
            for cache in caches.values():
                await cache.aclose()


@pytest.mark.parametrize("missing", [False, True])
async def test_three_targets_keep_success_and_fixed_prefix_before_model_blocks(
    postgres_url,
    monkeypatch,
    missing,
):
    from tests.sdk_core.test_feishu_render import message_text

    from xiaowei.feishu_render import build_feishu_message
    from xiaowei.feishu_report import build_report
    from xiaowei.webhook import _notification_text

    async with multi_opened(postgres_url, monkeypatch) as (env, adapters, caches):
        if missing:
            remove_audit(caches["fat1"])

            async def still_missing():
                return True

            monkeypatch.setattr(caches["fat1"], "refresh", still_missing)
        else:

            async def failed(*args):
                raise StarRocksError(StarRocksErrorCode.SERVER_TIMEOUT)

            monkeypatch.setattr(adapters["fat1"], "security_audit", failed)
        message = env.scripts.add(
            "检查三个集群，同一时间窗，一个失败继续其他已请求目标",
            *(tool_call("inspect_audit_security", **{**ARGS, "cluster": k}) for k in adapters),
            cite_results("两个源有结果，fat1 未完成"),
        )
        ctx = env.ctx(authorized=TOOLS).model_copy(update={"target_scope": frozenset(adapters)})
        turn = await env.app.run_turn(ctx, message)
        delivery = await env.evidence.validate_answer(turn, ctx)
        assert {f.target_id for f in delivery.facts} == set(adapters) - {"fat1"}
        assert len(turn.inspection_failures) == 1 and len(turn.context_evidence) == 2
        assert delivery.inspection_notice.startswith("⚪ 本次巡检未全部完成：fat1")
        assert "2026-10-22T18:00:00+08:00" in delivery.inspection_notice
        assert delivery.content.startswith(delivery.inspection_notice)
        assert delivery.model_dump()["inspection_notice"] == delivery.inspection_notice
        assert delivery.inspection_notice in message_text(
            build_feishu_message(
                delivery.model_copy(update={"channel": "feishu"}), 20000, "oc_synthetic"
            )
        )
        assert delivery.inspection_notice in _notification_text(delivery, "ignored")
        for format_ in ("md", "html"):
            report = build_report(
                delivery.model_copy(update={"report_format": format_, "channel": "feishu"})
            )
            assert "本次巡检未全部完成" in report.content.decode()
        assert await env.evidence_rows() == 2


@pytest.mark.parametrize("success_first", [False, True])
async def test_later_same_window_success_recovers_failure_without_mirroring_success(
    postgres_url,
    monkeypatch,
    success_first,
):
    async with opened(postgres_url, monkeypatch) as (env, _):
        original = env.adapter.security_audit
        calls = 0

        async def initially_failed(*args):
            nonlocal calls
            calls += 1
            if calls == (2 if success_first else 1):
                raise StarRocksError(StarRocksErrorCode.SERVER_TIMEOUT)
            return await original(*args)

        monkeypatch.setattr(env.adapter, "security_audit", initially_failed)
        message = env.scripts.add(
            "同窗恢复",
            tool_call("inspect_audit_security", **ARGS),
            tool_call("inspect_audit_security", **ARGS),
            cite_results(),
        )
        ctx = env.ctx(authorized=TOOLS)
        turn = await env.app.run_turn(ctx, message)
        assert len(turn.inspection_failures) == 1
        assert (
            bool((await env.evidence.validate_answer(turn, ctx)).inspection_notice) == success_first
        )
        # Trusted JSON round-trip, including old-answer defaults, must preserve the same outcome.
        from xiaowei.models import TurnAnswer

        restored = TurnAnswer.model_validate_json(turn.model_dump_json())
        assert (
            bool(
                (await env.evidence.validate_answer(restored, ctx, history=True)).inspection_notice
            )
            == success_first
        )


@pytest.mark.parametrize("case", ["budget", "busy", "unsupported", "denied"])
async def test_rejections_keep_trusted_windows_and_zero_audit_io(postgres_url, monkeypatch, case):
    from xiaowei.governance import ToolRejectedError
    from xiaowei.models import ToolRequest

    async with opened(
        postgres_url, monkeypatch, missing=("stmt",) if case == "unsupported" else ()
    ) as (env, cache):
        ctx = env.ctx(authorized=TOOLS)
        refreshes = 0

        async def refresh():
            nonlocal refreshes
            refreshes += 1
            return True

        monkeypatch.setattr(cache, "refresh", refresh)
        if case == "budget":
            remove_audit(cache)
            env.governed._reserve(ctx)
            env.governed._reserve(ctx)
            env.governed._reserve(ctx)
        elif case == "denied":
            remove_audit(cache)
            env.grants.allowed.discard(("alice", ARGS["cluster"], "local/inspect_audit_security"))
        elif case == "busy":
            remove_audit(cache)
            # Restore from shared refresh, then fail before this audit acquires a slot.

            refreshed = False

            async def restored():
                nonlocal refreshes, refreshed
                refreshes += 1
                refreshed = await type(cache).refresh(cache)
                return refreshed

            monkeypatch.setattr(cache, "refresh", restored)
            original = env.adapter._acquire

            async def acquire():
                if refreshed:
                    raise StarRocksError(StarRocksErrorCode.POOL_TIMEOUT, slot_wait=True)
                return await original()

            monkeypatch.setattr(env.adapter, "_acquire", acquire)
        request = ToolRequest(
            tool_id="local/inspect_audit_security",
            target_id=ARGS["cluster"],
            call_id=case,
            tool_name="inspect_audit_security",
            arguments=ARGS,
        )
        from xiaowei.governance import SecurityInspectionError

        with pytest.raises((ToolRejectedError, SecurityInspectionError)):
            await env.governed.invoke(
                ctx, request, env.executes[(request.tool_id, request.target_id)]
            )
        runs = env.governed.turn_runs(ctx.identity)
        if case == "denied":
            assert runs.inspection_failures == ()
        else:
            (failure,) = runs.inspection_failures
            assert (
                failure.reason
                == {"budget": "budget_exhausted", "busy": "busy", "unsupported": "unsupported"}[
                    case
                ]
            )
            assert not failure.counted and not failure.sql_started
            assert failure.start.isoformat() == ARGS["start_time"]
        assert refreshes == int(case == "busy")
        assert not audit_reads(env.drv) and await env.evidence_rows() == 0
        assert runs.started == ()


@pytest.mark.parametrize("coverage_case", ["unclassified", "limited", "omitted"])
async def test_successful_incomplete_classification_is_not_failed_inspection(
    postgres_url,
    monkeypatch,
    coverage_case,
):
    from xiaowei import starrocks_tools

    original = starrocks_tools.analyze_security_rows

    def analyze(*args, **kwargs):
        found = original(*args, **kwargs)
        return replace(
            found,
            **{
                "unclassified": {"unclassified_candidate_rows": 3},
                "limited": {"scan_limited": True},
                "omitted": {"omitted_samples": 2},
            }[coverage_case],
        )

    monkeypatch.setattr(starrocks_tools, "analyze_security_rows", analyze)
    async with opened(postgres_url, monkeypatch) as (env, _):
        message = env.scripts.add(
            "有结果但分类不完整", tool_call("inspect_audit_security", **ARGS), cite_results()
        )
        ctx = env.ctx(authorized=TOOLS)
        turn = await env.app.run_turn(ctx, message)
        delivery = await env.evidence.validate_answer(turn, ctx)
        assert turn.inspection_failures == () and delivery.inspection_notice is None
        data = json.loads(delivery.facts[0].result_json)
        assert data["coverage"]["match_counts_complete"] == (coverage_case == "omitted")


async def test_analysis_timeout_after_sql_uses_neutral_description(postgres_url, monkeypatch):
    from xiaowei import starrocks_tools

    def timeout(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(starrocks_tools, "analyze_security_rows", timeout)
    async with opened(postgres_url, monkeypatch) as (env, _):
        message = env.scripts.add(
            "读取后分析超时", tool_call("inspect_audit_security", **ARGS), clarify("未取得结果")
        )
        ctx = env.ctx(authorized=TOOLS)
        turn = await env.app.run_turn(ctx, message)
        delivery = await env.evidence.validate_answer(turn, ctx)
        (failure,) = turn.inspection_failures
        assert failure.reason == "timeout" and failure.sql_started and failure.counted
        assert len(audit_reads(env.drv)) == 2
        assert "读取或分析超时" in delivery.inspection_notice
        assert (
            "未执行" not in delivery.inspection_notice
            and "仍运行" not in delivery.inspection_notice
        )


async def test_failure_only_limit_rescue_manual_notification_and_permissions(
    postgres_url, monkeypatch
):
    from xiaowei.evidence import AnswerRejectedError
    from xiaowei.models import TurnAnswer
    from xiaowei.webhook import _notification_text

    async with opened(postgres_url, monkeypatch, missing=("stmt",)) as (env, _):
        ctx = env.ctx(authorized=TOOLS)
        ctx = ctx.model_copy(update={"budget": ctx.budget.model_copy(update={"max_turns": 1})})
        message = env.scripts.add(
            "只失败、无事实、步数用尽",
            tool_call("inspect_audit_security", **ARGS),
            clarify("未完成"),
        )
        turn = await env.app.run_turn(ctx, message)
        assert turn.limit_reached and turn.context_evidence == ()
        assert len(env.scripts.calls[message]) == 2
        assert env.scripts.calls[message][1].tools == []
        saved = TurnAnswer.model_validate_json(turn.model_dump_json())
        delivery = await env.evidence.validate_answer(saved, ctx, history=True)
        assert _notification_text(delivery, "模型自称完成").startswith("⚪ 本次巡检未全部完成")
        with pytest.raises(AnswerRejectedError):
            await env.evidence.validate_answer(saved, ctx, recipient_targets=frozenset())
        env.grants.allowed.clear()
        with pytest.raises(AnswerRejectedError):
            await env.evidence.validate_answer(saved, ctx, history=True)


@pytest.mark.parametrize(
    "error", [RuntimeError("private_bug"), StarRocksError(StarRocksErrorCode.RESULT_CONTRACT)]
)
async def test_unknown_or_contract_errors_still_abort_without_failure_fact(
    postgres_url, monkeypatch, error
):
    from xiaowei.governance import ToolExecutionError
    from xiaowei.models import ToolRequest

    async with opened(postgres_url, monkeypatch) as (env, _):

        async def failed(*args):
            raise error

        monkeypatch.setattr(env.adapter, "security_audit", failed)
        ctx = env.ctx(authorized=TOOLS)
        request = ToolRequest(
            tool_id="local/inspect_audit_security",
            target_id=ARGS["cluster"],
            call_id="known-boundary",
            tool_name="inspect_audit_security",
            arguments=ARGS,
        )
        with pytest.raises(ToolExecutionError):
            await env.governed.invoke(
                ctx, request, env.executes[(request.tool_id, request.target_id)]
            )
        assert env.governed.turn_runs(ctx.identity).inspection_failures == ()
        assert await env.evidence_rows() == 0


def test_checkpoint_defaults_and_new_failure_round_trip():
    from pydantic import TypeAdapter

    from xiaowei.governance import TurnLedger, TurnRuns
    from xiaowei.models import SecurityInspectionFailure

    parser = TypeAdapter(TurnLedger)
    old = parser.validate_json('{"used":0,"runs":{}}', strict=True)
    assert old.runs.inspection_failures == ()
    failure = SecurityInspectionFailure(
        target_id=ARGS["cluster"],
        call_id="checkpoint-failure",
        start=ARGS["start_time"],
        end=ARGS["end_time"],
        time_zone="UTC",
        reason="unsupported",
        counted=False,
        sql_started=False,
    )
    ledger = TurnLedger(0, TurnRuns(inspection_failures=(failure,)))
    assert parser.validate_json(parser.dump_json(ledger), strict=True) == ledger


@pytest.mark.parametrize("revoked", [False, True])
async def test_next_sdk_turn_rechecks_failure_sources_before_model(
    postgres_url, monkeypatch, revoked
):
    from xiaowei.app import TurnError
    from xiaowei.starrocks_security import SECURITY_TOOL

    async with opened(postgres_url, monkeypatch, missing=("stmt",)) as (env, _):
        first = env.scripts.add(
            "检查此前窗口",
            tool_call("inspect_audit_security", **ARGS),
            clarify("敏感故障摘要：本集群的审计源缺少 stmt，指定窗口未完成"),
        )
        await env.app.run_turn(env.ctx(authorized=TOOLS), first)
        if revoked:
            env.grants.allowed.discard(("alice", ARGS["cluster"], SECURITY_TOOL))
        second = env.scripts.add("继续", clarify("继续说明"))
        ctx = env.ctx(turn="t2", authorized=TOOLS - {SECURITY_TOOL})
        if revoked:
            with pytest.raises(TurnError, match="会话"):
                await env.app.run_turn(ctx, second)
            assert second not in env.scripts.calls
        else:
            await env.app.run_turn(ctx, second)
            history = json.dumps(env.scripts.calls[second][0].input, ensure_ascii=False)
            assert "敏感故障摘要" in history
            assert "inspection_target_ref" not in history


async def test_invalid_window_does_not_create_a_new_replay_permission_dependency(
    postgres_url, monkeypatch
):
    from xiaowei.starrocks_security import SECURITY_TOOL

    async with opened(postgres_url, monkeypatch) as (env, _):
        first = env.scripts.add(
            "窗口写错",
            tool_call("inspect_audit_security", **{**ARGS, "end_time": ARGS["start_time"]}),
            clarify("请确认时间范围"),
        )
        turn = await env.app.run_turn(env.ctx(authorized=TOOLS), first)
        assert turn.inspection_failures == ()
        env.grants.allowed.discard(("alice", ARGS["cluster"], SECURITY_TOOL))
        second = env.scripts.add("换个话题", clarify("请说明问题"))
        await env.app.run_turn(env.ctx(turn="t2", authorized=TOOLS - {SECURITY_TOOL}), second)
        assert len(env.scripts.calls[second]) == 1
