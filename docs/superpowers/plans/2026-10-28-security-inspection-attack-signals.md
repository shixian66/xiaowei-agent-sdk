# 安全巡检检测范围收窄：最小修改计划

> 状态：2026-10-28，blob 1a43823 已通过用户转交的独立审查，并获准离线实施；本工作树按审查删减兼容分支和验证，完成情况见 handoff。
> 实施方式：获准后按 P1 → P2 串行推进，使用 executing-plans；不因本计划新增执行框架。

**目标：** 安全巡检聚焦注入、延迟攻击线索，取消按数据库操作类型触发的整组检测；多语句保留为辅助信息。

**名称：** 保持“安全巡检”、`inspect_audit_security`、`local/inspect_audit_security` 和 `database_security.md` 不变。

**方案：** 复用现有 SDK、Governance、StarRocks Adapter、Evidence、Session、报告及定时交付，只收窄粗筛、精判与报告口径。不增加白名单、风险评分、通用规则引擎、配置开关、依赖或新的 Agent Loop。

**需求依据：** 用户本轮明确取消 `high_risk_operation` 整组检测，多语句仅作辅助，保留注入与延迟线索；随后明确能力名称不改。用户随后授权开始开发；真实服务、推送、合并和部署不在本轮范围。

## 1. 基线与现有链路

- 已核对 GitHub `main` 与本地 `origin/main`：`0b401b2e40e906c240de7bebd4bcacd8da0a10ae`。以下源码位置均属于此版本。
- 原计划保存目录：`/Users/kloenguyen/Desktop/agent-SDK`；该目录实际为旧分支 `codex/model-authored-report-plan`、HEAD `b89d4ee71cb5082adcde396f3dece12fd169af48`，已有修改与未跟踪文件全部保留。本计划不代表桌面工作树代码已更新到主线。
- 审查源码用 `git show 0b401b2e40e906c240de7bebd4bcacd8da0a10ae:文件路径`。实施时核对届时最新 main，在隔离主题分支开发，不在桌面旧分支上实现。

| 已核实位置 | 当前行为与可复用部分 |
| --- | --- |
| `src/xiaowei/starrocks.py:581–638,948–1021` | 同一只读连接依次执行全窗聚合与候选查询；OR、SLEEP、多语句、管理操作和 DELETE 粗筛并集。时间绑定、候选行/字节、SQL 长度、超时与资源限制已有。 |
| `src/xiaowei/starrocks_security.py:35–43,104–259` | `_operations` 识别九项操作，任一命中加入 `high_risk_operation`；Tokenizer 另识别 OR 同值常量、SLEEP 和多语句。只处理已读取数据，不执行被审计 SQL。 |
| `src/xiaowei/starrocks_tools.py:224–242,1037–1147` | 装配工具、说明与容量检查；`SECURITY_FIELDS` 同时用于四种投影和 required。固定源列、授权后刷新、观察结果与 Evidence 接线可复用。 |
| `src/xiaowei/evidence.py:1003–1014,1102–1129` | 当前策略说明不参与指纹，但投影字段、required、资源/数据范围参与；旧证据读取仍检查指纹、归属、期限、来源和权限。 |
| `src/xiaowei/feishu_render.py:237–350,531–576` | 同一 `business_presentation` 生成覆盖、检查表、样本；当前统一展示高风险操作。纯文本已省略逐行样本。 |
| `src/xiaowei/web.py:189`；`src/xiaowei/feishu_report.py:135,180` | Web、MD/HTML 复用上述 presentation，不需要新渲染组件或 JS 分支。 |
| `src/xiaowei/operations_history.py:398–410` | 历史工具返回原 Evidence 投影中的 data，已有 `rules_version` 会随数据保留；无需新建历史记录或迁移。 |
| `src/xiaowei/skills/database_security.md:5–13` | 已区分事实、推断、覆盖缺口与未完成；已有“不因操作类型机械要求核查”的表达，但检测和检查表仍保留该整组。 |

关键判断：仅改 Skill 或隐藏检查表仍会产生操作候选、计数和样本，不能完成本次范围调整；整组删除比增加豁免名单简单。这里接受检测范围变窄，不能宣称覆盖所有恶意 SQL：盗号后直接执行合法语法的删除/授权等，若没有保留规则的特征，本工具不再单独发现。

## 2. 最小行为与数据变更

### 2.1 粗筛与精判

1. 删除 `SECURITY_OPERATION_PATTERN`、`SECURITY_DELETE_PATTERN` 及其候选 SQL 分支/参数；`_SECURITY_GAP` 若因此无消费者一并删除。全窗聚合 SQL、固定源列、排序与取数预算不变。
2. 保留现有 OR、SLEEP、多语句三个粗筛分支，使用现有 LIKE/REGEXP 形状，不顺带扩大注入语法支持。OR 到常量夹注释等现有缺口继续声明；离线包含关系只覆盖已声明支持的写法，不声称规则全面。
3. 删除 `_operations`、九类操作计数与按操作挑选样本的逻辑，清理因此失用的 SQLGlot AST 解析和 import。不再为取消的 DELETE 规则做 AST 解析；Tokenizer 失败、超长候选等保留规则仍无法判断的情况继续计入 `unclassified_candidate_rows`。
4. **不是整条 SQL 豁免：** 每条候选仍做三个保留检查。`INSERT OVERWRITE ... OR 1=1`、`DELETE ... OR 1=1`、`SELECT 1; DROP TABLE t` 分别仍可命中布尔或多语句特征。后一个只提供多语句辅助信息，不因为 DROP 又恢复高风险检测。
5. 样本继续复用“每类先保留代表，再填充”的现有逻辑，顺序为 `boolean_probe`、`delay_call`、`stacked_statements`，让辅助样本不挤掉已读取的攻击线索代表；不增加评分或新限额。多语句仍占候选预算，若候选截断就如实报告，不能据未读部分宣称无攻击；本期不增加第二套扫描策略。

取消的九项为 `grant/revoke/account/password/drop/truncate/export/insert_overwrite/delete_no_where`。这不改变一般审计查询、只读工具的权限或 SQLGuard，也不禁止用户通过原有获准能力另查这些操作。

### 2.2 输出、计数与版本

新结果使用 `rules_version="manual-security-v2"`；只表示本次检测口径，不升级 Evidence、TurnAnswer、数据库 schema 或 `DATA_SCOPE_FORMAT`。

| 字段 | v2 口径 |
| --- | --- |
| `signal_counts` | 仅包含 `boolean_probe`、`delay_call`、`stacked_statements`，三项均显式给出整数；不输出 `high_risk_operation: 0` 冒充做过检测。 |
| `matched_rows` | 命中上述任一保留特征的去重审计行数，**包含辅助多语句，不能叫攻击条数**；同一行多种特征只计一次，分类数不能相加。 |
| `operation_counts` | 保留顶层键，值为 `{}`，表示本规则版本不做该组检测；不保留九个零计数。 |
| `columns/rows` | v2 删除无消费者的 `operations` 样本列与单元格；其余最小元数据、Query ID、信号与延迟参数不变。列名属于 payload 内容，不在策略投影字段集合中，此删除不改变策略指纹。 |
| `scanned_rows/failed_rows` | 仍为读取时服务端全窗总行数/ERR 数，不因候选缩减变成候选数量。 |
| `coverage/truncated` | 继续按既有口径计算：候选未截断且无法判定候选为零才分类完整；全窗超长数与无法判定候选数分列，样本省略不降为不完整分类。 |

只保留顶层 `operation_counts={}`，因为它是现有 required 投影字段，删除该字段会改变策略指纹、使旧巡检 Evidence 失效；不为此保留内部操作计数状态或样本空列。生产逻辑不再使用 `OPERATIONS`。工具装配中的最坏输出容量夹具同步 v2、三个信号、样本列与空字典，继续调用现有容量检查。

不新增“攻击总数”字段：现有输出没有足够信息把所有命中确定为攻击。覆盖完整且布尔/延迟均为零时，报告可说“本次规则范围内未发现注入或延迟攻击线索”，即使多语句数大于零；不能说“数据库安全”或“没有任何恶意 SQL”。

### 2.3 报告与 Agent

- v2 检查表改为四行：**注入探测（OR 同值常量线索）／时间延迟调用线索／多语句（辅助信息）／执行失败（ERR，全窗聚合，读取时）**。多语句与 ERR 不单独支持攻击结论，不增加固定风险评分或颜色校验。
- v2 样本业务表省去“操作”列；信号中的多语句也标明辅助。Query ID 留在原技术详情。通过 `feishu_render._security_presentation` 一处复用到 Web、飞书卡片、纯文本、HTML/MD；不修改通用工具的显示规则。
- brief 下覆盖和四行计数仍默认可见。纯文本仍只显示范围、覆盖、计数、必要提示与原样本条数提示，不逐行输出；模型正文按原 blocks 顺序组织。
- `SECURITY_NOTE`、工具说明与 `SECURITY_LIMITATIONS` 写清 v2 的三个特征及计数身份，删除已取消的操作检测/DELETE 覆盖承诺。当前 fact_note 也会用于读取旧证据，统一写成“matched_rows 是命中本份 signal_counts 所列任一特征的去重审计行数”，不写两套版本说明。
- 短 Skill 说明：结合证据解释布尔/延迟疑点；多语句只有辅助意义；操作类型、ERR、耗时或 OR 常量命中不能单独证明恶意或攻击成功。模型自主决定补证、澄清和收尾，不加固定调查顺序、重复确认或额外调用。
- 不把删除规则写成“禁止查询这些操作”。沿现有未完成、覆盖不足与权限反馈处理，不增加拒绝条件；未知业务用途不编成 ETL，也不因服务账号直接判正常。

## 3. 历史 Evidence 与交付兼容

1. **旧事实仍按旧规则成立。** v1 原始 JSON、计数、样本、Query ID、已保存模型文字均不修改；没有原始 SQL，不能靠减去操作计数重算新规则的 matched_rows，重叠命中尤其不能直接相减。
2. **展示按实际字段选择。** 仅 signal_counts 包含 high_risk_operation 时显示原高风险行及操作分项；仅样本带 operations 时显示“操作”列。没有该信号键时多语句标为辅助信息。v1 业务展示原样保留，不增加版本分支、缺版本兜底或 v1 专用提示。rules_version 本来就在 required 中。
3. **无需迁移、清会话或失效全部旧证据。** 保持 `SECURITY_FIELDS`、四种投影及 required、工具身份、输入 schema、资源与数据范围不变。`rules_version` 是事实 payload 的检测标记，不参与现有策略指纹；工具说明/fact_note 的文字更新也不参与。交付时一次性核对并记录基线与候选的同配置指纹值，不增加固定指纹常量的永久测试。
4. **各历史出口沿现有保障。** 历史查看/读取、PolicySession、定时结果历史及接收方复核继续走当前路径；历史工具原样携带 v1/v2 data，Skill 依据本份 signal_counts 解释，不把旧操作计数当当前攻击线索。无需修改历史存储或 Session 模块。
5. **已绑定内容按原字节交付。** 已发送、已绑定重发、原附件保留当时内容；只有后续新生成的展示采用上述字段选择。新旧混合报告逐份注明来源与口径，不合并成统一攻击总数，不为本次改动自动重查原窗口。

备选是删除旧字段并改变策略指纹，让旧巡检证据失效；它会牵连历史和现有会话，没有本次所需收益，因此不选。也不保留旧检测器用于重算；新旧结果共用已有显示代码。

## 4. 新增限制与本期不做

本次不新增运行时权限、预算、拒绝、重试或颜色校验。以下是收窄检测后必须对齐的输出语义，不是新审批关口。

| 输出语义 | 具体可达问题、现有机制为何不足 | 最小落点 |
| --- | --- | --- |
| 多语句不能单独支持攻击结论，matched_rows 不能称攻击数 | 正常客户端多语句已实测；当前精判仍把它列入 signals，Evidence 只能保证计数真实，不能保证模型解释正确。 | 检查项标签、工具字段说明、短 Skill；沿用业务样例验收，不加代码颜色拦截。 |

已有的数据权限、原文边界、被审计 SQL 不执行/不 EXPLAIN、覆盖缺口、资源与接收权限保障沿 [ARCHITECTURE §6–§8](../../../ARCHITECTURE.md)，不复制实现。

本期不做白名单、历史行为基线、评分、用户行为关联库、额外攻击类别、自动处置、分页/新调度器；不改变任务每次发送、代理 openUrl 或现有一般查询能力。SLEEP/简单布尔特征不等于完整盲注检测，普通 UNION 不新增关键词告警，既有粗筛缺口与公司验收未完成事项保留。

## 5. 固定验收样例

沿用现有 S1–S5 编号，仅随批准的范围变化修订金标；计划、实现、复审和公司验收使用下表，不另立一组标准。规则判断看获准数据；真实窗口不要求等于合成夹具数值。

| 场景：用户操作 → 系统行为 → 用户看到的结果 | 成功标准／不能接受 |
| --- | --- |
| **S1 混合窗口：** 请求 pre 指定小时安全巡检 → 现有 12 行夹具含 OR 同值、SLEEP、GRANT，其余正常，ERR 2 → 覆盖为 12、ERR 2，布尔 1、延迟 1、多语句 0、matched_rows 2，两个代表样本。 | GRANT 不计命中；无高风险一行。不能把候选/样本当全窗，不把命中称已确认攻击。 |
| **S2 延迟：** 问三条条件 SLEEP 与一条仅排队约 5 秒是否盲注 → 复用状态/耗时/排队信息 → 三条延迟线索、失败状态仍可见；普通排队不命中。 | 保留既有参数解析能力，不新增条件求值；既不因约 5 秒确认攻击，也不把 ERR 说成调用成功。 |
| **S3 正常/辅助：** 请求检查普通 UNION、WHERE 1=1、字符串/注释内攻击词及 TRUNCATE → 粗筛后精判 → 现有 5 行夹具 matched_rows 0；变体为 61 行 `SHOW VARIABLES; SELECT 1` → 仅多语句 61、matched_rows 61。 | 变体可说明“未发现注入/延迟线索，多语句仅作辅助”；不称 61 次攻击或自行解释业务用途。纯文本无样本行，Web/卡片按原 view 保留明细，覆盖仍可见。 |
| **S4 取消整组且不豁免整句：** 请求检查覆盖写、删表/库、清表、授权等记录是否有注入/延迟线索 → 单独九类操作均不命中；既有 GRANT OK、DROP ERR、TRUNCATE OK 三行结果为 matched_rows 0、ERR 1 → 无操作告警。追加混合 SQL 反例：`INSERT OVERWRITE t SELECT 0 OR 1=1`、`DELETE FROM t WHERE 0=1 OR 1=1` 仍有布尔线索，`SELECT 1; DROP TABLE t` 仅多语句。 | 不能误删同一 SQL 上的保留信号；不能根据取消的规则保证操作已授权或无风险。带 HINT/前导注释的纯覆盖写也不命中。 |
| **S5 覆盖与失败：** 问窗口是否查全 → 保留候选截断、超长候选/分词失败、零日志或源拒绝的既有路径 → 按原因展示下界、未知或未完成。 | 既有 206 行夹具的超长 INSERT 现在是非候选，不再把它计为无法判定；205 条 SLEEP 仍可触发候选截断。另用已读取的超长 SLEEP 候选覆盖真正无法判定分支。无命中、未完成和无日志仍不同。 |
| **H1 历史兼容：** 查看/追问有效 v1 与 v2 结果 → 当前授权复核、按实际字段展示 → v1 旧操作计数与文字保留，v2 四项检查正确。 | 一条按 v1 真实结构保存的 Evidence 用当前 catalog 仍可读；v1/v2 各走共用展示，v1 展示与基线一致。已有绑定字节不改。撤权路径未改，引用现有有效证据，不新增重复用例。 |

S3/S4 的“纯操作不进候选”和混合 SQL 应进候选，要通过实际 `_security_sql` 模板与绑定参数执行到现有 SQLite/REGEXP 替身验证，不能只给 `analyze_security_rows` 喂记录。字符串/注释造成粗筛误选允许存在，但精判不能因此产生攻击信号。

## 6. 实施任务与文件范围

### P1：检测、字段与成功/失败闭环

- [x] 在 `tests/p1b/test_starrocks_security.py`、`test_starrocks_security_audit.py` 先补 S4 整组取消及混合语句反例，确认旧代码失败；同步 S1–S5 金标，删除只保护已取消操作/DELETE AST 分支的测试。
- [x] 修改 `src/xiaowei/starrocks.py`、`starrocks_security.py`、`starrocks_tools.py`，落实 §2.1–§2.2；更新真实模板参数和装配容量夹具，确认全窗统计不受候选减少影响。
- [x] 更新 `tests/sdk_core/dialogue_security.py` 的原问题/CRITERIA（S4 改问攻击线索而不是盘点操作），同步 `test_security_inspection_eval.py`。尽早用真 SDK、Governance、Adapter、Evidence、PolicySession 与隔离 PostgreSQL 跑通 S1 和一个 S5 拒绝/不完整路径，不能手造成功 Evidence 替代正式调用。
- [x] 核对测试驱动是否真的执行粗筛；原本无条件返回 GRANT 的 driver 不证明候选已过滤。删除操作代表优先用例，替换为大量多语句中保留少量已读取布尔/延迟代表的行为断言。

### P2：报告、历史、任务样例与交付记录

- [x] 修改 `src/xiaowei/feishu_render.py`、`src/xiaowei/skills/database_security.md`，落实 §2.3/§3。其他出口优先无需源码改动，只验证消费者；不为各端复制版本判断。
- [x] 扩展 `tests/sdk_core/test_presentation_v4.py` 的 v2 与保留 v1 用例；更新 `test_security_notification_paths.py` 的 61 条通知和连续两次定时用例。新通知用辅助多语句/攻击线索数据；旧覆盖写材料留在历史兼容用例。两次任务仍各发送一次，不因降噪静默停发。
- [x] 一次性记录新旧同配置指纹；在 `test_security_inspection_eval.py` 以真实旧结构投影写入隔离 PostgreSQL 后，用新 EvidenceStore/catalog 做 H1 读取。复用既有权限保障，不新增撤权或永久指纹断言。保留原文/敏感数据不进入模型和展示的必要断言。
- [x] 只更新受影响的权威说明：`ARCHITECTURE.md` 安全巡检段、`docs/starrocks-security-company-checklist.md` 的规则/样例，以及 `AGENT_HANDOFF.md` 的确切版本、命令、结果与缺口。旧计划标注检测范围被本计划替代并链接，不重写历史验收记录；README 只有实际使用说明受影响才改，不另写规则表。
- [x] 完成下节检查后交独立审查；是否提交/推送/PR/合并按届时明确授权，不由计划自动授予。

## 7. 验证范围与未覆盖项

### 计划编写阶段（已完成）

运行现有 `tests/security/test_docs_command_consistency.py`、检查本计划链接和源码位置、`git diff --check`（未跟踪计划另外作 whitespace 检查）；给出内容 blob，保留全部已有修改；该阶段未运行功能测试或真实服务。

### 实施时的离线检查

按 [AGENTS §6](../../../AGENTS.md#6-按风险验证与交付) 和 [README 开发验证](../../../README.md) 使用届时 main 的锁定环境。以下是实施检查命令，实际执行的命令、版本与结果见 handoff；数据库使用现有隔离测试地址，不接公司数据源。

```bash
export SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres
uv run --locked --extra dev python -m pytest tests/p1b/test_starrocks_security.py tests/p1b/test_starrocks_security_audit.py tests/sdk_core/test_security_inspection_eval.py tests/sdk_core/test_presentation_v4.py tests/sdk_core/test_database_skills.py tests/sdk_core/test_security_notification_paths.py tests/security/test_docs_command_consistency.py -q
uv run --locked --extra dev ruff check .
uv run --locked --extra dev mypy src/xiaowei
git diff --check
```

- H1 仅新增旧结构保存后读取节点；指纹与 v1 展示基线一次性对照值放交付证据，不重跑整套 Session/审批/通知恢复矩阵。
- Web JS 未改，共用 presentation 单元测试覆盖 v1/v2、无命中/不完整、MD/HTML 和纯文本。只有原浏览器断言或夹具受影响才跑对应节点；实施已确认 default_view 写死五行，brief 使用已取消操作的旧默认夹具，因此只跑这两个受影响用例，不做渠道全排列。
- 确认旧正例变反例是批准的范围变化；不能删除 OR/SLEEP 正例、数据边界或覆盖断言迁就实现。既有未变权限/SQLGuard 机制引用有效证据，不无理由重测所有业务工具。
- 本地相关测试与 lint/type 足够；获准创建 PR 后由默认 CI 跑完整回归。本轮没有 CI 链接，也不启动 CI。

### 公司与真实模型验收（尚未执行）

- 保留的 OR/SLEEP/多语句分支表达式没有改变，不为删分支重复安排 DBA 语义复核；沿用有效既有证据。尚未完成的目标版本语义验收仍是既有缺口，不能因删除分支标为通过。粗筛新范围用实际模板/参数在离线替身验证。
- 代表小时回传新候选行数/字节、全窗聚合与耗时，不沿用旧 117 候选作为新规则结果。粗筛删除分支通常会减少候选，但没有新环境测量前不承诺降幅或时延收益。
- 真实 Agent 沿 S1–S5 各一次，优先看“只有多语句不报攻击”、不把 OR/SLEEP 直接确认攻击、S5 不误报完整、无多余确认/补查。记录版本、Profile 安全引用、工具选择、调用数、耗时与用量；不稳定再定向重复，脚本模型不证明这些判断质量。
- 公司截图只取辅助多语句的实际通知、Web/卡片任选一端的 v2 brief 覆盖与四行计数；S5 沿已有覆盖验收核对，不另做全渠道截图。历史 H1 先用离线证据，不扩大组合。
- 不再为了本切片要求 DBA 实际执行 GRANT/DROP/TRUNCATE 样本。延迟函数须以目标版本实际支持为准，不支持则明确缺口，不假造执行成功；沿用公司清单已有测试环境与原文边界。

本地实现与相关离线验证已完成；真实模型、公司 StarRocks、飞书/代理实收、已有 A0/A3/H3 未完成项和 openUrl/漏查目标既有缺口不自动关闭。代码审查通过、离线通过、CI 通过、部署与用户验收分别记录。
