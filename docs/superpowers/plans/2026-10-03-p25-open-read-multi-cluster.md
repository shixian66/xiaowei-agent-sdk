# P2.5：读取放开、SQL 放宽与多集群直连实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. 按依赖串行交付；每片有独立提交与精确 SHA 审查，不凭计划批准自行合并或部署。

**Goal:** Agent 能澄清集群和业务口径，搜索自动发现的表，完成复杂只读查询与诊断、跨集群追问；所有业务查询在执行前强制评估，事实始终受当前权限和容量约束。

**Architecture:** 保留一个具备业务工具和 Session 的 Agent，由 SDK Runner 驱动；本地工具复用 GovernedTools、SQLGuard、StarRocksAdapter、EvidenceStore、PolicySession 与 ChannelService。新增工作限于多目标路由、自动 schema/权限复核、StarRocks 风险判断和一次无工具的 SDK 用途识别；不建设数据库 MCP、权限平台、CBO、调度平台或第二套 Agent Loop。

**Tech Stack:** Python 3.11.16；openai-agents 0.22.3、sqlglot 30.17.0、asyncmy 0.2.15、SQLAlchemy 2.0.52、asyncpg 0.30.0、PostgreSQL 16；沿用 uv.lock，不先加依赖。

**Spec:** [ARCHITECTURE §5 本轮用途](../../../ARCHITECTURE.md#turn-purpose)、[§6 R1–R6](../../../ARCHITECTURE.md#p25-scope)、§9 是产品/权限边界唯一来源；[DEVELOPMENT_PLAN §6](../../../DEVELOPMENT_PLAN.md) 只维护顺序和退出条件。本文维护本阶段的技术契约、依赖与验收。群聊单独见 [单群计划](2026-10-03-feishu-group.md)，不在这里复制其任务。

**Baseline / 状态：** 规划源代码为 `7a715ff61d7a97457b03bb8b596ef4238c1049f2`，与本地 `origin/main` `0f831ebe070e7b11b1597fbdf59921b19981b129` 内容相同；P2 离线完成，P1/P2 实战缺口继承到 P3。文档候选 v1（2026-10-03），本轮只编写文档、运行有限离线探针与文档检查；Task 0–8 均未实施。

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
4. 用户只要求写/解释 SQL，或其他用户/工具内容诱导执行：自然语言误判与代码权限分别验证，诊断/待澄清轮业务 SQL 为零（Task 7）。
5. 星号、UNION、窗口/CTE、审计 SQL 的依赖提取及输出容量：不漏 JOIN/WHERE/排序引用，不静默丢列，不让大 schema 决定无限投影（Task 3、6）。

## 1. 最小方案与调用链

### 1.1 复用与最少新增

| 现有模块 | 沿用的职责 | 此次变化 |
| --- | --- | --- |
| `runtime.py`、`config.py`、`app.py` | 配置装配、期限、模型绑定、每轮独立 Agent | 多目标配置/能力摘要；用途识别后形成不可变的本轮范围 |
| `models.py`、`tools.py`、`governance.py` | 可信身份、工具参数、调用前权限/预算 | 同一工具选择目标；契约按 `(tool_id, target_id)` 查找；保留单目标 MCP 兼容路径 |
| `sqlguard.py` | StarRocks AST、规范化、封存产物与输入大小检查 | 自动 schema、多库、语法扩展、星号展开及依赖提取 |
| `starrocks.py`、`starrocks_tools.py` | 连接变量设置/回读、有界读取、值转换、固定错误、工具投影 | 每目标独立 Adapter；schema/权限读取；查询内部的强制评估；多库审计 |
| `evidence.py`、`session.py`、`storage.py` | 最终验证、会话暂存/提交、失败封闭、迁移 | Evidence 依赖/当前权限复核；多目标回放；指纹升级 |
| `channel.py`、`channel_store.py`、`web.py`、`feishu.py` | 可信入站、去重、结果状态、发送/重发 | 支持自动用途和多目标范围，保留明确用途快捷入口 |

自动 schema 和计划文本解析可分别落在 `starrocks_schema.py`、`starrocks_risk.py`，前提是直接被上述调用链使用；它们是 StarRocks 业务模块，不设抽象数据库基类、驱动注册中心或未来 TiDB 文件。函数集合仍由 StarRocks 代码维护。用途识别较长时可从 app 提取一个小模块，不造路由框架。

### 1.2 真实请求链

```text
Web / 飞书单聊（群入口在后续独立计划）
  → 可信身份、请求去重、会话/全局准入、当前 AccessPolicy
  → PolicySession 校验当前输入并复核历史 Evidence
  → 无工具 SDK 结构化用途识别（同一获准 Model Profile）
  → 用途 ∩ 当前授权 ∩ 目标能力 → 本轮不可变 Context 与业务 Agent
  → SDK Runner 自行选工具、续轮、纠正可修正错误
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

2026-10-03，在本工作树 `uv sync --locked --offline --extra dev` 建立的独立 `.venv` 中完成；未访问真实模型、数据库、飞书或 MCP：

- sqlglot 30.17.0：使用 `parse_one(read='starrocks')` 与公开 `qualify(dialect='starrocks', schema=..., expand_stars=True, validate_qualify_columns=True)`，6 个合成样本（UNION、UNION ALL、窗口+聚合 OVER、两层普通 CTE、跨库 JOIN、表别名星号）均可规范化；星号确实展开为限定列。这仅证明解析器可表达，不能证明现有 SQLGuard 已放行、依赖正确或数据库语义等价。
- Agents SDK 0.22.3：真实 Runner + ScriptedModel、tracing/exporter 关闭。初始 `is_enabled=False`，阻塞式 `input_guardrail(run_in_parallel=False)` 把状态置 True，观察顺序为 `enabled(False) → guardrail → enabled(True)`，但首个模型调用的 tools 仍为空。不能把 guardrail 内改变开关当作本轮首请求工具集合已经更新。
- 因此选择**先完成一个无工具、无 Session、单模型步的结构化识别，再装配业务 Agent**。两段都用 SDK 公开 Runner/Agent/output_type，只有后者持有业务工具和 Session；无 handoff、循环式重分类、第二个业务 Agent 或自建模型请求循环。识别的时间/请求数/usage 计入同一用户轮次预算。SDK guardrail 可用于固定检查，但不是替代现有治理或授权来源。
- SDK `FunctionTool`、动态 tools、严格 output_type、`default_tool_error_function`、SQLAlchemySession 均已有直接消费者；保留它们。当前 PolicySession 的 `get_items(limit)` 拒绝截断，RunState 恢复仍未支持；此次澄清是结束本轮后等待新消息，不引入 interruptions/RunState 持久化或提前建设审批框架。
- lark-channel-sdk 1.4.0 的公开 `get_chat_members(..., force=True)` 和分页行为已读源码，群计划继续做边界探针；没有真实协议或成员校验成功证据。

SDK 背景见 [官方 guardrails 文档](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)，准确时序以锁定版探针和 Task 7 真 Runner 验收为准。开发不得照搬在线新版本示例。

## 2. 跨边界契约

### 2.1 配置、目标与工具路由

- 新配置使用 `targets`，由稳定集群 ID 映射至类型（仅 `starrocks`）、显示说明、业务口径、连接引用和限额；取消旧单目标 `starrocks`、顶层单份 business_context 及手写表列字段。旧格式明确报迁移错误，不猜默认集群、不双读格式；当前无生产部署，可以一次迁移。
- AccessDecision 从一个目标变为目标集合；所有获准用户得到当前配置目标集合。RunContext 只保存身份、目标集合、工具范围、用途结论和预算等值，无连接、目录服务或可取得它们的引用。模型只看到集群 ID、说明、业务口径与能力状态，不见 host/port/user/凭据。
- Session 的 ModelBinding/数据策略绑定继续保留；原单份 BusinessContext 改为按目标 ID 排序的口径映射，任一已装配口径或目标集合变更后要求新建会话，首版接受这个保守粒度。不为每个集群另建 SDK Session，不因支持多目标丢掉现有口径变更检查。
- 对模型保持一套 `list_tables`、`describe_table`、`describe_table_layout`、`run_readonly_query`、`explain_query`、`list_slow_queries`，每个工具 `cluster` 必填。描述/布局额外明确 database、table；查询/计划含 sql；搜表含非空 keyword、可空 database 过滤、page/page_size；审计含受限时间窗/排序与可空 database 过滤。参数无隐式默认，边界及空值由严格 schema 明确。
- `cluster` 解析后才查对应契约与 Adapter；未知值、未授权目标或能力未配置，在该调用任何 DB I/O 前拒绝，不回退到别的集群。不为每个集群复制一套模型工具名。catalog 的查找、policy fingerprint、允许工具、回放核验、重发全部改用实际目标，不能只改入口路由。
- 某目标无审计源/无新鲜 schema/风险策略不合格，只让该目标相应能力不可用；共享工具可见时，其说明列出目标能力差异，调用时仍复核。配置结构/凭据引用/重复 ID 错误拒绝整个装配。

### 2.2 自动 schema 与当前权限

**结构缓存：** 每个目标一份只读快照，含采集/到期时间、来源指纹、版本标识、限定对象名、类型、可读列、类型/可空/注释及必要权限/对象版本证据。异步批量/分页获取，单目标 single-flight 刷新，原子替换完整快照；失败、超时、超出对象/列/字节上限不得发布半份快照。缓存不持久化凭据，不进入 RunContext。

**期限方案（待审设计值）：** 默认刷新间隔 60 秒，快照最大年龄 300 秒，单次刷新期限 10 秒；生产可按 Task 0 测量在配置中明确收紧/调整，须满足刷新间隔 ≤ 最大年龄，不能设无限。首次无快照、过期、来源变化时该目标的数据工具拒绝；刷新失败可沿用尚未到期的完整快照，到期立即关闭，不因后台重试无限延长。刷新失败标明目标能力状态，不让模型猜缺失字段；其他目标继续工作。新授权对象在下一次成功刷新后可发现，不能承诺故障时仍按固定周期出现。

**权限不是缓存成员关系：** 所有新查询由数据库实际 SELECT 再把关；向模型/渠道交付元数据、计划及历史事实前，还需要当前账号对所依赖对象/列的权限证明。首选由代码生成的零行 SELECT 权限探测（安全引用标识符，不接收原始 SQL/表达式，不返回业务行），每个读取/交付边界新做验证；仅在该次有界操作内对相同依赖去重，不跨请求缓存“仍有权限”。它是内部受控模板，不是业务查询，也不能用于成本试跑；元数据与权限探测仍共享 Adapter 的会话限额、连接槽、输入/返回大小和操作期限，不走无限额的旁路连接。

**Task 0 的阻断条件：** 必须证明该探测确实检查 SELECT 权限、不会因优化消除引用而漏检；表级/库级/角色/列级授权（若 4.1.4 不支持列级授权，记录能力限制，不伪造成功）、视图与 COUNT(*) 的表依赖均覆盖。information_schema 只可见不等于可 SELECT，EXPLAIN 本身也不能充当权限证明。若探测不成立，先用受测的当前权限元数据方案修订本节；不能用陈旧缓存替代后继续实施。

**撤权生效承诺：** 数据库拒绝后新的业务读取不能成功；下一次权限复核失败起，旧事实不再回放/交付，无额外权限缓存宽限期。数据库授权检查、执行和渠道发送不是同一事务，不承诺撤权瞬间追回已发送内容；执行后及发送前复核缩短窗口。原有旧进程在途/静态配置接管风险见 §6，不能用离线用例声称消除。

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

现有 `ToolRejectedError` 只表示工具 I/O 之前拒绝，现有 `ToolExecutionError` 使整轮中止。本阶段不能把 EXPLAIN 已发出的风险拒绝伪装成“零 I/O”。

- AST/参数/目标在 I/O 前拒绝：沿用现有安全错误回传，允许预算内修正；不预留数据库执行预算，但 SDK 轮次/总期限仍限制反复尝试。
- 完整计划明确超阈值且**业务 SQL 尚未发出**：新增窄的“预执行拒绝”结果，记录已经发生的是 EXPLAIN，消耗该次工具预算；允许 Agent 按原业务口径修正 SQL 后重新走全链。只返回已获准的风险类别和收窄建议，不回显连接/原始错误。
- 缺信息/歧义：回传可操作原因，Agent 澄清；不得擅自增加过滤条件后声称仍回答了原问题。
- EXPLAIN/权限探测的协议失败、超时或不可解释计划：本轮停止或明确澄清，业务 SQL 为零；首版不自动重试。业务 SQL 已发出后的超时、取消、结果契约失败、证据/Session 保存失败：保留当前中止语义，结果可能未知，不重试、不用第二次查询补偿。
- 拒绝信息以现有受控无 Evidence 输出进入 Session；已知风险拒绝不是成功查询的 Evidence。区分 Adapter 连接/设置/权限探测/EXPLAIN/业务执行计数，测试不把“无业务执行”写成“完全无 I/O”。

### 2.6 Evidence、Session 与重发

- 数据范围摘要升为显式版本：集群 ID、数据库类型、host/port/user 等来源身份、函数集合、SQL/结果/计划/内存/时间等资源限额、风险策略版本与阈值、审计源。规范化/排序稳定，密码及其引用不入摘要；完整 schema 不入摘要。StarRocks 旧证据一次性失效；`data_scope=None` 的 MCP/其他工具指纹必须逐字保持基线。
- Evidence 新增可信的最小数据依赖（限定对象、被读取/判断的列、必要对象/视图版本与来源）；由 SQLGuard/内部模板生成，不接受模型自报。列表/描述/布局、计划与审计每条原文各有依赖，不能只覆盖 run_query。
- Record、`_readable`/当前策略匹配、`validate_answer`、PolicySession 回放/最终提交、ChannelService 首次发送/历史、runtime.resend 共用当前数据复核。允许按一次边界操作去重权限探测，不把上轮“允许”当本轮结果。
- 一个 Session 可保留 A/B 集群事实。A 权限/来源变更不能冒用 B 契约；记录级按来源复核。若历史包含任何不可读证据，沿用整段拒绝并提示新建，不删一条引用却继续发送混有旧事实的文字。无变化的成功对照必须能跨轮、重启回放且不重跑业务查询。
- 新增应用表迁移（从 v3 开始的下一版本），保存依赖和格式版本；不直接改 SDK 表。无依赖的旧 StarRocks 记录不可读，旧 Session 通过既有绑定/证据检查要求新建；不自动补查依赖来“修好”历史。
- **明确改变旧契约：** runtime.resend 以前不连接 StarRocks；自动权限模式下必须装配有界的当前数据复核，仅做权限/元数据探测，不调用模型、不执行原业务查询。DB 不可达或权限无法证明时不发旧结果。CLI、后台交付与历史 GET 不能保留“离线直接发送已保存文本”的旁路。

### 2.7 多库审计与查结构

- `list_tables` 保留工具名、改成关键词搜索；精确计数不作为前提，分页绑定同一 schema 版本，刷新后旧游标拒绝并提示重新搜索。不提供无界空关键词导出整个目录。describe_table 按列分页/容量返回，标记还有内容；SQLGuard 用完整内部快照，模型输出分页不能改变权限。
- 审计候选去掉固定 default_database 条件，仍按受限时间窗、isQuery 与可选 database 参数读取；参数绑定、候选行数/字节/原文长度、时区与截断拒绝继承 P2 §2.7。去掉库条件后不保证覆盖全部慢查询，候选耗尽/过滤后不足明确标记。
- 每条候选保留其 Db 作为未限定名的解析上下文；SQL 显式跨库引用按当前 schema 与实际权限逐条验证。不能直接以当前任意库解析，不能只因审计表可 SELECT 就放出原文中未获准对象。审计 SQL 从不执行；权限不符/原文不完整整条不列出；依赖包括审计源和获准原文的引用对象。
- 查询与诊断共用同一套规范化和依赖提取；审计验证不做业务成本准入（它在分析过去的 SQL），但仍有严格的解析 CPU/大小/期限。当前解析超时保护与 P2 的截断判定不能删除。

### 2.8 用途识别与 Agent 任务

- 第一段只收到当前可信入站消息、必要且经 PolicySession 复核的有界历史和目标业务说明；无工具、无独立历史、不外发额外数据。输出固定结构：用途（query/diagnose/explain/generate_sql/clarify）、所需集群 ID、必要信息是否完整、缺失类别及所引用的当前会话上下文。代码验证格式、目标与引用，不信任模型声称的权限或任意高置信度数值。
- `query` 且信息完整才可开启对应目标业务查询；其他用途范围只收窄。当前消息中的明确诊断/生成要求不能被历史查询许可覆盖。裸 SQL/含否定、引用或信息不足样本按需澄清。识别 API 失败/非法输出停止，不降级到 query；主 Agent 仍可能在工具调用时发现口径不明而继续澄清。
- 第二段沿用既有业务 Agent、PolicySession 和最终 Evidence 验证。当前 AgentAnswer 无 Evidence 时只接受 clarification，尚无独立的一般解释/生成 SQL 分支；Task 7 按 ARCHITECTURE §9 已允许的无取证建议做最小回答契约扩展：澄清与未执行建议明确区分，后者不得填实际查询事实、表格或已核实结论，由代码标明未实际查询。旧的有 Evidence 回答仍逐项重验，不能因允许建议而接受空引用的取证分析。用户文本中任意事实真假不可能靠 schema 全面识别，须以真实模型样例评估。待澄清轮不提供业务查询工具；纯澄清正常提交后释放会话，下一条消息重新判定。
- 所有模型请求使用当前 ModelBinding、相同 trace/data_policy 与有界预算；至少测试识别耗尽期限后业务模型/数据库均不调用，不能为识别另开无预算免费请求通道。不得把部分识别输出先存入 Session 再绕过最终校验。

## 3. 失败处理总表

| 场景 | 用户/Agent 可见行为 | 允许的 I/O / 后续 |
| --- | --- | --- |
| 未授权身份/未知目标/参数非法 | 固定拒绝，可修正参数时给原因 | 对该调用 DB 零 I/O |
| schema 首次缺失/过期/超限 | 目标暂不可用，提示稍后或换已明确目标 | 仅有界元数据刷新；无业务执行 |
| schema 刷新失败但旧快照未过期 | 仅在期限内使用完整旧结构 | 当前权限仍单独核对；不延长年龄 |
| 新授权/撤权 | 新表成功刷新后可搜索；撤权事实不可再读取 | 业务访问由 DB 拒绝；历史验证失败不发事实 |
| AST/函数/歧义/展开超限 | 安全原因与可操作提示 | 该 SQL 尚未发送；可预算内修正 |
| 预评估明确超限 | 未执行，提示收窄；不擅改口径 | EXPLAIN 已执行，业务 SQL 0；占预算 |
| 计划缺失/截断/未知/失败 | 未执行，无法可靠评估 | 不自动重试或放宽；业务 SQL 0 |
| 会话变量回读不符/授权变化 | 固定拒绝 | 不发后续业务 SQL |
| 业务 SQL 超时/取消/网络不明 | 本轮未完成，说明结果/终止状态限制 | 不重放；断开连接，DB 超时兜底 |
| 结果超限 / 必需结构放不下 | 有界截断 / 整体受控失败 | 不能省略必须的来源、列头和限制说明 |
| 保存或历史重验失败 | 不交付旧事实，必要时新建会话 | 不通过再次查询补偿；恢复沿用现有契约 |
| 模糊用途/识别失败 | 澄清 / 固定模型失败 | 查询工具隐藏且强行调用拒绝 |
| 某集群故障 | 明确该目标不可用 | 不自动改查另一集群；其他独立请求可继续 |

## 4. 按独立结果拆分的任务

每项验收先指定可观察行为，再补失败测试、运行确认红灯、最小实现、检查成功对照/反例/边界；无需为计划预写普通函数名。依赖均指上片**独立审查通过的精确 SHA**，不允许同名但行为未交付的占位接口。

### Task 0：锁定外部事实，不改产品代码

**依赖：** 本计划经审查；仅可丢弃 StarRocks 4.1.4 + AuditLoader 5.0.0、合成库账号与隔离端口。**结果：** 能据证据决定 §2.2/2.4 是否可实施，发现不成立先修订计划。

**文件：** 实验脚本暂存工作树外；结果更新本计划本任务的结论与 handoff，不新增产品功能或长期实验框架。

- [ ] 在一次性实例上测试库级/表级/角色/列级（含不支持的情况）授权、撤权、视图引用无权限底表，核对 tables/columns/tables_config/views 的可见性与真正 SELECT 的差异；对照管理员只作实验裁判，不把它用于产品读取。
- [ ] 证实零行权限探测、COUNT(*) 表依赖、视图定义/对象重建版本的行为；拒绝用 EXPLAIN 成功证明有 SELECT 权限。若无法取得可靠证据，阻断 Task 2/5 的相应路径，提出最小替代并复审。
- [ ] 合成约 500 张表、至少 10,000 列，记录表结构读取的请求数、时间 p50/p95、原始及序列化字节、权限校验开销；验证批量/分页可在有界期限完成。用测量决定是否调整 §2.2 设计值，不编造性能通过线。
- [ ] 在锁定 sqlglot 上扩大 §1.3 六类样本，加入遮蔽/同名/大小写/引用/嵌套 alias.*、未知 catalog 与重名列反例；在 StarRocks 对规范化前后结果作语义对照。
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
- [ ] 明确超限回给真 Runner 后可以修正并重新评估，消耗预算；业务执行超时/异常后模型不续轮重试，资源/连接最终释放。风险修正不能偷偷改变口径。
- [ ] SR 上验证服务端实际限额，比较带 LIMIT 的大扫描与小表全扫成功对照；禁止 EXPLAIN ANALYZE/裸 EXPLAIN。无重型生产压力测试。
- [ ] 运行 §5 C4；隔离变异去掉预评估、把 unknown 放行、复用旧评估、计划 SQL 与执行 SQL 不同、把预执行拒绝退还预算应失败。提交 `feat: enforce query risk assessment before execution`，独立审查。

### Task 5：当前权限下的 Evidence 与跨目标历史

**依赖：** Task 4；**结果：** 撤权后新查询与旧事实的再次交付都闭合，范围未变可追问/重发且不重跑业务 SQL。**文件：** models/evidence/session/storage/runtime/channel、下一应用迁移；`test_evidence`、`test_session_policy`、`test_channel_service`、`test_runtime`、`test_starrocks_tools`。

**接口：** §2.6 新依赖、指纹版本、当前数据复核；存储升级与重发新依赖在同片交付。

- [ ] PostgreSQL 先保存 A/B 目标的查询、计划、布局、表结构、审计事实；撤销依赖权限后逐个验证回放、首次发送、历史读取、延迟发送、runtime.resend 均不交付事实/混入旧事实的文字。无变化和无关目标成功对照；重发业务 SQL/模型调用均为 0。
- [ ] 变更 host/账号/风险阈值/函数/资源/审计源使对应旧记录失效；输入顺序/允许的大小写规范化与 PYTHONHASHSEED 不影响稳定摘要；schema 本身不进入摘要；非 StarRocks `data_scope=None` 指纹与基线逐字相同。
- [ ] 完整来源依赖由代码构造，篡改/遗漏、视图变化、对象重建、权限检查失败都 fail closed。验证源复核不能重新执行原 SQL。
- [ ] v3 → 新版本升级只改应用表，旧 StarRocks 证据无依赖则失效一次；重启、旧会话、部分迁移失败、版本不符、SDK 写成功而结果保存失败、恢复后的期限等路径都覆盖。
- [ ] 运行 §5 C5 + SR 权限子集；变异仅禁新查询但允许旧 Evidence、只检查第一个目标、resend 跳过数据复核、data_scope None 被改应失败。提交 `feat: revalidate evidence against current database access`，独立审查。

### Task 6：可发现的表与多库慢查询闭环

**依赖：** Task 5；**结果：** Agent 能有界找表/读字段并定位多库慢查询，原文与证据不越界。**文件：** starrocks、starrocks_tools、runtime；`test_starrocks_audit`、`test_starrocks_audit_real`、`test_starrocks_tools`、工具契约用例。

- [ ] 关键词/库过滤/分页/刷新后游标失效/注释截断/同名表成功对照；空关键词、过大页、不可读对象拒绝；分页输出受四种容量约束。
- [ ] 审计 Db 与显式引用库不同、跨库 SQL、原文未限定名、Db 为空/未知、无权限引用、可能截断原文、候选耗尽均有验收；审计表有权不代表原文可放行。
- [ ] 保留 P2 time_zone、stmt_limit、解析期限与错误整轮停止，不因移除单库过滤读取无界候选。
- [ ] 运行 §5 C6 + SR 审计；变异使用默认库/不校验原文依赖/忽略候选上限应失败。提交 `feat: search schemas and diagnose slow queries across databases`，独立审查。

### Task 7：SDK 自然语言用途与用户任务

**依赖：** Task 6；**结果：** 确定性范围限制和 Agent 用户任务链同时成立；识别语义质量仍须 P3 实测。**文件：** app/channel/runtime、必要的用途识别模块、models/evidence/session 回答契约、Web 表单与飞书指令适配；`test_app`、`test_runtime`、`test_diagnosis`、`test_gate0`，新增 `tests/p25/test_turn_purpose.py`；固定样例继续使用 `tests/sdk_core/gate0.py`，不建评测平台。

- [ ] 真 Runner + ScriptedModel 覆盖 §2.8 识别→工具集合→受治理执行→Evidence/Session，识别失败/超时/非法输出时业务模型和 SQL 为 0；用途结论不能绕过当前授权。重放历史前失效则两个模型均不见旧事实。
- [ ] 业务样例：A 集群昨日订单分渠道转化（跨库 JOIN/CTE）；同口径 UNION 分库数据；窗口排名；接着问为什么慢；再查 B 集群并比较来源；超风险后解释限制、按用户补充缩小时间范围。
- [ ] 意图反例：只写 SQL、只解释/优化、裸 SQL、不要执行、引用“帮我查”、上轮 query 本轮 diagnose、缺集群/口径/时区、提示注入工具/表注释；模糊轮强行请求查询工具也不能执行。
- [ ] 无 Evidence 的一般解释/未执行 SQL 建议与真正澄清分别渲染，断言没有伪造结构化事实；有证据的实际 SQL/表格仍来自原验证器，旧回答回放兼容或明确失效，Session/飞书/Web 上限覆盖新分支。
- [ ] Web 默认自动识别并保留显式用途选项，飞书普通文字自动识别、指令继续可用；命令与自然语言冲突只收窄。历史/用户输入标记不能从正文伪造为可信指令。API/CLI 不新增用途。
- [ ] 运行 §5 C7 与正式入口脚本模型/浏览器；验证识别+业务运行总预算、超时、usage 与最后提交，没有额外持久化会话。提交 `feat: resolve turn purpose with native SDK before tool use`，独立审查。

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

当前仅授权文档与有限离线验证，本轮不启动 SR/PG、不改产品测试。实施 Task 0 时先核对镜像、端口和容器归属，不能借用/清理既有 release 容器；缺少可丢弃环境时保留阻断，不连接用户集群替代。

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
| Q3 | 几百表/万列的 schema 和权限探测是否满足所选容量/期限 | Task 0 量测后修订 §2.2 配置设计；不以无限缓存或省权限检查解决 |
| Q4 | 生产 SQL 的真实函数/语法覆盖与归一化结果正确性 | Task 3 合成反例 + P3 经批准样本；不自动放开未知函数/节点 |
| Q5 | 自然语言识别和主 Agent 的真实误判、额外延迟/用量 | Task 7 离线只验门控；P3 用固定正反样例和具体 Model Profile 验证，保留误判残余 |
| Q6 | 目标实际 StarRocks/AuditLoader 版本、Db/时区/stmt_limit、元数据与计划差异 | 继承 P2 G-A；P3 在获准目标逐项验证，不能以 4.1.4 容器替代 |

无新的产品方向待决定；上表是有明确关闭任务的技术门槛。若事实推翻用户要求或需要放宽权限，先报告证据和最小范围调整，不在实现中隐含修改。

## 8. 自审清单与审查交付

- [x] Agent 任务、所需信息、工具组合、自我修正、澄清和用户限制说明有场景与对应任务；SDK 原生能力与本地探针限制单列。
- [x] R1–R6 分别落到 Task 1/2/3/4/5/6/7/8；群聊只有依赖链接；旧 P2 计划不重写。
- [x] 业务 SQL、EXPLAIN、权限探测/元数据的 I/O 证据分开；预评估不能通过旧工具或 sealed SQL 旁路。
- [x] 数据范围由数据库权限决定，schema 不冒充权限；所有 Evidence 读取链、重发新增 I/O、混合 Session 和一次性失效明确。
- [x] 容量上界独立于全库列数，未知/失败行为和小表/低风险成功对照齐全；没有只验证“全部拒绝”的空洞成功。
- [x] 配置/迁移/SDK 表归属、恢复与旧进程风险明确；不把离线、隔离数据库、真实模型、部署和用户验收混为一谈。
- [ ] 针对本轮最终文档提交的精确 SHA 独立审查；文档检查通过不代表此项已完成。
