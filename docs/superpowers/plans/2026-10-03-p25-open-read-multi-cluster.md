# P2.5：读取放开、SQL 放宽与多集群直连实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. 按依赖串行交付；每片有独立提交与精确 SHA 审查，不凭计划批准自行合并或部署。

**Goal:** Agent 能澄清集群和业务口径，搜索自动发现的表，完成复杂只读查询与诊断、跨集群追问；所有业务查询在执行前强制评估，事实始终受当前权限和容量约束。

**Architecture:** 保留一个具备业务工具和 Session 的 Agent，由 SDK Runner 驱动；本地工具复用 GovernedTools、SQLGuard、StarRocksAdapter、EvidenceStore、PolicySession 与 ChannelService。新增工作限于多目标路由、自动 schema/权限复核、StarRocks 风险判断和单 Agent 的自然语言工具选择；不建设数据库 MCP、权限平台、CBO、调度平台或第二套 Agent Loop。

**Tech Stack:** Python 3.11.16；openai-agents 0.22.3、sqlglot 30.17.0、asyncmy 0.2.15、SQLAlchemy 2.0.52、asyncpg 0.30.0、PostgreSQL 16；沿用 uv.lock，不先加依赖。

**Spec:** [ARCHITECTURE §5 本轮意图](../../../ARCHITECTURE.md#turn-purpose)、[§6 R1–R6](../../../ARCHITECTURE.md#p25-scope)、§9 是产品/权限边界唯一来源；[DEVELOPMENT_PLAN §6](../../../DEVELOPMENT_PLAN.md) 只维护顺序和退出条件。本文维护本阶段的技术契约、依赖与验收。群聊单独见 [单群计划](2026-10-03-feishu-group.md)，不在这里复制其任务。

**Baseline / 状态：** 规划源代码为 `7a715ff61d7a97457b03bb8b596ef4238c1049f2`，与本地 `origin/main` `0f831ebe070e7b11b1597fbdf59921b19981b129` 内容相同；P2 离线完成，P1/P2 实战缺口继承到 P3。文档候选 v2（2026-10-03，按用户对 aed344b 的六项决定修订），本轮仅改文档；用户已允许先开始 Task 0，进展与未覆盖项见 §9。Task 1–8 未实施。

## Global Constraints

- 实现范围引用 Spec 的 R1–R6；自然语言、只读、当前权限、四种投影、Evidence 与 tracing 规则不得由局部实现放宽。
- 本阶段使用当前独立工作树；不切换或改动其他任务工作区，不动 `xiaowei-release-*`。不调用真实模型、用户 StarRocks、飞书或外部 MCP；Task 0 的可丢弃实验另按 §5 隔离运行。
- 普通内部函数名与拆文件方式由开发阶段决定；下文的数据含义、公开工具参数、权限边界、失败行为和证明义务不可省略。SDK 签名以锁定环境为准，不使用私有 API。
- 所有任务先写可失败的行为用例、确认命中目标分支，再做最小实现与对应检查。关键保护做定向隔离变异；不以 `-m security` 选中 0 项、测试总数或模拟模型回答代替证明。
- 阶段候选通过前不把中间提交迁入运行环境。当前文档批准不包含产品代码、部署或合并授权。

## Review Focus

1. 有 LIMIT、WHERE 或低估计行数但真实扫描仍很大的查询：成本判断不是实际扫描保证，数据库硬限额必须兜底（Task 0、4、8）。
2. 账号撤权但旧 schema、旧证据和自由文字仍在 Session：所有再次交付路径均拒绝，无删除引用后保留事实的旁路（Task 2、5）。
3. 多目标同名库表、同一工具的目标切换：路由、指纹、历史、重发与原采集目标一致（Task 1、5）。
4. 用户只要求写/解释 SQL，或其他用户/工具内容诱导执行：自然语言误判用真实模型评估；显式诊断入口和诊断工具的零执行业务 SQL 由代码验证，两者不混为一谈（Task 7）。
5. 星号、UNION、窗口/CTE、审计 SQL 的依赖提取及输出容量：不漏 JOIN/WHERE/排序引用，不静默丢列，不让大 schema 决定无限投影（Task 3、6）。

## 1. 最小方案与调用链

### 1.1 复用与最少新增

| 现有模块 | 沿用的职责 | 此次变化 |
| --- | --- | --- |
| `runtime.py`、`config.py`、`app.py` | 配置装配、期限、模型绑定、每轮独立 Agent | 多目标配置/能力摘要；按可信授权和显式入口形成不可变范围 |
| `models.py`、`tools.py`、`governance.py` | 可信身份、工具参数、调用前权限/预算 | 同一工具选择目标；契约按 `(tool_id, target_id)` 查找；保留单目标 MCP 兼容路径 |
| `sqlguard.py` | StarRocks AST、规范化、封存产物与输入大小检查 | 自动 schema、多库、语法扩展、星号展开及依赖提取 |
| `starrocks.py`、`starrocks_tools.py` | 连接变量设置/回读、有界读取、值转换、固定错误、工具投影 | 每目标独立 Adapter；schema/权限读取；查询内部的强制评估；多库审计 |
| `evidence.py`、`session.py`、`storage.py` | 最终验证、会话暂存/提交、失败封闭、迁移 | Evidence 依赖/当前权限复核；多目标回放；指纹升级 |
| `channel.py`、`channel_store.py`、`web.py`、`feishu.py` | 可信入站、去重、结果状态、发送/重发 | 普通消息交由单 Agent 判断，支持多目标范围，保留显式诊断入口 |

自动 schema 和计划文本解析可分别落在 `starrocks_schema.py`、`starrocks_risk.py`，前提是直接被上述调用链使用；它们是 StarRocks 业务模块，不设抽象数据库基类、驱动注册中心或未来 TiDB 文件。函数集合仍由 StarRocks 代码维护。自然语言工具选择写入既有 instructions 与工具说明，不增加用途识别模块。

### 1.2 真实请求链

```text
Web / 飞书单聊（群入口在后续独立计划）
  → 可信身份、请求去重、会话/全局准入、当前 AccessPolicy
  → PolicySession 校验当前输入并复核历史 Evidence
  → 当前授权 ∩ 目标能力 ∩ 显式入口约束 → 本轮不可变 Context 与唯一业务 Agent
  → SDK Runner 按本轮自然语言自行决定查询/解释/澄清、选工具与修正错误
  → 统一 FunctionTool（cluster 必填）→ 校验目标 → GovernedTools
  → 有效 schema / 当前 SELECT 权限 → SQLGuard 封存最终 SQL 与依赖
  → 预留预算/连接槽 → Adapter 设置并回读会话限额
  → 对最终 SQL 取 EXPLAIN LOGICAL → 代码判断风险
  → 通过才执行同一 SQL 一次 → 限量读取 → 权限/版本复核
  → Evidence 四种投影 → validate_answer → Session 提交
  → ChannelService 再验证 → 首次发送 / 历史读取 / runtime.resend
```

搜表、表结构、布局、权限探测与固定审计模板走各自受控分支，不接受任意内部 SQL；它们不递归做业务查询评估。诊断 `explain_query` 只取计划，不接上“通过即执行”的分支。

### 1.3 已做的有限规划验证与 SDK 取舍

2026-10-03，锁定环境为本工作树独立 `.venv`（`uv sync --locked --offline --extra dev`）；原始离线探针没有访问真实模型、数据库、飞书或 MCP：

- sqlglot 30.17.0 的公开 parse/qualify 对 UNION、UNION ALL、窗口+聚合 OVER、两层普通 CTE、跨库 JOIN、表别名星号六类合成样本可规范化并展开星号。它不是 StarRocks 语义检查器；用户复审指出 GROUP BY 漏列、SUM(文本列) 等可能通过，Task 0 复现并对照 4.1.4 的真实 EXPLAIN 结果，不预设服务器必拒绝或必隐式转换。
- 原 SDK 0.22.3 探针观察到：阻塞式 input_guardrail 改变 is_enabled 状态，不能保证首请求工具列表同步改变。此历史事实保留，但**用户已取消前置用途识别**，不再据此设计两次 Runner 调用或分类模块。
- 保留当前一个业务 Agent、一条 SDK Runner/PolicySession 路径。原生 FunctionTool、严格 output_type、工具错误回传、Session 和 usage 直接复用；工具按可信授权开放，模型在本轮决定是否调用，不加自定义识别循环。显式诊断入口可继续在装配前收窄工具。
- 当前 PolicySession 不支持 RunState 恢复；澄清正常结束一轮等待新消息，本阶段不引入 interruptions/审批。未知或高风险查询按用户决定拒绝，P3 再根据无法评估比例决定是否需要确认。
- lark-channel-sdk 1.4.0 的公开成员/回复能力已读源码；群协议继续由 F0 验证，无真实平台成功证据。Task 0 的新增实测单列 §9。

## 2. 跨边界契约

### 2.1 配置、目标与工具路由

- 新配置使用 `targets`，由稳定集群 ID 映射至类型（仅 `starrocks`）、显示说明、业务口径、连接引用和限额；取消旧单目标 `starrocks`、顶层单份 business_context 及手写表列字段。旧格式明确报迁移错误，不猜默认集群、不双读格式；当前无生产部署，可以一次迁移。
- AccessDecision 从一个目标变为目标集合；所有获准用户得到当前配置目标集合。RunContext 只保存身份、目标集合、工具范围和预算等值，无连接、目录服务或可取得它们的引用。模型只看到集群 ID、说明、业务口径与能力状态，不见 host/port/user/凭据。
- Session 的 ModelBinding/数据策略绑定继续保留；原单份 BusinessContext 改为按目标 ID 排序的口径映射，任一已装配口径或目标集合变更后要求新建会话，首版接受这个保守粒度。不为每个集群另建 SDK Session，不因支持多目标丢掉现有口径变更检查。
- 对模型保持一套 `list_tables`、`describe_table`、`describe_table_layout`、`run_readonly_query`、`explain_query`、`list_slow_queries`，每个工具 `cluster` 必填。描述/布局额外明确 database、table；查询/计划含 sql；搜表含非空 keyword、可空 database 过滤、page/page_size；审计含受限时间窗/排序与可空 database 过滤。参数无隐式默认，边界及空值由严格 schema 明确。
- `cluster` 解析后才查对应契约与 Adapter；未知值、未授权目标或能力未配置，在该调用任何 DB I/O 前拒绝，不回退到别的集群。不为每个集群复制一套模型工具名。catalog 的查找、policy fingerprint、允许工具、回放核验、重发全部改用实际目标，不能只改入口路由。
- 某目标无审计源/无新鲜 schema/风险策略不合格，只让该目标相应能力不可用；共享工具可见时，其说明列出目标能力差异，调用时仍复核。配置结构/凭据引用/重复 ID 错误拒绝整个装配。

### 2.2 自动 schema 与当前权限

**结构缓存：** 每个目标一份只读快照，含采集/到期时间、来源指纹、版本标识、限定对象名、类型、可读列、类型/可空/注释及必要权限/对象版本证据。异步批量/分页获取，单目标 single-flight 刷新，原子替换完整快照；失败、超时、超出对象/列/字节上限不得发布半份快照。缓存不持久化凭据，不进入 RunContext。

**期限方案（待审设计值）：** 默认刷新间隔 60 秒，快照最大年龄 300 秒，单次刷新期限 10 秒；生产可按 Task 0 测量在配置中明确收紧/调整，须满足刷新间隔 ≤ 最大年龄，不能设无限。首次无快照、过期、来源变化时该目标的数据工具拒绝；刷新失败可沿用尚未到期的完整快照，到期立即关闭，不因后台重试无限延长。刷新失败标明目标能力状态，不让模型猜缺失字段；其他目标继续工作。新授权对象在下一次成功刷新后可发现，不能承诺故障时仍按固定周期出现。

**权限不是缓存成员关系：** 新查询由数据库实际 SELECT 再把关；交付元数据、计划和历史事实前仍需当前权限证明。首选代码生成零行 SELECT 探测（安全标识符、无用户表达式、不返回业务行），其有效性由 Task 0 验证。权限结果明确分为允许、确定拒绝、暂时无法验证；连接/协议失败、超时、校验预算耗尽不能伪装成确定撤权。探测共用 Adapter 会话限额、连接槽、大小和期限。每批按（集群, database.object）合并证据引用及列依赖后去重，跨新的读取/交付边界重新验证，不能把旧允许结果当新边界授权。

**权限检查次数上限（待审设计值）：** 每个用户轮次累计最多 `max_scope_checks_per_turn=128` 次对象级探测，覆盖历史回放、工具结果、最终保存和首次交付；每批同一（集群, 对象）只查一次，列依赖先取并集。单独历史读取/重发每次同样最多 128 次。计数在探测前增加，失败不退还，不自动重试；第 129 次在 I/O 前以暂不可用拒绝且保留 Session。后续批次必须重验时仍计数，不能为省次数复用旧边界许可；批内新增未覆盖列必须补证并计数。只在现有请求生命周期保留一个计数器，不放服务引用到 RunContext、不增加常驻缓存。Task 0 测量后可按有界配置调整，不能设无限。

**Task 0 的阻断条件：** 必须证明该探测确实检查 SELECT 权限、不会因优化消除引用而漏检；表级/库级/角色/列级授权（若 4.1.4 不支持列级授权，记录能力限制，不伪造成功）、视图与 COUNT(*) 的表依赖均覆盖。information_schema 只可见不等于可 SELECT，EXPLAIN 本身也不能充当权限证明。若探测不成立，先用受测的当前权限元数据方案修订本节；不能用陈旧缓存替代后继续实施。

**撤权生效承诺：** 数据库拒绝后新业务读取不能成功；下一次确认撤权起相关旧事实不可读，没有额外权限缓存宽限期。若仅集群暂不可达，当前访问同样不放行，但按 §2.6 保留原证据/Session，恢复后新请求重验。执行后/发送前复核不能形成跨系统事务，不承诺撤权瞬间追回已发送内容；旧进程接管风险继续披露。

权限依赖按数据库实际访问规则建立：若只读账号可 SELECT 视图而不可直接 SELECT 底表，视图路径不能被错误转成必须取得全部底表直查权限；模型对计划展开内容的披露授权沿用 P2，但不能据此直接查询底表。Task 0 分别验证这两个方向。

视图定义改变、同名对象重建对历史范围的影响必须在 Task 0 核对最小对象版本标识。Evidence 依赖需能识别影响读取语义的变化；每次历史读取/交付须用当前元数据复核这些标识，不能只比较旧缓存。缺少可靠身份/视图版本时不得宣称历史仍安全，相关历史重放保持关闭并先修订设计，不把完整 schema 塞回全局指纹或预建权限平台。

### 2.3 SQL、依赖与容量

- 用 sqlglot 公开 AST/作用域与 qualify 能力；结构节点仍闭集，按 R3 增补。区分物理对象、CTE、输出别名、窗口表达式及函数；依赖覆盖 SELECT、WHERE、JOIN、GROUP/HAVING、ORDER、窗口 PARTITION/ORDER、嵌套 CTE/子查询与 UNION 每个分支。
- 业务 SQL 的物理表必须规范化为 database.table；未限定且只能唯一匹配快照可补全，多个匹配则返回歧义让 Agent 澄清，不能随便选库。审计原文的未限定对象使用审计记录自身的原始 Db 解析（§2.7）。不继承连接默认库，不猜大小写或跨 catalog 语义；Task 0 固定 StarRocks 标识符规则。
- 星号必须完全展开；COUNT(*) 是聚合语义，保留但仍记录表依赖。重复输出列名要求显式别名，不悄悄改列名。输入与展开/规范化后的 SQL 都检查 UTF-8 字节上限；不截断 SQL、列集合、权限依赖或待评估的计划。
- 保留顶层有限返回及 `max_rows+1` 截断判断；UNION 的 LIMIT 是整个集合的上限，不逐分支改写语义。固定样本比较结果与排序语义，不能只断言 parse 成功。
- 增加显式 `max_result_columns`、列名总字节、schema 对象/列/字节、schema 工具每页行数/字节与注释单值上限。初始测试值刻意小；生产值在样本与容量核对后配置，不从“整个 schema 有多少列”推导无界最坏结果。
- `worst_case_observation` 改用这些独立硬上限与 JSON 最坏转义膨胀，覆盖实际 SQL、cluster、列头、行、分页/截断/说明和 Evidence 包装；启动通过真实四种投影器校验可容纳必要字段，运行时同一计数规则复核。业务结果超限有截断标志；计划/权限依赖超限为拒绝，不能拿半份输入做放行判断。

### 2.4 强制业务查询评估

- 强制入口在 Adapter 的业务 `run_query` 路径内，所有调用者只能走它；封存的 SQLGuard 产物也不能绕过成本评估。`explain_query` 保持单独的诊断路径。用户/模型都不能传 `approved`、计划文本或跳过检查开关。
- 获取同一目标的连接槽，设置并回读 query_timeout/query_mem_limit/time_zone；在同一连接和一份不可变上下文中，对**最终带限额、已展开的规范化 SQL**取 `EXPLAIN LOGICAL`，解析后通过才执行该 SQL 一次。诊断原 SQL 的计划与这里的限额后计划用途不同，不能拿旧 Evidence 的计划替代本次预评估。
- 评估输入至少绑定集群、连接身份摘要、SQL 摘要、schema/依赖版本、风险策略版本、采集时间；结论只有 allow / reject / unknown 和有限原因。只在本次连接操作中有效，不持久保存可重用的执行许可。评估到执行之间再次核对当前授权、期限和引用对象版本；发生变化则拒绝，不自动重放。
- 最小风险策略使用受测计划里的扫描/分区范围、估算规模、JOIN/排序/聚合风险信号。Task 0 固定支持的节点/字段及未知值规则，配置明确估算规模/分区等阈值，记录单位；不把 optimizer cost 当毫秒、把 Estimates.row 当真实扫描量，或因有 WHERE/LIMIT 就放行。统计缺失/不可解释、计划未知格式/算子、截断或超时一律 unknown。小表全扫可通过，大范围扫描可能拒绝，禁止按 SQL 复杂程度一刀切。
- RiskPolicy 及目标版本验收信息是必填启动配置，缺失不开放该目标业务查询；生产阈值由 Task 0 合成边界与 P3 实际集群验证确定，不为未知容量提供“安全通用默认值”。DBA 资源组/扫描行数/CPU/内存限制及账号实际命中规则在目标上验收；若只能人工证明，明确记录证据与变更后的复验要求，不能把配置声明说成运行时证明。
- 预评估也使用工具预算、连接/客户端/整轮期限；EXPLAIN 和真正执行共用整次操作的客户端截止时间，每条数据库语句另有服务端限额。没有 `EXPLAIN ANALYZE`、试跑原 SQL 或自动放宽限额的路径。

### 2.5 可修正错误与预算

现有 ToolRejectedError 表示工具 I/O 前拒绝，ToolExecutionError 中止整轮。当前 StarRocksAdapter 的 `_query_code` 只映射有限权限/超时/丢失连接错误，其余归为当前失败阶段；没有可交回模型的 EXPLAIN 语义拒绝。本阶段新增窄的“预执行拒绝”，不能把已做 EXPLAIN 写成零 I/O。

- AST/参数/目标在 I/O 前拒绝：沿用安全错误回传，允许预算内修正；SDK 轮数和整轮期限仍约束尝试。
- 计划完整且明确超阈值：业务 SQL 未发出，消耗本次工具预算，返回固定风险类别与收窄建议。没有人工确认放行或自动放宽限额；Agent 只有保持业务口径或获得用户补充后才能修改 SQL 并重新评估。
- **EXPLAIN 阶段明确的 SQL 语义错误或 SELECT 无权限错误：** 业务 SQL 尚未执行，作为可修正拒绝交回同一个 Agent，消耗预算；无权限只能换到当前获准对象或澄清，不尝试提升权限。Task 0 固定 4.1.4 的错误码及必要的受测消息模板。不能仅因是 SQL 错误码就全部恢复，也不能把同一码下未知错误误分类。
- 错误投影只有固定类别，以及能够与本轮已提交、通过标识符约束的 AST/当前获准 schema 对应的表名、列名；不复制数据库消息、字面量、SQL 片段、账号、host 或连接信息。无法可靠对应对象时仅返回类别；无法可靠判定类别则停止。列名/表名同样有数量、字节和转义上限。
- 协议错误、连接中断、超时、无法识别的错误、计划格式未知/截断仍停止本轮，业务 SQL 为零且不自动重试。原业务 SQL 发出后的任何失败仍保持结果可能未知、停止、不重放；仅因为错误码相同不能套用 EXPLAIN 的可恢复语义。
- 预执行拒绝没有成功查询 Evidence；使用现有受控无 Evidence 工具结果回传。测试分别统计连接/设置/权限探测/EXPLAIN/业务执行和预算。权限探测不能证明当前权限时按 §2.6 暂不可用，不能混入“已经确定撤权”。

### 2.6 Evidence、Session 与重发

- 数据范围摘要升为显式版本：集群 ID、数据库类型、host/port/user 等来源身份、函数集合、SQL/结果/计划/内存/时间等资源限额、风险策略版本与阈值、审计源。规范化/排序稳定，密码及其引用不入摘要；完整 schema 不入摘要。StarRocks 旧证据一次性失效；`data_scope=None` 的 MCP/其他工具指纹必须逐字保持基线。
- Evidence 新增可信的最小数据依赖（限定对象、被读取/判断的列、必要对象/视图版本与来源）；由 SQLGuard/内部模板生成，不接受模型自报。列表/描述/布局、计划与审计每条原文各有依赖，不能只覆盖 run_query。
- Record、`_readable`/当前策略匹配、`validate_answer`、PolicySession 回放/最终提交、ChannelService 首次发送/历史、runtime.resend 共用三态复核。当前 `_currently_authorized` 把异常归为 EvidenceUnavailableError，Session 又映射为 SessionUnavailableError，必须在这些边界保留“确定失效”和“暂不可验证”的区别；不能只在工具层改错误文案。
- 一个 Session 可保留 A/B 集群事实，记录按来源核验。**确定撤权/过期/来源或依赖身份变更：** 相关 Evidence 失效，混有它的历史整段拒绝并提示新建，不能删引用后保留旧事实。**仅临时无法验证：** 本轮/本次交付不返回旧事实，丢弃尚未提交的本轮暂存；不删除证据、不改变指纹、不把原 Session 置为 closed。恢复后用户的新请求重验通过可继续相同 Session，不自动重跑失败请求。
- 新增应用表迁移（从 v3 开始的下一版本），保存依赖和格式版本；不直接改 SDK 表。无依赖的旧 StarRocks 记录不可读，旧 Session 通过既有绑定/证据检查要求新建；不自动补查依赖来“修好”历史。
- **明确改变旧契约：** runtime.resend 装配有界的当前数据复核，仅探测权限/元数据，不调用模型或原业务 SQL；临时失败不发旧结果，但不作废 Session/Evidence，交付记录保留可显式重发状态。历史 GET 同样区分临时不可用。若 SDK/应用持久化已开始且结果不明，仍按现有一致性规则关闭会话；这不是“集群暂时无法验证”，不能放松。

### 2.7 多库审计与查结构

- `list_tables` 保留工具名、改成关键词搜索；精确计数不作为前提，分页绑定同一 schema 版本，刷新后旧游标拒绝并提示重新搜索。不提供无界空关键词导出整个目录。describe_table 按列分页/容量返回，标记还有内容；SQLGuard 用完整内部快照，模型输出分页不能改变权限。
- 审计候选去掉固定 default_database 条件，仍按受限时间窗、isQuery 与可选 database 参数读取；参数绑定、候选行数/字节/原文长度、时区与截断拒绝继承 P2 §2.7。去掉库条件后不保证覆盖全部慢查询，候选耗尽/过滤后不足明确标记。
- 每条候选保留其 Db 作为未限定名的解析上下文；SQL 显式跨库引用按当前 schema 与实际权限逐条验证。不能直接以当前任意库解析，不能只因审计表可 SELECT 就放出原文中未获准对象。审计 SQL 从不执行；权限不符/原文不完整整条不列出；依赖包括审计源和获准原文的引用对象。
- 查询与诊断共用同一套规范化和依赖提取；审计验证不做业务成本准入（它在分析过去的 SQL），但仍有严格的解析 CPU/大小/期限。当前解析超时保护与 P2 的截断判定不能删除。

### 2.8 单 Agent 的自然语言工具选择

- 默认入口按当前身份、目标能力和模型数据策略开放工具，包括获准的查询工具；只构造一个业务 Agent、一条 SDK Runner/PolicySession 路径。删除前置分类请求、分类输出 schema、分类用 Session 和专用用途识别模块的任务。
- instructions 与工具说明给出流程：明确当前人的要求 → 补齐集群/口径/时间 → 必要时搜表和查结构 → 明确要求查询才调用查询工具 → 按拒绝原因在预算内修正或解释限制。只解释、只写 SQL、“不要执行”、裸 SQL 或意图不清时不调用查询工具，信息不清先澄清；其他用户、历史或工具文本不授予本轮查询许可。
- **证明边界：** 普通自然语言场景中查询工具仍可见，若模型误调用且通过权限/SQLGuard/风险与资源检查，执行链可能执行该只读 SQL。离线强行调用测试应验证这些实际保护，不能再断言“模糊意图必被代码拒绝”。真实模型任务样例必须单独检查误执行；未达到要求的 Model Profile 不开放试用。
- Web 显式诊断与 `/诊断` 是可信入口约束，仍可隐藏并拒绝查询；普通消息默认用单 Agent 判断，`/查询` 不忽略正文的否定或缺失信息。API/CLI 保留当前用途。旧默认诊断切到新默认行为在本 Task 一次完成并更新说明，不静默留下双重语义。
- 现有 AgentAnswer 无 Evidence 时只接受 clarification。按 ARCHITECTURE §9 的无取证建议做最小契约扩展：澄清和未执行建议分别展示，建议不能填实际查询事实/表格，代码标明未实际查询；有 Evidence 的回答继续原验证。自由文字真假不能靠 schema 全面识别，留真实模型验收。澄清提交后释放会话，下一消息重新按当前身份运行。
- 模型与工具继续在同一轮数、次数、时间和投影限额内运行；不新增分类调用。API 失败/非法最终输出停止，不补跑模型或工具。

## 3. 失败处理总表

| 场景 | 用户/Agent 可见行为 | 允许的 I/O / 后续 |
| --- | --- | --- |
| 未授权身份/未知目标/参数非法 | 固定拒绝，可修正参数时给原因 | 对该调用 DB 零 I/O |
| schema 首次缺失/过期/超限 | 目标暂不可用，提示稍后或换已明确目标 | 仅有界元数据刷新；无业务执行 |
| schema 刷新失败但旧快照未过期 | 仅在期限内使用完整旧结构 | 当前权限仍单独核对；不延长年龄 |
| 新授权/撤权 | 新表成功刷新后可搜索；撤权事实不可再读取 | 业务访问由 DB 拒绝；历史验证失败不发事实 |
| AST/函数/歧义/展开超限 | 安全原因与可操作提示 | 该 SQL 尚未发送；可预算内修正 |
| 预评估明确超限 | 未执行，提示收窄；不擅改口径 | EXPLAIN 已执行，业务 SQL 0；占预算 |
| 计划缺失/截断/未知或协议失败 | 未执行，无法可靠评估；建议缩小范围，服务异常可稍后重提 | 停止，无确认绕过；业务 SQL 0 |
| EXPLAIN 明确语义/SELECT 权限错误 | 固定类别和获准标识符，可修正 | 消耗预算，交回模型；业务 SQL 0 |
| 会话变量回读不符/授权变化 | 固定拒绝 | 不发后续业务 SQL |
| 业务 SQL 超时/取消/网络不明 | 本轮未完成，说明结果/终止状态限制 | 不重放；断开连接，DB 超时兜底 |
| 结果超限 / 必需结构放不下 | 有界截断 / 整体受控失败 | 不能省略必须的来源、列头和限制说明 |
| 保存结果不明 / 确定历史失效 | 不交付事实，按原一致性规则关闭或要求新建 | 不通过再次查询补偿 |
| 权限暂不可验证 / 校验预算耗尽 | 本次暂不可用，恢复后可再发新请求 | 原证据与 Session 保留；不自动重跑业务 SQL |
| 普通消息意图不清 / 明确不执行 | Agent 应澄清或只解释/建议 | 查询工具仍按授权可见；语义效果由真实模型评估，硬保护照常 |
| 显式诊断入口强行查询 | 可信范围拒绝 | 业务 SQL 为 0，不依赖自然语言识别 |
| 某集群故障 | 明确该目标不可用 | 不自动改查另一集群；其他独立请求可继续 |

## 4. 按独立结果拆分的任务

每项验收先指定可观察行为，再补失败测试、运行确认红灯、最小实现、检查成功对照/反例/边界；无需为计划预写普通函数名。依赖均指上片**独立审查通过的精确 SHA**，不允许同名但行为未交付的占位接口。

### Task 0：锁定外部事实，不改产品代码

**依赖：** 用户已允许本计划修订期间先开始 Task 0；Task 1 仍须等计划和 Task 0 结论经审查。仅可丢弃 StarRocks 4.1.4 + AuditLoader 5.0.0、合成库账号与隔离端口。**结果：** 能据证据决定 §2.2/2.4 是否可实施，发现不成立先修订计划。

**文件：** 实验脚本暂存工作树外；结果更新本计划本任务的结论与 handoff，不新增产品功能或长期实验框架。

- [ ] 在一次性实例上测试库级/表级/角色/列级（含不支持的情况）授权、撤权、视图引用无权限底表，核对 tables/columns/tables_config/views 的可见性与真正 SELECT 的差异；对照管理员只作实验裁判，不把它用于产品读取。
- [ ] 证实零行权限探测、COUNT(*) 表依赖、视图定义/对象重建版本的行为；拒绝用 EXPLAIN 成功证明有 SELECT 权限。若无法取得可靠证据，阻断 Task 2/5 的相应路径，提出最小替代并复审。
- [ ] 合成至少 1,000 张表、至少 30,000 列，记录表结构读取的请求数、时间 p50/p95、原始及序列化字节、权限校验开销；验证批量/分页可在有界期限完成。用测量决定是否调整 §2.2 设计值，不编造性能通过线。
- [ ] 在锁定 sqlglot 上扩大 §1.3 六类样本，加入遮蔽/同名/大小写/引用/嵌套 alias.*、未知 catalog 与重名列反例；在 StarRocks 对规范化前后结果作语义对照。
- [ ] 对 GROUP BY 漏列、SUM(文本列)、未知列/对象及无权限对象执行固定 LOGICAL，记录 MySQL 错误码/SQLSTATE、受测类别与安全模板；成功或隐式转换也如实记录。对照协议失败/未知码与同码不同错误，确定可恢复白名单；不保存含连接或凭据的原始错误。
- [ ] 核对 4.1.4 默认的统计自动收集开关、调度间隔、触发条件、新建/导入表首次统计和计划未知统计表现；区分默认配置、观察到一次自动采集与人工 ANALYZE，不能以人工收集证明默认已生效。
- [ ] 对上述语句验证固定 LOGICAL 在 FE 默认级别设为 ANALYZE 时仍不执行原查询；复用 P2 服务端扫描/审计/执行期必失败对照，测试后恢复 FE 设置。
- [ ] 收集 LOGICAL 的小扫描、全分区、大 JOIN/排序/聚合、视图/MV、缺统计、未知节点及截断样本；确定可解析字段、估计的单位/限制，说明统计失真不能由预评估消除。不得发明 optimizer cost 到真实 CPU/内存的换算。
- [ ] 使用极低测试阈值验证 Resource Group 实际账号匹配、CPU/内存/每 BE 扫描限制、query_timeout/内存回读、并发/取消残余；只对合成负载施加限制，不造生产规模压力。确认普通只读账号能否证明资源组绑定，不能证明则将 DBA 验收列为启用前提。
- [ ] 审计记录覆盖 Db 为 A、SQL 显式引用 B、跨库 JOIN、Db 为空、原文截断与候选被过滤；确定移除单库过滤后的有界行为。
- [ ] 提交 `docs: record p25 StarRocks prerequisite evidence`；给出镜像 digest、驱动/插件版本、命令、安全输出和未通过项，独立审查；任何必须条件失败不得进入依赖片。

### Task 1：多目标配置与一套工具的准确路由

**依赖：** Task 0；**结果：** 三个目标同名库表不会串连、串策略或串 Evidence 来源。**文件：** runtime/config/models、governance/tools、starrocks_tools、app/channel；相关 `test_runtime`、`test_starrocks_tools`、`test_governance`、`test_app`。

**接口：** 消费可信 targets 配置；提供 `(tool_id, target_id)` 契约选择、目标集合与模型可见的安全能力说明（§2.1）。

- [ ] 先测三个 recording Adapter 返回不同常量；未知/缺少 cluster、用户撤权、重复 ID、未知类型、单目标无 audit、并行不同目标都命中正确分支；拒绝时所有 Adapter/connector 调用为 0。
- [ ] 一套 SDK 函数 schema 必须要求 cluster，模型参数不得覆盖 target/连接；MCP 单目标和非 StarRocks 契约成功对照保留。配置旧格式报可操作迁移错误。
- [ ] 实现最小装配/路由，检查 Evidence、Session 和所有 catalog 调用者，无只有入口支持多目标的半成品。
- [ ] 运行 §5 C1；隔离变异删除目标校验、按 tool_id 单键查找，应分别导致用例失败。提交 `feat: route governed database tools to explicit targets`，独立审查。

### Task 2：有界自动 schema 与账号权限验证

**依赖：** Task 1；**结果：** 不需手写表列，新授权可发现、撤权不能继续交付结构/数据；旧快照仅在明确期限内可用。**文件：** starrocks、starrocks_tools、runtime；必要时增加 StarRocks schema 模块；`test_starrocks_adapter`、`test_starrocks_tools`、`test_runtime`，新测试集中在 `tests/p25/test_schema_scope.py`。

**接口：** §2.2 快照/权限结果、时间/容量边界；内部模板不得接受任意 SQL。

- [ ] 时钟可控地测试首次失败、成功、刷新失败沿用、到期拒绝、并发刷新只一次、完整替换、取消/关闭、目标独立、超过对象/列/字节限额不能发布部分快照。
- [ ] 权限成功/拒绝/网络失败对照，覆盖视图、表/列/角色授权与变化；输出字段不能只按 information_schema 可见性放行。
- [ ] 接真实可丢弃 StarRocks 复核 Task 0 权限结果及新建/撤权；记录新授权可见时限和当前权限拒绝边界。
- [ ] 运行 §5 C2 与 SR；变异跳过过期/权限检查或发布部分快照应失败。提交 `feat: discover bounded schemas under current database grants`，独立审查。

### Task 3：复杂 SQL、跨库与星号展开

**依赖：** Task 2；**结果：** R3 SQL 保持语义、完整依赖且有限输出；现有已支持的 DISTINCT/CASE/CAST 等只补缺口。**文件：** sqlguard、starrocks_tools；`test_p1b_sqlguard`、`test_starrocks_tool_contracts`、`test_starrocks_tools`；新增 `tests/p25/test_sql_scope.py`。

**接口：** §2.3 封存规范化 SQL、目标/快照绑定、完整列/对象依赖与输出列预算；查询和计划复用。

- [ ] 正例覆盖 R3 全集与组合，反例覆盖写/多语句/OUTFILE/LOCK/未知函数、危险表函数/外部访问、未知或歧义对象、CTE 遮蔽、子查询相关列、UNION 分支绕过、窗口引用受限列、重复结果名。
- [ ] 检查 AST/规范化前后语义，不用字符串包含替代作用域；星号展开后超过 SQL/列头/结果列上限在业务执行前拒绝；测试边界等于/多 1、UTF-8 和 JSON 转义。
- [ ] 更新最坏投影上界，完整/不足容量均有启动和运行对照。不可把整个 schema 拼入每次模型输入。
- [ ] 运行 §5 C3 与 SR；变异忽略 UNION 第二分支、窗口依赖、展开后大小检查应失败。提交 `feat: support bounded complex readonly StarRocks SQL`，独立审查。

### Task 4：每条业务查询强制风险准入

**依赖：** Task 3 与 Task 0 计划/资源限制结论；**结果：** 简单查询也先评估，通过才实际执行一次，任何入口不可绕过。**文件：** starrocks、StarRocks 风险解析模块、governance/tools、starrocks_tools；`test_starrocks_adapter`、`test_starrocks_tools`，新增 `tests/p25/test_query_preflight.py`。

**接口：** §2.4 绑定的临时评估与 §2.5 可修正预执行拒绝；不对外暴露可伪造的“批准”参数。

- [ ] recording 连接逐条断言顺序：设置/回读 → EXPLAIN 同一最终 SQL → 评估 → 执行；简单 SELECT、原始 SQL、AI SQL、修改后重试全覆盖；直接调 Adapter 也不能绕开。
- [ ] 高估算/全分区超阈值、大 JOIN、缺统计、未知算子、计划过大/截断、EXPLAIN 失败、回读不符、评估后过期/撤权/版本变化均业务 SQL=0；不能错误宣称 DB 零 I/O。
- [ ] 明确超限、EXPLAIN 语义或权限拒绝回给真 Runner 后可修正并重新评估，消耗预算；参数含敏感字面量、服务端错误含账号/地址或未知标识符时，模型/Session/日志只见固定类别与允许的对象列名；业务执行超时/异常后模型不续轮重试，资源/连接最终释放。风险修正不能偷偷改变口径。
- [ ] SR 上验证服务端实际限额，比较带 LIMIT 的大扫描与小表全扫成功对照；禁止 EXPLAIN ANALYZE/裸 EXPLAIN。无重型生产压力测试。
- [ ] 运行 §5 C4；隔离变异去掉预评估、把 unknown 放行、复用旧评估、计划 SQL 与执行 SQL 不同、把预执行拒绝退还预算应失败。提交 `feat: enforce query risk assessment before execution`，独立审查。

### Task 5：当前权限下的 Evidence 与跨目标历史

**依赖：** Task 4；**结果：** 撤权后新查询与旧事实的再次交付都闭合，范围未变可追问/重发且不重跑业务 SQL。**文件：** models/evidence/session/storage/runtime/channel、下一应用迁移；`test_evidence`、`test_session_policy`、`test_channel_service`、`test_runtime`、`test_starrocks_tools`。

**接口：** §2.6 新依赖、指纹版本、当前数据复核；存储升级与重发新依赖在同片交付。

- [ ] PostgreSQL 先保存 A/B 目标的查询、计划、布局、表结构、审计事实；撤销依赖权限后逐个验证回放、首次发送、历史读取、延迟发送、runtime.resend 均不交付事实/混入旧事实的文字。无变化和无关目标成功对照；重发业务 SQL/模型调用均为 0。
- [ ] 变更 host/账号/风险阈值/函数/资源/审计源使对应旧记录失效；输入顺序/允许的大小写规范化与 PYTHONHASHSEED 不影响稳定摘要；schema 本身不进入摘要；非 StarRocks `data_scope=None` 指纹与基线逐字相同。
- [ ] PostgreSQL 保存有效历史后，让目标超时/断连/返回不可识别错误：本次无事实交付、回放前失败则模型与业务 SQL 为 0；Session 仍 active、原历史/证据不变，恢复后新请求可继续。确定撤权对照必须拒绝旧事实，混合目标一处临时不可用不作废整段历史；SDK 写入不明仍关闭。
- [ ] 同批多个 Evidence 引用同一（集群, 对象）只探测一次，列依赖合并；同名跨集群查两次。真实计数断言达到 128 时成功/第 129 次前拒绝、失败不退款、新边界重查也计数、恢复后下一轮重新计数；容量拒绝不关闭 Session。
- [ ] 完整来源依赖由代码构造，篡改/遗漏、视图变化、对象重建按确定失效处理；临时权限服务失败按暂不可用处理。验证源复核不能重新执行原 SQL。
- [ ] v3 → 新版本升级只改应用表，旧 StarRocks 证据无依赖则失效一次；重启、旧会话、部分迁移失败、版本不符、SDK 写成功而结果保存失败、恢复后的期限等路径都覆盖。
- [ ] 运行 §5 C5 + SR 权限子集；变异仅禁新查询但允许旧 Evidence、只检查第一个目标、resend 跳过数据复核、将超时当撤权而关闭 Session、去掉检查次数上限、data_scope None 被改应失败。提交 `feat: revalidate evidence against current database access`，独立审查。

### Task 6：可发现的表与多库慢查询闭环

**依赖：** Task 5；**结果：** Agent 能有界找表/读字段并定位多库慢查询，原文与证据不越界。**文件：** starrocks、starrocks_tools、runtime；`test_starrocks_audit`、`test_starrocks_audit_real`、`test_starrocks_tools`、工具契约用例。

- [ ] 关键词/库过滤/分页/刷新后游标失效/注释截断/同名表成功对照；空关键词、过大页、不可读对象拒绝；分页输出受四种容量约束。
- [ ] 审计 Db 与显式引用库不同、跨库 SQL、原文未限定名、Db 为空/未知、无权限引用、可能截断原文、候选耗尽均有验收；审计表有权不代表原文可放行。
- [ ] 保留 P2 time_zone、stmt_limit、解析期限与错误整轮停止，不因移除单库过滤读取无界候选。
- [ ] 运行 §5 C6 + SR 审计；变异使用默认库/不校验原文依赖/忽略候选上限应失败。提交 `feat: search schemas and diagnose slow queries across databases`，独立审查。

### Task 7：单 Agent 的自然语言工具选择与用户任务

**依赖：** Task 6；**结果：** 保持一个业务 Agent，明确请求可查询，解释/生成/否定/模糊场景按固定样例不查询；自然语言可靠性在 P3 真实模型验收。**文件：** app/channel/runtime、既有 instructions/工具说明、models/evidence/session 回答契约、Web 与飞书默认入口；`test_app`、`test_runtime`、`test_diagnosis`、`test_gate0`；场景测试 `tests/p25/test_turn_purpose.py`，复用 `tests/sdk_core/gate0.py` 样例体系。

- [ ] 真 Runner + ScriptedModel 验证只有业务 Agent 的调用链，没有额外分类请求；获准用户查询工具可见，未授权/目标错误在 I/O 前拒绝。历史暂时无法验证时模型未收到旧事实，Session 保留；确定失效按 Task 5 处理。
- [ ] 业务正例：指定集群昨日订单跨库 JOIN/CTE、UNION、窗口排名、随后解释慢原因、换另一集群比较；风险拒绝后按用户补充缩小范围；EXPLAIN 语义拒绝后修正成功。
- [ ] 固定反例：只解释、只写 SQL、不要执行、裸 SQL、引用“帮我查”、上一轮 query 本轮只诊断、缺集群/口径/时间、多人指令混淆、工具/表注释注入。脚本模型证明预期调用与结果链；P3 真实模型证明是否真的没有选择查询工具，不把预设输出当意图能力证据。
- [ ] 普通自然语言“不要执行”场景中，故意让替身误调工具：断言仍受当前权限/SQLGuard/强制评估/限额约束，并记录通过这些保护后可能执行的残余；显式诊断入口同样强行调用应在业务执行前拒绝。不得为了让自然语言反例变绿新增关键词识别器或第二个分类 Agent。
- [ ] 无 Evidence 建议、澄清、有证据回答分别渲染；旧回答回放兼容或明确失效，四种投影覆盖新分支。验证 Agent 不把可修正拒绝伪装成成功事实。
- [ ] Web/飞书普通消息默认单 Agent 判断，显式诊断快捷方式保留；SDK 轮数/预算/usage/超时没有新旁路。运行 §5 C7 和正式入口脚本模型/浏览器，提交 `feat: let the single agent choose query tools from user intent`，独立审查。

P3 在具体 Model Profile 上记录每个固定样例的调用轨迹、误执行次数、成功率、步数、延迟/用量和无法评估比例；这些结论不能由离线测试数量替代。

### Task 8：阶段离线退出与可接续交付

**依赖：** Task 7；**结果：** 一个精确候选 SHA 具备完整离线/隔离服务证据、配置迁移与恢复说明，交给群聊阶段；不关闭 P3 实战。

- [ ] 运行 §5 全阶段检查；正式 runtime + Web/飞书替身 + 测试 PostgreSQL 演示完整任务链，验证三目标中的一处故障不改查别处。
- [ ] 固定样例登记期望工具、事实/来源、限制和澄清；P3 记录真实模型成功率、误执行样本、步数、延迟/usage，不能用预设 ScriptedModel 输出声称已达标。
- [ ] 更新 examples 配置、README 当前用法、handoff 证据和本文偏差；删除被替代的单库/手写表列分支，确认没有双权威配置和无消费者包装。
- [ ] 核对升级/备份恢复/一次性失效与 §6；提交 `docs: record p25 offline exit and remaining live validation`；对候选 SHA 独立架构/安全/StarRocks 审查。下一项为群聊计划 F0。

## 5. 环境与必要检查

计划中的新测试文件在对应任务创建；命令不能在文件尚不存在时当作当前可通过证据。所有测试只用合成数据。先 `uv sync --locked --extra dev`；下表 C* 需要 §PG 的隔离 PostgreSQL。

| 编号 | 命令 / 方式 |
| --- | --- |
| PG | `docker compose -p xiaowei-sdk-test -f compose.sdk-test.yml up -d --wait`；设置 `SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres`；只清理由本轮拥有的该测试实例，结束 `docker compose -p xiaowei-sdk-test -f compose.sdk-test.yml down -v` |
| C1 | `uv run --locked --extra dev python -m pytest tests/sdk_core/test_runtime.py tests/sdk_core/test_starrocks_tools.py tests/sdk_core/test_governance.py tests/sdk_core/test_app.py -q -W error` |
| C2 | `uv run --locked --extra dev python -m pytest tests/p25/test_schema_scope.py tests/p1b/test_starrocks_adapter.py tests/sdk_core/test_starrocks_tools.py tests/sdk_core/test_runtime.py -q -W error` |
| C3 | `uv run --locked --extra dev python -m pytest tests/p1b/test_p1b_sqlguard.py tests/p1b/test_starrocks_tool_contracts.py tests/p25/test_sql_scope.py tests/sdk_core/test_starrocks_tools.py -q -W error` |
| C4 | `uv run --locked --extra dev python -m pytest tests/p25/test_query_preflight.py tests/p1b/test_starrocks_adapter.py tests/sdk_core/test_starrocks_tools.py -q -W error` |
| C5 | `uv run --locked --extra dev python -m pytest tests/sdk_core/test_evidence.py tests/sdk_core/test_session_policy.py tests/sdk_core/test_channel_service.py tests/sdk_core/test_runtime.py tests/sdk_core/test_starrocks_tools.py tests/sdk_core/test_storage_v2.py -q -W error` |
| C6 | `uv run --locked --extra dev python -m pytest tests/p1b/test_starrocks_audit.py tests/sdk_core/test_starrocks_tools.py tests/p1b/test_starrocks_tool_contracts.py -q -W error` |
| C7 | `uv run --locked --extra dev python -m pytest tests/p25/test_turn_purpose.py tests/sdk_core/test_app.py tests/sdk_core/test_runtime.py tests/sdk_core/test_diagnosis.py tests/sdk_core/test_gate0.py tests/sdk_core/test_web.py tests/sdk_core/test_feishu.py tests/sdk_core/test_evidence.py tests/sdk_core/test_session_policy.py -q -W error` |
| SR | 按 P2 Task 0 的已验证方式创建全新 StarRocks 4.1.4（固定原受测镜像 digest）与 AuditLoader 5.0.0，只绑定 loopback、随机合成库/账号；用 `SDK_TEST_STARROCKS_ADMIN_URL=mysql://root@127.0.0.1:59030 uv run --locked --extra dev python -m pytest tests/p1b/test_starrocks_real.py tests/p1b/test_starrocks_audit_real.py -m 'starrocks_real or starrocks_audit_real' -q`，新场景在对应文件补齐；用完恢复临时 FE 设置并删除本轮容器/库 |
| 阶段回归 | `uv run --locked --extra dev python -m pytest tests/sdk_core tests/p1b tests/p25 -q -W error`；以 `SDK_TEST_CHROME` 运行现有 `test_serve_browser.py` / `test_web_browser.py` 的 `-m browser` 场景，必要的新交互随 Task 7 增补 |
| 静态 | `uv run --locked --extra dev ruff check src/xiaowei tests/sdk_core tests/p1b tests/p25`；`uv run --locked --extra dev ruff format --check src/xiaowei tests/sdk_core tests/p1b tests/p25`；`uv run --locked --extra dev mypy src/xiaowei`；`uv lock --check`；`git diff --check` |
| 文档 | `uv run --locked --extra dev python -m pytest tests/contract/test_doc_fact_binding.py tests/security/test_docs_command_consistency.py tests/security/test_capabilities_doc.py -q`；相对链接、命令与源码人工核对 |

SR 命令同时选中两种实际标记，不把审计用例漏选。沿用 P2 的真实数据库命令未加 `-W error`：handoff 已记录 asyncmy 在截断/中止路径的未 await 警告；不新增过滤器或吞掉警告，逐次记录已知与新增项，新增回归阻断，不能将此运行写成无警告通过。离线命令继续 `-W error`。

Task 0 的规模实验不进入日常 CI；后续每片只跑受影响命令和必要依赖回归，阶段候选再跑全阶段一次。`-m security` 在新测试目录选择 0 项不算证据。变异只在临时副本内做，每次只移除一个目标保护，并确认失败因为目标行为而非导入/语法错误。

用户已授权 Task 0 的可丢弃 StarRocks 实验；本轮不改产品代码或测试。实施 Task 0 时先核对镜像、端口和容器归属，不能借用/清理既有 release 容器；缺少可丢弃环境时保留阻断，不连接用户集群替代。

## 6. 兼容、回退与恢复

- 配置一次迁移：旧单目标 JSON 明确拒绝，给出到 targets 的字段映射；不兼容旧手写表列格式。保存旧配置/锁文件/镜像与 PostgreSQL 备份，但不把凭据写入文档或 Git。
- 应用表版本从 v3 显式升级；升级在现有实例锁下执行，运行时不隐式迁移。旧 StarRocks Evidence 和依赖它的 Session 一次失效，提示新建，不能自动重跑或从展示文本重建证据；SDK 表不改。
- 回退必须同时恢复对应代码、配置与受限备份，或使用明确隔离的新库；旧二进制对新应用版本拒绝启动，禁止只切代码后继续读新表。备份恢复后仍按当前 DB 权限复核，不以备份时权限放行。
- 停机先停止接收、有界排空/取消、关闭刷新任务和每目标连接。query_timeout 仍是远端最终期限，断开连接不是远端已停止证明；重启标中断，不重放模型/SQL。
- **继承残余风险：** 静态配置变更前的旧进程可按旧配置完成在途轮次；没有共享动态权限源或分布式撤权屏障，不声称即时接管撤权。DB 权限探测和最终发送亦存在检查后变化窗口；下一边界重新验证，当前阶段不建设跨系统事务。
- 不因计划/元数据缺失切回旧 allowlist 或绕过风险评估；单目标失败隔离，不自动换集群。缺新鲜结构/可靠权限证据时功能受控不可用，是明确的保守降级。

## 7. 尚未解决且影响正确性的疑点

| 编号 | 必须取得的事实 / 最小处理 | 阻塞与关闭者 |
| --- | --- | --- |
| Q1 | 4.1.4 的元数据权限过滤、角色/列授权支持、零行 SELECT 权限检查、视图/对象版本可见性 | Task 0 执行者提供正反证据、独立审查；未闭合不启用 Task 2/5 相应范围 |
| Q2 | LOGICAL 计划字段/未知算子/统计缺失可识别性、风险单位与有效阈值；普通账号的资源组绑定核验能力 | Task 0 固定版本契约；P3 DBA/使用者确认目标阈值和资源限制；无法可靠评估则拒绝，不能先放行 |
| Q3 | 至少千表/三万列的 schema 和权限探测是否满足所选容量/期限 | Task 0 量测后修订 §2.2 配置设计；不以无限缓存或省权限检查解决 |
| Q4 | 生产 SQL 的真实函数/语法覆盖与归一化结果正确性 | Task 3 合成反例 + P3 经批准样本；不自动放开未知函数/节点 |
| Q5 | 单 Agent 的自然语言工具选择误判、修正能力、延迟/用量 | Task 7 离线只验调用链/可信授权与显式模式门控；P3 用固定正反样例和具体 Model Profile 验证，保留误判残余 |
| Q6 | 目标实际 StarRocks/AuditLoader 版本、Db/时区/stmt_limit、元数据与计划差异 | 继承 P2 G-A；P3 在获准目标逐项验证，不能以 4.1.4 容器替代 |

无新的产品方向待决定；上表是有明确关闭任务的技术门槛。若事实推翻用户要求或需要放宽权限，先报告证据和最小范围调整，不在实现中隐含修改。

## 8. 自审清单与审查交付

- [x] Agent 任务、所需信息、工具组合、自我修正、澄清和用户限制说明有场景与对应任务；SDK 原生能力与本地探针限制单列。
- [x] R1–R6 分别落到 Task 1/2/3/4/5/6/7/8；群聊只有依赖链接；旧 P2 计划不重写。
- [x] 业务 SQL、EXPLAIN、权限探测/元数据的 I/O 证据分开；预评估不能通过旧工具或 sealed SQL 旁路。
- [x] 数据范围由数据库权限决定，schema 不冒充权限；确定失效与暂不可验证分开，按对象去重及整轮检查上限明确；所有 Evidence 读取链、重发新增 I/O、混合 Session 和一次性失效明确。
- [x] 容量上界独立于全库列数，未知/失败行为和小表/低风险成功对照齐全；没有只验证“全部拒绝”的空洞成功。
- [x] 配置/迁移/SDK 表归属、恢复与旧进程风险明确；不把离线、隔离数据库、真实模型、部署和用户验收混为一谈。
- [ ] 针对本轮最终文档提交的精确 SHA 独立审查；文档检查通过不代表此项已完成。

## 9. Task 0 当前证据（尚未完成）

用户已允许先开始。2026-10-03：锁定 sqlglot 30.17.0 和现有 SQLGuard 均接受 `SELECT id, SUM(value) FROM probe.t` 与 `SELECT SUM(text_col) FROM probe.t`（合成结构 id INT、text_col VARCHAR、value INT），证明不能把 AST 放行当作服务器语义成功；这两例的 4.1.4 EXPLAIN 行为仍需实测。

当前仅完成上述离线反例及本机可丢弃镜像/容器隔离检查；Task 0 的完整权限、千表/三万列、统计自动采集、风险格式/限额与审计验收未关闭，Task 1 不启动。新的实测证据在本节补齐，不另建任务说明。
