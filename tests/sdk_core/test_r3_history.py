"""R3 brief must survive saved answers, replay and CLI resend with current checks."""

import pytest
from tests.sdk_core.test_evidence_scope import Env, Outbox, ref, saved
from tests.sdk_core.test_evidence_scope import env as env
from tests.sdk_core.test_feishu_render import text_parts
from tests.sdk_core.test_r3_entrypoints import brief

from xiaowei.channel import ResultUnavailableError, ResultUnverifiableError

pytestmark = pytest.mark.loopback


async def make_brief(env, rt):
    await saved(env, rt, "om_original")
    message = env.scripts.add("Only tell me the returned row count", brief)
    record = await env.turn(rt, message, "om_short")
    assert record.state == "completed", record.failure_code
    return record


async def test_saved_brief_replays_and_cli_resends_without_model_or_business_sql(env: Env):
    config = env.config()
    async with env.opened(config) as rt:
        await make_brief(env, rt)
        view = await rt.service.results.view(ref("om_short"))
        assert view.delivery.facts[0].view == "brief"
        assert view.delivery.facts[0].summary == "\u672c\u6b21\u8fd4\u56de\u884c\u6570\uff1a2\u3002"
        outbox = Outbox()
        assert await rt.service.results.send(ref("om_short"), outbox) == "failed"
    requests = sum(len(calls) for calls in env.scripts.calls.values())
    env.db.forget()
    outcome, sends = await env.resend(config, "om_short")
    assert outcome == "sent"
    visible = "\n".join(text_parts(sends[0][1], folded=False))
    assert "\u672c\u6b21\u8fd4\u56de\u884c\u6570\uff1a2\u3002" in visible
    assert "east" not in visible and "east" in "\n".join(text_parts(sends[0][1]))
    assert env.db.business() == []
    assert sum(len(calls) for calls in env.scripts.calls.values()) == requests


@pytest.mark.parametrize("failure", ["revoked", "unreachable"])
async def test_brief_later_delivery_fails_closed_or_recovers_when_recheck_restored(
    env: Env, failure
):
    async with env.opened(env.config()) as rt:
        record = await make_brief(env, rt)
        env.db.forget()
        if failure == "revoked":
            env.db.revoked = {("shop", "sales")}
        else:
            env.db.down = True
        outbox = Outbox()
        error = ResultUnavailableError if failure == "revoked" else ResultUnverifiableError
        with pytest.raises(error):
            await rt.service.results.send(ref("om_short"), outbox)
        assert outbox.sent == [] and env.db.business() == []
        assert (
            await env.scalar(
                "SELECT delivery FROM xiaowei_request WHERE request_key=:key",
                key=record.request_key,
            )
            == "failed"
        )
        if failure == "unreachable":
            env.db.down = False
            view = await rt.service.results.view(ref("om_short"))
            assert view.delivery.facts[0].view == "brief"
            assert env.db.business() == []
