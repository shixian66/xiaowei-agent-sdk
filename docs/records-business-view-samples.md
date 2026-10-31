# rows / records 业务展示修复样例

基线：`58d5e5d3e570249bba58396616fdb99356921d44`；候选：本文件所在提交。
以下均为合成数据，从同一组旧格式 Evidence envelope 读回并经实际 `_shown → _fact → business_text(..., plain_transport=True)` 生成，不调用模型或公司服务。基线源码由固定提交导出；前后使用同一份 [测试夹具](../tests/sdk_core/test_business_result_tables.py)。此处是业务纯文本出口；飞书卡片对应使用表格组件，Web 使用同一业务 presentation。完整原值仍在技术详情。

## 范围与已知例外

- `rows` 与 `records` 只选一个；两者同时存在时优先 `rows`，包括空列表；另一份只保留在技术原文，不重复放入正文。非行对象列表沿原兜底；已有 `rows` 的 Web 嵌套值处理不变。
- DS 项目、运行、任务列表将分页 coverage 变为一行中文说明；最后一页不等于全部结果，时间边界排除、投影省略及截断均不能说“已全部列出”。environment/cluster/kind 不再重复列为正文属性。
- DS 详情与日志仅随通用 `records` 成表变化；详情的 coverage/tasks/nodes/relations/related、日志的 coverage/task 保持原展示，未新增专门展示。
- history 外层 records 成表；按用户确认，嵌套 facts/gaps 仍为 JSON 单元格，顶层 gaps 同样保留。检索/证据标识继续由原有展示投影排除；原 Evidence、模型内容、payload 与投影不变。
- 已复现 Alertmanager `get_alerts` 的 data 和 Grafana `get_annotations` 的 annotations 走“字段｜值 + JSON”兜底。本次只登记，不修复。它们不是“业务正文无 JSON”已通过项。
- Prometheus 非表格 query 沿 [2026-10-22 计划](superpowers/plans/2026-10-22-feishu-input-presentation-repair.md) 的 brief 收起契约，不生成指标表。
- 登记测试覆盖 SQL、DS 六项、history、Alertmanager、Grafana、Prometheus、安全巡检、动作状态、任务及通知；JSON 例外按具体合成值和次数断言，其他整段数组/对象出现即失败，同时断言必要业务值仍可见。动作批准材料中必须展示的实际参数沿既有契约，不以本次代表性状态回执样例声称所有动作参数都已去 JSON。

## ds_list_projects

修复前（列：无；字段/值兜底）：

```text
数据范围：ds-sr-fat · 采集于 2026-10-30T04:00:00+00:00
environment: DS 测试环境
cluster: ds-sr-fat
kind: ds_list_projects
records: [
  {
    "code": 101,
    "name": "数仓调度"
  }
]
coverage: {
  "page_no": 1,
  "page_size": 20,
  "page_start_offset": 0,
  "upstream_total": 1,
  "has_more": false,
  "boundary_excluded": 0,
  "projection_omitted": 0,
  "scope": "指定对象／页"
}
```

修复后（列：code、name）：

```text
数据范围：ds-sr-fat · 采集于 2026-10-30T04:00:00+00:00
覆盖：上游总数 1 条；已全部列出（1 条）；指定对象／页。
第 1 行
code: 101
name: 数仓调度
```

## history/search_operations

修复前（列：无；字段/值兜底）：

```text
数据范围：history · 采集于 2026-10-30T04:00:00+00:00
records: [
  {
    "time": "2026-10-29T12:00:00+00:00",
    "state": "completed",
    "facts": [
      {
        "tool": "local/run_readonly_query",
        "target": "pre",
        "captured_at": "2026-10-29T12:00:00+00:00",
        "data": {
          "columns": [
            "total"
          ],
          "rows": [
            {
              "total": 7
            }
          ],
          "row_count": 1
        },
        "truncated": false
      }
    ],
    "gaps": {}
  }
]
gaps: {}
earliest_at: 2026-10-29T12:00:00+00:00
has_earlier: false
notice: 历史状态不证明当前状态。
```

修复后（列：time、state、facts、gaps）：

```text
数据范围：history · 采集于 2026-10-30T04:00:00+00:00
gaps: {}
earliest_at: 2026-10-29T12:00:00+00:00
has_earlier: false
notice: 历史状态不证明当前状态。
第 1 行
time: 2026-10-29T12:00:00+00:00
state: completed
facts: [
  {
    "tool": "local/run_readonly_query",
    "target": "pre",
    "captured_at": "2026-10-29T12:00:00+00:00",
    "data": {
      "columns": [
        "total"
      ],
      "rows": [
        {
          "total": 7
        }
      ],
      "row_count": 1
    },
    "truncated": false
  }
]
gaps: {}
```

## ds_get_workflow_instance

修复前（列：无；字段/值兜底）：

```text
数据范围：ds-sr-fat · 采集于 2026-10-30T04:00:00+00:00
environment: DS 测试环境
cluster: ds-sr-fat
kind: ds_get_workflow_instance
records: [
  {
    "id": 201,
    "name": "日汇总",
    "state": "FAILURE",
    "projectCode": 101
  }
]
coverage: {
  "projection_omitted": 0,
  "gaps": [],
  "tasks_scope": "指定有效任务；非完整历史尝试",
  "dependency_scope": "运行依赖状态不含等待起止；等待时长需带时间日志，静态 DAG 不证明耗时"
}
tasks: [
  {
    "id": 301,
    "name": "汇总写入",
    "state": "FAILURE",
    "taskType": "SQL"
  }
]
nodes: [
  {
    "code": 401,
    "name": "汇总写入",
    "taskType": "SQL"
  }
]
relations: [
  {
    "preTaskCode": 0,
    "postTaskCode": 401
  }
]
related: []
```

修复后（列：id、name、state、projectCode）：

```text
数据范围：ds-sr-fat · 采集于 2026-10-30T04:00:00+00:00
environment: DS 测试环境
cluster: ds-sr-fat
kind: ds_get_workflow_instance
coverage: {
  "projection_omitted": 0,
  "gaps": [],
  "tasks_scope": "指定有效任务；非完整历史尝试",
  "dependency_scope": "运行依赖状态不含等待起止；等待时长需带时间日志，静态 DAG 不证明耗时"
}
tasks: [
  {
    "id": 301,
    "name": "汇总写入",
    "state": "FAILURE",
    "taskType": "SQL"
  }
]
nodes: [
  {
    "code": 401,
    "name": "汇总写入",
    "taskType": "SQL"
  }
]
relations: [
  {
    "preTaskCode": 0,
    "postTaskCode": 401
  }
]
related: []
第 1 行
id: 201
name: 日汇总
state: FAILURE
projectCode: 101
```

## ds_read_task_log

修复前（列：无；字段/值兜底）：

```text
数据范围：ds-sr-fat · 采集于 2026-10-30T04:00:00+00:00
environment: DS 测试环境
cluster: ds-sr-fat
kind: ds_read_task_log
records: [
  {
    "line": 1,
    "byte_offset": 0,
    "byte_start": 0,
    "byte_end": 27,
    "text": "2026-10-30 ERROR task failed\n",
    "utf8_replacements": false
  }
]
coverage: {
  "read_byte_range": [
    0,
    27
  ],
  "received_bytes": 27,
  "redacted_bytes": 0,
  "reached_end": true,
  "truncation_reason": null,
  "projection_omitted": 0,
  "matches_in_read_range": null
}
task: {
  "project_code": 101,
  "workflow_instance_id": 201,
  "id": 301,
  "state": "FAILURE"
}
```

修复后（列：line、byte_offset、byte_start、byte_end、text、utf8_replacements）：

```text
数据范围：ds-sr-fat · 采集于 2026-10-30T04:00:00+00:00
environment: DS 测试环境
cluster: ds-sr-fat
kind: ds_read_task_log
coverage: {
  "read_byte_range": [
    0,
    27
  ],
  "received_bytes": 27,
  "redacted_bytes": 0,
  "reached_end": true,
  "truncation_reason": null,
  "projection_omitted": 0,
  "matches_in_read_range": null
}
task: {
  "project_code": 101,
  "workflow_instance_id": 201,
  "id": 301,
  "state": "FAILURE"
}
第 1 行
line: 1
byte_offset: 0
byte_start: 0
byte_end: 27
text: 2026-10-30 ERROR task failed

utf8_replacements: false
```

## 验证与后续

相关模块回归、ruff/mypy 和本机 Chrome 的 DS 展示/历史重载检查见 [handoff](../AGENT_HANDOFF.md#records-business-view)。未跑本地全量，未调用真实模型或公司服务；实现提交已由用户确认独立审查通过，CI 与合并状态见 [PR #152](https://github.com/shixian66/xiaowei-agent-sdk/pull/152)。合并部署后，按 AGENTS.md §6 在真实飞书查询一次 `ds-sr-fat` 项目列表，确认表格、中文覆盖、技术详情和原始值。
