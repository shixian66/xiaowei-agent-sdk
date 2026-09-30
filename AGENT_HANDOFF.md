# 小维：当前交接

> 更新：2026-09-30，Asia/Shanghai。这里只记录当前事实、证据与下一项工作；设计见 [ARCHITECTURE.md](ARCHITECTURE.md)，路线见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)，协作规则见 [AGENTS.md](AGENTS.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 1. 当前快照

| 项目 | 已核对事实 |
| --- | --- |
| 仓库 | [shixian66/xiaowei-agent-sdk](https://github.com/shixian66/xiaowei-agent-sdk) |
| 本地目录 / 分支 | `/Users/kloenguyen/Desktop/agent-SDK` / `claude/p1b-sqlguard`（从 `origin/main` 的 `3abd1b3` 分出，该提交合入了 Gate 0 离线部分 PR #9） |
| M5 历史起点 | `372c381f44ecfa1fa53961f137d0058033cbd805`；不是远端当前 main 的核验结论 |
| P1-A 代码基线 / 当前主线 | `148abaa4729ac6644c03056cf265a9dc18f1acd8` / `3abd1b3c455a51565baa74192b3269b584b0fa8c`（P1-B 计划与 Gate 0 离线部分已合入） |
| 当前阶段 | **P1-B Task 1 SQLGuard 离线实现完成，待独立审查**；Gate 0 离线部分已合入（PR #9），真实运行未进行，阻塞 Task 3–9。P1-A 离线完成（Task 1–5 经 PR #2–#7 合入）；Gemini 只有部分真实证据 |
| 当前源码与依赖 | 新包 `src/xiaowei/`（`config.py`、`storage.py`、`model_api.py`、`models.py`、`governance.py`、`evidence.py`、`session.py`、`mcp.py`、`tools.py`、`app.py`、`migrations/001_initial.sql`）与旧 `src/xiaowei_agent/` 并存；锁定 `openai-agents[sqlalchemy]` 0.22.3、`mcp` 2.2.0、`openai` 3.20.0、SQLAlchemy 2.0.52、asyncpg 0.30.0，Python 3.11.16；wheel 同时打包两个包，CLI 仍指向旧包 |
| 新产品入口 | 只有开发验证命令（见第 5 节）；尚无产品启动入口，旧 CLI/Compose 不算新入口 |
| 本次工作范围 | 计划顺序调整（用户批准：Gate 0 只阻塞 Task 3–9）与 [P1-B 计划](docs/superpowers/plans/2026-09-30-p1b-starrocks-dual-entry.md) Task 1：新增 `src/xiaowei/sqlguard.py` 与 `tests/p1b/test_p1b_sqlguard.py`；未接入 `GovernedTools`（Task 3），未改依赖或迁移，未部署 |
| 外部操作 | 主线曾获用户授权用合成数据调用 Gemini API（见第 3 节），验证后本机凭据文件已删除，用户负责作废该密钥；Gate 0 与 SQLGuard 离线工作没有获准 Profile 与凭据，未调用真实模型、StarRocks、飞书或外部 MCP Server |

表中分别列出 P1-B 所依据的 P1-A 代码基线与计划所依据的当前主线，不能混用。接手先用 `git rev-parse HEAD` 和 `git status --short` 取得实际版本；审查使用对应提交的精确 SHA，本文件的修改历史由 Git 保存。

## 2. 已确定的产品边界

- 按 SDK 原生方式从产品需求设计，允许从零开始；旧代码只按当前价值复用，不保留旧 Runtime 作为前提。
- 首版统一 StarRocks 查询与慢查询诊断，提供最小 Web 与飞书对话；单 Agent、单进程、一个连接，两端独立会话。
- MCP 是最小通用客户端接入；StarRocks 先走本地受治理工具，不在首版自建业务 MCP Server。
- 用户要求接入 Gemini、DeepSeek、OpenAI 等模型 API；运行核心仍固定为 SDK，具体模型与端点未选定。
- SDK Session、严格 RunContext、调用前治理、四种数据投影、最终 Evidence 验证、动态工具集合与默认关闭 tracing 按架构落实。
- 首版正式存储为 SQLAlchemySession + PostgreSQL，SDK 表与应用表独立归属；Docker Compose 部署小维与 PostgreSQL 两个常驻容器，数据使用持久卷，服务器 Web 通过 SSH 本地转发访问。
- 七项最小方案已确认：本轮查询/诊断范围、程序生成关键事实、简短业务口径、PostgreSQL 初始化/升级与备份恢复、会话上限/新建/过期清理、请求编号与基础健康检查、简单容器部署。详细契约和任务分工见架构与计划，尚未实现。
- 首版只读。未来生产变更统一为 Action，先展示具体内容并取得有权限用户明确确认，执行前重新检查 Policy / Approval / Action Binding；诊断不能自动升级为写。
- 开发采用小步实现、按风险验证和可接续交接；不把文档调优视为源代码实施、产品验收或生产操作授权。

详细规则不在本文重复展开，以架构及开发规则为准。M5、旧 ADR 与旧验收仅是历史材料，不作为新产品已经可用的证据。

## 3. 当前计划与下一项工作

P1-A 实施事实保留在 [P1-A：SDK 与治理执行核心](docs/superpowers/plans/2026-09-29-p1a-sdk-governed-core.md)。下一阶段唯一详细计划是 [P1-B：真实只读查询与双入口](docs/superpowers/plans/2026-09-30-p1b-starrocks-dual-entry.md)，顺序为 **SQLGuard → StarRocks Adapter →（真实模型 Gate 0 须已通过）受治理工具/Evidence → PostgreSQL v2 → 共享 ChannelService → Web → 飞书 → 正式入口 → P1 实战退出**。产品边界仍以 `ARCHITECTURE.md` 为唯一权威，当前证据仍以本文为准。

**Task 1 已证明（离线、合成数据、scripted 模型）：**

- 新包在本仓库 `.venv` 中从零安装（`uv sync --locked --extra dev`），pytest、pytest-asyncio、pytest-socket、Ruff、mypy、pip-audit 齐全；导入新包不加载旧 `xiaowei_agent`。
- 真 `Runner` 调用一个合成 function tool 一次，工具结果回传后模型续轮，返回 Pydantic `output_type`。
- 测试 PostgreSQL 的身份由代码核对：镜像按 digest 固定；环境变量必须与唯一的管理地址 `postgresql+asyncpg://postgres@127.0.0.1:55432/postgres` 逐字相同（查询参数、密码、其他端口一律拒绝），实际连接地址只取自该常量；建库和删库各自在执行的同一连接上先核对服务器 `cluster_name=xiaowei-sdk-test`；调用体抛出异常时也会删除临时库。
- `SQLAlchemySession` 在隔离的真实 PostgreSQL 16.15 上写入工具调用配对；重建引擎和 Session 对象后回放，追问不重跑工具。未初始化或连接失败时 `check_storage` 拒绝，模型调用次数为 0，错误不含地址或凭据。
- MCP：使用 SDK `MCPServerStreamableHttp` 连接 loopback 测试服务，通过公开的 `list_tools()` / `call_tool()` 构造薄 FunctionTool。范围不允许时发出的 `tools/call` 请求为 0；允许时禁止字段不进入模型输入。`httpx_client_factory` 已确认被 SDK 使用。
- `configure_runtime()` 关闭 tracing 并移除默认导出处理器；对照组能观察到导出，处理后 `trace_exports == []`。
- 反向验证：缺少 `SDK_TEST_POSTGRES_URL` 时报错而不是跳过；去掉 tracing 两步中任一步、MCP 前检查或结果过滤中任一项，对应测试都会失败。

**Task 1B 已证明（离线、HTTP mock，真实 SDK Model + Runner）：**

- Responses（OpenAI Profile）与 Chat Completions（Gemini Profile）经真实 `OpenAIResponsesModel` / `OpenAIChatCompletionsModel` 完成工具调用 → 工具结果回传 → 类型化最终回答；请求使用 Profile 的端点、期限与输出 token 上限，Responses 发送 `store=false`，未配置的推理/采样参数不发送。
- `json_object`（DeepSeek Profile）经薄 `Model` 委托发送 `response_format={"type":"json_object"}` 并附 schema 说明；缺字段、错类型、非 JSON、空内容与 `null` 都不会成为成功结果。
- 三个 Profile 的假密钥只发往各自端点；`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_ORG_ID`、`OPENAI_PROJECT_ID`、`OPENAI_CUSTOM_HEADERS` 等环境值不随请求发出，同名的 `Host`/`Accept`/`Content-Type`/`Content-Length` 也不能替换协议头；默认网络 transport 不读取 `SSL_CERT_FILE` 等环境配置。Profile 指纹稳定、不含密钥，端点/协议/模型/输出模式任一变化都会变。
- Chat Completions 截断、过滤或缺失终态（含合法 JSON 与语法完整的工具调用）在 SDK 解析前拒绝，工具零执行、无续轮；Responses `incomplete` 由 SDK 拒绝作为对照。三个 Profile 的带工具请求都发送 `parallel_tool_calls=false`。
- 401/403/429/503、超时、非法 JSON 与跨端点 307：只发出一次请求、不重试、不跟随、不切换端点、工具只执行一次。
- 请求超限在发出前拒绝（端点收到 0 个请求）；响应超限在读取阶段停止（64 KiB 响应只读取不超过 5 块）；压缩响应（200 与 503）被拒绝；SSE 或非 JSON 的成功响应被拒绝。
- 缺失 usage 时 `raw_usage is None`，不与“为 0”混淆。Profile 校验拒绝非 HTTPS、带用户信息/查询、原始密钥、非正限额、未映射参数、不支持的协议/输出模式/推理强度组合。

**Task 2 已证明（离线、合成工具、真 Runner + ScriptedModel、隔离的真实 PostgreSQL）：**

- 工具展示后撤权：SDK 仍按模型请求调用，治理层在 I/O 前拒绝，recording adapter 零调用，模型收到固定拒绝信息。执行期间或证据写入期间撤权：执行 1 次，本轮中止、模型没有续轮（Task 5 审查修复后；此前是把固定信息交给模型，模型可在同轮重试），异常信息无业务字段。越出 Tool/Target Scope、目标不符、未登记工具、参数类型错误/缺失/多余时同样零 I/O，且不调用授权回调。
- 诊断轮（Tool Scope 不含查询工具）即使用户有查询权限也不展示查询工具，模型强行调用零 I/O。
- 预算上限 1：`asyncio.gather` 与 SDK 并行执行的两个调用都只执行一次；计数按 subject/session/turn 隔离，`end_turn` 后清理。执行失败不退还预算，错误不含下层异常文本与原因链。
- RunContext 拒绝额外字段（客户端、连接串、密钥）、非声明类型的元素、空标识、未知渠道与非正预算，构造后不可修改。
- 四种投影各自只含获准字段并满足各自字节上限，模型与 Session 投影另受记录所在渠道的字段与上限约束：模型把工具结果原样抄进分析或澄清，飞书交付中也不出现飞书禁止的字段（Web 对照中该字段正常出现）；合成禁止字段不出现在任何投影和 PostgreSQL 行中；截断（字段省略或来源截断）与空结果有明确标记。
- 伪造、他人、其他会话/渠道、目标越出范围、撤权与过期的证据均不可读，信息相同；重建引擎和存储对象后检查仍成立。重建时策略字段或上限收窄、策略换版或工具移除，旧证据的四种投影和最终回答都被拒绝，同一策略重建则照常可读。
- 最终回答：无引用的结论、分析引用未选证据/伪造证据、重复引用、空回答、澄清与结果混用均被拒绝；真实值 100 只出现在代码生成的事实区域，模型分析中的“200”只在标注为推断的分析区；模型多交事实字段时 SDK 拒绝最终输出。
- 应用表缺失、版本不符时拒绝就绪；重复初始化不重建，版本不符时初始化也不升级；证据写入不改动 SDK 会话项。

**Task 3 已证明（离线、合成工具、真 Runner + ScriptedModel、SQLAlchemySession + 隔离的真实 PostgreSQL）：**

- SDK 在一轮内写入的用户输入、工具配对与最终消息先进入暂存区：`Runner.run` 返回后、提交前 SDK 表为空；提交后 SDK 表只有纯文本消息、工具调用与证据引用，不含合成禁止字段，也不含 SDK 的 item `id`/`status`。
- 模型与 Session 共用同一份投影：字段相同、顺序相反且容量只放得下一个字段时，模型看到的就是 Session 保存的那一个，Session 未选中的字段不经模型文字落库。
- 超过字节上限或含禁止模式的用户输入在首个模型调用前拒绝：模型零调用、工具零执行、SDK 表为空；符合策略的文字照常保存与回放（对照）；策略收紧后含旧禁止模式的历史整段拒绝。
- 模型可达的工具数据取模型、Session、渠道三方共有字段：模型把看到的一切抄进中间文字、下一次工具参数和最终分析，Model-only 值不落库；Session-only 值不进入任何一轮模型输入；共有字段正常保存并回放（对照）。
- 追问时 SDK 消费重建的调用/结果配对，回放内容是当前的模型可达投影（`rows`），工具不重跑。回放与暂存都核对证据由同一次调用生成：只改函数名、参数、调用标识、证据引用，或参数无法解析、缺少调用项，都整段拒绝；完整匹配的对照正常回放。
- 应用元数据缺失而底层已有纯文字历史时，不登记、不返回历史、模型零调用，`clear_session` 也拒绝；空 Session 正常登记（对照）。撤权、Target Scope 收窄、他人或其他渠道使用同一会话标识（含只有文字、没有证据的历史）时，新一轮在首个模型调用前拒绝；恢复权限后原历史照常可用。
- 引用伪造证据的最终回答（SDK 类型合法）在提交时被 Evidence 验证器拒绝；多交事实字段时 SDK 拒绝；两种情况 SDK 表均为空、轮数为 0，下一轮不带出残留暂存项。没有最终回答的暂存内容不能提交。
- 底层写入中途失败（已写入 1 条后抛出）：会话停在 `writing`，重建引擎和存储对象后仍在模型调用前拒绝，工具不重跑。
- 轮数或字节达到配置上限时新轮零模型调用；放宽上限后原历史完整可用（未被裁剪）。本轮使历史超限时拒绝提交并封存，工具只执行 1 次。证据过期或会话过期（两种保留期先后分别验证）时，记录仍在库中也不能回放。
- 端点、协议、模型或 `data_policy_id` 任一变化：旧会话在首个模型请求前拒绝；新会话独立运行，首个模型输入只有新消息。
- 图片输入、system 角色、托管工具项没有保存形式，整轮拒绝；治理拒绝的工具输出以固定文字保存与回放；篡改的历史（原始内容、未知证据、证据挂到其他调用、错配/孤立结果、未应答调用）整段拒绝。`pop_item` 只撤回暂存项；`clear_session` 底层清除失败时会话已关闭、不可回放。

**Task 5 已证明（离线、合成数据、真 Runner + ScriptedModel、loopback MCP fixture、SQLAlchemySession + 隔离的真实 PostgreSQL）：**

- `Application.run_turn` 一轮内经真 Runner 调用本地工具与 MCP 工具各一次，最终回答通过 Evidence 校验后提交，Web 交付含代码生成的事实区域与标注为推断的分析；同一会话追问时模型从回放的历史引用上一轮两条证据，本地与远端工具都不重跑。同样的问题在飞书会话得到飞书投影（无 `rows` 与 `note`），禁止字段不出现在任何交付中。
- 两个用户的会话并发：A 停在模型调用中时 B 以诊断范围完成一轮，A 之后的模型调用仍是自己的工具集合；两轮的工具预算各为 1，各自执行一次；交付只含本轮数据；B 引用 A 的证据被拒绝。
- 同会话已有一轮在运行时，新消息立即拒绝（`session_busy`），同时到达的两条消息只有一条进入；并发达到上限时其他会话拒绝（`busy`）；被拒绝的消息模型零调用；结束后释放。
- 引用伪造证据、夹带事实字段、非 JSON 回答与上游异常（消息含模型/上游内容）：无交付、SDK 表为空、会话轮数为 0，错误信息固定、不含模型内容且无原因链；下一轮模型输入只有新消息。
- 总期限（0.5 秒）在模型调用中到期：在期限内失败，工具只执行一次，不提交；调用中取消：取消照常传播，不提交；下一轮不重放任何工具，看不到未提交轮次的内容。
- 用途范围：查询轮展示并执行查询工具；下一轮为诊断时查询工具不展示，模型强行调用时 SDK 拒绝，本轮失败且零执行、不提交；用户未获查询授权时查询用途也不含它。本轮 Tool Scope 超出 Profile 数据策略、用户输入不符合数据策略的准入模式、存储未就绪：都在首个模型调用前拒绝。
- 在真实 PostgreSQL 的 SDK 消息表上以触发器注入写入失败：无交付，工具执行一次，会话停在 `writing`；移除触发器后该会话在模型调用前拒绝，工具不补跑。提交之后、交付之前撤权：本轮已保存，但不交付。
- 阶段日志：root 在 DEBUG 级别收集全部日志，`xiaowei.app` 每条都只有 `turn_id`、阶段、原因代码与耗时，成功与失败轮次各自的阶段序列完整；合成 SQL、模型分析、禁止字段、远端夹带内容与假 MCP 凭据不出现在任何 logger 的输出中。
- 全局 tracing 被重新打开并挂上处理器时，本应用的一轮仍不产生任何 trace/span。
- 启动时拒绝：缺少查询或诊断用途、Profile 没有对应数据策略、用途中的工具未登记、本地工具不是已登记的 `local/` 工具。

**Task 5 审查修复（从 `6d3464d` 到最终提交 `777b725`；复审通过并经 PR #7 合入）：**

- 运行依赖绑定：`open_model` 只按 Profile 的 `api_key_ref` 解析凭据（没有另传密钥的参数，引用无法解析时不创建客户端、零请求），返回只能由它创建的 `ModelBinding`（Profile、SDK Model、设置、指纹），`Application` 只接受它，直接构造或传入裸 Model 均拒绝；`GovernedTools(evidence)` 的工具目录与授权取自证据存储本身，`Application` 不再单独接收证据存储；MCP 接入使用另一个治理对象时拒绝装配，远端零请求。应用测试改为经 `open_model` + HTTP mock 驱动真实 `OpenAIResponsesModel`。
- 数据策略绑定：会话绑定 Profile 指纹与规范化的数据策略内容；同一 `data_policy_id` 下收窄 `model_tools` 或输入上限后，旧会话在首个模型调用前拒绝，内容不变的新应用照常回放。工具投影字段与容量的收窄由已有的证据策略指纹覆盖（Task 2/3 用例）。
- 参数：`normalize_arguments` 按 JSON 严格模式校验，并在校验调用上强制禁止未声明字段（含嵌套模型，不论模型配置或被覆盖的 schema）；字符串数字、嵌套字符串数字、整数字段给浮点、NaN、Infinity、额外字段（模型配置为 allow/ignore 或 schema 伪装为禁止）都在 I/O 前拒绝。执行与证据使用同一有效请求（校验器改写后的值，如 `" east "` → `"EAST"`、`1` → `1.0`）；会话核对历史调用时经同一函数规范化后与证据比较，原始写法与等价有效写法都通过，其他参数不通过。工具目录按 Pydantic 运行时字段（不读 schema）要求参数字段（含嵌套模型）全部必填，且只由基本类型、枚举、Literal、嵌套模型及其容器组成；schema 伪装为必填的默认值在登记时拒绝。有效参数经 `contract_dump` 按声明字段与类型从已校验实例递归生成，不调用模型的序列化，也不交回同一模型校验：计算字段、`field_serializer`、根/嵌套 `model_serializer`、`PlainSerializer`、`Field(exclude=True)`，以及让同一模型二次校验通过的 before validator 组合，都不改变执行与证据收到的字段；字段取自登记模型而非实例的运行时类型；模型、枚举与基本类型要求类型完全一致，validator 把根或嵌套模型换成子类、无关模型或字典、删掉字段、改写出错误类型（含以 `bool` 充当整数）或非有限数值（含枚举动态成员、嵌套容器）时在 I/O 前受控拒绝；联合类型按实际校验出的分支生成（分支顺序互换、继承、列表与映射包裹均保留子类字段，不同参数的摘要可区分）；字段校验器规范化照常（对照）。工具目录把参数与结果模型的字段类型限定在生成规则之内（映射的键只能是 `str`，枚举成员与 Literal 选项须为有限 JSON 基本值），其他类型在登记时拒绝。
- 结果：MCP 结果校验强制忽略未声明字段（含嵌套），结果模型配置为 allow 且 schema 伪装为禁止时，嵌套的远端字段也不进入 Evidence、模型或会话；工具目录不再凭 schema 判断结果模型的额外字段规则。结果同样经 `contract_dump` 生成：嵌套计算字段、serializer 与 validator 组合的内容不进入模型、会话或 Evidence；结果根对象被替换或字段值不符合声明类型时本轮中止、不生成证据；联合类型同样保留实际分支，非有限数值拒绝。证据规则版本升为 `/5`：按旧规则（模型自己的序列化）生成的证据不再可读，读取、交付与回放都在模型调用前拒绝。
- 结果未知：只有 I/O 之前的治理拒绝交给模型修正；执行开始后的失败（本地执行错误、MCP 错误/超时/不合约/超限、证据无法保存或执行期间撤权）中止本轮，模型没有续轮，应用映射为 `tool_failed`，不提交。本地与 MCP 各有“执行后失败、模型试图再调用”用例，参数错误交给模型修正作为对照。

**Task 5 缺口：** SDK 自身在模型错误时写 ERROR 日志，锁定版默认（`OPENAI_AGENTS_DONT_LOG_MODEL_DATA` 未设置）会隐去错误内容，已用例验证；显式关闭该默认时上游错误体会进入日志（对照已验证），应用未强制该运行配置；参数模型不能有默认值与可选字段，也不能用数据类、TypedDict 等结构；字段校验器改变取值时，校验器代码变化会使历史调用规范化结果变化，相应证据在回放时不可读（保守拒绝）；执行后失败会让整轮失败，模型不能自行换用其他工具继续；真实模型只有 Gemini 的部分证据（见下文“真实模型尝试”），三个 Profile 均未完成工具续轮到交付与追问的闭环；并发上限与同会话互斥在进程内，只拒绝不排队，多进程部署需另行设计（首版单进程）；总期限覆盖提交，期限或取消落在提交过程中时会话被隔离，只能新建；提交后到交付前撤权时本轮已保存但不交付，下一轮回放会因证据不可读而拒绝；最终结果与渠道投递状态的保存、Session 已提交而结果保存失败的双存储边界属 P1-B；阶段日志只经标准库 logging 输出，处理器、格式与保留未配置；MCP Server 不可用只减少 `available_tools`，未进入就绪检查；SDK 自身的 `OPENAI_AGENTS_DONT_LOG_*` 依赖默认值；用户消息大小由数据策略的输入准入上限约束；策略模型的 serializer 与计算字段不参与有效数据，需要派生值时由 Adapter 自己计算；after validator 不经校验改写的值只核对类型，`Field`/`Annotated` 的长度、范围等约束不重新检查；字段值类型不符只能在调用时发现，参数侧以固定的“参数不符合工具契约”交给模型；集合（`set`/`frozenset`）按迭代顺序输出，字符串集合的顺序随进程变化，证据回放可能因摘要不同而保守拒绝（此前的 `model_dump` 同样如此，未在本次处理）；模型调用失败（含上游 503）不重试，接真实服务时是否增加有限重试属产品取舍，未决。

**Task 4 已证明（离线、loopback MCP fixture、SDK 官方 Streamable HTTP 客户端、真 Runner + ScriptedModel、隔离的真实 PostgreSQL）：**

- 空配置在禁止一切网络的用例中进入与退出，不建立连接，工具集合为空。
- 真 Runner 经薄 FunctionTool 调用远端 `lookup` 一次，模型收到代码生成的证据标识与获准字段；最终回答通过 Evidence 验证，事实区域含远端值。只有一段 JSON 文本、没有结构化内容的结果同样按契约接收。两个 Server 的同名工具映射为 `alpha__lookup`/`beta__lookup`，只在对应 Server 执行，证据记录 `beta/lookup`。
- 本轮范围不含该工具时不展示，模型强行调用由 SDK 拒绝；展示后撤权、参数不是 JSON 对象时，治理层在发出前拒绝：远端工具零执行、HTTP 请求数不变、不生成证据。远端结果中的注入文字声称预算不限，上限 1 时第二次调用仍被拒绝。
- 登记与目录不一致（名字冲突、目录缺少契约、策略不符、策略无结果模型、`server_id` 重复）或参数 schema 不受 SDK 严格模式支持（如映射参数）时，在连接任何 Server 之前拒绝装配，远端零请求；远端改了参数类型、缺少登记的工具、未登记而自称只读的工具不开放，强行调用零执行。
- 结果模型由工具目录约束：不能接收未声明字段（含嵌套模型），投影字段必须是它声明的字段。远端结果按 JSON 严格模式校验，字符串 `"7"` 不会转换成整数。
- 结果过滤：禁止字段与远端伪造的证据标识不进入模型输入，也不出现在 Evidence、SDK 会话表与会话元数据中；下一轮从 Session 回放 MCP 证据，远端不重跑。不合约的类型（含可被宽松转换的字符串数字）、结构化内容合约但附带图片/资源链接、远端报告错误（附带形似合约的内容）、超过接收上限的响应：执行一次，本轮中止、模型没有续轮（Task 5 审查修复后；此前把固定失败信息交给模型，模型可在同轮重试），不生成证据，之后的调用照常。压缩响应的 Server 整体不可用。
- 认证：Bearer 凭据只随发往登记端点的请求发出；错误或缺失的凭据使该 Server 不可用，只隐藏它的工具，另一 Server 与本地工具照常。跨源重定向不跟随，另一端点零请求；同源重定向到不同路径、附加 query（`/mcp?other-service=1`）或编码不同的路径（`/mcp%2Ftenant-a`）时不发出，只有登记的原始目标收到请求且带凭据。假凭据不出现在模型输入、数据库或日志中。
- 日志：MCP 库会在本地过滤前把完整 JSON-RPC 消息写入日志（会话模块的 logger 名为 `client`，不在 `mcp` 命名空间下）。连接前包装进程的 LogRecord 工厂，源自 `mcp` 包源文件的记录在生成时只保留 logger 名字、级别与异常类型。处理器分别挂在 root、`mcp`、`mcp.client.streamable_http`（接入前）与 `mcp.client`（接入后），DEBUG 级别运行成功调用、远端错误，以及远端夹带未知通知（日志参数）、参数无效的通知与畸形消息（异常文字）：工具参数、禁止字段、伪造证据、注入文字、远端错误内容、夹带内容与假凭据均不出现，固定信息与异常类型照常输出。
- 证据指纹包含规范化的结果 schema（去掉标题与说明文字），不含工具说明：真实 PostgreSQL 中结果字段改型后旧证据不可读，只改说明文字时照常可读。
- 超时（0.5 秒，远端 3 秒）在期限内失败，远端只执行一次、不重试，之后的调用照常；调用中取消本轮时抛出取消，远端调用不重放。退出后工具集合为空，服务端连接数归零，没有遗留 asyncio 任务。
- 配置拒绝：非 loopback 的 HTTP、`localhost` 名字、用户信息、查询参数、原始凭据、`local` 或含下划线的 `server_id`、空工具表、非法或过长的工具名、非正期限。

**Task 4 缺口：** 工具列表只在启动时发现一次，远端此后改动工具由每次调用的结果模型校验兜底，参数 schema 不再复核；远端输出 schema 不比较；schema 比较只覆盖平铺参数，嵌套 `$defs` 名字不同会保守隐藏；越出端点或压缩编码在响应头阶段被拒绝时会关闭该 Server 的连接，之后调用都失败，需重启恢复（失败方向安全）；Server 运行中断开不会自动重连或隐藏工具；认证只支持静态 Bearer，OAuth 与其他 transport 未实现；MCP 日志约束通过进程级 LogRecord 工厂实现，之后替换工厂而不串联原工厂的代码会解除约束，Task 5 装配日志时须保持；日志调用的 `extra` 字段在工厂之后写入记录，锁定版 MCP 客户端不使用 `extra`（只有服务端与 CLI 使用）；SDK 的 `OPENAI_AGENTS_DONT_LOG_TOOL_DATA` 被显式关闭时 SDK 自身会记录工具输入输出，属运行配置，未在此处强制；映射等严格模式不支持的参数形状不开放，需要时另行验证非严格模式与各模型；远端 schema 中值为 schema 的 `additionalProperties` 视为不符，这一分支没有 loopback 用例；认证引用解析失败与连接失败同样只记录日志，未进入就绪检查（Task 5/P1-B）；远端执行约束依赖远端自身，客户端治理不能证明远端只读；没有接入任何真实外部 MCP Server。修复版本经复审后由 PR #6 合入。

**Task 3 缺口：** 用户输入准入只按可信配置的字节上限与禁止模式判断，不能识别模式之外的敏感内容，也不对模型自身知识写出的文字做模式检查；准入策略与 Profile `data_policy_id` 的对应由 Task 5 装配；Evidence 的 Session 列现与模型列内容相同（保留列结构，未另做迁移）；`ToolRequest.tool_name` 由应用的工具包装从 SDK 上下文填入，治理层不另行核对它与 `tool_id` 的对应；（参数默认值已在 Task 5 审查修复中禁止，证据摘要改为有效参数，此项不再适用）；任一证据失效即整段历史不可回放，撤权或证据保留期短于会话保留期时会话提前结束（有意的保守选择）；推理项不保存，真实推理模型经 Responses 的无状态续轮需实测；单轮用户输入大小只在提交时计入历史字节，模型输入前的单轮上限属 Task 5；同会话互斥与轮次结束清理由 Task 5 应用入口负责，`PolicySession` 只以状态比较交换防止重复提交；SDK 的 RunState 恢复路径（`get_items(limit)`）不支持；物理清理命令与渠道新建会话属 P1-B。

**Task 2 缺口：** 代码不能识别自由文字，渠道边界靠“模型只看得到渠道允许的数据”保证，用户自己输入或模型自身知识不在此约束内；复核与交给 SDK 之间仍有极短的检查-使用窗口；撤权后已写入的证据行保留到过期（不可读）；每条证据仍保存另一渠道的投影（永不可读，可在清理任务中收窄）；预算计数在进程内，依赖 Task 5 在每轮结束调用 `end_turn`；渠道展示仍是 JSON 投影，表格/摘要格式属 P1-B；证据物理清理命令属 P1-B；授权回调是应用接口，真实权限来源未接入。

**Task 1B 缺口：** OpenAI 与 DeepSeek 尚无真实 API 证据；Gemini 只有下文记录的部分证据，尚未完成工具续轮到交付与 Session 追问。`parallel_tool_calls=false` 只是请求参数，供应商不遵守时仍会返回多个调用（Task 2 原子预算兜底）；真实供应商若返回 `stop`/`tool_calls` 以外的正常终态会被拒绝，需实测确认；`strict` 工具、JSON mode 与推理字段的供应商兼容性仍未完整实测。SDK 客户端把上游错误体写进异常消息；Task 5 应用出口已映射为固定的 `model_failed`。流式调用未验证。

**Task 1 未覆盖、留给后续任务：** `tool_input_guardrails` / `tool_filter` 未验证（Task 4 不依赖它们）；真实模型后来只有 Gemini 部分证据，完整闭环仍是 Gate 0。显式开启 tracing 时的字段限制未验证。

CI integration job 设置 `SDK_TEST_POSTGRES_URL`，启动 `compose.sdk-test.yml`，运行完整 `python -m pytest -q`，并用 shell `EXIT` trap 清理测试项目；不保留旧 PostgreSQL service。SDK 地址缺失时 fixture 明确失败，避免数据库测试静默跳过。

**真实模型尝试（2026-09-30，Gemini，Chat Completions `https://generativelanguage.googleapis.com/v1beta/openai`，`json_schema`，免费额度密钥经 `env:` 引用）：** 验证脚本在仓库外，用真实 `Application` + `open_model` + 合成本地工具 + 隔离测试 PostgreSQL，固定 4 个样例（查询→同会话诊断追问、诊断轮要求执行查询、模糊问题）。

- 已观察：`gemini-2.5-flash`、`gemini-2.5-pro` 对新用户返回 404（不再开放）；`gemini-3.7-flash` 在最小请求下返回合规的 `AgentAnswer`；正式样例中模型选择 `order_total(region=east)`，经治理层进入 Adapter 并生成证据；Gemini 3 工具调用携带的 `thought_signature` 在同轮续调用中由 SDK 原样回传；模型拿不到所需字段时反复调用工具，被工具预算与轮次上限终止为 `turn_limit`；上游 404/503/429 均映射为固定的 `model_failed`，不重试、不含上游内容。
- 未完成：没有一轮到达交付（工具结果后的类型化回答被 503 中断，之后免费额度耗尽返回 429）；追问时 Session 回放是否保留签名、Gemini 是否接受回放历史，诊断轮隐藏查询工具与澄清表达均未观察到。
- 发现：测试用 `PROJECTIONS` 的模型可达投影只有 `rows`，模型看不到 `total`，不适合作为真实模型样例（脚本另配投影）；3.x 模型在此期间多次 503。

**P1-B 计划审查：** Claude 对 `df6aad1`、`e3f9702`、`41adc3f` 做了三轮独立审查，`41adc3f` 通过（开工前阻断清零）。该轮另提 3 项验收事项未写入计划，实施对应任务时落实：Task 3 启动容量检查直接用最坏情况的合成结果调用真实投影器，不另写算术公式；Task 3 在 `ToolPolicy` 声明必需字段，由投影器报告省略，不在 `EvidenceStore` 按工具 ID 特判；Task 4/8 所有请求状态迁移按期望前一状态条件更新，失锁的旧实例条件更新失败即 readiness false 并退出，并写明持锁连接探测间隔。计划修订时 `git diff --check`、计划相对链接、`tests/security/test_docs_command_consistency.py` 与 `tests/contract/test_doc_fact_binding.py` 通过。

**Gate 0 离线部分已证明（HTTP mock、真 Runner、`open_model` + 真实 `Application`、`PolicySession` + 隔离 PostgreSQL）：**

- 固定 4 个样例（查询、同会话追问、诊断轮要求执行查询、模糊问题）经产品路径跑通：查询工具执行 1 次并交付引用证据的回答；追问回放上一轮工具调用、不重跑工具并引用上一轮证据；诊断轮只展示 `list_regions`，模型强行调用 `sales_total` 时本轮失败、查询零执行；模糊问题得到澄清。专用策略四种投影都含 `total` 与 `rows`。
- 按 Gemini 3 OpenAI 兼容协议形状的 mock：本轮续调用时锁定 SDK 把 `extra_content.google.thought_signature` 原样带回（mock 对缺签名返回 400）；追问请求中回放的历史工具调用**不带签名**（`PolicySession` 保存形式只有 `call_id/name/arguments`）。若供应商也校验历史签名，追问在首个模型请求失败（400 → `model_failed`），工具不重跑——这正是 Gate 0 真实运行要判定的问题。
- 真实模型命令 `scripts/gate0_real_model.py` 复用同一装配，只把最底层 transport 换成不重试、不读环境配置的网络 transport；输出 JSON 只含 Profile 摘要/指纹、SDK 版本、每个样例的判定、状态码、耗时、展示的工具名、签名计数与可取得的 usage，不含消息、模型文字、证据标识、工具结果或凭据。Profile 文件缺失/不合法、凭据引用未设置、测试 PostgreSQL 未设置或不是唯一声明的实例时退出码 2；计入 Gate 的三个样例未全部通过时退出码 1。命令在任何 SDK 导入前强制两项 `OPENAI_AGENTS_DONT_LOG_*` 为 1，外部预置 `0/false` 的子进程用例验证生效。
- 判定只认直接证据（PR #9 独立审查 `725473c` 的 2 个阻断已修复）：没有检查项的样例不算通过；三个计入判定的样例必须各出现一次；查询轮的模型请求中工具结果须含 `total` 与 `rows`（按字段名白名单观测），追问回放的工具结果同样须含二者，且两种工具都零调用；诊断轮须至少发出一次模型请求、每次都展示 `list_regions` 而不展示 `sales_total`。mock 只依据模型实际收到的工具结果作答，看不到总额时只能澄清。usage 只记录 `prompt/completion/input/output/total_tokens` 五个整数计数，供应商返回的其他键丢弃。Profile 文件不是合法 UTF-8 时退出码 2、固定错误信息。
- 反向验证：转发前剥掉本轮签名、诊断用途加入查询工具、追问脚本重跑工具、去掉日志开关强制；审查修复后另 7 项（无检查项即通过、判定不核对样例集合、不核对模型是否看到 `total/rows`、诊断轮不要求模型请求与元数据工具、追问不计元数据工具调用、usage 不按白名单、不捕获 UTF-8 解码错误）。对应用例均失败。

**Gate 0 未覆盖：** 没有获准 Profile 与凭据，未对任何真实端点运行；mock 只证明本应用与锁定 SDK 在该协议形状下的行为，不证明任何供应商接受回放历史、遵守 `parallel_tool_calls=false` 或给出可用的回答质量。样例数据与 Web 渠道固定，飞书渠道投影未在 Gate 0 中运行。

**Task 1 SQLGuard 离线已证明（纯函数，锁定 sqlglot 30.17.0）：** `guard_readonly_query(sql, QueryPolicy) -> GuardedQuery`，拒绝抛 `QueryRejectedError`，只带 10 个 `QueryRejectionCode` 之一与固定说明，切断异常链。

- 先分词：字节超限、空输入、注释/hint、多语句、非 `SELECT`/`WITH` 开头在解析前拒绝。非 SELECT 语句在 sqlglot 中会回退为 `Command` 并把原文写进 sqlglot 日志，因此必须在解析前挡住；用例断言拒绝时日志不含输入 canary。
- 解析后按节点闭集与参数位置检查：集合运算、递归 CTE、窗口、变量/占位符、锁、hint、`INTO OUTFILE`、表函数/`FILES()`、`NATURAL`/`USING`、分区/索引 hint/时间旅行、列别名列表、十六进制与 `COLLATE` 等都拒绝；函数按 sqlglot 规范名（大写）过 allowlist，覆盖 `Anonymous`、`Cast`（含 `DATE '…'`）、`If`、`COUNT(*)`；`CASE WHEN` 不需要 `IF`。
- 物理表按 sqlglot scope 与 CTE 区分，只允许默认 database 中的获准对象；再用 `qualify` 按“只含获准列”的 schema 完整限定。`qualify` 不校验 HAVING/ORDER BY 中的未限定名，因此逐 scope 遍历全部列复核；ORDER BY 输出别名替换为别名所指表达式，其余未限定名拒绝，执行的 SQL 中不留需要数据库再解析的名字。相关子查询、跨来源歧义列拒绝。
- 顶层 LIMIT 缺失或超过 `max_rows` 改为 `max_rows + 1`，较小值保留；`OFFSET` 与非整数字面量拒绝。规范化 SQL 同样受字节上限约束，往返幂等。`GuardedQuery` 只能由 SQLGuard 构造。
- 标识符按 sqlglot 的 StarRocks 语义大小写敏感比较：大小写、全角、同形字差异只会失败关闭。StarRocks 列名实际大小写不敏感，因此模型写错大小写会得到“列未获准”，可在本轮修正。
- 反向验证 15 项（改用 `Scope.columns`、放行任意函数、去掉 LIMIT 改写/物理表检查/相关子查询检查/多语句检查/SELECT 开头检查/星号检查/参数位置检查/歧义检查/别名内联/规范化长度检查/异常链切断/构造封印/注释 hint 词元检查）均使用例失败。节点级 `comments` 检查经变异证明被词元检查完全覆盖，已删除。

**SQLGuard 未覆盖：** 未接入 `GovernedTools` precheck 与预算（Task 3）；零 I/O 的 recording pool 断言属于 Task 2/3；未对真实 StarRocks 验证规范化 SQL 的执行语义（G1、Task 9）。

**下一项：** Task 1 独立审查（精确 SHA 见 PR），通过后开始 Task 2 StarRocks Adapter 的离线部分。Gate 0 真实运行可并行：用户提供并授权一个有额度的 Profile（不含凭据的 `ModelProfile` JSON + 本机 `env:` 凭据引用）后运行：

```bash
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \
  uv run --locked --extra dev python -m scripts.gate0_real_model --profile PROFILE.json
```

记录输出中的判定、状态、耗时与 usage。计入判定的三个样例（查询、同会话追问、诊断轮隐藏查询工具）全部通过后才开始 Task 3（用户 2026-09-30 批准：Gate 0 只阻塞 Task 3–9，Task 1–2 离线先行）；若追问因历史签名等供应商字段失败，先做计划中的 Session 保存形式前置修复（精确字段 allowlist、类型与容量限制，篡改/超长/非 allowlist 字段离线回归），再复验。

P1-A 是内部核心。P1-B 才接真实查询与双入口并切换正式入口，P2 增加诊断，P3 做实际用户验收。Gate 0 是 Task 3 开工前例外；其他环境缺失不阻塞不依赖该环境的离线部分，但不能跳过对应实战退出条件。

## 4. 实测环境与兼容性缺口

| 对象 | 当前状态 | 实测前所需信息 |
| --- | --- | --- |
| OpenAI 模型 API | Responses 路径已实现，HTTP mock 通过；未实测 | 获准端点/协议/模型 ID、凭据安全引用、数据范围与预算 |
| Gemini 模型 API | Chat Completions 路径已实现，HTTP mock 通过；真实 API 部分验证（工具选择、同轮签名回传、类型化回答），闭环因 503/429 未完成；2.5 系列对新用户不可用 | 有额度的密钥；补完工具续轮到交付、追问回放与诊断范围 |
| DeepSeek 模型 API | Chat Completions + json_object 已实现，HTTP mock 通过；未实测 | 同上；单独验证 JSON mode、工具与推理参数组合 |
| 外部 MCP Server | 只有 loopback 测试 fixture；未接入任何真实 Server | 具体 Server、端点、认证方式、工具的实际行为与返回契约、网络与凭据边界 |
| PostgreSQL / SDK Session | 隔离测试库已验证 SDK 表与应用表 v1 初始化、版本检查、Evidence 读写，以及 Session 策略包装的暂存提交、回放复核、上限、过期与失败隔离 | 物理清理命令（P1-B）、正式部署的保留期配置与备份恢复验证（P3） |
| StarRocks | 新产品未联调 | 目标版本、测试连接、只读账号、获准库表/视图、数据投影范围与简短业务口径 |
| 飞书 | 新产品未联调 | 应用与事件配置、获准租户/单聊用户、可信身份来源 |
| 本机 Web | 新产品未实现 | P1-B 落实正式启动、身份/会话边界及浏览器实测 |
| Docker Compose | 新产品双容器尚未交付 | 应用镜像、PG 持久卷、loopback/SSH 访问、启动检查与备份恢复实战 |

凭据只在本机或获准部署环境安全配置，不粘贴到对话、仓库或日志。未提供的环境信息不是用户已授权向任意服务发数据。

## 5. 验证记录与限制

Task 1 验证环境：本仓库 `.venv`，Python 3.11.16。测试 PostgreSQL 由 `compose.sdk-test.yml` 启动（`postgres:16.15-bookworm@sha256:bb3e1a57…d825`，`cluster_name=xiaowei-sdk-test`，只绑定 `127.0.0.1:55432`，数据在 tmpfs）。本机没有 `docker compose` 插件，使用独立的 `docker-compose` 5.5.1。

下表是修复后实际执行的完整命令（`$SDK_PG` 即 `postgresql+asyncpg://postgres@127.0.0.1:55432/postgres`，`$AUDIT` 是临时目录中的导出文件路径）：

| 命令 | 结果 |
| --- | --- |
| `uv sync --locked --extra dev` | 通过；首轮提交时曾删除 `.venv` 从零重建 |
| `docker-compose -p xiaowei-sdk-test -f compose.sdk-test.yml up -d --wait` | healthy；`config -q` 通过 |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core -q` | 28 passed；没有残留的 `xw_sdk_test_*` 数据库 |
| `env -u SDK_TEST_POSTGRES_URL uv run --locked --extra dev python -m pytest tests/sdk_core -q` | 23 passed，5 errors（需要 PostgreSQL 的 5 个用例明确失败，不跳过） |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q` | 1742 passed，152 skipped（旧集成测试使用另一个变量 `PYTEST_POSTGRES_DSN`，未设置时跳过，属原有行为） |
| `uv run --locked --extra dev ruff check .` | 通过 |
| `uv run --locked --extra dev mypy src` | 通过（105 个文件） |
| `uv run --locked --extra dev mypy src/xiaowei` | 通过 |
| `uv export --frozen --no-emit-project --extra dev -o $AUDIT` 后执行 `uv run --locked --extra dev pip-audit --strict -r $AUDIT` | 没有已知漏洞。直接运行 `pip-audit --strict` 会因为本地项目不在 PyPI 而失败，必须先导出 |
| `docker-compose -p xiaowei-sdk-test -f compose.sdk-test.yml down -v` | 清理测试容器与网络 |

本轮 CI 同步验证：`tests/contract/test_integration_gate.py` 与 `tests/security/test_workflow_policy.py` 共 33 项通过；真实 SDK 测试库上的全量 pytest 为 1742 passed、152 skipped；`ruff check .`、相关文件格式检查、`mypy src` 与 workflow YAML 解析通过。容器由退出清理路径移除。

Task 1B 验证（同一环境，锁文件未变）：

| 命令 | 结果 |
| --- | --- |
| `uv run --locked --extra dev python -m pytest tests/sdk_core/test_model_api.py -q` | 实现前因缺少接口在收集阶段失败；首轮 44 passed。审查修复前新增用例 22 项按预期失败，修复后 70 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core -q` | 98 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q` | 1812 passed，152 skipped；无残留测试库 |
| `uv run --locked --extra dev ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src` | 通过 |

Task 2 验证（同一环境，锁文件未变）：

| 命令 | 结果 |
| --- | --- |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_governance.py tests/sdk_core/test_evidence.py -q` | 实现前因缺少 `xiaowei.evidence` 在收集阶段失败；首轮 27 passed。审查修复前新增/调整用例 9 项按预期失败（模型收到撤权后的 `total=100`、飞书交付含模型专属标记、策略收窄后旧证据仍可读），修复后 36 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core -q` | 134 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q` | 1848 passed，152 skipped；无残留测试库 |
| `uv run --locked --extra dev ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src` | 通过 |
| `uv build --wheel`（输出到临时目录） | wheel 含 `xiaowei/migrations/001_initial.sql` |

Task 3 验证（同一环境，锁文件未变）：

| 命令 | 结果 |
| --- | --- |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_session_policy.py -q` | 移走 `session.py` 时收集阶段失败；首轮 30 passed。审查修复前新增/调整用例 7 项按预期失败（`region` 进入下一轮模型、`987654321` 落库、孤立历史被读取并调用模型、改名/改参数仍回放或暂存），修复后 43 passed。增量复审修复前新增用例 4 项按预期失败，修复后 47 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core -q` | 181 passed（pytest `-W error` 下同样通过） |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q` | 1895 passed，152 skipped；5 条警告来自旧网络隔离测试，原有行为 |
| `uv run --locked --extra dev ruff check src/xiaowei tests/sdk_core`、`ruff format --check`、`mypy src/xiaowei` | 通过 |

Task 3 反向验证 37 项见计划“Task 3 实测记录”，全部使对应用例失败。

Task 4 验证（同一环境，锁文件未变）：

| 命令 | 结果 |
| --- | --- |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_mcp_integration.py -q -W error` | 22 passed（含配置参数化）。实现先于测试；“超时后下一次调用照常”与审查修复的回归断言均先失败、修复后通过 |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core -q -W error` | 208 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q` | 1922 passed，152 skipped；5 条警告来自旧网络隔离测试，原有行为 |
| `uv run --locked --extra dev ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src` | 通过 |

Task 4 反向验证：首轮 30 项、首轮审查修复 13 项、复审修复 5 项，见计划“Task 4 实测记录”，全部使对应用例失败。

Task 5 验证（同一环境，锁文件未变）：

| 命令 | 结果 |
| --- | --- |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_app.py -q -W error` | 移走 `app.py` 时收集阶段失败；实现后 13 passed。首次运行时伪造证据的原因为 `content_rejected`（提交路径与输入拒绝同一异常），改为提交前单独校验后通过 |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core -q -W error` | 221 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q` | 1935 passed，152 skipped；5 条警告来自旧网络隔离测试，原有行为 |
| `uv run --locked --extra dev ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src` | 通过 |

Task 5 反向验证 21 项见计划“Task 5 实测记录”：20 项使对应用例失败（其中一项在补充用例后），1 项证明不承重并删除。

Task 5 审查修复验证（同一环境，锁文件未变）：

| 命令 | 结果 |
| --- | --- |
| 修复前新增用例（旧代码） | 参数 6 项、执行后失败 2 项（本地、MCP）、数据策略绑定 1 项均按预期失败：字符串数字被执行、NaN 在 I/O 之后才被拒绝、执行收到未规范化的 `1`、模型在同轮再次执行、收窄策略后追问照常执行 |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core -q -W error` | 232 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q` | 1946 passed，152 skipped；5 条警告来自旧网络隔离测试 |
| `uv run --locked --extra dev ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src` | 通过 |
| 反向验证 | 审查修复 13 项全部使对应用例失败；首轮 20 项在新的 HTTP 装配下重跑，全部仍失败 |

Task 5 增量复审修复验证（同一环境，锁文件未变）：

| 命令 | 结果 |
| --- | --- |
| 修复前新增用例（`cd79114`） | 7 项按预期失败：`open_model` 仍接受任意密钥；schema 伪装禁止的根/嵌套额外参数与伪装必填的根/嵌套默认值都被执行；嵌套结果中的远端字段进入模型输入；证据拒绝等价的有效参数 `EAST` |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core -q -W error` | 237 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q` | 1951 passed，152 skipped；5 条警告来自旧网络隔离测试 |
| `uv run --locked --extra dev ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src` | 通过 |
| 反向验证 | 8 项全部使对应用例失败：凭据不按 Profile 引用、参数不强制禁止额外字段、不检查必填、不递归嵌套模型、结果不强制忽略额外字段、证据记录原始请求、核对历史不规范化、执行收到原始参数 |

Task 5 第三至五轮复审修复验证（同一环境，锁文件未变）：

| 命令 | 结果 |
| --- | --- |
| 修复前新增用例 | `bdae614` 上 10 项、`0ce5b2f` 上 7 项组合与 2 项登记边界按预期失败（见 Git 历史）；`f79bba3` 上 15 项按预期失败：根/嵌套子类的 `privileged` 进入参数，无关模型、字典根与缺字段漏出 `AttributeError`，枚举动态成员的 Infinity 在 Adapter 执行后才因摘要失败，联合类型（基类在前、列表、映射、MCP 结果）丢掉子类字段，Infinity 的 Literal/枚举成员可登记，MCP 根对象替换使禁止内容进入模型，旧规则证据仍可回放 |
| 复审 45 项矩阵（`f79bba3` 上 10 项失败） | 44 passed；余下 1 项期望根对象换成子类时投影回登记字段，现实现为受控拒绝（见计划） |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core -q -W error` | 278 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q` | 1992 passed，152 skipped；5 条警告来自旧网络隔离测试 |
| `uv run --locked --extra dev ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src/xiaowei`、`git diff --check` | 通过 |
| 反向验证 | 第五轮 6 项均使对应用例失败：根按运行时类型取字段、根改为 isinstance、嵌套改为 isinstance（补 `bool` 充当整数用例后）、缺字段不转换、去掉有限数值检查、规则版本不升级。另 2 项证明不承重并删除：容器的完全类型检查、运行时再检查 Literal 选项（登记时已核对） |

Task 2 反向验证：分别去掉撤权复核、调用时范围检查、展示范围过滤、目标一致性、参数校验、预算预占，把预算检查与计数拆到 `await` 两侧，保留执行异常原因链，去掉契约 schema 比对、SQL 归属条件、过期/目标/授权/渠道检查，改为投影全部字段、忽略投影上限或来源截断，去掉回答的引用子集/非空/去重/澄清混用检查、`AgentAnswer` 或 RunContext 的 `extra="forbid"`、应用表版本/存在检查、已安装判断，共 26 项；审查修复后另加 8 项：去掉写入后复核、模型或 Session 的渠道约束（分别及同时）、较小上限、策略指纹比较，指纹去掉投影或契约部分。34 项对应用例均失败。可信类型的 `strict` 经变异证明不承重，已删除。

Task 1B 反向验证：分别改为沿用 SDK 请求头，或去掉密钥固定、`identity` 编码、压缩检查、逐块计数、默认 transport 的 `trust_env=False`、Chat 终态检查（含只查第一个 choice、允许空 choices、允许 `length`）、JSON 解析检查、`parallel_tool_calls=False`、请求上限、`follow_redirects=False`、`store=False`、`preserve_raw_usage`、`json_object` 委托、推理强度白名单、HTTPS 校验，对应用例均失败；媒体类型与声明长度检查经变异证明分别被 JSON 解析与逐块计数覆盖，已删除。重试与期限各有两层（SDK 设置与客户端），只去掉一层时测试仍通过；两层同时去掉时 429/503/超时与期限断言失败。

Task 1 反向验证（临时改动，验证后已恢复）：

- 去掉 tracing 两步中任一步、MCP 前检查或结果过滤中任一项，对应用例都失败。
- 冒名实例：在 55432 端口起一台没有 `cluster_name` 的普通 PostgreSQL，夹具在建库前失败，冒名库上 `xw_sdk_test_*` 为 0；去掉实例核对后，同一用例在冒名库上建库成功。
- 查询参数绕过：55432 不启动服务，只在 55433 起一台带相同 `cluster_name` 的实例，DSN 加 `?port=55433`，夹具在连接前失败，那台实例上没有测试库。把 `parse_admin_url` 退回字段比较后，7 个反例（查询参数覆盖、密码、非法端口）变红。
- 去掉 `_verified_ddl` 中的核对后，`test_every_ddl_connection_verifies_the_instance_first` 变红。
- 把删库移出 `finally` 后，`test_database_is_dropped_when_the_body_raises` 变红，并确实留下了测试库（验证后已手工删除）。
- 往 `src/xiaowei` 临时加入 `import asyncpg` 和 `type: ignore` 注释：修复前两个护栏都通过（假绿），修复后都失败。

测试代码的类型检查不属于必需检查：`mypy --explicit-package-bases src/xiaowei tests/sdk_core`（`MYPYPATH=src`）只报一处 `yaml` 缺少类型存根，与现有 `tests/contract/test_compose_contract.py` 情况相同，未为此增加依赖或放宽配置。本机正在运行的旧 `xiaowei-release` 容器未被触及。

Gate 0 离线部分验证（同一环境，锁文件未变）：

| 命令 | 结果 |
| --- | --- |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_gate0.py -q -W error` | 首轮 6 passed；审查修复前新增用例在收集阶段因缺少接口失败，修复后 19 passed（含审查的两个反例：诊断轮无模型请求、查询投影只剩 `region`；未知 usage 键 canary；非法 UTF-8 Profile） |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core -q -W error` | 首轮 284 passed；审查修复后 297 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q` | 首轮 1998 passed，152 skipped；5 条警告来自旧网络隔离测试（审查修复只改 Gate 0 三个文件，未重跑全量） |
| `uv run --locked --extra dev ruff check .`、`ruff format --check src/xiaowei tests/sdk_core scripts/gate0_real_model.py`、`mypy src`、`git diff --check` | 通过；新增三个文件另以 `MYPYPATH=src mypy --explicit-package-bases` 检查通过 |
| 未设置凭据引用时运行 `python -m scripts.gate0_real_model --profile <示例 Gemini Profile>` | `gate0: 环境变量 XIAOWEI_GATE0_GEMINI_KEY 未设置或为空`，退出码 2，未发出请求 |

SQLGuard 验证（同一环境，锁文件未变）：

| 命令 | 结果 |
| --- | --- |
| `uv run --locked --extra dev python -m pytest tests/p1b/test_p1b_sqlguard.py -q` | 实现前收集阶段因缺少 `xiaowei.sqlguard` 失败；实现后 159 passed |
| `uv run --locked --extra dev python -m pytest -q --ignore=tests/sdk_core`；`-m security` | 1873 passed，152 skipped；923 passed，79 skipped |
| `ruff check .`、`ruff format --check src/xiaowei/sqlguard.py tests/p1b`、`mypy src`、`git diff --check` | 通过 |

独立审查：Codex 审查 `bf8963d`、`6dbbb6b`、`5aee8f5` 均为暂不通过，复审 `8dba33a` 为本地技术验收通过。本轮只同步 CI 与对应合同测试，未将自查称为独立审查；GitHub CI 是独立执行证据，不代表真实服务或产品运行。真实模型只有 Gemini 部分证据（未完成闭环），尚无真实 StarRocks、正式浏览器、飞书运行或用户验收证据，未来生产 Action 仍只有设计约束。
