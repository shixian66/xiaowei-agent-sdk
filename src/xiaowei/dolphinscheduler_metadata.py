"""已核实 DS 运行 DAG 和依赖上下文的最小投影，不推导根因或等待时长。"""

import json

from xiaowei.dolphinscheduler_logs import redact_bytes


def _dependency_state(value: object) -> bool:
    return value is None or (
        isinstance(value, str) and value in {"SUCCESS", "WAITING", "FAILED", "NON_EXEC"}
    )


def dependency_fields(task: dict[str, object], token: str = "") -> dict[str, object]:
    """局部状态未知只省略该项；原始参数不进入诊断投影。"""

    def visible(value: str) -> str:
        return redact_bytes(value.encode(), token).decode(errors="replace")[:200]

    result: dict[str, object] = {}
    gaps, omitted = [], 0
    legacy = task.get("dependentResult")
    if legacy:
        try:
            parsed = json.loads(legacy) if isinstance(legacy, str) else legacy
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            states = {}
            omitted += max(0, len(parsed) - 32)
            for key, value in list(parsed.items())[:32]:
                if _dependency_state(value):
                    states[visible(str(key))] = visible(str(value)) if value is not None else None
                else:
                    omitted += 1
                    gaps.append("依赖结果含未识别状态，该项未核实")
            result["dependentResult"] = states
        else:
            gaps.append("旧版依赖结果结构未核实")
    current = task.get("taskInstanceDependentResults")
    if current is not None:
        if not isinstance(current, list):
            gaps.append("依赖上下文列表结构未核实")
        else:
            omitted += max(0, len(current) - 32)
            rows = []
            fields = {
                "contextType",
                "projectCode",
                "workflowDefinitionCode",
                "taskDefinitionCode",
                "dateCycle",
                "dependentResult",
            }
            for row in current[:32]:
                if not isinstance(row, dict) or not _dependency_state(row.get("dependentResult")):
                    omitted += 1
                    gaps.append("依赖结果含未识别状态或结构，该项未核实")
                    continue
                shown = {}
                for key, value in row.items():
                    if key not in fields:
                        continue
                    if value is None or isinstance(value, (str, int, bool)):
                        shown[key] = visible(value) if isinstance(value, str) else value
                    else:
                        gaps.append("依赖上下文字段结构未核实，已省略该字段")
                rows.append(shown)
            result["taskInstanceDependentResults"] = rows
    if omitted:
        result["dependency_results_omitted"] = omitted
    if gaps:
        result["dependency_gaps"] = list(dict.fromkeys(gaps))
    return result


def dag_projection(
    workflow: dict[str, object], version: str, selected: dict[str, object] | None, token: str
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    dag = workflow.get("dagData")
    if not isinstance(dag, dict):
        return [], [], {"gaps": ["本次运行历史 DAG 未提供，未用当前定义代替"]}
    definitions = dag.get("taskDefinitionList")
    relations = dag.get(
        "processTaskRelationList" if version == "3.1.8" else "workflowTaskRelationList"
    )
    gaps = []
    node_total = len(definitions) if isinstance(definitions, list) else None
    edge_total = len(relations) if isinstance(relations, list) else None
    if not isinstance(definitions, list):
        gaps.append("历史 DAG 节点列表结构未核实")
        definitions = []
    if not isinstance(relations, list):
        gaps.append("历史 DAG 关联列表结构未核实")
        relations = []
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
            definitions = [
                definitions[requested],
                *definitions[:requested],
                *definitions[requested + 1 :],
            ]
    nodes = []
    for row in definitions[:100]:
        if not isinstance(row, dict) or any(
            key in row and type(row[key]) is not int for key in ("code", "version")
        ):
            gaps.append("历史 DAG 节点身份字段未核实，已省略该节点")
            continue
        node: dict[str, object] = {}
        for key in ("code", "version", "name", "taskType"):
            if key not in row:
                continue
            value = row[key]
            if key in {"name", "taskType"}:
                if not isinstance(value, str):
                    gaps.append("历史 DAG 节点说明字段未核实，已省略该字段")
                    continue
                value = redact_bytes(value.encode(), token).decode(errors="replace")[:4096]
            node[key] = value
        if (
            selected
            and type(row.get("version")) is int
            and row.get("code") == selected.get("taskCode")
            and row["version"] == selected.get("taskDefinitionVersion")
        ):
            params = row.get("taskParams")
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except ValueError:
                    params = None
            if isinstance(params, dict):
                texts, truncated = {}, False
                for key in ("sql", "rawScript", "preStatements", "postStatements"):
                    value = params.get(key)
                    if isinstance(value, str):
                        masked = redact_bytes(value.encode(), token).decode(errors="replace")
                        texts[key] = masked[:2048]
                        truncated = truncated or len(masked) > 2048
                if texts:
                    node.update(
                        diagnostic_text=texts, text_limit_chars=2048, text_truncated=truncated
                    )
        nodes.append(node)
    edges = []
    fields = ("preTaskCode", "postTaskCode", "preTaskVersion", "postTaskVersion")
    for row in relations[:100]:
        if not isinstance(row, dict) or any(
            key in row and type(row[key]) is not int for key in fields
        ):
            gaps.append("历史 DAG 关联身份字段未核实，已省略该关联")
            continue
        edges.append({key: row[key] for key in fields if key in row})
    return (
        nodes,
        edges,
        {
            "dag_nodes_total": node_total,
            "dag_relations_total": edge_total,
            "dag_nodes_omitted": len(definitions) - len(nodes),
            "dag_relations_omitted": len(relations) - len(edges),
            "gaps": list(dict.fromkeys(gaps)),
        },
    )
