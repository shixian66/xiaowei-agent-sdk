"""S3：两版真实字段夹具经正式 Web/SDK/治理/PG；不注入35分钟等派生结论。"""

import json

import pytest
from tests.sdk_core.test_app import tool_call
from tests.sdk_core.test_dolphinscheduler_runtime import config_values, ds_answer, ds_api, source
from tests.sdk_core.test_runtime import Env
from tests.sdk_core.test_runtime import env as env

from xiaowei.runtime import ServeConfig

pytestmark = pytest.mark.loopback


def protocol(version, *, dependent=False, missing_events=False, dynamic=False):
    definition = "processDefinition" if version == "3.1.8" else "workflowDefinition"
    owner = "processInstanceId" if version == "3.1.8" else "workflowInstanceId"
    code = definition + "Code"

    def respond(path, params):
        if path.endswith("download-log"):
            if dependent:
                return (
                    (
                        b"[2026-10-28 00:00:00] dependency WAITING\n"
                        b"[2026-10-28 00:35:00] dependency SUCCESS\n"
                    )
                    if not missing_events
                    else (b"execution start at 00:35, execution end at 00:40\n")
                )
            return b"SQLSyntaxErrorException SQLState 42S02 code 1146 ods.order_delta missing\n"
        if path.endswith("query-sub-by-parent"):
            return {
                "code": 0,
                "data": {
                    "subProcessInstanceId" if version == "3.1.8" else "subWorkflowInstanceId": 702
                },
            }
        if path.endswith("query-dynamic-sub-workflows"):
            return {
                "code": 0,
                "data": [
                    {
                        "workflowInstanceId": 702,
                        "state": "FAILURE",
                        "parameters": {"password": "must-drop"},
                    },
                    {
                        "workflowInstanceId": 703,
                        "state": "SUCCESS",
                        "parameters": {"password": "must-drop"},
                    },
                ],
            }
        if path.endswith("/701/tasks"):
            row = {
                "id": 801,
                owner: 701,
                "taskCode": 999,
                "taskDefinitionVersion": 1,
                "taskType": "DEPENDENT"
                if dependent
                else ("SUB_PROCESS" if version == "3.1.8" else "SUB_WORKFLOW"),
                "retryTimes": 0,
                "startTime": "2026-10-28 00:35:00",
                "endTime": "2026-10-28 00:40:00",
                "state": "SUCCESS" if dependent else "FAILURE",
            }
            if dependent and version == "3.1.8":
                row["dependentResult"] = '{"22_88_77_day":"SUCCESS"}'
            elif dependent:
                row["taskInstanceDependentResults"] = [
                    {
                        "contextType": "DEPENDENT_RESULT_CONTEXT",
                        "projectCode": 22,
                        "workflowDefinitionCode": 88,
                        "taskDefinitionCode": 77,
                        "dateCycle": "day",
                        "dependentResult": "SUCCESS",
                    }
                ]
            return {"code": 0, "data": {"taskList": [row]}}
        if path.endswith("task-instances"):
            return {
                "code": 0,
                "data": {
                    "total": 1,
                    "totalList": [
                        {
                            "id": 802,
                            owner: 702,
                            "taskCode": 400,
                            "retryTimes": 0,
                            "state": "FAILURE",
                        }
                    ],
                },
            }
        instance = int(path.rsplit("/", 1)[1])
        return {
            "code": 0,
            "data": {
                "id": instance,
                code: 99 if instance == 701 else 199,
                definition + "Version": 2,
                "state": "FAILURE",
                "startTime": "2026-10-28 00:00:00",
                "endTime": "2026-10-28 00:40:00",
                "dagData": {
                    definition: {
                        "code": 99 if instance == 701 else 199,
                        "version": 2,
                        "projectCode": 11,
                    },
                    "taskDefinitionList": [
                        {
                            "code": 999,
                            "name": "await_or_child",
                            "version": 1,
                            "taskType": "DEPENDENT" if dependent else "SUB_WORKFLOW",
                            "taskParams": json.dumps(
                                {"sql": "select * from ods.order_delta", "password": "must-drop"}
                            ),
                        }
                    ],
                    "processTaskRelationList"
                    if version == "3.1.8"
                    else "workflowTaskRelationList": [{"preTaskCode": 999, "postTaskCode": 400}],
                },
            },
        }

    return respond


def detail(task_id=None, *, dynamic=False, instance=701):
    return tool_call(
        "ds_get_workflow_instance",
        cluster="ds-b",
        project_code=11,
        workflow_instance_id=instance,
        task_id=task_id,
        dynamic_sub_workflows=dynamic,
    )


@pytest.mark.parametrize("version", ["3.1.8", "3.4.0"])
async def test_d2_c1_tracks_actual_child_not_later_success(env: Env, monkeypatch, version):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api(respond=protocol(version)) as (url, requests):
        config = ServeConfig.model_validate(
            config_values(env.port, [source(base_url=url, version=version)])
        )
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "DS B 父运行701失败，定位子流程数据库错误",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                detail(801),
                tool_call(
                    "ds_list_task_instances",
                    cluster="ds-b",
                    project_code=11,
                    workflow_instance_id=702,
                    state=None,
                    keyword=None,
                    page_no=1,
                    page_size=100,
                ),
                tool_call(
                    "ds_search_task_log",
                    cluster="ds-b",
                    project_code=11,
                    workflow_instance_id=702,
                    task_id=802,
                    task_page_no=1,
                    terms=["SQLState", "1146"],
                    case_sensitive=False,
                    max_snippets=4,
                    context_lines=1,
                ),
                ds_answer,
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed", body
            text = json.dumps(body["delivery"], ensure_ascii=False)
            assert "ods.order_delta" in text and "702" in text
            assert not any("703" in path for path, _, _ in requests)
            assert "must-drop" not in text
            assert len(requests) <= 6
            await served.finish()


@pytest.mark.parametrize("version", ["3.1.8", "3.4.0"])
@pytest.mark.parametrize("missing_events", [False, True])
async def test_d3_w1_exposes_native_dependency_state_and_real_timestamps_only(
    env, monkeypatch, version, missing_events
):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api(respond=protocol(version, dependent=True, missing_events=missing_events)) as (
        url,
        requests,
    ):
        config = ServeConfig.model_validate(
            config_values(env.port, [source(base_url=url, version=version)])
        )
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "DS B 701 跑40分钟，核对依赖等待依据",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                detail(801),
                tool_call(
                    "ds_search_task_log",
                    cluster="ds-b",
                    project_code=11,
                    workflow_instance_id=701,
                    task_id=801,
                    task_page_no=1,
                    terms=["dependency"],
                    case_sensitive=False,
                    max_snippets=4,
                    context_lines=0,
                ),
                ds_answer,
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed", body
            text = json.dumps(body["delivery"], ensure_ascii=False)
            field = "dependentResult" if version == "3.1.8" else "taskInstanceDependentResults"
            assert field in text
            assert "00:35:00" in text and "00:40:00" in text
            assert ("00:00:00] dependency WAITING" in text) is (not missing_events)
            assert "wait_minutes" not in text and "35分钟" not in text
            assert len(requests) == 3
            await served.finish()


async def test_dynamic_relation_returns_ids_without_guessing_child_project(env, monkeypatch):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    with ds_api(respond=protocol("3.4.0", dynamic=True)) as (url, requests):
        config = ServeConfig.model_validate(
            config_values(env.port, [source(base_url=url, version="3.4.0")])
        )
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "DS B 701 的动态子流程实际关联",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                detail(801, dynamic=True),
                ds_answer,
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed", body
            text = json.dumps(body["delivery"], ensure_ascii=False)
            assert "702" in text and "703" in text and "must-drop" not in text
            assert "未核实" in text
            assert any(path.endswith("query-dynamic-sub-workflows") for path, _, _ in requests)
            assert len(requests) == 3
            await served.finish()


@pytest.mark.parametrize("version", ["3.1.8", "3.4.0"])
async def test_cross_project_child_keeps_independent_parent_fact_and_explicit_gap(
    env, monkeypatch, version
):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    base = protocol(version)
    definition = "processDefinition" if version == "3.1.8" else "workflowDefinition"

    def respond(path, params):
        body = base(path, params)
        if path.endswith("/702"):
            body["data"]["dagData"][definition]["projectCode"] = 22
        return body

    with ds_api(respond=respond) as (url, requests):
        config = ServeConfig.model_validate(
            config_values(env.port, [source(base_url=url, version=version)])
        )
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "DS B 701，先确认父运行再定位跨项目子流程",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                detail(),
                detail(801),
                ds_answer,
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed", body
            text = json.dumps(body["delivery"], ensure_ascii=False)
            assert "701" in text and "生产 B" in text
            assert "子运行" in text and "项目未核实" in text
            assert not any(path.endswith("download-log") for path, _, _ in requests)
            assert len(requests) == 5
            await served.finish()


async def test_history_task_definition_version_mismatch_does_not_expose_wrong_sql(env, monkeypatch):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    base = protocol("3.4.0", dependent=True)

    def respond(path, params):
        body = base(path, params)
        if path.endswith("/701"):
            body["data"]["dagData"]["taskDefinitionList"][0]["version"] = 9
        return body

    with ds_api(respond=respond) as (url, requests):
        config = ServeConfig.model_validate(
            config_values(env.port, [source(base_url=url, version="3.4.0")])
        )
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "DS B 701 节点运行配置",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                detail(801),
                ds_answer,
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed", body
            assert "select * from ods.order_delta" not in json.dumps(
                body["delivery"], ensure_ascii=False
            )
            assert len(requests) == 2
            await served.finish()


async def test_workflow_dag_version_disagrees_with_instance_is_not_accepted(env, monkeypatch):
    monkeypatch.setenv("XW_TEST_DS_TOKEN", "synthetic-token")
    base = protocol("3.4.0", dependent=True)

    def respond(path, params):
        body = base(path, params)
        if path.endswith("/701"):
            body["data"]["dagData"]["workflowDefinition"]["version"] = 9
        return body

    with ds_api(respond=respond) as (url, requests):
        config = ServeConfig.model_validate(
            config_values(env.port, [source(base_url=url, version="3.4.0")])
        )
        async with env.running(config) as served:
            await served.page()
            message = env.scripts.add(
                "DS B 701 的历史配置",
                tool_call("ds_select_clusters", clusters=["ds-b"]),
                detail(),
                ds_answer,
            )
            body = (await served.turn(message, "diagnose")).json()
            assert body["state"] == "completed", body
            assert body["delivery"]["facts"] == []
            assert len(requests) == 1
            await served.finish()


def test_dependency_results_are_status_context_not_arbitrary_payload():
    from xiaowei.dolphinscheduler_metadata import dependency_fields
    from xiaowei.governance import DolphinSchedulerReadError

    with pytest.raises(DolphinSchedulerReadError):
        dependency_fields(
            {"taskInstanceDependentResults": [{"dependentResult": {"password": "must-drop"}}]}
        )


def test_requested_node_beyond_summary_cap_is_still_available():
    from xiaowei.dolphinscheduler_metadata import dag_projection

    nodes = [{"code": n, "version": 1, "name": str(n)} for n in range(150)]
    nodes.append({"code": 999, "version": 1, "taskParams": '{"sql":"select requested_node"}'})
    workflow = {"dagData": {"taskDefinitionList": nodes, "workflowTaskRelationList": []}}
    rows, _, coverage = dag_projection(
        workflow, "3.4.0", {"taskCode": 999, "taskDefinitionVersion": 1}, ""
    )
    assert rows[0]["code"] == 999
    assert rows[0]["diagnostic_text"]["sql"] == "select requested_node"
    assert len(rows) == 100 and coverage["dag_nodes_omitted"] == 51


def test_long_diagnostic_text_discloses_truncation():
    from xiaowei.dolphinscheduler_metadata import dag_projection

    workflow = {
        "dagData": {
            "taskDefinitionList": [
                {"code": 999, "version": 1, "taskParams": json.dumps({"sql": "a" * 3000})}
            ],
            "workflowTaskRelationList": [],
        }
    }
    rows, _, _ = dag_projection(
        workflow, "3.4.0", {"taskCode": 999, "taskDefinitionVersion": 1}, ""
    )
    assert rows[0]["text_truncated"]
    assert len(rows[0]["diagnostic_text"]["sql"]) == 2048


def test_dag_scalar_fields_cannot_carry_nested_raw_parameters():
    from xiaowei.dolphinscheduler_metadata import dag_projection
    from xiaowei.governance import DolphinSchedulerReadError

    workflow = {
        "dagData": {
            "taskDefinitionList": [{"code": 999, "version": 1, "name": {"password": "must-drop"}}],
            "workflowTaskRelationList": [],
        }
    }
    with pytest.raises(DolphinSchedulerReadError):
        dag_projection(workflow, "3.4.0", None, "")
