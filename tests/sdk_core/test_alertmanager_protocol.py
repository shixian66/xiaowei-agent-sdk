"""A4 官方 API v2 与社区候选实测；均为 loopback、合成数据。"""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from agents.mcp import MCPServerStreamableHttp
from mcp.shared.exceptions import MCPError
from tests.sdk_core.alertmanager_fixture import (
    PASSWORD,
    backend,
    official,
    seed,
    wait_ready,
)
from tests.sdk_core.alertmanager_fixture import (
    alertmanager_binary as alertmanager_binary,
)
from tests.sdk_core.test_runtime import free_port

pytestmark = pytest.mark.loopback


def test_official_api_read_auth_and_silence_state(alertmanager_binary: Path) -> None:
    with (
        official(alertmanager_binary) as url,
        httpx.Client(base_url=url, auth=("reader", PASSWORD), trust_env=False, timeout=2) as client,
    ):
        for auth in (None, ("reader", "wrong")):
            assert httpx.get(f"{url}/api/v2/alerts", auth=auth, trust_env=False).status_code == 401
        silence_id = seed(client)
        body = client.get(f"/api/v2/silence/{silence_id}").json()
        assert body["status"]["state"] == "active"
        assert body["matchers"][0]["isEqual"] is True
        assert client.get("/api/v2/silences").json()[0]["id"] == silence_id
        assert client.get("/api/v2/alerts").json()[0]["status"]["silencedBy"] == [silence_id]
        assert client.get("/api/v2/alerts/groups").json()[0]["receiver"]["name"] == "ops"
        assert client.get("/api/v2/receivers").json() == [
            {"name": "ops", "labels": {"name": "ops"}}
        ]
        assert client.get("/api/v2/status").json()["versionInfo"]["version"] == "0.34.1"
        assert (
            client.get(
                "/api/v2/alerts",
                params=[("filter", 'instance="host1"'), ("filter", 'alertname="HostCPU"')],
            ).status_code
            == 200
        )
        assert client.get("/api/v2/alerts", params={"filter": "bad("}).status_code == 400
        assert client.get("/api/v2/silence/not-a-uuid").status_code == 422
        assert client.get("/api/v2/silence/11111111-1111-4111-8111-111111111111").status_code == 404


async def test_community_candidate_http_incompatibility_and_paged_error_loss() -> None:
    python = os.environ.get("XW_TEST_ALERTMANAGER_COMMUNITY_PYTHON")
    root = os.environ.get("XW_TEST_ALERTMANAGER_COMMUNITY_ROOT")
    if not python or not root:
        pytest.skip("需要社区 4a653a7 的隔离 mcp 1.8.1 环境")
    with backend() as (api, state):
        port = free_port()
        process = subprocess.Popen(  # noqa: S603 - 明确选择的仓库外协议实验环境
            [
                python,
                str(Path(root) / "src/alertmanager_mcp_server/server.py"),
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=root,
            env={
                **os.environ,
                "ALERTMANAGER_URL": api,
                "ALERTMANAGER_USERNAME": "reader",
                "ALERTMANAGER_PASSWORD": PASSWORD,
                "MCP_API_KEY": "synthetic-token",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            url = f"http://127.0.0.1:{port}/mcp/"
            await asyncio.to_thread(wait_ready, url, process)
            for token in (None, "wrong"):
                headers = {} if token is None else {"authorization": f"Bearer {token}"}
                async with httpx.AsyncClient(trust_env=False) as client:
                    assert (await client.post(url, headers=headers, json={})).status_code == 401
            assert state.requests == []
            with pytest.raises(ExceptionGroup) as failure:
                async with MCPServerStreamableHttp(
                    params={"url": url, "headers": {"authorization": "Bearer synthetic-token"}},
                    max_retry_attempts=0,
                ):
                    pytest.fail("候选 HTTP 未满足锁版 SDK 协议")

            def leaves(error: BaseException) -> list[BaseException]:
                if isinstance(error, BaseExceptionGroup):
                    return [leaf for child in error.exceptions for leaf in leaves(child)]
                return [error]

            errors = leaves(failure.value)
            assert len(errors) == 1
            assert isinstance(errors[0], MCPError)
            assert errors[0].code == -32000
            assert errors[0].message == "SSE stream ended without a response"
            assert state.requests == []
            # 候选 HTTP 不可接入；另在其原环境调用未修改的公开读取函数，核对分页根因。
            script = """
import asyncio, json, sys
sys.path.insert(0, sys.argv[1] + '/src')
from alertmanager_mcp_server import server
async def probe():
    try:
        await server.get_alerts(count=1, offset=0)
    except Exception as error:
        paged = {'type': type(error).__name__, 'message': str(error)}
    else:
        paged = {'type': 'success'}
    print(json.dumps({'paged': paged, 'status': await server.get_status()}))
asyncio.run(probe())
"""
            for code in (401, 500):
                state.status = code
                result = await asyncio.to_thread(
                    subprocess.run,
                    [python, "-c", script, root],
                    env={
                        **os.environ,
                        "ALERTMANAGER_URL": api,
                        "ALERTMANAGER_USERNAME": "reader",
                        "ALERTMANAGER_PASSWORD": PASSWORD,
                    },
                    capture_output=True,
                    timeout=5,
                    check=True,
                )
                body = json.loads(result.stdout)
                assert body["paged"] == {"type": "KeyError", "message": "slice(0, 1, None)"}
                assert str(code) in body["status"]["error"]
            assert len(state.requests) == 4
        finally:
            process.terminate()
            await asyncio.to_thread(process.wait, 5)
