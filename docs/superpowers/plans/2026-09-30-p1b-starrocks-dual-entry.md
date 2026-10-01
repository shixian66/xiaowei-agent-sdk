# P1-B 真实只读查询与双入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Keep the tasks serial unless the reviewed result of the preceding task is already fixed at an exact SHA.

**Goal:** 在 P1-A 运行核心上接入一个受限 StarRocks 只读目标、最小 Web 与飞书单聊入口，并把新产品切换到唯一正式启动入口，完成 P1 的代码与实战退出条件。

**Architecture:** OpenAI Agents SDK 继续独占 Agent Loop。渠道适配器只建立可信身份与本轮用途，共享服务负责请求去重、Session 选择与结果状态；现有 `Application` 负责 SDK Runner，现有 `GovernedTools` 在 I/O 前复核授权，SQLGuard 与 StarRocks Adapter 只承担受限查询；现有 `EvidenceStore` 生成渠道事实并在首次发送、读取和重发前复核。应用保持单 Agent、单进程、单 StarRocks 目标，不增加 Worker、任务平台、业务 MCP Server 或第二套聊天历史。

**Tech Stack:** Python 3.11、OpenAI Agents SDK 0.22.x、Pydantic 2、SQLAlchemy / asyncpg、PostgreSQL、sqlglot 30.17.0、FastAPI / Uvicorn；StarRocks 异步 MySQL 驱动候选为 `asyncmy` 0.2.15，飞书候选为官方 `lark-channel-sdk` 1.4.0，均须在对应任务安装验证后才写入最终锁文件。

**Authoritative sources:** 产品范围、权限和数据边界只在 [ARCHITECTURE.md](../../../ARCHITECTURE.md) 第 3、5–11 节维护；阶段退出条件只在 [DEVELOPMENT_PLAN.md](../../../DEVELOPMENT_PLAN.md) 维护；当前事实与证据只在 [AGENT_HANDOFF.md](../../../AGENT_HANDOFF.md) 维护。本文只维护 P1-B 的实现顺序、跨边界接口、失败语义和逐片验收，不复制普通内部实现。

**Baselines:** P1-A Task 1–5 的代码基线是 `148abaa4729ac6644c03056cf265a9dc18f1acd8`；本计划修订所依据的主线与文档基线是 `b0ae2740cba20dd08d4c63fc58281be23e8e042a`。两者之间只有 P1-A 收尾与协作文档变更。Task 复选框表示未来实施工作；第 8 节只记录本文的计划自审。

**External contracts checked for this plan:** StarRocks 通过 MySQL 协议接入，查询期限和内存限制使用其受支持的会话变量；以 [StarRocks MySQL 接入说明](https://docs.starrocks.io/docs/integrations/airflow/) 与 [System variables](https://docs.starrocks.io/docs/sql-reference/System_variable/) 为准。SDK 敏感日志开关以 [OpenAI Agents SDK configuration](https://openai.github.io/openai-agents-python/config/) 为准。飞书包迁移和异步生命周期以官方 [Channel SDK 说明](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/doc/channel.md) 为准；具体版本仍按 Task 7 安装结果锁定。

## Global Constraints

- **OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。** 不解析模型文本自行调用工具，不建立 Planner、Resolver、工作流或第二套模型循环。
- 只实现 P1-B：`list_tables`、`describe_table`、`run_readonly_query`、Web、飞书、请求结果记录、维护入口和正式启动。`explain_query`、Query Profile 属于 P2；正式 Compose、备份恢复实战和用户 UAT 属于 P3。
- 复用 `src/xiaowei/`。生产代码不得导入旧 `src/xiaowei_agent/`；旧 SQLGuard、Adapter、TaskStore 和 Worker 只能作为攻击样例参考，不能成为新运行路径或兼容目标。
- 本轮用途由可信入口逐条生成，默认 `diagnose`。`diagnose` 只展示 `list_tables`、`describe_table`；明确选择 `query` 且当前授权允许时才额外展示 `run_readonly_query`。上一轮查询权、模型意图和历史文字都不能提升本轮 Tool Scope。
- SQLGuard、目标/对象/列/函数授权、工具预算必须在连接池获取或任何 StarRocks I/O 前完成。只读数据库账号和服务端资源限额是独立防线，不能替代前置校验。
- 原始结果只在 Adapter 的有界内存中存在。模型、Session、Web 和飞书继续使用现有 `ToolPolicy` 的独立字段与容量；Evidence 不新增原始结果仓库。
- 同一会话仍只运行一轮。失败、取消、超时、重投、投递失败和进程重启均不得自动重放 Agent、模型请求或数据库查询。
- PostgreSQL 初始化、v1→v2 升级和清理只能由显式命令触发；普通启动与请求路径不自动建表、升级或降级。启动可以做必要的一致性恢复，但不得自动执行旧请求。
- 所有外部依赖使用公开 API、精确锁版和有界关闭路径。锁版验证失败时停止该任务并修订本文，不能用私有 API、monkey patch、旧 SDK 或宽松配置绕过。
- 每个任务形成一个可独立审查和回退的提交。权限、SQL、数据、Session、渠道身份及运行入口任务都要针对候选提交的精确 SHA 做独立审查；合并、部署、真实数据调用和归档另行授权。

## Review Focus

1. **SQL 与零 I/O：** 任一多语句、写语句、导出、锁、变量、越权对象/列/函数、无法解析或未支持 AST 都在连接获取前拒绝；拒绝用例的 recording pool 与 Adapter 调用次数必须为 0。
2. **双存储一致性：** SDK Session 已提交而请求结果未保存时，相关会话必须关闭；重启恢复不能把中断轮次拼接进新轮，也不能重跑查询补偿。
3. **重投与重发：** 重复请求只读取已保存的 `AgentAnswer` 并按当前身份、权限、Evidence、过期和渠道策略重新生成 `Delivery`；未知投递状态不自动再次发送。
4. **渠道身份：** 浏览器不能选择 subject/session，飞书消息不能选择租户、发送者或 chat；跨身份、跨会话和跨渠道读取返回相同的受控拒绝。
5. **数据投影：** Web 结构化表格由代码从当前 Web 投影生成；飞书投影包含同一获准有限行，由代码渲染为纯文本摘要/分段。模型文字不能重写实际 SQL、数值、来源、采集时间或截断状态。
6. **资源和取消：** 客户端取消不等于 StarRocks 已停止；查询端期限、客户端期限和整轮期限都要存在。结果未知时丢弃连接且不重试。
7. **日志与 SDK 默认值：** SDK 模型/工具数据日志开关必须在任何 `agents` 导入前被产品启动入口强制关闭；不能依赖环境默认值。日志只含请求编号、阶段、安全错误码、耗时和可取得的 usage。
8. **正式入口：** wheel 和 `xiaowei` 命令只指向新包；旧包源码可暂留 Git 工作树作为历史，但不能继续进入 wheel、主命令或产品运行依赖。

## 1. 最小方案与关键调用链

### 1.1 直接复用

| 现有模块 | P1-B 用法 | 只增加的边界 |
| --- | --- | --- |
| `model_api.py` / `ModelBinding` | 继续装配单一可信模型 Profile | 不新增模型路由；Gate 0 先用一个获准 Profile 证明真实工具续轮与 Session 追问 |
| `app.py` / `Application` | 继续负责每轮 Agent、Runner、并发和 Session 提交 | `run_turn` 在提交前校验，提交后返回已校验的 `AgentAnswer`；不负责请求存储或渠道投递 |
| `governance.py` / `GovernedTools` | 继续完成工具目录、参数、目标、当前授权、预算与结果契约 | 登记三个 StarRocks 工具；增加一个同步、策略绑定、预算预留前的查询 precheck |
| `tools.py` | 继续把本地执行函数包装为 SDK function tools | 不增加另一路工具包装 |
| `evidence.py` / `EvidenceStore` | 继续记录四类投影并校验最终引用 | 从当前 Web 投影生成受信结构化事实；重发仍调用同一验证器 |
| `session.py` / `PolicySession` | 继续委托 SDK `SQLAlchemySession`，负责过滤、提交、回放和状态隔离 | 在本模块增加显式关闭与维护清理能力；不读取或修改 SDK 私有表结构 |
| `storage.py` / PostgreSQL v1 | 继续管理引擎、SDK 表和应用表版本 | 增加显式 v1→v2 迁移、请求/渠道映射表和一致性恢复 |
| `mcp.py` | 保留可选外部 MCP 能力 | P1-B 不改变协议或建设业务 MCP Server；故障只移除对应远端工具 |
| FastAPI、Uvicorn、sqlglot | 使用已锁定的现有依赖 | FastAPI 提供同源最小 Web；sqlglot 负责 StarRocks 方言 AST |

不复用旧 `xiaowei_agent` 的 SQLGuard、Runtime、TaskStore、Worker、API 或 CLI。它们绑定旧的确定性执行链和旧 DTO；提取它们会形成第二套权威与兼容层。可以把旧 SQL 安全用例中的攻击类别转换成新 `tests/p1b/` 的输入样例，但不能导入旧实现。

### 1.2 调用链

```text
Web / Feishu event
  -> channel adapter（协议、可信身份、消息类型、显式用途）
  -> ChannelService（请求去重、会话映射、当前 AccessPolicy、状态）
  -> Application.scope_for_turn + Application.run_turn
  -> SDK Agent / Runner
  -> governed function tool
  -> GovernedTools（目录/范围/参数/最新权限）
  -> SQLGuard precheck（仅 run_readonly_query；同步、纯函数、零 I/O）
  -> GovernedTools（预算预留）
  -> StarRocksAdapter（有界连接、期限、读取与规范化）
  -> EvidenceStore（四种投影、归属、保留期）
  -> Application（最终回答预校验）
  -> PolicySession（提交 SDK 历史）
  -> RequestStore（保存可重发 AgentAnswer 与结果状态）
  -> EvidenceStore.validate_answer（按当前渠道重新生成 Delivery）
  -> Web JSON/DOM 或 Feishu 单次发送
```

`AccessPolicy` 是启动时装配的一个可信对象：其授权方法同时传给 `EvidenceStore`，其本轮授权集合供 `ChannelService` 计算 Tool Scope。不能分别实现一套“入口权限”和“Evidence 权限”。

## 2. 跨边界契约

### 2.1 可信入口、用途与应用结果

只固定下列跨模块类型；HTTP、SDK 回调和 SQLAlchemy 的内部函数签名留给实施任务决定。

- `InboundRequest(channel, channel_request_id, subject_id, conversation_id, mode, message, received_at)` 为不可变、禁止额外字段的可信信封。身份、会话语境和用途来自渠道适配器；`message` 仍是不可信用户内容，必须通过现有输入策略。HTTP body 和消息正文不能直接提交 `subject_id`、`conversation_id`、`session_id`、`target_scope` 或 `tool_scope`。
- `mode` 只有 `query | diagnose`。Web 每次显式提交；飞书只解析消息首部的 `/查询`、`/诊断`，普通文本固定为 `diagnose`。命令剥离后的空消息拒绝。`/新建` 在渠道层处理，不进入模型。
- `AccessPolicy.resolve(inbound) -> AccessDecision` 至少固定内部 subject、唯一目标范围、当前允许工具和渠道数据策略；任何异常都按拒绝处理。首版只有一个配置的 StarRocks `target_id`。
- `ChannelService.accept(inbound) -> RequestReceipt` 负责持久去重、会话绑定与有界接收；`ChannelService.process(receipt)` 才进入 `Application`。Web 可在同一进程等待完成，飞书先持久接受再放入有界内存队列。
- `ChannelService` 持有覆盖整条业务提交窗口的进程内会话互斥：进入 `Application.run_turn` 前取得，直到请求的 completed 或 failed 状态成功持久化后才释放。`Application` 的 `_active` 继续作为 Runner 边界内的第二道防线；若结果状态无法持久化，会话锁不开放给新轮次，同时把 readiness 锁为 false。
- `Application.run_turn(ctx, message) -> AgentAnswer` 在提交前调用现有 `EvidenceStore.validate_answer`；校验成功后提交 Session，并返回该已校验回答。提交后的权限变化由渠道交付阶段再次检查，`Application` 不记录“已交付”。
- `ChannelService` 严格按“保存受限 `AgentAnswer` 与 completed 状态 → 调用共享 `EvidenceStore.validate_answer(answer, ctx)` 生成 `Delivery` → 渠道发送”的顺序处理。completed 重读与显式重发直接调用同一 `EvidenceStore`，不装配 `ModelBinding`、不解析模型凭据，也不调用 Runner。
- 不增加 `TurnOutcome`、`Application.revalidate` 或 `Application.close_session`。会话关闭由 `session.py` 的公共操作负责：状态置为不可回放，不删除历史，也不补偿执行。

请求正文只在当前运行内传给 SDK；请求表保存带服务端密钥的正文摘要用于判定“同一请求编号是否真是同一内容”，不保存正文。摘要还绑定 channel、subject、conversation、mode 与数据策略版本，避免请求编号被不同语义复用。

### 2.2 StarRocks 工具与 SQLGuard

首版只登记以下工具 ID：

| Tool ID | 模型参数 | 受信执行 |
| --- | --- | --- |
| `local/list_tables` | 无参数（严格空对象） | 代码生成的 `information_schema` 查询，只返回配置中允许的表/视图 |
| `local/describe_table` | 受限对象名 | 对象先按配置解析，再执行代码生成、绑定参数的元数据查询 |
| `local/run_readonly_query` | 一条 SQL 文本 | 先经 `guard_readonly_query`，再只执行其返回的规范化 SQL |

`diagnose` 展示 `list_tables`、`describe_table`，用于补齐对象和字段信息；`query` 展示三个工具。`run_readonly_query` 在诊断轮必须不可见，模型强行调用仍由治理层拒绝并保持 Adapter 零 I/O。元数据工具不接受 catalog、主机、凭据、文件路径、过滤条件或任意查询参数；它们同样经过 `GovernedTools` 的目标、授权和预算检查。现有工具目录要求参数全部必填，因此 `list_tables` 不保留“可选过滤”参数。

跨边界接口固定为：

- `QueryPolicy(target_id, default_database, allowed_objects, allowed_columns, allowed_functions, max_rows, max_sql_bytes)`：来自可信静态配置，配置项均为显式 allowlist；没有列/函数清单的对象不开放。
- `GuardedQuery(target_id, normalized_sql, referenced_objects, referenced_columns, max_returned_rows)`：只由 SQLGuard 构造，不接受外部直接实例化为执行凭据。
- `guard_readonly_query(sql, policy) -> GuardedQuery`：同步纯函数；不查询元数据、不取连接、不访问网络或凭据。拒绝只返回稳定的 `QueryRejectionCode`，至少区分输入过大、无法解析、多语句、不支持语法、投影星号、对象/列/函数未获准、歧义引用和 LIMIT 不支持；原因码及固定说明不得包含 SQL、标识符、字面量或解析器原文。
- `GovernedTools` 为 `run_readonly_query` 绑定上述同步 precheck：目录/范围/参数规范化和最新授权通过后，先把规范化 `ToolRequest` 与可信 `QueryPolicy` 转为 `GuardedQuery`，再预留工具预算。Adapter 只收到 `GuardedQuery`；Evidence 仍绑定同一个规范化 `ToolRequest`。precheck 的 `ToolRejectedError` 把安全原因码与固定说明作为“未执行”返回模型，本次调用不占工具调用预算，模型可据此在整轮 `max_turns` 内修正；precheck 通过后的执行、结果或 Evidence 失败仍按 `ToolExecutionError` 中止，不退还预算、不自动重试。

SQLGuard 的最小允许集如下，未列出的语法默认拒绝：

1. 输入必须在字节上限内且恰好解析为一个 StarRocks 方言语句；根节点为 `SELECT`，或带 `WITH` 且最终主体为 `SELECT`。P1-B 不支持集合运算、递归 CTE、相关子查询、窗口、用户变量、参数占位符、锁、`INTO` / `OUTFILE`、hint、注释、外部/表函数和 catalog 切换。
2. 遍历所有嵌套 scope、CTE、子查询和 JOIN 的物理表引用。CTE 名不能误判为物理表，物理表必须解析到唯一的 `default_database.object`，随后在实际 SQL 中完整限定；显式其他 database/catalog 拒绝。
3. 输出、过滤、JOIN、聚合、排序和子查询中的每个物理列都必须属于对应对象 allowlist；歧义的未限定列拒绝。输出别名、CTE 列名和物理列先按所在 scope 区分，不能把别名误当成已授权物理列。投影中的 `*` 和 `table.*` 拒绝；只有函数 allowlist 明确包含 `COUNT` 时允许 `COUNT(*)`。
4. 每个函数节点都必须在大小写规范化后的 allowlist 中；未知 AST 类型、无法归属的列、未支持方言扩展和解析警告均失败关闭。
5. 顶层 `LIMIT` 只接受非负整数字面量，不允许 `OFFSET`。缺少或超过上限时改为 `max_rows + 1`，用于发现截断；较小合法值保留。交付展示 SQLGuard 最终生成并实际执行的规范化 SQL。

标识符按 StarRocks/sqlglot 实际语义统一处理大小写、反引号和 Unicode 后再匹配 allowlist，不能以表示差异绕过。函数闭集按 AST 节点类型检查，明确覆盖 sqlglot 的专用节点（至少 `Cast`、`If`、`Anonymous`），不能只读通用函数名。列检查覆盖 SELECT、WHERE、JOIN、GROUP BY、HAVING、ORDER BY 及所有子查询，并区分 ORDER BY 输出别名。规范化结果必须往返幂等；`WITH` 查询的 LIMIT 改写落在最终查询主体。

P2 的 EXPLAIN 诊断需要分析用户任意 SQL 时，另行设计“只解析与范围校验、绝不执行”的策略；不得通过放宽 P1-B 查询闭集来复用本 SQLGuard。

规划时已用基线锁文件中的 sqlglot 30.17.0 做只读探针：`starrocks` dialect 可解析普通 `SELECT` 与 `WITH ... SELECT`，并拒绝探针中的 `INTO OUTFILE`；这只证明候选解析入口存在，不代替 Task 1 的闭集和变异验证。

### 2.3 StarRocks Adapter、结果与 Evidence

`StarRocksTarget` 是启动时可信配置，至少包含 target ID、固定 FE 地址/端口、默认 database、TLS 设置、只读用户名与密码引用、连接/查询期限、池大小、时区、查询内存上限、结果行/总字节/单值字节上限，以及对象/列/函数 allowlist。凭据在 Adapter 装配时解析，不进入 Pydantic dump、RunContext、Session、日志或异常。

Adapter 只接受代码生成的元数据请求或 `GuardedQuery`。连接池获取有明确期限；每次 checkout 先用可信值设置 StarRocks 会话 `query_timeout`、`query_mem_limit` 和时区，再回读并核对实际生效值；任一设置或核对失败则废弃连接且不执行查询。TLS 开启时必须验证证书与主机。查询使用驱动公开的非缓冲流式读取能力，最多探测 `max_rows + 1`，并同时执行累计字节和单值字节限制，不预设驱动一定实现服务端游标。不得先把全部结果载入内存再裁剪。

Task 2 实施修订（锁定 asyncmy 0.2.15 后核对）：asyncmy 自带连接池没有获取期限，且按 `connected` 决定回收，`close()` 后的连接仍可能回到空闲队列；因此 Adapter 不复用连接，`pool_size` 是并发槽位，每次请求新建连接并设置、回读会话变量，完整读完才 `QUIT`，其余情况直接断开。等待槽位与建立连接共用连接期限。StarRocks 4.1.4 实测错误码 5203（拒绝访问）映射为权限拒绝，5024（超过 `query_timeout`）单列为服务端超时。

可进入 `ToolObservation` 的值只允许 `null | bool | int | finite float | string`：`Decimal` 用十进制字符串，日期/时间按目标时区输出 ISO 8601；bytes、非有限浮点和未知驱动类型拒绝。列名重复拒绝，避免映射覆盖。下一行或单值超过容量时不保留该行的部分值，停止读取并标记 `truncated=true`。返回字段至少包含实际 SQL、列、已返回行、已返回行数、截断、target、采集时间和耗时；不声称已知数据库中的总行数。

连接/认证/语法/权限/超时/结果契约错误映射成固定安全错误码，不带地址、账号、SQL 字面量或服务端原始文本。截断、超时、取消、网络中断、驱动状态不明或仍有未读取结果时废弃连接，不回池；明确说明服务端可能继续到服务端超时，不自动重试。

每个工具继续用现有 `ToolPolicy` 明确四种投影。P1-B 不扩展当前按顶层字段整体取舍的投影器：

- `QueryPolicy.allowed_columns` 是模型、Session、Web 与飞书共同允许的列交集；P1-B 不支持只给某一渠道的列。Adapter 只产生一组共同获准的有限行，Web 与飞书都不得得到比模型/Session 更多的列或行。
- 查询结果的 model、session、Web、Feishu 四种投影都必须包含同一个完整 `rows` 字段及该工具声明的其他模型必需字段。分别按 Web 会话与飞书会话计算“model ∩ session ∩ 当前渠道”的字段和最小容量，两条路径都必须保留完整 `rows`。
- Adapter 的行数、结果总字节和单值上限必须同时容纳于上述两条模型可达投影。启动检查使用与运行时相同的 JSON 编码和 UTF-8 字节计数，以声明上限构造最坏情况：覆盖控制字符的 `\u00XX` 转义、引号与反斜杠转义、`max_sql_bytes`、全部允许列名、所有模型投影字段、元数据及 Evidence 信封开销；不能用数据库原始值字节数代替序列化后大小。
- 运行时 `EvidenceStore.record` 仍做最后兜底：查询结果在任一渠道投影或该渠道的模型可达投影中整体省略 `rows` 时，直接以 `EvidenceStoreError` 中止本轮，不保存 Evidence、不把只有列名且 `truncated=false` 的结果交给模型。该检查防止配置、字段或编码规则变化后静默丢数据。
- 各受众仍可有不同的非必需顶层字段与总字节上限。Web 把共同有限行显示为结构化表格；“飞书摘要”只描述纯文本渲染、按消息长度分段或标注展示截断，不缩小模型与飞书获准接收的数据范围。具体共同列和容量由首个环境的数据政策决定，不在代码中给任意表默认放行。
- 增加受信 `DeliveryFact(evidence_id, tool_id, columns, rows, metadata)` 和 `Delivery.facts`。它只能由 `EvidenceStore` 从当前 Web 投影生成，值域仍是上述 JSON scalar；模型不能提交它。飞书不发送结构化 `facts`，但代码使用含同一组 `rows` 的飞书投影生成纯文本结果与截断说明。
- 请求结果只保存已通过最终验证、受字节限制的 `AgentAnswer` JSON 与 Evidence 引用；不保存一次性渲染后的 Web 表格或飞书文本。首次发送、GET 和显式重发都重新生成 `Delivery`。

若真实需求要求按受众裁剪行或提供 Web 专属列，先单独修订计划，扩展结构化投影规则并升级 `_PROJECTION_RULE`；不在 P1-B 当前实现中预建。

首个目标的可信 `BusinessContext(target_id, version, text)` 由配置装配进 Agent instructions，只解释字段含义、时区、单位和必要过滤条件，不扩大工具或数据权限。其规范化指纹加入现有会话绑定指纹；版本或内容变化后旧会话拒绝继续，要求新建会话。

### 2.4 PostgreSQL v2、去重与会话一致性

保留 `001_initial.sql`，新增单向 `002_p1b_channels.sql`；应用 schema 版本升为 2。

- 全新 `storage init` 在同一受控流程中顺序执行 001、002；现有 v1 只能由显式 `storage upgrade` 在 PostgreSQL advisory lock 和事务中升级到 v2。
- 普通 `initialize_storage`、应用启动与请求路径遇到 v1 或未知版本必须拒绝，不自动升级。v2 程序拒绝 v1；旧 v1 程序也会因版本不符拒绝 v2。
- v2 只增加两类应用状态：渠道会话映射，以及请求/可重发结果记录。继续复用现有 `xiaowei_session` 与 `xiaowei_evidence`，不创建聊天消息表、lease、任务队列、事件流或 Worker 表。

渠道会话映射至少保存 channel、内部 subject、受限 conversation/destination 引用、当前 session ID、generation、状态、创建/过期时间及 Web 凭证摘要。Web 只持有随机不可猜的 cookie，数据库只存其带服务端密钥摘要；飞书内部 subject/conversation 从 app、tenant、sender、chat 的可信事件字段生成。客户端不能提交任意 session ID。

请求表以 `(channel, channel_request_key)` 唯一；`channel_request_key` 是 channel、可信 owner/conversation 与原始请求 ID 的带密钥摘要，不让不同 owner 的同名客户端 ID 相互冲突。记录至少保存 owner/session/turn、mode、带密钥的消息语义摘要、`accepted | running | completed | failed | interrupted` 状态、受限 `AgentAnswer` JSON、安全失败码、`pending | sending | sent | failed | unknown` 投递状态、时间与过期时间。不得保存原消息、原始数据库结果、原始异常或凭据。可重发结果保留期不得长于其 Evidence 保留期，启动时校验。

去重与恢复语义固定如下：

| 情况 | 行为 |
| --- | --- |
| 同 key、同语义、`running` | 返回“处理中”；模型和工具调用均为 0 |
| 同 key、同语义、`completed` | 不运行 Agent；按当前 context 重验保存的 `AgentAnswer`。Web 由 GET 读取；飞书已 `sent` 时不再发送 |
| 同 key、同语义、`failed/interrupted` | 返回原安全状态；不自动再跑。用户需用新请求 ID 发起新请求 |
| 同 key 但 owner、conversation、mode 或语义摘要不同 | 拒绝冲突；不泄露原记录是否属于别人 |
| Session 提交成功，结果保存失败 | 不交付；通过 `session.py` 关闭该 Session。能写请求状态时标为 failed；状态也写不了时立即把 readiness 锁为 false，拒绝所有新轮次并保持会话互斥，停止并重启 serve 后由启动恢复处理，不能继续以“处理中”服务 |
| 结果已保存，渠道发送失败或未知 | 保留 completed；只更新投递状态。不得重跑 Agent 或查询 |
| 飞书首次发送或同一事件重投 | 读取保存的 `AgentAnswer` 并复核当前身份/目标/渠道/Evidence/过期；紧邻发送前只能用数据库条件更新取得 `pending → sending`。成功改 `sent`，明确失败改 `failed`，结果不明改 `unknown`；failed/unknown/sent/sending 都不自动发送 |
| 显式 `requests resend` | 只接受当前 owner 下的 failed/unknown，复核保存的 `AgentAnswer` 后用数据库条件更新取得 `failed/unknown → sending`；只有取得者发送一次。pending 仍留给首次发送路径，sent/sending 拒绝 |
| 进程重启发现 accepted/running | 在应用表事务中关闭关联 Session 并标为 interrupted；不得放回队列。无法完成一致性恢复时 readiness 失败 |

请求状态的任一关键更新失败都会锁低进程 readiness，并拒绝新工作；该进程不尝试在线恢复。操作者停止并重启 `serve` 后，新的进程先取得 PostgreSQL 会话级 advisory lock，再执行唯一的启动一致性恢复；不能让独立 CLI 与仍运行的 serve 并发恢复。崩溃遗留的 `sending` 在启动恢复中只转为 `unknown`，随后仍需用户显式重发，绝不自动发送。

`serve` 用一条专用 PostgreSQL 连接在整个进程生命周期持有固定的 session-level advisory lock；锁已被占用时第二个实例拒绝就绪，持锁连接丢失时当前实例立即拒绝新工作并退出。启动恢复只在取得该锁后执行。`storage init/upgrade` 同样必须独占这把锁；`requests resend` 与 `storage cleanup` 不执行恢复，可按下述条件更新与在线 serve 并发。

`/新建` 或 Web 新建会话在映射表中生成新随机 session ID 并递增 generation；不复制历史或查询许可。当前 session 有 running 请求时拒绝切换。旧 session 立即不再作为当前映射，仍按保留期保存且受现有权限/过期规则约束。

维护清理按显式批次运行：每次删除都带数据库过期与状态条件，跳过 current session 及 accepted/running/sending 请求；使用 SDK 公共 Session `clear_session()` 清历史，再删对应已过期 Evidence、请求和映射，竞争导致条件不再成立时跳过，失败保守停下。`requests resend` 与 `storage cleanup` 可以作为独立进程和 `serve` 并发，但只能依赖上述条件更新取得所有权。普通启动只做中断一致性恢复，不做物理清理。

### 2.5 渠道与运行入口

**Web** 使用同源 FastAPI 与一个静态页面，不引入前端构建链、Markdown renderer 或 HTML sanitizer。最小 API 为：

- `POST /api/turns`：只接收 client request ID、`query | diagnose` 与 message；session/subject/target 均从安全 cookie 和服务端配置取得。
- `GET /api/turns/{request_id}`：只返回同一 cookie owner 的安全状态和重新验证后的 `Delivery`。
- `POST /api/sessions`：新建会话；有运行中请求时拒绝。
- `GET /healthz`：仅进程存活；`GET /readyz`：配置、PostgreSQL schema 与中断恢复均就绪，必要 StarRocks 配置可装配。不得调用付费模型或执行查询。

Web 默认只接受配置的 loopback Host/Origin，按解析后的 scheme/host/port 精确匹配，不通过 DNS 解析来放行别名；POST 必须是同源 JSON，关闭宽泛 CORS。先检查合法 `Content-Length`，并在 ASGI receive 包装中累计限制每个 chunk；缺少长度或使用 chunked 时也必须在完整 body 缓冲前停止。cookie 为随机、`HttpOnly`、`SameSite=Strict`，部署是否加 `Secure` 由 HTTPS/SSH 入口配置明确。页面只用 `textContent` 和 DOM API 渲染文字，以受信 `DeliveryFact` 建表；数据库字符串绝不传给 `innerHTML`。首版固定本机操作者，不增加账号系统。

**飞书** 候选使用官方独立 `lark-channel-sdk`。Task 7 必须先验证锁定版本的异步连接生命周期、标准化事件字段和发送返回值，再决定最终 import；不同时保留旧 channel package。只接收配置租户和用户的 p2p 文本，拒绝群聊、非文本、机器人自身、过期事件和未授权发送者，且拒绝发生在模型调用前。

飞书事件处理先持久化接受状态，再用有界 `asyncio.Queue` 和固定消费者数运行共享服务；配置必须满足 `1 <= consumer_count <= max_concurrent_turns`，Web 与飞书共同受 `Application` 的全局并发上限约束。队列只是单进程背压，不提供恢复或后台任务语义。队列满时把已持久请求标为 failed/busy，ack 事件，并通过同一投递条件更新最多发送一次固定“系统繁忙，请稍后用新消息重试”；出队后遇到全局 busy 也作同样处理。重启恢复的 interrupted 请求最多发送一次固定“上次处理已中断，请重新发送”，不进入 Agent 或工具。出站禁用 SDK 自动重试、fallback、卡片、流式和媒体；每次只做一次文本发送，将明确成功、明确失败和结果未知分别记录。消息超过渠道上限时按代码生成的安全边界分段或截断，并保留截断说明；部分分段成功按 `unknown` 处理，不能改成重新查询。

飞书回调只有在请求已持久化为可确定处理的 accepted 或终态后才 ack。持久化失败时不得返回成功；Task 7 必须用锁定 SDK 证明实际的抛错、NACK 或关闭连接能触发平台重投。若 SDK 回调无法表达失败且总会 ack，停止 Task 7 并修订接入方案。未授权、非文本等已确定的安全终态可以 ack，不进入模型。

**正式入口** 使用标准库参数解析即可，不增加 CLI 框架。`xiaowei` 指向新 `xiaowei.cli:main`，`python -m xiaowei` 由最小 `src/xiaowei/__main__.py` 转入同一入口；命令为 `serve`、`storage init`、`storage upgrade`、`storage cleanup`、`requests resend`。`src/xiaowei/__init__.py` 保持无导入副作用；`cli.py` 与 `__main__.py` 均在任何可能导入 `agents` 的链路前强制设置 `OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1` 与 `OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1`，再惰性导入运行装配，保留现有 tracing 双重关闭。两个入口都用独立子进程证明外部预置为 `0/false` 时仍不会把 canary 模型/工具数据写入日志。

## 3. 失败处理总表

| 边界 | 用户可见结果 | 状态与后续 | 必须证明的副作用 |
| --- | --- | --- | --- |
| 渠道身份、消息类型、用途或请求冲突拒绝 | 固定拒绝或不处理 | 不创建可运行轮次 | 模型、工具、StarRocks 均 0 调用 |
| SQL/对象/列/函数 precheck 拒绝 | 用户看到固定范围拒绝；模型收到枚举原因码与固定安全说明 | 作为未执行的 `ToolRejectedError`，不占工具预算；模型只能在本轮剩余 `max_turns` 内按原因修正 | 连接池 acquire、Adapter 与 SQL execute 均 0；原因不含 SQL、标识符、字面量或解析器原文 |
| StarRocks 连接、权限、超时、取消、结果契约失败 | 固定工具失败 + 请求编号 | 不生成 Evidence，不重试；状态不明时丢连接 | 不泄露原始错误或 SQL 字面量 |
| Evidence 保存/回读失败 | 本轮失败 | 不把结果交给模型；不退还预算、不重跑 | Session 不提交最终回答 |
| 模型或最终回答失败 | 固定模型/回答错误 | Session 不提交；请求 failed | 已执行工具不补跑 |
| Session 写入中断 | 提示新建会话 | Session 保持 writing/closed，不再回放 | 请求不 completed、不投递 |
| 请求结果或关键状态保存失败 | 提示保存失败并新建会话 | 关闭 Session；状态也无法写入时立即 readiness false，停止并重启 serve 后由持锁启动恢复标记 interrupted | 不投递、不接新轮、不重跑 |
| Evidence 在投递前撤权/过期 | 提示结果不可用 | completed 记录保留但 delivery failed | 不发送旧渲染结果 |
| Web 客户端断开 | GET 可查 completed；running 继续受服务端期限约束 | 不因断开再起一轮 | 同 request ID 只有一个 Runner |
| 飞书发送失败/未知 | 记录 failed/unknown 和请求编号 | 仅显式重发已保存结果 | Agent、模型、查询不重跑 |
| 飞书队列满、出队 busy 或恢复 interrupted | 固定繁忙/中断回执，failed/interrupted | 通过投递 CAS 最多发送一次；用户用新消息重试 | Agent、模型、工具均 0 调用 |
| PostgreSQL/schema/恢复不可用 | readiness 失败，拒绝新轮 | 等待修复或显式升级 | 模型、StarRocks 均 0 调用 |
| 可选 MCP 不可用 | 本轮工具集不含对应 MCP 工具 | 本地 StarRocks 仍可用 | 不伪装 MCP 工具成功 |

## 4. 按独立结果拆分的实施任务

任务按 **1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9** 串行推进；Gate 0 可与 Task 1–2 并行，但未通过时不得开始 Task 3。Task 1 SQLGuard 与 Task 2 Adapter 不依赖模型行为或 Session 保存形式，Gate 0 的结果不会使其返工（用户 2026-09-30 批准调整）。Task 6 与 Task 7 技术上都依赖 Task 5，但首轮实施仍串行，避免在共享服务契约未稳定时形成两套入口假设。

### Gate 0：真实 Profile 的工具续轮与 Session 追问

**Source:** 主线 `b0ae274` 已记录 Gemini 部分证据，并明确要求在 P1-B 实施前补完至少一个 Profile 的真实工具闭环与追问；这不是 Task 9 才检查的退出项。

**Result:** 使用一个有额度、获准的真实 Profile，以合成数据、本地合成工具、真实 `Application`、`EvidenceStore`、`PolicySession` 和隔离 PostgreSQL 完成“选择工具 → 治理执行 → 类型化 `AgentAnswer` → 交付”，随后在同一 Session 追问并证明工具不重跑。

- [ ] 固定 provider/endpoint/protocol/model、SDK 版本、Profile 指纹、合成数据范围、预算与期限；凭据只用安全引用，不进入仓库、命令输出或记录。
- [ ] Gate 0 使用专用合成工具与 `ToolPolicy`，其 model、session 和所选渠道投影都包含回答所需的全部字段（例如 `rows` 与 `total`）并满足模型可达容量；不得直接复用 handoff 已证明模型看不到 `total` 的旧 `PROJECTIONS` fixture。
- [ ] 检查真实供应商工具调用项经过 Session 保存与回放后仍保留继续对话所必需的字段。当前 `PolicySession._function_call` 只保存 `call_id/name/arguments`；若选择 Gemini 3，必须证明 `provider_data.thought_signature` 经精确字段 allowlist、类型与容量限制后保存并原样回放，禁止透传任意 `provider_data`。若因此修改 Session 保存形式，先加离线回归：篡改已保存签名必须使回放失败，超长签名必须在写入/回放前拒绝，非 allowlist 字段不得持久化或回放。
- [ ] 若闭环或回放失败，先建立一个独立的最小 P1-A 前置修复任务，加入离线回归、真实 Profile 复验和精确 SHA 独立审查；修复合入并复验通过后才进入 Task 3。
- [ ] 记录每个固定样例的结果、失败阶段、耗时和可取得的 usage，并把实际证据更新到 `AGENT_HANDOFF.md`；上游 429/503 只能记录为未通过，不能把已观察到的工具选择提升为闭环完成。

### Task 1：纯 SQLGuard 与授权闭集

**Depends on:** 无（离线纯函数，不依赖 Gate 0）。

**Result:** 给任意 SQL 与可信 `QueryPolicy`，只产生一个可执行的 `GuardedQuery`，或返回不含输入内容的枚举原因码与固定说明；全程无 I/O。

**Files:** Create `src/xiaowei/sqlguard.py`, `tests/p1b/test_p1b_sqlguard.py`（旧 `tests/security/test_sqlguard.py` 仍在，测试目录无 `__init__.py`，basename 不能重复）; optionally add shared fixtures under `tests/p1b/conftest.py` only when Task 2 consumes them.

- [ ] 先写正例：单表、显式列、JOIN、非递归 CTE、非相关子查询、允许函数、较小 LIMIT，以及缺失/过大 LIMIT 被改成 `max_rows + 1`；断言规范化 SQL、物理对象和列集合。规范化 SQL 再进一次 SQLGuard 必须完全相同，`WITH` 的 LIMIT 必须落在最终查询主体。
- [ ] 覆盖 SELECT、WHERE、JOIN、GROUP BY、HAVING、ORDER BY 与子查询中的列；单独验证 ORDER BY 输出别名。用大小写、反引号和 Unicode 等价/混淆样例验证标识符规范化与 allowlist 匹配。
- [ ] 按节点类型验证函数闭集，至少覆盖 `Cast`、`If`、`Anonymous` 及普通聚合/标量函数；不能只靠 `exp.Func` 名称放行。
- [ ] 先写关键反例：空/超长/多语句、DDL/DML、UNION、递归/相关、注释/hint、锁、变量、导出、外部/表函数、catalog/跨库、越权表/列/函数、歧义列、投影星号、非字面 LIMIT/OFFSET、未知节点与畸形方言；逐类断言稳定的 `QueryRejectionCode`、固定安全说明和 recording I/O 为 0，并证明原因不回显输入 SQL、对象名、字面量或解析器错误。
- [ ] 运行 `uv run --locked --extra dev python -m pytest tests/p1b/test_p1b_sqlguard.py -q`，确认新增用例先因缺实现失败。
- [ ] 只实现上述闭集；使用 sqlglot 公共 AST/scope 能力，不写字符串前缀判断，不自动查询数据库补列信息。
- [ ] 运行目标测试、`ruff check src/xiaowei/sqlguard.py tests/p1b/test_p1b_sqlguard.py`、`mypy src/xiaowei/sqlguard.py`；对删除关键遍历、放行未授权函数、移除 LIMIT 重写做变异检查，相关测试必须变红。
- [ ] 独立审查候选精确 SHA，重点核对嵌套 scope、CTE 影子名、函数遍历与 fail-closed；提交 `feat: guard StarRocks read-only SQL`。

### Task 2：有界 StarRocks Adapter

**Depends on:** Task 1 reviewed SHA.

**Result:** 通过 recording transport 和获准测试 StarRocks 分别证明：执行端只接受 `GuardedQuery`/代码生成元数据请求，结果读取有界，错误不泄露且没有自动重试。

**Files:** Create `src/xiaowei/starrocks.py`, `tests/p1b/test_starrocks_adapter.py`, `tests/p1b/test_starrocks_real.py`, `tests/p1b/conftest.py`（`starrocks_real` 显式开关）; Modify `pyproject.toml`, `uv.lock`, `tests/security/test_dependency_baseline.py`（批准依赖集合）.

- [ ] 在临时实验中安装候选 `asyncmy==0.2.15`，核对 Python 3.11、TLS 证书验证、连接池获取/查询期限、非缓冲流式读取、取消/关闭、字段类型和 StarRocks MySQL 协议行为；实验不接获准环境时只锁公开 API，不宣称 StarRocks 兼容。
- [ ] 先写 recording driver 测试：SQLGuard 拒绝时 pool acquire 为 0；pool acquire 有期限；checkout 后会话限额在查询前设置并回读生效值；执行 SQL 与 `GuardedQuery.normalized_sql` 完全相同；最多读 `max_rows + 1`；字节/单值/类型/重复列/超时/取消/网络错误均按契约关闭。
- [ ] 实现 `StarRocksTarget`、Adapter 生命周期、元数据请求和有界结果类型；驱动异常统一映射，不在错误链、repr 或日志中保留下层异常。
- [ ] 证明截断、取消、超时和任何未读完结果都会废弃连接而不回池；正常读完的连接才允许复用。Adapter 的行数与字节上限按 §2.3 对齐 Web 与飞书两条模型可达投影；结果字节计数使用生产 JSON 编码后的 UTF-8 大小，覆盖控制字符、引号和反斜杠的转义膨胀。
- [ ] 使用获准测试库验证 read-only grant、TLS 证书验证、会话变量设置后回读、非缓冲流式读取、Decimal/date/time、空结果、截断、客户端取消与服务端 query timeout。真实测试使用单独显式命令；该命令缺配置时必须失败，不能静默 skip。缺环境时 Task 2 只关闭“离线 Adapter”部分，并把真实项留到 Task 9。
- [ ] 运行目标 pytest、Ruff、mypy 和依赖审计；确认驱动只出现一次且锁文件完整。
- [ ] 独立审查候选精确 SHA，重点核对连接获取顺序、资源关闭、未知结果与无重试；提交 `feat: add bounded StarRocks adapter`。

### Task 3：受治理工具与结构化 Evidence 交付

**Depends on:** Task 2 reviewed SHA; Gate 0 passed at an exact reviewed SHA, or its required prerequisite repair passed and was merged.

**Result:** 真 SDK Runner 通过现有治理按用途调用 StarRocks 工具；Web 与飞书会话的模型/Session 都收到同一组共同获准列和完整有限行，Web 生成结构化事实，飞书把同一数据范围渲染成受限纯文本。

**Files:** Modify `src/xiaowei/models.py`, `src/xiaowei/governance.py`, `src/xiaowei/evidence.py`, `src/xiaowei/app.py`, `src/xiaowei/starrocks.py`; Create `tests/p1b/test_starrocks_tools.py`, `tests/p1b/test_delivery_projection.py`.

Task 3 实施修订（不改变产品范围与权限边界）：

- 工具契约、参数、投影策略与执行绑定放在新模块 `src/xiaowei/starrocks_tools.py`，`starrocks.py` 只增加 `target` 属性与元数据 SQL 的字节上限，Adapter 不依赖治理层。
- 前置检查是通用的 `Prechecked(check, run)`：`check` 同步、零 I/O，在当前授权之后、预算预留之前运行，`run` 只收到它的结果（查询为 `GuardedQuery`）。`describe_table` 的对象 allowlist 也走同一机制，参数名为 `table`。
- 依赖真实 PostgreSQL 与真 Runner 的用例放在 `tests/sdk_core/test_starrocks_tools.py`（含交付投影用例）：CI 的 `tests` 任务忽略 `tests/sdk_core` 且不提供 PostgreSQL，只有 `integration` 任务提供。离线容量契约在 `tests/p1b/test_starrocks_tool_contracts.py`；真实 StarRocks + PostgreSQL 的端到端用例加入显式的 `test_starrocks_real.py`。
- 启动容量检查按审查要求不写算术公式：按声明上限构造序列化后最大的合成结果，直接交给证据的真实投影器检查 Web 与飞书两条路径。服务端列名由同一上限在运行时约束，超出按结果契约失败。投影规则升为 `/6`（策略可声明必需字段），升级前的证据随之不可读。首轮审查后必需字段进入策略指纹，规则升为 `/7`；飞书纯文本的单元格与模型文字只占一行，单元格竖线写作 `\|`，表格行带首尾竖线。
- `Application.run_turn` 返回已提交的 `AgentAnswer`，最后阶段日志由 `delivered` 改为 `completed`；交付由调用方经 `EvidenceStore.validate_answer` 生成。`DeliveryFact` 另带 `target_id`、`captured_at`、`truncated`。飞书纯文本逐行渲染表格并转义单元格中的控制字符，按消息长度分段留给 Task 7。

- [ ] 先写真 Runner + scripted Model 用例：查询轮展示三个工具；诊断轮展示 `list_tables`、`describe_table` 且隐藏 `run_readonly_query`；未授权工具不展示。模型强行调用诊断轮查询、展示后撤权、参数越界均断言 Adapter 0 调用。
- [ ] 为三个工具登记严格参数/结果 schema 与四种 `ToolPolicy`；`list_tables` 使用严格空参数对象。元数据结果和查询结果都只从受信 Adapter 输出建立 Evidence。
- [ ] 在 `GovernedTools` 实现同步策略 precheck。证明 SQLGuard 拒绝返回带稳定 `QueryRejectionCode` 的 `ToolRejectedError`，pool/Adapter/SQL execute 为 0、工具调用预算不变且模型可按安全原因在剩余轮次修正；precheck 通过后的执行失败仍是 `ToolExecutionError`、预算已消耗且模型不续轮。
- [ ] 增加 `DeliveryFact` / `Delivery.facts`；保持 `AgentAnswer` 不允许模型提交事实字段。`Application.run_turn` 预校验、提交并返回 `AgentAnswer`，不增加 `TurnOutcome`、重验代理或关闭代理。
- [ ] 装配版本化 `BusinessContext` 到 Agent instructions，并把其规范化指纹加入会话绑定；内容/版本变化时旧会话在模型调用前拒绝，权限集合不因此扩大。
- [ ] 分别用 Web 与飞书身份证明同一合成查询结果：四种 `ToolPolicy` 投影都含同一完整 `rows`；两条“model ∩ session ∩ 当前渠道”投影均完整保留 rows。Web 从它生成结构化表格，飞书从它生成纯文本摘要/分段；渲染形式不同但获准数据范围相同，不存在 Web 专属列/行。
- [ ] 启动容量检查以生产 JSON 编码后的最坏情况为准，覆盖 SQL、列、行、全部模型字段、元数据、Evidence 信封与转义膨胀；Web 或飞书任一路径容不下时启动拒绝。再用绕过/错误配置的运行时用例证明：`rows` 被顶层投影整体省略时 `EvidenceStore.record` 直接失败，不生成 Evidence、不让模型续轮，即使信封原本会显示 `truncated=false` 也不能静默通过。
- [ ] 实际 SQL、来源、时间、截断和数值来自 Evidence。伪造、跨会话、过期、撤权或策略变更在首次发送和重验中都拒绝。
- [ ] 运行相关 P1-A 回归与新增目标测试、Ruff、mypy；变异删除投影交集、重验或渠道检查时，对应用例必须失败。
- [ ] 独立审查候选精确 SHA，重点核对“模型不可生成事实”、当前权限与数据落点；提交 `feat: expose governed StarRocks tools`。

### Task 4：PostgreSQL v2 请求与渠道状态

**Depends on:** Task 3 reviewed SHA. Uses the existing isolated PostgreSQL harness.

**Result:** 显式迁移、请求去重、可重发回答、会话映射、中断恢复与清理在真实 PostgreSQL 上有确定状态机，不引入后台执行平台。

**Files:** Create `src/xiaowei/migrations/002_p1b_channels.sql`, `src/xiaowei/channel_store.py`, `tests/p1b/test_storage_v2.py`, `tests/p1b/test_channel_store.py`; Modify `src/xiaowei/storage.py`, `src/xiaowei/session.py`.

Task 4 实施修订（不改变产品范围与权限边界）：

- 两个测试文件依赖真实 PostgreSQL，与 Task 3 同理放在 `tests/sdk_core/test_storage_v2.py`、`tests/sdk_core/test_channel_store.py`（CI 只有 `integration` 任务提供 PostgreSQL）。
- `storage.py`：应用 schema 版本为 2，迁移按序号顺序执行；`upgrade_storage` 是唯一升级入口。就绪检查先比较版本：v1 报版本不符（需显式升级），不报“未初始化”。实例锁为 `hold_instance_lock`（专用连接上的会话级 `pg_try_advisory_lock`，退出时废弃该连接而不放回连接池）；初始化与升级在事务内用同一键的 `pg_try_advisory_xact_lock`，取不到即 `StorageBusyError`，不排队。持锁连接丢失由 `InstanceLock.verify()` 发现并锁低 `Readiness`；周期性检查与退出进程属于 Task 8 的 `serve`。初始化在任何建表（含 SDK 表）前先在专用连接上取得同一把会话级锁并持有到建表结束，应用表迁移在该持锁连接的事务内执行，避免内部重复取锁自冲突；被拒绝的初始化不修改数据库。
- `channel_store.py` 的 `ChannelStore` 只用基本类型的入参（`InboundRequest` 属于 Task 5）。会话语境与请求编号只保存 HMAC 摘要；Web 的 cookie 就是 Web 的会话语境原值。飞书 chat ID 是否保存供显式重发取决于 G2，本任务不保存目的地，留给 Task 7。
- readiness 规则：接受请求、状态迁移、取得投递权、结束投递与启动恢复的写入失败都锁低 readiness（接受请求的提交结果可能不明，锁低后交给重启恢复，避免同一请求编号永久“处理中”）。结果保存失败例外：先在同一事务把请求标为 `failed(result_not_saved)` 并关闭 Session，这一步也失败才锁低。锁低后拒绝新请求、新会话（含首次创建映射；已有映射仍可读取）与新发送，在途请求仍可写入终态。关键写入（接受、状态迁移、投递、结果保存及其失败标记、启动恢复）期间被取消时提交结果不明：锁低 readiness 后原样传播取消，交给重启恢复。
- 失败码为闭集 `busy | model_failed | evidence_failed | session_failed | result_not_saved | interrupted`：写入前核对、读取时拒绝未知值，迁移 002 的 CHECK 覆盖完整闭集。调用者经 `fail()` 只能写前四个；`result_not_saved` 只由结果保存失败路径与关闭 Session 在同一事务写入，`interrupted` 只由启动恢复写入。`fail()` 收到调用者闭集外的值（含两个内部码）时不写入，按关键状态失败锁低 readiness，留给重启恢复。新增失败码需要新迁移；`Application.TurnReason` 到失败码的映射属于 Task 5。
- 投递：首次发送取得 `pending → sending` 的条件包括 completed 结果与 failed/interrupted 的固定回执；显式重发只对 completed 结果取得 `failed/unknown → sending`；两者都要求请求未过期并核对 owner。
- 清理按对象各自的过期时间：会话已过期、非 current、没有 accepted/running/sending 请求时关闭并经 SDK `clear_session()` 清历史，再删除该会话已过期的请求（已结束且不在发送中）与证据；会话不再有任何请求时才删除元数据与已退役映射。从未进入 PolicySession 的渠道会话（没有会话元数据，也就没有 SDK 历史）另走受限路径：映射已退役、已过期且没有 accepted/running/sending 请求时，删除其已过期的终态请求与证据，再在不再有任何请求时删除映射，不调用 `clear_session()`；两类合计不超过批次大小。未过期的请求与证据留到下一次清理。

- [ ] 先写 v1→v2、fresh v2、重复命令、并发 advisory lock、未知版本、失败事务回滚和 v1/v2 双向拒绝测试。普通 `init` 与 `check` 不得把 v1 自动升级。
- [ ] 先写请求状态测试：同 key running/completed/failed/interrupted、不同摘要冲突、两个进程竞争同 key、结果大小/结构校验、`pending/sending/sent/failed/unknown` 投递状态和过期读取。自动首次发送/事件重投只能竞争 `pending → sending`；只有显式 resend 能竞争 `failed/unknown → sending`，各自都只允许一个进程成功。
- [ ] 先写一致性反例：Session 成功后结果写失败、结果成功后发送失败、进程在 accepted/running/sending 退出、恢复中断失败、当前会话运行中 `/新建`、清理与 serve/resend 并发、SDK clear 失败。事件重投遇到 failed/unknown 必须零发送，显式 resend 作为对照只发送一次。
- [ ] 实现最少两类表与明确事务；任一关键状态持久化失败时锁低本进程 readiness 并拒绝新轮，不能让同一 key 永久显示“处理中”。恢复只在持有单实例 advisory lock 的 `serve` 启动阶段执行：关闭不确定 Session、标记 interrupted，并把遗留 sending 转 unknown；不调用 Runner 或发送。
- [ ] 用专用连接实现进程生命周期 advisory lock：第二个 serve 与运行中的 serve 并存时拒绝，持锁连接断开时停止接收；`storage init/upgrade` 只有在独占同一锁时运行。不得增加一个能与在线 serve 并发修改请求状态的“恢复”命令。
- [ ] 会话关闭能力只放在 `session.py`。清理用受限批次、数据库过期/状态条件和 SDK 公共 Session API，只处理已过期且不再 current/running/sending 的对象。
- [ ] 运行新增 PostgreSQL 测试、相关 `tests/sdk_core` Session/Evidence 回归、Ruff、mypy；检查迁移 SQL 只修改 `xiaowei_` 表。
- [ ] 独立审查候选精确 SHA，重点核对竞争、跨表失败、过期与恢复方向；提交 `feat: persist channel request outcomes`。

### Task 5：共享 ChannelService

**Depends on:** Task 4 reviewed SHA.

**Result:** 两个渠道都只需把可信 `InboundRequest` 交给同一服务；用途、身份、Session、去重、运行、保存、重验和新建会话只有一条业务路径。

**Files:** Create `src/xiaowei/channel.py`, `tests/p1b/test_channel_service.py`; Modify `src/xiaowei/config.py`, `src/xiaowei/app.py` only for the fixed boundary methods.

Task 5 实施修订（不改变产品范围与权限边界）：

- 测试依赖真实 PostgreSQL，与 Task 3、4 同理放在 `tests/sdk_core/test_channel_service.py`。`config.py` 不需要修改：预算直接用现有 `Budget`，授权由 `AccessPolicy` 提供。`app.py` 只增加只读属性 `Application.evidence`；`channel_store.py` 只增加只读属性 `ChannelStore.readiness`。
- `AccessPolicy` 是协议：`resolve(channel, subject_id) -> AccessDecision | None` 与 `authorize(identity, target_id, tool_id)`。读取、发送与新建会话没有消息正文，因此 `resolve` 只取可信身份，用途不参与授权（由 `scope_for_turn` 处理）。返回 `None`、类型不符或抛出任何异常都按拒绝处理。`AccessDecision` 固定内部 subject、唯一 `target_id`、允许工具与数据策略版本。配置驱动的具体实现随 Task 8 装配（G6/G7 决定身份清单）。
- 拆为两个对象：`ResultDelivery`（请求存储 + `EvidenceStore` + `AccessPolicy`，负责 GET 视图、首次发送与显式重发）不持有 `Application` 或模型绑定，Task 8 的 `requests resend` 只装配它；`ChannelService`（`accept`、`process`、`new_session`）在其上加 `Application`。装配时核对 `EvidenceStore.authorize == AccessPolicy.authorize` 及应用与交付使用同一个 `EvidenceStore`。
- 本轮 Tool Scope 在 `accept` 时按本条消息的用途与当前授权计算一次，随接受回执交给 `process`；工具调用时治理层仍按当前授权复核。
- `TurnReason` 映射到 Task 4 的调用者失败码闭集，不新增失败码：`session_busy/busy → busy`；`model_failed/turn_limit/timeout → model_failed`；`answer_rejected/tool_failed → evidence_failed`；`session_unavailable/content_rejected/storage_failed/storage_unavailable/scope_rejected → session_failed`。每个失败码有固定回执文字；更细的用户提示需要新迁移，另行决定。
- 同会话互斥：进程内集合覆盖 `start → run_turn → complete/fail`；同会话已有请求在运行或等待保存时，新请求直接记为 `failed/busy`，模型与工具 0 调用。终态保存失败（readiness 已锁低）时该会话不再释放。`run_turn` 期间被取消时锁低 readiness 并原样传播，交给重启恢复标为 interrupted。
- 发送：渠道提供单次发送函数（返回 `sent | failed | unknown`）。completed 结果先重新验证再取得投递权；验证失败时取得投递权并记为 failed，不发送旧内容；发送函数抛出异常或被取消时记为 unknown 并原样传播。failed/interrupted 回执只走首次发送，不能显式重发。

- [ ] 先写 AccessPolicy 失败关闭、默认诊断、明确查询、每轮重新计算 scope、不可伪造 target/session、同会话繁忙、全局并发、重复请求、Session/结果双存储失败、状态写失败后 readiness false、新建会话和显式重验测试。
- [ ] ChannelService 的会话锁必须覆盖 `run_turn` 以及随后 completed/failed 持久化。用可控屏障让第一轮已经从 `run_turn` 返回但尚未保存结果，此时同一 Web cookie/Session 的第二轮必须返回 busy，模型和工具调用均为 0；只有第一轮终态成功落库后才允许下一轮。状态落库失败时会话保持封闭并触发 readiness false。
- [ ] 只实现 `InboundRequest`、`AccessDecision`、`RequestReceipt` 与 `ChannelService`；不把 HTTP、飞书 SDK、数据库客户端或凭据放入 RunContext。
- [ ] 固定时序并逐个注入失败：`Application.run_turn` 返回已校验 `AgentAnswer` → RequestStore 保存 completed → `EvidenceStore.validate_answer` 生成首次 `Delivery`。提交后撤权时 completed 结果仍保存但交付受控失败，后续 Session 回放也因当前 Evidence 权限失败。
- [ ] 证明 `completed` 重读和显式重发直接调用共享 `EvidenceStore.validate_answer` 而非 `run_turn`；该路径不装配 `ModelBinding`、不解析模型凭据。用计数模型/Adapter 断言模型、工具、StarRocks 都为 0。
- [ ] 证明入口授权与 Evidence 授权是同一个 `AccessPolicy` 装配实例；撤权后旧结果不可读、不可发。
- [ ] 运行目标测试及所有 P1-A `Application` 回归、Ruff、mypy。
- [ ] 独立审查候选精确 SHA，重点核对双存储时序和授权单一来源；提交 `feat: coordinate trusted channel requests`。

### Task 6：最小同源 Web

**Depends on:** Task 5 reviewed SHA.

**Result:** 正式启动的浏览器路径可以新建会话、提交查询/诊断、轮询结果并安全显示受信事实；不能跨 cookie/session 读取或执行脚本。

**Files:** Create `src/xiaowei/web.py`, `src/xiaowei/static/index.html`, `tests/p1b/test_web.py`; Modify `src/xiaowei/config.py`.

- [ ] 先写 ASGI 测试覆盖四个 API、cookie 建立、固定操作者、Host/Origin/JSON 检查、跨 cookie/request 读取、请求冲突、并发、新建会话和 readiness。Host/Origin 用精确 allowlist，覆盖外部域名解析到 loopback、`localhost.evil`、替代端口、尾点、用户信息与 IPv6 表示等 DNS rebinding/解析混淆反例。
- [ ] 请求体限制同时覆盖：超限 `Content-Length` 在调用 receive 前拒绝；伪造较小长度、缺少长度和 chunked body 在累计读取达到上限时停止，不完整缓冲超限正文。
- [ ] 加入含 `<script>`、事件属性、HTML 标签、Unicode 和超长值的结果；断言 API 是 JSON，页面代码只用 `textContent`/DOM 创建表格且没有 `innerHTML`、Markdown 或动态脚本执行。
- [ ] 实现一个静态页面和最少 FastAPI route；不增加模板系统、前端框架、WebSocket、公开登录或多用户角色。
- [ ] 用真实浏览器从正式 `xiaowei serve` 路径验证诊断、查询、有限表格、截断、失败编号、刷新后 GET 与新建会话。helper/ASGI 测试不能替代浏览器证据。
- [ ] 运行目标测试、Ruff、mypy；独立审查候选精确 SHA，重点核对身份、CSRF、XSS、跨会话和数据投影；提交 `feat: add local Web chat entry`。

### Task 7：飞书单聊长连接

**Depends on:** Task 6 reviewed SHA and verified public API of the locked Feishu SDK candidate. Real app configuration remains a Task 9 gate.

**Result:** 官方 SDK 长连接只接受获准单聊文本，持久去重后异步运行共享服务，单次发送保存的已校验结果；重复事件和发送失败不重跑 Agent。

**Files:** Create `src/xiaowei/feishu.py`, `tests/p1b/test_feishu.py`; Modify `src/xiaowei/config.py`, `pyproject.toml`, `uv.lock`.

- [ ] 用候选 `lark-channel-sdk==1.4.0` 做安装探针，固定实际 async connect/disconnect、事件字段、回调 ack/NACK/抛错与重投语义、发送 API、自动重试/fallback 设置与 `SendResult`；若持久化失败无法阻止成功 ack，或无法满足其他边界，记录具体限制并修订计划，不回退到未经验证的旧 raw client。
- [ ] 先写 SDK 边界 fake 测试：允许单聊、未授权 tenant/user、群聊、非文本、自身消息、过期事件、重复 message ID、持久化失败、队列满、出队 busy、进程取消/重启 interrupted、明确发送失败与未知结果。
- [ ] 实现 `/查询`、`/诊断`、`/新建` 和普通诊断文本；指令只影响当前消息，新会话不继承查询许可。
- [ ] 配置校验 `1 <= consumer_count <= max_concurrent_turns`；证明 Web 与飞书共享全局上限。队列满、出队 busy 和 interrupted 分别形成确定终态及一次固定安全回执；回执失败只更新投递状态，不进入 Agent。
- [ ] 证明同一 message ID 最多一个 Runner/查询；首次发送与 completed 事件重投只能取得 `pending → sending`，completed + sent/failed/unknown/sending 的事件重投均不发送。failed/unknown 只经显式 `requests resend`、Evidence 重验和对应数据库 CAS 单次发送，且不重跑 Agent。
- [ ] 在获准测试应用中验证真实长连接接收、回复、断线关闭、重投与一次发送失败；记录 app/tenant 范围和 SDK 版本，不保存 token 或业务正文。
- [ ] 运行目标测试、Ruff、mypy、依赖审计；独立审查候选精确 SHA，重点核对身份字段、ack/队列竞态和发送语义；提交 `feat: add governed Feishu chat entry`。

### Task 8：正式装配、维护命令与打包切换

**Depends on:** Task 7 reviewed SHA.

**Result:** 从干净环境安装后，唯一 `xiaowei` 命令可以显式初始化/升级/清理/重发并启动 Web + 可选飞书；wheel 和运行依赖不再携带旧产品入口。

**Files:** Create `src/xiaowei/runtime.py`, `src/xiaowei/cli.py`, `src/xiaowei/__main__.py`, `tests/p1b/test_runtime.py`, `tests/p1b/test_cli.py`; Modify `src/xiaowei/__init__.py`, `src/xiaowei/config.py`, `pyproject.toml`, `uv.lock`, `.env.example`, `.github/workflows/ci.yml`, `README.md`.

- [ ] 先写子进程测试，分别通过 console script `xiaowei` 和 `python -m xiaowei` 启动；确保 `src/xiaowei/__init__.py` 无导入副作用，两个 SDK `DONT_LOG` 环境值都在任何 `agents` import 前被强制为 `1`。预置 `0/false` 并触发含 canary 的模型/工具错误，所有 logger 输出不得出现 canary。
- [ ] 实现单一 runtime lifespan：配置校验 → 专用 PostgreSQL 连接取得单实例 advisory lock → schema 检查与中断恢复 → StarRocks Adapter → Evidence/Governance/Application/ChannelService → Web/飞书 → 反序关闭并释放锁。部分启动失败也要按逆序关闭已创建资源；持锁连接丢失时停止接收并退出。
- [ ] 实现 `serve`、`storage init/upgrade/cleanup`、`requests resend`，不增加独立恢复命令；恢复只随持锁的 serve 启动执行，`storage init/upgrade` 也须独占同一锁。显式 resend 只接受当前 owner 下的 failed/unknown 记录，经 Evidence 重验与数据库 CAS 后发送一次。危险目标、未知请求、Evidence 过期、pending/sent/sending 或并发竞争均失败关闭；命令输出只给状态和请求编号。
- [ ] 将 console script 改为 `xiaowei.cli:main`，增加同一行为的 `python -m xiaowei`，wheel 只包含 `src/xiaowei`。清点旧包与约 1700 项旧测试的依赖：新产品不使用、但旧测试仍需要的包移入现有 dev extra，不留在生产依赖；旧测试继续作为独立 legacy CI 回归，不计作 P1-B 证据。`tests/sdk_core`、`tests/p1b` 和仍适用的仓库级 security/contract checks 是新产品必过门禁。旧源码是否物理删除另列清理，不在本任务扩大差异。
- [ ] 健康检查不发模型请求或查询；必要配置、PG schema/恢复失败时 readiness false，可选 MCP/飞书暂时不可用只影响相应能力并有安全状态。
- [ ] 从空环境执行 `uv sync --locked --extra dev`、wheel 安装、两个入口的 CLI help、`tests/sdk_core`、`tests/p1b`、仓库级 security/contract checks、独立 legacy CI、Ruff、mypy 与依赖审计。独立审查候选精确 SHA 后提交 `build: switch to P1-B product entry`。

### Task 9：P1-B 实战退出与交接

**Depends on:** Task 8 reviewed SHA and all correctness gates in section 7 resolved for the selected environment.

**Result:** 一个固定候选 SHA 同时具备离线、真实 PostgreSQL、一个真实模型、一个真实只读 StarRocks、正式浏览器和真实飞书单聊证据；仍不把它称为部署/UAT。

**Files:** Modify `AGENT_HANDOFF.md`, `README.md`, this plan only for actual evidence and deviations; test fixture changes only when a real failure first becomes a separate repair task and commit.

- [ ] 固定候选 commit/base/merge-base 和锁文件；从干净工作树执行完整 `tests/sdk_core` + `tests/p1b`、Ruff、mypy、依赖审计和 CI 合同检查。
- [ ] 在 Gate 0 已通过的 Profile 上运行 `DEVELOPMENT_PLAN.md` §4 的固定真实模型样例集：正确选工具、工具失败、同会话连续追问、无证据不编造、恶意工具文本。逐项记录行为结果、关键事实、失败、耗时和 usage；不比较逐字回答，也不让同一模型自评代替验收。
- [ ] 再接获准 StarRocks，验证 list/describe/query、空结果、明确 LIMIT、自动限额、撤权零 I/O、只读 grant、超时/取消、结果过大与连续追问；不保存未脱敏业务数据到文档。
- [ ] 通过正式浏览器与真实飞书分别完成结构查询、一次实际查询、追问、新建会话、重复请求、发送失败/显式重发和进程重启中断。两端分别留证，不能互相代替。
- [ ] 针对候选精确 SHA 做独立安全与架构审查；任何 P1 阻塞项先回到对应任务形成修复提交、重跑相关证据，再审新 SHA。
- [ ] 更新 handoff 的已验证事实、真实缺口、命令和下一项；README 只写实际可复制的用户命令。P1-B 是否接受、是否合并、部署和 P3 UAT 由用户另行决定。

## 5. 环境与必要检查

| 层级 | 必需环境 | 允许证明什么 | 不能替代 |
| --- | --- | --- | --- |
| 纯离线 | Python 3.11、锁定依赖、无网络 | SQLGuard、投影、状态机、渠道适配合同、日志 canary | 驱动与真实服务兼容 |
| PostgreSQL 集成 | 现有隔离 PostgreSQL 16 harness | migration、Session、Evidence、请求并发/恢复/清理 | 正式持久卷和备份恢复 |
| StarRocks 集成 | 获准版本、TLS/FE、真实只读账号、受限测试对象 | 协议、类型、会话变量、只读/资源/超时 | 生产权限或业务口径 |
| 模型实测 | 一个获准 Profile 与合成/脱敏数据 | 真实工具续轮、结构化结果、usage/期限 | 其他供应商/模型组合 |
| 浏览器 | 正式 `xiaowei serve` 页面 | cookie、同源、DOM、正式路由 | ASGI helper |
| 飞书 | 获准测试应用/tenant/user | 长连接、标准事件、重投、发送结果 | SDK fake |

每个源码任务的最小检查是目标 pytest + 相关 P1-A 回归 + `ruff check` + `mypy`。依赖变化另做锁文件一致性与 `pip-audit`；数据库迁移另做真实 PostgreSQL；渠道阶段另做正式入口手工/自动化验证。纯文档任务只做链接、事实、命令和差异检查，不扩大到全仓产品测试。

## 6. 兼容、回退与恢复

- **数据库升级：** v1→v2 前必须取得同一 PostgreSQL 的受限备份并记录 schema 版本。迁移失败由事务回滚；迁移成功后不能自动降级。代码回退到 P1-A 时须先恢复 v1 备份，不能让旧二进制读 v2。
- **代码回退：** 每个任务独立提交。未变更数据库时回退到前一 reviewed SHA；Task 4 之后的代码回退必须与 schema 兼容性一起判断。不得用 `git reset` 覆盖用户工作。
- **依赖回退：** driver/Feishu 探针失败时恢复该任务开始前的 `pyproject.toml` 与 `uv.lock`，不保留两个并行客户端或可选 fallback。
- **请求恢复：** 只有取得单实例 advisory lock 的 `serve` 启动路径执行恢复：把 accepted/running 变为 interrupted 并关闭对应 Session，把遗留 sending 变为 unknown；用户用新请求/新会话继续，或对允许的投递状态显式重发。任何未知 StarRocks/模型/飞书结果都不自动重试。
- **渠道恢复：** Web completed 结果可经 GET 重验；飞书 failed/unknown 由显式 `requests resend` 重验一次。Evidence 过期或撤权后不能从保存文本绕过。
- **旧入口：** P1-B 不提供旧 CLI/DTO/HTTP 的兼容层。需要查看旧行为时使用 Git 历史或 P1-A 之前的已知 SHA，不让两个正式入口并存。
- **P3 边界：** 本计划只规定迁移前备份和代码/状态恢复语义；正式 Compose、持久卷备份、隔离恢复演练和部署回滚仍在 P3。

## 7. 尚未解决且影响正确性的疑点

以下不是实现细节；在关联任务进入真实数据或正式入口前必须得到环境事实或明确决定。G4 是 Task 3 开工前的 Gate 0；其余 Gate 未解决时可以完成不依赖该环境的离线部分，但不能关闭相应验收项。

| Gate | 需要确定 | 阻塞范围 |
| --- | --- | --- |
| G1 StarRocks 目标 | 精确版本、FE/TLS 接入、default database、表/视图/列/函数 allowlist、真实只读 grants、query timeout/memory/resource group 能力、目标时区 | Task 2 真实集成、Task 3 最终政策、Task 9 |
| G2 数据政策 | 每个工具共同允许给 model/session/Web/飞书的列与行集；查询结果四种投影都含同一 `rows`，Web 与飞书各自的模型可达交集均能容纳按生产 JSON 编码计算的完整最坏情况；各投影总字节/单值上限；Evidence、Session、请求结果、渠道映射/目的地的保留期；是否允许保存飞书 chat ID 供显式重发 | Task 2 上限、Task 3、4、7、9 |
| G3 业务口径 | 首个目标的可信字段含义、时间/时区、单位、必要过滤条件、文本与版本；其规范化指纹进入会话绑定 | Task 3 提示与会话、Task 9 质量验收 |
| G4 真实模型 Gate 0 | 获准 provider/endpoint/protocol/model、secret ref、数据接收范围、预算与保留政策；工具续轮到交付及 Session 追问通过，供应商必需字段可安全回放 | 阻塞 Task 3–9；未通过先做独立前置修复 |
| G5 驱动兼容 | `asyncmy` 锁定版对目标 StarRocks 的 TLS 证书验证、非缓冲流式读取、会话变量设置/回读、类型、取消和连接废弃行为 | Task 2 关闭、Task 9 |
| G6 Web 边界 | 固定操作者标识、允许 Host/Origin、HTTP/HTTPS/SSH 使用方式、cookie Secure 设置 | Task 6 正式配置、Task 9 |
| G7 飞书边界 | app/tenant/user allowlist、所需 scopes、长连接环境、消息时效、文本上限、目的地保留与显式重发授权 | Task 7 真实验证、Task 9 |
| G8 SDK 包行为 | `lark-channel-sdk` 锁定版能否禁用自动发送重试/fallback并区分成功/失败/未知；`asyncmy` 和 Feishu 依赖的审计/供应链结果 | Task 2、7、8 |

P1-A 已有 Gemini 的真实部分证据：观察到类型化回答、工具选择和同轮 `thought_signature` 回传，但没有完成工具结果后的最终交付，也没有证明 Session 追问回放。主线决定要求在 Task 3 接入模型工具路径前完成 Gate 0，不能把这项假设推迟到 Task 9。其余环境 Gate 缺失时可以推进不依赖该环境的离线部分，但不能关闭对应验收项。任何 Gate 的最终值进入对应的唯一配置/数据政策与 handoff 实测记录，不另建第二份环境需求表。

## 8. 计划自审清单

- [x] 本文没有把 P2 EXPLAIN/Profile、P3 Compose/UAT、写操作、多人 Web、群聊、跨渠道身份、Worker、Redis 或新 MCP Server 带入 P1-B。
- [x] 每个外部 I/O 前都有可信身份、目标、参数、SQL、预算和当前授权检查；拒绝路径有零 I/O 验收。
- [x] 首次发送、GET、重发、Session 持久化都复用 Evidence 验证，不信任旧渲染文本。
- [x] 真实 Profile 闭环是 Task 3 前的 Gate 0；诊断轮保留元数据工具并隐藏实际查询工具。
- [x] 现有顶层投影能力只承诺共同列与共同有限行；Web 与飞书投影都含完整 `rows`，两条模型可达路径分别做序列化容量检查和运行时缺失兜底，没有承诺渠道专属列/行或隐式按行裁剪。
- [x] 双存储所有失败窗口都有“关闭会话或拒绝就绪”的保守结果，没有查询补偿重跑。
- [x] 每片有成功路径、关键失败、精确验证命令、独立审查 SHA 和独立提交。
- [x] 真实模型、StarRocks、浏览器、飞书、部署与用户接受仍分别记录，没有由离线测试自动晋级。
