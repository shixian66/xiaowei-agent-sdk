# Evidence 一轮内重复复核精简：最小修改计划

> 状态：2026-11-01 草案，待独立审查与用户决定；未实施。
> 基线：`origin/main@f6e824c`（本地，网络不可用未重新 fetch）；计划写在叠加分支 `claude/evidence-recheck-plan`（其下为精简第 1、2 步 `cb7a26b`、`ff7cb66`，与本计划无代码依赖）。

**目标：** 一轮内同一条证据、同一状态、相同信任边界上的重复实时复核只做一次，减少对 StarRocks 的依赖探测和对复核额度的占用；AGENTS §3.8 要求的最终持久化、发送、历史读取和重发复核全部保留。

**不做：** 不改首次发送复核（§3.8 明确要求，改动属于边界变更，用户未要求）；不改 `record()` 交给模型前的复核；不加缓存、不加按时间跳过的规则、不加开关；不改 `_CHECKS_PER_LISTED_OBJECT`（见 §5）。

## 1. 测量（离线，正式装配）

正式装配：真 Runner、治理、`EvidenceStore`、`DependencyCheck`、隔离 PostgreSQL、StarRocks 假驱动。在读取边界计数，按调用阶段归类。每次“读取”= 1 次 PostgreSQL 读 + 1 次当前权限查询；每“批”含依赖的读取另按目标合并做 1 次 StarRocks 复核（取连接、逐对象零行探测、读版本）。

| 阶段（调用点） | 1 次查询 | 3 次查询同表 | 第 2 轮无工具（历史 1） | 第 3 轮 1 查询（历史 1） |
| --- | --- | --- | --- | --- |
| 0 SDK 读历史 `session.get_items` → `_replay` | – | – | 1 | 1 |
| 1 交给模型前 `evidence.record` → `_readable` | 1 | 3 | – | 1 |
| 2 暂存 `session.add_items` → `_stored_form` → `project` | 1 | 3 | – | 1 |
| 4 提交校验 `commit_validated` → `validate_answer` | 1 | 1 | 1 | 1 |
| 5 提交回放 `commit_validated` → `_replay` → `project_many` | 1 | 1 | 1 | 1 |
| 6 首次发送 `validate_answer`（独立计数） | 1 | 1 | 1 | 1 |
| **StarRocks 复核次数** | **5** | **9** | **4** | **6** |

- 0–5 共用一轮 `scope_checks` 额度；3 次查询一轮占 8，其中阶段 1、2 占 6。跨库 2 对象时每阶段探测 2 个对象。
- 本轮证据每条 PostgreSQL/权限读取 5 次；历史证据每轮 4 次。
- 未测：真实耗时、审批恢复、历史读取工具、定时任务、webhook（各自另行计数，本计划不改其路径）。

## 2. 判断

| 阶段 | 结论 | 依据 |
| --- | --- | --- |
| 2 暂存 | **重复，删除复核** | 紧跟阶段 1，同一证据同一状态；暂存只在内存，不落库。暂存内容的出口只有两类：SDK 任何一次 `get_items`（锁定 0.22.3 在运行开始、续跑与写入核对时调用；`PolicySession.get_items` 每次都走完整 `_replay` 复核）与阶段 4/5 的持久化；`pop_item` 只撤回不读出；审批暂停时 `discard_pending`，暂停只保存 RunState。 |
| 4 与 5 | **同一提交内两批，合并为一批** | 两者在同一次 `commit_validated` 内先后执行，证据集合在正常路径上相同（`context_evidence` = 本轮回放过的历史证据 + 本轮产生的证据）。 |
| 0、1、6 | 保留 | 历史读取边界、模型边界（快照版本首次对比当前库）、发送边界。 |

## 3. 改动

### 3.1 `EvidenceStore`：拆出“逐条检查”与“批量复核”

`project_many` 现为 `_readable`（逐条 `_checked` + `validate_records` 批量复核）+ 调用绑定核对。拆出：

```python
async def checked_calls(
    self, references: Sequence[tuple[str, ToolCall | None]], ctx: RunContext, audience: Audience
) -> list[EvidenceRecord]:
    """逐条做归属/渠道/过期/范围/策略/当前工具权限与调用绑定检查；不复核对象依赖，不可直接交付。"""
```

`project_many` 改为 `checked_calls` + `validate_records` + 取投影，行为不变。`checked_calls` 与已有 `inspect` 同类（“已检查、未验证依赖”），只给可信调用方把记录并入同一批复核。

### 3.2 `PolicySession.add_items`：只做结构检查

`_stored_form` 的 `function_call_output` 分支保留：信封形状、能找到同批已暂存的对应 `function_call`；删除 `self._evidence.project(..., "session", call=call)`。找不到调用时仍抛 `SessionItemRejectedError("本轮工具结果无法保存到会话")`。

### 3.3 `PolicySession.commit_validated`：一次提交一批复核

顺序改为：

1. `_without_superseded_answers`、最终回答结构解析（不变）。
2. `_replay` 结构检查并收集引用（不变），证据部分改为 `checked_calls(references, ctx, "session")`：
   - 本轮暂存项的引用失败 → `SessionItemRejectedError("本轮工具结果无法保存到会话")`（保持原 `content_rejected` 归类，覆盖原阶段 2 的绑定/归属拒绝）；
   - 已提交历史的引用失败 → `self._unavailable()`（不变）。
3. `validate_answer(turn, ctx, source_records=[*source_records, *回放记录中不在 context_evidence 的那部分])`：依赖在这一批内按目标合并复核一次。正常路径下附加部分为空。
4. 用第 2 步的记录取 Session 投影组装回放、计字节、写入（不变）。

`get_items`（阶段 0）继续走完整 `project_many`，不受影响。

### 3.4 不改

`record()`、首次发送、历史读取工具、重发、审批恢复的 `project_many`（`app.py:1402`）、`actions.py`、定时任务与 webhook。

## 4. 失败行为变化

| 情形 | 现在 | 改后 |
| --- | --- | --- |
| 本轮证据在工具返回后、暂存前被撤权或对象变化 | 阶段 2 拒绝：`content_rejected`，SDK 运行提前停止 | 运行继续到提交；阶段 4 拒绝：`answer_rejected`（`evidence_unavailable`）。后续工具调用仍各自经治理复核权限，不会多产生未授权 I/O |
| 暂存的工具结果信封指向别的会话/调用（篡改） | 阶段 2 `content_rejected` | 提交时第 2 步 `content_rejected` |
| 依赖暂时无法复核 | `scope_unverifiable` | 不变 |
| 历史证据失效 | 阶段 4 先拒绝（`answer_rejected`） | 不变 |

可观察差异只有第一行：用户得到的失败类别由“内容未保存”变为“回答证据不可用”，两者对用户都是本轮失败、可重试；多出的模型调用以本轮剩余步数为上限。

## 5. 复核额度

改后一轮内每对象：记录 1 次 + 提交 1 次（下一轮回放另计）。3 次查询同表一轮由 8 降到 4；6 对象宽查询由 24 降到 12。`_CHECKS_PER_LISTED_OBJECT = 5` 的注释依据（记录、暂存、提交校验、回放）随之过时；本切片只更新注释，不降低倍数——降低只会允许更小的配置值，不带来安全收益，留作后续单独决定。

## 6. 测试

- **更新** `test_evidence_scope.py::test_a_turn_succeeds_at_the_check_limit_and_stops_before_io_beyond_it`：`needed == 6 * 4` → `6 * 2`，注释同步；“超限在探测前拒绝”断言保留。
- **改写** `test_session_policy.py` 中 `add_items` 拒绝外会话证据与调用绑定不符的用例：`add_items` 接受，`commit_validated` 拒绝且为 `SessionItemRejectedError`、底层 Session 无写入。
- **新增** 回归：一轮 3 次同表查询 + 首次发送，StarRocks 零行探测条数 = 3（记录）+ 1（提交）+ 1（发送）；改前为 9。
- **新增** 失败路径：工具返回后撤权，本轮以 `answer_rejected` 结束，会话无新增轮次，证据不出现在任何交付里。
- 保留并运行：`test_sdk_approval.py` 的额度连续性断言（暂停/续跑计数等于实际探测）、`test_session_capacity.py`、`test_sql_parse_recovery.py`、`test_starrocks_tools.py`、`tests/security`。
- 门禁：CI 普通 job 等价命令、相关 sdk_core 与 PostgreSQL 套件、`ruff check .`、`mypy src`。

## 7. 风险与审查重点

1. **集合等价假设**：合并依赖 `context_evidence` 覆盖提交回放中的全部证据；不覆盖的部分以 `source_records` 并入同批，不会漏验。审查请核对 `app.py` 构造 `context_evidence` 的所有入口（普通轮、审批续跑、群轮）。
2. **暂存不复核的出口**：请核对 pending 除阶段 0 回放与提交外没有其他读者（含暂停保存、群会话、`pop_item`）。
3. **错误归类**：本轮暂存项与已提交历史的失败分开映射，保持 `content_rejected` / `session_unavailable` 原含义。
4. 本计划只减少同一状态下的重复探测；不改变哪些边界复核、复核什么。

## 8. 实施与交付

获准后在主题分支实施，按 §3.1 → §3.3 顺序串行，测试同步。属于 §6 表中“数据/Evidence、Session”类改动：须正常与拒绝路径、真 SDK 与实际存储验证，并对具体差异做独立审查。不推送、不合并、不部署，不调用真实模型或公司服务。
