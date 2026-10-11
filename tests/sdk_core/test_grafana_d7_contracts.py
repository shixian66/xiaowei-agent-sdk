"""D7 selected conversion/precheck and Action preparation contracts."""

import json
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from xiaowei.actions import ActionWriteRejectedError
from xiaowei.governance import ToolRejectedError
from xiaowei.grafana import (
    DashboardMissingError,
    prepare_dashboard,
    schema_matches,
    wire_arguments,
    wire_payload,
)
from xiaowei.models import ToolRequest


def request(**arguments: object) -> ToolRequest:
    return ToolRequest(
        tool_id="grafana-write/update_dashboard",
        target_id="grafana-write",
        call_id="one",
        tool_name="grafana-write__update_dashboard",
        arguments=arguments,
    )


def args(body: object) -> dict[str, object]:
    return {
        "dashboard_json": json.dumps(body),
        "folder_uid": "",
        "message": "approved",
        "mode": "create",
    }


BODY = {"uid": "host", "title": "Host CPU", "panels": [], "version": 0}


@pytest.mark.parametrize(
    "body",
    [
        {"uid": "host", "title": "v2", "elements": {}, "layout": {}},
        {**BODY, "elements": {}},
        {**BODY, "uid": "../host"},
        {**BODY, "title": ""},
        {**BODY, "panels": {}},
        [],
    ],
)
def test_unsupported_dashboard_rejected_before_io(body: object) -> None:
    with pytest.raises(ToolRejectedError):
        prepare_dashboard(request(**args(body)))


@pytest.mark.parametrize(
    "json_text",
    [
        '{"uid":"host","title":"a","title":"b","panels":[]}',
        '{"uid":"host","title":"a","panels":[],"value":NaN}',
        "not JSON",
    ],
)
def test_bad_json_is_not_silently_normalized(json_text: str) -> None:
    with pytest.raises(ToolRejectedError):
        prepare_dashboard(request(**{**args(BODY), "dashboard_json": json_text}))


def test_full_definition_capacity_is_refusal() -> None:
    with pytest.raises(ToolRejectedError) as error:
        prepare_dashboard(request(**args({**BODY, "description": "x" * 16000})))
    assert error.value.code == "capacity_exceeded"


def test_classic_wire_branch_has_exact_selected_parameters() -> None:
    prepared = prepare_dashboard(request(**args(BODY)))
    assert wire_arguments("update_dashboard", prepared.arguments) == {
        "dashboard": BODY,
        "folderUid": "",
        "message": "approved",
        "overwrite": False,
    }
    assert wire_arguments("get_dashboard_by_uid", {"uid": "host", "version": 0}) == {"uid": "host"}
    assert wire_arguments("get_dashboard_by_uid", {"uid": "host", "version": 7}) == {
        "uid": "host",
        "version": 7,
    }
    assert wire_arguments("list_dashboard_versions", {"uid": "host", "limit": 1, "start": 0}) == {
        "uid": "host",
        "limit": 1,
    }


@pytest.mark.parametrize(
    "name", ["get_dashboard_by_uid", "list_dashboard_versions", "update_dashboard"]
)
def test_only_exact_selected_wire_schema_is_accepted(name: str) -> None:
    schemas = json.loads(Path(__file__).with_name("grafana_d7_schemas.json").read_text())
    assert schema_matches(name, schemas[name])
    drifted = schemas[name]
    drifted["properties"]["bypass"] = {"type": "string"}
    assert not schema_matches(name, drifted)


def result(text: str, *, error: bool = True) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=error)


def test_only_locked_classic_409_is_definitely_not_written() -> None:
    text = "unable to save dashboard: [POST /dashboards/db] postDashboard (status 409): {}"
    with pytest.raises(ActionWriteRejectedError):
        wire_payload(result(text), "update_dashboard", args(BODY))
    for value in (text + " extra", text.replace("409", "500"), text.replace("POST", "GET")):
        with pytest.raises(ValueError):
            wire_payload(result(value), "update_dashboard", args(BODY))


def test_missing_is_bound_to_current_uid_and_specific_status() -> None:
    detail = {
        "code": 404,
        "status": "Failure",
        "reason": "NotFound",
        "details": {
            "name": "host",
            "group": "dashboard.grafana.app",
            "kind": "dashboards",
        },
    }
    text = (
        'get dashboard "host" via k8s api: kubernetes API error: 404 Not Found (HTTP 404): '
        + json.dumps(detail)
    )
    with pytest.raises(DashboardMissingError):
        wire_payload(result(text), "get_dashboard_by_uid", {"uid": "host", "version": 0})
    for fields in ({"uid": "other", "version": 0}, {"uid": "host", "version": 1}):
        with pytest.raises(ValueError):
            wire_payload(result(text), "get_dashboard_by_uid", fields)


def test_full_read_keeps_definition_and_drops_unrelated_metadata() -> None:
    value = {
        "dashboard": BODY,
        "meta": {"folderUid": "hosts", "version": 7, "createdBy": "private"},
        "isV2": False,
    }
    payload = wire_payload(
        result(json.dumps(value), error=False),
        "get_dashboard_by_uid",
        {"uid": "host", "version": 0},
    )
    actual = json.loads(payload["result_json"])
    assert actual == {
        "dashboard": BODY,
        "meta": {"folderUid": "hosts", "version": 7},
        "isV2": False,
    }


@pytest.mark.parametrize("version", [True, 1.0, "1", 0])
@pytest.mark.parametrize(
    "name", ["get_dashboard_by_uid", "list_dashboard_versions", "update_dashboard"]
)
def test_wire_versions_are_positive_integers_not_coerced(name: str, version: object) -> None:
    if name == "get_dashboard_by_uid":
        value = {
            "dashboard": {**BODY, "version": version},
            "meta": {"version": version},
            "isV2": False,
        }
        arguments = {"uid": "host", "version": 1}
    elif name == "list_dashboard_versions":
        value = [{"version": version}]
        arguments = {"uid": "host", "limit": 1, "start": 0}
    else:
        value = {"uid": "host", "status": "success", "version": version}
        arguments = args(BODY)
    with pytest.raises(ValueError):
        wire_payload(result(json.dumps(value), error=False), name, arguments)


def test_locked_mcp_omits_root_folder_but_rejects_wrong_field_type() -> None:
    value = {"dashboard": BODY, "meta": {"version": 1}, "isV2": False}
    payload = wire_payload(
        result(json.dumps(value), error=False),
        "get_dashboard_by_uid",
        {"uid": "host", "version": 0},
    )
    assert json.loads(payload["result_json"])["meta"].get("folderUid", "") == ""
    value["meta"]["folderUid"] = None
    with pytest.raises(ValueError):
        wire_payload(
            result(json.dumps(value), error=False),
            "get_dashboard_by_uid",
            {"uid": "host", "version": 0},
        )


@pytest.mark.parametrize(
    "left,right,same",
    [
        ({"value": 1.0}, {"value": 1}, True),
        ([{"value": True}], [{"value": 1}], False),
        ({"value": 1}, {"value": "1"}, False),
        ({"value": None}, {}, False),
        ([1, 2], [1], False),
    ],
)
def test_readback_compares_json_semantics_without_bool_coercion(left, right, same) -> None:
    from xiaowei.grafana import _same_json

    assert _same_json(left, right) is same
