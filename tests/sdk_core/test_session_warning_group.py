"""S3 group warning uses the shared session, with current actor permissions."""

import pytest
from tests.sdk_core.test_app import cite, tool_call
from tests.sdk_core.test_group_identity import A, B, Env, group_ref
from tests.sdk_core.test_group_identity import env as env

from xiaowei.channel_store import RequestUnavailableError

pytestmark = pytest.mark.loopback


async def test_group_warning_counts_shared_owner_and_new_session_resets(env: Env) -> None:
    for number, actor in enumerate((A, B, A, B), start=1):
        message = env.scripts.add(
            f"成员排障{number}", tool_call("order_total", region="east"), cite()
        )
        record = await env.run(env.group(message, f"om_{number}", actor))
        assert record.state == "completed"
    view = await env.results.view(group_ref("om_4", B), first=True)
    assert view.delivery is not None and view.delivery.session_notice is not None
    assert "4/5" in view.delivery.session_notice and "/新建" in view.delivery.session_notice
    # Shared SDK history does not grant access to another actor's request/delivery record.
    with pytest.raises(RequestUnavailableError):
        await env.results.view(group_ref("om_4", A))
    message = env.scripts.add("A 的下一轮", tool_call("order_total", region="west"), cite())
    last = await env.run(env.group(message, "om_5", A))
    assert last.state == "completed" and last.session_id == record.session_id
    own = await env.results.view(group_ref("om_5", A), first=True)
    assert own.delivery is not None and "5/5" in own.delivery.session_notice
    assert "下轮需要新建" in own.delivery.session_notice
    await env.service.new_session(
        "feishu", B, group_ref("om_4").conversation_id, group=group_ref("om_4").group
    )
    message = env.scripts.add("新建后的线索", tool_call("order_total", region="west"), cite())
    fresh = await env.run(env.group(message, "om_fresh", A))
    assert fresh.session_id != record.session_id
    assert fresh.session_state is not None and fresh.session_state.committed_turns == 1
    fresh_view = await env.results.view(group_ref("om_fresh", A), first=True)
    assert fresh_view.delivery is not None and fresh_view.delivery.session_notice is None
