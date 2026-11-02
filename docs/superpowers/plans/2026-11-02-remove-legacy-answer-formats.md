# 删除旧回答格式（v2/v3/Legacy）：最小修改计划

> 状态：2026-11-02 r2。用户已决定现在删除（精简第 3 步选 A），接受保留期内旧回答不再可读、不再重发。实施中发现渲染层删除会牵动约 150 个按旧布局断言的测试，用户选择分两步（B）：**本切片 3a 只做数据边界**（§2 第 1–3、5 条，`Delivery` 字段与渲染层不动），渲染层清理（§2 第 4 条）另作 3b，草稿存于分支 `claude/remove-legacy-render-wip@d1820d3`，未审查。
> 基线：分支 `claude/remove-legacy-answers`，叠在第 4 步 `b38f15e` 之上（无代码依赖）。

**目标：** 只保留当前 v4 回答语义。3a 删除 Legacy（S3 前固定栏目）、v2/v3（自由块 + 自动补“其余结果”）的解析与补全；3b 再删渲染分支，使渲染只剩一套。

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
| 同一请求键重复到达 | `RequestUnavailableError`，不重新运行（Web 404）；同会话的新请求照常进入原会话 |
| 审批暂停材料 | 查看／重发材料同上（不可用）。**`/批准` 恢复不读材料回答**：`resume_action → native_run → verify_material → claim_native_action` 只核对动作、SDK 快照、提案 Evidence、材料请求 `paused` 状态与审批分项回执，以及 `min(approval_expires_at, 快照 expires_at)`，不解析请求表 `answer`。因此旧暂停不可批准依赖 15 分钟有效期（`actions.py` 的 `APPROVAL_WINDOW`），而不是解析失败 |

Session 回放不解析回答 JSON（旧助手消息只作为模型历史文字），不受影响。

**暂停恢复的事实与假设：** 上表恢复路径是源码事实。“不存在尚未过期的旧暂停”是假设：只有从 v4 之前的版本直接升级到本版本，且升级前 15 分钟内留下暂停时，才会出现；若生产已运行 v4 超过 15 分钟，就不存在这种暂停（部署日期未核实）。即使出现，审批人批准的是当时已送达的材料（分项回执仍须为 `sent`），执行与旧版相同，恢复后的最终回答按 v4 生成；只是材料不能再查看或重发。本切片不为此新增版本拒绝逻辑。

## 2. 改动

1. **`models.py`**：删除 `LegacyAnswer`、`legacy_blocks`、`AnswerInference`／`AnswerFactView`（源码中只有 `LegacyAnswer` 引用）。`TurnAnswer.answer: AgentAnswer`；`answer_version: Literal[4] = ANSWER_VERSION`，删除 `_version_for_code`、`_version_matches` 与 `blocks_answer`（调用方改用 `answer`）。v2/v3 带其他版本号，S3 之前的格式没有 `blocks`，都解析失败；缺版本的 `AgentAnswer` 形状从未被写入（v2 起写入都带版本），默认为 4 不放宽任何已存在的数据。*3b：*`FactSection` 去掉 `"remaining"`；`Delivery` 删除 `answer_version`、`basis`、`report_native`（v4 下等价于 `report_format is not None`）。
2. **`channel_store.py`**：`_saved_answer` 只解析 `TurnAnswer`；`RequestRecord.answer`／`answer_version` 类型同步收窄或删除。
3. **`evidence.py` `validate_answer`**：删除 `include_remaining_results` 参数与“其余结果”、Legacy 不补动作的 `complete` 分支、旧 `content` 行拼装、`_basis`；动作归并与 `business_text` 成为唯一路径；`_delivery_fact` 删除 `modern` 参数，按 v4 分支保留。产出的 `Delivery` 固定 `answer_version=4`、`report_native = report_format is not None`（与原 v4 取值相同），`basis` 不再计算（渲染层对 v4 本来就不展示）。
4. **渲染（3b，本切片不做）**：`feishu_render.py`、`feishu_report.py`、`feishu_delivery.py`、`webhook.py`、`web.py`、`static/app.js` 把 `modern`／`answer_version == 4` 视为恒真，删除另一分支和只服务旧布局的辅助函数（如 `_basis_panels`、remaining 区）。`channel.py` 的 `answer_version == 4` 判断同样恒真。
5. **调用方**：`scheduled_tasks.py`、`webhook.py` 去掉 `include_remaining_results=False` 实参；`operations_history.py` 的 `blocks_answer` 改为 `answer`；`channel.py` 构造 `TurnAnswer` 时不再传版本。

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

## 5. 实施记录（3a，2026-11-02）

- 测试：删除只验证旧格式的用例（Legacy／v2／v3 解析与交付、`legacy_v3_answers` 夹具及其 3 个用例、`test_s3_legacy_resume.py`、v3“其余结果”补全、v2 报告文字语义、两个浏览器测试的 v3 参数）；新增 `test_answers_saved_before_v4_are_unavailable_without_io`（v2／v3／S3 前信封／最早裸回答四种形状）与 `test_saved_answers_before_v4_are_unavailable`。
- `test_delivery_parts.py` 的 `request()` 原把回答强制改为 v3，以覆盖旧版“正文 + 证据明细”双文件布局；v3 记录不再可读，改为 v4 原样，9 个投递机制用例（存储失败、补发保留字节与 UUID、结果不明、上传不明、每次投递一次权限快照、真实 SDK 复用 UUID 等）按 v4 的“一个报告文件 + 正文”调整分项与计数，测试意图不变。
- 渲染单元测试直接构造不带版本的 `Delivery`，渲染层未改，保持原样。
- `tests/deployment` 升级演练（`test_different_image_upgrade_rollback_and_paired_restore`）原断言 P3 镜像保存的 S3 前回答在升级后仍可重放。按本切片的决定改为：升级后重新提交同一请求得到 404（不重跑、不报 500）；`verify` 中同一请求键按不可用处理，新请求仍落在原会话。探测脚本的旧回答改为只在 `seed`（旧镜像内执行）中构造，避免新镜像导入即失败。探测快照记录每条请求的 `state` 与原始 `answer` 的 SHA-256，演练断言两条种子请求在升级前快照、升级后 `verify`、配套备份恢复、恢复原库与回退中保持不变（不迁移、不改写旧数据）。该演练证明的是请求归属与会话映射延续，不等于完整 SDK 历史回放或追问验收。
- 补回现行审批边界回归 `test_approval_needs_material_part_receipt_not_parent_request_state[failed|unknown]`：原 `legacy_v3_mixed_round...` 用例中 `failed_part="approval"` 分支验证的是 `delivery_version=1` 下只认审批分项回执，与回答格式无关；改为只含审批材料的 v4 用例，父请求被改为 `sent` 后 `/批准` 仍不执行。仅属 v3 暂停轮数据交付的分支不恢复。
- 发现：`delivery_version=0`（迁移 012，10-15 之前）的记录全部早于 v4，其回答现在都不可读，相关发送分支实际不再可达；留到 3b 一并评估。
