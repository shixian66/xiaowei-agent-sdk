# P2.5：读取放开、SQL 放宽与多集群直连实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. 按依赖串行交付；每片有独立提交与精确 SHA 审查，不凭计划批准自行合并或部署。

**Goal:** Agent 能澄清集群和业务口径，搜索自动发现的表，完成复杂只读查询与诊断、跨集群追问；查询与事实始终受当前权限、资源和容量约束。

**Architecture:** 保留一个具备业务工具和 Session 的 Agent，由 SDK Runner 驱动；本地工具复用 GovernedTools、SQLGuard、StarRocksAdapter、EvidenceStore、PolicySession 与 ChannelService。新增工作限于多目标路由、自动 schema/权限复核和单 Agent 的自然语言工具选择；不建设数据库 MCP、权限平台、CBO、调度平台或第二套 Agent Loop。

**Tech Stack:** Python 3.11.16；openai-agents 0.22.3、sqlglot 30.17.0、asyncmy 0.2.15、SQLAlchemy 2.0.52、asyncpg 0.30.0、PostgreSQL 16；沿用 uv.lock，不先加依赖。

**Spec:** [ARCHITECTURE §5 本轮意图](../../../ARCHITECTURE.md#turn-purpose)、[§6 R1–R6](../../../ARCHITECTURE.md#p25-scope)、§9 是产品/权限边界唯一来源；[DEVELOPMENT_PLAN §6](../../../DEVELOPMENT_PLAN.md) 只维护顺序和退出条件。本文维护本阶段的技术契约、依赖与验收。群聊单独见 [单群计划](2026-10-03-feishu-group.md)，不在这里复制其任务。

**Baseline / 状态：** 规划源代码为 `7a715ff61d7a97457b03bb8b596ef4238c1049f2`，与本地 `origin/main` `0f831ebe070e7b11b1597fbdf59921b19981b129` 内容相同；P2 离线完成，P1/P2 实战缺口继承到 P3。文档候选 v2（2026-10-03，按用户对 aed344b 的六项决定修订）已在 `719c1c7` 通过复审；Task 0 实测证据、对计划的影响与当前范围见 §9，经复审随 PR #32 合入（`29678006f380621dda5ddb772b7affa072a59776`）。Task 1 多目标路由经独立审查随 PR #33 合入（`6ccc7a4e6e5c8ec4bac5093d4300bda63ae88605`）；Task 2 自动结构快照及审查修订（前移 Evidence 当前权限闭环、摘要 v2、列表探测硬上限）经复审随 PR #34 合入（`d497a34e468eb0b9987a5e0eac1dea7a9fbf8a08`，实施说明见 Task 2 节末）；§9.8 的 D1/D2 经独立审查随 PR #35 合入（`7523c74fcebc91718f0a7de4d10782c2a7214260`）。Task 3 复杂 SQL 与跨库经四轮独立审查随 PR #36 合入（`9ef4f3eaa9cb29d218313a3d010d84d1e937e4ca`，实施说明见 Task 3 节末）。原 Task 4 已按用户决定取消，决定见 ARCHITECTURE §6 R4–R5。Task 5 证据复核次数上限与跨目标历史验收经独立审查通过（PR #38，实施说明见 Task 5 节末）。Task 6 搜表分页与多库慢查询已实现，第一轮独立审查（head `78aa8c0`）的分页与截断意见已修订、待复审（实施说明见 Task 6 节末）；Task 7–8 未实施。

## Global Constraints

- 实现范围引用 Spec 的 R1–R6；自然语言、只读、当前权限、四种投影、Evidence 与 tracing 规则不得由局部实现放宽。
- 本阶段使用当前独立工作树；不切换或改动其他任务工作区，不动 `xiaowei-release-*`。不调用真实模型、用户 StarRocks、飞书或外部 MCP；Task 0 的可丢弃实验另按 §5 隔离运行。
- 普通内部函数名与拆文件方式由开发阶段决定；下文的数据含义、公开工具参数、权限边界、失败行为和证明义务不可省略。SDK 签名以锁定环境为准，不使用私有 API。
- 所有任务先写可失败的行为用例、确认命中目标分支，再做最小实现与对应检查。关键保护做定向隔离变异；不以 `-m security` 选中 0 项、测试总数或模拟模型回答代替证明。
- 阶段候选通过前不把中间提交迁入运行环境。当前文档批准不包含产品代码、部署或合并授权。

## Review Focus

1. 有 LIMIT、WHERE 或低估计行数但真实扫描仍很大的查询：LIMIT 和计划估算不保证实际扫描量受控，沿用执行限额，目标资源组与大查询保护在 P3 验收（Task 0 历史证据、Task 8 回归）。
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
| `starrocks.py`、`starrocks_tools.py` | 连接变量设置/回读、有界读取、值转换、固定错误、工具投影 | 每目标独立 Adapter；schema/权限读取；沿用查询运行限额；多库审计 |
| `evidence.py`、`session.py`、`storage.py` | 最终验证、会话暂存/提交、失败封闭、迁移 | Evidence 依赖/当前权限复核；多目标回放；指纹升级 |
| `channel.py`、`channel_store.py`、`web.py`、`feishu.py` | 可信入站、去重、结果状态、发送/重发 | 普通消息交由单 Agent 判断，支持多目标范围，保留显式诊断入口 |

自动 schema 使用 `starrocks_schema.py`，数据库规则集中在被调用链实际使用的 StarRocks 业务模块，不设抽象数据库基类、驱动注册中心或未来 TiDB 文件。函数集合仍由 StarRocks 代码维护。自然语言工具选择写入既有 instructions 与工具说明，不增加用途识别模块。

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
  → 执行 SQLGuard 封存的最终 SQL 一次 → 限量读取 → 权限/版本复核
  → Evidence 四种投影 → validate_answer → Session 提交
  → ChannelService 再验证 → 首次发送 / 历史读取 / runtime.resend
```

搜表、表结构、布局、权限探测与固定审计模板走各自受控分支，不接受任意内部 SQL；诊断 `explain_query` 按需只取明确的 EXPLAIN LOGICAL 计划，不接上“通过即执行”的分支。

### 1.3 已做的有限规划验证与 SDK 取舍

2026-10-03，锁定环境为本工作树独立 `.venv`（`uv sync --locked --offline --extra dev`）；原始离线探针没有访问真实模型、数据库、飞书或 MCP：

- sqlglot 30.17.0 的公开 parse/qualify 对 UNION、UNION ALL、窗口+聚合 OVER、两层普通 CTE、跨库 JOIN、表别名星号六类合成样本可规范化并展开星号。它不是 StarRocks 语义检查器；用户复审指出 GROUP BY 漏列、SUM(文本列) 等可能通过，Task 0 复现并对照 4.1.4 的真实 EXPLAIN 结果，不预设服务器必拒绝或必隐式转换。
- 原 SDK 0.22.3 探针观察到：阻塞式 input_guardrail 改变 is_enabled 状态，不能保证首请求工具列表同步改变。此历史事实保留，但**用户已取消前置用途识别**，不再据此设计两次 Runner 调用或分类模块。
- 保留当前一个业务 Agent、一条 SDK Runner/PolicySession 路径。原生 FunctionTool、严格 output_type、工具错误回传、Session 和 usage 直接复用；工具按可信授权开放，模型在本轮决定是否调用，不加自定义识别循环。显式诊断入口可继续在装配前收窄工具。
- 当前 PolicySession 不支持 RunState 恢复；澄清正常结束一轮等待新消息，本阶段不引入 interruptions/审批。查询执行与按需诊断按 ARCHITECTURE §6 R4–R5，不引入执行前人工确认。
- lark-channel-sdk 1.4.0 的公开成员/回复能力已读源码；群协议继续由 F0 验证，无真实平台成功证据。Task 0 的新增实测单列 §9。

## 2. 跨边界契约

### 2.1 配置、目标与工具路由

- 新配置使用 `targets`，由稳定集群 ID 映射至类型（仅 `starrocks`）、显示说明、业务口径、连接引用和限额；取消旧单目标 `starrocks`、顶层单份 business_context 及手写表列字段。旧格式明确报迁移错误，不猜默认集群、不双读格式；当前无生产部署，可以一次迁移。
- AccessDecision 从一个目标变为目标集合；所有获准用户得到当前配置目标集合。RunContext 只保存身份、目标集合、工具范围和预算等值，无连接、目录服务或可取得它们的引用。模型只看到集群 ID、说明、业务口径与能力状态，不见 host/port/user/凭据。
- Session 的 ModelBinding/数据策略绑定继续保留；原单份 BusinessContext 改为按目标 ID 排序的口径映射，任一已装配口径或目标集合变更后要求新建会话，首版接受这个保守粒度。不为每个集群另建 SDK Session，不因支持多目标丢掉现有口径变更检查。
- 对模型保持一套 `list_tables`、`describe_table`、`describe_table_layout`、`run_readonly_query`、`explain_query`、`list_slow_queries`，每个工具 `cluster` 必填。描述/布局额外明确 database、table；查询/计划含 sql；搜表含非空 keyword、可空 database 过滤、page/page_size；审计含受限时间窗/排序与可空 database 过滤。参数无隐式默认，边界及空值由严格 schema 明确。
- `cluster` 解析后才查对应契约与 Adapter；未知值、未授权目标或能力未配置，在该调用任何 DB I/O 前拒绝，不回退到别的集群。不为每个集群复制一套模型工具名。catalog 的查找、policy fingerprint、允许工具、回放核验、重发全部改用实际目标，不能只改入口路由。
- 某目标无审计源/无新鲜 schema，只让该目标相应能力不可用；共享工具可见时，其说明列出目标能力差异，调用时仍复核。配置结构/凭据引用/重复 ID 错误拒绝整个装配。

### 2.2 自动 schema 与当前权限

**结构缓存：** 每个目标一份只读快照，含采集/到期时间、来源指纹、版本标识、限定对象名、类型、可读列、类型/可空/注释及必要权限/对象版本证据。异步批量/分页获取，单目标 single-flight 刷新，原子替换完整快照；失败、超时、超出对象/列/字节上限不得发布半份快照。缓存不持久化凭据，不进入 RunContext。

**期限方案（待审设计值）：** 默认刷新间隔 60 秒，快照最大年龄 300 秒，单次刷新期限 10 秒；生产可按 Task 0 测量在配置中明确收紧/调整，须满足刷新间隔 ≤ 最大年龄，不能设无限。首次无快照、过期、来源变化时该目标的数据工具拒绝；刷新失败可沿用尚未到期的完整快照，到期立即关闭，不因后台重试无限延长。刷新失败标明目标能力状态，不让模型猜缺失字段；其他目标继续工作。新授权对象在下一次成功刷新后可发现，不能承诺故障时仍按固定周期出现。

**权限不是缓存成员关系：** 新查询由数据库实际 SELECT 再把关；交付元数据、计划和历史事实前仍需当前权限证明。首选代码生成零行 SELECT 探测（安全标识符、无用户表达式、不返回业务行），其有效性由 Task 0 验证。权限结果明确分为允许、确定拒绝、暂时无法验证；连接/协议失败、超时、校验预算耗尽不能伪装成确定撤权。探测共用 Adapter 会话限额、连接槽、大小和期限。每批按（集群, database.object）合并证据引用及列依赖后去重，跨新的读取/交付边界重新验证，不能把旧允许结果当新边界授权。

**权限检查次数上限（用户 2026-10-04 决定：可配置，按次计数）：** 每个用户轮次累计最多 `budget.max_scope_checks` 次对象级探测（必填，1–100000，示例 1024），覆盖历史回放、工具结果、写入 Session、最终校验与保存；每批同一（集群, 对象）只查一次，列依赖先取并集。轮次结束后的首次发送/读取、单独历史读取与重发每次单独计数，上限相同。Task 5 实测一轮新会话中每个新对象被复核 5 次（工具记录、写入 Session、最终校验、提交时的校验与提交前的整段回放），原设计值 128 只够约 25 个对象，示例中一页 200 行的列表必然失败；因此启动时要求上限不小于各目标 `policy.max_rows` 的 5 倍。搜表对本页对象的探测（每页至多 `max_rows`，Task 6 起；此前列表按 `2 × max_rows` 封顶）不计入。计数在探测前增加，失败不退还，不自动重试；超过配置上限时在 I/O 前以暂不可用拒绝且保留 Session。后续批次必须重验时仍计数，不能为省次数复用旧边界许可；批内新增未覆盖列必须补证并计数。只在现有请求生命周期保留一个计数器，不放服务引用到 RunContext、不增加常驻缓存。Task 0 测量后可按有界配置调整，不能设无限。

**Task 0 的阻断条件：** 必须证明该探测确实检查 SELECT 权限、不会因优化消除引用而漏检；表级/库级/角色/列级授权（若 4.1.4 不支持列级授权，记录能力限制，不伪造成功）、视图与 COUNT(*) 的表依赖均覆盖。information_schema 只可见不等于可 SELECT，EXPLAIN 本身也不能充当权限证明。若探测不成立，先用受测的当前权限元数据方案修订本节；不能用陈旧缓存替代后继续实施。

**撤权生效承诺：** 数据库拒绝后新业务读取不能成功；下一次确认撤权起相关旧事实不可读，没有额外权限缓存宽限期。若仅集群暂不可达，当前访问同样不放行，但按 §2.6 保留原证据/Session，恢复后新请求重验。执行后/发送前复核不能形成跨系统事务，不承诺撤权瞬间追回已发送内容；旧进程接管风险继续披露。

权限依赖按数据库实际访问规则建立：若只读账号可 SELECT 视图而不可直接 SELECT 底表，视图路径不能被错误转成必须取得全部底表直查权限；模型对计划展开内容的披露授权沿用 P2，但不能据此直接查询底表。Task 0 分别验证这两个方向。

视图定义改变、同名对象重建对历史范围的影响必须在 Task 0 核对最小对象版本标识。Evidence 依赖需能识别影响读取语义的变化；每次历史读取/交付须用当前元数据复核这些标识，不能只比较旧缓存。缺少可靠身份/视图版本时不得宣称历史仍安全，相关历史重放保持关闭并先修订设计，不把完整 schema 塞回全局指纹或预建权限平台。

### 2.3 SQL、依赖与容量

- 用 sqlglot 公开 AST/作用域与 qualify 能力；结构节点仍闭集，按 R3 增补。区分物理对象、CTE、输出别名、窗口表达式及函数；依赖覆盖 SELECT、WHERE、JOIN、GROUP/HAVING、ORDER、窗口 PARTITION/ORDER、嵌套 CTE/子查询与 UNION 每个分支。
- 业务 SQL 的物理表必须规范化为 database.table；未限定且只能唯一匹配快照可补全，多个匹配则返回歧义让 Agent 澄清，不能随便选库。审计原文的未限定对象使用审计记录自身的原始 Db 解析（§2.7）。不继承连接默认库，不猜大小写或跨 catalog 语义；Task 0 固定 StarRocks 标识符规则。
- 星号必须完全展开；COUNT(*) 是聚合语义，保留但仍记录表依赖。重复输出列名要求显式别名，不悄悄改列名。输入与展开/规范化后的 SQL 都检查 UTF-8 字节上限；不截断 SQL、列集合或权限依赖。诊断计划继续按 P2 的有界读取与截断标记交付，不作为执行许可。
- 保留顶层有限返回及 `max_rows+1` 截断判断；UNION 的 LIMIT 是整个集合的上限，不逐分支改写语义。固定样本比较结果与排序语义，不能只断言 parse 成功。
- 增加显式 `max_result_columns`、列名总字节、schema 对象/列/字节、schema 工具每页行数/字节与注释单值上限。初始测试值刻意小；生产值在样本与容量核对后配置，不从“整个 schema 有多少列”推导无界最坏结果。
- `worst_case_observation` 改用这些独立硬上限与 JSON 最坏转义膨胀，覆盖实际 SQL、cluster、列头、行、分页/截断/说明和 Evidence 包装；启动通过真实四种投影器校验可容纳必要字段，运行时同一计数规则复核。业务结果超限有截断标志；权限依赖超限为拒绝，不能拿半份依赖做权限判断；诊断计划的截断须保留标志及未执行说明。

### 2.4 业务查询执行与资源限额

产品决定以 [ARCHITECTURE §6 R4–R5](../../../ARCHITECTURE.md#p25-scope) 为准，原 Task 4 取消后复用现有执行路径：

- GovernedTools 复核目标、权限与预算，SQLGuard 生成封存产物；Adapter 核对产物/目标、取得连接槽，设置并回读 query_timeout、query_mem_limit、time_zone 与固定 sql_mode，然后执行最终 SQL 一次并有界读取。不增加风险配置、计划解析器或额外 EXPLAIN 往返。
- `explain_query` 保持独立的按需诊断路径，只取 EXPLAIN LOGICAL，不执行被诊断 SQL，也不产生可复用的执行许可。计划估算不可用不关闭正常查询能力；schema 与当前权限的失败行为仍按 §2.2/§2.6。
- 沿用连接/客户端/整轮期限、资源限制、截断标记与安全错误；不能为执行成功自动提高限额或改变业务口径。LIMIT 不代表实际扫描量上限。
- Task 8 复核已有运行限额的回归证据；实际目标上的资源组绑定、限制及代表性正常/超限 SQL 对照纳入既有 P3 验收。Task 0 的极低合成阈值不是生产值，一次查询命中某资源组也不证明所有后续查询绑定；DBA 验收记录适用规则和变更后的复核要求。

### 2.5 可修正错误与预算

复用现有 ToolRejectedError 与 ToolExecutionError，不新增原 Task 4 的“EXPLAIN 预执行拒绝”类型或错误恢复白名单：

- AST/参数/目标在工具 I/O 前拒绝：沿用安全原因和错误回传，允许预算内修正；SDK 轮数、工具次数与整轮期限继续约束尝试。
- 查询、诊断及其他数据库工具的执行异常沿用整轮停止规则；只给固定安全类别，不回显数据库原始消息、SQL 字面量、账号或连接信息。结果不明不自动重放，诊断失败不能转成执行原 SQL。
- 拒绝或失败不记录成功查询 Evidence。权限探测仍按 §2.6 区分确定失效与暂不可验证；不能把连接失败当撤权。§9.5 保留当时的实测错误事实，白名单方案随 Task 4 取消。

### 2.6 Evidence、Session 与重发

- 数据范围摘要升为显式版本：集群 ID、数据库类型、host/port/user 等来源身份、函数集合、SQL/结果/计划/内存/时间等资源限额、审计源。规范化/排序稳定，密码及其引用不入摘要；完整 schema 不入摘要。StarRocks 旧证据一次性失效；`data_scope=None` 的 MCP/其他工具指纹必须逐字保持基线。
- Evidence 新增可信的最小数据依赖（限定对象、被读取/判断的列、必要对象/视图版本与来源）；由 SQLGuard/内部模板生成，不接受模型自报。列表/描述/布局、计划与审计每条原文各有依赖，不能只覆盖 run_query。
- Record、`_readable`/当前策略匹配、`validate_answer`、PolicySession 回放/最终提交、ChannelService 首次发送/历史、runtime.resend 共用三态复核。当前 `_currently_authorized` 把异常归为 EvidenceUnavailableError，Session 又映射为 SessionUnavailableError，必须在这些边界保留“确定失效”和“暂不可验证”的区别；不能只在工具层改错误文案。
- 一个 Session 可保留 A/B 集群事实，记录按来源核验。**确定撤权/过期/来源或依赖身份变更：** 相关 Evidence 失效，混有它的历史整段拒绝并提示新建，不能删引用后保留旧事实。**仅临时无法验证：** 本轮/本次交付不返回旧事实，丢弃尚未提交的本轮暂存；不删除证据、不改变指纹、不把原 Session 置为 closed。恢复后用户的新请求重验通过可继续相同 Session，不自动重跑失败请求。
- 新增应用表迁移（从 v3 开始的下一版本），保存依赖和格式版本；不直接改 SDK 表。无依赖的旧 StarRocks 记录不可读，旧 Session 通过既有绑定/证据检查要求新建；不自动补查依赖来“修好”历史。
- **明确改变旧契约：** runtime.resend 装配有界的当前数据复核，仅探测权限/元数据，不调用模型或原业务 SQL；临时失败不发旧结果，但不作废 Session/Evidence，交付记录保留可显式重发状态。历史 GET 同样区分临时不可用。若 SDK/应用持久化已开始且结果不明，仍按现有一致性规则关闭会话；这不是“集群暂时无法验证”，不能放松。

### 2.7 多库审计与查结构

- `list_tables` 保留工具名、改成关键词搜索；精确计数不作为前提，分页绑定同一 schema 版本，刷新后旧游标拒绝并提示重新搜索。不提供无界空关键词导出整个目录。describe_table 按列分页/容量返回，标记还有内容；SQLGuard 用完整内部快照，模型输出分页不能改变权限。
- 审计候选去掉固定 default_database 条件，仍按受限时间窗、isQuery 与可选 database 参数读取；参数绑定、候选行数/字节/原文长度、时区与截断拒绝继承 P2 §2.7。去掉库条件后不保证覆盖全部慢查询，候选耗尽/过滤后不足明确标记。
- 每条候选保留其 Db 作为未限定名的解析上下文；SQL 显式跨库引用按当前 schema 与实际权限逐条验证。不能直接以当前任意库解析，不能只因审计表可 SELECT 就放出原文中未获准对象。审计 SQL 从不执行；权限不符/原文不完整整条不列出；依赖包括审计源和获准原文的引用对象。
- 查询与诊断共用同一套规范化和依赖提取；审计验证仍有严格的解析 CPU/大小/期限。当前解析超时保护与 P2 的截断判定不能删除。

### 2.8 单 Agent 的自然语言工具选择

- 默认入口按当前身份、目标能力和模型数据策略开放工具，包括获准的查询工具；只构造一个业务 Agent、一条 SDK Runner/PolicySession 路径。删除前置分类请求、分类输出 schema、分类用 Session 和专用用途识别模块的任务。
- instructions 与工具说明给出流程：明确当前人的要求 → 补齐集群/口径/时间 → 必要时搜表和查结构 → 明确要求查询才调用查询工具 → 按拒绝原因在预算内修正或解释限制。只解释、只写 SQL、“不要执行”、裸 SQL 或意图不清时不调用查询工具，信息不清先澄清；其他用户、历史或工具文本不授予本轮查询许可。
- **证明边界：** 普通自然语言场景中查询工具仍可见，若模型误调用且通过权限/SQLGuard/资源检查，执行链可能执行该只读 SQL。离线强行调用测试应验证这些实际保护，不能再断言“模糊意图必被代码拒绝”。真实模型任务样例必须单独检查误执行；未达到要求的 Model Profile 不开放试用。
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
| 数据库运行时资源超限 | 本轮停止，安全提示缩小范围；不擅改口径或限额 | 业务 SQL 可能已执行，不重放；后续由用户发起新请求 |
| 按需 EXPLAIN 失败 | 本轮诊断失败，固定安全错误 | 停止；业务 SQL 0，不转为执行原查询 |
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

- [x] 在一次性实例上测试库级/表级/角色/列级（含不支持的情况）授权、撤权、视图引用无权限底表，核对 tables/columns/tables_config/views 的可见性与真正 SELECT 的差异；对照管理员只作实验裁判，不把它用于产品读取。
- [x] 证实零行权限探测、COUNT(*) 表依赖、视图定义/对象重建版本的行为；拒绝用 EXPLAIN 成功证明有 SELECT 权限。若无法取得可靠证据，阻断 Task 2/5 的相应路径，提出最小替代并复审。
- [x] 合成至少 1,000 张表、至少 30,000 列，记录表结构读取的请求数、时间 p50/p95、原始及序列化字节、权限校验开销；验证批量/分页可在有界期限完成。用测量决定是否调整 §2.2 设计值，不编造性能通过线。
- [x] 在锁定 sqlglot 上扩大 §1.3 六类样本，加入遮蔽/同名/大小写/引用/嵌套 alias.*、未知 catalog 与重名列反例；在 StarRocks 对规范化前后结果作语义对照。
- [x] 对 GROUP BY 漏列、SUM(文本列)、未知列/对象及无权限对象执行固定 LOGICAL，记录 MySQL 错误码/SQLSTATE、受测类别与安全模板；成功或隐式转换也如实记录。对照协议失败/未知码与同码不同错误，确定可恢复白名单；不保存含连接或凭据的原始错误。
- [x] 核对 4.1.4 默认的统计自动收集开关、调度间隔、触发条件、新建/导入表首次统计和计划未知统计表现；区分默认配置、观察到一次自动采集与人工 ANALYZE，不能以人工收集证明默认已生效。
- [x] 对上述语句验证固定 LOGICAL 在 FE 默认级别设为 ANALYZE 时仍不执行原查询；复用 P2 服务端扫描/审计/执行期必失败对照，测试后恢复 FE 设置。
- [x] 收集 LOGICAL 的小扫描、全分区、大 JOIN/排序/聚合、视图/MV、缺统计、未知节点及截断样本；确定可解析字段、估计的单位/限制，说明统计失真不能由预评估消除。不得发明 optimizer cost 到真实 CPU/内存的换算。
- [x] 使用极低测试阈值验证 Resource Group 实际账号匹配、CPU/内存/每 BE 扫描限制、query_timeout/内存回读、并发/取消残余；只对合成负载施加限制，不造生产规模压力。确认普通只读账号能否证明资源组绑定，不能证明则将 DBA 验收列为启用前提。
- [x] 审计记录覆盖 Db 为 A、SQL 显式引用 B、跨库 JOIN、Db 为空、原文截断与候选被过滤；确定移除单库过滤后的有界行为。
- [x] 提交 `docs: record p25 StarRocks prerequisite evidence`；给出镜像 digest、驱动/插件版本、命令、安全输出和未通过项，独立审查；任何必须条件失败不得进入依赖片。

### Task 1：多目标配置与一套工具的准确路由

**依赖：** Task 0；**结果：** 三个目标同名库表不会串连、串策略或串 Evidence 来源。**文件：** runtime/config/models、governance/tools、starrocks_tools、app/channel；相关 `test_runtime`、`test_starrocks_tools`、`test_governance`、`test_app`。

**接口：** 消费可信 targets 配置；提供 `(tool_id, target_id)` 契约选择、目标集合与模型可见的安全能力说明（§2.1）。

- [x] 先测三个 recording Adapter 返回不同常量；未知/缺少 cluster、用户撤权、重复 ID、未知类型、单目标无 audit、并行不同目标都命中正确分支；拒绝时所有 Adapter/connector 调用为 0。
- [x] 一套 SDK 函数 schema 必须要求 cluster，模型参数不得覆盖 target/连接；MCP 单目标和非 StarRocks 契约成功对照保留。配置旧格式报可操作迁移错误。
- [x] 实现最小装配/路由，检查 Evidence、Session 和所有 catalog 调用者，无只有入口支持多目标的半成品。
- [x] 运行 §5 C1；隔离变异删除目标校验、按 tool_id 单键查找，应分别导致用例失败。提交 `feat: route governed database tools to explicit targets`，独立审查。

**Task 1 实施说明（已审查合入）：**

- **配置：** `targets` 是列表而不是以 ID 为键的对象：JSON 解析对重复键静默取后者，列表才能拒绝重复 ID。每项为 `type`（仅 `starrocks`）、`description`、`business_context`（`version`/`text`，可为 null）与 `starrocks`（原单目标配置）；集群 ID 取 `starrocks.target_id`，限 1–32 位小写字母、数字、`_`、`-`。顶层出现旧 `starrocks`/`business_context` 时报迁移说明，不双读。
- **手写表列字段暂留：** 每个目标仍带 SQLGuard allowlist（`policy`）。取消它依赖 Task 2 的自动 schema 与当前权限，本片只做路由，不提前删除。
- **路由：** 工具目录按 `(tool_id, target_id)` 登记与查找；同一工具登记在多个目标上时，各目标的参数 schema 必须相同且声明 `cluster`。模型看到一个函数，包装层按 `cluster` 选择契约与执行函数，未知、缺失或非字符串的 `cluster` 在授权查询与 I/O 前拒绝；治理层再核对参数 `cluster` 等于契约目标。策略按目标登记（`starrocks.<集群>.<工具>`），数据范围摘要不变。
- **兼容：** MCP 与合成工具不声明 `cluster`，仍各有唯一目标；`data_scope=None` 的指纹公式未改。StarRocks 策略 ID 与会话绑定（新增目标集合与各目标口径）变化，已有 StarRocks 证据与会话一次失效，与 §6 的配置一次迁移一致。
- **说明交给模型：** instructions 列出每个集群的 ID、用途说明、可用工具与业务口径；连接地址、账号与凭据引用不进入 instructions 或工具定义（测试断言）。只配置了审计源的集群登记 `list_slow_queries`。

### Task 2：有界自动 schema 与账号权限验证

**依赖：** Task 1；**结果：** 不需手写表列，新授权可发现、撤权不能继续交付结构/数据；旧快照仅在明确期限内可用。**文件：** starrocks、starrocks_tools、runtime；必要时增加 StarRocks schema 模块；`test_starrocks_adapter`、`test_starrocks_tools`、`test_runtime`，新测试集中在 `tests/p25/test_schema_scope.py`。

**接口：** §2.2 快照/权限结果、时间/容量边界；内部模板不得接受任意 SQL。

- [x] 时钟可控地测试首次失败、成功、刷新失败沿用、到期拒绝、并发刷新只一次、完整替换、取消/关闭、目标独立、超过对象/列/字节限额不能发布部分快照。
- [x] 权限成功/拒绝/网络失败对照，覆盖视图、表/列/角色授权与变化；输出字段不能只按 information_schema 可见性放行。
- [x] 接真实可丢弃 StarRocks 复核 Task 0 权限结果及新建/撤权；记录新授权可见时限和当前权限拒绝边界。
- [x] 运行 §5 C2 与 SR；变异跳过过期/权限检查或发布部分快照应失败。提交 `feat: discover bounded schemas under current database grants`，独立审查。

**Task 2 实施说明（已复审，随 PR #34 合入 `d497a34`）：**

- **快照：** 新增 `starrocks_schema.SchemaCache`，每个目标一份。一次刷新在 `refresh_timeout_seconds` 内依次读取 `information_schema.tables` / `columns` / `tables_config`（排除 `information_schema`、`sys`、`_statistics_`），再在一条连接上对每个对象做零行 SELECT 探测，只保留确认可读的对象。读取超过 `max_objects` / `max_columns` / `max_bytes`、探测无法判定、结果不合契约或超时，都整份不发布；旧快照沿用到它自己的到期时间（从采集开始计），失败不延长期限。single-flight，等待者取消不中止共享刷新，`aclose` 取消进行中的刷新。启动时各目标并行刷新一次（失败只让该目标不可用），之后后台定时刷新。
- **配置：** `policy` 只剩 `allowed_functions`、`max_rows`、`max_sql_bytes`，旧的 `allowed_objects` / `allowed_columns`（及 `target_id`、`default_database`）报迁移说明；新增必填的 `schema_limits`（刷新间隔、最大年龄、刷新期限有设计默认值 60 / 300 / 10 秒，容量必须配置）。
- **工具：** 所有工具先取当前快照，没有或已到期时在任何 I/O 前拒绝。`list_tables` 与 `describe_table` 的结构取自快照，交付前对要列出的每个对象重新探测：列表跳过已不可读的对象，`describe_table` / `describe_table_layout` 遇到已不可读的对象以 `object_unreadable` 停止本轮；探测本身失败（连接、超时、未知错误）是“暂时无法验证”，不当作撤权。两个 describe 工具新增必填参数 `database`。`run_readonly_query` / `explain_query` 的 SQLGuard 范围是快照中**默认库**的可读对象与全部列（跨库见 Task 3），执行时由数据库权限最终把关。
- **与原契约的差异（用户 2026-10-03 决定）：**
  - **列范围（接受）：** 4.1.4 不支持列级授权，配置也不再限列，可读表的全部列都在范围内。列限制须由 DBA 用视图实现。
  - **审计源表（接受）：** 配置的审计源表即使可读也不进入快照，原文只经 `list_slow_queries` 逐条检查后交付；未配置审计源的目标，审计表是普通可读对象，不增加黑名单。
  - **撤权与旧证据（不接受过渡状态）：** 不能等到 Task 5，Evidence 当前权限闭环前移到本 PR，见下一条。
- **Evidence 当前权限闭环（审查修订，前移 Task 5 的必要部分）：**
  - **依赖：** StarRocks 观测由可信代码给出对象依赖（`ObjectDependency`）：列表的每个对象（`listed`，只复核权限）；表结构与布局的对象（`read`，全部列）；查询与计划引用的对象与列（来自 SQLGuard 产物）；审计源（`listed`）与各行原文引用的对象与列。依赖以带格式版本的 JSON 存入新应用表迁移 v4 的 `xiaowei_evidence.dependencies`；声明了数据范围的策略缺依赖、格式不符一律不可读，v3 遗留的 StarRocks 证据因此（及摘要版本变化）一次性失效。
  - **三态复核：** record、Session 回放（整段一批）、最终回答、Session 提交、首次发送、历史读取与 `requests resend` 共用 `EvidenceStore` 读取边界：同一批依赖按目标合并，每个目标一条连接，零行探测当前权限，`read` 依赖再用绑定库表名读当前 `TABLE_TYPE`、`TABLE_ID` 与列类型比较。确定撤权或版本变化为不可读（Session 整段拒绝、提示新建）；连接、超时或无法识别的错误为暂不可验证：本次不交付，Session 保持 active、Evidence 不变，轮次失败码 `scope_unverifiable`、Web 读取 503、飞书投递记为 failed 仍可重发，恢复后新请求重新复核。复核不调用模型、不执行原业务 SQL。
  - **视图：** 依赖非 `BASE TABLE` 读取的证据只在产生它的这一轮首次交付（运行中的模型/最终校验/提交、Web 提交响应、飞书首次发送）；跨轮回放、历史读取与重发在任何 I/O 前拒绝，因此含视图事实的会话不能续轮，需新建会话。
  - **执行计划与审计：** 计划证据在记录时即按当前权限与版本复核，快照之后撤权的对象不交付；审计行交付前对原文引用的对象零行探测，引用已不可读对象的行不列出。
  - **对象版本不比较创建时间：** 4.1.4 实测 `information_schema.tables.CREATE_TIME` 全表读取时按会话时区、带 `TABLE_SCHEMA` 条件时按服务器时区给出，同一对象两种读法相差时区偏移；版本改用 `TABLE_TYPE` + `TABLE_ID`（全局分配、同名重建即变，Task 0 §9.2 与本轮 SR 复核）+ 被读取列类型。
  - **`requests resend`：** 改为连接 StarRocks 做上述复核（只读探测与元数据），需要 StarRocks 密码环境变量。
- **数据范围摘要 v2（审查修订）：** 显式 `format`（`xiaowei.data_scope.starrocks/2`）、`database_type`、集群 ID、host/port/user/TLS、默认库、函数、`max_rows`/`max_sql_bytes`、结果/单值/计划上限、`query_timeout_seconds`、`query_mem_limit_bytes`、`client_timeout_seconds`、`time_zone`、`schema_limits` 与审计源；不含密码引用、`tls_ca_file`、`connect_timeout_seconds`、`pool_size`（只影响能否取得连接）、整轮预算与结构快照。非 StarRocks 的 `data_scope=None` 指纹逐字不变。
- **列表探测上限（审查修订）：** 按已尝试的对象数硬性封顶 `2 × max_rows`，与可读对象多少无关；还有未检查候选时标记截断。Task 6 起列表改为关键词分页，每页只探测本页（至多 `max_rows`），见 Task 6 节末。
- **实测（本机可丢弃 4.1.4，同一 digest）：** 1,000 张可见表、其中 900 张可读，30,000 列。刷新 n=5，中位 1.67 s（1.29–2.36 s）。`list_tables` 一页 200 行（探测 400 个对象），中位 371 ms；`describe_table` 约 7 ms。全快照 27,000 列上 SQLGuard 单次 p50 16 ms，为同步 CPU，Task 3 应只用被引用对象构造 qualify schema。只授 INSERT 的表、只经未激活角色授权的表不进入快照；只授视图的视图进入快照，底表不进入。新授权在下一次成功刷新后可用（最迟一个 `refresh_seconds` 加一次刷新耗时；刷新失败时更晚）；撤权后交付前探测即拒绝，新查询由数据库以 `permission_denied` 拒绝，下一次刷新移出快照；库级授权与撤销同样生效。
- **留给后续：**
  - `list_tables` 仍是无关键词的有界列表；关键词搜索与分页属于 Task 6。
  - 原留给 Task 5 的检查次数上限与计数、跨目标历史的其余验收与 SR 权限子集回归已在 Task 5 完成（上限改为可配置 `budget.max_scope_checks`，见 Task 5 节末）。
  - 视图定义摘要（`SHOW CREATE VIEW`）未采集；视图事实只交付一次（见上），递归核对依赖链按 §9.9 不做。

### Task 3：复杂 SQL、跨库与星号展开

**依赖：** Task 2；**结果：** R3 SQL 保持语义、完整依赖且有限输出；现有已支持的 DISTINCT/CASE/CAST 等只补缺口。**文件：** sqlguard、starrocks_tools；`test_p1b_sqlguard`、`test_starrocks_tool_contracts`、`test_starrocks_tools`；新增 `tests/p25/test_sql_scope.py`。

**接口：** §2.3 封存规范化 SQL、目标/快照绑定、完整列/对象依赖与输出列预算；查询和计划复用。

- [x] 正例覆盖 R3 全集与组合，反例覆盖写/多语句/OUTFILE/LOCK/未知函数、危险表函数/外部访问、未知或歧义对象、CTE 遮蔽、子查询相关列、UNION 分支绕过、窗口引用受限列、重复结果名。
- [x] 检查 AST/规范化前后语义，不用字符串包含替代作用域；星号展开后超过 SQL/列头/结果列上限在业务执行前拒绝；测试边界等于/多 1、UTF-8 和 JSON 转义。
- [x] 更新最坏投影上界，完整/不足容量均有启动和运行对照。不可把整个 schema 拼入每次模型输入。
- [ ] 运行 §5 C3 与 SR；变异忽略 UNION 第二分支、窗口依赖、展开后大小检查应失败。提交 `feat: support bounded complex readonly StarRocks SQL`，独立审查。（C3、SR 与变异已完成，待独立审查）

**Task 3 实施说明（待独立审查）：**

- **范围：** `QueryPolicy.tables` 是结构快照全部库的可读对象与按 `ORDINAL_POSITION` 排序的列（快照中列名只差大小写的对象不进入）；不再有默认库。物理表一律规范化为 `` `库`.`表` ``；未限定表名只在恰好一个库有同名对象时补全，多个库都有时拒绝（新原因码 `ambiguous_object`，提示写成 `库名.表名`），CTE 名优先于同名物理表。`库.表.列` 只接受本层未起别名、库名一致的物理表。依赖改为 `(库, 对象)` 与 `(库, 对象, 列)`，工具按库从快照生成 `read` 依赖。
- **语法：** 根节点可为 SELECT 或 UNION/UNION ALL（含括号分支）；INTERSECT/EXCEPT、`UNION BY NAME`、递归 CTE、窗口框架与命名窗口、`* EXCEPT` 仍拒绝。窗口函数按函数 allowlist（如 `ROW_NUMBER`、`RANK` 须在 `allowed_functions` 中）。相关子查询开放：外层列沿外层 scope 解析并记入依赖。UNION 的 ORDER BY 只接受序号或输出名，名字换为在最左分支中的位置；LIMIT 作用于整个集合，分支内 LIMIT 不改。D1 的序号恢复扩展到 UNION。
- **列名（§9.9 调整）：** 列名按快照（CTE/派生表按其输出名）不区分大小写匹配并改写为来源写法；最外层、CTE 与派生表的裸列先写出原名作为别名，列头与外层按名引用保持用户写法。
- **名字解析（复审 B1）：** 与本层输出别名同名的未限定列按 4.1.4 实测的子句规则绑定，物理列直接写上来源名，不交给 sqlglot 的别名展开：WHERE、JOIN ON 与窗口只认物理列（只有别名时 `column_not_allowed`）；投影、GROUP BY 与 HAVING 先物理列、后别名；ORDER BY 中与本层唯一输出名（显式别名与裸列的隐式列名，不区分大小写）同名的引用原样保留为带引号的输出名，交给数据库绑定（复审第 4 轮）：4.1.4 的输出字段带有来源关系名，排序名在规划时再与投影表达式、分组列和窗口中出现的同来源同名列匹配，可能落到输出列（`SELECT a AS x … ORDER BY x`）或同名物理列（`SELECT a AS x, x AS x2 …`、`… GROUP BY a, x ORDER BY x`、`ORDER BY x + 0` 匹配投影 `x + 0`），原文无效时同样报错。SQLGuard 不复刻这些规则；依赖计入输出表达式的列与本层来源中的同名物理列（都在可读对象中，快照按对象取全部列）。多个同名输出 `ambiguous_reference`（数据库同样报歧义）。分组查询中 sqlglot 会把与投影表达式相同的排序项改写为该投影的别名，限定前用括号隔开、限定后还原。WHERE、ON 与窗口中本层没有的名字按外层来源解析（相关子查询）：各外层合计恰好一个来源时绑定，多个即歧义，中间层有同名来源别名时拒绝；投影、GROUP BY、HAVING 中本层只有别名而外层有同名列时拒绝（4.1.4 取外层列且多半报错）。（复审第 2 轮）
- **星号：** `*`、`别名.*` 只在查询列表中（含 CTE、派生表、子查询），按快照列序完全展开，残留即拒绝；展开到含重名输出的 CTE/派生表时要求先起唯一别名，重名按展开后的真实列名判断（复审 B3）。`库.表.*` 与 `库.表.列` 同样检查库名（复审 B2）。展开后的规范化 SQL 按同一 `max_sql_bytes` 检查（UTF-8）。
- **结果列（只在查询路径）：** 结果列数不超过新增必填配置 `policy.max_result_columns`（`too_many_columns`）；结果列名不区分大小写重复即拒绝（Adapter 无法交付重名列，原先在发送 SQL 后才失败）；结果列名可能来自的查询体（最外层、CTE、派生表，UNION 取最左分支）中，表达式列须有显式别名（`unnamed_column`），因为 sqlglot 会改名为 `_col_N`。执行计划与审计原文不检查这些：`_col_N` 在规范化 SQL 内部前后一致，计划不变；诊断常见的无别名聚合因此仍可取计划。
- **审计：** 新增 `audit_references`（不产出可执行产物），审计原文在 Adapter 与工具中都改用它；未限定名按审计记录所在的目标默认库解析（Task 6 改为每条记录自己的库），显式写出的其他库对象同样按快照与当前权限检查。
- **容量：** 每个结果列名都以别名出现在规范化 SQL 中，列名合计不超过 SQL 字节上限；`worst_case_observation` 改为列数取满、除一列外每列一个最宽转义字符、合计等于 SQL 字节上限。运行时 `_observation` 按同一上界复核。不向模型输入拼接 schema。
- **摘要 v4：** 纳入 `max_result_columns`，格式升为 `xiaowei.data_scope.starrocks/4`；SQL 语义（跨库、星号、大小写）改变，旧 StarRocks 证据一次性失效。
- **验证：** 新增 `tests/p25/test_sql_scope.py`（先写、确认因接口缺失失败后实现）；SQLGuard 旧用例按新契约迁移（UNION/窗口/相关子查询/星号由拒绝改为正例，列名大小写改为接受，顶层重名在查询路径改为拒绝、序号由计划路径证明）；正式装配（真 Runner + 治理 + Evidence + PostgreSQL）跨库查询的交付、依赖与下一轮回放探测，以及列数超限与歧义表名退回模型且零 I/O；最坏投影列数增长与转义列头满载对照。SR 上原 SQL 与规范化 SQL 对 12 类样本（UNION、分支 LIMIT、窗口、多层 CTE、相关 EXISTS/标量子查询、跨库 JOIN、星号、别名星号、列名大小写、TRIM/CAST/COALESCE）逐行、顺序与列头一致，范围取自真实快照。隔离变异：忽略 UNION 第二分支、窗口依赖、展开后大小、唯一匹配、列数上限、结果列名重复、无别名表达式、星号来源重名、相关子查询外层列、最坏列头构造，均有用例失败。
- **复审修复验证（B1–B4）：** 先补离线用例（查询与计划两条路径），在 `d23cb4e` 上 51 项失败、修复后通过；正式装配零 I/O 用例加入库名不符的 `库.表.*` 与 WHERE 别名。SR 新增 `test_r3_names_resolve_like_the_server`：11 类样本（窗口/WHERE/HAVING/投影遮蔽、ORDER BY 别名与同列别名、不同来源星号、CTE 名写法、CTE 星号、多层星号、正确库名星号）原 SQL 与规范化 SQL 的列头、行与顺序一致，计划路径 LOGICAL 计划一致；4 类原 SQL 在服务器报错的样本两条路径均拒绝；该用例在 `d23cb4e` 上失败。隔离变异 8 项（遮蔽列不写来源、WHERE/窗口放行别名、ORDER BY 冲突放行、星号不查库名、星号重名用未展开名、只给最外层写别名、派生列不改写写法、外层同名放行）均有用例失败。
- **复审第 2 轮（`b4add1c`）：** 首轮修复在 ORDER BY 别名规则前就按物理来源拒绝，误拒唯一输出别名（聚合别名、两个来源都有同名列）；相关子查询中本层没有的名字不向外层解析。修复：ORDER BY 先按唯一别名、只拒绝交叉别名；WHERE/ON/窗口向外层查找。来源输出名改为按 scope 缓存、由内向外求得（`_Names`），约 1,200 层星号 CTE 不再抛裸 `RecursionError`；名字绑定中的递归错误映射为 `unsupported_syntax`。离线新增用例在 `b4add1c` 上 21 项失败、修复后通过；SR 对照新增唯一别名（聚合、JOIN、大小写）与相关外层列样本、两个外层来源与交叉别名的拒绝，在 `b4add1c` 上失败；隔离变异 8 项新保护与 7 项原有保护均被发现。
- **复审第 3 轮（`616b9aa`）：** 交叉别名只识别直接的列节点，`SELECT a AS x, (x) AS x2 … ORDER BY x` 被改写为按 `a` 排序后执行（第 2 轮修复引入，4.1.4 按物理列 `x` 排序）；裸列的隐式输出名不参与 ORDER BY 解析，两个来源都有同名列时误拒（`SELECT t.x … JOIN u … ORDER BY x`），另一同名列来自其他来源时也误拒（`t.a AS x, u.x AS ux`）。在 4.1.4 上实测 58 条形态后按上节规则重写 ORDER BY 绑定（`_bind_order_output`）。离线新增用例在 `616b9aa` 上 20 项失败、修复后通过，`CAST(...) AS x, x AS x2` 由拒绝改为接受（实测按别名）；SR 对照新增隐式输出、其他来源、自连接、表达式输出 4 类成功样本（查询与计划路径），以及括号一层/多层、限定、同一来源 JOIN 的交叉拒绝，并断言其 `ORDER BY … LIMIT` 结果与按别名排序不同；正式装配新增交叉别名零 I/O 退回、改正后按隐式输出名执行一次，该用例在 `616b9aa` 上把错误 SQL 发给驱动。隔离变异 8 项：不剥括号、忽略隐式输出名、不比较来源、来源比较失效、表达式输出也查交叉、限定裸列不改写、整条分支失效均被发现；多同名输出取第一个由后续 `_inline_alias_reference` 兜底拒绝，来源不唯一时取第一个不可达（投影列本身已因歧义拒绝）。
- **耗时：** 1,000 对象 / 30,000 列的范围上，构造 `QueryPolicy` 约 4 ms（每次刷新一次）；含 JOIN、窗口与 UNION 的查询 SQLGuard p50 约 1.6 ms，星号展开约 1.1 ms（qualify schema 只含被引用对象；Task 2 全快照时为 16 ms）。
- **复审第 4 轮（扩大复审，`15d5651`）：** 在 4.1.4 上对 738 个组合形态与产品路径的复核发现，ORDER BY 名字绑定仍会被改写：表达式、分组与窗口组合下交付错误的 Top-N，原文报错的排序引用被修正为成功执行（N1）；标量子查询别名被内联进 ORDER BY，数据库拒绝（N2）；同一 CTE 的两个关系别名被当作同一来源而误拒。三者同根：规范化改写了 ORDER BY 的名字（内联输出表达式、按来源推断交叉别名），另有 sqlglot 在分组查询中把排序项改写为别名。修复见上节名字解析：输出名引用原样保留、分组排序项加括号隔开、去掉交叉别名推断（`_column_source`），UNION 的 `ORDER BY (x)` 剥括号后换为位置。验证：离线新增用例在 `15d5651` 上失败、修复后通过；SR 新增 `test_order_by_facts_keep_the_original_binding`（治理 → SQLGuard → 真实驱动 → PostgreSQL 证据 → 最终事实，查询行与原文一致、计划与原文 EXPLAIN LOGICAL 一致，原文报错时两条路径都报错不产生事实；在 `15d5651` 上交付 `(1, 30)` 而非原文 `(3, 10)`）；`test_r3_names_resolve_like_the_server` 增加表达式/分组/窗口匹配、三种标量子查询与 CTE 自连接样本，交叉别名由拒绝改为与原文对照。复审者的 738 个形态与本轮补充的 168 个形态（未限定投影、分组排序项、DISTINCT、HAVING、子查询内 ORDER BY + LIMIT）在 4.1.4 上逐条比较原文与规范化 SQL：除并列值顺序、N3 与下述既有限制外一致或同样报错。隔离变异：恢复内联、去掉括号隔离、保留括号、不计同名物理列依赖、UNION 不剥括号、忽略隐式输出名均被发现；两处重复的多输出检查同时去掉被发现（单独去掉互为兜底）；排序子句检查不可达（`qualify` 已展开 HAVING 中的别名，其余位置先被拒绝）。
- **已知兼容性限制（N3，后续待办）：** 投影含星号且另有与星号列同名的输出、并按限定列排序时（`SELECT t.a AS x, u.* … ORDER BY t.x`），4.1.4 对展开前后的 SQL 行为不同：查询路径因结果列名重复已在 I/O 前拒绝；计划路径展开后的 EXPLAIN 在数据库端报内部错误（工具执行失败，不交付错误事实）。同类按输出名排序（`… ORDER BY x`）因展开后出现同名输出而 `ambiguous_reference`。需要先为冲突列起唯一别名。
- **留给后续：** 关键词搜表与审计按每条记录的库解析（Task 6）；生产 SQL 的函数与语法覆盖（Q4）由 P3 经批准样本验证；`SUM(文本列)` 等隐式转换不拦截（§9.5）。

### Task 4：每条业务查询强制风险准入（已取消）

**状态：** 用户 2026-10-03 取消，决定见 [ARCHITECTURE §6 R4–R5](../../../ARCHITECTURE.md#p25-scope)。保留编号，不作为已实现任务；不实施本片的强制 EXPLAIN、风险准入与专属错误恢复。

Task 5 改为依赖 Task 3。现有运行限额回归由 Task 8 核对，目标资源组与大查询保护在既有 P3 验收，不另建替代任务。

### Task 5：当前权限下的 Evidence 与跨目标历史

> 依赖、三态复核、resend 复核、视图只交付一次、v4 迁移与摘要 v2 已按用户决定前移到 Task 2 审查修订（见 Task 2 实施说明）；下列各项按实际覆盖在 Task 5 复核并补齐其余部分。

**依赖：** Task 3；原 Task 4 已取消，不构成依赖。**结果：** 撤权后新查询与旧事实的再次交付都闭合，范围未变可追问/重发且不重跑业务 SQL。**文件：** models/evidence/session/storage/runtime/channel、下一应用迁移；`test_evidence`、`test_session_policy`、`test_channel_service`、`test_runtime`、`test_starrocks_tools`。

**接口：** §2.6 新依赖、指纹版本、当前数据复核；存储升级与重发新依赖在同片交付。

- [x] PostgreSQL 先保存 A/B 目标的查询、计划、布局、表结构、审计事实；撤销依赖权限后逐个验证回放、首次发送、历史读取、延迟发送、runtime.resend 均不交付事实/混入旧事实的文字。无变化和无关目标成功对照；重发业务 SQL/模型调用均为 0。
- [x] 变更 host/账号/函数/资源/审计源使对应旧记录失效；输入顺序/允许的大小写规范化与 PYTHONHASHSEED 不影响稳定摘要；schema 本身不进入摘要；非 StarRocks `data_scope=None` 指纹与基线逐字相同。
- [x] PostgreSQL 保存有效历史后，让目标超时/断连/返回不可识别错误：本次无事实交付、回放前失败则模型与业务 SQL 为 0；Session 仍 active、原历史/证据不变，恢复后新请求可继续。确定撤权对照必须拒绝旧事实，混合目标一处临时不可用不作废整段历史；SDK 写入不明仍关闭。
- [x] 同批多个 Evidence 引用同一（集群, 对象）只探测一次，列依赖合并；同名跨集群查两次。真实计数断言达到上限时成功/超出一次在探测前拒绝、失败不退款、新边界重查也计数、恢复后下一轮重新计数；容量拒绝不关闭 Session。
- [x] 完整来源依赖由代码构造，篡改/遗漏、视图变化、对象重建按确定失效处理；临时权限服务失败按暂不可用处理。验证源复核不能重新执行原 SQL。
- [x] v3 → 新版本升级只改应用表，旧 StarRocks 证据无依赖则失效一次；重启、旧会话、部分迁移失败、版本不符、SDK 写成功而结果保存失败、恢复后的期限等路径都覆盖。
- [x] 运行 §5 C5 + SR 权限子集；变异仅禁新查询但允许旧 Evidence、只检查第一个目标、resend 跳过数据复核、将超时当撤权而关闭 Session、去掉检查次数上限、data_scope None 被改应失败。提交 `feat: revalidate evidence against current database access`，独立审查。

**Task 5 实施说明（独立审查通过，PR #38）：**

- **检查次数上限：** `Budget.max_scope_checks`（配置 `budget.max_scope_checks`，必填，1–100000）。`EvidenceStore` 每次复核在探测前按（目标, 对象）去重计数并累加，超出时在任何探测前抛 `EvidenceUnverifiableError`（轮次失败码 `scope_unverifiable`、Web 503、飞书投递 failed 可重发），Session 与证据不变；失败不退还。计数放在 `evidence.scope_checks()` 设置的 ContextVar 中：`Application.run_turn` 包住整轮，SDK 派生的工具任务复制 context 后仍引用同一计数，离开即丢弃，没有跨请求状态，也不进入 RunContext。不在其中的读取（首次发送/读取、历史读取、重发）每次单独计数；与原计划“首次交付并入轮次计数”的差别是首次交付发生在轮次结束之后，单独计数仍有同一上限。启动校验要求上限不小于各目标 `policy.max_rows` 的 5 倍（实测一轮新列表的每个对象复核 5 次），否则满页列表必然失败。
- **验收补齐（多数行为已随 Task 2 实现，本片补用例）：** 列表、表结构与布局事实撤权后追问、历史读取与重发都拒绝且业务 SQL 为 0；A/B 混合历史中 B 暂时不可达只阻断本次、Session 与两条证据保留、恢复后同会话继续；历史只有 A 的事实时 B 撤权或不可达不影响且 B 无连接；同名对象在两个集群各探测、各计数一次。摘要稳定性、字段变化失效、`data_scope=None` 指纹与 v3→v4 迁移沿用 Task 2/3 的既有用例，本片未改。
- **证据：** 正式装配 + 隔离 PostgreSQL + StarRocks 驱动替身：一轮 6 个对象的查询正好用 30 次，上限 30 成功、下一轮重新计数仍成功，上限 29 时在最后一次复核前拒绝（只探测 24 次）且 Session 仍 active；历史读取与重发在上限 5 时零探测拒绝、上限 6 时各探测 6 次成功（两条证据引用同一组对象只算一次）；两集群同名对象在上限 11 时零探测拒绝、12 时各探测 6 次；上限的下界（5 × 一页）足够一轮满页列表。隔离变异 13 项均被发现：去掉上限、不包住整轮、不去重、跨目标去重、`>=` 越界、不计数、去掉启动校验、倍数改 4、只在本轮之外复核（放过旧证据）、只复核第一个目标、历史/重发跳过复核、超时当撤权、`data_scope=None` 也进指纹。另在新建的可丢弃 StarRocks 4.1.4 + AuditLoader 5.0.0 上回归了 §5 SR 用例。本片不新增真实场景：计数在 Evidence 层，真实依赖复核已由 Task 2 的 SR 用例覆盖。
- **残余：** 计数假设 SDK 的工具任务在 `run_turn` 的 context 中派生（锁定的 openai-agents 0.22.3 实测成立，换版需复核）；同一会话的历史越长每轮所需次数越多，长会话可能需要新建；替身不能证明真实 StarRocks 的探测耗时，上限的生产取值留待 P3 按目标规模核定。

### Task 6：可发现的表与多库慢查询闭环

**依赖：** Task 5；**结果：** Agent 能有界找表/读字段并定位多库慢查询，原文与证据不越界。**文件：** starrocks、starrocks_tools、runtime；`test_starrocks_audit`、`test_starrocks_audit_real`、`test_starrocks_tools`、工具契约用例。

- [x] 关键词/库过滤/分页/刷新后游标失效/注释截断/同名表成功对照；空关键词、过大页、不可读对象拒绝；分页输出受四种容量约束。
- [x] 审计 Db 与显式引用库不同、跨库 SQL、原文未限定名、Db 为空/未知、无权限引用、可能截断原文、候选耗尽均有验收；审计表有权不代表原文可放行。
- [x] 保留 P2 time_zone、stmt_limit、解析期限与错误整轮停止，不因移除单库过滤读取无界候选。
- [ ] 运行 §5 C6 + SR 审计；变异使用默认库/不校验原文依赖/忽略候选上限应失败。提交 `feat: search schemas and diagnose slow queries across databases`，独立审查。（C6、SR 与变异已完成；第一轮审查意见已修订，待复审）

**Task 6 实施说明（第一轮审查意见已修订，待复审）：**

- **搜表（`list_tables`）：** 参数全部必填：`cluster`、`keyword`（1–64 字符，去空白后不能为空）、`database`（可空）、`page_size`、`cursor`（可空）。关键词不区分大小写匹配快照中的“库名.表名”、表注释（快照已按 `max_comment_chars` 截取）与列名；结果按库表名排序。前置检查从游标位置起，按 `page_size`（至多 `max_rows`）与结果的单值、总字节上限在 I/O 前规划本页对象（与查询读取同一计数规则，`fitting_rows`），只对它们做零行探测；撤权只会让本页变短，已撤权的不列出但已越过。结果带 `next_cursor`：从第一个未交付的对象继续，没有剩余时为 null（此时才不标记截断）。游标为 `<快照版本>-<偏移>-<校验>`，校验由版本、casefold 后的关键词、库过滤、页大小与偏移计算：快照刷新后为 `SNAPSHOT_STALE`，换了参数或手写偏移为 `CURSOR_MISMATCH`，格式不符为 `CURSOR_INVALID`，都在 I/O 前拒绝、可修正；游标只防误用，不是授权凭据，每页仍按当前权限探测。单个匹配对象的一行就超过单值或结果上限时在 I/O 前拒绝（不点名该对象，因为它可能已撤权；提示用库过滤或更具体的关键词避开），不让模型反复缩小页。`page_size` 越界与布尔值在 I/O 前拒绝。搜表与表结构的策略多一个必需字段 `next_cursor`，启动容量检查按各自的固定元数据模板 SQL、列头与最长游标另查。原“2 × `max_rows` 尝试封顶”随分页取消。
- **表结构（`describe_table`）：** 新增必填可空 `cursor`（`describe_table_layout` 参数不变，另用 `TableArgs`）。从游标位置起按结果单值与总字节上限规划本次的列，结果带 `next_cursor`；游标绑定快照版本与库表；每次都重新探测当前权限，撤权后续取整体失败；单列定义就超过上限时在 I/O 前拒绝。
- **多库慢查询：** 审计候选去掉 `db = 目标默认库`，改为读取所有库；新增可空参数 `database`，非空时以绑定值过滤会话当前库。候选另读 `db` 列并输出为 `database`（`''` 输出 null）。原文的未限定表名只在该条记录的会话当前库中解析（`audit_references(sql, policy, database)`；`''` 时未限定名一律不在范围内，不退回唯一匹配补全），限定名照常按快照与当前权限检查；Adapter 过滤与工具路径的依赖重算用同一规则。`QueryPolicy.default_database` 删除：业务查询不再可能带上审计的解析规则。候选 `LIMIT` 绑定 `candidate_rows + 1`，读满且有下一条时标记截断（P2 时读满不标记）。输出列满 `max_rows` 而还有未检查的候选时保守标记截断（不为确认它们是否获准继续解析），获准的行因单值上限未列出时同样标记；原文不可读（NULL）只是无法确认获准，不标记。时间窗、`stmt_limit`、解析期限、候选字节上限与错误整轮停止不变。
- **契约变化：** 三个工具的公开参数与结果形态改变（搜表新增 `keyword`、`database`、`page_size`、`cursor` 与 `next_cursor` 字段；表结构新增 `cursor` 与 `next_cursor`；慢查询新增 `database` 参数与输出列，截断含义扩大到列满上限与上限导致的遗漏）；数据范围摘要升为 `xiaowei.data_scope.starrocks/5`，此前保存的 StarRocks 证据一次性失效。`StarRocksTarget.database` 只剩连接默认库的含义。
- **第一轮证据（`78aa8c0`；其中搜表按原 `page`/`snapshot` 契约，已由下文修订后的证据替代）：** 离线：搜表 15 项（匹配、同名表库过滤、翻页与截断、刷新后旧页拒绝、撤权不前移、8 种参数在 I/O 前拒绝、参数全部必填）；审计 Adapter 4 项（同名表按各自库解析、`''`/未知库只接受限定名、库过滤为绑定值、候选读满标记截断）；正式装配（`open_runtime` + `ChannelService` + 隔离 PostgreSQL + 脚本模型 + 驱动替身）3 项：多库慢查询成功并在撤销 `archive.sales` 后历史读取确定拒绝、零业务 SQL；撤权后新列表移除引用它的行且库过滤为绑定值；搜表先以过大页被拒、修正后翻两页取得两张同名表。真实（新建可丢弃 StarRocks 4.1.4 + AuditLoader 5.0.0）：搜表匹配真实表注释（截取前 100 字符，截取之外的词搜不到）与列名、两页翻页、刷新后旧版本拒绝；两个库执行同一原文，审计 `db` 分别为两库，列表按各自库解析，限定名跨库通过，错库列的失败语句不列出，工具依赖同时指向两库的 `sales`。原审计真实用例改为按会话当前库过滤（实例上其他用例的语句不再挤占候选）；跨库用例只读最近 2 分钟，候选须能读完（两次连续完整运行实测不足 2000 条）。§5 SR 两次连续完整运行各 36 passed，5 个警告都是已知 asyncmy `_finish_unbuffered_query`。环境发现：沿用自 Task 5 的插件包只改了 `max_stmt_length`，`max_batch_interval_sec` 仍是默认 60，与 §9.1 及审计真实用例前提（导入间隔不超过 10 秒）不符，审计用例 60 秒等待因此时过时不过；按 §9.1 改为 10 后稳定。Task 5 记录的首次完整运行中那一项审计用例失败，很可能也是这个原因（未复现核实）。隔离变异 13 项均被发现：按连接默认库解析、无当前库时退回唯一匹配、不校验原文范围、工具依赖固定按默认库、候选不多读一条、忽略库过滤、不核对快照版本、忽略关键词、搜表忽略库过滤、页大小不设上限、不做交付前探测、页码不偏移；另有一项因变异写法引入未定义名、失败原因不对，已替换为固定按默认库解析依赖的变异。
- **第一轮审查修订（head `78aa8c0`）：** 审查指出的三项均在该 SHA 复现成立。共同根因：把“截断标志”当成可恢复的分页，却没有保存或返回第一个未交付的位置——搜表页内按字节截断后续页仍从 `page × page_size` 开始，被截掉的对象永久取不到（复现：8 张带 150 字符注释的表、`page_size=5`，第 1 页返回 t0–t3，第 2 页从 t5 开始）；`describe_table` 截断后没有任何参数可取得后续列（复现：20 列宽表只返回 8 列）；慢查询在 `_listed` 列满 `max_rows` 时直接结束并报告未截断（复现：6 条获准候选、`max_rows=3`，返回 q0–q2 且 `truncated=false`），原用例把这一错误断言成了 `false`。修复：搜表与表结构改用上述续取游标与 I/O 前按字节规划的页；慢查询列满上限而仍有候选、或获准行因单值上限丢弃时标记截断，原断言随契约改正并扩为三组对照。新增/改写的失败用例在原 SHA 上全部失败（慢查询 3 项失败、恰好上限且读完的对照通过；搜表与表结构用原契约的复现脚本失败），修复后通过。
- **修订后的证据：** 离线：搜表与表结构 25 项（匹配、同名表库过滤、按序续取到 null、字节上限触发后无遗漏无重复且每页放得下、撤权不前移、刷新后旧游标拒绝、换关键词/同义关键词/库过滤/页大小与手写偏移都被拒、单行与单列放不下时 I/O 前拒绝且可避开、宽表取得全部 20 列、游标绑定库表、续取间撤权失败、参数全部必填）；审计 Adapter 新增合格行超过上限/恰好上限且读完/上限后仍有未检查候选三组与获准行因单值上限丢弃一组；正式装配 5 项（原 2 项多库慢查询、新增：`max_rows=2` 时模型收到 `truncated=true` 且交付事实区显示“结果已截断”；搜表先过大页被拒、续页换库过滤收到 `CURSOR_MISMATCH` 后原样续取成功；宽表三次取得全部列）。阶段回归 1846 passed、浏览器 3 passed。SR（新建可丢弃 4.1.4 + AuditLoader 5.0.0，导入间隔 10 秒）：第 1 次完整运行 35 passed、1 failed——失败发生在插件刚装好后第一个审计用例的等待写入阶段（测试辅助以管理员查审计表 60 秒未见全部 13 条，产品代码尚未读取），与已记录的“插件刚安装后首批记录可能缺失”一致；随后连续两次完整运行各 36 passed，警告都是已知 asyncmy `_finish_unbuffered_query`。隔离变异 12 项均被发现（对照 142 passed）：列满上限不标记、丢弃获准行不标记、续页越过放不下的对象、校验不含参数、校验不含偏移、不核对快照版本、表结构不给游标、不拒绝放不下的单行、不拒绝放不下的单列、续取不复核权限、搜表从不标记截断、规划不计总字节。
- **残余：** 去掉库条件后候选来自整个集群，热门集群可能在 `candidate_rows` 内只看到少量获准记录（标记截断，`database` 参数可缩小范围）；列满 `max_rows` 的截断是保守的，后面的候选可能最终都不获准；“库名.表名、注释、列名”的子串匹配不是全文检索，`.` 这类关键词仍可逐页遍历全部可读对象（每页有界、计入工具次数）；单个对象或单列放不下结果上限时只能避开或调大目标的 `max_value_bytes`/`max_result_bytes`；审计 `db` 的语义只在 4.1.4 + AuditLoader 5.0.0 上实测，目标版本在 P3 复核（Q6）；替身与真实容器都不能证明真实模型会正确原样传回 `next_cursor` 并保持参数（Task 7 / P3）。

### Task 7：单 Agent 的自然语言工具选择与用户任务

**依赖：** Task 6；**结果：** 保持一个业务 Agent，明确请求可查询，解释/生成/否定/模糊场景按固定样例不查询；自然语言可靠性在 P3 真实模型验收。**文件：** app/channel/runtime、既有 instructions/工具说明、models/evidence/session 回答契约、Web 与飞书默认入口；`test_app`、`test_runtime`、`test_diagnosis`、`test_gate0`；场景测试 `tests/p25/test_turn_purpose.py`，复用 `tests/sdk_core/gate0.py` 样例体系。

- [ ] 真 Runner + ScriptedModel 验证只有业务 Agent 的调用链，没有额外分类请求；获准用户查询工具可见，未授权/目标错误在 I/O 前拒绝。历史暂时无法验证时模型未收到旧事实，Session 保留；确定失效按 Task 5 处理。
- [ ] 业务正例：指定集群昨日订单跨库 JOIN/CTE、UNION、窗口排名、随后解释慢原因、换另一集群比较；SQLGuard 的 I/O 前拒绝在预算内修正成功；运行时资源超限停止并提示缩小范围，由用户发起新请求，不在本轮自动重跑。
- [ ] 固定反例：只解释、只写 SQL、不要执行、裸 SQL、引用“帮我查”、上一轮 query 本轮只诊断、缺集群/口径/时间、多人指令混淆、工具/表注释注入。脚本模型证明预期调用与结果链；P3 真实模型证明是否真的没有选择查询工具，不把预设输出当意图能力证据。
- [ ] 普通自然语言“不要执行”场景中，故意让替身误调工具：断言仍受当前权限/SQLGuard/限额约束，并记录通过这些保护后可能执行的残余；显式诊断入口同样强行调用应在业务执行前拒绝。不得为了让自然语言反例变绿新增关键词识别器或第二个分类 Agent。
- [ ] 无 Evidence 建议、澄清、有证据回答分别渲染；旧回答回放兼容或明确失效，四种投影覆盖新分支。验证 Agent 不把可修正拒绝伪装成成功事实。
- [ ] Web/飞书普通消息默认单 Agent 判断，显式诊断快捷方式保留；SDK 轮数/预算/usage/超时没有新旁路。运行 §5 C7 和正式入口脚本模型/浏览器，提交 `feat: let the single agent choose query tools from user intent`，独立审查。

P3 在具体 Model Profile 上记录每个固定样例的调用轨迹、误执行次数、成功率、步数、延迟/用量，区分正常查询成功与资源超限失败；这些结论不能由离线测试数量替代。

### Task 8：阶段离线退出与可接续交付

**依赖：** Task 7；**结果：** 一个精确候选 SHA 具备完整离线/隔离服务证据、配置迁移与恢复说明，交给群聊阶段；不关闭 P3 实战。

- [ ] 运行 §5 全阶段检查（C4 随 Task 4 取消），核对既有限额的成功与超限失败回归；正式 runtime + Web/飞书替身 + 测试 PostgreSQL 演示完整任务链，验证三目标中的一处故障不改查别处。
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
| C4 | 随原 Task 4 取消，不创建专属检查组；现有 Adapter 资源限额回归保留 |
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
- 不因元数据缺失切回旧 allowlist 或省略当前权限复核；单目标失败隔离，不自动换集群。缺新鲜结构/可靠权限证据时功能受控不可用，是明确的保守降级。

## 7. 尚未解决且影响正确性的疑点

| 编号 | 必须取得的事实 / 最小处理 | 阻塞与关闭者 |
| --- | --- | --- |
| Q1 | 4.1.4 的元数据权限过滤、角色/列授权支持、零行 SELECT 权限检查、视图/对象版本可见性 | Task 0 执行者提供正反证据、独立审查；未闭合不启用 Task 2/5 相应范围 |
| Q2 | 目标上只读账号实际命中的资源组与限额；正常 SQL 可用与超限查询受控失败 | Task 0 隔离事实保留；P3 按 ARCHITECTURE §6 R5 验证目标配置、实际行为和阈值，变更后复核；未满足不开放该目标试用 |
| Q3 | 至少千表/三万列的 schema 和权限探测是否满足所选容量/期限 | Task 0 量测后修订 §2.2 配置设计；不以无限缓存或省权限检查解决 |
| Q4 | 生产 SQL 的真实函数/语法覆盖与归一化结果正确性 | Task 3 合成反例 + P3 经批准样本；不自动放开未知函数/节点 |
| Q5 | 单 Agent 的自然语言工具选择误判、修正能力、延迟/用量 | Task 7 离线只验调用链/可信授权与显式模式门控；P3 用固定正反样例和具体 Model Profile 验证，保留误判残余 |
| Q6 | 目标实际 StarRocks/AuditLoader 版本、Db/时区/stmt_limit、元数据与计划差异 | 继承 P2 G-A；P3 在获准目标逐项验证，不能以 4.1.4 容器替代 |

无新的产品方向待决定；上表是有明确关闭任务的技术门槛。若事实推翻用户要求或需要放宽权限，先报告证据和最小范围调整，不在实现中隐含修改。

## 8. 自审清单与审查交付

- [x] Agent 任务、所需信息、工具组合、自我修正、澄清和用户限制说明有场景与对应任务；SDK 原生能力与本地探针限制单列。
- [x] R1–R6 落到 Task 1/2/3/5/6/7/8 与 P3 既有验收；原 Task 4 标注取消并移除依赖；群聊只有依赖链接；旧 P2 计划不重写。
- [x] 业务 SQL、EXPLAIN、权限探测/元数据的 I/O 证据分开；权限、SQLGuard 与运行限额不能通过旧工具或 sealed SQL 旁路。
- [x] 数据范围由数据库权限决定，schema 不冒充权限；确定失效与暂不可验证分开，按对象去重及整轮检查上限明确；所有 Evidence 读取链、重发新增 I/O、混合 Session 和一次性失效明确。
- [x] 容量上界独立于全库列数，未知/失败行为和正常查询成功对照齐全；没有只验证“全部拒绝”的空洞成功。
- [x] 配置/迁移/SDK 表归属、恢复与旧进程风险明确；不把离线、隔离数据库、真实模型、部署和用户验收混为一谈。
- [ ] 针对本轮最终文档提交的精确 SHA 独立审查；文档检查通过不代表此项已完成。

## 9. Task 0 证据（2026-10-03，已复审）

基线 `719c1c70d8d790e2db38e8791aa3a2af34b1322c`，只改文档；`src/`、`tests/`、配置、依赖与 P2 计划无差异。下列结论只覆盖本机可丢弃 StarRocks 4.1.4 与锁定依赖，不代表 P3 目标集群（Q6）。

### 9.1 环境、复现与清理

- **镜像与版本：** `starrocks/allin1-ubuntu@sha256:faf7ce9c24d9c29c9431b4e8cbd4bb7a74cd169907c63f0c5ebaacc7f9df276b`，`current_version()` = `4.1.4-4a9848e`；官方 AuditLoader 5.0.0（`auditloader.zip` sha256 `cd2a8ace…8ea2`，jar sha256 `031848a2…2005`），`plugin.conf` 只改 `max_stmt_length=1000`、`max_batch_interval_sec=10`；审计表按官方 5.0.0 DDL 建在 `starrocks_audit_db__.starrocks_audit_tbl__`。Python 3.11.16，本工作树 `.venv`（`uv sync --locked --offline --extra dev`）：sqlglot 30.17.0、asyncmy 0.2.15。
- **隔离：** 新建容器 `xw-p25-t0-sr`（标签 `xiaowei.task=p25-task0`），只发布 `127.0.0.1:59030→9030`、`127.0.0.1:58030→8030`；测试 PostgreSQL 为 `compose.sdk-test.yml` 的 `xiaowei-sdk-test`（127.0.0.1:55432）。本机只有 `docker-compose`（无 compose 插件），§5 PG 行的 `docker compose` 需写作 `docker-compose`。未接触 `xiaowei-release-*`、用户 StarRocks、真实模型、飞书或 MCP。
- **账号与数据：** 管理员 root 只做建库/授权/插件/FE 设置与对照；结论均以随机口令只读账号 `xw_p25_ro` 实测（口令只存工作树外权限 600 的临时文件，结束删除）。合成库 `p25a`/`p25b`：直接授权、库级授权、角色授权、INSERT-only、无权限、视图（底表无权）、同名表跨库、大小写同名表；`p25a.big` 按天 30 分区 × 4 桶，`generate_series` 写入 1,000,000 行；`p25a.nostats` 在关闭 `enable_statistic_collect_on_first_load` 时写入 200,000 行后恢复开关；异步 MV `mv_daily`（只读账号无权）；规模库 `p25scale` 1,000 张 × 30 列（含中文注释，库级 SELECT）与 `p25scale_w` 100 张 × 30 列（只授 INSERT）。
- **方法：** 实验脚本放工作树外（会话 scratchpad，不入库），每组先登记预期与成功/拒绝对照。asyncmy 取 errno 与消息，容器内 `mysql` CLI（只读账号）取 SQLSTATE；记录只保留去掉地址/账号的消息模板。规模与权限探测经真实 `guard_readonly_query` → `open_starrocks(...).run_query`（每次新连接、设置并回读会话限额），与产品路径相同。
- **既有实测：** 开始与结束各跑一次 §5 SR 命令（`-m 'starrocks_real or starrocks_audit_real'`），均 19 passed、6 warnings，全部为已知 asyncmy `MySQLResult._finish_unbuffered_query was never awaited`，无新增类别；按 §5 不加 `-W error`、不加过滤器。
- **审查轮补证（基于 `0978ade`）：** 为审查意见 B1–B3 与 D1 范围，用同一 digest 新建可丢弃容器（标签 `xiaowei.task=p25-task0-review`，只绑定 127.0.0.1:59030），另建合成库 `r1`（嵌套视图）、`r2.skew`（倾斜分区/分桶）与 `r4.t_sem`。临时把 FE `tablet_stat_update_interval_second` 改为 10（实测要等当前 300 s 周期结束才生效），之后恢复 300 并回读。本轮资源组 `rg_r3_*` 均已删除，只剩默认组；容器与只读口令文件已删除，59030 不再监听。
- **恢复与清理：** FE `query_explain_level` 改 ANALYZE 后恢复 NORMAL 并回读；`enable_statistic_collect_on_first_load` 恢复 true 并回读；资源组 `rg_p25_ro` 删除后只剩 `default_wg`/`default_mv_wg`。容器（含一次为 §9.6 规模元数据补测重建的同名容器）与 `xiaowei-sdk-test` 均已删除，59030/55432 不再监听；镜像未声明卷，未留下本轮卷。

### 9.2 权限、元数据与对象版本（§2.2，Q1）

| 事实 | 4.1.4 实测 |
| --- | --- |
| 列级授权 | `GRANT SELECT(id, a) ON TABLE …` 为 1064 语法错误：**不支持**。数据范围只能到表/视图；列限制须由 DBA 用视图实现 |
| 角色 | `activate_all_roles_on_login=false`：授予用户的角色登录后 `current_role()`=NONE，角色权限下的表在 `information_schema` 不可见、SELECT 5203；`SET ROLE ALL` 或 `SET DEFAULT ROLE` 后才生效。Adapter 不设置角色，**只靠角色授权的账号在当前产品里没有任何数据权限**，须 DBA 设默认角色（或直接授权），列为 P3 验收项 |
| 可见 ≠ 可 SELECT | 只授 INSERT 的表出现在 `information_schema.tables/columns/tables_config`、`SHOW TABLES`，任何 SELECT/探测均 5203；千表实验同样多出 100 张表、3,000 列 |
| 视图元数据 | 视图在 `tables_config` 中 `TABLE_ID=0`；只读账号对视图有 SELECT、对底表无权时 `information_schema.views` **无行**（管理员可见定义；补授底表后可见），`SHOW CREATE VIEW`（与 `SHOW CREATE TABLE`）可返回定义 |
| 零行探测 | `SELECT 1 FROM db.t WHERE 1=0`、`… LIMIT 0`、`WHERE FALSE`、未使用的 CTE、`1=0 AND EXISTS(…)`、派生表、UNION 任一分支引用无权对象均 5203；有权对象返回 0 行。视图有权、底表无权时视图探测成功、底表探测 5203（两个方向成立） |
| COUNT(*) | 无权表 5203；有权表分区裁剪后计划为 `META-SCAN` 仍先做权限检查 |
| 撤权 | REVOKE 表/视图/库级/角色后，**同一已打开连接**的下一条语句即 5203，新连接相同；无缓存宽限 |
| EXPLAIN 与 SELECT | 所有样本中 `EXPLAIN LOGICAL` 与实际 SELECT 的权限结果一致，但计划会把查询透明改写到只读账号**无权**的 MV（`SCAN [mv_daily]`，执行成功）；权限依赖必须取自 SQL 而非计划对象，EXPLAIN 不作为权限证明 |
| 表版本 | DROP+CREATE 同名表：`TABLE_ID`（11214→11288）与 `CREATE_TIME` 变化，**原授权随旧对象消失**（新表 5203）。`ALTER TABLE ADD COLUMN`：`TABLE_ID`、`CREATE_TIME`、`UPDATE_TIME` 都不变，只有列集合变化 |
| 视图版本 | DROP+CREATE 视图：`CREATE_TIME` 变化、授权消失。`ALTER VIEW` 保留授权与 `CREATE_TIME`，`tables` 无任何变化，但可让同一视图**暴露此前不可见的底表列**（实测改为暴露 `secret` 后只读账号可读）；视图**自身定义**的变化只能由 `SHOW CREATE VIEW` 文本与视图列集合发现，间接依赖的变化见本表最后一行 |
| 标识符 | `lower_case_table_names=0`：库名、表名大小写敏感（`CaseT`/`caset`、`p25a`/`P25A` 可同时存在，错写为 5502/5501）；列名不敏感；表别名敏感（`X.id` 对别名 `x` 为 1064） |
| 视图的间接依赖（审查 B1 补证） | 只读账号只有外层视图 `v_outer`（`SELECT id, a FROM v_inner`）的 SELECT，内层视图 `v_inner` 与底表无权。`ALTER VIEW v_inner AS SELECT id, secret AS a …` 后，`v_outer` 的 `a` 列返回原 `secret` 值，而 `v_outer` 的 `CREATE_TIME`、`SHOW CREATE VIEW` 摘要与列签名**逐字不变**；底表 DROP+CREATE 同名重建并换数据、底表加列，外层元组同样不变。只读账号对 `v_inner` 的 `SHOW CREATE VIEW` 为 5203，`information_schema` 中看不到内层视图与底表。`SELECT *` 视图在创建时展开，底表加列后视图列不变 |

**结论：** 零行探测可作当前 SELECT 权限证明（不被优化消除、覆盖表/库/视图/已激活角色），§2.2 首选方案成立。对象版本**不能**只靠 `information_schema.tables`。直接查询的表：最小版本标识为 (`TABLE_ID`, `CREATE_TIME`, 依赖列的名字/类型签名)，可发现重建与列变化。视图：(`CREATE_TIME`, `SHOW CREATE VIEW` 文本摘要, 列签名) **只覆盖视图自身定义**，不能发现内层视图或底表的变化；只读账号通常无权读取这些间接依赖，无法递归核对。因此依赖视图的历史 Evidence **不能证明版本未变**，按 §2.2“缺少可靠视图版本时历史重放保持关闭”处理：本轮新读取仍由数据库实时权限把关，跨轮回放、历史读取与重发对这类证据一律按确定失效拒绝。只有在 DBA 授予整条依赖链的读取权限并实现递归核对后才能重新评估，本阶段不做。视图定义只能逐个 `SHOW CREATE VIEW`（代码模板 + 校验后的标识符），不能批量从 `information_schema.views` 取。

### 9.3 规模：1,000 表 / 30,000 列（§2.2 设计值，Q3）

只读账号、单 FE/BE 容器（宿主 2 vCPU、约 3 GB 给 Docker）。**计时口径：** 客户端 `time.perf_counter()`，含执行与 `fetchall` 读完全部行，不含 JSON 序列化。p95 取升序第 ⌊0.95n⌋ 个样本（从 1 计；n=15 时为第 14 个，n=1,000 时为第 950 个）；n=3 的分页项只给中位数与最大值，不给 p95。

| 操作 | 连接方式 | 请求数 / 样本数 | 耗时 | 行 / 原始字节 / JSON 字节 |
| --- | --- | --- | --- | --- |
| `information_schema.tables`（两库） | 同一长连接，不含建连与会话设置 | 1 / n=15 | p50 16.9、p95 57.6 ms | 1,100 / 58,190 / 82,390 |
| `information_schema.columns` 一次读全 | 同上 | 1 / n=15 | p50 103.3、p95 197.3 ms | 33,000 / 1,941,200 / 2,997,200 |
| 列按每批 100 张表 | 同上 | 10 / n=3 整轮 | 中位 794、最大 988 ms | 30,000 |
| 列 keyset 分页每页 2,000 行 | 同上 | 16 / n=3 整轮 | 中位 1,501、最大 1,604 ms | 30,000 |
| 零行探测，同一连接连续 | 同上 | 1,000 | 单次 p50 0.6、p95 1.9 ms；合计 0.82 s | — |
| 零行探测，`Adapter.run_query` 串行 | 每次新连接 + 设置并回读会话变量 + 执行 + 关闭；**不含 SQLGuard**（封存产物预先生成） | 1,000 | 单次 p50 3.8、p95 8.0 ms；墙钟 4.51 s；前 128 次 0.59 s | — |
| 同上，并发 4（`pool_size=4`） | 同上 | 1,000 | 单次 p50 6.7、p95 12.0 ms；墙钟 1.85 s | — |
| SQLGuard 生成探测产物（30,000 列 allowlist） | 纯 CPU，同步 | 1,000 | 单次 p50 16.9、p95 18.1 ms | — |

INSERT-only 表经 Adapter 映射为 `permission_denied`。`WHERE 1 = 0` 在 FE 折叠为 `EMPTY`（`EXECUTE IN FE`），探测成本与表数据量无关。SQLGuard 单次 CPU 随 allowlist 线性增长：30 / 3,000 / 30,000 列时 p50 0.40 / 2.03 / 16.55 ms，Task 3 应只用被引用对象的结构构造 qualify schema，而不是整个目标快照。

**结论（标明实测与推算）：** 实测只覆盖上表各单项，没有端到端刷新或整轮复核的计时。推算如下：一次结构刷新的两条元数据读取在长连接上 p95 之和约 0.26 s，产品若每条读取都新建连接并设置会话，再加约 2 × 3.8 ms。若刷新同时对 1,000 个对象做探测，再加 1.85 s（并发 4）到 4.51 s（串行）；按 §9.2 对视图逐个 `SHOW CREATE VIEW` 的成本未测。128 次检查在 Adapter 上实测 0.59 s；若每次再经 30,000 列的 SQLGuard，需另加约 128 × 16.9 ms ≈ 2.2 s 同步 CPU（推算）。§2.2 规定探测由代码模板生成，不必走 SQLGuard，但这条路径尚无公开实现，Task 2 必须实测并避免在事件循环中做这类 CPU 工作。在这些前提下，60 s / 300 s / 10 s 与 `max_scope_checks_per_turn=128` 暂不调整；Task 2 须以端到端计时复核，P3 在目标上复测，不据此给生产通过线。

### 9.4 sqlglot 规范化与 StarRocks 语义（§2.3，Q4）

以只读账号分别执行原 SQL 与 `qualify(expand_stars=True, identify=True)` 后的 SQL 比较结果（有 ORDER BY 比有序列表）。**一致：** UNION、UNION ALL + ORDER/LIMIT、聚合 OVER、RANK/LAG、两层 CTE、跨库 JOIN + `x.*`、CTE/派生表嵌套 `alias.*`、CTE 遮蔽同名物理表、相关子查询、CASE/CAST/TRIM/COALESCE、各分支 LIMIT 的括号 UNION、`date_trunc`、反引号、显式 `default_catalog`、默认与 DESC 的 NULL 排序。**差异与反例：**

| 样本 | sqlglot 30.17.0 | 4.1.4 | 现有 SQLGuard（`719c1c7`） |
| --- | --- | --- | --- |
| 重复输出名 + `ORDER BY 1` | 改为 `ORDER BY id`，执行 1064 歧义 | 原 SQL 正常 | **接受并改为错误列**（见 9.8 D1） |
| `WHERE` 引用输出别名 | 内联为原表达式，可执行 | 原 SQL 1064 | Task 3 复审起拒绝 `column_not_allowed`（同名物理列存在时按物理列） |
| `SELECT ID`（列大小写不同） | qualify 失败 | 正常（列名不敏感） | 拒绝 `column_not_allowed`（误拒，安全方向） |
| 无别名函数列 | 列名改为 `_col_0` | 列名 `count(*)`、`sum(txt)` | 同 sqlglot：列头变化 |
| `b DIV 2`（BIGINT） | 改写为 `CAST(b / 2 AS INT)`，5000000001 得 NULL | 2500000000 | 拒绝 `unsupported_syntax` |
| `s \|\| 'x'` | 改写为 `s OR 'x'` | 默认 `sql_mode=ONLY_FULL_GROUP_BY` 时也是 OR（结果相同）；`PIPES_AS_CONCAT` 时为拼接（结果不同） | **接受并改写为 OR**（见 9.8 D2） |
| `other_cat.db.t` | 保留 | 5078 未知 catalog | 拒绝（任何带 catalog 的名字） |
| GROUP BY 漏列、`SUM(文本列)` | 接受 | 见 9.5 | 接受 |

### 9.5 EXPLAIN LOGICAL 阶段错误（§2.5）

以下保留 Task 0 的实测事实与当时提出的白名单方案；原 Task 4 取消后，不实施该错误恢复方案，当前行为以 §2.5 为准。

对同一 SQL 分别取 `EXPLAIN LOGICAL` 与实际执行，两者错误一致的不再分列：

| 类别（受测模板） | errno / SQLSTATE | 例 |
| --- | --- | --- |
| 语义分析 `Getting analyzing error …` | 1064 / HY000 | GROUP BY 漏列（`'…' must be an aggregate expression or appear in GROUP BY clause`，含 HAVING/ORDER BY 非分组列）、未知列 `Column '…' cannot be resolved`、歧义列 `is ambiguous`、未知函数或参数个数 `No matching function with signature`、UNION 列数不等、GROUP BY 序号越界、别名大小写不符 |
| 语法 `Getting syntax error …` | 1064 / HY000 | 残缺 WHERE |
| 未知表 / 库 / catalog | 5502 / 42602；5501 / 3F000；5078 / 42000 | 表名大小写不符也是 5502 |
| 无 SELECT 权限 | 5203 / 42000 | 含子查询内；消息带对象名与当前/未激活角色名 |
| **不报错** | — | `SUM(txt)`/`AVG(txt)`：数字字符串隐式转换求和（`'10','20','x'` → 30.0，`'x'` 静默当 NULL），全非数字 → NULL；`val / 0` → NULL；`CAST('x' AS INT)` → NULL；`dt = 'not-a-date'` → 空结果；`RANK() OVER ()`、多列 `COUNT(DISTINCT a, b)` 正常 |
| 只在执行期出现 | 1064 / HY000 | 标量子查询多行 `Expected LE 1 …`、`assert_true` 失败、内存超限（消息含 **BE 地址**）、资源组 scan/CPU/并发超限；EXPLAIN LOGICAL 对这些都成功 |
| 连接 / 中止 | 2006（服务端 KILL 连接后客户端）、1317（KILL QUERY）、5024（query_timeout） | — |

**当时提出的可恢复白名单（已取消，只针对 EXPLAIN 阶段、业务 SQL 未发出）：** 5203、5502、5501、5078，以及消息以 `Getting analyzing error` 开头的 1064。errno 1064 同时覆盖语法、语义与执行期错误，SQLSTATE 均为 HY000，**不能只按错误码判定**。规范化 SQL 由代码生成，EXPLAIN 出现语法错误说明规范化或方言与服务端不一致，应停止而不是交回模型修正。执行阶段的任何 1064/5024/1317/2006 都按“结果可能未知、停止、不重放”。服务端消息含对象名、字面量、角色名与 BE 地址，只能内部匹配，不能投影。`SUM(文本列)` 等隐式转换不能靠 EXPLAIN 发现；如需拦截，只能由 Task 3 按快照列类型限制聚合参数。

### 9.6 统计信息与 LOGICAL 风险字段（§2.4，Q2）

- **默认配置（FE）：** `enable_statistic_collect=true`、`enable_auto_collect_statistics=true`、`enable_collect_full_statistic=true`、`enable_statistic_collect_on_first_load=true`、`enable_statistic_collect_on_update=true`、`statistic_collect_interval_sec=600`、`statistic_auto_collect_ratio=0.8`、`statistic_auto_collect_small_table_rows=10000000`、`statistic_auto_collect_large_table_interval=43200`、`statistic_auto_analyze_start_time/end_time=00:00:00/23:59:59`、`tablet_stat_update_interval_second=300`。
- **观察到的自动采集：** 小表首次导入后约 1 s 内 `FULL ONCE`；百万行 `big` 首次导入 `SAMPLE ONCE`，约 2.3 分钟后一次 `FULL SCHEDULE`。`s_new` 第二次导入 +400k 行后 60 s 内无新任务（随后做了人工 ANALYZE，未继续观察调度）；关闭 first-load 时导入的 `nostats` 十分钟后仍只有占位元数据（`init_stats_sample_job=true`、Healthy 0%），没有采集任务。**人工：** `DROP STATS` 后计划回退，`ANALYZE TABLE … WITH SYNC MODE` 后恢复。三者分开记录，人工结果不证明默认已生效。
- **缺统计的表现不唯一：** `DROP STATS` 后扫描为 `row: 1` 且 `cpu/memory/network: ?`；从未采集的 `nostats` 显示 `row: 100000`（实际 200,000），**没有 `?`**；百万行表在列统计采集前，`v = 3` 估算为 499,995（50%），同样无 `?`。仅凭 LOGICAL 文本不能可靠识别缺统计。只读账号可读 `SHOW STATS META` / `SHOW ANALYZE STATUS`（只列有任一权限的表，含 Healthy 与采集时间），无权读 `_statistics_.column_statistics`。
- **规模元数据：** 对有权表，只读账号可读 `information_schema.tables.TABLE_ROWS/DATA_LENGTH` 与 `information_schema.partitions_meta.ROW_COUNT/DATA_SIZE`（与管理员相同），但导入后约 5 分钟（一个 `tablet_stat_update_interval_second` 周期）内为 0；`SHOW PARTITIONS` 对无权表 5203。
- **分区/tablet 比例不是行数比例（审查 B2 补证）：** 表 `r2.skew` 30 个日分区、4 桶，day1 970,000 行，其余每天 1,000 行；分桶键 97% 为同一值。实测：

  | 查询 | 计划比例 | 实际匹配行 | 比例 × `TABLE_ROWS`(999,000) | 计划 SCAN 估算 | 前 a 大分区之和 |
  | --- | --- | --- | --- | --- | --- |
  | `dt = day1` | partition 1/30、tablet 4/4 | 970,000 | 33,300 | 33,300 | 970,000 |
  | `dt = day2` | partition 1/30、tablet 4/4 | 1,000 | 33,300 | 33,300 | 970,000 |
  | `k = 1 AND dt = day1` | partition 1/30、tablet 1/4 | 940,900 | 8,325 | 1,332 | 970,000 |
  | `k = 1` | partition 30/30、tablet 30/120 | 940,929 | 249,750 | 39,960 | 999,000 |

  比例按个数计，倾斜时可低估两个数量级，不能乘以总行数。LOGICAL 不给出选中的分区名（普通 EXPLAIN 也只有 `partitions=1/30`）。保守上界只能取“`partitions_meta` 中 ROW_COUNT 最大的 a 个分区之和，忽略 tabletRatio”，上表四例均不低于实际。**该上界依赖元数据新鲜度：** 向 day2 再写入 500,000 行后，`partitions_meta.ROW_COUNT` 仍为 1,000（实际 501,000），而 `VISIBLE_VERSION` 2→3、`VISIBLE_VERSION_TIME` 立即更新。行数要等下一次 tablet 统计上报（`tablet_stat_update_interval_second`，默认 300 s），上报前该分区的上界会低估。只读账号能看到版本时间，看不到行数对应的版本。
- **估算不是扫描量（Review Focus 1 反例）：** `SELECT … FROM big WHERE id * 2 = 3 LIMIT 10` 计划扫描 `row: 2`、`partitionRatio: 30/30, tabletRatio: 120/120`，实际审计 `scanRows=1,000,000`。不带过滤的 `LIMIT 10` 扫描 40 行。审计 `scanRows` 本身也会因字典/zone map 过滤为 0（`txt = 'never'`），不能当真实读取量。
- **LOGICAL 可解析内容：** 行首 `EXECUTE IN FE`（可选）、`- Output => […]`；算子 `SCAN`、`META-SCAN`、`SCHEMA-SCAN`、`EMPTY`、`VALUES`、`TABLE FUNCTION`、`AGGREGATE(GLOBAL|LOCAL|DISTINCT_GLOBAL|DISTINCT_LOCAL)`、`EXCHANGE(GATHER|SHUFFLE|BROADCAST|ROUND_ROBIN)`、`HASH/INNER JOIN`、`HASH/RIGHT SEMI JOIN`、`NESTLOOP/CROSS JOIN`、`SORT(FINAL|PARTIAL|GLOBAL)`、`TOP-N(FINAL|PARTIAL)`、`ANALYTIC`、`UNION`、`LIMIT`、`DECODE`、`CTEAnchor/CTEProduce/CTEConsume`；字段 `Estimates: {row, cpu, memory, network, cost}`（未知为 `?`）、`partitionRatio: a/b`、`tabletRatio: c/d`、`predicate`、`limit`、`MaterializedView: true`。`SCHEMA-SCAN` 与 `TABLE FUNCTION`（`generate_series(1, 1e9)` 估算 1 行）的估算无意义；MV 改写让计划对象不同于 SQL 对象。没有单位说明，不能把 cost/cpu 换算为时间或资源。
- **大小：** 25 个样本计划 4–25 行、163–1,634 字节；200 分支 UNION 为 1,203 行 / 69,580 字节，超过默认 `max_plan_lines=500`，按截断处理。
- **零执行：** FE `query_explain_level=ANALYZE` 时，对 `assert_true` 必失败、`sleep(3)`、百万行自连接、LIMIT 大扫描的 `EXPLAIN LOGICAL` 只返回计划（审计 `EOF`、`scanRows=0`、毫秒级），GROUP BY 漏列仍为分析错误；`SHOW PROFILELIST` 前后均为 4。阳性对照裸 `EXPLAIN`：`assert_true` 在 BE 失败，计数查询审计 `scanRows=1,000,000`，Profile 增至 6。之后恢复 NORMAL。

### 9.7 资源限额、取消与多库审计（§2.4、§2.7，Q2）

- **会话限额：** `query_mem_limit=1048576` 回读生效后大 JOIN 为 1064 内存超限（71 ms），消息模板 `Memory of Query… exceed limit`；`query_timeout=1` 为 5024（约 1.0 s）。
- **资源组（极低阈值，只施加于合成负载）：** `CREATE RESOURCE GROUP rg_p25_ro TO (user='xw_p25_ro') WITH (cpu_weight=1, mem_limit=0.2, concurrency_limit=1, big_query_scan_rows_limit=100000, big_query_cpu_second_limit=2)` 按账号匹配：小查询成功；百万行扫描在运行中以 `exceed big query scan_rows limit: current is 125021 but limit is 100000` 失败（88 ms）；CPU 超限以 `exceed big query cpu limit` 失败；第二条并发查询**立即拒绝**（`Exceed concurrency limit`，不排队）；均为 1064。审计 `resourceGroup` 列记录命中的组。
- **资源组内存限制（审查 B3 补证，与会话限额分开）：** 会话 `query_mem_limit=0` 且回读为 0，`EXPLAIN VERBOSE` 首行确认命中目标组后：
  - `big_query_mem_limit=1048576`：百万行按 id 聚合失败，消息为 `Memory of Group=rg_r3_mem, Query… exceed limit`；小查询成功。
  - 组池 `mem_limit=0.0001`（BE `MemLimit` 2.294 GB 的 0.01%，约 221,700 字节）：聚合与小查询都失败，消息为 `Memory of rg_r3_pool exceed limit … Limit: 221700`。
  - 无资源组时同一聚合成功。

  三种内存限制的消息模板可区分，均为 1064，且都含 BE 地址。
- **只读账号能否证明资源组绑定（审查 B3 修正）：** 只能观察**某一条语句**在**观察时刻**命中的组，不能证明后续查询的绑定。默认权限下，`EXPLAIN VERBOSE <SQL>` 首行给出该 SQL 当前命中的组；`SHOW RESOURCE GROUPS` 可读全部组的阈值与分类器（含其他账号的）。反例（同一账号、同一会话）：分类器 `user`、`user + db='r1'`、`user + plan_cpu_cost_range='[1000000, 1e12)'` 并存时，便宜查询命中 `rg_r3_user`，高代价聚合命中 `rg_r3_cost`，`r1` 视图查询命中 `rg_r3_db`。DBA 随后 `ALTER RESOURCE GROUP rg_r3_user DROP ALL`，便宜查询改为 `default_wg`，其他两条不变。所以启动时或按周期的一次核对，不能覆盖不同库、不同计划代价的查询，也不能覆盖核对之后的分类器变更。当时讨论的逐条 `EXPLAIN VERBOSE` 核对也存在“核对到执行”之间被 DBA 改动的窗口，并且多一次数据库往返；该方案未实施，随原 Task 4 取消。资源组绑定、生产阈值和规则变更后的复核按 ARCHITECTURE §6 R5 纳入 P3 验收。
- **取消：** 一条单独运行 8.1 s 的 JOIN，在 2 s 时客户端断开，1 s 内从 FE `SHOW PROC '/current_queries'` 消失。只观察到 FE 视图，未观察 BE 片段或半开 TCP；`query_timeout` 仍是远端最终期限。
- **多库审计：** `db` 列是语句执行时的会话当前库（连接默认库，`USE` 后改变），不是 SQL 引用的库；无默认库时为 `''`，此时未限定表名执行为 1046，因此成功的 `''` 记录不会依赖未限定名。Db=A 引用 B、A/B 跨库 JOIN、同名表按各自 Db 解析均与执行一致；原文按 `max_stmt_length` 截断到恰好 1,000 字节（Task 6 的 `max_sql_bytes ≤ stmt_limit - 4` 规则继续适用）。失败语句 `state=ERR`，分析错误 `errorCode=ANALYSIS_ERR`，权限错误 `INTERNAL_ERR`；`EXPLAIN` 语句也以 `isQuery=1` 进入审计。本实验两小时窗口内 `isQuery=1` 的记录多数 `db=''`：去掉库条件后候选来自所有库与无默认库会话，必须继续由候选行数/字节上限约束，候选耗尽时明确标记（§2.7 设计成立）。

### 9.8 现有代码缺陷（只记录，不在 Task 0 修改）

下文保留 Task 0 发现时的事实。D1/D2 后续独立修复（PR #35，基于 PR #34 合入的 `d497a34`）已在 `81cf4cc16981e74f2b2307f12127ff8d66309f83` 通过独立审查；实施证据见 [AGENT_HANDOFF](../../../AGENT_HANDOFF.md) 的“D1/D2 修复”。固定 SQL 模式的当前契约只在 [ARCHITECTURE §6](../../../ARCHITECTURE.md#6-只读查询保护) 维护。新增真实证据已确认：内层重名、外层唯一的 CTE/子查询在旧代码上可经治理与 Evidence 交付错误结果，修复后与原 SQL 一致。

- **D1 输出名重复时，ORDER BY 序号被改写到同名的另一列（`src/xiaowei/sqlguard.py`，当前主线即有）。**
  - **已验证（`guard_readonly_query` / `guard_explain_query` 产物，加只读账号绕过 Adapter 直接执行规范化 SQL）：** `SELECT t.id AS x, t.val AS x FROM t_sem t ORDER BY 1` 两种产物都改写为 `ORDER BY t.val`（序号 1 应为 `t.id`）。以 `max_rows=2`（LIMIT 3）执行，原 SQL 返回 `[(1,3),(2,None),(3,1)]`，规范化 SQL 返回 `[(2,None),(3,1),(5,2)]`。`… a.id, b.id … ORDER BY 1, 2` 改写为 `ORDER BY b.id, b.id`；`… GROUP BY 1, 2 ORDER BY 3 DESC, 1` 改写为 `ORDER BY COUNT(*) DESC, t.val`（序号 1 应为 `t.grp`），这两例在该数据上恰好返回相同行。
  - **不受影响（已验证）：** GROUP BY 序号按位置解析正确；输出名唯一时 ORDER BY 序号正确。
  - **经产品路径的实际影响（复审以真实 SQLGuard + 真实 Adapter + recording 连接核对；源码一致）：** 上一条的执行结果绕过了 Adapter，不代表工具会交付这些行。对上述顶层输出名重复的样例：
    - 改写错误的 SQL **会发送到数据库**。
    - Adapter 在 `execute` 返回列头后发现列名重复，按 `result_contract` 拒绝，**业务行读取数为 0**（`src/xiaowei/starrocks.py` `_query` 的重复列名检查；既有回归 `test_duplicate_column_names_are_rejected_before_reading_rows`）。
    - 治理层把 Adapter 异常转为 `ToolExecutionError` 并停止本轮，不进入成功 Evidence 的记录路径（`src/xiaowei/governance.py`）。
    - 唯一列名的对照正常返回结果。
    - 诊断路径 `explain_query` 发送的是序号已被改写的 `EXPLAIN LOGICAL`：计划只有一列，不触发重复列名检查，因此分析的是错误的 SQL。
  - **Task 0 当时未验证（后续 D1 修复已补证，见本节开头）：** 内层（CTE/子查询）输出名重复而外层输出唯一时，是否会产生可交付的错误排序结果。不涉及权限越界或数据范围扩大。
  - **最小修复：** 输出名重复时拒绝 ORDER BY 序号（或要求唯一的显式别名，§2.3 已有此方向），或按位置而非名字解析；回归用例覆盖查询与执行计划两种产物、GROUP BY 对照和唯一名对照。
- **D2 `||` 被改写为 OR，且 Adapter 未固定 `sql_mode`。** 现有 SQLGuard 接受 `s || 'x'` 并输出 `s OR 'x'`。4.1.4 默认 `sql_mode=ONLY_FULL_GROUP_BY` 时 `||` 本就是 OR，结果相同；目标全局 `sql_mode` 含 `PIPES_AS_CONCAT` 时，用户的拼接被改为逻辑或（实测 `'ax'` 变 NULL）。影响：结果语义取决于目标未受控的服务端设置。最小修复：Adapter 在现有会话设置中固定并回读 `sql_mode`，或 SQLGuard 拒绝 `||`；补回归用例。

两项都不放宽权限。**已决定（2026-10-03 审查轮）：** D1/D2 的产品代码修复另开小 PR，排在 Task 3 之前，不在 Task 0 文档 PR 中修改。

### 9.9 对计划的影响与当前范围

| 计划条款 | Task 0 结论 | 当前处理 | 依赖状态 |
| --- | --- | --- | --- |
| §2.2 权限探测 | 成立 | 写明列级授权不可用（只到表/视图）；角色须是默认/已激活角色，否则账号视为无权 | Task 1 不受影响；Task 2 可按此实施 |
| §2.2/§2.6 对象版本 | `information_schema` 不足；视图元组不能发现间接依赖变化（B1） | 直接查询的表用 9.2 的表版本元组。依赖视图的 Evidence 在跨轮回放、历史读取与重发时一律按确定失效拒绝。本轮新取得的视图证据，其保存与首次交付照常按当前权限验证，不提前判失效；Task 5 必须区分这两类路径。视图元组只作直接定义变化的附加检测 | Task 2/5 的表版本部分按此实施；**视图历史回放保持关闭**，重开需另行设计并复审 |
| §2.3 规范化 | 多数一致，有 9.4 差异 | 列名按快照不区分大小写解析（库/表/别名保持敏感）；无别名函数列与重复名按“要求显式别名”处理；`DIV`、`||` 保持拒绝或固定语义；qualify schema 只含被引用对象 | Task 3 |
| §2.4 原风险评估 | 估算不等于扫描量，分区/tablet 比例不是行数比例；元数据新鲜度不能靠等待一个上报周期证明（B2） | 历史反例保留；最大 a 个分区行数之和不能直接作为放行证明。查询执行与资源保护按 ARCHITECTURE §6 R4–R5 | **原 Task 4 已取消**，不再阻塞后续任务 |
| §2.5 原可恢复错误 | §9.5 记录错误码与消息模板，同码可能处于不同执行阶段 | 白名单方案随 Task 4 取消；沿用 I/O 前可修正拒绝与执行异常停止 | 不新增恢复实现 |
| R5 资源 | 只读账号只能观察单条语句当下命中的组（B3）；组内存限制已独立命中 | 不把一次核对当作后续绑定证明；由 DBA 核实目标规则，正常/超限 SQL 验收并记录变更后的复核要求，不增加逐条 EXPLAIN VERBOSE | Task 8 核对既有回归；P3 验证实际目标 |
| §2.7 审计 | 设计成立 | 无 | Task 6 |

Task 0 已经独立审查并随 PR #32 合入，历史实测证据保留。原 Task 4 取消后不再构成后续切片依赖；其他当前权限、容量和对象版本要求继续适用。

**未覆盖：** 只验证单 FE/BE allin1 容器；没有 TLS 目标、存算分离、外表/外部 catalog、生产规模数据与并发；`statistic_collect_interval_sec` 调度对第二次导入的完整周期未观察完；BE 侧在取消后的残留与半开 TCP 未观察；`SHOW CREATE VIEW` 在千视图规模下的耗时未测；视图依赖链的递归核对未设计也未测（本阶段关闭视图历史回放）；BE 统计上报超过一个周期仍未到达（上报失败）时的行为未测；结构刷新与整轮权限复核没有端到端计时；MV 布局取值与目标版本差异继续由 P3 复核。
