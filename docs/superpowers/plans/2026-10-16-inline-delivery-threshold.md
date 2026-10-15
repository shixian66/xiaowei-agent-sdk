# 回答留在对话：飞书平台门槛与预览修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新语义飞书交付以飞书平台限制判断是否转附件；转附件时正文仍先给回答和准确预览；MD 文件对真实数据输出可读表格。

**Architecture:** 只改展示/发送层（`feishu_render`、`feishu_delivery`、`feishu_report`）。SDK Agent Loop、提示词、工具、治理、Evidence、Session、迁移与依赖均不变。新行为只经 `delivery_version == 1` 的 `send_complete_delivery` 生效；旧 v0 请求与审批材料卡沿原容量规则。

**Tech Stack:** Python 3.11、锁定版 `lark_channel`（Channel SDK）、`markdown-it-py`（仅测试）、pytest、隔离测试 PostgreSQL。

**Spec:** [ARCHITECTURE.md §自然回答与完整结果交付](../../../ARCHITECTURE.md#result-delivery)（本计划 Task 5 修订其中“文件与容量”一段）；问题来源为 2026-10-16 生产对话复盘（下文 §1）。

## Global Constraints

- 飞书官方限制（2026-10-16 核对 open.feishu.cn）：卡片与富文本（post）请求体最大 30 KB；文本消息请求体最大 150 KB；卡片 JSON 2.0 每张最多 200 个元素或组件；每张卡片最多 5 个表格组件，每表最多 50 列。
- 本地字节预算保持 `MAX_REQUEST_BYTES = 30_000`（按 SDK 内层 JSON 字符串与外层再次转义实测，低于 30 KB = 30,720 字节）；组件预算保持 200；不提高、不新增其他数值。
- `max_reply_chars` 配置校验保持 200–3500，含义收窄为：纯文本消息（`{"text": ...}`）的字符上限与 SDK `text_chunk_limit`。锁版 SDK 只对 `OutboundText` 与 markdown 源 post 分段，预建 post AST 与卡片原样发送（`lark_channel/channel/outbound/sender.py:550-597`）。
- 新语义数据卡片/预建 post：只受 30,000 字节与 200 组件约束，不再受 `max_reply_chars` 约束。
- 不改：旧 v0 交付、审批材料卡容量、提示词与工具说明、Evidence/投影字段、数据库表、依赖与锁文件、单文件 400,000 字节拆分规则。
- 实际 SQL 在对话卡片中仍为必需项；放不下时沿既有“SQL 未展示。”说明。
- 不新增模型可提供可信事实的入口；表格、行数、预览范围全部由代码从获准投影生成。

## Review Focus

1. 结果字节合规但组件超过 200（例如 60 列 × 250 行宽表按逐行文本展示）——必须转附件并给准确预览，不能因只看字节而发送被平台拒绝的卡片。→ Task 2 `test_element_budget_still_forces_attachment`。
2. 固定回执、会话提示等纯文本消息超过 `max_reply_chars`——仍按原上限处理，不能交给 SDK 自动分段成多条。→ Task 1 `test_plain_text_keeps_reply_char_limit`。
3. 含大整数（>2^53）或嵌套值的结果，且“表格放得下、表格加原文放不下”——可见单元格已字符串化，原文 JSON 仍为必需项，必须换附件，不能以“重复”为由内联并丢掉类型。→ Task 1 `test_lossy_fact_keeps_raw_json_required`（大整数、嵌套各一）。
4. MD 单元格或列名含换行、首尾空格、反引号、竖线、`<at>`、URL、`__强调__`、U+2028——渲染时不得跳出表格或产生链接、列表、强调、HTML、mention，值与列名可无损还原；表下明细标题同样适用。→ Task 4 `test_md_table_cells_are_inert_and_reversible`、`test_md_table_headers_and_detail_titles_are_inert_and_reversible`。
5. 用户已在旧 3500 规则下保存的分项被手动补发——新检查必须接受（规则只放宽），不能因模板变化拒发。→ Task 2 `test_old_saved_body_part_still_passes_capacity_check`。

---

## 1. 问题与根因（2026-10-16 复盘，基线 `eda4a0b`）

| 编号 | 用户可见现象 | 根因（代码位置为基线行号） |
| --- | --- | --- |
| T | 普通查询、主机列表、看板搜索几乎都进附件 | 新语义正文按 `max_reply_chars`（≤3500，且折叠区计入）判断；数据卡片还强制带与表格重复的“原文 JSON”（`feishu_render.py:491` 的 `all_technical`），实际可用空间约一千多字，远低于平台 30 KB |
| A | 正文写“预览 0／本次返回 3 行”又写“（无数据行）” | `_preview`（`feishu_delivery.py:44-90`）先截 `fact.rows` 再以 `allow_preview=False` 渲染，渲染器看到空行而输出“（无数据行）”（`feishu_render.py:236-239`），与计划 D1a 验收 5“无行可容纳时说明预览不可展示”冲突 |
| C（本切片部分修复） | MCP 小结果（主机、看板）正文“预览无法在本消息容量内展示” | 同一 `_preview` 只截 `rows`；MCP 结果的数据在 `metadata`，截不到，所有候选大小相同。本切片后：合平台限制的 MCP 结果直接内联；多字段结果超限时按字段预览。**单个大字段（如投影里的 `dashboards` 字符串）超限时仍显示“预览 0／共 N 个字段”、没有任何条目**，需 MCP 行视图解决，不在本切片宣称修复 |
| D | 转附件时正文没有模型回答 | `_preview` 设置 `"analysis": ()`（`feishu_delivery.py:61`）；渲染器原有预览顺序也是先装满行，再装分析 |
| B | 用户要求 MD 表格仍是逐行 JSON | `_SIMPLE_MD`（`feishu_report.py:239`）白名单不允许 `:`、`.`、`/`、换行等，时间戳+IP 两列也无法成表 |

离线复现（基线代码，合成数据）：A、B、C 均复现；HTML 对 SQL 结果有表格，对 MCP 结果无表格。

**本切片不处理（记入 handoff）：** MCP 结果的结构化行视图（Prometheus/Grafana 结果在文件和卡片中按表格展示）；busy/evidence_failed 回执细分；R3/R4/R9/R15 需要生产日志才能定根因。

## 2. Agent 视角检查

- **任务：** 用户在飞书问“fat 最近 3 小时最慢的 3 条查询”“fat 有哪些主机”“找 Node Exporter 看板”，答案与结果直接出现在对话；超出飞书限制时附件给完整结果，正文仍有回答与准确的“预览 N／本次返回 M 行”；明确要 MD 时得到可读表格。
- **SDK 原生：** 继续使用锁版 SDK 公开发送；不依赖 SDK 隐式分段（已核实只对纯文本/markdown post 生效）。没有新增编排、重试或会话逻辑。
- **工具：** 工具集合与契约不变。
- **自我修正 / 澄清 / 工作方法：** 不涉及模型行为；提示词不变。
- **评估：** 离线证明交付选择与渲染；真实模型任务样例与飞书客户端显示列为未验证缺口（Task 5）。
- **体验：** 正文先回答；预览范围由代码标明；原文 JSON 未展示时有固定说明。

## 3. 文件结构

| 文件 | 本次职责变化 |
| --- | --- |
| `src/xiaowei/feishu_render.py` | `build_feishu_message` 增加 `platform_limits` 关键字：卡片/post 不计字符上限、回答优先的预览顺序、可无损替代的原文 JSON 改为可选；`_card` 增加预览范围与原文未展示说明 |
| `src/xiaowei/feishu_delivery.py` | `_preview` 改为复用渲染器预览；`_data_parts`、`_check_capacity` 对数据部分启用平台限制，审批材料保持原规则 |
| `src/xiaowei/feishu_report.py` | `_md_fact` 对任意有列结果输出表格：安全值直出，其余放转义代码片段，多行/过长值放表下 |
| `tests/sdk_core/test_inline_delivery.py`（新建） | Task 1–3 的渲染与交付选择回归 |
| `tests/sdk_core/test_feishu_report_layout.py` | Task 4 的 MD 表格回归；更新两条旧断言（说明见 Task 4） |
| `ARCHITECTURE.md`、`deploy/OPERATIONS.md`、`AGENT_HANDOFF.md` | Task 5 契约与状态 |

---

### Task 1: 渲染器支持平台限制与回答优先预览

**Files:**
- Modify: `src/xiaowei/feishu_render.py:150-312`（`_card`）、`:366-571`（`build_feishu_message`）
- Test: `tests/sdk_core/test_inline_delivery.py`（新建）

**Interfaces:**
- Produces: `build_feishu_message(delivery, max_chars, chat_id, *, reply_to=None, max_bytes=MAX_REQUEST_BYTES, allow_preview=True, platform_limits=False) -> Message`。`platform_limits=True` 时：(1) 卡片与 post 只检查字节与组件，`{"text": ...}` 仍检查 `max_chars`；(2) 完整卡片中只有“不可无损替代”的原文 JSON 是必需项；(3) 预览先放模型分析，再放行与 SQL。
- Produces: 常量 `RAW_OMITTED: Final = "原文 JSON 未展示（单消息容量）；如需精确原值与类型，可要求 MD/HTML 文件。"`
- Produces: `_card(..., raw_hidden: AbstractSet[int] = frozenset())`，只控制原文 JSON 是否以 `RAW_OMITTED` 代替；`row_count`、`elapsed_ms`、`next_cursor` 等技术字段仍由 `technical` 控制，在完整判断中始终必需。
- Produces: `_raw_redundant(fact) -> bool`：可见行/字段逐值等同原文（按 JSON 文本比较）时为 True。
- Produces: 每个部分展示的事实在结果标题后有 `预览 {n}／本次返回 {m} 行`（有列）或 `预览 {n}／共 {m} 个字段`（无列）。

- [ ] **Step 1: 写失败测试**

新建 `tests/sdk_core/test_inline_delivery.py`：

```python
"""新语义飞书交付：平台限制门槛、回答优先预览与准确的预览范围。"""

import json
from datetime import UTC, datetime

import pytest

from xiaowei.feishu_render import (
    RAW_OMITTED,
    MessageCapacityError,
    build_feishu_message,
    request_bytes,
)
from xiaowei.channel_store import DeliveryPart
from xiaowei.feishu_delivery import _check_capacity, _data_parts, _preview
from xiaowei.models import AnswerInference, Delivery, DeliveryFact

CHAT = "oc_inline"


def fact(columns, rows, *, metadata=None, raw=None, tool_id="local/run_readonly_query"):
    metadata = {"sql": "SELECT 1"} if metadata is None else metadata
    if raw is None:
        raw = json.dumps({**metadata, "columns": list(columns), "rows": list(rows)}, ensure_ascii=False)
    return DeliveryFact(
        evidence_id="ev_inline",
        tool_id=tool_id,
        target_id="fat",
        captured_at=datetime(2026, 10, 15, tzinfo=UTC),
        truncated=False,
        columns=tuple(columns),
        rows=tuple(rows),
        metadata=metadata,
        result_json=raw,
    )


def delivery(*facts, analysis="最慢的查询来自 etl 用户。"):
    return Delivery(
        content="结果",
        evidence_ids=("ev_inline",),
        channel="feishu",
        facts=facts,
        analysis=(AnswerInference(text=analysis, evidence_ids=("ev_inline",)),) if analysis else (),
    )


def audit_rows(count, stmt_chars):
    return [
        {"queryId": f"q-{i}", "user": "etl", "duration_ms": 120000 + i, "sql_text": "S" * stmt_chars}
        for i in range(count)
    ]


def shown(message) -> str:
    return json.dumps(message, ensure_ascii=False)


def test_medium_result_is_inline_under_platform_limits_but_not_under_reply_chars() -> None:
    d = delivery(fact(("queryId", "user", "duration_ms", "sql_text"), audit_rows(3, 1500)))
    with pytest.raises(MessageCapacityError):
        build_feishu_message(d, 3500, CHAT, allow_preview=False)
    message = build_feishu_message(d, 3500, CHAT, allow_preview=False, platform_limits=True)
    assert "card" in message and request_bytes(message, CHAT) <= 30_000
    assert "S" * 1500 in shown(message) and "预览" not in shown(message)
    assert "最慢的查询来自 etl 用户。" in shown(message) and "SELECT 1" in shown(message)


def test_redundant_raw_json_is_optional_but_technical_fields_stay() -> None:
    # 6×3000 字：只放可见行约 18 KB 可容纳；再带一份原文 JSON 则超过 30,000 字节。
    rows = audit_rows(6, 3000)
    metadata = {"sql": "SELECT 1", "row_count": 6, "elapsed_ms": 1234567}
    d = delivery(fact(("queryId", "user", "duration_ms", "sql_text"), rows, metadata=metadata))
    message = build_feishu_message(d, 3500, CHAT, allow_preview=False, platform_limits=True)
    text = shown(message)
    assert text.count("S" * 3000) == 6  # 每行一次，没有原文 JSON 的第二份
    assert RAW_OMITTED in text
    # 省略的只是重复的原文 JSON；其他获准技术字段仍完整。
    assert "elapsed_ms: 1234567" in text and "row_count: 6" in text


def _lossy_rows(kind: str):
    """可见单元格与原值类型不同（大整数→字符串、嵌套→JSON 文本），且带足够长的填充。"""
    big = 2**60
    if kind == "bigint":
        shown_rows = [{"id": str(big + i), "sql_text": "S" * 3000} for i in range(6)]
        raw_rows = [{"id": big + i, "sql_text": "S" * 3000} for i in range(6)]
    else:
        shown_rows = [{"tags": json.dumps(["a", i]), "sql_text": "S" * 3000} for i in range(6)]
        raw_rows = [{"tags": ["a", i], "sql_text": "S" * 3000} for i in range(6)]
    columns = tuple(shown_rows[0])
    raw = json.dumps({"sql": "SELECT 1", "columns": list(columns), "rows": raw_rows})
    return columns, shown_rows, raw


@pytest.mark.parametrize("kind", ["bigint", "nested"])
def test_lossy_fact_keeps_raw_json_required(kind: str) -> None:
    # 表格单独约 18 KB 放得下，表格加原文放不下：原文不能省，整体必须换附件。
    columns, rows, raw = _lossy_rows(kind)
    d = delivery(fact(columns, rows, raw=raw))
    with pytest.raises(MessageCapacityError):
        build_feishu_message(d, 3500, CHAT, allow_preview=False, platform_limits=True)
    *files, body = _data_parts(d, 0, 3500, CHAT, None)
    assert files and RAW_OMITTED not in body.body.decode()


def test_plain_text_keeps_reply_char_limit() -> None:
    receipt = Delivery(
        content="会" * 300, evidence_ids=(), channel="feishu", session_notice="提示" * 10
    )
    with pytest.raises(ValueError):
        build_feishu_message(receipt, 200, CHAT, allow_preview=False, platform_limits=True)


def test_preview_keeps_answer_first_and_states_exact_range() -> None:
    d = delivery(
        fact(("queryId", "user", "duration_ms", "sql_text"), audit_rows(3, 12_000)),
        analysis="结论：三条都是 etl 的批量写入。",
    )
    message = build_feishu_message(d, 3500, CHAT, platform_limits=True)
    text = shown(message)
    assert "结论：三条都是 etl 的批量写入。" in text
    assert "预览 2／本次返回 3 行" in text or "预览 1／本次返回 3 行" in text
    assert "（无数据行）" not in text


def test_zero_row_preview_never_claims_no_data() -> None:
    d = delivery(fact(("sql_text",), ({"sql_text": "S" * 40_000},)), analysis=None)
    message = build_feishu_message(d, 3500, CHAT, platform_limits=True)
    text = shown(message)
    assert "（无数据行）" not in text
    assert "预览 0／本次返回 1 行" in text and "结果行未展示。" in text


def test_metadata_fact_preview_counts_fields() -> None:
    metadata = {f"k{i}": "v" * 3000 for i in range(12)}
    raw = json.dumps(metadata)
    d = delivery(fact((), (), metadata=metadata, raw=raw, tool_id="grafana/search"), analysis=None)
    message = build_feishu_message(d, 3500, CHAT, platform_limits=True)
    text = shown(message)
    assert "共 12 个字段" in text and "预览" in text


def test_single_large_metadata_field_preview_is_honest_but_empty() -> None:
    # 已知边界（本切片不做 MCP 行视图）：看板列表通常是一个字符串字段，超限时预览不到条目。
    metadata = {"dashboards": json.dumps([{"title": "N" * 100}] * 400)}
    raw = json.dumps({"dashboards": json.loads(metadata["dashboards"])})
    d = delivery(fact((), (), metadata=metadata, raw=raw, tool_id="grafana/search"), analysis=None)
    text = shown(build_feishu_message(d, 3500, CHAT, platform_limits=True))
    assert "预览 0／共 1 个字段" in text and "结果行未展示。" in text
    assert "（无数据行）" not in text
```


- [ ] **Step 2: 运行并确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/sdk_core/test_inline_delivery.py -p no:cacheprovider --tb=short`
Expected: 收集失败 `ImportError: cannot import name 'RAW_OMITTED'`；去掉该导入单独试时 `TypeError: ... unexpected keyword argument 'platform_limits'`。

- [ ] **Step 3: 实现**

在 `feishu_render.py` 常量区（`BRIEF_ORIGINAL_OMITTED` 之后）增加：

```python
RAW_OMITTED: Final = "原文 JSON 未展示（单消息容量）；如需精确原值与类型，可要求 MD/HTML 文件。"


def _raw_redundant(fact: DeliveryFact) -> bool:
    """可见表格/字段已逐值保留原类型时，原文 JSON 只是同一数据的第二份。"""
    if fact.result_json is None or fact.view == "brief":
        return False
    data = json.loads(fact.result_json)
    if not isinstance(data, dict):
        return False

    def same(left: object, right: object) -> bool:
        # 按 JSON 文本比较，区分 true/1、大整数与其字符串、嵌套值与其 JSON 文本。
        return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(
            right, sort_keys=True, ensure_ascii=False
        )

    if fact.columns:
        if not same(data.get("rows"), [dict(row) for row in fact.rows]):
            return False
        data = {k: v for k, v in data.items() if k not in ("rows", "columns")}
    return same(data, fact.metadata)
```

在 `_card` 的事实循环中，紧接 `elements.append(_div("SQL 未展示。"))` 所在的 `if fact.tool_id == "local/run_readonly_query" and index not in sqls:` 块之后、`if fact.view == "brief":` 之前插入（`rows` 与 `all_rows` 已在循环开头计算；审批材料分支在此之前已 `continue`）：

```python
        if omissions and fact.view != "brief" and len(rows) < len(all_rows):
            elements.append(
                _div(
                    f"预览 {len(rows)}／本次返回 {len(all_rows)} 行"
                    if fact.columns
                    else f"预览 {len(rows)}／共 {len(all_rows)} 个字段"
                )
            )
```

给 `_card` 增加只控制原文 JSON 的关键字参数（不复用 `technical`，`row_count`、`elapsed_ms`、`next_cursor` 等技术字段仍由 `technical` 决定）：

```python
def _card(
    delivery: Delivery,
    counts: Sequence[int],
    sqls: set[int],
    analysis: Sequence[AnswerInference],
    technical: set[int],
    *,
    omissions: bool = True,
    formatted: bool = False,
    raw_hidden: AbstractSet[int] = frozenset(),
) -> Message:
```

（导入 `from collections.abc import Sequence, Set as AbstractSet`。）依据面板中把

```python
            if fact.result_json is not None:
                details.append(_div(f"原文 JSON:\n{fact.result_json}"))
```

替换为：

```python
            if fact.result_json is not None:
                # 只有可由可见值无损替代的原文才会被列入 raw_hidden；省略时如实说明。
                details.append(
                    _div(RAW_OMITTED)
                    if index in raw_hidden
                    else _div(f"原文 JSON:\n{fact.result_json}")
                )
```

修改 `build_feishu_message` 签名与 `fits`：

```python
def build_feishu_message(
    delivery: Delivery,
    max_chars: int,
    chat_id: str,
    *,
    reply_to: str | None = None,
    max_bytes: int = MAX_REQUEST_BYTES,
    allow_preview: bool = True,
    platform_limits: bool = False,
) -> Message:
    ...
    def fits(message: Message) -> bool:
        # 平台限制：卡片/预建 post 只受请求字节与组件约束；纯文本仍会被 SDK 按字符分段。
        chars_ok = (
            _chars(message) <= max_chars
            if not platform_limits or set(message) == {"text"}
            else True
        )
        return (
            chars_ok
            and _elements(message) <= 200
            and request_bytes(message, chat_id, reply_to=reply_to) <= max_bytes
        )
```

在 `full = _card(...)` 的 `if fits(full): ... return` 之后、`if not allow_preview:` 之前插入：

```python
    if platform_limits:
        # 行、实际 SQL、分析、全部技术字段与不可替代的原文都完整才算完整消息；
        # 只有能由可见值无损替代的原文 JSON 可省略，放得下的仍逐条恢复展示。
        hidden = {i for i, f in enumerate(delivery.facts) if _raw_redundant(f)}

        def core(raw_hidden: set[int], *, formatted: bool = False) -> Message:
            return _card(
                delivery,
                totals,
                all_sql,
                delivery.analysis,
                all_technical,
                formatted=formatted,
                raw_hidden=raw_hidden,
            )

        if hidden and fits(core(hidden)):
            for index in sorted(hidden):
                if fits(core(hidden - {index})):
                    hidden = hidden - {index}
            plain = core(hidden)
            formatted = core(hidden, formatted=True)
            return formatted if fits(formatted) else plain
```

预览部分改为回答优先：把现有 `for item in delivery.analysis:` 循环抽成局部函数，并按 `platform_limits` 决定顺序。将 `sqls: set[int] = set()` 起到分析循环结束的代码替换为：

```python
    sqls: set[int] = set()

    def fit_analysis(counts: list[int]) -> None:
        for item in delivery.analysis:
            candidate = _card(delivery, counts, sqls, [*analysis, item], technical)
            if fits(candidate):
                analysis.append(item)
                continue
            low, high = 0, len(item.text)
            while low < high:
                mid = (low + high + 1) // 2
                short = item.model_copy(update={"text": item.text[:mid]})
                if fits(_card(delivery, counts, sqls, [*analysis, short], technical)):
                    low = mid
                else:
                    high = mid - 1
            if low:
                analysis.append(item.model_copy(update={"text": item.text[:low]}))
            break

    if platform_limits:
        # 正文先回答：分析先占预算，再放整行与实际 SQL。
        fit_analysis([0] * len(totals))
    counts = fitting_prefix(sqls)
    if counts is None:
        return omitted_message()
    for index in sorted(all_sql):
        selected_counts = fitting_prefix(sqls | {index})
        if selected_counts is not None:
            sqls.add(index)
            counts = selected_counts
    if not platform_limits:
        fit_analysis(counts)
```

（`fitting_prefix` 通过闭包读取 `analysis`，因此回答优先时行数搜索已扣除分析占用。）

- [ ] **Step 4: 运行新测试**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/sdk_core/test_inline_delivery.py -p no:cacheprovider --tb=short`
Expected: 10 passed（`test_lossy_*` 为参数化 2 项）。

- [ ] **Step 5: 运行受影响的渲染回归**

Run: `SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/sdk_core/test_feishu_format.py tests/sdk_core/test_feishu.py -p no:cacheprovider --tb=short`
Expected: 全部通过。旧路径 `platform_limits=False` 的唯一预期输出变化是预览卡新增的“预览 N／本次返回 M 行”（`raw_hidden` 默认为空，旧路径不出现 `RAW_OMITTED`）；若有用例因此失败，只为这一行补充期望，其他断言不改、不删。

- [ ] **Step 6: Commit**

```bash
git add src/xiaowei/feishu_render.py tests/sdk_core/test_inline_delivery.py
git commit -m "fix(feishu): 平台限制门槛与回答优先预览的渲染支持"
```

---

### Task 2: 新语义交付启用平台限制

**Files:**
- Modify: `src/xiaowei/feishu_delivery.py:44-139`（`_preview`、`_data_parts`、`_check_capacity`）、`:312-322`（两处 `_check_capacity` 调用）
- Test: `tests/sdk_core/test_inline_delivery.py`

**Interfaces:**
- Consumes: Task 1 的 `build_feishu_message(..., platform_limits=True)`、`RAW_OMITTED`。
- Produces: `_check_capacity(parts, max_chars, chat_id, reply_to, *, platform_limits: bool) -> None`；`_data_parts(...)` 与 `_preview(...)` 签名不变。

- [ ] **Step 1: 写失败测试**

追加到 `tests/sdk_core/test_inline_delivery.py`：

```python
def test_medium_result_without_file_request_has_no_attachment() -> None:
    d = delivery(fact(("queryId", "user", "duration_ms", "sql_text"), audit_rows(3, 1500)))
    (body,) = _data_parts(d, 0, 3500, CHAT, None)
    assert body.kind == "body"
    assert "S" * 1500 in body.body.decode()


def test_oversized_result_gets_file_and_body_with_answer_and_exact_preview() -> None:
    d = delivery(
        fact(("queryId", "user", "duration_ms", "sql_text"), audit_rows(3, 12_000)),
        analysis="结论：三条都是 etl 的批量写入。",
    )
    *files, body = _data_parts(d, 0, 3500, CHAT, None)
    assert [f.kind for f in files] == ["md"]
    text = body.body.decode()
    assert "本次完整结果见附件。" in text and "结论：三条都是 etl 的批量写入。" in text
    assert "（无数据行）" not in text and "／本次返回 3 行" in text


def test_preview_of_metadata_fact_shows_some_fields() -> None:
    metadata = {f"host{i}": f"10.0.0.{i}" for i in range(400)}
    metadata["dashboards"] = "D" * 40_000
    d = delivery(
        fact((), (), metadata=metadata, raw=json.dumps(metadata), tool_id="grafana/search"),
        analysis=None,
    )
    message = _preview(d.model_copy(update={"report_format": "md"}), 3500, CHAT, None, "本次完整结果见附件。")
    text = shown(message)
    assert "预览无法在本消息容量内展示" not in text and "10.0.0.0" in text


def test_element_budget_still_forces_attachment() -> None:
    # 25 条小结果：完整卡片约 2 万字节（低于 30,000），但组件约 300 个，超过 200。
    facts = tuple(
        fact(("v",), ({"v": i},)).model_copy(update={"evidence_id": f"ev_{i}"}) for i in range(25)
    )
    d = delivery(*facts, analysis=None)
    with pytest.raises(MessageCapacityError):  # 字符与字节都放开时仍不合，只能是组件预算
        build_feishu_message(d, 10**6, CHAT, allow_preview=False, max_bytes=10**6)
    *files, body = _data_parts(d, 0, 3500, CHAT, None)
    assert files and files[0].kind == "md"
    assert "结果预览" in body.body.decode()


def test_old_saved_body_part_still_passes_capacity_check() -> None:
    small = delivery(fact(("v",), ({"v": 1},)))
    old = build_feishu_message(small, 3500, CHAT, allow_preview=False)
    part = DeliveryPart(0, "body", json.dumps(old, ensure_ascii=False).encode())
    _check_capacity((part,), 3500, CHAT, None, platform_limits=True)


def test_approval_parts_keep_reply_char_rule() -> None:
    card = {"card": {"schema": "2.0", "body": {"elements": [{"tag": "div", "text": {"tag": "plain_text", "content": "批" * 4000}}]}}}
    part = DeliveryPart(0, "approval", json.dumps(card, ensure_ascii=False).encode())
    with pytest.raises(MessageCapacityError):
        _check_capacity((part,), 3500, CHAT, None, platform_limits=False)
    _check_capacity((part,), 3500, CHAT, None, platform_limits=True)
```

- [ ] **Step 2: 运行并确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/sdk_core/test_inline_delivery.py -p no:cacheprovider --tb=short`
Expected: Task 2 的 6 项失败（第一项因仍转附件、第二项出现“（无数据行）”、`_check_capacity` 不接受 `platform_limits` 等）。

- [ ] **Step 3: 实现**

`_preview` 全部替换为：

```python
def _preview(
    delivery: Delivery, max_chars: int, chat_id: str, reply_to: str | None, notice: str
) -> Message:
    # 复用渲染器的整行预览：回答优先，范围与“结果行未展示”由同一预算生成，不先截事实。
    if delivery.facts:
        candidate = delivery.model_copy(update={"report_notice": notice + "\n结果预览。"})
    else:
        candidate = delivery.model_copy(
            update={
                "content": notice + "\n" + delivery.content,
                "feishu_text": (notice + "\n" + delivery.feishu_text)
                if delivery.feishu_text is not None
                else None,
            }
        )
    try:
        message = build_feishu_message(
            candidate, max_chars, chat_id, reply_to=reply_to, platform_limits=True
        )
    except (MessageCapacityError, ValueError):
        message = None
    if message is not None and message != capacity_notice(delivery.session_notice):
        return message
    text = notice + "\n预览无法在本消息容量内展示。"
    limits = list(
        dict.fromkeys(
            [n for f in delivery.facts for n in fact_limit_notices(f)]
            + [n for n in (delivery.monitoring_notice, delivery.session_notice) if n]
        )
    )
    message = {"text": text + ("\n" + "\n".join(limits) if limits else "")}
    if (
        len(message["text"]) > max_chars
        or request_bytes(message, chat_id, reply_to=reply_to) > MAX_REQUEST_BYTES
    ):
        raise MessageCapacityError("容量说明无法完整展示")
    return message
```

并在导入中加入 `capacity_notice`：

```python
from xiaowei.feishu_render import (
    MAX_REQUEST_BYTES,
    Message,
    MessageCapacityError,
    build_feishu_message,
    capacity_notice,
    fact_limit_notices,
    request_bytes,
)
```

`_data_parts` 中完整消息尝试改为：

```python
            message = build_feishu_message(
                delivery,
                max_chars,
                chat_id,
                reply_to=reply_to,
                allow_preview=False,
                platform_limits=True,
            )
```

`_check_capacity` 替换为：

```python
def _check_capacity(
    parts: tuple[DeliveryPart, ...],
    max_chars: int,
    chat_id: str,
    reply_to: str | None,
    *,
    platform_limits: bool,
) -> None:
    for p in parts:
        if p.send_state == "sent":
            continue
        if p.kind in ("md", "html"):
            if len(p.body) > MAX_REPORT_BYTES:
                raise ReportCapacityError("容量不足，不能重发")
        else:
            message = json.loads(p.body)
            # 使用原卡片的容量规则，绝不按新模板重建/逐字节比较。
            from xiaowei.feishu_render import _chars, _elements

            limit_chars = not platform_limits or set(message) == {"text"}
            if (
                (limit_chars and _chars(message) > max_chars)
                or _elements(message) > 200
                or request_bytes(message, chat_id, reply_to=reply_to) > MAX_REQUEST_BYTES
            ):
                raise MessageCapacityError("容量不足，不能重发")
```

两处调用：

```python
        _check_capacity(approvals, max_chars, chat_id, reply_to, platform_limits=False)
```

```python
        _check_capacity(data_parts, max_chars, chat_id, reply_to, platform_limits=True)
```

审批材料构造（`feishu_delivery.py:200` 附近的 `build_feishu_message(material, ...)`）保持不变。

- [ ] **Step 4: 运行新测试**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/sdk_core/test_inline_delivery.py -p no:cacheprovider --tb=short`
Expected: 16 passed。

- [ ] **Step 5: 运行交付回归（需隔离测试 PostgreSQL）**

```bash
docker compose -p xiaowei-sdk-test -f compose.sdk-test.yml up -d --wait
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -q tests/sdk_core/test_delivery_parts.py tests/sdk_core/test_feishu_reports.py \
  tests/sdk_core/test_feishu.py tests/sdk_core/test_group_actions.py tests/sdk_core/test_action_recovery.py \
  -p no:cacheprovider --tb=short
```

Expected: 全部通过。`test_capacity_failure_shows_preview_but_resend_is_local_only` 仍须看到“完整结果尚未交付”“预览”且无“见附件”。依赖旧 600 字符上限才转附件的用例（例如 `_data_parts(delivery, 0, 600, ...)`）若因现在直接内联而失败：改为构造真正超过 30,000 字节的事实来保持原意图，不删除断言；逐条在提交说明中列出。

- [ ] **Step 6: Commit**

```bash
git add src/xiaowei/feishu_delivery.py tests/sdk_core/test_inline_delivery.py tests/sdk_core/test_delivery_parts.py
git commit -m "fix(feishu): 新语义交付按飞书平台限制决定附件并保留回答预览"
```

---

### Task 3: 生产复盘样例的端到端交付回归

**Files:**
- Test: `tests/sdk_core/test_inline_delivery.py`

**Interfaces:**
- Consumes: Task 2 的 `_data_parts`。

- [ ] **Step 1: 写测试（覆盖 R7、R11、R14 的形态）**

```python
def test_production_round_r7_audit_rows_stay_inline_when_under_limit() -> None:
    rows = [
        {
            "execution_time": f"2026-10-15 14:0{i}:00",
            "queryId": f"6f1c-{i}",
            "user": "etl",
            "db": "dw",
            "duration_ms": 120000 + i,
            "duration_sec": 120.0 + i,
            "state": "EOF",
            "sql_text": "INSERT INTO dw.t SELECT *\nFROM ods.s\nWHERE dt = '2026-10-15'",
        }
        for i in range(3)
    ]
    d = delivery(fact(tuple(rows[0]), rows))
    (body,) = _data_parts(d, 0, 3500, CHAT, None)
    text = body.body.decode()
    assert "（无数据行）" not in text and "6f1c-2" in text


def test_production_round_r14_dashboard_search_stays_inline() -> None:
    metadata = {
        "dashboards": json.dumps(
            [{"uid": f"rYdddlPWk{i}", "title": f"Node Exporter Full {i}"} for i in range(20)]
        )
    }
    raw = json.dumps({"dashboards": json.loads(metadata["dashboards"])})
    d = delivery(fact((), (), metadata=metadata, raw=raw, tool_id="grafana/search_dashboards"))
    parts = _data_parts(d, 0, 3500, CHAT, None)
    assert [p.kind for p in parts] == ["body"]
```

- [ ] **Step 2: 运行**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/sdk_core/test_inline_delivery.py -p no:cacheprovider --tb=short`
Expected: 18 passed（这两项在 Task 2 完成后应直接通过；若在基线 `eda4a0b` 上运行则失败，作为复盘回归证据，记录在提交说明中）。

- [ ] **Step 3: 变异检查（临时，不提交）**

逐项在工作树中临时改动并运行本文件，确认对应用例失败后 `git checkout -- src` 还原：
1. `_preview` 恢复为截 `fact.rows` 后 `allow_preview=False` → `test_zero_row_preview_never_claims_no_data` 或 `test_oversized_result_gets_file_and_body_with_answer_and_exact_preview` 失败。
2. `fits` 中对卡片也检查 `max_chars` → `test_medium_result_*` 失败。
3. `_raw_redundant` 永远返回 `True` → `test_lossy_fact_keeps_raw_json_required[bigint]` 与 `[nested]` 失败（错误实现会内联并丢失原类型）。
4. 删去回答优先的 `fit_analysis([0] * len(totals))` → `test_preview_keeps_answer_first_and_states_exact_range` 失败。
5. 平台完整判断改为 `technical` 只含不可替代项（即把技术字段与原文 JSON 一起设为可选）→ `test_redundant_raw_json_is_optional_but_technical_fields_stay` 失败。

把五次结果（失败用例名与退出码）记入提交说明。

- [ ] **Step 4: Commit**

```bash
git add tests/sdk_core/test_inline_delivery.py
git commit -m "test(feishu): 生产复盘 R7/R14 形态的交付回归"
```

---

### Task 4: MD 文件输出可读表格

**Files:**
- Modify: `src/xiaowei/feishu_report.py:239-315`（`_SIMPLE_MD` 保留；`_md_fact` 表格分支重写）
- Test: `tests/sdk_core/test_feishu_report_layout.py`

**Interfaces:**
- Produces: `_md_cell(value: object) -> str | None`（`None` 表示放表下明细）。表格对任何有列结果生成；文件仍以“本文件展示 N 行。”结尾。

- [ ] **Step 1: 写失败测试**

追加到 `tests/sdk_core/test_feishu_report_layout.py`：

```python
def _md_body(result: dict) -> str:
    delivery = _delivery("md")
    fact = delivery.facts[0].model_copy(update={"result_json": json.dumps(result, ensure_ascii=False)})
    return build_report(delivery.model_copy(update={"facts": (fact,)})).content.decode()


def test_md_table_for_timestamps_ips_and_multiline_sql() -> None:
    body = _md_body(
        {
            "columns": ["ts", "host", "sql_text"],
            "rows": [
                {"ts": "2026-10-15 14:00:00", "host": "10.0.0.1", "sql_text": "SELECT 1\nFROM t"},
            ],
        }
    )
    assert "| ts | host | sql_text |" in body
    assert '`"2026-10-15 14:00:00"`' in body and '`"10.0.0.1"`' in body
    assert "见下方 第 1 行" in body
    assert "SELECT 1\nFROM t" in body
    html = MarkdownIt("default").enable("table").render(body)
    assert "<table>" in html and "<td><code>&quot;10.0.0.1&quot;</code></td>" in html


def test_md_table_cells_are_inert_and_reversible() -> None:
    values = [
        "a|b",
        "x`y",
        "<at id=all>",
        "https://invalid.example/path",
        "__strong__",
        "line\u2028sep",
    ]
    body = _md_body({"columns": ["v"], "rows": [{"v": v} for v in values]})
    html = MarkdownIt("default").enable("table").render(body)
    assert "<table>" in html
    assert "<a href=" not in html and "<strong>" not in html and "<at" not in html
    cells = re.findall(r"<td><code>(.*?)</code></td>", html)
    assert [json.loads(unescape(c)) for c in cells[:5]] == values[:5]
    assert "\\u2028" in cells[5]


def test_md_table_headers_and_detail_titles_are_inert_and_reversible() -> None:
    # SQLGuard 接受带引号的别名，列名可含换行、链接与 Markdown 结构，不能指望上游拦截。
    names = [
        "x\n\n[click](https://example.invalid)\n\ny",
        " lead ",
        "a`b",
        "a|b",
        "z\n\n- injected",
        "ok",
    ]
    row = {name: "1" for name in names}
    row["z\n\n- injected"] = "multi\nline"  # 触发表下明细标题
    body = _md_body({"columns": names, "rows": [row]})
    html = MarkdownIt("default").enable("table").render(body)
    assert "<table>" in html
    assert "<a href=" not in html and "<li>" not in html
    headers = re.findall(r"<th>(.*?)</th>", html)
    assert len(headers) == len(names)
    decoded = [
        json.loads(unescape(h[len("<code>") : -len("</code>")])) if h.startswith("<code>") else h
        for h in headers
    ]
    assert decoded == names
    assert "见下方 第 1 行" in body and "multi\nline" in body
```

（文件顶部已有 `json`、`re`、`unescape`、`MarkdownIt` 导入。）

- [ ] **Step 2: 运行并确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/sdk_core/test_feishu_report_layout.py -k "md_table" -p no:cacheprovider --tb=short`
Expected: 3 failed（基线没有表格；表头用例在基线上还会生成链接或列表）。

- [ ] **Step 3: 实现**

在 `_safe_md_cell` 之后增加：

```python
_MD_CELL_MAX = 120


def _md_cell(value: object) -> str | None:
    """表格单元格：简单值直出；其余放入代码片段并转义竖线；多行或过长值放表下明细。"""
    text = _json_value(value)
    if (isinstance(value, str) and ("\n" in value or "\r" in value)) or len(text) > _MD_CELL_MAX:
        return None
    if _safe_md_cell(text):
        return text
    # 代码片段内不解析强调、链接与 HTML；GFM 表格先处理 \|，再还原为字面竖线。
    return _inline_code(display_text(text).replace("|", "\\|"))


def _md_label(name: str) -> str:
    """列名/字段名：简单名直出；其余用 JSON 单行编码放入代码片段，可逆且不跨行。"""
    if _safe_md_cell(name) and name == name.strip():
        return name
    # json.dumps 把换行等转成转义序列，引号保住首尾空格；再转义竖线、显示不可见控制符。
    return _inline_code(display_text(_json_value(name)).replace("|", "\\|"))
```

`_md_label` 同时替换 `_md_fact` 中既有的元数据字段标题 `**{_md_escape(key)}**` 为 `**{_md_label(key)}**`：简单字段名输出不变，含换行等的字段名不再能改变文档结构。

把 `_md_fact` 中 `if table is not None:` 分支整体替换为：

```python
    if table is not None:
        columns, rows = table
        if columns:
            details = []
            lines = [
                "| " + " | ".join(_md_label(c) for c in columns) + " |",
                "| " + " | ".join("---" for _ in columns) + " |",
            ]
            for index, row in enumerate(rows, 1):
                cells = []
                for column in columns:
                    if column not in row:
                        cells.append("（未提供）")
                        continue
                    item = row[column]
                    cell = None if column in _RAW_FIELDS else _md_cell(item)
                    if cell is None:
                        cells.append(f"见下方 第 {index} 行")
                        title = f"**第 {index} 行** · {_md_label(column)}"
                        if isinstance(item, str):
                            language = (
                                "sql" if column in ("sql", "ddl") else "text"
                            )
                            details.append(f"{title}（字符串原文）\n\n{_fence(item, language)}")
                        else:
                            details.append(f"{title}\n\n{_fence(_json_value(item, indent=2), 'json')}")
                    else:
                        cells.append(cell)
                lines.append("| " + " | ".join(cells) + " |")
            blocks.append("\n".join(lines))
            blocks.extend(details)
        elif "columns" in data:
            blocks.append("**列定义**\n\n" + _fence(_json_value(columns), "json"))
        blocks.append(f"本文件展示 {len(rows)} 行。")
```

- [ ] **Step 4: 更新两条旧断言（契约变化，非放宽）**

在 `tests/sdk_core/test_feishu_report_layout.py`：
- `test_md_report_uses_table_for_simple_rows_and_fences_complex_originals` 的 hostile 段：把 `assert "| ddl |" not in body` 改为 `assert "| ddl |" in body and "见下方 第 1 行" in body`；保留 `~~~~sql\n` 与 DDL 原文断言。
- `test_md_report_keeps_source_links_and_markup_inside_fences`：把 `assert "| value |" not in body` 与围栏 JSON 断言改为 `assert "| value |" in body and f'`"{value}"`' in body`；保留 `rendered` 中无 `<a href=` 与无 `<strong>strong</strong>` 的安全断言。

旧断言要求“不安全值不进表格”；新契约是“进表格但处于代码片段内”，安全属性仍由渲染断言保证。

- [ ] **Step 5: 运行文件相关回归**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/sdk_core/test_feishu_report_layout.py tests/sdk_core/test_feishu_reports.py -p no:cacheprovider --tb=short`（`test_feishu_reports.py` 中依赖 PostgreSQL 的用例需带 `SDK_TEST_POSTGRES_URL`）
Expected: 全部通过；拆分用例（每个文件 ≤400,000 字节、整行不拆）不受影响。若其他既有用例断言复杂行为“第 N 行 + JSON 围栏”形态，改为断言表格中的对应单元格或“见下方 第 N 行”明细，原有的值、类型（NULL/字符串 NULL/大整数/嵌套）与安全断言逐条保留；每处改动在提交说明中列出。

变异检查（临时，不提交）：把 `_md_label` 改回 `_inline_code(display_text(name).replace("|", "\\|"))`（保留换行），确认 `test_md_table_headers_and_detail_titles_are_inert_and_reversible` 失败后 `git checkout -- src` 还原，结果记入提交说明。

- [ ] **Step 6: Commit**

```bash
git add src/xiaowei/feishu_report.py tests/sdk_core/test_feishu_report_layout.py
git commit -m "fix(report): MD 文件对真实数据输出转义表格，多行与长值放表下"
```

---

### Task 5: 契约、运维说明与交接；整体检查

**Files:**
- Modify: `ARCHITECTURE.md`（§自然回答与完整结果交付 “文件与容量”段）
- Modify: `deploy/OPERATIONS.md:289` 附近容量段
- Modify: `AGENT_HANDOFF.md` §1 快照与 §3 下一步

- [ ] **Step 1: 修改 ARCHITECTURE**

把“保留当前 `max_reply_chars` 作为正文阅读预算，保留每条序列化请求的 30,000 字节本地预算与组件限制；”替换为：

> 新语义飞书数据卡片与预建 post 以飞书平台限制为门槛：每条序列化请求 30,000 字节本地预算（低于官方卡片/富文本 30 KB）与每卡 200 组件；`max_reply_chars` 只约束纯文本消息并作为 SDK 分段上限。完整消息由全部引用结果行、实际 SQL、已保存分析、必要限制及不能由可见值无损替代的原文 JSON 组成；可替代的原文 JSON 放得下才附带，否则说明未展示。转附件时正文先给回答，再给代码标明的“预览 N／本次返回 M 行”。旧 v0 请求与审批材料卡沿原容量规则。（2026-10-16 用户确认）

- [ ] **Step 2: 修改 OPERATIONS**

把“同时遵守 `max_reply_chars`（200–3500）、完整 HTTP 请求体 UTF-8 的 **30,000 字节**和组件预算。”改为：

> 新请求的结果卡片与 post 遵守完整 HTTP 请求体 UTF-8 的 **30,000 字节**和每卡 200 组件；`max_reply_chars`（200–3500）只约束纯文本消息（固定回执、会话提示）及 SDK 分段。旧 v0 请求与审批材料卡仍同时遵守 `max_reply_chars`。

同段“字符包括可见及折叠文字”保留并加限定“（适用于仍受 `max_reply_chars` 约束的消息）”。

- [ ] **Step 3: 更新 handoff**

§1 快照“本地目录 / 分支”写入本工作树与 `codex/inline-delivery-threshold`、基线 `eda4a0b`；§3 下一步写入：本切片的源码/离线证据（命令与日志路径）、未覆盖项（MCP 行视图、回执细分、R3/R4/R9/R15 待日志、真实模型与真实飞书客户端），以及合并后在获准测试群验证接近 30,000 字节卡片的手机/桌面显示。

- [ ] **Step 4: 整体检查**

```bash
.venv/bin/ruff check --no-cache .
.venv/bin/ruff format --check src/xiaowei/feishu_render.py src/xiaowei/feishu_delivery.py src/xiaowei/feishu_report.py tests/sdk_core/test_inline_delivery.py tests/sdk_core/test_feishu_report_layout.py
.venv/bin/mypy src --no-incremental
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/contract/test_doc_fact_binding.py tests/security/test_docs_command_consistency.py tests/security/test_capabilities_doc.py -p no:cacheprovider --tb=short
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -q tests/sdk_core tests/p1b -p no:cacheprovider --tb=short 2>&1 | tee /private/tmp/xw-inline-sdk-core.log
git diff --check
docker compose -p xiaowei-sdk-test -f compose.sdk-test.yml down -v
```

Expected: 全部通过；记录 passed/skipped 数与日志路径到 handoff。文档事实检查若绑定了旧句子，按新契约更新绑定，不删除检查。

- [ ] **Step 5: 独立审查**

对本分支相对 `eda4a0b` 的差异做一次独立审查（展示层改动涉及证据真实性表述：预览范围、原文省略、完整性定义）。审查结论与处理记入 handoff。

- [ ] **Step 6: Commit**

```bash
git add ARCHITECTURE.md deploy/OPERATIONS.md AGENT_HANDOFF.md docs/superpowers/plans/2026-10-16-inline-delivery-threshold.md
git commit -m "docs: 飞书交付以平台限制为门槛并记录复盘修复状态"
```

## 4. 验收与未覆盖

| 层级 | 本切片 | 说明 |
| --- | --- | --- |
| 源码 + 离线测试 | Task 1–5 | 渲染、交付选择、文件表格、旧分项兼容、审批材料隔离 |
| 真实模型 | 不涉及 | 提示词与工具未改；任务样例沿结果交付计划 §2，未运行 |
| 真实飞书 | 未验证 | 接近 30,000 字节卡片在手机/桌面的显示与性能；MD 表格在飞书文件预览及常见编辑器中的显示；均需获准测试群 |
| 部署 | 不做 | 不推送、不合并、不部署，除非用户另行授权 |

**审核修订（`5898dda` 审核）：** 原文 JSON 的省略改用独立的 `raw_hidden`，技术字段始终完整；MD 列名与明细标题用 JSON 单行编码；补充“表格放得下、表格加原文放不下”的大整数与嵌套样本；C 改记为部分修复并加单大字段边界用例。

**退出条件：** 中等结果（<30,000 字节、≤200 组件）无附件；超限结果有附件且正文含回答与准确预览范围，不再出现“（无数据行）”误述；显式 MD 对真实数据成表且安全断言保持；旧 v0 与审批材料卡行为不变；整体检查通过并有独立审查记录。
