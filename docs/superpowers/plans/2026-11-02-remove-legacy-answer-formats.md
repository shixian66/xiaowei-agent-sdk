# 删除旧回答格式（v2/v3/Legacy）：最小修改计划

> 状态：2026-11-02 草案；用户已决定现在删除（精简第 3 步选 A），接受保留期内旧回答不再可读、不再重发。未实施。
> 基线：分支 `claude/remove-legacy-answers`，叠在第 4 步 `b38f15e` 之上（无代码依赖）。

**目标：** 只保留当前 v4 回答语义，删除 Legacy（S3 前固定栏目）、v2/v3（自由块 + 自动补“其余结果”）的解析、补全与渲染分支，渲染只剩一套。

**事实：**
- 生产 `evidence_retention_seconds`、`request_retention_seconds`、会话 `retention_seconds` 均为 30 天（用户 2026-11-02 回传）。v4 约 10-23 合入，部署日期未核实；旧回答最晚约在 11-22 之后自然清除。
- 落库的回答只有 `TurnAnswer` JSON：请求表回答、审批暂停材料、定时任务结果；读回只有两个入口：`channel_store._saved_answer` 与 `operations_history.py:326`。`Delivery` 不落库，只在 `web.py:183` 序列化给前端。v4 写入时 JSON 自带 `"answer_version": 4`。
- `_record` 的调用方都是按单条请求读取，没有列表读取，一条旧记录不会让其他记录失败。
- **回执不受影响（已实测）：** `channel.py` 的失败回执与“后续回答无法交付”构造 `Delivery` 时不带版本号，目前走的是旧渲染分支。scratchpad 脚本对 5 类回执 × `platform_limits`／`attachment_preview`／`allow_preview` 共 40 种组合，分别按 `answer_version=None` 与 `4` 生成飞书消息，差异为 0；Web 前端对无事实、无正文块的交付提前返回，只显示文字，也与版本号无关。

## 1. 旧记录被读到时的行为

`TurnAnswer` 只接受 v4，旧记录解析失败后，`_saved_answer` 抛 `RequestUnavailableError`，沿用现有的不可用路径，不新增提示文案：

| 入口 | 旧记录的结果 |
| --- | --- |
| Web 查看／重发 | 404，与记录过期相同 |
| 飞书重发／历史 | `_WITHHELD`：不发送，记日志，与记录过期相同 |
| 运维历史读取 | 解析失败按现有逻辑退回只读原事实，不带旧分析 |
| 同一请求键重复到达 | `RequestUnavailableError`，不重新运行 |
| 审批暂停材料 | 审批有效期 15 分钟（`actions.py` 的 `APPROVAL_WINDOW`），v3 时期的材料早已过期；若读到即不可批准 |

Session 回放不解析回答 JSON（旧助手消息只作为模型历史文字），不受影响。

## 2. 改动

1. **`models.py`**：删除 `LegacyAnswer`、`legacy_blocks`、`AnswerInference`／`AnswerFactView`（源码中只有 `LegacyAnswer` 引用）。`TurnAnswer.answer: AgentAnswer`；`answer_version: Literal[4]`，JSON 输入必须带版本，代码构造时自动写入，删除 `_version_matches` 与 `blocks_answer`（调用方改用 `answer`）。`FactSection` 去掉 `"remaining"`。`Delivery` 删除 `answer_version`、`basis`、`report_native`（v4 下等价于 `report_format is not None`），`has_system_content` 去掉 basis 一项。
2. **`channel_store.py`**：`_saved_answer` 只解析 `TurnAnswer`；`RequestRecord.answer`／`answer_version` 类型同步收窄或删除。
3. **`evidence.py` `validate_answer`**：删除 `include_remaining_results` 参数与“其余结果”、Legacy 不补动作的 `complete` 分支、旧 `content` 行拼装、`_basis`；动作归并与 `business_text` 成为唯一路径；`_delivery_fact` 删除 `modern` 参数，按 v4 分支保留。
4. **渲染**：`feishu_render.py`、`feishu_report.py`、`feishu_delivery.py`、`webhook.py`、`web.py`、`static/app.js` 把 `modern`／`answer_version == 4` 视为恒真，删除另一分支和只服务旧布局的辅助函数（如 `_basis_panels`、remaining 区）。`channel.py` 的 `answer_version == 4` 判断同样恒真。
5. **调用方**：`scheduled_tasks.py`、`webhook.py` 去掉 `include_remaining_results=False` 实参；`operations_history.py` 的 `blocks_answer` 改为 `answer`。

不改：数据库 schema（不迁移、不清数据）、Session 表、已绑定交付原字节的重发路径、v4 的任何展示结果。

## 3. 测试

- **删除** 只验证旧格式的测试：`test_s3_legacy_resume.py`、`test_s3_review_fixes.py` 中的旧格式用例、`legacy_v3_answers` 夹具及其用例、`test_s3_answer.py`／`test_evidence.py`／`test_r3_render.py` 等中 Legacy／v2／v3 的参数化分支。
- **调整** 直接构造 `Delivery` 而未带版本、因此测到旧渲染的测试（约 18 个文件）：删掉版本参数后，凡断言随之变化的，先确认新输出就是 v4 现有行为（同一输入在改动前加 `answer_version=4` 的输出），再更新断言；不能确认的列出复核。
- **新增** 一条：请求表中保存的 v3／Legacy JSON 读到时，Web 返回 404、飞书不发送、没有模型调用或业务 I/O。
- 门禁：CI integration 等价命令（除 `tests/deployment`）、`-m browser` 的 Web／报告浏览器测试、`ruff check .`、`mypy src`。

## 4. 风险

1. **v4 展示是否被误改**：渲染分支删除最容易出错。按 §3 第二条，以“改动前 v4 输出”为基准逐条核对。
2. **遗漏的落库位置**：如果有未发现的 `TurnAnswer` 落库位置，旧数据读到时会走解析失败；§1 的入口已按源码核对，审查请再确认。
3. 本计划只删兼容代码，不改变权限、证据复核或 v4 行为；属于“数据/Evidence 与用户入口”类改动，实施后对具体差异做独立审查。
