# Evidence 一轮内重复复核精简：最小修改计划

> 状态：2026-11-02 r3。r2 按独立审查 `6109df5` 的意见修订了提交顺序、失败分类和测试；r3 按 r2 复审（结论“修改后实施”，保留不合批）修正绑定检查顺序和两条测试预期。待用户决定；未实施。
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
| 2 暂存 | **重复，删除复核** | 紧跟阶段 1，同一证据同一状态；暂存只在内存，不落库。暂存内容的出口：SDK 任何一次 `get_items`（锁定 0.22.3 在运行开始、续跑与写入核对时调用；`PolicySession.get_items` 每次都走完整 `_replay` 复核）与阶段 4/5 的持久化。`pop_item` 会返回暂存项，但暂存的工具结果只含证据引用、不含数据；0.22.3 只在会话重试回滚时用它比对指纹，比对失败经 `add_items` 放回，仍只是暂存。审批暂停另存独立 RunState，保存前 `_paused` 对全部模型可见证据做完整 `validate_answer`，之后才 `discard_pending`；安全依据是这次完整校验，不是 `discard_pending`。 |
| 4 与 5 | **同一提交内两批，依赖复核只对未覆盖部分补做** | 两者在同一次 `commit_validated` 内先后执行。普通轮 `context_evidence` = 本轮回放过的历史证据 + 本轮产生的证据，与提交回放集合相同。群审批续跑时 `context_evidence` 用暂停时保存的集合，而提交回放还含等待期间其他成员提交的历史（`app.py:950–980`，`test_waiting_releases_group_and_resume_commits_after_other_investigation`），两者不等，这部分必须继续复核。 |
| 0、1、6 | 保留 | 历史读取边界、模型边界（快照版本首次对比当前库）、发送边界。 |

## 3. 改动

### 3.1 `EvidenceStore`：拆出“逐条检查”与“批量复核”

`project_many` 现在的顺序是：`_readable`（逐条 `_checked`，再 `validate_records` 批量复核依赖），最后核对调用绑定。拆成三段，顺序不变：

```python
async def checked_calls(
    self, evidence_ids: Sequence[str], ctx: RunContext, audience: Audience
) -> list[EvidenceRecord]:
    """逐条做归属/渠道/过期/范围/策略/当前工具权限检查；不复核对象依赖、不核对调用绑定，不可直接交付。"""

def check_bindings(
    self, records: Sequence[EvidenceRecord], calls: Sequence[ToolCall | None]
) -> None:
    """调用标识、函数名与有效参数摘要须与记录一致；即现有 project_many 尾部的循环。"""
```

`project_many` 改为 `checked_calls` → `validate_records` → `check_bindings` → 取投影，顺序和行为都不变，阶段 0 的 `get_items` 完全不受影响。`checked_calls` 与已有 `inspect` 同类（“已检查、未验证依赖”），只给 `commit_validated` 用来找出未覆盖的记录并补做依赖复核。

### 3.2 `PolicySession.add_items`：只做结构检查

`_stored_form` 的 `function_call_output` 分支保留：信封形状、能找到同批已暂存的对应 `function_call`；删除 `self._evidence.project(..., "session", call=call)`。找不到调用时仍抛 `SessionItemRejectedError("本轮工具结果无法保存到会话")`。

### 3.3 `PolicySession.commit_validated`：回放只为未覆盖部分补做依赖复核

保持现有顺序，只改第 3 步：

1. `_without_superseded_answers`（不变）。
2. `_validate_final_answer` → `validate_answer(turn, ctx, source_records=source_records)`（不变）：`context_evidence` 全部逐条检查，依赖与 `source_records` 同批复核一次。
3. `_replay`：结构检查与巡检来源（不变）；证据部分由 `project_many` 改为三段，顺序与原阶段 5 相同：`checked_calls` 逐条检查 → 对**不在 `context_evidence` 中的记录**调一次 `validate_records`（为空则跳过，普通轮就是空的）→ `check_bindings`。三段的 `EvidenceUnavailableError` 都按原来的方式映射为 `self._unavailable()`，`EvidenceUnverifiableError` 映射为 `SessionUnverifiableError`。依赖复核仍在绑定检查之前，所以复合故障（绑定不符，同时依赖暂时无法复核）的优先级也和现在一样，仍报 `scope_unverifiable`。
4. 用第 3 步的记录取 Session 投影，组装回放、计字节、写入（不变）。

不把未覆盖记录并入第 2 步的批次。并进去会把与本轮回答无关的历史失效报成“回答未通过证据校验”，这类失效现在报的是 `session_unavailable`，后者更准确。分开以后，第 3 步只在群审批续跑且有新历史时才多一批，探测的对象也比现在的阶段 5 少（只探测未覆盖部分）。代价是同一对象如果同时出现在两批里，会被探测和计费两次，现在也是这样，不比现状差。

`get_items`（阶段 0）继续走完整 `project_many`。PostgreSQL 读取与权限查询在提交时仍是每条两次（第 2、3 步各一次，与现在相同）。本计划只减少 StarRocks 复核，不另外暴露 `validate_answer` 内部的记录来省这次读取。

### 3.4 不改

`record()`、首次发送、历史读取工具、重发、审批恢复的 `project_many`（`app.py:1402`）、`actions.py`、定时任务与 webhook。

## 4. 失败行为变化

“工具授权”指 `_checked` 的检查：归属、渠道、过期、目标范围、当前策略指纹、当前工具读权限。“对象依赖”指 `_verify` 对数据库对象做的权限和版本零行探测。`_turn_error` 的映射优先级不变：暂时无法复核（`scope_unverifiable`）> `session_unavailable` > `answer_rejected` > `content_rejected`。

| 证据位置 | 失效发生时间 | 工具授权失效 | 对象依赖失效 | 暂时无法复核 |
| --- | --- | --- | --- | --- |
| 本轮产生（在 `context_evidence` 中） | `record` 之后、`add_items` 之前 | 现在：阶段 2 报 `content_rejected`，运行提前停止。**改后：提交第 2 步报 `answer_rejected`** | 同左 | `scope_unverifiable`，不变 |
| 本轮产生 | `add_items` 之后、提交之前 | `answer_rejected`，不变 | `answer_rejected`，不变 | `scope_unverifiable`，不变 |
| 回放的历史（在 `context_evidence` 中） | 阶段 0 之后、提交之前 | `answer_rejected`，不变 | `answer_rejected`，不变 | `scope_unverifiable`，不变 |
| 续跑时等待期间新提交的历史（不在 `context_evidence` 中） | 续跑的 `get_items` 之后、提交之前 | `session_unavailable`，不变 | `session_unavailable`，不变（改后在第 3 步补做的批次里发现） | `scope_unverifiable`，不变 |
| 暂存工具结果的信封与调用对不上，或指向别的会话（同一进程内的篡改，正常运行到不了这里） | 暂存时 | 现在：阶段 2 报 `content_rejected`。**改后：提交第 3 步报 `session_unavailable`** | 不适用 | 不适用 |

可观察的变化只有加粗的两项。第一项是用户得到的失败类别由“内容未保存”变为“回答证据不可用”（渠道回执由 `session_failed` 变为 `evidence_failed`，见 `channel.py` 的 `_FAILURE_CODES`），两者对用户都是本轮失败、可以重试。多出来的模型调用以本轮剩余步数为上限，后续工具调用仍各自经过治理复核权限，不会多出未授权 I/O。第二项只有直接调用 Session API 才能触发，仍然在写入前拒绝；两个类别在渠道里都是 `session_failed`，回执不变。这两项都接受，不为保持原来的分类增加归因逻辑。

## 5. 复核额度

改后普通轮内每对象：记录 1 次 + 提交 1 次（下一轮回放另计）；群审批续跑另加未覆盖历史的对象数，不超过现在阶段 5 的计费。3 次查询同表一轮由 8 降到 4；6 对象宽查询由 24 降到 12。`_CHECKS_PER_LISTED_OBJECT = 5` 的注释依据（记录、暂存、提交校验、回放）随之过时；本切片只更新注释，不降低倍数——降低只会允许更小的配置值，不带来安全收益，留作后续单独决定。

## 6. 测试

- **更新** `test_evidence_scope.py::test_a_turn_succeeds_at_the_check_limit_and_stops_before_io_beyond_it`：`needed == 6 * 4` → `6 * 2`，注释同步；“超限在探测前拒绝”断言保留。
- **改写** `test_session_policy.py` 中 `add_items` 拒绝外会话证据、拒绝调用绑定不符的用例：改为 `add_items` 接受，`commit_validated` 抛 `SessionUnavailableError`，底层 Session 没有写入。
- **新增** 回归：一轮 3 次同表查询 + 首次发送，StarRocks 零行探测条数 = 3（记录）+ 1（提交）+ 1（发送）；改前为 9。
- **新增** 失败路径（§4 第一行）：`record` 之后、`add_items` 之前撤销工具授权，本轮以 `answer_rejected` 结束，会话没有新增轮次，证据不出现在任何交付里。另做一条对象依赖失效的同类用例。
- **新增** 群审批续跑、等待期间有其他成员提交历史（在 `test_waiting_releases_group_and_resume_commits_after_other_investigation` 的装配上扩展）：
  - 新历史正常：续跑提交成功，新历史的依赖确实在第 3 步被复核（计数 > 0），且只探测未覆盖部分；
  - 故障在 `app.py:951` 续跑的最后一次 `get_items` **之后**注入：新历史的对象依赖确定失效，本轮报 `session_unavailable`。断言 Session 的消息和轮数都没有增加；审批和动作记录保留实际状态（审批在 `app.py:1458` 已领取，动作可能已在 `actions.py:672` 保存成功结果）；已执行的动作不会重复执行。故障不能提前注入，否则会在 `get_items` 就失败，绕过要验证的提交补验分支；
  - 同一位置暂时无法复核：报 `scope_unverifiable`，其余断言同上。
- **新增** 定时任务来源校验（独立用例，不与群审批续跑组合：自动任务不进入审批暂停，见 `app.py:935`，`source_records` 只由自动任务提供，见 `app.py:989`）：来源失效时拒绝；来源与本轮证据的同一对象在第 2 步同批去重计费，额度计数等于实际探测数。
- **新增** 复合故障：历史引用的调用绑定不符，同时对象依赖暂时无法复核，`get_items` 和提交回放都仍报 `scope_unverifiable`，用来锁定“依赖复核在绑定检查之前”的顺序。
- 保留并运行：`test_sdk_approval.py` 的额度连续性断言（暂停/续跑计数等于实际探测）、`test_session_capacity.py`、`test_sql_parse_recovery.py`、`test_starrocks_tools.py`、`tests/security`。
- 门禁：CI 普通 job 等价命令、相关 sdk_core 与 PostgreSQL 套件、`ruff check .`、`mypy src`。

## 7. 风险与审查重点

1. **集合不等是正常路径**：群审批续跑时，提交回放比 `context_evidence` 多出等待期间提交的历史。改后靠第 3 步对未覆盖记录补做 `validate_records` 来保证不漏验，§6 有专门的回归测试。
2. **暂存不复核的出口**：r1 审查确认没有其他读者。`pop_item` 和暂停保存的依据已按 §2 修正。
3. **错误归类**：按 §4 的表格执行，接受加粗的两项变化。
4. 本计划只减少同一状态下的重复探测；不改变哪些边界复核、复核什么。

## 8. 实施与交付

获准后在主题分支实施，按 §3.1 → §3.3 顺序串行，测试同步。属于 §6 表中“数据/Evidence、Session”类改动：须正常与拒绝路径、真 SDK 与实际存储验证，并对具体差异做独立审查。不推送、不合并、不部署，不调用真实模型或公司服务。
