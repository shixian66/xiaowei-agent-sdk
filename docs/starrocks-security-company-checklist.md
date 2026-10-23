# 手动安全巡检：公司侧操作清单

已收到用户在公司执行的部分 A0：3.5.14 / AuditLoader 5.0.0、固定列存在、代表小时 237398 行 / ERR 3 / 按 16000 阈值超长 135，旧粗筛候选 117。以下基础聚合不必在同一窗口重复执行；目前只补来源集群、隔离测试环境、实际 SQL/候选字节与截断配置、脱敏/拆行结论，以及最终 LIKE/REGEXP 谓词在 3.5.14 的候选数、字节量和语义。旧 117 不代表新谓词已通过容量验证。计划 blob 340af95 已独立复审通过；本地已实现取数、分析、接线与短 Skill，真实源与模型仍待公司验收；本地不连接公司源或模型。

## 1. DBA 执行 A0（只读）

先确认本次获准目标、只读账号安全引用和一小时时间窗。将下列库表、时间、16000 替换成已有 `audit.database/table`、按 `audit.time_zone` 换算的起止时刻、实际 `policy.max_sql_bytes`；保留原有查询超时/资源限额。不按 isQuery/catalog 过滤。仅回传版本、列名/类型、数值与是/否，不回传原文、账号、IP、口令或配置全文。

```sql
SELECT current_version();
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
FROM information_schema.columns
WHERE TABLE_SCHEMA = 'audit_database' AND TABLE_NAME = 'audit_table'
ORDER BY ORDINAL_POSITION;

SELECT COUNT(*) AS window_rows,
       COALESCE(SUM(CASE WHEN state = 'ERR' THEN 1 ELSE 0 END), 0) AS err_rows,
       COALESCE(SUM(CASE WHEN stmt IS NULL OR stmt = '' THEN 1 ELSE 0 END), 0) AS no_text_rows,
       COALESCE(SUM(CASE WHEN LENGTH(stmt) > 16000 THEN 1 ELSE 0 END), 0) AS unread_sql_rows,
       COALESCE(SUM(LENGTH(stmt)), 0) AS sql_bytes
FROM audit_database.audit_table
WHERE `timestamp` >= '2026-10-23 10:00:00' AND `timestamp` < '2026-10-23 11:00:00';
```

同时回传：AuditLoader 版本；既有候选行数/字节上限、SQL 字节上限；聚合耗时。DBA 在公司核实下列事项，只回是/否或整数：各 FE 是否覆盖；GRANT/DDL、语法错误、连接事件是否入表；多语句是否拆行；口令语句是否脱敏；isQuery/catalog/queryType 是否符合当前模板假设；审计写入时区是否与配置一致、典型延迟秒数。字段不存在或无法确认就标未知，不扩大查询或输出原文。

**粗筛语义验收（3.5.14，不读表）：** A1b 提供最终固定谓词及同一组合成样例后，先逐条执行 `SELECT REGEXP('<合成SQL>', '<最终分支谓词>') AS actual;`，LIKE 分支用相同字面量执行相同 LIKE 表达式；大小写归一、SQL 字面量转义和正则转义必须与最终采集 SQL 一致。这里只把合成 SQL 当字符串，不执行其中的语句。先验各分支，再用同一字面量代入最终谓词并集核对：

| 合成样例 | 预期粗筛结果 |
| --- | --- |
| `SELECT 1OR 1=1`；OR 后换行/空白/多层左括号再接数字或引号常量 | OR 分支 1 |
| `SELECT floor(1)`；`SELECT 0 OR column_a=1` | OR 分支 0 |
| `SELECT 0 OR/**/(1=1)` | OR 分支 0，首版已知缺口，不当作包含关系通过正例 |
| 前导普通注释后的 GRANT；`INSERT/*a*/OVERWRITE t SELECT 1`；`INSERT /*+ SET_VAR(a=1) */ OVERWRITE t SELECT 1` | 管理前缀分支 1 |
| `ALTER TABLE t DROP PARTITION p`；`CREATE TABLE t AS SELECT 1` | 管理前缀分支 0；无其他信号时并集 0 |
| `SELECT 1; /*a*/ DROP TABLE t`；`SELECT 1; DELETE FROM t` | 管理/DELETE 前缀分支 0，多语句分支及并集 1 |
| `/*a*/ DELETE FROM t` | DELETE 分支 1；全文含 WHERE 或 WITH…DELETE 仍按计划列为缺口 |

语义通过后再在获准一小时窗口测最终并集候选数、按实际 SQL 阈值返回的候选字节，以及同一连接依次执行聚合/候选的合计耗时。只回版本、谓词对应代码/文档版本、样例编号的预期/实际 0或1、计数/字节/耗时；不回原文或身份。这是公司验收门槛，证据同时记入 handoff；未验证则标待验收，不新增“粗筛已验证”的运行时配置。`match_counts_complete` 仅等于“候选未截断且无法判定候选为零”，全窗超长数与无法判定候选数分列不相加。两次查询不承诺同一快照，近期窗口保留晚到日志限制。

## 2. 公司隔离测试环境生成样本（已选 A）

只在 DBA 确认的独立测试库执行，不向生产回放。先记测试起止时刻，等待已确认的审计入库延迟后再查；下列三个 SELECT 不读取业务数据。

```sql
SELECT 1 AS xw_security_boolean WHERE 0 = 1 OR 1 = 1;
SELECT SLEEP(1) AS xw_security_delay;
SELECT 'sleep(1); DROP TABLE harmless' AS xw_security_literal;
```

S4 由 DBA 在测试库选定一个无数据临时表和既有测试角色：做一次仅限该表 SELECT 的 GRANT、一次 TRUNCATE，以及一次针对已确认不存在测试表的 DROP（预期失败）；保留公司内操作记录并清理临时授权。不得对业务对象操作。多语句记录形式由 DBA 用相邻的两个 `SELECT 1` 核实，不能据此假定生产所有客户端相同。

对照只在公司执行：在上述时间窗内按对应测试动作统计条数与 ERR 数；需要定位时 DBA 在公司按 Query ID 查原记录，外部只回计数/是或否。全窗 COUNT/ERR 不是规则命中数，正常历史窗口不要求恰好出现离线的 12/3/2。

S2/S4 的测试记录可在公司按下列形式回查最小指标，不执行审计原文；替换时间、库表和本地 Query ID。只在公司对照状态、耗时和排队，外部仍只回编号、计数/是或否与打码截图：

```sql
SELECT queryId, state, queryTime, pendingTimeMs
FROM audit_database.audit_table
WHERE `timestamp` >= '2026-10-23 10:00:00' AND `timestamp` < '2026-10-23 11:00:00'
  AND queryId IN ('DBA本地测试QueryID1', 'DBA本地测试QueryID2')
ORDER BY `timestamp`, queryId LIMIT 20;
```

## 3. A1b 定稿、发行包明确后部署与实战（当前待执行）

沿 [既有安装、升级与回退流程](../deploy/OPERATIONS.md) 核对发行包 SHA/镜像摘要，先在隔离测试环境部署；保留公司配置、凭据和数据卷。显式加入 `data_policy.model_tools`；个人配 `access.grants`，群配 `feishu.group.tools`。受影响的数据策略需新建会话。定时复用规则见 [ARCHITECTURE §6](../ARCHITECTURE.md#6-只读查询保护)；tracing 维持关闭。

每条问题先明确环境与上述绝对时间窗；替换下表的“本窗/这些记录”，不把固定工具调用顺序当验收条件。

| 样例 | 要问小维的话 | 公司侧对照与通过标准 |
| --- | --- | --- |
| S1 | 检查测试环境本窗数据库安全情况，出 Markdown 报告 | 对照 COUNT/ERR 与采集覆盖；范围、计数、线索、限制齐全。有限扫描不能说全窗无风险 |
| S2 | 这些约 1 秒记录是时间盲注吗？ | 对照 SLEEP、状态、耗时/排队；识别延迟线索，不确认应用注入成功，不把普通慢查询说成攻击 |
| S3 | 检查这批正常 ETL/维护记录，其中 TRUNCATE 已获批 | 字符串/注释/普通 UNION 不被确定为攻击；TRUNCATE 事实仍展示并说明维护背景 |
| S4 | 有没有改权限、删表等值得核查的操作？ | 对照测试 GRANT、TRUNCATE、失败 DROP 的分类计数、代表样本和状态，不能说失败 DROP 已删表 |
| S5 | 这个窗口能否完整判断风险？ | 选实际超过候选预算或包含晚到日志的窗口，对照 COUNT 和覆盖；如实说明缺口、截止时刻与后续步骤。无法制造的失败变体保留离线证据和真实缺口 |

先各运行一次，出现不稳定或高风险错误后定向重复。每条回传：部署 SHA、请求编号、计数/是或否、打码截图；截图遮住 SQL 原文、账号/IP、凭据及连接信息。另记工具名/次数、是否自行补取原文、澄清/修正是否合理、耗时与模型用量。飞书选一个格式确认实收可读，Web 验一次现有交付；未执行或失败项明确保留，不自动升级为验收通过。

SLEEP 参数口径以 [StarRocks 官方文档](https://docs.starrocks.io/docs/sql-reference/sql-functions/utility-functions/sleep/)及目标版本为准；AuditLoader 覆盖以公司实际结果为准。

有源码开发环境时，可选用既有真实模型采集器先验同一 S1–S5 合成金标；`security` 默认各一次，不强制三次，不用它代替 3.5.14 源验证。按 README 准备隔离测试 PostgreSQL，Profile 只用公司已批准的安全配置引用，源码树须为干净的明确版本；在交互终端设置 `XW_EVAL_PROFILE` 后运行：

```bash
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \
  uv run --locked --extra dev python -m scripts.dialogue_real_model --profile "$XW_EVAL_PROFILE" --suite security
```

采集器沿用安全结构化用量/耗时记录和关闭 tracing 的约定；合成正文只供即时人工核对，不进入机器报告。SQLite/正则替身不证明 StarRocks 字节、权限或 REGEXP 语义；替身不支持的合法 SQL 单列。仅发行包环境可直接按上表手动验收，不要求为了采集器额外搭建开发环境。

## 4. 报告呈现验收（另获部署授权后执行）

本次呈现修复的本地实现不等于公司验收，产品口径见 [ARCHITECTURE](../ARCHITECTURE.md#6-只读查询保护)，场景和验证范围见[呈现计划 §7–§8](superpowers/plans/2026-10-23-security-inspection-report-presentation.md#8-公司验收截图与交付)。沿用原获准目标、Profile、时间窗和对照查询，不为排版重验全部 S1–S5；部署/发行另按用户授权。截图保留请求编号、实际版本、计数与时间口径，身份等打码，无需回传原始审计 SQL。

- 正常报告首屏：飞书、Web 各一张，结论、覆盖和紧凑检查表可见；按当次 Evidence/DBA 对照数核实，不固定要求历史的 29,232/ERR 6/多语句 1。
- 同一请求展开技术区：任选一端一张，确认 Query ID 能对应样本。若实际走纯文本则记录载体并核对业务正文，不要求折叠。
- 无命中、不完整：各任选一端一张；不完整优先用 brief，仍可见覆盖和下界计数。无日志和无法判定不当作无风险。
- Web 普通 `run_readonly_query`：展开技术区截一张，确认多行实际 SQL 可直接阅读/复制，字符串单元格不加外层引号，NULL/空串/缺失标记符合现有约定；本次不额外扩展其他工具的公司验收矩阵。
- 本次实际请求 MD/HTML 或触发容量文件时，再补一份实际格式的文件截图；不要求两种格式全验，也不重跑上传/恢复矩阵。

每个选中场景首轮一次，不稳定再定向重复；回传打码截图、请求编号与计数，并沿既有约定记录模型工具选择、说明质量、调用数、耗时与用量。离线脚本模型不能证明真实 Agent 的结论先行、选色及原因解读，真实飞书客户端效果也仍待本节验收。

## 5. A/B 故障恢复验收（另获部署授权后执行）

本地只验证脚本模型、协议/驱动替身与真实 SDK/隔离 PostgreSQL，不证明公司服务行为。按 [A 计划](superpowers/plans/2026-10-23-scheduled-agent-first-run-recovery.md)、[B 计划](superpowers/plans/2026-10-23-security-inspection-incomplete-results.md)的既定样例验收，回传打码请求号/任务号、固定原因码、计数与截图：

- 图片能力开启的原飞书群定时任务正常首跑；隔离测试环境一次失败可见，下一周期可继续，每次仅一条状态或结果。
- 一次审计源快照恢复、一次多目标部分失败；对照无名结构日志数量及当次 Evidence，正常目标保留，未完成原因与时间窗准确。不要用旧夹具数值作线上预期。
- B2/B5 的真实 Agent 原因和结论质量：不把缺表编造成超时/内存，不把超长当无法判定、不凭 ERR/多语句确认语法探测/ETL；未取得结果不写总体绿色。漏调用目标自动检测延期，不能据无失败提示确认所有目标都查过。A0/A3 遗留项照旧保留。

## 6. C 纯文本通知验收（另获部署授权后执行）

仅取两类代表证据，沿 [C 计划 §7](superpowers/plans/2026-10-23-security-plain-notification.md#7-沿用的业务验收样例与公司缺口)的 C1/C2/C3，不重做全量组合：

1. 一条真实纯文本通知截图与脱敏请求号：结论/样本概括来自模型，归纳数字有说明；对照当次 Evidence 检查计数、样本数、本地时区，无逐行样本。代理链接选其他合适业务页面，不指向小维 Web UI。缺省 openUrl 行为仍未知，本次不改必填协议，不试探公司接口。
2. 同类事实的 Web/卡片代表截图：brief 默认显示覆盖与检查项，明细仍能按当前权限展开；另用真实 Agent 的零命中但无法判定案例核对文案。模型是否合理概括、是否不推测 ERR/ETL、真实群内收到与 A0/A3 遗留项分别记录，本地浏览器/脚本模型通过不代替公司验收。
