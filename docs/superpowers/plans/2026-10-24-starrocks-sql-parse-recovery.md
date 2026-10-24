# StarRocks SQL 解析失败的预算内自主修正计划

状态：待独立审查；仅完成调查和计划，本文件不代表实施、真实服务调用、推送、PR、合并或部署授权。

**目标：** 用户要求排查告警时，模型提交的只读 SQL 被 FE 明确拒绝解析后，小维返回安全、可操作的工具反馈，由同一个 SDK Runner 在现有预算内继续调查；失败本身不生成成功 Evidence，也不抹掉已有的有效结果。

**版本与依据：** 公司运行镜像标签及本地 `origin/main` 为 `3aad626b42fc4ef2f37cafe342cf1826d453ca30`；调查工作树为 `/Users/kloenguyen/.codex/worktrees/manual-starrocks-security/agent-SDK`，分支 `codex/security-plain-notification`，HEAD 为 `e56eb24fc8d609134a5d0670ebb970e01ec70f9a`，两者 Git tree 均为 `572710d262ba2dfe859b8468bb33d55a50e7ca5d`。创建本计划前工作区干净；不改 `/Users/kloenguyen/Desktop/agent-SDK`。锁定依赖为 Agents SDK 0.22.3、asyncmy 0.2.15、SQLGlot 30.17.0。

**执行方式：** 计划通过审查并另获实施授权后，按 P0 → P1 → P2 串行实施（executing-plans）。届时核对最新 main，建立独立分支；本轮不建实施分支、不提交。需求和设计集中在本计划，不另建重复规格文档。

## 1. 已确认事实与根因

- 两次请求最终均为 `reason=tool_failed tool_error=query_failed tool_id=local/run_readonly_query`，不是 `answer_rejected`。此前的多次 `invalid_arguments` 为另一类前置拒绝，本切片不推断其具体原因。
- 审计记录为 22:08:15 / 22:09:26 的 `ANALYSIS_ERR`，分别耗时 3ms / 1ms；同一 FE 日志在 22:08:15.368 / 22:09:26.028 出现 `ConnectProcessor.handleQuery: Failed to parse SQL`，相应连接属于小维只读账号。这里只证明排查查询解析失败，不证明 Compaction 告警根因。
- 上次按时间 grep 漏掉了多行异常续行，尚未取得两条完整解析错误说明和行列位置；不把疑似保留字别名当成已确认的具体语法错误。
- `starrocks.py::_query_code` 没有解析错误类别；`_query` 只返回错误枚举，`_run` 再构造固定错误，安全细节已丢失。合成探针及 `test_starrocks_adapter.py::test_query_failures_are_mapped_without_the_server_text` / `test_failures_while_reading_discard_the_connection` 证明：语法、内存和超时文本可能同为 1064，不能只按 1064 判断。
- `governance.py::GovernedTools.invoke` 将普通 SQL 执行异常转为 `ToolExecutionError`；`tools.py::_function_tool` 不为这类错误返回反馈；SDK 异常传到 `app.py::_turn_error` 后中止整轮。`ARCHITECTURE.md` §6、应用指令和 `database_query.md` 均保留了这一契约。
- SDK 0.22.3 的公开 `FunctionTool.on_invoke_tool` 支持返回错误字符串给模型；小维已有 PromQL 的 `PromQLExpressionError → 可信失败记录 → 结构化反馈 → 当前 Runner 继续` 路径。`RunConfig.tool_error_formatter` 在锁版中仅面向审批拒绝/工具不存在，不是本问题的执行错误恢复入口。
- 应用还要求所有已开始查询都有成功 Evidence 或可信失败去向（`app.py` 约 923 行）。只捕获异常并返回文本，会遗漏失败记账，可能再触发 `query_unfinished`。只有解析失败、没有成功结果时，也须让现有预算收尾识别该失败记录（约 808 行）。

源码位置均以以上基线为准；实施可据实调整内部函数名，不以函数命名偏好作为审查阻断。

## 2. 推荐方案与范围

采用现有受治理 function tool 的窄错误反馈路径，SDK 继续承担全部工具选择和模型循环。模型可以修改 SQL、查询必要结构、选择其他已获准证据，或说明证据不足；不要求先查结构、先 EXPLAIN，也不强制再试一次。

恢复范围是 **`local/run_readonly_query` 和 `local/explain_query` 中，模型提供的 SQL 在发送执行时得到的明确解析失败**。两者共享 Adapter 和 SQLGuard，诊断用途本来就能使用 EXPLAIN，因此一起覆盖同一根因。固定元数据 SQL、审计采集 SQL、会话设置、结果读取、写操作和结果不明不进入该例外，继续原错误处理。

本次是对“执行后失败中止整轮”的明确契约调整：请求已经到达 FE，并非 I/O 前拒绝；仅在确认解析阶段拒绝、没有进入该语句业务执行时允许修正。没有增加读取权限、工具或数据范围。已获准的手动/定时 Agent 调用复用同一工具行为，本切片不另改调度或通知契约。

备选是不细分错误而把全部 `query_failed` 转成模型反馈；不选，因为它混有资源错误和结果不明，扩大已批准边界。全局 SDK 异常转文本也无法替代业务分类和可信失败记账，不采用。仅加提示词不能改变实际中止分支。

不新增依赖、配置开关、独立 Agent、修复 Runner、通用错误框架、修复次数配额或 SQL 去重/黑名单。现有工具次数、SDK 轮次、整轮期限和资源限额已限制修正成本；没有本次证据支持再加同一边界的限制。

## 3. 最小接口与调用链

### 3.1 Adapter：只保留受控诊断

- 在现有 StarRocks 受控异常上保留“已确认解析失败”及可选 `line` / `column`；不携带原始异常文本、SQL 片段或异常原因链。沿用现有 `query_failed` 的通用错误码/固定文案兼容其他消费者，不把新分类自动推广到所有 Adapter 方法。
- 识别依据由 P0 核实：目标版本的稳定错误码与明确解析错误格式组合；解析签名必须锚定在错误信息开头，不能只做包含匹配。1064、`ANALYSIS_ERR` 或文本中单独出现 `syntax` 都不是充分条件。行列位置是可选诊断，不把“没有位置”本身当成不可恢复理由；只支持已有可靠证据的格式，不预建多版本/多语言解析器。
- 若 P0 发现目标版本的位置格式不稳定，直接省略 `line` / `column`，不写多版本位置解析。
- EXPLAIN 实际发出 `EXPLAIN_PREFIX + SQL`；位置必须对应模型写的原文。首版采用第 1 行省略 `column` 的较小方案，不直接返回包含前缀偏移的列号；后续行只返回能稳定对应原文的位置。
- `_query` 的错误返回槽可由枚举改为受控异常对象，`_run` 在离开捕获原异常的上下文后抛出该对象；本地已核实 `_query` 只有 `_run` 一个调用方。其他返回值和公开 `QueryResult` 不变；`_query_code` 及其他 Adapter 路径原映射保留。
- 恢复识别落在业务 SQL 的 `execute` 失败位置，不能把会话设置失败或取结果时的异常当成“语句尚未执行”。失败连接仍按现有路径关闭/丢弃，不复用、不自动重发原语句。

### 3.2 治理与 SDK：记账后交回，不代替模型决策

- 只对 §2 的两个可信工具转换上述诊断；代码模板解析失败继续作为工具故障。使用当前受控异常/工具返回模式，不全局吞异常。
- `TurnRuns` 增加带默认空值的解析失败记录，例如 `sql_parse_failures: tuple[ToolId, ...] = ()`，每次实际失败追加一次。它的消费者只有查询去向核对、预算收尾和已有暂停账本，不存 SQL、账号、IP 或业务结果，不另建数据库表或事件仓库。
- 已开始调用正常消耗现有工具预算，不退款，也不调用表示 I/O 前拒绝的 `note_rejected`。并发时从当前账本追加，不能用 await 前的旧快照覆盖其他成功或失败。
- 解析失败说明 SQL 已在拿到连接槽位后发出，须和成功路径一样清除本次调用对应的 `slot_busy`；`slot_retries` 不复位，与 C8“额度不复位”一致。否则“繁忙 → 再试时解析失败”会留下 `slot_busy` 和 `slot_retries`，使该目标之后的修正调用被准入条件误判为繁忙。
- 在现有 logger 的分类点记一条安全 INFO：turn、call_ref（现有 call_id 哈希方式）、tool、target、固定 code、可用行列、correction_allowed。只记这些字段，不记录 SQL 和原始错误正文。
- FunctionTool 返回的最小反馈如下；字段名是本切片的新反馈契约：

```json
{
  "status": "invalid_sql",
  "source": "pre",
  "reason": "SQL未通过解析；勿原样重发，可在本轮剩余预算内检查语法并修正，或说明未完成",
  "correction_allowed": true
}
```

按 §3.1 确认位置稳定且对应模型原文时，附加整数 `line` / `column`；其余位置省略，不伪造。`correction_allowed` 沿用现有修正语义，只表示治理工具额度尚有余量；SDK 轮次和整轮期限仍独立生效，不承诺下一次调用一定能执行。反馈没有 evidence_id，不能作为成功事实引用。

### 3.3 应用、Session 与历史

- 查询去向核对将上述记录按实际工具计入明确失败；保留 `query_unfinished` 对真正去向不明查询的阻断。
- 只有解析失败的轮次也可沿用现有预算耗尽收尾；不增加模型调用额度或新的收尾循环。正常返回后模型可以直接说明未完成，不强制耗尽预算。
- 已有成功 Evidence、当前权限复核、最终引用校验和各渠道交付继续原路径；失败不生成成功 Evidence，解析失败也不使之前的有效结果消失。
- `TurnLedger` 已由 `TypeAdapter` 序列化/恢复整个 `TurnRuns`，新字段默认空值，旧暂停账本可读取；恢复后计数保留，已执行调用不重放。只追加必要账本字段，不新增恢复存储层。
- 普通 Session 对无 Evidence 的工具输出已有 `NO_EVIDENCE_OUTPUT` 投影，保持该行为，不为本片把详细错误写入历史。新反馈不进入成功 Evidence，既有 Evidence/TurnAnswer 版本和历史结果格式无需升级，无 schema 迁移。
- 需同步修订的现行表述列全如下（行号以本计划基线为准）：`ARCHITECTURE.md:301`（C8）、`:314`（精确审计取数）、`:330`（R7），以及 `src/xiaowei/skills/database_query.md:32`；这些“执行后失败中止整轮”表述均补上明确解析失败的窄例外，相关应用指令同步消除冲突。权威行为契约在 `ARCHITECTURE.md` 维护，其他文档按职责引用。不新增 Skill，也不改变 `database_security.md` 的采集/报告规则。

## 4. 新增限制与保留边界

本切片没有新增通用拒绝条件；以下是窄恢复例外的识别边界，不是把“本期不做”改成禁止。

| 边界 | 具体失败场景及现有机制为何不足 | 最小处理 |
| --- | --- | --- |
| 明确解析失败才恢复 | 1064 / query_failed 混有超时和内存错误；现有粗码不能证明未执行业务 SQL | 仅匹配 P0 验证的解析格式；不匹配继续原处理 |
| 仅模型 SQL 的两个只读工具 | Adapter 也执行代码自有 SQL；将其故障交给模型会让模型无从修复模板，并掩盖程序问题 | 在可信工具边界限定例外，不增加权限检查层 |
| 错误到模型的字段白名单 | FE 的实际日志已含 SQL；成功结果的 Evidence 投影并不覆盖异常正文，直接转文本可能外泄 | 固定类别/说明和可选位置；日志与 SDK 暂停态也不含原错误正文 |

每次新调用的授权、SQLGuard、对象范围、预算及结果/Evidence 验证均复用现有机制，不重复实现。权限、超时/资源、连接中断、未知错误、结果不明、Evidence 和存储错误仍按现有边界处理，不因本例外放宽。若 P0 发现必须恢复其他类别才能完成任务，先列依据并局部修订计划，不混入实现。

## 5. 实施任务与文件

### P0：核实可恢复解析格式

- [ ] 核对目标 StarRocks 的解析错误返回码/格式，使用对应版本源码、已有完整脱敏异常或隔离环境无害合成语句验证；不重放审计 SQL，不为本片开启真实 tracing。
- [ ] 在 `tests/p1b/test_starrocks_adapter.py` 先补分类正例及 1064 内存、超时、会话设置、读取阶段和“运行期错误正文中带有解析错误字样”的反例，验证包含匹配不会误放行，确认新正例在修复前失败；只用合成 SQL，不复制公司 SQL。
- [ ] 在 `src/xiaowei/starrocks.py` 完成最小受控诊断，验证其他错误映射、连接关闭与公开结果不变。

退出条件：能依据明确签名区分解析错误和已有反例；不能用全放行 query_failed 代替。公司实测返回格式尚未核实时标明差异和缺口；独立离线工作可继续，不宣布真实目标兼容通过。

### P1：尽早跑通正式应用入口

- [ ] 修改 `src/xiaowei/governance.py`、`src/xiaowei/tools.py`、`src/xiaowei/app.py`，接通分类反馈、失败记账、查询核对和现有预算收尾；必要时仅在 `starrocks_tools.py` 调整具体反馈说明，不另加包装平台。
- [ ] 新增 `tests/sdk_core/test_sql_parse_recovery.py`，复用 `test_starrocks_tools.py` 的 `assembled`、RecordingScripts、驱动与 PostgreSQL fixture；使用真 SDK/治理/Adapter/Evidence/PolicySession，外部数据库和模型为替身。
- [ ] 先跑 S1 成功闭环，再跑 S2 预算耗尽失败闭环；记录实际命令、HEAD、工作区差异状态及结果。不能仅直接调用分类 helper 或手工构造成功 Evidence 代替。

退出条件：首次解析失败后可以继续，修正成功才产生 Evidence，之前的成功事实保留；全部解析失败也能收尾说明未完成，而非误触发 query_unfinished。

### P2：边界、兼容与交付说明

- [ ] 在同一新测试文件及现有 Adapter 测试补 S3；补一个“一次失败＋一次成功”的并发记账对照，不做并发数量矩阵。
- [ ] 补“繁忙 → 再试时解析失败 → 修正后的调用成功”：断言解析失败后 `slot_busy` 清除、`slot_retries` 保留，修正调用不因旧繁忙状态被拒绝。
- [ ] 保留位置时，补 EXPLAIN 原文位置对照：第 1 行不返回 `column`，后续行返回的行列位置对应模型写的 SQL 原文，而非带前缀的实际语句。若 P0 已整体省略位置，只断言反馈无位置字段，不再开发或测试多版本位置解析。
- [ ] 用现有 `tests/sdk_core/test_sdk_approval.py` / `tests/spike/test_sdk_resume_ledger.py` 的机制验证旧账本缺字段、含解析失败的新账本暂停/恢复；确认不重放 SQL。只补受影响节点，不重跑全部审批组合。
- [ ] 按 §3.3 的完整表述清单更新 `ARCHITECTURE.md`、`src/xiaowei/skills/database_query.md`、相关应用指令和 `AGENT_HANDOFF.md` 的实际状态/验证缺口；Skill 契约检查放在现有 `test_database_skills.py`。README、部署手册没有新命令时不重复修改。
- [ ] 按 §7 完成检查，对最终实际差异做独立审查；只有获得后续对应授权才提交、推送或创建 PR。任何阶段均不把离线通过升级成公司验收。

## 6. 固定业务验收样例

这些样例沿用到计划、开发、审查及公司验收；不另定固定工具顺序，不要求模型逐字相同。

| 样例：用户操作 → 系统行为 → 用户结果 | 成功标准 | 不能接受的结果 |
| --- | --- | --- |
| S1：用户“排查 pre 上该 BE 的 Compaction 压力告警” → 已有一份获准指标事实；后续只读 SQL 被明确拒绝解析；模型根据反馈修正，或选择必要结构/其他获准证据后完成 → 交付实际获得的事实、判断和限制 | 离线脚本走一次失败再成功；失败与成功均占预算，失败不产 Evidence，成功及此前事实可引用；用户实际根因只在证据充分时判断 | 首次解析失败即整轮退出；仅换成错误文本后被 query_unfinished 拒绝；伪造成功或用旧告警值冒充新查询 |
| S2：同一任务下语法仍未修正、剩余额度耗尽，或模型判断现有能力不足 → 保留已取得事实并按现有收尾路径说明未完成 | 失败次数与实际调用一致，无额外修复额度/自动重放；无成功结果时给未经数据验证的说明 | 无界循环、退款绕过预算；声称已定位告警或健康；把明确失败漏记成系统去向不明 |
| S3：同一任务发生权限拒绝、1064 内存/超时文本、连接中断或未知错误 → 保持现有对应处理 → 如实说明结果，不进入解析修正例外 | 参数化确定性反例验证；敏感哨兵按 §7 的两处断言验证 | 将所有 1064 / ANALYSIS_ERR 都解释成语法错误；自动重放未知结果；为“修复”跨目标、放宽授权或绕过 SQLGuard |

脚本模型只证明可达调用链、结果和边界；真实模型是否会选对工具、有效修正、保留问题语义并给出合格诊断，单独验收。

## 7. 验证成本与证据

确定性分类和连接关闭复用 Adapter fixture；S1/S2 各跑一个正式 SDK/隔离 PostgreSQL 闭环，S3 用参数化边界测试。对并发、暂停恢复只补本次字段和计数相关节点，不为每个渠道或每个错误重复建立 Agent。

敏感哨兵断言只保留两处：模型收到的白名单反馈 JSON，以及该条 INFO 日志。Session/暂停态复用既有投影和白名单反馈路径，不再分别增加哨兵断言。修改后的写 SQL 被 SQLGuard 在发送前拒绝已有测试覆盖，该路径未改，不在 S3 重复新增用例。

计划命令（当前尚未执行；新增文件须在实施时创建）：

```sh
export SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres
uv run --locked --extra dev python -m pytest tests/p1b/test_starrocks_adapter.py -k 'parse or query_failures_are_mapped or failures_while_reading or session' -q
uv run --locked --extra dev python -m pytest tests/sdk_core/test_sql_parse_recovery.py tests/sdk_core/test_database_skills.py tests/security/test_docs_command_consistency.py -q
uv run --locked --extra dev ruff check .
uv run --locked --extra dev mypy src/xiaowei
git diff --check
```

再按实际修改节点选择既有 `test_app.py`、`test_starrocks_tools.py` 和审批/账本测试；命令及节点记录到 handoff，不机械执行整文件或本地全量。修改 Python 文件按项目现有格式检查。完整默认测试由后续获准 PR 的 CI 执行；CI 和模型调用不属于本轮计划授权。

展示格式未改，不新增浏览器截图、Web/飞书/HTML/MD 全组合测试；复用一个已获准结果的交付断言，确认失败后的成功回答仍走原交付路径。默认 tracing 关闭；机器证据只记版本、用量、状态、计数和原因码，不保存公司 SQL 或原始异常。

## 8. 尚未验证与公司验收

- 本次 FE 完整异常续行、驱动实际返回码/格式仍缺；既有 A0 的 3.5.14 结果不替代本次 pre 目标版本核对。P0 以目标版本明确依据定识别签名，不假装已经复现具体语法位置。
- 本轮不调用真实模型或公司服务。后续经授权由用户在公司执行 S1/S2 各一次；隔离测试环境使用无害合成 SQL产生解析错误，不重放生产审计 SQL。
- 回传部署 SHA、脱敏请求编号、解析反馈类别/位置、实际工具次数、成功 Evidence/交付是否正确、耗时和模型用量即可；不必回传账号、IP、SQL 原文或异常全文。真实模型不稳定时再定向复跑，不固定三次。
- 本片不会自动关闭原安全巡检 A0/A3、代理实收或其他历史验收缺口；也不承诺已找到原 Compaction 告警的真实原因。

审查重点：恢复签名是否可靠；两个用户 SQL 工具与代码模板的分界是否成立；失败预算/去向是否闭合；旧暂停账本能否读取；错误反馈是否有足够修正信息且未泄露原文；是否重复增加了 SDK 或现有治理已经承担的限制。
