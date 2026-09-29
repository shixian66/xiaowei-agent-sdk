# 小维：当前交接

> 更新：2026-09-29，Asia/Shanghai。这里只记录当前事实、证据与下一项工作；设计见 [ARCHITECTURE.md](ARCHITECTURE.md)，路线见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)，协作规则见 [AGENTS.md](AGENTS.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 1. 当前快照

| 项目 | 已核对事实 |
| --- | --- |
| 仓库 | [shixian66/xiaowei-agent-sdk](https://github.com/shixian66/xiaowei-agent-sdk) |
| 本地目录 / 分支 | `/Users/kloenguyen/Desktop/agent-SDK` / `claude/p1a-task3-session-policy`（从 `main` 的 `f8aadb1` 分出；`main` 已包含 Task 1、1B 与 2） |
| M5 历史起点 | `372c381f44ecfa1fa53961f137d0058033cbd805`；不是远端当前 main 的核验结论 |
| 本次实施起点 | `f8aadb1351ba79419fa8debbf7a09e34136bc2fc`（PR #4 合并后的 `main`）；开始时工作树干净 |
| 当前阶段 | P1-A Task 1（PR #2）、Task 1B（PR #3）与 Task 2（PR #4，审查修复后合入）已在 `main`；Task 3（Session 写入前与回放前策略）首轮审查（`5004951`）的 3 项 P1 中两项已由增量复审（`11f9b13`）确认关闭；剩余模型可达数据一项的同根缺口（投影分别计算、用户输入无准入策略）已修复，待再次增量复审 |
| 当前源码与依赖 | 新包 `src/xiaowei/`（`config.py`、`storage.py`、`model_api.py`、`models.py`、`governance.py`、`evidence.py`、`session.py`、`migrations/001_initial.sql`）与旧 `src/xiaowei_agent/` 并存；锁定 `openai-agents[sqlalchemy]` 0.22.3、`mcp` 2.2.0、`openai` 3.20.0、SQLAlchemy 2.0.52、asyncpg 0.30.0，Python 3.11.16；wheel 同时打包两个包，CLI 仍指向旧包 |
| 新产品入口 | 只有开发验证命令（见第 5 节）；尚无产品启动入口，旧 CLI/Compose 不算新入口 |
| 本次工作范围 | Task 3：`PolicySession`/`SessionLimits`、应用表 `xiaowei_session`（并入 v1）、Evidence 的完整调用绑定（`tool_name`、参数摘要）、模型与 Session 共用一份投影（规则 `/3`）、用户输入准入策略 `SessionInputPolicy`，及 `tests/sdk_core/test_session_policy.py`；无依赖变更，未改 Compose、CI 或 README |
| 外部操作 | 未调用真实模型、StarRocks 或飞书；Task 3 只用合成工具、ScriptedModel 与隔离测试 PostgreSQL；分支未推送；没有部署或用户验收 |

表中的 SHA 是本次实施起点，Task 1 提交在其之后。接手先用 `git rev-parse HEAD` 和 `git status --short` 取得实际版本；本文件的修改历史由 Git 保存。

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

唯一详细计划：[P1-A：SDK 与治理执行核心](docs/superpowers/plans/2026-09-29-p1a-sdk-governed-core.md)。任务顺序为 **Task 1 → Task 1B → Task 2–5**。Task 1 经 PR #2、Task 1B 经 PR #3、Task 2 经 PR #4 合入 `main`（`f8aadb1`）。Task 3 在本分支完成离线实现；审查与增量复审意见已修复，待再次增量复审。与计划的差异记录在计划各任务的“实测记录”中。

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

- 工具展示后撤权：SDK 仍按模型请求调用，治理层在 I/O 前拒绝，recording adapter 零调用，模型收到固定拒绝信息。执行期间或证据写入期间撤权：执行 1 次，模型收到固定信息而无业务字段，同轮不重试。越出 Tool/Target Scope、目标不符、未登记工具、参数类型错误/缺失/多余时同样零 I/O，且不调用授权回调。
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

**Task 3 缺口：** 用户输入准入只按可信配置的字节上限与禁止模式判断，不能识别模式之外的敏感内容，也不对模型自身知识写出的文字做模式检查；准入策略与 Profile `data_policy_id` 的对应由 Task 5 装配；Evidence 的 Session 列现与模型列内容相同（保留列结构，未另做迁移）；`ToolRequest.tool_name` 由应用的工具包装从 SDK 上下文填入，治理层不另行核对它与 `tool_id` 的对应；参数有默认值而模型省略时，规范化摘要可能与执行参数不同，回放会保守拒绝；任一证据失效即整段历史不可回放，撤权或证据保留期短于会话保留期时会话提前结束（有意的保守选择）；推理项不保存，真实推理模型经 Responses 的无状态续轮需实测；单轮用户输入大小只在提交时计入历史字节，模型输入前的单轮上限属 Task 5；同会话互斥与轮次结束清理由 Task 5 应用入口负责，`PolicySession` 只以状态比较交换防止重复提交；SDK 的 RunState 恢复路径（`get_items(limit)`）不支持；物理清理命令与渠道新建会话属 P1-B；未经独立审查。

**Task 2 缺口：** 代码不能识别自由文字，渠道边界靠“模型只看得到渠道允许的数据”保证，用户自己输入或模型自身知识不在此约束内；复核与交给 SDK 之间仍有极短的检查-使用窗口；撤权后已写入的证据行保留到过期（不可读）；每条证据仍保存另一渠道的投影（永不可读，可在清理任务中收窄）；预算计数在进程内，依赖 Task 5 在每轮结束调用 `end_turn`；渠道展示仍是 JSON 投影，表格/摘要格式属 P1-B；证据物理清理命令属 P1-B；授权回调是应用接口，真实权限来源未接入。

**Task 1B 缺口：** 三家均无真实 API 证据；`parallel_tool_calls=false` 只是请求参数，供应商不遵守时仍会返回多个调用（Task 2 原子预算兜底）；真实供应商若返回 `stop`/`tool_calls` 以外的正常终态会被拒绝，需实测确认；`strict` 工具、JSON mode 与推理字段的供应商兼容性只由 mock 覆盖。SDK 客户端把上游错误体写进异常消息，原始异常尚未映射为受控失败（Task 5 应用出口负责）。流式调用未验证。

**Task 1 未覆盖、留给后续任务：** MCP HTTP 接收字节上限、认证、超时与关闭行为（Task 4）；`tool_input_guardrails` / `tool_filter` 未验证；真实模型（Task 5）。显式开启 tracing 时的字段限制未验证。

CI integration job 设置 `SDK_TEST_POSTGRES_URL`，启动 `compose.sdk-test.yml`，运行完整 `python -m pytest -q`，并用 shell `EXIT` trap 清理测试项目；不保留旧 PostgreSQL service。SDK 地址缺失时 fixture 明确失败，避免数据库测试静默跳过。

**下一项：** Task 3 的独立审查；需要时推送并开 PR，核对 CI 后单独批准合并，再从合并后的 `main` 开始 Task 4（最小 MCP Client Integration）。

P1-A 是内部核心。P1-B 才接真实查询与双入口并切换正式入口，P2 增加诊断，P3 做实际用户验收。环境缺失不阻塞独立离线任务，但不能跳过对应实战退出条件。

## 4. 实测环境与兼容性缺口

| 对象 | 当前状态 | 实测前所需信息 |
| --- | --- | --- |
| OpenAI 模型 API | Responses 路径已实现，HTTP mock 通过；未实测 | 获准端点/协议/模型 ID、凭据安全引用、数据范围与预算 |
| Gemini 模型 API | Chat Completions 路径已实现，HTTP mock 通过；未实测 | 同上；单独验证工具续轮、结构化结果与协议字段 |
| DeepSeek 模型 API | Chat Completions + json_object 已实现，HTTP mock 通过；未实测 | 同上；单独验证 JSON mode、工具与推理参数组合 |
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

独立审查：Codex 审查 `bf8963d`、`6dbbb6b`、`5aee8f5` 均为暂不通过，复审 `8dba33a` 为本地技术验收通过。本轮只同步 CI 与对应合同测试，未将自查称为独立审查；GitHub CI 是独立执行证据，不代表真实服务或产品运行。尚无真实模型、真实 StarRocks、正式浏览器、飞书运行或用户验收证据，未来生产 Action 仍只有设计约束。
