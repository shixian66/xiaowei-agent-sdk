"""版本 HTTP 协议与有界失败，不证明真实 Agent 业务能力。"""

from datetime import UTC, datetime

import httpx2
import pytest

from xiaowei.dolphinscheduler import DSAdapter, DSConfig
from xiaowei.governance import DolphinSchedulerReadError
from xiaowei.models import ToolRequest


@pytest.fixture
async def adapter(monkeypatch):
    monkeypatch.setenv("XW_DS_UNIT_TOKEN", "synthetic-token")
    value = DSAdapter(
        DSConfig(
            cluster_id="ds-a",
            description="测试 A",
            version="3.1.8",
            api_base_url="http://ds.invalid/api",
            token_ref="env:XW_DS_UNIT_TOKEN",  # noqa: S106 - synthetic env reference
            time_zone="Asia/Shanghai",
        ),
        lambda: datetime.now(UTC),
    )
    yield value
    await value.aclose()


async def transport(adapter, handler):
    await adapter.client.aclose()
    adapter.client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


@pytest.mark.parametrize(
    "exc,reason",
    [
        (httpx2.ConnectError, "ds_connect"),
        (httpx2.ConnectTimeout, "ds_connect"),
        (httpx2.ReadTimeout, "ds_timeout"),
        (httpx2.RemoteProtocolError, "ds_unavailable"),
    ],
)
async def test_ds_transport_failure_classification_no_retry(adapter, exc, reason):
    calls = []

    def get(request):
        calls.append(request)
        raise exc("must-not-escape-token-host")

    await transport(adapter, get)
    with pytest.raises(DolphinSchedulerReadError) as caught:
        await adapter._json("/projects", {})
    assert caught.value.reason == reason
    assert len(calls) == 1
    assert "must-not-escape" not in str(caught.value)


@pytest.mark.parametrize(
    "status,body,reason",
    [
        (401, {}, "ds_auth"),
        (403, {}, "ds_auth"),
        (404, {}, "ds_object_missing"),
        (302, {}, "ds_unavailable"),
        (500, {}, "ds_unavailable"),
        (200, {"code": 10018, "msg": "private-error"}, "ds_invalid_response"),
        (200, [], "ds_invalid_response"),
    ],
)
async def test_http_and_business_failures_are_not_success(adapter, status, body, reason):
    calls = []

    def get(request):
        calls.append(request)
        return httpx2.Response(status, json=body, headers={"Location": "http://other.invalid"})

    await transport(adapter, get)
    with pytest.raises(DolphinSchedulerReadError) as caught:
        await adapter._json("/projects", {})
    assert caught.value.reason == reason
    assert len(calls) == 1
    assert "private-error" not in str(caught.value)


async def test_bounded_metadata_reception(adapter):
    await transport(adapter, lambda _: httpx2.Response(200, content=b"x" * 1_048_577))
    with pytest.raises(DolphinSchedulerReadError) as caught:
        await adapter._json("/projects", {})
    assert caught.value.reason == "ds_response_limit"


@pytest.mark.parametrize(
    "version,key", [("3.1.8", "processInstanceId"), ("3.4.0", "workflowInstanceId")]
)
async def test_task_list_maps_actual_parameter_and_preserves_attempts(adapter, version, key):
    adapter.config = adapter.config.model_copy(update={"version": version})
    calls = []

    def get(request):
        calls.append(request)
        return httpx2.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "total": 2,
                    "totalList": [
                        {"id": 1, key: 701, "retryTimes": 0, "flag": "NO", "state": "FAILURE"},
                        {"id": 2, key: 701, "retryTimes": 1, "flag": "YES", "state": "SUCCESS"},
                    ],
                },
            },
        )

    await transport(adapter, get)
    result = await adapter.execute(
        "ds_list_task_instances",
        ToolRequest(
            tool_id="local/ds_list_task_instances",
            target_id="ds-a",
            call_id="c",
            tool_name="ds_list_task_instances",
            arguments={
                "cluster": "ds-a",
                "project_code": 11,
                "workflow_instance_id": 701,
                "state": None,
                "keyword": None,
                "page_no": 1,
                "page_size": 20,
            },
        ),
    )
    assert calls[0].url.params[key] == "701"
    assert [r["retryTimes"] for r in result.payload["records"]] == [0, 1]
    assert len(calls) == 1
