"""已核实 DS 运行 DAG 和依赖上下文的最小投影，不推导根因或等待时长。"""

import json

from xiaowei.dolphinscheduler_logs import redact_bytes
from xiaowei.governance import DolphinSchedulerReadError


def _dependency_state(value: object) -> bool:
    return value is None or (
        isinstance(value, str) and value in {"SUCCESS", "WAITING", "FAILED", "NON_EXEC"}
    )


def dependency_fields(task: dict[str, object]) -> dict[str, object]:
    """3.1.8 是 JSON 字符串；3.4.0 是 TaskInstanceDependentDetails 的上下文列表。"""
    result: dict[str, object] = {}
    omitted = 0
    legacy = task.get("dependentResult")
    if legacy:
        try:
            parsed = json.loads(legacy) if isinstance(legacy, str) else legacy
        except ValueError:
            raise DolphinSchedulerReadError("ds_invalid_response") from None
        if not isinstance(parsed, dict):
            raise DolphinSchedulerReadError("ds_invalid_response")
        if any(not _dependency_state(v) for v in parsed.values()):
            raise DolphinSchedulerReadError("ds_invalid_response")
        omitted += max(0, len(parsed) - 32)
        result["dependentResult"] = {
            str(k)[:200]: (str(v)[:32] if v is not None else None)
            for k, v in list(parsed.items())[:32]
        }
    current = task.get("taskInstanceDependentResults")
    if current is not None:
        if not isinstance(current, list):
            raise DolphinSchedulerReadError("ds_invalid_response")
        if any(
            not isinstance(row, dict) or not _dependency_state(row.get("dependentResult"))
            for row in current
        ):
            raise DolphinSchedulerReadError("ds_invalid_response")
        omitted += max(0, len(current) - 32)
        fields = {
            "contextType",
            "projectCode",
            "workflowDefinitionCode",
            "taskDefinitionCode",
            "dateCycle",
            "dependentResult",
        }
        result["taskInstanceDependentResults"] = [
            {
                k: v[:200] if isinstance(v, str) else v
                for k, v in row.items()
                if k in fields and (v is None or isinstance(v, (str, int, bool)))
            }
            for row in current[:32]
            if isinstance(row, dict)
        ]
    if omitted:
        result["dependency_results_omitted"] = omitted
    return result


def dag_projection(
    workflow: dict[str, object], version: str, selected: dict[str, object] | None, token: str
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    dag = workflow.get("dagData")
    if not isinstance(dag, dict):
        return [], [], {"gaps": ["本次运行历史 DAG 未提供，未用当前定义代替"]}
    definitions = dag.get("taskDefinitionList")
    relation_key = "processTaskRelationList" if version == "3.1.8" else "workflowTaskRelationList"
    relations = dag.get(relation_key)
    if not isinstance(definitions, list) or not isinstance(relations, list):
        raise DolphinSchedulerReadError("ds_invalid_response")
    ordered = definitions
    if selected is not None:
        requested = next(
            (
                i
                for i, row in enumerate(definitions)
                if isinstance(row, dict)
                and row.get("code") == selected.get("taskCode")
                and row.get("version") == selected.get("taskDefinitionVersion")
            ),
            None,
        )
        if requested is not None:
            ordered = [
                definitions[requested],
                *definitions[:requested],
                *definitions[requested + 1 :],
            ]
    nodes = []
    for row in ordered[:100]:
        if not isinstance(row, dict):
            raise DolphinSchedulerReadError("ds_invalid_response")
        node = {key: row[key] for key in ("code", "version", "name", "taskType") if key in row}
        for key, value in node.items():
            if value is not None and (
                (key in {"code", "version"} and type(value) is not int)
                or (key in {"name", "taskType"} and not isinstance(value, str))
            ):
                raise DolphinSchedulerReadError("ds_invalid_response")
            if isinstance(value, str):
                node[key] = redact_bytes(value.encode(), token).decode(errors="replace")[:4096]
        # 仅该次运行选中节点、且版本一致时附必要文本，不返回整份参数或连接信息。
        if (
            selected
            and row.get("code") == selected.get("taskCode")
            and row.get("version") == selected.get("taskDefinitionVersion")
        ):
            params = row.get("taskParams")
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except ValueError:
                    params = None
            if isinstance(params, dict):
                texts = {}
                text_truncated = False
                for key in ("sql", "rawScript", "preStatements", "postStatements"):
                    value = params.get(key)
                    if isinstance(value, str):
                        masked = redact_bytes(value.encode(), token).decode(errors="replace")
                        texts[key] = masked[:2048]
                        text_truncated = text_truncated or len(masked) > 2048
                if texts:
                    node["diagnostic_text"] = texts
                    node["text_limit_chars"] = 2048
                    node["text_truncated"] = text_truncated
        nodes.append(node)
    if any(
        not isinstance(row, dict)
        or any(
            type(value) is not int
            for key, value in row.items()
            if key in {"preTaskCode", "postTaskCode", "preTaskVersion", "postTaskVersion"}
        )
        for row in relations[:100]
    ):
        raise DolphinSchedulerReadError("ds_invalid_response")
    edges = [
        {
            k: row[k]
            for k in ("preTaskCode", "postTaskCode", "preTaskVersion", "postTaskVersion")
            if k in row
        }
        for row in relations[:100]
        if isinstance(row, dict)
    ]
    return (
        nodes,
        edges,
        {
            "dag_nodes_total": len(definitions),
            "dag_relations_total": len(relations),
            "dag_nodes_omitted": max(0, len(definitions) - 100),
            "dag_relations_omitted": max(0, len(relations) - 100),
        },
    )
