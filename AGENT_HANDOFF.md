# 小维：当前交接

> 更新：2026-10-03，Asia/Shanghai。这里只记录当前事实、证据与下一项工作；设计见 [ARCHITECTURE.md](ARCHITECTURE.md)，路线见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)，协作规则见 [AGENTS.md](AGENTS.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 1. 当前快照

| 项目 | 已核对事实 |
| --- | --- |
| 仓库 | [shixian66/xiaowei-agent-sdk](https://github.com/shixian66/xiaowei-agent-sdk) |
| 本地目录 / 分支 | 本轮独立工作树 `/private/tmp/xiaowei_p25_t1`，`claude/p25-task1-target-routing`，自 `origin/main` `29678006f380621dda5ddb772b7affa072a59776`（PR #32 合入 Task 0 后）建立；未切换或修改 `/Users/kloenguyen/Desktop/agent-SDK` 及其他任务的工作区 |
| M5 历史起点 | `372c381f44ecfa1fa53961f137d0058033cbd805`；不是远端当前 main 的核验结论 |
| 规划基线 / 本地 main 引用 | 规划基线 `7a715ff61d7a97457b03bb8b596ef4238c1049f2`；本轮开始时获取的 `origin/main` 为 `29678006f380621dda5ddb772b7affa072a59776`，在 P2 离线退出（PR #30）与开发规则（PR #31）之上合入 P2.5 计划 v2 与 Task 0 证据（PR #32）；历史切片证据见第 3 节 |
| 当前阶段 | **P2 离线完成（Task 0–8 经独立审查合入，Task 8 见第 3 节 P2 部分）：真实部分（两端实战、真实模型诊断质量、目标版本的计划与审计源事实）按顺序调整在 P3 获准环境补齐；下一阶段为 P2.5（直连、读取/SQL 放开与强制执行前评估），其后单群增量，再到 P3；两份计划 v2 已在 `719c1c7` 通过复审，P2.5 Task 0 经复审随 PR #32 合入，Task 1 多目标路由已实现、待独立审查（见第 3 节下一步）。** **P1-B Task 1 SQLGuard 已随 PR #10 合入；Task 2 StarRocks Adapter 随 PR #11 合入（`dd24d8a`）。Gate 0 在主线 `6389d78` 上以 `glm-4.7-flash` Profile 真实运行通过；Task 3 受治理工具与结构化 Evidence 交付经复审通过，随 PR #13 合入（`5058ae3`）；Task 4 PostgreSQL v2 请求与渠道状态经两轮审查修复与复审通过，随 PR #14 合入（`e5e380c`）；Task 5 共享 ChannelService 经一轮审查修复（B1 授权来源对象身份）与复审通过，随 PR #15 合入（`98f4167`）；Task 6 最小同源 Web 经一轮审查修复（B1 不可用端口、B2 浏览器 smoke）与复审通过，随 PR #16 合入（`1d1aa01`）；Task 7 飞书单聊长连接（用户接受“先 ack、后落库”）经两轮审查修复（B1–B4、N1）与复审通过，随 PR #17 合入（`0c3161c`），真实飞书验证待授权；Task 8 正式装配、维护命令与打包切换经五轮审查修复与复审通过，随 PR #18 合入（`16f80d4`）：首轮审查 B1–B3、增量复审的 B2（接管交接竞态）与 B3（第三方 WARNING 与飞书 handler）、第三轮的 N1（会话创建/轮换、重复请求与投递权未进入所有权屏障）、第四轮的 N2（请求启动与 Session 恢复边界）与 N3（投递尝试归属）、第五轮的 N3.1（重发所有者连接退出后仍留在连接池）均已修复。Task 9 离线部分已在 `16f80d4` 上完成，真实部分按用户决定推迟到 P3 部署后。**P1-A 离线完成（Task 1–5 经 PR #2–#7 合入）。真实模型：Gate 0 通过的 Profile 只有 `bbtoken-glm-4.7-flash`；Gemini 只有 2026-09-30 的历史部分证据（未闭环）；同端点 DeepSeek V4 Flash 已实测不兼容 |
| 当前源码与依赖 | 新包 `src/xiaowei/` 含 `sqlguard.py`、`starrocks.py`、`starrocks_tools.py`、`channel_store.py`、`migrations/002_p1b_channels.sql`、`migrations/003_delivery_attempt.sql`（应用表 v3）、`channel.py` 、`web.py` 与 `static/`，`feishu.py`，以及 Task 8 新增的 `runtime.py`、`cli.py`、`__main__.py`；锁定 `asyncmy==0.2.15`、`lark-channel-sdk==1.4.0`（精确钉版，已加入依赖基线）。另锁定 `openai-agents[sqlalchemy]` 0.22.3、`mcp` 2.2.0、`openai` 3.20.0、SQLAlchemy 2.0.52、asyncpg 0.30.0，urllib3 2.8.0（间接依赖，修复 CVE-2026-97687/97688/97689），Python 3.11.16。Task 8 起 wheel 只含 `src/xiaowei`，`xiaowei` 命令指向 `xiaowei.cli:main`；旧包专用的 `alembic` 移出生产依赖（dev 与 `legacy` extra 保留），旧源码仍在工作树 |
| 新产品入口 | 主线：`xiaowei`（与 `python -m xiaowei` 相同）的 `serve`、`storage init/upgrade/cleanup`、`requests resend`，配置为 JSON 文件（示例 `examples/xiaowei.example.json`；本分支起为 `targets` 多目标格式，旧单目标格式启动时报迁移错误）。离线验证完成；真实模型、StarRocks 与飞书未参与。旧 CLI/Compose 不是产品入口 |
| 本次工作范围 | P2.5 Task 1「多目标配置与一套工具的准确路由」：`runtime`/`app`/`governance`/`tools`/`starrocks_tools`/`channel`/`evidence`/`mcp`/`models` 及相关测试、示例配置、README、计划 Task 1 与本文；不含自动 schema、SQL 放宽、风险评估或 Evidence 依赖（Task 2–5），依赖与锁文件无变化 |
| 外部操作 | 主线曾获用户授权用合成数据调用 Gemini API（见第 3 节），验证后本机凭据文件已删除，用户负责作废该密钥；Gate 0 离线部分与 SQLGuard 没有调用真实模型；全部工作均未调用飞书或外部 MCP Server。Gate 0 真实运行经用户授权使用第三方中转端点 `bbtoken.boywe.cn`（OpenAI 兼容 Chat Completions）发送合成数据，共 22 次模型请求（含诊断探测），另有 1 次模型列表查询，串行、不重试；凭据只写入仓库外权限 600 的临时文件，运行后已删除，用户负责作废该密钥。Task 2 从 Docker Hub 拉取官方 `starrocks/allin1-ubuntu:latest`（digest `sha256:faf7ce9c…276b`，StarRocks 4.1.4）在本机 127.0.0.1:59030 运行可丢弃容器，只写入随机名合成库，用后删除；未连接用户的 StarRocks。P2 Task 0 用同一本地镜像在 127.0.0.1:59030 运行可丢弃容器，从 `releases.starrocks.io/resources/auditloader.zip` 下载官方 AuditLoader 5.0.0（sha256 `cd2a8ace…8ea2`）只装入该容器，临时把 FE `query_explain_level` 改为 ANALYZE 后已恢复；容器与合成数据已删除。P2.5 Task 0 用同一 digest 新建可丢弃容器 `xw-p25-t0-sr`（只发布 127.0.0.1:59030/58030）与 `xiaowei-sdk-test` PostgreSQL，装入同一 AuditLoader 5.0.0；临时改 FE `query_explain_level`、`enable_statistic_collect_on_first_load` 并建极低阈值资源组，均已恢复/删除并回读；容器与测试库已删除，未触碰 `xiaowei-release-*`。P2.5 Task 1 只用本机 `xiaowei-sdk-test` PostgreSQL 与 StarRocks 驱动替身，未连接任何 StarRocks、模型 API、飞书或外部 MCP |

表中列出本轮规划基线与本地 main 引用，历史任务 SHA 只证明对应切片，不能混用。接手先用 `git rev-parse HEAD` 和 `git status --short` 取得实际版本；审查使用对应提交的精确 SHA，本文件的修改历史由 Git 保存。

## 2. 已确定的产品边界

- 按 SDK 原生方式从产品需求设计，允许从零开始；旧代码只按当前价值复用，不保留旧 Runtime 作为前提。
- 首版统一 StarRocks 查询与慢查询诊断；一个业务 Agent、单进程、Web/飞书渠道隔离。P2.5 Task 1 起支持多个 StarRocks 目标（一套工具、`cluster` 参数选择集群）；其余增量与单群范围只按 ARCHITECTURE §5–§7 的已确认增量推进。
- 通用 MCP 客户端保留；StarRocks 继续本地 Adapter 直连，数据库 MCP 暂缓，不把外部 Server 作为当前前提。
- 用户要求接入 Gemini、DeepSeek、OpenAI 等模型 API；运行核心仍固定为 SDK，具体模型与端点未选定。
- SDK Session、严格 RunContext、调用前治理、四种数据投影、最终 Evidence 验证、动态工具集合与默认关闭 tracing 按架构落实。
- 首版正式存储为 SQLAlchemySession + PostgreSQL，SDK 表与应用表独立归属；Docker Compose 部署小维与 PostgreSQL 两个常驻容器，数据使用持久卷，服务器 Web 通过 SSH 本地转发访问。
- 七项最小方案已确认：本轮查询/诊断范围、程序生成关键事实、简短业务口径、PostgreSQL 初始化/升级与备份恢复、会话上限/新建/过期清理、请求编号与基础健康检查、简单容器部署。详细契约和任务分工见架构与计划，尚未全部完成。
- 首版只读。未来生产变更统一为 Action，先展示具体内容并取得有权限用户明确确认，执行前重新检查 Policy / Approval / Action Binding；诊断不能自动升级为写。
- 开发采用小步实现、按风险验证和可接续交接；不把文档调优视为源代码实施、产品验收或生产操作授权。

详细规则不在本文重复展开，以架构及开发规则为准。M5、旧 ADR 与旧验收仅是历史材料，不作为新产品已经可用的证据。

## 3. 当前计划与下一项工作

**当前计划：** P2 由 [P2 诊断闭环计划](docs/superpowers/plans/2026-10-02-p2-explain-diagnosis.md) 实施，离线部分随 Task 8 收尾，进度与证据见本节末尾的 P2 部分；下一阶段为 [P2.5 直连实施计划](docs/superpowers/plans/2026-10-03-p25-open-read-multi-cluster.md)，再执行 [飞书单群计划](docs/superpowers/plans/2026-10-03-feishu-group.md)，其后 P3；新计划尚未实施。下文 P1-A/P1-B 的记录保留为已完成阶段的证据。

P1-A 实施事实保留在 [P1-A：SDK 与治理执行核心](docs/superpowers/plans/2026-09-29-p1a-sdk-governed-core.md)。已完成的下一切片记录在 [P1-B：真实只读查询与双入口](docs/superpowers/plans/2026-09-30-p1b-starrocks-dual-entry.md)，顺序为 **SQLGuard → StarRocks Adapter →（真实模型 Gate 0 须已通过）受治理工具/Evidence → PostgreSQL v2 → 共享 ChannelService → Web → 飞书 → 正式入口 → P1 实战退出**。产品边界仍以 `ARCHITECTURE.md` 为唯一权威，当前证据仍以本文为准。

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

**Task 5 缺口：** SDK 自身在模型错误时写 ERROR 日志，锁定版默认（`OPENAI_AGENTS_DONT_LOG_MODEL_DATA` 未设置）会隐去错误内容，已用例验证；显式关闭该默认时上游错误体会进入日志（对照已验证），应用未强制该运行配置；参数模型不能有默认值与可选字段，也不能用数据类、TypedDict 等结构；字段校验器改变取值时，校验器代码变化会使历史调用规范化结果变化，相应证据在回放时不可读（保守拒绝）；执行后失败会让整轮失败，模型不能自行换用其他工具继续；Task 5 当时真实模型只有 Gemini 的部分证据（见下文“真实模型尝试”），闭环后来由 Gate 0 以 `glm-4.7-flash` Profile 通过，OpenAI Responses 与 Gemini 仍未闭环；并发上限与同会话互斥在进程内，只拒绝不排队，多进程部署需另行设计（首版单进程）；总期限覆盖提交，期限或取消落在提交过程中时会话被隔离，只能新建；提交后到交付前撤权时本轮已保存但不交付，下一轮回放会因证据不可读而拒绝；最终结果与渠道投递状态的保存、Session 已提交而结果保存失败的双存储边界属 P1-B；阶段日志只经标准库 logging 输出，处理器、格式与保留未配置；MCP Server 不可用只减少 `available_tools`，未进入就绪检查；SDK 自身的 `OPENAI_AGENTS_DONT_LOG_*` 依赖默认值；用户消息大小由数据策略的输入准入上限约束；策略模型的 serializer 与计算字段不参与有效数据，需要派生值时由 Adapter 自己计算；after validator 不经校验改写的值只核对类型，`Field`/`Annotated` 的长度、范围等约束不重新检查；字段值类型不符只能在调用时发现，参数侧以固定的“参数不符合工具契约”交给模型；集合（`set`/`frozenset`）按迭代顺序输出，字符串集合的顺序随进程变化，证据回放可能因摘要不同而保守拒绝（此前的 `model_dump` 同样如此，未在本次处理）；模型调用失败（含上游 503）不重试，接真实服务时是否增加有限重试属产品取舍，未决。

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

**Task 1B 缺口：** OpenAI Responses 尚无真实 API 证据；DeepSeek 只在第三方中转端点实测过 V4 Flash（带输出格式时不调用工具，不兼容，见 Gate 0），官方端点未实测；Gemini 只有下文记录的部分证据，尚未完成工具续轮到交付与 Session 追问。`parallel_tool_calls=false` 只是请求参数，供应商不遵守时仍会返回多个调用（Task 2 原子预算兜底）；真实供应商若返回 `stop`/`tool_calls` 以外的正常终态会被拒绝，需实测确认；`strict` 工具、JSON mode 与推理字段的供应商兼容性仍未完整实测。SDK 客户端把上游错误体写进异常消息；Task 5 应用出口已映射为固定的 `model_failed`。流式调用未验证。

**Task 1 未覆盖、留给后续任务：** `tool_input_guardrails` / `tool_filter` 未验证（Task 4 不依赖它们）；完整闭环后来由 Gate 0 的 `glm-4.7-flash` Profile 通过（见下文）。显式开启 tracing 时的字段限制未验证。

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

**Gate 0 真实运行（2026-10-01，主线 `6389d78`，`openai-agents` 0.22.3 / `openai` 3.20.0）：** 通过的 Profile 是 `bbtoken-glm-4.7-flash`（provider `openai_compatible`、Chat Completions、模型 `glm-4.7-flash`、`json_object`，指纹 `sha256:471b836d…ef54f`），命令退出码 0：

- 查询：2 次模型请求（7.6 s / 722 tokens，34.5 s / 1078 tokens），工具执行 1 次，模型看到 `total` 与 `rows`，交付引用 1 条证据。
- 同会话追问：1 次请求（14.2 s / 1240 tokens），回放历史中的 2 个工具调用与结果，工具零调用，引用上一轮证据。
- 诊断轮：只展示 `list_regions`，查询工具零执行（判定通过）；该轮最终以 `model_failed` 结束，不计入判定。模糊问题（不计判定）：第二次请求被上游 429 限流，`model_failed`。
- 同一端点的 `bbtoken-DeepSeek-V4-Flash`（`json_object` 与 `json_schema` 两个 Profile）未通过：8 次请求工具调用均为 0。最小探测证明只要请求带 `response_format`，该模型就不调用工具（不带时正常调用）；SDK 在设置 `output_type` 时每次请求都带输出格式，因此该模型不能用于本产品路径。平台返回的实际模型名为 `/data/DeepSeek-V4-Flash-0731-INT8`。

**Gate 0 未覆盖：** 只有一个第三方中转端点上的一个模型通过，单次运行，不代表该模型的稳定质量或该平台的可用性（免费套餐有限流，模糊问题样例已遇到 429）；没有验证 Gemini 签名回放；飞书渠道投影未在 Gate 0 中运行。

**Task 1 SQLGuard 离线已证明（纯函数，锁定 sqlglot 30.17.0）：** `guard_readonly_query(sql, QueryPolicy) -> GuardedQuery`，拒绝抛 `QueryRejectedError`，只带 10 个 `QueryRejectionCode` 之一与固定说明；拒绝在下层 `except` 结束后才抛出，`__cause__` 与 `__context__` 都为空。预期的输入失败（sqlglot 的 `SqlglotError`、过深嵌套的 `RecursionError`、不可编码为 UTF-8 的孤立代理项）都映射为原因码，其余异常视为程序缺陷照常传播。

- 先分词：字节超限、空输入、注释/hint（先查全部词元，再去掉可选尾部分号：尾部注释附着在分号词元上）、多语句、非 `SELECT`/`WITH` 开头在解析前拒绝。原始 SQL 只交给分词器，解析器只收到词元：sqlglot 解析器用原文生成诊断和 `Command` 回退日志（`WITH … SHOW` 这类输入在 `WITH` 之后仍会触发回退）。用例断言拒绝时全部日志不含输入 canary，非 SELECT 语句不产生任何 sqlglot 日志。
- 解析后按节点闭集与参数位置检查：集合运算、递归 CTE、窗口、变量/占位符、锁、hint、`INTO OUTFILE`、表函数/`FILES()`、`NATURAL`/`USING`、分区/索引 hint/时间旅行、列别名列表、十六进制与 `COLLATE` 等都拒绝；函数按 sqlglot 规范名（大写）过 allowlist，覆盖 `Anonymous`、`Cast`（含 `DATE '…'`）、`If`、`COUNT(*)`；`CASE WHEN` 不需要 `IF`。
- 物理表按 sqlglot scope 与 CTE 区分，只允许默认 database 中的获准对象；再用 `qualify` 按“只含获准列”的 schema 完整限定。`qualify` 不校验 HAVING/ORDER BY 中的未限定名，因此逐 scope 遍历全部列复核；ORDER BY 输出别名替换为别名所指表达式，其余未限定名拒绝，执行的 SQL 中不留需要数据库再解析的名字。相关子查询、跨来源歧义列、重复的来源别名（sqlglot scope 抛 `OptimizeError`）拒绝。
- 顶层 LIMIT 缺失或超过 `max_rows` 改为 `max_rows + 1`，较小值保留；`OFFSET` 与非整数字面量拒绝。规范化 SQL 同样受字节上限约束，往返幂等。`GuardedQuery` 只能由 SQLGuard 构造。
- 标识符按 sqlglot 的 StarRocks 语义大小写敏感比较：大小写、全角、同形字差异只会失败关闭。StarRocks 列名实际大小写不敏感，因此模型写错大小写会得到“列未获准”，可在本轮修正。
- 反向验证 15 项（改用 `Scope.columns`、放行任意函数、去掉 LIMIT 改写/物理表检查/相关子查询检查/多语句检查/SELECT 开头检查/星号检查/参数位置检查/歧义检查/别名内联/规范化长度检查/异常链切断/构造封印/注释 hint 词元检查）均使用例失败。节点级 `comments` 检查经变异证明被词元检查完全覆盖，已删除。
- PR #10 独立审查（`b40f85a`）的 2 个阻断已修复：尾部分号后的注释/hint 曾被放行；重复来源别名与孤立代理项曾以原始异常逃逸，`WITH … SHOW` 曾把原文写进 sqlglot 日志，下层异常曾留在 `__context__`。隔离变异：注释检查挪回去掉分号之后、原文重新交给解析器、不转换 `OptimizeError` 或 `UnicodeEncodeError`、在 `except` 内抛拒绝，对应用例均失败；原有 15 项（另加 SELECT 开头检查以“不产生 sqlglot 日志”为证）重跑仍全部被捕获。

**SQLGuard 未覆盖：** 未对用户的 StarRocks 验证规范化 SQL 的执行语义（G1、Task 9）。

**Task 2 StarRocks Adapter 已证明：**

- 离线（recording 驱动替身，Adapter 其余代码全真实）：只执行本目标的 `GuardedQuery` 与代码生成、参数绑定的元数据查询；`describe_table` 与目标不符在 I/O 前拒绝。每次请求新建连接，先设置并回读 `query_timeout`、`query_mem_limit`、`time_zone`，失败即不执行查询。最多读 `max_returned_rows + 1` 行；总字节与单值按生产 JSON 编码（`ensure_ascii=False`）的 UTF-8 大小计数，覆盖控制字符、引号、反斜杠转义膨胀，放不下的行整行丢弃并标记截断。值只转为 `null/bool/int/有限 float/str`（Decimal 定点字符串，日期时间带目标时区的 ISO 8601），bytes、非有限数、`timedelta`、未知类型与重复列名拒绝。只有完整读完才正常关闭；截断、错误、超时、取消（含正常关闭过程中的取消）一律同步断开，取消照常传播，且不重连、不重试。等待槽位与建立连接共用一个绝对期限，超期阶段分别映射为 `pool_timeout` / `connect_failed`；客户端期限覆盖会话设置、执行与读取。返回的 `row_count` 是显式字段，出现在 schema 与序列化结果中，校验恒等于 `len(rows)`。错误映射为 11 个固定码，不带服务端原文，`__cause__`/`__context__` 为空。
- 连接不复用（与计划“池”的差异，已写入计划 §2.3）：asyncmy 自带池没有获取期限，且按 `connected` 回收，`close()` 后的连接仍可能回到空闲队列；每次新建连接保证中断或截断的连接不会被再次使用。
- 本机 StarRocks 4.1.4 容器（显式 `-m starrocks_real`）：以只读账号验证类型与时区、`LIMIT` 改写后的截断与后续查询、读到一半的字节截断、元数据只列出获准且有权限的对象与列、未授权表映射为权限拒绝（实测错误码 5203）、只读账号写入被拒、服务端 `query_timeout` 终止慢查询（实测 5024，单列为 `server_timeout`）、取消后槽位释放、TLS 开启连接明文服务端失败关闭（不降级）、错误密码映射为认证失败、`SSCursor` 逐行读取。
- 反向验证 15 项（行数上限、会话回读核对、总字节、单值、未读完时断开、槽位/连接/客户端期限、`describe_table` allowlist、目标核对、重复列、未知类型、在 except 内抛出、会话设置先于查询、5203 映射）均使用例失败；两项期限变异原本导致挂起，已给用例加外层期限使其快速失败。PR #11 审查修复另做 4 项（建连重新计时、关闭被取消时不断开、吞掉取消、去掉 `row_count` 一致性校验），均使对应用例失败。

**Task 2 未覆盖：** 未连接用户的 StarRocks（G1/G5）：TLS 证书验证只证明了“开启 TLS 不降级”，未对启用 TLS 的服务端验证证书链与主机名；grants、资源组与目标版本错误码需在获准环境复核。已知驱动噪音：截断或中止路径上，asyncmy `MySQLResult.__del__` 调用协程而不 await，产生 `RuntimeWarning: coroutine '_finish_unbuffered_query' was never awaited`；该协程不会运行、没有 I/O，产品代码未为此触碰驱动私有状态。

**Task 3 受治理工具已证明（真 Runner + `open_model` 装配的 mock 模型端点 + 真实治理/Evidence/Session + 隔离 PostgreSQL；StarRocks 驱动为 Task 2 的 recording 替身）：**

- 查询轮展示 `list_tables`、`describe_table`、`run_readonly_query`；诊断轮与只授权元数据时只展示前两个。模型给出的 SQL 经同步前置检查转为 `GuardedQuery`，驱动收到的正是规范化 SQL；结果经证据交给模型（同一组列与完整行），Web 交付带 `DeliveryFact`（列、行、实际 SQL、行数、来源、采集时间、截断），飞书交付为纯文本表格且不含结构化事实，单元格中的换行被转义，不能伪造“分析建议”段落。截断在模型、Web 与飞书中都标明；追问回放上一轮的行，查询不重跑。元数据工具只执行代码生成、参数绑定的查询。
- 前置检查拒绝（多语句、越权列/对象、星号、写语句、对象不在 allowlist、检查函数意外异常）时：模型只收到固定原因码与说明（不含 SQL、对象名或下层异常，`__context__` 为空），不占工具预算（上限为 1 时改正后的查询仍执行），驱动零连接、零证据。诊断轮强行调用查询、展示后撤权、参数越界（多余字段、错误类型）同样零 I/O。
- 前置检查通过后的执行失败（如 5203 权限拒绝）：本轮 `tool_failed`，模型不续轮，预算已消耗，不重试，无证据。错误配置下飞书投影放不下 `rows` 时 `EvidenceStore.record` 以“必需字段无法完整保留”中止，不生成证据。
- 启动容量检查：按声明上限构造的最坏结果直接交给真实投影器，Web 与飞书两条路径的模型可达投影和渠道投影都须完整保留全部字段；与 `json.dumps` 独立计算的阈值一致（恰好通过、少 1 字节失败）。控制字符、引号与反斜杠膨胀最大的真实 Adapter 结果不超过该阈值；服务端列名超出同一上限时按结果契约失败。
- `run_turn` 返回已提交的 `AgentAnswer`；提交后撤权时，交付经 `validate_answer` 拒绝，Session 已保存。伪造、跨会话、跨渠道证据与模型提交的 `facts` 字段都被拒绝。`BusinessContext` 进入 Agent instructions 与会话绑定：版本变化后旧会话在首个模型调用前拒绝；目标没有登记工具时拒绝装配。
- 本机 StarRocks 4.1.4 容器 + 测试 PostgreSQL（显式 `-m starrocks_real`）：只读账号经治理调用查询工具，越权列在前置检查拒绝；获准查询的 Decimal 与带时区的日期时间经证据成为 Web 结构化事实。
- 首轮审查修复（针对 `f018c8a`）：飞书纯文本每个单元格只占一行，列名、行值、标量与模型文字中的 `\r`、`\n`、U+0085、U+2028、U+2029 等分行或不可见字符写成 JSON 转义，单元格中的竖线写作 `\|`，表格行以 `| ` 开头、` |` 结尾，数据不能形成系统标题行或伪造列；Web 的 JSON 文本同样不含这些原始字符，`DeliveryFact` 保持数据库原值。`ToolPolicy.required` 进入策略指纹（投影规则升为 `/7`）：只把 `rows` 加入必需字段时，此前省略 `rows` 的证据在 model/session/web/feishu 投影、Session 回放与交付中均不可读，相同策略重建为成功对照。
- 反向验证 13 项（前置检查移到预算之后、不强制必需字段、容量检查不运行投影/只查 Web/最坏结果不用转义字符、模型可达投影不取渠道交集、飞书也给事实、飞书单元格不转义、读取不核对渠道、`describe_table` 不预检对象、业务口径不进绑定/不进 instructions、不检查列名上界）在最终代码上均使用例失败；去掉读取时的渠道核对后，证据查询本身仍按渠道过滤，由原有 `test_four_data_boundaries` 捕获。

**Task 3 未覆盖：** 真实模型只在 Gate 0 的合成工具上验证过，未用真实模型调用 StarRocks 工具；渠道入口、请求保存与重发属于 Task 4–7；飞书按消息长度分段属于 Task 7；投影规则升为 `/7` 后，升级前（含 `/6`）保存的证据不可再读（保留期内的旧会话追问会因证据不可读而拒绝）。飞书文本对含换行的长文字只做转义，可读性下降，分段属于 Task 7。用户环境的 StarRocks 目标与 grants（G1）、四种用途的共同列与容量（G2）、业务口径（G3）及驱动在目标上的兼容性（G5）仍待提供或验证。

**Task 4 已证明（真实 PostgreSQL 16.15 隔离库，未运行 Agent、模型或工具）：**

- 全新初始化先取得实例锁再建任何表，应用表在持锁连接的一个事务内顺序执行 001、002，得到 v2；重复执行不变。实例锁被占用时初始化拒绝，空库仍为零表（含 SDK 表）；两个进程反复并发首次初始化，只有取得锁的一方建表，另一方得到 `StorageBusyError`。v1 数据库被就绪检查与普通初始化拒绝（版本与表都不变），只有 `upgrade_storage` 把它升到 v2，重复升级无变化；未初始化或未知版本（99）不升级。全新初始化把版本推进到 2（旧程序只接受 1 由基线源码保证，未执行旧程序）。迁移中途失败（表名被占用）整体回滚，版本仍为 1。两个进程并发升级时只执行一次。
- 实例锁：持锁期间第二个实例、初始化与升级都得到 `StorageBusyError`；释放后可再取得。终止持锁连接后 `verify()` 失败并锁低 readiness，锁已释放、另一实例可取得。
- 请求：只保存摘要，原消息、cookie 与请求编号原值不落库。同编号在 accepted/running/completed/failed 各状态返回原记录，不新建；正文或用途不同即冲突；不同 owner 或会话语境的同名编号互不影响、不能互读。两个引擎并发接受同一编号只创建一条，状态迁移只有一个成功。过期请求不可读、不可再接受、不能首次发送或重发。回答超出上限时标为 `result_not_saved`；被篡改为不合契约的回答读取时拒绝。
- 投递：两个引擎并发首次发送只有一个取得；unknown/failed 时事件重投零取得，显式重发只有一个取得；sent 后两者都拒绝；pending 不能显式重发；failed 回执不能重发；其他 owner 不能重发；不在 sending 时结束投递锁低 readiness。
- 双存储与关键状态：结果保存失败（触发器注入）时请求标为 failed、Session 关闭、readiness 保持；失败状态也写不了时 readiness 锁低、请求保持 running，此后拒绝新请求与新会话；readiness 锁低后已有映射仍可读取，不再创建新映射。开始执行、结束投递的写入失败都锁低 readiness。接受、状态迁移、结果保存、结果失败标记与启动恢复在数据库写入途中被取消（触发器 `pg_sleep` 延迟后取消）时，取消原样传播、readiness 锁低；模拟重启后持锁恢复不留 accepted/running，被中断请求的会话均已关闭。
- 失败码：调用者可写的 4 个失败码均可往返；内部码 `result_not_saved`、`interrupted` 与含内部主机和用户的异常文字、超长与空字符串都不经 `fail()` 写入，running 请求保持原状态、其 active Session 不变且 readiness 锁低，不会出现 `failed/result_not_saved` 而 Session 仍 active；数据库 CHECK 拒绝闭集外的值与 failed 状态上的 `interrupted`；去掉约束后写入的未知值在读取时拒绝。
- 启动恢复（持锁连接、一个事务）：accepted/running 变为 interrupted 并关闭各自 Session，遗留 sending 变为 unknown，pending 与其他 Session 不变；之后中断回执可首次发送一次，unknown 只能显式重发；恢复失败整体回滚并锁低 readiness。
- 会话映射：同一 owner/会话语境稳定返回同一 current 会话，不同 owner、语境或渠道互不相同。有运行中请求时拒绝新建；新建后 generation 递增、新请求绑定新会话、旧请求仍可读。两个引擎并发新建后只有一个 current。
- 清理：只删除过期、非 current 会话的历史、元数据、映射与已过期请求，current 会话保留；有 sending 请求的会话跳过；与显式重发并发时未过期请求保留且重发照常取得；SDK 历史清除失败时停下，会话已关闭、历史保留，修复后再次运行完成；按批次大小分批。没有会话元数据的已退役映射（Agent 运行前新建会话的空会话、运行前 failed、恢复后 interrupted）过期后连同已过期请求删除；current 映射、发送中或运行中请求、未过期的终态请求及与显式重发竞争的请求都保留，请求过期后下一次清理完成。
- 反向验证 21 项（初始化自动升级、维护不取锁、释放时连接放回连接池、锁丢失不锁低、版本检查顺序、首次发送可取 failed/unknown、重发不要求 completed、结束投递不要求 sending、不比较语义摘要、请求键不含 owner、结果失败不关闭会话或不锁低、恢复不关会话或不处理 sending、新建会话不查运行中请求、清理不跳过 sending 或 current、读取或发送不查过期、关键迁移失败不锁低、回答不限大小）均使对应用例失败。审查修复另做 12 项隔离变异：初始化先建表后取锁、首次创建映射不查 readiness、共享关键写入/结果保存/失败标记各自不处理取消、失败码写入/读取/数据库三处闭集检查、无元数据清理删除时不要求没有请求、两层同时去掉 `retired` 条件，共 10 项使对应用例失败；复审修复再做 3 项（`fail()` 放行 `result_not_saved`、放行 `interrupted`、结果保存失败路径不关闭 Session），均使对应用例失败；只去掉候选查询或删除语句其中一层的 `retired` 条件不失败（另一层仍拦截，属冗余防护）。

**Task 4 未覆盖：** `ChannelService` 的调用顺序与会话互斥属于 Task 5；`serve` 周期检查持锁连接并退出、启动时调用恢复与 readiness 端点属于 Task 8；飞书目的地是否保存供重发待 G2；保留期、回答上限与摘要密钥的正式配置值未定（G2）。接受新请求读 current 映射用共享锁、新建会话用排他锁，二者互斥只经过只读推理与并发新建用例，没有确定性地复现“接受与新建交错”的竞争。没有可用的 v1 → v2 真实部署数据，升级只在合成 v1 库上验证；升级前备份由操作者负责（P3 演练）。并发首次初始化用例在修复前也可能通过（锁外竞争不必然复现），“被拒绝的初始化不建表”由持锁空库用例确定性证明。取消只在数据库语句执行中途注入，没有在提交报文已发出、确认未返回的窗口精确注入，该窗口由 readiness 锁低与重启恢复兜底。新增失败码需要新迁移；`Application.TurnReason` 到存储失败码的映射留给 Task 5。

**Task 5 已证明（真 Runner + `open_model` 装配的脚本模型，HTTP mock 不建立网络连接；受治理合成工具；隔离的真实 PostgreSQL 16.15）：**

- 授权失败关闭：`resolve` 抛异常、返回 `None` 或返回类型不符时都拒绝，请求、模型与工具都为 0。入口授权与 Evidence 授权必须是同一个 `AccessPolicy` 对象：`EvidenceStore.authorize` 须是绑定在该对象本身上的 `authorize`。另一个对象复用原绑定方法（含与原策略值相等的同类对象）、包装函数、同一对象的其他方法都在装配时拒绝，请求、模型、工具与 Adapter I/O 都为 0（修复前，复用原绑定方法的代理把 mallory 解析为 alice 后 Adapter 实际执行了 `local/order_total`）。应用与交付必须是同一个 `EvidenceStore`，否则装配失败。`ResultDelivery` 的存储、Evidence、授权来源、预算与 `ChannelService.results` 装配后只读，替换抛 `AttributeError`。
- 本轮范围：查询轮展示实际查询工具，诊断轮不展示；撤销查询授权后下一轮查询也不展示，上一轮许可不延续。入口信封不能携带 session、target 或 tool scope，空消息拒绝；RunContext 的 subject、session、turn 与目标来自授权解析与服务端会话映射。
- 去重：运行中重复提交返回“处理中”，完成后重复提交返回原记录，模型与工具调用次数不变。
- 会话互斥：第一轮已从 `run_turn` 返回但结果尚未保存时，同会话第二轮记为 `failed/busy`，模型与工具 0 调用，新建会话被拒绝；第一轮保存后同会话继续可用。全局并发上限满时另一会话的请求同样记为 `failed/busy`。
- 失败与双存储：模型失败、伪造证据引用、输入超出准入策略分别保存 `model_failed`、`evidence_failed`、`session_failed`，回执为固定文字，不含上游错误体。结果保存失败时请求为 `failed/result_not_saved`、Session 关闭、readiness 正常，同会话下一轮在模型调用前失败，新建会话后恢复；终态也写不了时 readiness 锁低，此后拒绝新请求与新会话。运行中取消时取消原样传播、readiness 锁低；重启持锁恢复后请求为 interrupted、会话关闭，中断回执只发送一次。
- 交付：不持有 `Application` 的 `ResultDelivery` 重读 completed 结果并生成含结构化事实的 Web 交付；首次发送 failed 后事件重投零发送，显式重发只成功一次，两次发送内容相同，模型与工具调用次数不变。撤权后结果不可读、不发送并记为 delivery failed，同会话追问在模型调用前失败。其他身份、其他渠道与不存在的编号得到相同拒绝。发送函数抛出异常时记为 unknown 并原样传播，之后只能显式重发。失败回执不能显式重发。
- 反向验证 14 项（同会话不互斥、`run_turn` 返回后即释放会话、取消不锁低 readiness、不核对单一 `AccessPolicy`、不核对同一 `EvidenceStore`、撤权结果不记 failed、失败码映射错位、授权返回值不核对类型、授权异常不按拒绝处理、范围不按本轮用途计算、发送异常不记 unknown、交付不重新验证、重复请求也运行、新建会话不先查 readiness）均使对应用例失败；其中前两项表现为用例卡住，按 90 秒超时判定。B1 修复另做 6 项：来源核对退化为 callable 相等、owner 改用 `==`、去掉 owner 身份、去掉方法相等、`access` 公开可写、`results` 公开可写，均使对应用例失败；最初的 `inspect.ismethod` 条件经变异证明与 `__self__` 身份核对等价，已删去。

**Task 5 未覆盖：** 实现先于测试写成（未按计划先写测试），以上 14 项隔离变异用来证明测试能发现对应缺陷。HTTP 与飞书协议、Web cookie、飞书队列与 ack 属于 Task 6/7；`AccessPolicy` 的配置驱动实现与正式装配、`requests resend` 命令属于 Task 8（身份清单待 G6/G7）。失败码沿用 Task 4 的 4 个调用者码，多个应用层原因共用一个码，回执文字因此较笼统（如输入超限与会话不可回放都显示为 `session_failed`）；更细的提示需要新迁移，另行决定。readiness 锁低后会话不释放这一保护与“readiness 锁低拒绝一切新工作”重叠，单独去掉时没有可观察差异。新建会话的进程内忙碌检查与数据库条件（存在 accepted/running 请求时拒绝）重叠。真实模型与真实 StarRocks 未参与。

**Task 6 已证明（ASGI 调用 FastAPI 应用，其后是与 Task 5 相同的真实路径：ChannelService → Application 真 Runner + HTTP mock 脚本模型 → 受治理合成工具 → 隔离的真实 PostgreSQL）：**

- 页面换发随机 cookie（43 字符，`HttpOnly`、`SameSite=Strict`，未配置时不带 `Secure`）；API 写操作缺少或伪造格式的 cookie 时 403，不在失败响应里换发，请求与模型都为 0。身份是配置的固定操作者，会话来自 cookie；正文出现 session、subject、target 等多余字段即 400。
- 查询轮完成后 POST 与刷新后 GET 返回同一交付（结构化事实含 `rows`），GET 不再调用模型或工具；模型失败给固定回执，不含上游错误体；结果保存失败给“结果保存失败”回执；终态也写不了时 POST 与随后的 GET 都返回 503，不再以“处理中”服务，readiness 锁低。
- Host 只按允许地址字面匹配：外部域名、`localhost.evil`、替代端口、缺端口、尾点、大小写变体、用户信息、IPv4 映射与展开的 IPv6、`127.1`、十进制 IP、空值、重复或缺少 Host 都在读取正文前 400；`[::1]` 与 `localhost` 只在配置后放行。POST 必须带与 Host 同源的 Origin（缺少、`null`、他站、替代端口、其他 scheme、尾点、另一个允许但不同源的地址都 403，有效 cookie 的对照请求才被接受）；带他站 Origin 的 GET 403；不返回任何 CORS 头。Content-Type 必须是 JSON（415）。
- 正文上限：声明超限在调用 receive 前 413；非法、负数、带空格、逗号或重复的 Content-Length 400；伪造较小长度、chunked 与缺少长度时在累计越过上限的那一块停止（20 块中只读 5 块）。
- 其他 cookie 读取他人编号、无 cookie 读取与读取不存在的编号得到相同 404；他人可复用同一编号，互不冲突。同编号换内容或用途 409；撤权后 POST 403、零 I/O；先查后撤的结果 GET 403 且不含数据。同 cookie 运行中重复提交 202 “处理中”，并发第二条记为 busy，运行中新建会话 409；完成后重复提交返回原结果，新建会话后的请求落在新会话。readiness 锁低后 `/readyz` 503（不暴露原因）、新请求与新会话 503，`/healthz` 与已完成结果仍可读。
- 显示：含 `<script>`、事件属性、HTML 标签、双向控制符与长文本的结果只以 JSON 返回（`nosniff`）；页面 CSP 禁止内联脚本与 eval，HTML 只有一个外部脚本且无事件属性，脚本不含 `innerHTML` 等 HTML 解析入口。
- 反向验证 17 项（不查 Host、Host 大小写与尾点归一、非 GET 不查 Origin、Origin 只需在允许列表、GET 不查 Origin、不查 Content-Type、不预检长度、读取时不累计、API 写不要求 cookie、未就绪仍显示处理中、结果未保存不处理、接受任意 cookie 值、正文允许多余字段、cookie 非 HttpOnly、SameSite=Lax、去掉 CSP、去掉 nosniff）均使对应用例失败。“非 GET 不查 Origin”与“Origin 只需在允许列表”起初存活（用例没有 cookie，被 cookie 检查挡住），补上有效 cookie 与同源对照后失败。

**Task 6 审查修复（针对 `07a4c6c` 的首轮独立审查）：**

- B1 共同根因：允许地址的校验只要求“显式端口的规范写法”，没有核对浏览器能否发出与之字面相同的 Host/Origin；`_Guard` 按字面匹配，所以端口 0 与 scheme 默认端口（浏览器省略 `http` 80、`https` 443）的配置能通过、却让每个请求 400。修复在 `WebConfig._canonical_loopback`：端口为 0 或 scheme 默认端口时配置失败（单独或与可用地址混合），`_Guard` 的精确匹配不变；`http://127.0.0.1:443`、`https://127.0.0.1:80` 不是默认端口，照常接受。正式默认地址 `http://127.0.0.1:8501`（用户 2026-10-01 决定）由 Task 8 经配置传入，`web.py` 不写端口；测试的正常端口统一为 8501。`127.0.0.1:8501`、`[::1]:8501` 与获准的 `localhost:8501` 各自完成一次同源查询轮（成功对照）；配置拒绝时请求、模型与工具都为 0。
- B2：Task 6 的结果限定为 `create_web_app` 在真实 Uvicorn 与 Chrome 中的组件闭环，正式 `xiaowei serve` 的浏览器验收唯一归入 Task 8（计划已改，要求未删）。浏览器证据改由仓库内 `tests/sdk_core/test_web_browser.py` 复现（标记 `browser`，默认不收集；`-m browser` 时缺少 `SDK_TEST_CHROME` 即失败）：进程内 Uvicorn 0.52.4 绑定 `127.0.0.1:8501`，经 `--remote-debugging-pipe`（标准库实现的最小 CDP 客户端 `tests/sdk_core/browser.py`，不新增依赖、不开调试端口）驱动无头 Chrome 154，后端为测试装配。
- 补充保护：CSP 直接断言含 `form-action 'none'`；表单显式 `method="post" action="/api/turns"`；`secure_cookie=True` 时 cookie 带 `Secure`（不代表 HTTPS/SSH 的 G6 实战完成）。

**Task 6 浏览器 smoke 已证明（真实 Uvicorn + Chrome，测试装配，不是正式入口）：** `document.cookie` 为空，CDP 读到的 cookie 为 `HttpOnly`、`SameSite=Strict`、非 `Secure`；默认用途为诊断，诊断轮模型看不到查询工具，查询轮看得到，发送后用途重置为诊断；查询轮显示两张各 2 行的有限表格（本轮与回放的上一轮证据）与截断说明；`<script>`、`<img onerror>`、模型分析中的 `<b>` 都只作为文字（`document.title` 未变，没有 `img`/`b` 元素，只有 1 个脚本，无对话框），U+202E 显示为 `\u202e`；失败显示固定回执与请求编号，不含上游内容；模型调用中刷新页面（中断进行中的 fetch）后该轮仍在服务端完成，刷新后的 GET 恢复全部 4 条结果，各消息模型与工具调用次数不变；新建会话清空列表，之后的请求落在不同的会话；access log 只有方法、路径与状态，不含任何消息。屏蔽 `app.js` 后提交表单：地址不变、历史中没有消息、access log 无新增行（CSP `form-action 'none'` 阻止提交），请求、模型、工具均为 0。

**Task 6 未覆盖：** 正式 `xiaowei serve` → 浏览器页面/脚本/cookie → 成功及保护性失败的验收属 Task 8；smoke 只证明同一 ASGI 应用在真实 Uvicorn 与 Chrome 中的行为，装配、模型与工具是替身。smoke 未纳入 CI（需本机 Chrome，按需用 `-m browser` 运行），只在 macOS + Chrome 154 上运行过；它固定使用 8501，端口被占用时失败而不换端口。允许地址只支持 loopback；经 HTTPS 反向代理或 SSH 的使用方式、`Secure` 设置与操作者标识待 G6。cookie 没有设置有效期（浏览器会话 cookie）；关闭浏览器后旧会话不能再从页面访问（数据仍按保留期保存）。交付 `content` 中的事实区域与 Web 表格重复显示同一数据（Task 3 交付格式），页面未去重。POST 在请求内等待本轮完成，没有单独的服务端请求期限，依赖 `Budget.timeout_seconds`。真实模型与真实 StarRocks 未参与。

**Task 7 安装探针（`1d1aa01`，仓库外 venv，离线）：** `FeishuChannel` 把消息处理异步调度后立即 ack，处理器抛错也写回 `code=200`；低层 `ws.Client` 抛错可回 `code=500`，但没有公开的关闭路径。计划要求“持久化失败不得 ack”无法用公开 API 满足，按计划停工，三个候选方案见计划 Task 7。

**Task 7 已证明（离线：真 Runner + HTTP mock 脚本模型、受治理合成工具、隔离的真实 PostgreSQL；飞书发送为替身）：**

- 契约（用户 2026-10-01 选择）：SDK 先 ack；落库失败（含 readiness 锁低）时消息不处理、不回复、只记原因码，请求、模型、工具、发送均为 0。
- 入站只用 `on("raw")` 的完整事件。其他事件类型、其他 app、其他租户（header 或发送者）、bot/app 发送者、未登记的 `open_id`、群聊、图片/富文本、过期或来自未来的事件、空文本、非 JSON 或非字符串内容、超长文本、缺失或超长的消息/会话编号，共 21 类，都在持久化与模型前丢弃，日志只有原因码、不含消息。登记了但当前无授权的用户同样为零调用。
- 普通文本为诊断，模型看不到查询工具；`/查询` 只作用于本条，下一条普通文本与 `/诊断` 都看不到查询工具；命令后为空只回一次固定提示。`/新建` 不进入模型，代数加 1；重投不再新建，新会话看不到旧历史；运行中 `/新建` 被拒并提示。
- 同一事件并发投 3 次再投 1 次：模型 2 次、工具 1 次、发送 1 次、请求 1 条。两个用户使用相同 `message_id` 互不冲突。结果已保存但未发送时，事件重投只发送保存的结果一次、不重跑。发送明确失败、结果不明或抛出异常时，投递状态分别为 failed/unknown/unknown，重投不再发送。重启恢复后的 interrupted 请求在事件重投时只发一次“已中断”回执。
- 队列满：第二条记为 failed/busy，只发一次繁忙回执，模型 0 次。Web 占住全局唯一并发槽时，飞书消息得到繁忙回执且模型 0 次（两端共享 `max_concurrent_turns`）。`consumer_count` 超过全局上限时装配失败。消费者在模型调用中被取消时 readiness 锁低。
- SDK 装配：`max_attempts=1`、`text_chunk_limit=max_reply_chars`、只发 `text` 且不带 `reply_to`、`emit_raw_events`、策略 disabled、关闭入站附加 API 调用。超长回复在行边界截断并附固定说明。`SendResult` 中成功映射为 sent，明确拒绝类映射为 failed，unknown/超时/未连接/非预期返回映射为 unknown。桥接：raw 处理器在 SDK 线程循环上被调用，`receive` 在应用循环上运行；发送在 SDK 循环上执行，超时为 unknown。
- 真实 SDK 分发器（离线，webhook 传输注入）：`handle_webhook_request` 在处理前即返回 200；事件经 SDK 后台线程的 `on("raw")` 进入应用循环，完成一轮并单次回复，群聊事件被丢弃。
- 反向验证 21 项（去掉 header/发送者租户、发送者类型、allowlist、单聊、文本类型、过期、未来时间、编号长度检查；普通文本默认查询；队列满直接丢弃；重投不发送；不查消费者上限；命令不去重；重试 3 次；SDK 分段上限；unknown 映射为 failed；不截断；桥接在 SDK 循环运行；发送不限时；关闭 raw 事件）均使对应用例失败。

**Task 7 审查修复（针对 `58aa5f6` 的首轮独立审查）：**

- B1 正文边界：编号上限 200 被误用于序列化正文，`max_message_chars` 分支实际不可达。修复后，正文容器上限为 `max_message_chars × 12 + 64`，超出时在解析前拒绝（`content_too_large`），解析后再按文本长度限制（`too_long`）。上限 1000 时，500 字中文、恰好 1000 字、引号与反斜杠、控制字符、emoji 在转义和原样两种序列化下都被接受；1000+1（含 emoji）判为 `too_long`，20 KB 的 JSON 与 20 KB 的非法 JSON 判为 `content_too_large`。各类拒绝的原因码逐一断言。
- B2 桥接准入：SDK 回调到应用循环的在途事件最多 `queue_size` 个。阻塞应用侧后注入 200 个事件：修复前启动 200 个协程，修复后只有 4 个，其余 196 个只记 `intake_full`，名额释放后恢复接收，停止后的回调记 `closing`。被丢弃的事件请求、模型与工具调用均为 0。这是先 ack 契约下的持久化前丢弃，已入库请求的队列满繁忙回执不变（契约差异记在计划 Task 7）。
- B3 结果落定（共同根因：结果保存后到投递状态落定之间没有取消安全的边界，且把取得投递权前的异常误当作“已记为 unknown”吞掉）：
  - 取得投递权前被取消、校验或读取异常时，锁低 readiness 并原样传播。只有已取得投递权后的发送异常才按 unknown 记录。
  - 消费者意外退出、被取消，或 `run` 结束时仍有排队请求，都锁低 readiness。
  - 新增 `drain(timeout)`：先停止接收，再有界等待队列与投递结束，超时锁低 readiness。
  - 启动恢复把飞书 `completed` + `pending` 记为 `failed`，之后只能显式重发、不重跑，Web 结果不变。
  - 覆盖的窗口：保存后、取得投递权前被取消；发送中被取消（记 unknown）；交付校验因证据存储故障失败（重投路径与消费者路径各一）；排队未运行时停止超时；正常 `drain`。
- B4 关闭期限：SDK 公开的同步 `stop(join_timeout=…)` 在守护线程中运行，应用循环最多等待 `stop_timeout_seconds`（新增配置），超时返回 False。另验证了正常关闭、关闭一直不返回、重复停止、启动失败后停止，以及关闭异常原样传播。
- 有意接受的残留（复审确认可接受）：交付前撤权或结果过期时不发送，请求在进程内保持 `completed` + `pending`（无法按撤权后的身份定位记录并改状态），直到重启恢复记为 `failed`。显式重发仍复核当前权限。相应不变量限定为“意外异常或取消不留下无人处理的 pending”。

**Task 7 复审修复（针对 `ee10951` 的复审）：**

- N1 关闭竞态：事件已过关闭检查、仍在 `ChannelService.accept()` 等数据库时，队列为空，`drain()` 立即成功；随后请求入队却没有消费者，readiness 仍为 true。修复后 `FeishuGateway` 统计已进入 `receive` 的调用，`drain()` 在同一期限内先等它们归零，再等队列；超时锁低 readiness。`/新建`、空命令提示与重复事件的首次发送同样计入。
- 复审的非阻断建议一并处理：正文含孤立代理码点（`{"text":"\ud800"}`）时以 `content` 原因码拒绝，不再从 `InboundRequest` 抛 `ValidationError`；`LarkTransport.start()` 失败后恢复为不接收回调。

**Task 7 未覆盖：** 真实长连接、平台事件字段、平台重投、真实发送结果、断线重连与关闭只能在获准的测试应用中验证（需要用户授权与 G7 配置）。离线用例的事件字典按 1.4.0 源码与探针构造，不能证明平台实际下发相同字段。chat ID 不持久化（G2）：重启后 interrupted 只在同一事件重投时回执，主动通知与 Task 8 显式重发的目的地待 G2 决定。`/新建` 与空命令提示只在进程内去重，重启后时效内的重投可能再次新建会话。SDK 自带的消息管线仍运行（无消费者）。真实分发器用例中 SDK 启动时会做一次域名解析（连接被 pytest-socket 阻断）。关闭超时后守护线程与长连接的实际残留未核对（G7/Task 9）。正式装配、`serve` 中的导入顺序约束、先 `drain` 再取消消费者并关闭长连接的停止顺序都属 Task 8。

**Task 8 已证明（离线：真实 PostgreSQL 隔离库、正式装配顺序与真实 Uvicorn；模型为 HTTP mock 脚本或本机已关闭的 HTTPS 端口，StarRocks 为驱动替身，飞书为 SDK 公开面替身）：**

- 装配顺序：配置校验 → 引擎 → 实例锁 → schema 检查 → StarRocks 工具与容量检查 → 唯一授权来源 `StaticAccess`（入口解析与 Evidence 授权读同一张表）→ 启动恢复 → 模型绑定 → Application → ChannelService。启动失败（数据库未初始化、另一实例持锁、模型凭据缺失、端口被占用）都不对外服务并逆序释放：锁可被再次取得、端口无人监听；端口被占用在触及数据库前失败。
- 正式装配下：诊断轮看不到查询工具；查询轮执行 SQLGuard 产物并返回结构化表格；模型上游失败为固定回执；启动前遗留的 running 请求在启动恢复中记为 interrupted 且不重跑；持锁连接被数据库终止时进程以退出码 1 结束。停止时 Web 停止接收，飞书先 `drain`（在途轮次未结束前不关闭长连接，回复发出后才关闭），再取消消费者、关闭长连接，正常停止退出码 0。飞书长连接启动失败只把 `/readyz` 的 `feishu` 标为 `unavailable`，Web 照常服务。
- 显式重发：飞书明确发送失败的已保存结果只重发一次（模型与 StarRocks 调用数不变），第二次返回不可重发；换 chat（参与会话摘要）、未知消息、无授权 subject、证据过期都在发送前拒绝。重发不装配模型、不连接 StarRocks；真实 `FeishuChannel`（未启动长连接）构造与关闭在 pytest-socket 下不发起网络请求。
- 命令（子进程，console script 与 `python -m xiaowei` 各一遍）：未初始化时 `serve` 退出 1；`storage init`、`storage upgrade`（输出版本 2）、`storage cleanup` 成功；`serve` 就绪后页面换发 cookie、模型失败回执可 GET、新建会话成功、其他 Host 400、跨源写入 403；第二个实例（另一端口、同一数据库）退出 1 并提示另一进程；SIGTERM 后退出 0。配置错误退出 2，只报字段路径，不回显值。
- SDK 日志开关：外部预置 `0`/`false` 时，对照进程的 DEBUG 日志含 canary（模型输入、模型输出、工具参数与结果）；先导入正式入口的进程不含。`import xiaowei` 不导入 `agents`、不改环境变量。正式 `serve` 在 DEBUG 日志下处理含 canary 的消息，输出不含消息与凭据。
- 打包：wheel 只含 `xiaowei`（含迁移 SQL 与静态文件），在只装锁定运行依赖、没有仓库源码的环境中两个入口可运行，`xiaowei_agent` 与 `alembic` 不可导入。
- 正式入口的真实浏览器验收（`127.0.0.1:8501`，Chrome）：子进程 `xiaowei serve` 下 cookie 为 `HttpOnly`、`SameSite=Strict`，`document.cookie` 为空，失败回执带请求编号并在刷新后恢复，新建会话清空列表，脚本未加载时提交被 CSP 阻止，`localhost` 别名被拒且不换发 cookie；同一装配在进程内替换模型与 StarRocks 后，诊断、查询表格与模型调用中刷新恢复在 Chrome 中完成。
- 反向验证 16 项（不强制模型或工具日志开关、包导入带副作用、启动不恢复、不核对持锁连接、先关长连接再 drain、飞书连不上即启动失败、允许非 loopback 监听、允许地址不含监听地址、Web 与飞书共用 subject、`authorize` 不核对目标、重发走首次发送路径、`/readyz` 不报组件、wheel 带旧包、alembic 回到生产依赖、配置错误回显输入）均使对应用例失败。

**Task 8 首轮审查修复**（审查针对 `03f73c3`，三项均经源码与复现确认成立）：

- B1 根因：配置校验把同一监听地址的 `http`、`https` 都当作有效 Origin，而正式监听不配置 TLS、只提供 HTTP；`_Guard` 按字面匹配，HTTPS-only 配置能通过校验，浏览器的 `http://` Origin 却被 403（修复前在 `_Guard` 上复现）。修复在 `ServeConfig._consistent`：`web.allowed_origins` 必须字面包含 `http://{listen_host}:{listen_port}`（IPv6 写 `[::1]`）；`WebConfig` 原有的“同一 host:port 只能对应一个 scheme”使同址 HTTPS 不可能与之并存。`_Guard` 的 Host/Origin 精确匹配不变。
- B2 根因：`_watch` 先睡满 `lock_check_seconds`（默认 5 秒）再核对，持锁连接被终止后旧实例在间隔内仍 ready；修复前用例在终止锁连接 2 秒后 `/readyz` 仍为 200。修复：`hold_instance_lock` 取得锁后经 SQLAlchemy 公开的 `get_raw_connection().driver_connection` 在 asyncpg 连接上注册公开的 `add_termination_listener`，连接终止时立即锁低 readiness（`instance_lock_lost`）并设置 `InstanceLock.lost`；`_watch` 等待该事件，周期 `verify` 保留为兜底；主动释放前先注销，正常关闭不算丢锁。修复中发现：正常停止时取消 watch 若打断 `verify` 的查询，SQLAlchemy 会使持锁连接失效（修复前也会提前断开锁连接，只是无人观察），现由 `stopping` 事件通知 watch 退出，不在查询中途取消。
- B3 根因：`cli.py` 用 `logging.basicConfig` 设置根 logger，`--log-level INFO/DEBUG` 同时放开所有第三方 logger；httpx2 在 INFO 写出完整端点，httpcore2 在 DEBUG 写出连接地址与端口，外部预置 `OPENAI_LOG=debug` 还会让 openai 自己的 logger 写出请求行。修复为 `cli.configure_logging`：根 logger 与输出端门槛为 WARNING（或更高的所选级别），所选级别只作用于 `xiaowei` logger；输出端按来源过滤，第三方即使自行调高 logger 级别也只输出 WARNING 及以上。
- 回归：HTTPS-only 同址配置被拒、HTTP 配置（含 `[::1]` 与额外的其他端口 HTTPS）通过，正式浏览器路径 POST 成功，其他 Host 400、跨源 403 不变；周期核对设为 60 秒时终止锁连接，旧实例 2 秒内不再 ready，丢锁后的新请求不调用模型与 StarRocks，退出码 1；旧实例在途轮次期间第二实例取得锁并恢复为 interrupted、不重跑，旧实例随后的结果不能覆盖（请求保持 interrupted）；核对查询频繁在途时连续 5 次正常停止均退出 0；两个入口在 INFO、DEBUG 下（预置 `OPENAI_LOG=debug`）不出现端点 canary、模型连接地址、数据库端口、请求行与请求选项、消息与凭据，产品自身的启动恢复与轮次阶段日志仍在；canary 子进程改用正式入口的 `configure_logging`，对照进程证明成功响应时 httpx2 会写出含端点 canary 的完整 URL。
- 隔离变异 9 项（同址 HTTPS 算监听地址、不注册终止通知、watch 先睡满间隔、正常停止仍取消 watch、释放前不注销通知、通知不锁低 readiness、回到根 logger `basicConfig`、不按来源过滤、不放开产品 logger）均使对应用例失败。

**Task 8 增量复审修复**（复审针对 `f8c03af`；B1 已确认闭合，B2、B3 经复现确认未完全闭合）：

- B2 共同根因：锁丢失通知、旧实例正在提交的接收事务与新实例启动恢复之间没有数据库级交接边界。
  - B2.1：旧实例的接收事务通过检查后、提交前失去锁，新实例的恢复看不到未提交的 INSERT，旧事务随后把请求提交为 accepted 并长期滞留。修复在 `storage.InstanceLock.admits` / `exclude_accepts` 与 `ChannelStore.accept` / `recover`：接收新请求的事务先取得接收屏障（独立 advisory 键）的共享事务锁，再在库内核对实例锁仍由本进程的持锁后端持有（`pg_locks` + `pg_stat_activity` 的 pid 与 backend_start）；核对失败不写入、锁低 readiness 并拒绝。恢复在持锁连接的事务内先取得同一屏障的排他事务锁，再读取请求，因此会等仍在提交的接收事务结束；恢复成功后该存储的接收都经此核对。修复前真实 PostgreSQL 上复现为请求停在 accepted（存储层恢复 interrupted=1 而非 2；正式装配的最终状态为 `['accepted', 'completed']`），终止通知被错过时旧实例仍能写入新请求。
  - B2.2：f8c03af 在取得锁并提交后才注册终止通知，连接在此之前关闭则通知已错过（复现：`closed_before_listener True`、`late_listener_fired False`）。修复为 `InstanceLock.watching`：注册后立即复核 `is_closed()`，已关闭即按丢锁处理并拒绝启动（`StorageUnavailableError`），不等周期核对；正常停止不取消核对查询的修复保留。
- B3 根因：日志输出端仍放行第三方 WARNING 及以上，且飞书 SDK 导入时给 `Lark` logger 自装 stdout handler，绕过输出端过滤；真实锁定版的端点探针失败时，stdout 与 stderr 都写出 `HTTPConnectionPool(host='127.0.0.1', port=…)` 与含端点的完整路径。修复在 `cli.configure_logging`：输出端只接受 `xiaowei` 日志，第三方在任何级别都不输出；先导入 `lark_channel.core.log` 再移除其 handler（保留传播，由根 handler 过滤），不改依赖源码。早期 SDK 日志开关强制关闭不变。
- 回归：存储层与正式装配各一项“接收事务暂停 → 终止旧锁 → 新实例接管恢复 → 放行旧事务”，最终没有 accepted/running，重复请求得到 interrupted 终态，模型与 StarRocks 未调用、旧实例退出 1；终止通知被错过时持有者核对拒绝写入并锁低 readiness，新实例照常接收；已关闭连接注册通知时立即锁低并拒绝；真实 `lark_channel` 探针在 INFO/DEBUG（预置 `OPENAI_LOG=debug`）下，对照进程两路输出都含端点与主机端口，正式日志配置下 stdout 只有脚本自身输出，stderr 不含端点、主机、端口、`HTTPConnectionPool`、`[Lark]`，产品事件与 `reason=` 仍在。首轮回归（B1 的 IPv4/IPv6/跨源拒绝、正式浏览器、丢锁即停、正常停止）全部保留并通过。
- 隔离变异 9 项（接收不取共享屏障、恢复不取排他屏障、不核对锁持有者、恢复后不绑定实例锁、核对失败仍写入、去掉注册后的关闭复核、重新允许第三方 WARNING、保留 Lark 自带 handler、第三方按所选级别放行）均使对应用例失败。
- 首轮 B3 记录中“第三方只输出 WARNING 及以上”已被本轮取代。

**Task 8 第三轮复审修复 N1**（复审针对 `7328d7f`；B1、B2.1、B2.2、B3 已确认闭合并保持）：

- 根因与 B2 相同：所有权屏障只覆盖了“写入新请求”，其他开始新工作的写入不在其中。错过终止通知的旧实例在新实例接管后，仍能创建第一代会话映射、轮换 current（generation 1→2），对重复请求返回已有记录，并取得自动投递权（pending→sending）。最后一项是本轮核对重复请求路径时确认的同根问题：飞书重投会走这条路径。
- 修复在 `ChannelStore._admit`（复用 `InstanceLock.admits` 与接收屏障，不加表、迁移或新锁）：`accept` 在事务开始即核对（新请求与重复请求一致），`new_session` 在读取或退役 current 之前核对，`_current` 在创建第一代之前核对，`claim_send` 在同一事务内核对后再 CAS。核对与屏障持续到事务提交。已有会话的纯读取不变；只有经 `recover` 绑定实例锁的存储核对，`requests resend` 用的未绑定存储不受影响。`finish_send`、`start`、`complete`、`fail` 只推进已取得的工作，仍靠数据库条件更新，并由恢复改写的状态拒绝。
- 修复前（`7328d7f`）新增 4 项失败：轮换、首次创建、重复请求、取得投递权都未抛 `NotReadyError`。修复后，旧实例的这些写入都被拒，readiness 锁低为 `instance_lock_lost`；会话映射、generation 与 delivery 不变；新实例照常创建、轮换、去重并取得一次投递权。持锁实例的成功对照（创建第一代、两个存储并发轮换只留一个 current、接收与投递）通过。
- 既有用例中有 5 个在恢复后释放锁，再继续用这个存储取得投递权或轮换会话（channel_store 2、channel_service 1、feishu 2）。现在改为在持锁期间完成这些操作，与 serve 一致；断言未改（`git diff -w` 只多了注释）。
- 隔离变异 7 项全部被发现：首次创建不核对、轮换不核对、重复请求不核对、取得投递权不核对，以及复跑 B2.1 的不取共享屏障、不取排他屏障、不核对持有者。B2.2/B3 的 3 项变异（去掉关闭复核、放行第三方 WARNING、保留 Lark handler）复跑后仍被发现。第一版用例把重复接收与取得投递权写在同一个用例里，“取得投递权不核对”的变异因此漏检（readiness 已被前一步锁低），已拆成两个独立用例。

**Task 8 与计划的差异**（JSON 配置、启动失败即退出、`serve` 不装配 MCP、重发的 chat_id 由操作者给出、legacy 镜像与冒烟脚本、wheel 契约测试的调整、测试位置）记在计划 Task 8 的实施修订中。

**Task 8 第四轮审查修复 N2、N3**（审查针对 `49ed6df`；N1、B1、B2.1、B2.2、B3 保持闭合）：

- N2 根因：接管边界只覆盖了请求状态，没有覆盖“开始运行”和 Session 生命周期。
  - `start()` 不经所有权核对：新实例取得锁、尚未恢复时，旧实例仍能 `accepted → running` 并运行模型。
  - 恢复的 `close_sessions` 只能更新已存在的元数据：旧 Runner 尚未或正在登记时更新 0 行，随后的首次登记写入 active 并提交历史，后续轮次回放了中断轮次。
- N2 修复：
  - `ChannelStore.start` 在同一事务内经 `_admit`。
  - 恢复改用 `session.close_interrupted_sessions`：元数据不存在时写入已关闭的占位行（`INSERT … ON CONFLICT DO UPDATE SET state = 'closed'`，与首次登记的 `ON CONFLICT DO NOTHING` 在同一主键上串行）；已存在（含 active/writing）时直接关闭。
  - closed 没有回到 active 的迁移，所以旧 Runner 的登记只能读到 closed，提交时 `active → writing` 失败。不依赖结果保存失败后的补偿关闭。
  - `PolicySession._open` 先判断状态再判断指纹，占位行不会被报成“配置已变化”。
- N3 根因：`sending` 没有尝试归属，`_FINISH_SEND` 只认当前状态，旧尝试返回可以落定新尝试；启动恢复又把仍在进行的独立重发当作遗留 sending。
- N3 修复：
  - 迁移 003（schema v3）给 `xiaowei_request` 增加 `delivery_attempt` 与 `delivery_owner_pid/started`。
  - 每次取得写入新的随机尝试标识：`claim_send` 返回 `DeliveryClaim`，`finish_send(claim, outcome)` 只落定同一标识并清空它；落定失败时锁低该实例 readiness，不改写状态。
  - `runtime.resend` 发送期间经 `storage.hold_backend` 占用一条连接，并把它的 pid + backend_start 记入尝试；退出（正常、异常、取消）时作废并关闭该物理连接，不归还连接池，并在期限内确认其身份已从 `pg_stat_activity` 消失，正常退出时无法确认即报错（第五轮 N3.1：原实现只归还连接池，引擎销毁前恢复仍视其为存活所有者，遗留 sending 被跳过）；`_RECOVER_SENDING` 只把没有存活所有者的 sending 记为 unknown 并作废其尝试。serve 的尝试不记所有者：旧实例失去锁即失去投递权。
  - `ResultDelivery.send` 在发送本身抛错或被取消、而落定失败时，原样传播原异常（含取消）。
  - 契约不变：首次发送只从 pending 取得，显式重发只从 completed + failed/unknown 取得；unknown 不自动重发；Agent、查询与 Evidence 不重跑；Evidence 重验、授权与目标绑定不变。
- 修复前（`49ed6df`）新增 18 项中 14 项失败：
  - start 在新实例持锁后仍运行（未抛 `NotReadyError`）；
  - 恢复早于或并发于首次登记时，旧 Runner 仍完成（未抛 `TurnError`）；
  - 首次发送与显式重发 × 旧尝试 sent/failed/unknown/异常/取消共 10 项，新尝试被改写；
  - serve 启动恢复把进行中的独立重发改为 unknown。

  另 4 项修复前已通过，作为对照与守护：持锁实例正常运行、active/writing 时恢复、并发重发只发一次。
- 修复后：上述 18 项、存储层的尝试不可复用与单次 sent/failed/unknown 对照、v2→v3 升级保留请求均通过。恢复后同一会话语境的新轮次在模型前以 `session_failed` 失败；新建会话后的新轮次完成，模型输入不含中断轮次内容。
- 既有用例调整：
  - 投递接口改为尝试对象（`sum(claims) == 1` 改为“恰好一个取得”）；
  - schema 版本断言 2 → 3；
  - `test_cleanup_removes_retired_sessions_that_never_ran` 中被恢复的会话现在有已关闭占位，清理计数由 (0, 3) 变为 (1, 2)，三者仍全部删除。
- 隔离变异 8 项全部被发现：start 不核对、恢复不写占位、占位遇到已有会话不关闭、落定不核对尝试、尝试标识可复用、恢复不区分存活的重发、重发不记录连接身份、发送异常/取消时落定失败覆盖原异常。N1/B2.1 的 7 项与 B2.2/B3 的 3 项复跑仍被发现。

**Task 8 未覆盖：** 真实模型、真实 StarRocks、真实飞书长连接与发送（Task 9，需授权）；正式 `serve` 的成功轮次只在进程内替换模型后证明，子进程中的正式命令只证明到模型失败回执。丢锁检测依赖连接终止通知：服务端终止或本端收到断开时立即生效；网络中断而本端未收到断开时，仍要等周期核对（`lock_check_seconds`，默认 5 秒，加上命令超时）发现。丢锁时已在运行的轮次在停止期限内继续调用模型与 StarRocks，结果因数据库条件更新不能覆盖新实例的恢复结论，但这些调用发生在无锁状态（立即取消需改变取消契约，未做）。旧实例的接收事务若在提交前长时间卡住（如网络分区下未断开），新实例的恢复在接收屏障上等待，超过命令超时即启动失败、需重试；持有者核对依赖同一数据库角色能读到持锁后端的 `backend_start`，读不到时按丢锁拒绝接收（失败关闭）。“取得锁后、注册通知前断连”的交错无法在正式进程中确定性注入，由对 `watching` 的直接用例与终止通知端到端用例共同证明。第三方依赖的日志不再输出，其中可能有助排障的错误需靠小维自身的原因码与退出码定位。N2/N3 用例在存储与服务层模拟“错过终止通知”和跨进程交错，正式进程中无法确定性制造。旧实例在接管前已启动的轮次仍会调用模型与工具（不可撤回，取消契约未改），只是结果与历史都不能落定。独立重发进程若在发送中崩溃，它的 sending 要到下一次 serve 启动恢复才记为 unknown；重发占用的连接身份同样依赖同一数据库角色可读 `pg_stat_activity`，读不到时按无所有者处理（记为 unknown，保守）。旧实例在新实例恢复前把已接受请求记为 failed/busy（`reject_busy`）仍可能发生：请求终态为 failed 而非 interrupted，不运行，回执经所有权核对才能发送，未作为阻断处理。浏览器验收与 compose-smoke 的新镜像只在本机或 CI 运行过一次（浏览器用例未纳入 CI）。`.env.example` 仍是旧 M5 变量说明，未改。新产品 Compose、持久卷与备份恢复属 P3。

**Task 9 离线部分（候选 `16f80d4`，干净工作树，2026-10-02）：** 完整测试、`-m security`、文档检查、正式 `serve` 浏览器 smoke、Ruff/format、mypy、锁文件一致性与 `pip-audit --strict` 均通过（数字见第 5 节）；主线 CI 在 `16f80d4` 上通过。

**下一项：** 按用户 2026-10-02 的决定，Task 9 真实部分推迟到 P3 的 Docker Compose 部署完成后，在获准环境中与 P2、P3 实战一并验证；P1-B 只记为离线完成，真实退出证据保持打开（届时所需授权与环境事实仍为：真实模型 Profile 与预算（G4）、StarRocks 目标与只读账号（G1、G5）、四种投影与保留期（G2）、业务口径（G3）、Web 访问方式（G6）、飞书测试应用（G7））。当前在 P2：

- **Task 1（`8291631`，独立审查通过）：** `ToolPolicy.data_scope` 与 `data_scope_digest(target)`（目标、默认库、排序后的对象/列/函数、`max_rows`、`max_sql_bytes`、`max_result_bytes`、`max_value_bytes`）进入策略指纹；`data_scope=None` 时指纹与基线相同。离线证据：以收窄范围（移除对象——含与证据无关的对象、列、函数，或降低四项上限任一）重新装配后，`project(model/session/web)` → `EvidenceUnavailableError`、`validate_answer` → `answer_rejected`、同会话续轮在首个模型调用前 `session_unavailable`；正式装配重启后网页历史读取 403、飞书重发 `ResultUnavailableError` 且不发送；同一范围（顺序/大小写不同）照常可读与续轮；摘要在 6 个不同 `PYTHONHASHSEED` 的子进程中一致；MCP/合成工具的已保存指纹等于基线值。完整离线 `tests/sdk_core tests/p1b -W error` 1012 passed（隔离 PostgreSQL），Ruff、format、mypy、`uv lock --check`、`git diff --check` 通过；8 项隔离变异全部被发现（指纹不含范围、对象/列按集合迭代顺序、漏列、漏函数、漏 `max_result_bytes`、漏 `max_value_bytes`、`None` 时仍写键）。用例放在 `test_starrocks_tools.py`（Catalog/Session）与 `test_runtime.py`（历史/重发），而非计划所列的 `test_session_policy.py`/`test_channel_service.py`：前者才有真实 StarRocks 工具装配。`-m security` 在新测试目录选中 0 个用例，不计为证据。
- **Task 0（已完成）：** `EXPLAIN_LEVEL = "LOGICAL"`。FE `query_explain_level=ANALYZE` 时裸 `EXPLAIN` 确实执行（BE 报 `assert_true` 失败；另一查询审计 `ScanRows=30000` 且新增 Profile），`EXPLAIN LOGICAL/COSTS/VERBOSE` 零执行；`COSTS` 输出列 min/max、`VERBOSE` 输出资源组名，均不合格。`tables_config` 中视图有一行（与计划原文不符，已更正）。**审计表的 `QueriedRelations` 在 AuditLoader 5.0.0 + 4.1.4 上全为 NULL，触发停止条件 (2)：Task 6 暂停，等待用户就计划 §7 E3 作出决定（已由 E3 方案 A 解除）。** 容器与合成数据已删除。
- **方向已定（用户 2026-10-02 确认）：** 数据库能力以后走外部 MCP Server（责任方待定），治理留在小维；新增阶段“P2.5 数据库 MCP 接入与多集群”，位于 P2 完成后、P3 之前，见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) §6 与 [接入约定](docs/contracts/database-mcp-server.md)。P2 仍用本地 asyncmy 直连完成。
- **Task 2（`20314cc`，独立审查通过，无阻断）：** `guard_explain_query` 与 `guard_readonly_query` 共用同一校验（`_guard`），唯一差别是不改写 LIMIT；产物 `ExplainQuery` 是独立封存类型，与 `GuardedQuery` 无继承关系、不可直接构造。离线证据：128 个查询拒绝样例在 explain 路径给出相同原因码与固定说明、异常链为空；`EXPLAIN`/`EXPLAIN ANALYZE`/`LOGICAL`/`DESC`/`TRACE`/`ANALYZE TABLE` 等前缀输入 `unsupported_syntax`；获准视图与表同等通过，未获准视图 `object_not_allowed`；规范化幂等，上一轮 `GuardedQuery.normalized_sql` 输入后不变。`tests/p1b tests/sdk_core -W error` 1146 passed（隔离 PostgreSQL），Ruff、format、`mypy src`、`git diff --check` 通过；3 项隔离变异（explain 改写 LIMIT、跳过对象/列校验、不封存）全部被发现。尚无调用方：Adapter 以固定级别执行与类型互斥属 Task 3。
- **Task 3（`9a11b45`，独立审查通过，无阻断）：** `StarRocksAdapter.explain(ExplainQuery)` 只发出 `EXPLAIN LOGICAL ` + 规范化 SQL（级别为模块常量，调用方不可选择），复用查询路径的槽位、期限、会话限额设置与回读、非缓冲有界读取与截断即断开；结果须恰为一列文本，列名固定为 `plan`，否则 `result_contract`；计划行数上限为新配置 `max_plan_lines`（默认 500），不随查询 LIMIT 或 `max_rows`。`run_query`/`explain` 互拒对方产物且不建连接。`data_scope_digest` 加入 `host`/`port`/`user`/`max_plan_lines`（部署后已有 StarRocks 证据一次性失效）；`password_ref`、期限与时区不在摘要内。离线 `tests/p1b tests/sdk_core -W error` 1175 passed（隔离 PostgreSQL）；本机可丢弃 StarRocks 4.1.4 真实测试 16 passed：表/视图/CTE 计划不含列统计与资源组，未授权对象 `permission_denied`，紧会话限额下可用与 `max_plan_lines` 截断；FE `query_explain_level=ANALYZE` 时 Adapter 对 `ASSERT_TRUE` 与 `SLEEP(5)` 只返回计划、Profile 不增，阳性对照裸 `EXPLAIN` 确实执行，结束恢复 NORMAL。10 项隔离变异全部被发现。未覆盖：工具登记、带前缀 SQL 比 `max_sql_bytes` 多 16 字节与投影最坏容量（Task 4）；物化视图改写的计划输出未实测。
- **Task 3 审查的非阻断项：**
  - **Task 4 验收门槛：** 登记 `explain_query` 前须先让返回的 `sql`（带 16 字节前缀）不超过投影上限，并让启动时 `worst_case_observation` 按 `max_plan_lines` 与带前缀 SQL 计算最坏容量。
  - **技术债：** `dataclasses.replace` 能绕过 `ExplainQuery` 与 `GuardedQuery` 的封存，仅进程内代码可为，风险低。复查条件：出现新的构造或复制这两个产物的代码时修复。
  - **披露检查不完整：** 真实测试只断言计划不含 column statistics 与 `RESOURCE GROUP` 两个标记，能防选错级别，不是完整披露审查。StarRocks 升级版本时按 Task 0 方式重测零执行与披露范围。
  - **未验证的低风险：** 客户端超时只断开连接、不发 `KILL QUERY`（与查询路径相同）；EXPLAIN 只在 FE 优化不执行，但断开后 FE 优化是否立即停止未实测。
  - **缺口：** 物化视图改写时的计划输出未实测（Task 0 亦未测）；物化视图名在已批准披露范围内，输出是否带其他内容待测。
  - **流程偏离（审查接受）：** 先实现后补测试，以 10 项变异全部被发现作补充证据。
- **Task 4（`078cc61`，独立审查通过，无阻断）：** 登记 `local/explain_query`（`Prechecked`：`guard_explain_query` 在授权后、预算与任何 I/O 前同步运行，拒绝为“执行计划未获取（原因码）”；执行只调 `adapter.explain`）。查询与诊断用途都可见，诊断用途仍不含 `run_readonly_query`。`ToolPolicy.fact_note`（不进指纹、不存证据）由交付代码取当前登记策略，附在来源行后（飞书经 `_one_line` 转义）并给出 `DeliveryFact.note`，Web 以 `textContent` 显示；事实区标题改为“工具结果（系统根据证据生成）”。Task 3 的两项门槛已满足：`worst_case_observation` 的 SQL 上限改为 `max(max_sql_bytes + len(EXPLAIN_PREFIX), 元数据模板)`；计划行受 `max_result_bytes` 约束，与查询共用同一最坏容量。示例配置开放 `explain_query` 并写出 `max_plan_lines`。离线 `tests/sdk_core tests/p1b -W error` 1190 passed、`-m browser` 3 passed（含正式入口 Chrome 中计划说明的显示）；正式入口（`runtime.serve` + Web API）成功与越权拒绝零 I/O 场景通过；8 项隔离变异全部被发现。未覆盖：真实 StarRocks 未重跑（Adapter 未改，沿用 Task 3 证据）；真实模型是否选择 `explain_query` 留到 Task 7 / P3。
- **Task 4 审查的非阻断项：**
  - **升级影响：** 配置开放 `explain_query` 后 `model_tools` 变化，已有会话按既有绑定规则拒绝并提示新建；最坏容量增加 192 字节（SQL 与列名各多 16 个最宽转义字符，各 96 字节），原本刚好卡在边界的 `projection_bytes` 启动时被拒绝，需要调大。
  - **计划偏离（审查接受）：** 示例配置的 `starrocks.audit` 留到 Task 6（该字段届时才存在）；`test_facts_header_is_neutral` 未单独成用例，标题断言分散在多个测试中。
  - **Task 7 验收重点：** `PLAN_NOTE` 只出现在交付内容中，模型收到的工具结果不含它；模型能否写明“计划只是估算、未执行原查询”依赖工具说明与 Task 7 的诊断指令。
- **Task 5（`1abaca0`，独立审查通过，无阻断）：** `StarRocksAdapter.describe_layout(name)` 与工具 `local/describe_table_layout`（与 `describe_table` 共用对象前置检查，Adapter 内再查一次）：代码模板 + 绑定参数读 `information_schema.tables_config` 的表模型、分区键、分桶方式与键、桶数、排序键、主键，不读 `PROPERTIES`/`TABLE_ID`；视图以 `TABLE_ENGINE <> 'VIEW'` 排除，返回空结果（计划 §2.5 二选一，选空结果）。键字段按逗号拆分、去反引号后每个名字都须是该对象的获准列（精确匹配），否则整段替换为“（含未获准列，未显示）”，不保留获准部分；无法拆成列名的表达式同样替换。多于一行、列不符、键非文本或被截断 → `result_contract`。诊断与查询用途都可见；策略共用同一 `data_scope`、无 `fact_note`；`metadata_sql_bytes` 计入新模板。离线 `tests/sdk_core tests/p1b -W error` 1219 passed；正式入口（`runtime.serve` + Web API）布局成功与越权零 I/O 通过；本机可丢弃 StarRocks 4.1.4（同一 digest）真实 17 passed：DUP/表达式分区（`date_trunc('day', ts)` 只显示 `ts`）/主键表含未获准列被整段隐藏/视图与无权限表无行；9 项隔离变异全部被发现。未覆盖：物化视图与外表的布局取值；生产版本的 `tables_config` 列（P3 复核）。
- **Task 5 审查的非阻断项：**
  - **升级影响：** 配置开放 `describe_table_layout` 后 `model_tools` 变化，已有会话按既有规则提示新建；数据范围摘要不变，已有证据不失效；最坏容量基本不变（新模板短于 `max_sql_bytes + 16`）。
  - **已改：** 工具说明改为“视图或当前账号看不到的表返回空结果”，避免模型把空结果解读为视图（说明文字不进入指纹）。
  - **P3 复核：** `model` 与 `distribute_type` 原样透传；测物化视图、外表时核对这两个字段的实际取值，必要时改为只接受已知枚举。
  - **Task 7 诊断指令须写明：** 表达式分区只显示列名、不显示粒度（`date_trunc('day', ts)` 只显示 `ts`），分区裁剪效果须结合计划中的 `partitionRatio` 判断。
  - **可选测试缺口：** 布局被拒后再调合法工具证明预算未占；单值超限截断的布局契约用例。两者走已有机制与同一路径。
- **Task 6（`f3a5c2c`，独立审查通过，无阻断）：** `StarRocksTarget.audit: AuditSource | None`（库表名 `[A-Za-z0-9_]{1,64}`、审计时区、必填 `stmt_limit`、窗口/输出/候选行数与候选字节上限；配置校验要求 `max_sql_bytes <= stmt_limit - 4`、`candidate_rows >= max_rows`、`candidate_bytes` 容得下一条最长候选）。`StarRocksAdapter.slow_queries(window, order_by)`：代码模板，库表名为校验后的反引号引用、排序列取固定映射，其余（长度上限、`default_catalog`、库、窗口起止、候选行数）全部绑定；超过 `max_sql_bytes` 的原文不读出；候选按独立的 `candidate_bytes` 与放宽的单值上限读取，达到即断开并标记截断。每条候选在线程中（计入客户端期限）经 `guard_explain_query`，任何失败（含解析器意外异常）都只丢弃该行，原文不进结果、日志或错误；输出固定 12 列，NULL/空串/负值为 `null`，CPU 以 `Decimal` 换算毫秒；无时区审计时间按审计时区解释。新错误码 `object_missing`（5502/1146，适用于所有 StarRocks 查询路径，原为 `query_failed`）。工具 `local/list_slow_queries` 只在配置审计源时登记，两种用途可见；`ServeConfig` 在未配置审计源时拒绝开放或授予它；`AUDIT_NOTE` 由策略渲染；`data_scope_digest` 加入审计源（部署后已有 StarRocks 证据再统一失效一次，与计划 §2.0 一致）。示例配置给出官方默认库表名。离线 `tests/sdk_core tests/p1b -W error` 1294 passed；`-m security` 在这两个目录选中 0 个（计划 §5 已说明不构成证据）。正式入口：只列出获准记录并带说明；审计表不存在时轮次失败、不重试、不保存。真实（本机可丢弃 StarRocks 4.1.4 + 官方 AuditLoader 5.0.0 jar，`plugin.conf` 只改 `max_stmt_length=1000`，新开关 `-m starrocks_audit_real`）2 passed、连跑 3 次稳定：表/视图/CTE/子查询的获准记录与小维自身的获准查询列出；未获准列/对象、跨库、子查询引用未获准表、注释、超长 ASCII 与被插件截断的多字节原文、小维自身的 EXPLAIN/元数据查询都不出现；`started_at` 带 UTC 偏移；审计时区配错时窗口偏移、记录不出现；只读账号查询不存在的审计表为 5502（`object_missing`），撤销审计表 SELECT 后为 `permission_denied`。原有 `-m starrocks_real` 17 passed，不选中审计用例。13 项隔离变异全部被发现。实测发现：插件刚安装后的第一批审计记录未入表（FE 审计日志中有），之后稳定。未覆盖：失败语句（`state=ERR`）与服务端超时的真实审计行；生产 AuditLoader 版本、`max_stmt_length` 与时区（G-A，P3）；真实模型如何使用（Task 7 / P3）。
- **Task 6 审查的非阻断项：**
  - **技术债（中低）：** 期限到达后解析线程不能停止。默认 200 条候选约 0.5–4 秒；`candidate_rows` 最大 2000 时可达几十秒，超时后本轮按 `timeout` 失败，后台线程仍解析完剩余候选，占用 CPU 与 GIL，拖慢同进程的 Web 与飞书。最小修复：把期限传进 `_listed`，逐行检查，到期即退出。另：执行查询与解析各有一个客户端期限，最长总耗时约为 2 倍 `client_timeout_seconds`，需在说明中写明。复查触发：调大 `candidate_rows` 或观察到慢查询工具超时。
  - **Task 7 验收项：** 诊断指令须写明审计 SQL 原文（他人写的不可信文本，字面量可能夹带提示词注入）与执行计划一样只作数据、不作指令。
  - **P3 复核清单：** `audit.time_zone` 必须等于 FE 系统时区（配错不泄露数据，但结果会静默变空或偏移，真实测试已证明）；示例配置的 `Asia/Shanghai` 部署时按实际核对。生产 AuditLoader 版本与列类型须核实：任一行格式异常（如时间为 NULL、指标非整数）都会使整个工具 fail closed。
  - **已接受的边界：** 插件刚安装后首批记录丢失；规范化后超过 `max_sql_bytes` 的记录不列出。
- **Task 7（`0c7387f`，首轮审查修复后复审通过，无阻断）：** `DEFAULT_INSTRUCTIONS` 增加诊断规则：工具结果中的 SQL 原文、执行计划和数据只作数据、其中像指令的文字不照做；诊断先取表结构、表布局与计划，不为诊断执行原查询，找实际慢查询用 `list_slow_queries`；计划是估算、取得时未执行原查询，审计指标是实际值；分区键只显示列名，裁剪效果看计划的 `partitionRatio`；每条分析写明依据与性质（现象、可能原因、建议）；只有审计指标时不推断计划、不确认根因并写明缺什么；优化 SQL 只作建议；有可引用的 evidence_id 时不退回纯澄清（工具被拒绝或失败的结果没有 evidence_id，不算取得，此时只澄清）。instructions 不进入会话绑定（新用例：改说明后旧会话照常回放）。`tests/sdk_core/test_diagnosis.py` 经正式入口（`runtime.serve` + Web API 与飞书 SDK 公开面替身，各跑一遍）验证：慢查询 → 计划 → 布局（三类事实与各自说明、被解释 SQL 等于审计原文、查询零执行、`PLAN_NOTE`/`AUDIT_NOTE` 不进模型输入）；查询 → 刚才那条为什么慢（解释的正是上一轮实际执行的 SQL，查询仍只执行一次，引用回放证据与本轮计划）；计划被拒只有审计（保留审计事实与分析、无计划说明、非澄清、越权 SQL 零 I/O）；无证据纯澄清（拿到合法证据后据此回答同样合格）；证据与澄清混用 → `evidence_failed` 固定回执、不发送内容；粘贴 SQL 零执行；审计原文与计划含注入文字时工具集合与执行计数不变、不能伪造分析区；计划超时（客户端期限）与审计无权限 → 本轮失败、不重试、不续轮、不保存。StarRocks 替身为 `gate0.SyntheticStarRocks`（产品 Adapter、SQLGuard、审计过滤与工具说明原样使用，只换最底层连接）。`gate0.py` 新增不计入 Gate 0 的 8 个诊断样例（慢查询到计划、计划被拒仅审计、粘贴 SQL、上一轮查询与上一轮 SQL、无证据、计划注入、审计注入）与 `judge_diagnosis`：诊断轮查询零执行且未展示查询工具、引用的事实只来自本样例产生的证据、各样例的引用与解释对象、只有审计时有分析且越权 SQL 未到驱动、无证据时纯澄清；报告只含类别计数，不含 SQL、消息或证据标识。计划偏离：样例用产品工具层 + 合成连接，而不是计划写的“合成工具”，使 P3 评估的是真实工具说明与 SQLGuard；`scripts/gate0_real_model.py` 尚未接入诊断样例（P3 再接，并补回答文本的人工评审）。离线 `tests/sdk_core tests/p1b -W error` 1416 passed（无 xfail）；首轮 11 项与复审修复 14 项隔离变异全部被发现。
- **Task 7 首轮审查修复（针对 `720d020`）：**
  - **F1 说明与回答契约冲突：** 原说明“已取得任何工具结果时照常引用”在工具被拒绝时（模型收到拒绝文字但没有 evidence_id）会诱导既不澄清又无引用的回答，被 `validate_answer` 拒为 `evidence_failed`。改为以“可引用的 evidence_id”为条件，并写明被拒绝或失败的结果不算取得。
  - **F2 飞书超限先保住分析（用户 2026-10-02 选 a）：** `EvidenceStore.validate_answer` 为飞书交付同时生成 `Delivery.layout`（事实来源/说明行、结果行、分析行；构造时校验与 `content` 逐行一致；每行不含换行；`exclude=True` 不进入 Web JSON）。`feishu.render(delivery, max_chars)`（首次发送与 `resend` 两个调用点）：不超限时与 `content` 逐字相同；超限时先放完整分析，剩余字数先给每条事实的来源与说明行、再按顺序给结果行，放不下的截掉并在分析前注明“（工具结果超过飞书单条上限，已截断）”（不指向 Web）；分析本身超限时保留“分析建议”标题、从末尾截并注明；无分段的澄清与固定回执仍从末尾截。整行取舍，必须截半行时按转义单位（`\uXXXX`、反斜杠加一字符）取前缀；分段来自结构而非在文字中找标题，伪造标题只出现在转义后的表格行中；总长不超过上限。正式入口用例：80 行计划时分析完整保留、计划从前往后截；计划含伪造标题与截断说明时真标题与真说明各只有一个。
  - **N1 合成替身误分类：** `SyntheticStarRocks.classify` 改为按 Adapter 模板常量全等（会话设置/回读、表清单、列、布局、各排序列的审计模板）与固定 EXPLAIN 前缀，其余一律为实际查询；补字面量含元数据表名、审计表名与会话前缀的获准查询经真实 Adapter 执行仍计为查询的反例。
  - **N2 判定冤枉合理作答：** `no_evidence` 只在本样例没有任何非会话语句到达驱动时要求澄清，取得合法证据后据此回答同样合格（取得证据后仍澄清不合格）；`previous_sql` 接受被解释 SQL 经查询规范化后等于上一轮实际执行的语句（模型可不照抄代码追加的 LIMIT）。
  - **N3：** 飞书用例的终态推断同时核对 completed 与 failed 计数，要求恰好一个加一。
  - **未改（审查明确不在本次范围）：** `tool_failed` 与 `answer_rejected` 共用一条渠道回执；未配置审计源时说明仍提及 `list_slow_queries`；`scripts/gate0_real_model.py` 接入诊断样例（P3）。
  - **残余风险：** 飞书超限时结果行按事实顺序填充，前面的事实结果很长时后面事实只剩来源与说明行；分析本身接近上限时工具结果可能只剩标题与截断说明。
- **Task 7 复审的非阻断项（`0c7387f`，复审另以 30000 组随机分段、转义与上限压测 `render` 性质，0 失败）：**
  - **澄清超长时正文整段丢失（上轮遗漏，修复前已有）：** 无分段交付以 `keep=0` 截断，第二行放不下时只剩“需要澄清”标题与截断说明。默认上限下基本碰不到，`max_reply_chars` 配到下限 200 时会出现。修法：无分段时 `keep=1`，保住第二行的前缀，并补断言。Task 8 处理。
  - **`ContentLine` 只禁止 `\n`（本次引入，防御深度）：** `\r`、U+2028 等分行字符能通过校验；实际内容都经 `_one_line` 转义，当前无风险，但校验弱于注释所述。修法：一并禁止这些分行字符。Task 8 处理。
  - **轻微误放（接受）：** `previous_sql` 判定按查询规范化比较，模型解释带 `LIMIT 100` 的版本也被规范化为同一条。
  - **已接受：** 作者列出的两项残余风险（前面事实很长时挤掉后面事实的结果行；分析接近上限时工具结果只剩标题），是选 a 的固有代价。私有模板常量在测试中引用可接受：模板变化时替身会归为实际查询，判定失败而不是误放。
- **Task 8（`78e3f38`，文档修订在其后，独立审查通过）：** P2 离线退出候选。处理前序审查积压：
  - **慢查询解析期限（Task 6 技术债）：** `_listed` 接收与 `asyncio.timeout` 相同的 `time.monotonic()` 期限，逐行核对，到期抛出 `TimeoutError`，本次调用按 `timeout` 失败后线程不再检查剩余候选。修复前用例：期限到达后线程又检查了 9 行。最坏总耗时约为 2 倍 `client_timeout_seconds`（读取候选与解析各一个期限），已写入 README 与代码注释。
  - **封存产物 `replace` 绕过（Task 3 技术债）：** `GuardedQuery` / `ExplainQuery` 的封存标记改为 `InitVar`；`dataclasses.replace` 必须显式给出它，不能复制封存产物再换 SQL。`copy.copy` 原样复制不受影响。
  - **Task 7 复审非阻断 1、2：** 无分段的飞书交付以 `keep=1` 截断，澄清正文放不下时保留前缀而不是只剩标题；`ContentLine` 拒绝 `str.splitlines` 的全部分行字符（`\r`、`\v`、`\f`、`\x1c`–`\x1e`、`\x85`、U+2028/2029）。
  - **文档：** ARCHITECTURE（诊断范围、§5 工具表含 `describe_table_layout`、`list_slow_queries` 必交且未配审计源不登记、`get_query_profile` 暂不实现、`EXPLAIN LOGICAL` 的选择与零执行依据、MCP 化后审计过滤仍在小维、数据范围摘要缺口更新、飞书截断规则、证据可读性绑定数据范围的第 5 条）；README（诊断用法、审计源各字段与限制）；DEVELOPMENT_PLAN（阶段表、P2 离线完成状态与 P2.5 前提）；P2 计划 §9 实施结果与偏差。该历史提交当时未确定新增需求阶段名；当前顺序以本节“已决定与下一步”及 DEVELOPMENT_PLAN 为准。
  - **验证（`78e3f38` 干净工作树）：** `tests/sdk_core tests/p1b -W error` 1419 passed；整仓 `uv run --locked --extra dev python -m pytest -q` 3137 passed、152 skipped；`-m security` 927 passed、79 skipped；文档检查（`test_doc_fact_binding`、`test_docs_command_consistency`、`test_capabilities_doc`）8 passed；浏览器（本机 Chrome）3 passed；`ruff check .`、`ruff format --check src/xiaowei tests/sdk_core tests/p1b`、`mypy src`、`uv lock --check`、`git diff --check` 通过；`pip-audit --strict`（导出的 dev 依赖）无已知漏洞。本机可丢弃 StarRocks 4.1.4（同一 digest）+ 官方 AuditLoader（`max_stmt_length=1000`）：`-m starrocks_real` 17 passed（含 FE 级别为 ANALYZE 时 `EXPLAIN LOGICAL` 零执行的反例），`-m starrocks_audit_real` 连跑 3 次各 2 passed；加 `-W error` 时 4 例因已知的 asyncmy `_finish_unbuffered_query never awaited` 警告失败（截断与中止路径，计划命令不带 `-W error`）。插件刚安装后首批记录再次被中止，与 Task 6 一致。4 项隔离变异全部被发现（去掉逐行期限、封存标记改回普通字段、澄清不保留前缀、行校验只拦 `\n`）。容器与测试 PostgreSQL 用后删除。
  - **未覆盖：** 真实模型的诊断质量与 `scripts/gate0_real_model.py` 接入诊断样例；两端实战路径；目标版本的计划格式、披露与零执行复核；生产审计源事实（G-A）；物化视图改写的计划与布局取值。
- **Task 8 审查结论：** 独立审查（`03f0721`）通过、无阻断：慢查询解析期限（变异确认）、封存产物 `replace` 一律 `TypeError`（pickle/deepcopy 照常）、飞书无分段截断 `keep=1`、`ContentLine` 拒绝 10 种分行字符均核对成立；离线 `tests/sdk_core tests/p1b -W error` 1419 passed、文档检查 8 passed、ruff/format/mypy/diff check 通过，其余证据引用未重跑。文档建议 D1（三项新需求未记录）与 D2（ARCHITECTURE §5 旧的“普通 EXPLAIN”说法）在合并前补齐，只改文档。
- **已决定与下一步：** 产品边界见 ARCHITECTURE §5–§7：单 Agent 自行判断自然语言，不增加前置分类；P2.5 不设查询前人工确认。计划 v2 已复审通过；Task 0 经复审随 PR #32 合入。下一项为 Task 1（本分支）的独立审查；D1/D2 小修复 PR 须在 Task 3 前完成，Task 4 另等 §9.9 调整被接受。群计划区分仅排队与已经开始的重启恢复；数据权限区分确定撤权与暂时无法验证。其余产品实施、真实模型/用户 StarRocks/飞书调用与部署仍未授权。
- **规划验证：** 计划 v2 在 `719c1c7` 通过复审（文档检查 8 passed、相对链接有效、`git diff --check` 通过）。
- **P2.5 Task 0（PR #32 经复审合入 `2967800`）：** 本机可丢弃 StarRocks 4.1.4（同一 digest）+ AuditLoader 5.0.0、合成数据、只读账号实测；证据、复现方法、安全输出与清理见 [P2.5 计划 §9](docs/superpowers/plans/2026-10-03-p25-open-read-multi-cluster.md#9-task-0-证据2026-10-03已复审)。要点：零行 SELECT 探测可证明当前权限（不被优化消除，撤权对同一连接立即生效）；4.1.4 不支持列级授权，角色默认不激活；`information_schema` 可见不等于可 SELECT；表的版本元组可发现重建与列变化，**视图元组不能发现内层视图或底表变化（只读账号无权读取它们），依赖视图的历史回放保持关闭**；1,000 表 / 30,000 列单项计时与推算口径见 §9.3（Adapter 探测 128 次 0.59 s 不含 SQLGuard，无端到端刷新计时）；GROUP BY 漏列在 EXPLAIN 阶段为 1064 分析错误，`SUM(文本列)` 静默隐式转换；1064 同时用于语法/语义/执行期错误，可恢复白名单须按阶段与消息前缀判定；LIMIT 查询计划估算 2 行而实际扫描 1,000,000 行；分区/tablet 比例按个数计，倾斜时“比例 × 总行数”低估约 29–113 倍，保守上界须取最大的 a 个分区之和并处理元数据上报滞后；缺统计不能只靠计划文本识别；FE ANALYZE 级别下 LOGICAL 仍零执行；资源组的 scan/CPU/并发与三种内存限制均有独立命中，但只读账号只能观察单条语句当下命中的组，不能证明后续绑定。既有 SR 实测开始/结束各 19 passed（6 个已知 asyncmy 警告）。
- **Task 0 发现的现有缺陷（未修改代码）：** D1 现有 SQLGuard 在输出名重复时把 `ORDER BY` 序号改写到同名的另一列（查询与执行计划产物相同；错误 SQL 会发送，但顶层重名样例在 Adapter 因重复列名按 `result_contract` 拒绝、业务行读取为 0，治理层停止本轮且不记录成功 Evidence；诊断路径分析的是改写错误的 SQL；GROUP BY 序号与唯一名正确；内层重名、外层唯一的可交付影响待 D1 小 PR 验证）；D2 `||` 被改写为 OR，而 Adapter 未固定 `sql_mode`，目标开启 `PIPES_AS_CONCAT` 时语义改变。复现与最小修复见计划 §9.8；**已决定另开小 PR 修复，排在 Task 3 之前。**
- **P2.5 Task 1（`claude/p25-task1-target-routing`，待独立审查）：** 配置改为 `targets` 列表（重复 ID、未知类型、非法 ID、目标外字段与旧单目标格式均在启动时拒绝且不回显配置值）；工具目录按 `(tool_id, target_id)` 查契约，模型只见一套工具且 `cluster` 必填，未知/缺失 `cluster`、不在本轮范围或未授权的目标在任何 StarRocks I/O 前拒绝，参数中的 `cluster` 与请求目标不一致时治理层拒绝；Evidence、回放、交付与 MCP 绑定全部改用实际目标。离线证据：三个目标同名库表、驱动返回不同常量，经真 Runner 并行各查一次只到达所指集群，事实按目标交付，下一轮回放三条证据均可用；正式入口（Web + 测试 PostgreSQL + 驱动替身）两集群成功与未知集群零 I/O 拒绝；只有配置审计源的集群提供慢查询。隔离变异（删参数-目标一致性校验、按 tool_id 单键查找、包装层忽略 cluster、删目标范围校验）均使对应用例失败。手写 allowlist 仍按目标保留到 Task 2。
- **缺口：** §2.4 风险评估按 §9.9 调整（最大 a 个分区上界 + 新鲜度 + 统计健康度 + 数据库硬限额），**Task 4 继续暂停到审查者接受调整**；资源组绑定的核对方式随 Task 4 决定；Task 2/5 只按表版本元组实施，视图历史回放保持关闭。未覆盖 TLS 目标、存算分离、外部 catalog、生产规模与并发、BE 侧取消残留、千视图 `SHOW CREATE VIEW` 耗时、端到端刷新计时与统计上报失败。
- **仍待后续决定：** DDL 的实际执行路径与实施范围，TiDB/MySQL 在 StarRocks P2.5/P3 完成后再议。表结构权限可见性、风险计划解析和群协议等技术疑点在新计划 Task 0/F0 中设门槛，不以假设关闭。


P1-A 是内部核心。P1-B 才接真实查询与双入口并切换正式入口，P2 增加诊断，P3 做实际用户验收。Gate 0 是 Task 3 开工前例外；其他环境缺失不阻塞不依赖该环境的离线部分，但不能跳过对应实战退出条件。

## 4. 实测环境与兼容性缺口

| 对象 | 当前状态 | 实测前所需信息 |
| --- | --- | --- |
| OpenAI 模型 API | Responses 路径已实现，HTTP mock 通过；未实测 | 获准端点/协议/模型 ID、凭据安全引用、数据范围与预算 |
| Gemini 模型 API | Chat Completions 路径已实现，HTTP mock 通过；真实 API 部分验证（工具选择、同轮签名回传、类型化回答），闭环因 503/429 未完成；2.5 系列对新用户不可用 | 有额度的密钥；补完工具续轮到交付、追问回放与诊断范围 |
| DeepSeek 模型 API | Chat Completions + json_object 已实现，HTTP mock 通过；官方端点未实测。第三方中转的 V4 Flash 已实测：请求带 `response_format` 时不调用工具，不能用于本产品路径 | 官方或其他获准端点；单独验证 JSON mode、工具与推理参数组合 |
| OpenAI 兼容中转（`bbtoken.boywe.cn`） | `glm-4.7-flash`（Chat Completions、`json_object`）Gate 0 真实运行通过：单次运行、合成工具与合成数据，测试密钥已由用户负责作废 | 正式使用需重新确认端点、密钥、数据接收范围与预算；未用真实模型调用 StarRocks 工具 |
| 外部 MCP Server | 只有 loopback 测试 fixture；未接入任何真实 Server | 具体 Server、端点、认证方式、工具的实际行为与返回契约、网络与凭据边界 |
| PostgreSQL / SDK Session | 隔离测试库已验证 SDK 表与应用表 v1 初始化、版本检查、Evidence 读写，以及 Session 策略包装的暂存提交、回放复核、上限、过期与失败隔离 | 物理清理命令（P1-B）、正式部署的保留期配置与备份恢复验证（P3） |
| StarRocks | 本机可丢弃的 StarRocks 4.1.4 容器上验证了 Adapter 协议（Task 2）与受治理工具端到端（Task 3）；用户环境未连接 | 目标版本、测试连接、只读账号、获准库表/视图（G1）、数据投影范围（G2）与简短业务口径（G3） |
| 飞书 | 只有 Evidence 飞书投影与纯文本交付的离线/测试 PostgreSQL 验证；正式渠道未运行 | 应用与事件配置、获准租户/单聊用户、可信身份来源 |
| 本机 Web | Task 6 组件 smoke；正式 `xiaowei serve` 在 Chrome 中离线验收（模型为本机关闭端口或进程内脚本） | 真实模型下的正式验收（Task 9）；HTTPS/SSH、操作者与 `Secure`（G6） |
| Docker Compose | 新产品双容器尚未交付 | 应用镜像、PG 持久卷、loopback/SSH 访问、启动检查与备份恢复实战 |

**CI 已知不稳定：** secret-scan 的“豁免窄度自检”每次用随机生成的值充当 secret，gitleaks 偶尔不把它判为泄露，该步骤随之失败（PR #22 首次运行，单独重跑后通过，全历史扫描无泄露）；与被测改动无关，修复待另开小任务。

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

P1-A CI 同步时的验证：`tests/contract/test_integration_gate.py` 与 `tests/security/test_workflow_policy.py` 共 33 项通过；真实 SDK 测试库上的全量 pytest 为 1742 passed、152 skipped；`ruff check .`、相关文件格式检查、`mypy src` 与 workflow YAML 解析通过。容器由退出清理路径移除。

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

Task 3 受治理工具验证（锁文件未变，测试 PostgreSQL 与 StarRocks 均为本机容器）：

| 命令 | 结果 |
| --- | --- |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_starrocks_tools.py tests/p1b/test_starrocks_tool_contracts.py -q` | 22 + 5 passed（首轮）；审查修复后见下文 |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q`；`-m security` | 2383 passed，152 skipped，12 deselected；923 passed，79 skipped。原有 `test_app.py` 4 个用例随 `run_turn` 返回值调整 |
| `SDK_TEST_STARROCKS_ADMIN_URL=mysql://root@127.0.0.1:59030 SDK_TEST_POSTGRES_URL=$SDK_PG … pytest tests/p1b/test_starrocks_real.py -m starrocks_real -q` | 12 passed（StarRocks 4.1.4）；用后无残留合成库 |
| 首轮审查修复：`SDK_TEST_POSTGRES_URL=$SDK_PG … pytest tests/sdk_core/test_starrocks_tools.py tests/p1b/test_starrocks_tool_contracts.py tests/sdk_core/test_session_policy.py tests/sdk_core/test_evidence.py tests/sdk_core/test_app.py tests/sdk_core/test_gate0.py -q` | 修复前新增的飞书敌意数据、澄清伪造与必需字段收紧 3 个用例失败；修复后 127 passed。全量 2386 passed，152 skipped，12 deselected；`-m security` 923 passed。反向验证 9 项（单元格不转义竖线、不做单行编码、不转义 U+2028/2029、不转义 C1、表格行无首尾竖线、分析/澄清/JSON 事实不做单行编码、指纹不含 `required`）均使对应用例失败。未重跑真实模型与 StarRocks |
| `ruff check .`、`ruff format --check src/xiaowei tests/p1b tests/sdk_core`、`mypy src`、`git diff --check` | 通过 |

Task 5 共享 ChannelService 验证（锁文件未变，测试 PostgreSQL 为本机容器）：

| 命令 | 结果 |
| --- | --- |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_channel_service.py tests/sdk_core/test_app.py tests/sdk_core/test_channel_store.py -q -W error` | 79 passed（新增 20 项）。实现先于测试；首次运行 2 项失败，均为用例或顺序问题（触发器未撤销；readiness 锁低时新建会话先报忙碌，改为先查 readiness） |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q`；`-m security` | 2461 passed，152 skipped，12 deselected；923 passed，79 skipped |
| `ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src`、`git diff --check` | 通过 |
| B1 修复：同上三个目标文件另加 `test_evidence.py`、`test_governance.py` | 新增 6 项（成功对照 1、装配拒绝 4、装配后替换 1，原装配用例保留）；修复前先写的代理与替换 2 项失败（值相等孪生与同对象其他方法 2 个边界在变异后补充），修复后 169 passed，`-W error` 通过。全量 2467 passed，152 skipped，12 deselected；`-m security` 923 passed，79 skipped；文档检查 8 passed |

Task 6 最小同源 Web 验证（锁文件未变，测试 PostgreSQL 为本机容器）：

| 命令 | 结果 |
| --- | --- |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_web.py -q -W error` | 64 passed。测试先于实现写成，实现前收集阶段因缺少 `xiaowei.web` 失败；首次运行发现 1 处用例错误（空 Content-Type 被默认值替换）与 2 个实现缺陷（终态写不了时仍返回 202“处理中”；首个 POST 失败时新换发的 cookie 丢失，已保存的请求无法再读取，改为只由页面换发 cookie） |
| 同上另加 `test_channel_service.py`、`test_app.py`、`test_channel_store.py`、`test_evidence.py`、`test_governance.py` | 233 passed，`-W error` 通过 |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q`；`-m security`；文档检查 | 2531 passed，152 skipped，12 deselected；923 passed，79 skipped；8 passed |
| `ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src`、`git diff --check` | 通过 |
| `python -m hatchling build -t wheel` | wheel 含 `xiaowei/web.py` 与 `xiaowei/static/` 三个文件 |
| 审查修复：修复前新增用例（`07a4c6c` 源码） | 4 项按预期失败：`http://127.0.0.1:0`、`http://127.0.0.1:80`、`https://localhost:443` 都通过配置（DID NOT RAISE）；表单没有显式 POST |
| 审查修复：`SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_web.py -q -W error` | 71 passed |
| 审查修复：同上另加 `test_channel_service.py`、`test_app.py`、`test_channel_store.py`、`test_evidence.py`、`test_governance.py` | 240 passed，`-W error` 通过 |
| 审查修复：`SDK_TEST_CHROME=<Chrome 路径> SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_web_browser.py -m browser -q -W error` | 1 passed（约 4 秒），连续 4 次通过，无残留 Chrome 进程；不设 `SDK_TEST_CHROME` 时 1 error（明确失败） |
| 审查修复：`SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q`；`-m security`；文档检查 | 2538 passed，152 skipped，13 deselected；923 passed，79 skipped；5 passed |
| 审查修复：`ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src`、`git diff --check`、wheel | 通过；wheel 仍含 `xiaowei/web.py` 与 `static/` 三个文件。新测试文件另以 `MYPYPATH=src mypy --explicit-package-bases` 检查无新增错误（测试目录原有 12 处） |
| 审查修复：隔离变异 11 项 | 全部使对应用例失败：去掉端口约束、只拒绝端口 0、只拒绝默认端口、CSP 去掉 `form-action`（ASGI 与浏览器各一）、表单去掉显式 POST、表单去掉 POST 且 CSP 去掉 `form-action`（浏览器中消息进入 URL）、`secure_cookie` 不生效、双向控制符不转义、`textContent` 改 `innerHTML`、cookie 非 HttpOnly |

Task 7 飞书单聊验证（锁文件新增 `lark-channel-sdk==1.4.0`，测试 PostgreSQL 为本机容器，未连接飞书）：

| 命令 | 结果 |
| --- | --- |
| 安装探针（仓库外独立 venv，离线帧实验） | `FeishuChannel` 分发器：处理器抛错仍写回 `code=200`；低层分发器：抛错写回 `code=500`。`raw` 事件字典含 header 的 `app_id`、`tenant_key`、`create_time` |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_feishu.py -q -W error` | 51 passed，连续 3 次通过。首次运行发现 1 个实现缺陷（发送替身抛错使消费者退出、整个渠道停止；改为状态已记 unknown 后记录并继续）；自查补上编号长度上限（超长编号会使 `InboundRequest` 校验异常逃出 `receive`）；`-W error` 暴露测试替身未关闭自己的事件循环，已修 |
| 同上另加 `test_channel_service.py`、`test_web.py`、`test_app.py`、`test_channel_store.py`、`tests/security/test_dependency_baseline.py` | 215 passed，`-W error` 通过 |
| `SDK_TEST_CHROME=<Chrome 路径> SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_web_browser.py -m browser -q -W error` | 1 passed（`channel.py` 有新增方法，Web 浏览器 smoke 复跑） |
| 隔离变异 21 项 | 全部使对应用例失败（见上文 Task 7 已证明） |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q`；`-m security`；文档检查 | 2590 passed，152 skipped，13 deselected；924 passed，79 skipped；3 passed |
| `uv export --frozen --no-emit-project --extra dev` + `pip-audit --strict` | No known vulnerabilities found |
| `ruff check src/xiaowei tests`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src`、`git diff --check`、wheel | 通过；wheel 含 `xiaowei/feishu.py`，元数据声明 `lark-channel-sdk==1.4.0`。测试文件以 `MYPYPATH=src mypy --explicit-package-bases` 检查无新增错误 |
| 审查修复：修复前（`58aa5f6`）新增回归 | B1/B3 共 23 项失败（正文 200 字符被判 `malformed`；保存后取消 readiness 仍为 true；`EvidenceStoreError` 被吞并误记为“结果不明”；消费者遇异常继续运行；`drain` 不存在）。B2/B4 用旧 API 复现：200 个事件启动 200 个协程；关闭 3 秒后仍在等待 |
| 审查修复：`SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_feishu.py -q -W error` | 81 passed，连续 3 次通过 |
| 审查修复：同上另加 `test_channel_service.py`、`test_channel_store.py` | 151 passed，`-W error` 通过 |
| 审查修复：`SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q`；`-m security` | 2620 passed，152 skipped，13 deselected；924 passed，79 skipped |
| 审查修复：隔离变异 12 项 + 首轮 21 项复跑 | 全部使对应用例失败（正文恢复 200 上限、去掉解析前大小检查、去掉桥接准入、取消不保护、再次吞掉一般异常、去掉关闭期限、重复停止再次关闭、恢复不处理未发送结果、恢复误改 Web、`drain` 不停止接收、`drain` 超时不锁低、发送异常不转为 unknown） |
| 审查修复：`ruff check src/xiaowei tests`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src`、`git diff --check` | 通过；测试文件以 `MYPYPATH=src mypy --explicit-package-bases` 检查无错误。依赖与锁文件未变 |
| 复审修复：修复前（`ee10951`）新增回归 | 4 项失败：落库前 `receive` 未结束时 `drain` 已返回 True、同一情形超时不锁低、命令与重复事件不计入等待、孤立代理码点不按 `content` 拒绝 |
| 复审修复：`SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_feishu.py -q -W error` | 85 passed，连续 3 次通过；另加 `test_channel_service.py`、`test_channel_store.py` 共 155 passed |
| 复审修复：`SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q`；`-m security` | 2624 passed，152 skipped，13 deselected；924 passed，79 skipped |
| 复审修复：隔离变异 5 项 | 全部使对应用例失败（`drain` 不等 `receive`、`receive` 不置未完成标志、`drain` 不等队列、不拒绝孤立代理码点、启动失败不复位接收） |
| 复审修复：`ruff check src/xiaowei tests`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src`、`git diff --check` | 通过；`test_feishu.py` 以 `MYPYPATH=src mypy --explicit-package-bases` 检查无错误（测试目录原有 12 处不变）。依赖与锁文件未变 |

Task 8 正式入口验证（锁文件只把 alembic 从生产依赖移到 dev/legacy extra，测试 PostgreSQL 为本机容器，未连接任何外部服务）：

| 命令 | 结果 |
| --- | --- |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_runtime.py tests/sdk_core/test_cli.py -q -W error` | 27 + 11 passed；`test_runtime.py` 连续 2 次通过。首轮发现：成功轮次的断言写法、重发测试复用已关闭替身、TOML 无法表达必填的 null（改用 JSON）、服务结束后用重绑端口判断释放受 TIME_WAIT 干扰（改为连接被拒） |
| `SDK_TEST_CHROME=<Chrome 路径> SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_serve_browser.py tests/sdk_core/test_web_browser.py -m browser -q -W error` | 3 passed，连续 2 次（Chrome，`127.0.0.1:8501`），无残留 Chrome 进程。同一会话连续运行时，Task 6 smoke 原用重新绑定判断端口占用，被前一用例的 TIME_WAIT 误判；两处探测改为“能否连上” |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q`；`-m security`；文档检查 | 2665 passed，152 skipped，15 deselected；927 passed，79 skipped；5 passed。工作流策略与 wheel 契约按新增作业和新打包契约更新后通过 |
| 本机复现 CI `product-entry`：`uv build --wheel`、`uv export --frozen --no-dev --no-emit-project`、新 venv 安装运行依赖与 wheel、两个入口 `--help` | 通过；运行依赖导出中没有 alembic 与 dev 工具，venv 中 `xiaowei_agent`、`alembic` 不可导入 |
| `uv export --frozen --no-emit-project --extra dev --extra legacy` + `pip-audit --strict` | No known vulnerabilities found |
| 隔离变异 16 项 | 全部使对应用例失败（见第 3 节 Task 8 已证明）；“alembic 回到生产依赖”用 `--frozen` 运行（`--locked` 会先因锁文件不一致拒绝执行） |
| `ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src`、`git diff --check`、`uv lock --check` | 通过；新测试文件以 `MYPYPATH=src mypy --explicit-package-bases` 检查无错误 |
| 审查修复：修复前运行新用例 | B1 配置用例 4 项未抛错（HTTPS-only 通过校验）；B2 两项在 `not_serving` 超时（终止锁连接 2 秒后 `/readyz` 仍 200）；B3 四项（两入口 × INFO/DEBUG）日志含 `HTTP Request` 或模型连接端口 |
| 审查修复：`SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_runtime.py tests/sdk_core/test_storage_v2.py tests/sdk_core/test_cli.py -q -W error` | 34 + 13 + 17 passed；`test_runtime.py` 连续 3 次通过 |
| 审查修复：浏览器（同上命令） | 3 passed，连续 2 次 |
| 审查修复：全量；`-m security` | 2680 passed，152 skipped，15 deselected；927 passed，79 skipped |
| 审查修复：隔离变异 9 项 | 全部使对应用例失败 |
| 审查修复：`ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src`、`git diff --check`、`uv lock --check` | 通过；依赖与锁文件未变 |
| 增量复审修复：修复前运行新用例 | B2.1 存储层恢复 interrupted=1（期望 2），正式装配最终状态 `['accepted', 'completed']`，错过通知时旧实例仍写入（未抛 `NotReadyError`）；B2.2 草稿脚本按 `f8c03af` 的注册方式在已关闭连接上注册，通知不触发；B3 两级别下 stdout 含 `[Lark]` 原始异常、stderr 含第三方 WARNING 的端点 |
| 增量复审修复：`SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_storage_v2.py tests/sdk_core/test_runtime.py tests/sdk_core/test_cli.py tests/sdk_core/test_channel_store.py -q` | 114 passed；接管与丢锁相关 9 项连续 5 次通过 |
| 增量复审修复：浏览器；全量；`-m security` | 3 passed；2686 passed，152 skipped，15 deselected；927 passed，79 skipped |
| 第三轮 N1：修复前（`7328d7f`）运行新用例 | 4 failed（轮换、首次创建、重复请求、取得投递权均未抛 `NotReadyError`），成功对照 passed |
| 第三轮 N1：`SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_channel_store.py tests/sdk_core/test_channel_service.py tests/sdk_core/test_runtime.py tests/sdk_core/test_storage_v2.py -q -W error`；另跑 `test_feishu.py`、`test_web.py`、`test_cli.py` | 126 passed；175 passed；接管与丢锁相关 14 项连续 3 次通过 |
| 第三轮 N1：全量；`-m security`；浏览器；文档检查 | 2691 passed，152 skipped，15 deselected；927 passed，79 skipped；3 passed；8 passed |
| 第三轮 N1：隔离变异 7 项 + B2.2/B3 复跑 3 项；ruff、format、mypy、`git diff --check`、`uv lock --check` | 全部使对应用例失败；检查通过，依赖与锁文件未变 |
| 第四轮 N2/N3：修复前（`49ed6df`）运行新用例 | 18 项中 14 failed（start 未拒绝、恢复早于/并发于登记未拒绝、10 项旧尝试改写新尝试、serve 恢复改写进行中的重发），4 项对照与守护 passed |
| 第四轮 N2/N3：`SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_channel_store.py tests/sdk_core/test_channel_service.py tests/sdk_core/test_runtime.py tests/sdk_core/test_storage_v2.py -q -W error`；另跑 feishu/web/cli/app/session_policy | 149 passed；240 passed；新竞态 26 项连续 3 次通过 |
| 第四轮 N2/N3：全量；`-m security`；浏览器；文档检查；wheel 含迁移 003 | 2714 passed，152 skipped，15 deselected；927 passed，79 skipped；3 passed；8 passed；是 |
| Task 9 离线：候选 `16f80d4` 干净工作树，`SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q`；`-m security`；文档检查（`test_doc_fact_binding`、`test_docs_command_consistency`、`test_capabilities_doc`）；`SDK_TEST_CHROME=… pytest tests/sdk_core/test_serve_browser.py -m browser -q -W error` | 2718 passed，152 skipped，15 deselected；927 passed，79 skipped；8 passed；2 passed（本机 Chrome） |
| Task 9 离线：`ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src`、`uv lock --check`、`git diff --check`、`uv export --frozen --no-emit-project --extra dev` + `pip-audit --strict`；主线 CI | 全部通过，无已知漏洞；`16f80d4` 的 CI 通过 |
| 第五轮 N3.1：修复前（`deb7450`）运行新用例；修复后同四个文件 + feishu/web/cli/app/session_policy + wheel 契约 + 全量 | 修复前所有者退出 3 项（正常/异常/取消）均 failed（退出后身份仍在 `pg_stat_activity`）；修复后 153 passed；240 passed；1 passed；2718 passed，152 skipped，15 deselected |
| 第五轮 N3.1：隔离变异 3 项（只关闭不作废、异常/取消路径归还连接池、不确认后端退出）；ruff、format、mypy、`git diff --check`、`uv lock --check` | 全部使对应用例失败；检查通过，依赖与锁文件未变 |
| 第四轮 N2/N3：隔离变异 8 项 + N1/B2/B3 复跑 10 项；ruff、format、mypy、`git diff --check`、`uv lock --check` | 全部使对应用例失败；检查通过，依赖与锁文件未变 |
| 增量复审修复：隔离变异 9 项；`ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src`、`git diff --check`、`uv lock --check` | 变异全部使对应用例失败；检查通过，依赖与锁文件未变 |

Task 4 请求与渠道状态验证（锁文件未变，测试 PostgreSQL 为本机容器）：

| 命令 | 结果 |
| --- | --- |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core/test_storage_v2.py tests/sdk_core/test_channel_store.py -q` | 测试先于实现写成。首次运行发现 1 个实现缺陷（就绪检查把 v1 报成“未初始化”，已修正）与 2 处用例自身错误（DELETE 触发器不能引用 `NEW`；恢复用例的请求共用一个会话），修正后 35 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q`；`-m security` | 2421 passed，152 skipped，12 deselected；923 passed，79 skipped |
| 审查修复：同上两个目标文件 | 新增 20 项回归；修复前 14 项失败（另 2 项为成功对照或并发用例，修复前也通过）；修复后 55 passed，`-W error` 通过；取消与竞争用例连续 10 次通过。全量 2441 passed，152 skipped，12 deselected；`-m security` 923 passed，79 skipped；文档检查 8 passed |
| 复审修复：同上两个目标文件 | `fail()` 拒绝内部码的回归在修复前失败（`result-not-saved` 用例 1 项），修复后 55 passed，`-W error` 通过 |
| `ruff check .`、`ruff format --check src/xiaowei tests/sdk_core tests/p1b`、`mypy src`、`git diff --check` | 通过；迁移 SQL 不含 `agent_` 表（用例核对） |

StarRocks Adapter 验证（锁文件新增 `asyncmy` 0.2.15）：

| 命令 | 结果 |
| --- | --- |
| `uv run --locked --extra dev python -m pytest tests/p1b/test_starrocks_adapter.py -q` | 实现前收集阶段因缺少 `xiaowei.starrocks` 失败；实现后 82 passed。审查修复：新增用例在修复前 7 个失败，修复后 93 passed |
| `SDK_TEST_STARROCKS_ADMIN_URL=mysql://root@127.0.0.1:59030 uv run --locked --extra dev python -m pytest tests/p1b/test_starrocks_real.py -m starrocks_real -q` | 11 passed（本机 StarRocks 4.1.4 容器，审查修复后重跑仍为 11 passed）；不设变量时 11 errors，非 loopback 地址同样失败；用后无残留合成库 |
| `python -m pytest -q --ignore=tests/sdk_core`；`-m security` | 审查修复后 2059 passed，152 skipped，11 deselected；923 passed，79 skipped |
| `ruff check .`、`ruff format --check`、`mypy src`、`pip-audit --strict`（导出的 dev 依赖）、`git diff --check` | 通过；无已知漏洞 |

SQLGuard 验证（同一环境，锁文件未变）：

| 命令 | 结果 |
| --- | --- |
| `uv run --locked --extra dev python -m pytest tests/p1b/test_p1b_sqlguard.py -q` | 实现前收集阶段因缺少 `xiaowei.sqlguard` 失败；实现后 159 passed。审查修复：新增用例在修复前 22 个失败，修复后 252 passed |
| `uv run --locked --extra dev python -m pytest -q --ignore=tests/sdk_core`；`-m security` | 1873 passed，152 skipped；923 passed，79 skipped |
| `ruff check .`、`ruff format --check src/xiaowei/sqlguard.py tests/p1b`、`mypy src`、`git diff --check` | 通过 |

独立审查：P1-A Task 1 经 Codex 审查 `bf8963d`、`6dbbb6b`、`5aee8f5` 均为暂不通过，复审 `8dba33a` 为本地技术验收通过；P1-B 各 PR 的审查结论见对应 PR。自查不称为独立审查；GitHub CI 是独立执行证据，不代表真实服务或产品运行。真实服务证据只有：Gate 0 以 `glm-4.7-flash` Profile 通过（合成数据）、Gemini 历史部分证据、本机 StarRocks 4.1.4 容器；用户环境的 StarRocks、正式浏览器、飞书运行与用户验收均无证据，未来生产 Action 仍只有设计约束。
