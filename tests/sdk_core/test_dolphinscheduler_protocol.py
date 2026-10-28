"""版本 HTTP 协议与有界失败，不证明真实 Agent 业务能力。"""

import json
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
    headers = dict(adapter.client.headers)
    await adapter.client.aclose()
    adapter.client = httpx2.AsyncClient(headers=headers, transport=httpx2.MockTransport(handler))


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
        (200, {"code": 10018, "msg": "private-error"}, "ds_object_missing"),
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


async def test_metadata_page_trims_whole_rows_and_reports_projection_gap(adapter):
    await transport(
        adapter,
        lambda _: httpx2.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "total": 10,
                    "totalList": [{"id": i, "name": "a" * 500} for i in range(10)],
                },
            },
        ),
    )
    req = ToolRequest(
        tool_id="local/ds_list_projects",
        target_id="ds-a",
        call_id="large",
        tool_name="ds_list_projects",
        arguments={"cluster": "ds-a", "keyword": None, "page_no": 1, "page_size": 10},
    )
    result = await adapter.execute("ds_list_projects", req, output_limit=2000)
    assert 0 < len(result.payload["records"]) < 10
    assert result.payload["coverage"]["projection_omitted"] > 0
    assert result.truncated
    assert len(json.dumps(result.payload, ensure_ascii=False).encode()) < 1800


async def test_instance_time_window_excludes_inclusive_api_endpoint(adapter):
    await transport(
        adapter,
        lambda _: httpx2.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "total": 2,
                    "totalList": [
                        {"id": 1, "startTime": "2026-10-28 07:59:59"},
                        {"id": 2, "startTime": "2026-10-28 08:00:00"},
                    ],
                },
            },
        ),
    )
    req = ToolRequest(
        tool_id="local/ds_list_workflow_instances",
        target_id="ds-a",
        call_id="time",
        tool_name="ds_list_workflow_instances",
        arguments={
            "cluster": "ds-a",
            "project_code": 11,
            "definition_code": None,
            "state": None,
            "keyword": None,
            "page_no": 1,
            "page_size": 10,
            "start_time": "2026-10-27T20:00:00+08:00",
            "end_time": "2026-10-28T08:00:00+08:00",
        },
    )
    result = await adapter.execute("ds_list_workflow_instances", req)
    assert [r["id"] for r in result.payload["records"]] == [1]
    assert result.payload["coverage"]["boundary_excluded"] == 1
    assert result.payload["coverage"]["upstream_total"] == 2


async def test_http200_false_is_not_numeric_success_code(adapter):
    await transport(adapter, lambda _: httpx2.Response(200, json={"code": False, "data": {}}))
    with pytest.raises(DolphinSchedulerReadError):
        await adapter._json("/projects", {})


@pytest.mark.parametrize("kind", ["metadata", "log"])
async def test_identity_header_and_compression_rejection_before_body_decoding(adapter, kind):
    class Compressed(httpx2.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("must not decode unbounded compressed body")
            yield b""  # pragma: no cover

    def respond(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx2.Response(200, headers={"Content-Encoding": "gzip"}, stream=Compressed())

    await transport(adapter, respond)
    received = []
    with pytest.raises(DolphinSchedulerReadError) as caught:
        if kind == "metadata":
            await adapter._json("/projects", {})
        else:
            from xiaowei.dolphinscheduler import DolphinScheduler

            await DolphinScheduler._download(adapter, 11, 801, 4096, received.append)
    assert caught.value.reason == "ds_invalid_response"
    assert received == []


def test_detail_schema_has_no_dynamic_mode_and_size_limits_are_fixed():
    from xiaowei.dolphinscheduler import DetailArgs

    assert "dynamic_sub_workflows" not in DetailArgs.model_fields
    assert "max_response_bytes" not in DSConfig.model_fields
    assert "max_log_bytes" not in DSConfig.model_fields
