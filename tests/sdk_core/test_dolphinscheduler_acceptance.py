"""S4 fixed business variants through real Web/SDK/PG; scripted choices are not Agent evals."""

import pytest
from tests.sdk_core.test_app import clarify, tool_call
from tests.sdk_core.test_dolphinscheduler_runtime import (
    config_values,
    ds_answer,
    ds_api,
    list_failed,
    source,
)
from tests.sdk_core.test_runtime import env as env

from xiaowei.feishu_render import business_text
from xiaowei.models import Delivery
from xiaowei.runtime import ServeConfig

pytestmark = pytest.mark.loopback


@pytest.mark.parametrize("variant", ["D1-E2", "D1-E3", "D1-E4", "D1-E5", "D1-E9"])
async def test_natural_choice_protocol_preserves_pending_task_and_explicit_scope(
    env, monkeypatch, variant
):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        sources = [
            source("ds-a", url + "/a"),
            source("ds-b", url + "/b"),
            source("ds-c", url + "/c"),
        ]
        config = ServeConfig.model_validate(config_values(env.port, sources))
        async with env.running(config) as served:
            await served.page()
            if variant == "D1-E2":
                message = env.scripts.add("昨天失败任务", clarify("请选择 DS 环境：A、B、C"))
                assert (await served.turn(message, mode="query", request_id="clarify")).json()[
                    "state"
                ] == "completed"
                assert requests == []
            elif variant not in {"D1-E3"}:
                message = env.scripts.add(
                    "查 DS A 昨晚失败任务",
                    tool_call("ds_select_clusters", clusters=["ds-a"]),
                    list_failed("ds-a"),
                    ds_answer,
                )
                assert (await served.turn(message, mode="query", request_id="initial")).json()[
                    "state"
                ] == "completed"
                requests.clear()
            if variant in {"D1-E4", "D1-E9"}:
                text = "换到生产查" if variant == "D1-E4" else "B 以前也出过事"
                message = env.scripts.add(
                    text,
                    clarify(
                        "请明确生产 B 或 C" if variant == "D1-E4" else "仅提及 B，未更改 DS 选择"
                    ),
                )
                assert (await served.turn(message, mode="query", request_id="mention")).json()[
                    "state"
                ] == "completed"
                assert requests == []
                assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") == ["ds-a"]
            cluster = "ds-a" if variant == "D1-E9" else "ds-b"
            message = env.scripts.add(
                "昨晚呢"
                if variant == "D1-E9"
                else ("B" if variant in {"D1-E2", "D1-E4"} else "改查 DS B 昨晚失败任务"),
                *(
                    []
                    if variant == "D1-E9"
                    else [tool_call("ds_select_clusters", clusters=[cluster])]
                ),
                list_failed(cluster),
                ds_answer,
            )
            result = (await served.turn(message, mode="query", request_id="continue")).json()
            assert result["state"] == "completed", result
            assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") == [cluster]
            assert len(requests) == 1 and requests[0][0].startswith("/" + cluster[-1] + "/")
            await served.finish()


@pytest.mark.parametrize("empty", [False, True])
async def test_d1_e10_main_web_shows_actual_environment_even_when_empty(env, monkeypatch, empty):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")

    def respond(path, params):
        return {"code": 0, "data": {"total": 0, "totalList": []}}

    with ds_api(respond=respond if empty else None) as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url)]))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "查 DS B 的失败任务",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                list_failed(),
                ds_answer,
            )
            result = (await served.turn(message, mode="query")).json()
            assert result["state"] == "completed", result
            delivery = Delivery.model_validate(
                {
                    **result["delivery"],
                    "channel": "web",
                    "facts": [
                        {k: v for k, v in fact.items() if k != "presentation"}
                        for fact in result["delivery"]["facts"]
                    ],
                }
            )
            visible = business_text(delivery)
            for text in (visible,):
                assert "生产 B" in text and "ds-b" in text
                assert "采集" in text
            assert len(requests) == 1
            if empty:
                assert delivery.facts[0].metadata["records"] == "[]"
            # Reading the saved result revalidates Evidence, without another DS read.
            view = await served.client.get("/api/turns/r1")
            assert view.status_code == 200, view.text
            assert len(requests) == 1
            await served.finish()


async def test_d4_e2_conflicting_old_object_and_new_session_require_clarification_without_io(
    env, monkeypatch
):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(
            config_values(env.port, [source("ds-a", url + "/a"), source("ds-b", url + "/b")])
        )
        async with env.running(config) as served:
            await served.page()
            for n, cluster in enumerate(("ds-a", "ds-b")):
                message = env.scripts.add(
                    "查 DS " + cluster,
                    tool_call("ds_select_clusters", clusters=[cluster]),
                    list_failed(cluster),
                    ds_answer,
                )
                assert (await served.turn(message, mode="query", request_id=f"read-{n}")).json()[
                    "state"
                ] == "completed"
            requests.clear()
            message = env.scripts.add("刚才那个恢复了吗", clarify("刚才 A 的 701，还是 B 的实例？"))
            result = (await served.turn(message, mode="query", request_id="conflict")).json()
            assert result["state"] == "completed" and requests == []
            assert await env.scalar(
                "SELECT s.ds_clusters FROM xiaowei_session s "
                "JOIN xiaowei_channel_session c USING(session_id) WHERE c.state='current'"
            ) == ["ds-b"]
            response = await served.client.post(
                "/api/sessions",
                json={},
                headers={"origin": str(served.client.base_url).rstrip("/")},
            )
            assert response.status_code == 200, response.text
            message = env.scripts.add("旧任务恢复了吗", clarify("请明确 DS 环境及实例"))
            assert (await served.turn(message, mode="query", request_id="new-session")).json()[
                "state"
            ] == "completed"
            assert requests == []
            assert (
                await env.scalar(
                    "SELECT s.ds_clusters FROM xiaowei_session s "
                    "JOIN xiaowei_channel_session c USING(session_id) WHERE c.state='current'"
                )
                is None
            )
            await served.finish()


@pytest.mark.parametrize("count", [3, 6])
async def test_three_or_six_source_directory_routes_only_explicit_member(env, monkeypatch, count):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        names = [f"ds-{n}" for n in range(count)]
        config = ServeConfig.model_validate(
            config_values(env.port, [source(name, url + "/" + name) for name in names])
        )
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "查 DS " + names[-1],
                tool_call("ds_select_clusters", clusters=[names[-1]]),
                list_failed(names[-1]),
                ds_answer,
            )
            result = (await served.turn(message, mode="query")).json()
            assert result["state"] == "completed", result
            assert len(requests) == 1 and requests[0][0].startswith("/" + names[-1] + "/")
            # DS work method and actual trusted directory reach the same native Agent.
            instructions = env.scripts.calls[message][0].instructions
            assert all(name in instructions for name in names)
            assert "数据库" in instructions and "无固定工具顺序" in instructions
            await served.finish()


@pytest.mark.parametrize("prior_b_evidence", [False, True])
async def test_d4_e3_partial_revocation_preserves_existing_history_boundary(
    env, monkeypatch, prior_b_evidence
):
    from xiaowei.runtime import StaticAccess

    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    revoked = False
    original = StaticAccess.authorize

    async def authorize(self, identity, target_id, tool_id):
        if revoked and target_id == "ds-b":
            return False
        return await original(self, identity, target_id, tool_id)

    monkeypatch.setattr(StaticAccess, "authorize", authorize)
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(
            config_values(env.port, [source("ds-a", url + "/a"), source("ds-b", url + "/b")])
        )
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "比较 DS A B",
                tool_call("ds_select_clusters", clusters=["ds-a", "ds-b"]),
                list_failed("ds-a"),
                *([list_failed("ds-b")] if prior_b_evidence else []),
                ds_answer,
            )
            assert (await served.turn(message, mode="query", request_id="before-revoke")).json()[
                "state"
            ] == "completed"
            requests.clear()
            revoked = True
            follow = env.scripts.add("今天呢", list_failed("ds-a"), list_failed("ds-b"), ds_answer)
            result = (await served.turn(follow, mode="query", request_id="after-revoke")).json()
            assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") == ["ds-a", "ds-b"]
            if prior_b_evidence:
                assert result["state"] == "failed", result
                assert requests == [] and not env.scripts.calls.get(follow)
            else:
                assert result["state"] == "completed", result
                assert len(requests) == 1 and requests[0][0].startswith("/a/")
                # I/O-before authorization rejection is not a DS read failure.
                assert "读取未核实" not in result["delivery"]["content"]
                assert '当前不可用：["ds-b"]' in env.scripts.calls[follow][0].instructions
            await served.finish()


async def test_d1_e8_database_and_ds_choices_stay_independent(env, monkeypatch):
    from tests.sdk_core.test_app import cite

    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url)]))
        async with env.running(config) as served:
            await served.page()
            for request_id, message in (
                ("db-before", "查 StarRocks sr-test 销售额"),
                ("db-after", "再看 StarRocks sr-test 销售额"),
            ):
                if request_id == "db-after":
                    question = env.scripts.add("小海豚有哪些失败任务", clarify("需要查哪套 DS？"))
                    assert (await served.turn(question, mode="query", request_id="ds-ask")).json()[
                        "state"
                    ] == "completed"
                    assert requests == []
                    selected = env.scripts.add(
                        "DS B",
                        tool_call("ds_select_clusters", clusters=["ds-b"]),
                        list_failed(),
                        ds_answer,
                    )
                    assert (await served.turn(selected, mode="query", request_id="ds-read")).json()[
                        "state"
                    ] == "completed"
                env.scripts.add(
                    message,
                    tool_call(
                        "run_readonly_query",
                        cluster="sr-test",
                        sql="SELECT region, total FROM shop.sales",
                    ),
                    cite(),
                )
                result = (await served.turn(message, "query", request_id)).json()
                assert result["state"] == "completed", result
                assert any(f["target_id"] == "sr-test" for f in result["delivery"]["facts"])
            assert len(requests) == 1
            assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") == ["ds-b"]
            await served.finish()


@pytest.mark.parametrize("state,retry", [("SUCCESS", 1), ("FAILURE", 2)])
async def test_d4_fresh_attempt_and_failed_followup_do_not_claim_old_fact_is_current(
    env, monkeypatch, state, retry
):
    from tests.sdk_core.test_dolphinscheduler_dependencies import detail

    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    phase = 0

    def respond(path, params):
        if phase == 2:
            return {"code": 500, "msg": "private failure body must not enter evidence"}
        return {
            "code": 0,
            "data": {
                "id": 701,
                "state": "FAILURE" if phase == 0 else state,
                "runTimes": 1 if phase == 0 else retry,
                "processDefinitionCode": 99,
            },
        }

    with ds_api(respond=respond) as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url)]))
        async with env.running(config) as served:
            await served.page()
            for phase in range(3):
                message = f"DS B 的701恢复了吗（轮{phase}）"
                env.scripts.add(
                    message,
                    *([tool_call("ds_select_clusters", clusters=["ds-b"])] if phase == 0 else []),
                    detail(),
                    ds_answer,
                )
                result = (
                    await served.turn(message, mode="query", request_id=f"fresh-{phase}")
                ).json()
                assert result["state"] == "completed", result
                if phase == 1:
                    fact = result["delivery"]["facts"][-1]
                    assert state in fact["metadata"]["records"]
                    assert str(retry) in fact["metadata"]["records"]
                if phase == 2:
                    content = result["delivery"]["content"]
                    assert "未核实" in content
                    assert "采集时" in content
                    assert "private failure body" not in content
            assert len(requests) == 3 and all(path.endswith("/701") for path, _, _ in requests)
            assert await env.scalar("SELECT count(*) FROM xiaowei_evidence") == 2
            await served.finish()


async def test_current_unavailable_uses_real_grant_revocation_between_turns(env, monkeypatch):
    from xiaowei.runtime import StaticAccess

    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    instances = []
    original_init = StaticAccess.__init__

    def capture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(StaticAccess, "__init__", capture)
    with ds_api() as (url, requests):
        config = ServeConfig.model_validate(config_values(env.port, [source(base_url=url)]))
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "后面查 DS B",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                clarify("已选择 DS B"),
            )
            assert (await served.turn(message, mode="query", request_id="select-only")).json()[
                "state"
            ] == "completed"
            assert requests == []
            assert instances
            for access in instances:
                # Keep the real authorization implementation; revoke actual DS grants only.
                access._grants = {
                    subject: frozenset(t for t in tools if not t.startswith("local/ds_"))
                    for subject, tools in access._grants.items()
                }
            follow = env.scripts.add("今天呢", clarify("DS 生产 B 当前不可用，未查询"))
            result = (
                await served.turn(follow, mode="query", request_id="after-grants-revoked")
            ).json()
            assert result["state"] == "completed", result
            instructions = env.scripts.calls[follow][0].instructions
            assert '本轮 DS 集合：["ds-b"]' in instructions
            assert '当前不可用：["ds-b"]' in instructions
            assert requests == []
            assert await env.scalar("SELECT ds_clusters FROM xiaowei_session") == ["ds-b"]
            await served.finish()
