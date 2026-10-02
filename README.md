# 小维 · StarRocks 数据库助手

小维是一款以 **OpenAI Agents SDK** 为核心设计的数据库助手。首版目标是在 **Web 对话和飞书单聊** 中完成“查结构 → 只读查询 → 解释结果 → 分析慢查询”的连续对话。

> 下文描述首版目标，不代表仓库已经具备全部能力。当前实现、可运行入口和验证状态统一见 [AGENT_HANDOFF.md](AGENT_HANDOFF.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 首版目标

- 了解获准 StarRocks 数据库的表、字段和结构。
- 用自然语言生成 SQL，或提交自己的 SQL，在受控范围执行只读查询并解释结果。
- 继续追问“这条 SQL 为什么慢”，查看估算执行计划（不执行原查询），得到有依据的优化建议与局限说明。
- 在 Web 或飞书连续对话，查看实际 SQL、有限结果和诊断依据。

查询与诊断共用一个 Agent。StarRocks 工具是小维的受治理 function tools：P1/P2 开发期由本地 Adapter 直连数据库，P2.5 起改经外部独立维护的数据库 MCP Server 执行，首版试用时经该路径访问多个获准的 StarRocks 集群；治理始终在小维，本仓库不内嵌业务 MCP Server。另具备通用 MCP Client Integration，连接获准的外部工具服务。两条工具路径都在调用前复核权限、结果进入模型前过滤，最终发送前验证 Evidence。模型、会话、Web 与飞书分别接收各自允许的数据。

## 最小产品形态

| 项目 | 首版设计 |
| --- | --- |
| Agent 核心 | OpenAI Agents SDK 的 Agent、Runner、function tools、Session |
| 模型 API | 计划接入 OpenAI、Gemini、DeepSeek；通过配置选择经过验证的端点和模型，一次运行使用一个模型 |
| Web | 本机使用的简单对话页，显示文本、SQL、有限结果与执行提示 |
| 飞书 | 获准用户与企业自建机器人的单聊文本消息 |
| 数据源 | 多个获准的 StarRocks 集群，经外部只读数据库 MCP Server 访问，每个集群有独立授权范围（P2.5；P1/P2 开发期为一个本地直连的只读连接） |
| MCP | 官方 SDK 接入能力 + 小维可信配置与治理；配置为空时，本地功能照常运行 |
| 会话 | 两端分别保留上下文，共用业务逻辑；暂不跨渠道同步 |
| 运行与存储 | Docker Compose 管理小维与 PostgreSQL 两个容器；小维单进程，SDK Session 首版使用 SQLAlchemySession + PostgreSQL；飞书优先长连接 |

会话继续使用 SDK 公共 Session 接口；SDK 历史与应用记录各自管理表结构，共用 PostgreSQL 实例。数据保存在持久卷，并提供备份恢复办法。引入数据库不增加 Worker 或分布式调度。生产默认关闭 tracing 与外发；真实数据 trace 需要显式配置允许范围。

首版按以下方式使用，功能随实施逐项交付：

- Web 每条消息选择“查询/诊断”，默认诊断；飞书用 `/查询 内容` 或 `/诊断 内容`。普通消息用于解释和诊断，需要实际查询时再明确选择；上轮查过数据不代表本轮也允许执行。
- 查询结果中的数字、实际 SQL、表格和时间由程序按真实证据生成，AI 分析建议单独展示；不清楚时区、金额单位或指标含义时先问清楚。
- Web 点“新建会话”，飞书发 `/新建`；对话过长会提示新建，旧记录按保留期失效和清理。
- 请求失败时提供排障编号，用于区分模型、数据库、保存或飞书发送问题，日志不记录原始查询数据。
- 本机打开 Web；部署在服务器时通过 SSH 安全转发访问。Compose 的 Web 端口只发布到宿主机本地地址，PostgreSQL 不开放公共端口。

OpenAI Agents SDK 负责 Agent 运行，实际推理可由不同厂商提供。接入方案优先使用 SDK 原生 Responses / Chat Completions 模型能力；是否能完成工具调用、结构化回答和连续追问，按具体端点与模型实测，兼容性记录见 handoff。首版通过服务端配置切换，换模型开启新会话，不自动把旧对话发送到另一家；不用先建设模型管理后台。

慢查询来自目标上已有的 AuditLoader 审计表，按环境能力配置（见下文“审计源”）；Query Profile 暂不接入。缺少审计源时，仍可分析 SQL 与执行计划，但必须明确证据不足；不会自动修改目标配置来开启采集。

MCP 负责标准化工具接入，不能替代业务授权。只有参数含义、风险和结果策略已有支持的服务，才能主要通过配置注册；并非填写一个地址就可安全使用任意工具。

首版不执行数据修改、配置变更或优化建议；不建设复杂管理后台、多 Agent 或分布式任务平台。

未来接入生产变更时，数据库修改、配置调整、取消查询、重启与发布统一按具体 Action 管理。所有生产写操作都先展示目标、改动和影响，取得有审批权限的用户明确确认，再复核权限后执行；目标或关键参数变化需要重新确认。“帮我诊断/优化”不代表同意执行生产变更。

## 怎样开始

唯一正式命令是 `xiaowei`（`python -m xiaowei` 相同）。它已用测试 PostgreSQL、脚本模型与替身完成离线验证；真实模型、StarRocks 与飞书的实战验证尚未完成（见 handoff）。旧 CLI、Worker 或 Compose 命令不是产品入口。

实施时需要：

1. 开发使用 Python 3.11 与 uv，部署使用 Docker 和 Docker Compose；新依赖和镜像版本验证后锁定。P1-A 的存储验证也使用隔离的真实 PostgreSQL。
2. 一家获准的模型服务（OpenAI、Gemini、DeepSeek 或兼容网关），明确 API 地址、协议、模型 ID，以及对应 API key 的本机安全引用。
3. 获准的 StarRocks 测试连接、真实只读账号、数据库/视图范围、简短业务口径说明，以及允许向模型与渠道展示的数据。
4. 飞书企业自建机器人、消息权限、事件订阅及获准单聊用户。

凭据仅在本机或部署环境配置，不粘贴到对话、仓库、浏览器或日志。缺少真实环境时可以开发和离线验证，但不能标记对应实战验收完成。

**正式命令。** 复制 [examples/xiaowei.example.json](examples/xiaowei.example.json)，按获准环境填写模型 Profile、StarRocks 目标与 allowlist、授权表和 Web 地址；如需飞书，再加入 `feishu` 段。配置中的凭据只写 `env:NAME` 引用，变量在运行环境中设置：

```bash
uv sync --locked
export XW_DATABASE_URL=...   # postgresql+asyncpg://...，以及 XW_DIGEST_KEY、XW_MODEL_API_KEY、XW_STARROCKS_PASSWORD
uv run --locked xiaowei --config xiaowei.json storage init      # 全新数据库；已有 v1/v2 用 storage upgrade（先备份）
uv run --locked xiaowei --config xiaowei.json serve             # Web 默认 http://127.0.0.1:8501，Ctrl-C 停止
uv run --locked xiaowei --config xiaowei.json storage cleanup --batch-size 100
uv run --locked xiaowei --config xiaowei.json requests resend --subject <subject> --chat <chat_id> --message <message_id>
```

`serve` 持有数据库实例锁并在启动时执行中断恢复，第二个实例会被拒绝；普通启动不建表、不升级。`requests resend` 只重发飞书中投递为 failed/unknown 的已保存结果，不重跑模型或查询。退出码：0 成功，1 运行失败或请求被拒，2 参数或配置错误。Web 只提供 HTTP，`web.allowed_origins` 必须包含 `http://<listen_host>:<listen_port>`；持锁的数据库连接一旦断开，进程立即停止接收并以 1 退出；新实例接管时会等待旧实例仍在提交的请求接收，再执行恢复。正式日志只输出小维自身日志（级别由 `--log-level` 决定），依赖库的日志在任何级别都不输出。

**诊断。** Web 选“诊断”或飞书直接发文字（不加 `/查询`）：可以粘贴 SQL、问“刚才那条为什么慢”，或在配置了审计源时问“最近一小时最慢的查询”。诊断轮只能查看表结构、表布局（表模型、分区、分桶、排序键）、执行计划与审计慢查询，看不到也不能调用实际查询工具。执行计划只用固定级别 `EXPLAIN LOGICAL` 获取，不执行原查询，不包含实际耗时；EXPLAIN ANALYZE 不开放。回答中的工具结果由程序生成并附固定说明（如“计划是估算”“审计有导入延迟”），分析建议单列为模型推断，优化后的 SQL 只是建议、不会被执行。飞书回复超过单条上限时先完整保留分析建议，工具结果截短并注明。

**审计源（可选）。** `starrocks.audit` 指向目标上已有的 AuditLoader 审计表；小维不安装插件、不改其配置。各字段：

- `database` / `table`：审计库表名（官方默认 `starrocks_audit_db__` / `starrocks_audit_tbl__`），只读账号需要该表的 SELECT 权限。
- `time_zone`：必须等于 FE 服务器的系统时区（审计时间按它写入）。配错不会报错，但时间窗会偏移，结果为空或错位；示例中的 `Asia/Shanghai` 部署时按实际核对。
- `stmt_limit`：必填，填插件的 `max_stmt_length`，并要求 `policy.max_sql_bytes <= stmt_limit - 4`，保证读出的原文没有被插件截断。
- `max_window_minutes` / `max_rows` / `candidate_rows` / `candidate_bytes`：可查的最长时间窗、最多列出的行数，以及一次取出并检查的候选行数与字节上限。

不配置 `audit` 时不提供 `list_slow_queries`，配置开放或授予它会在启动时报错。只列出本目标库中、原文引用的对象、列与函数全部获准的查询，列表可能少于上限，为空也不代表没有慢查询。插件刚安装后首批记录可能缺失；审计表任一行格式异常（如时间为空、指标不是整数）时整个工具失败，而不是返回部分结果。候选读取与原文检查各有一个客户端期限，最坏总耗时约为 2 倍 `client_timeout_seconds`。

**开发验证**（只用合成数据、脚本模型、替身与隔离的测试 PostgreSQL，不连接任何真实模型或外部服务）：

```bash
uv sync --locked --extra dev
docker compose -p xiaowei-sdk-test -f compose.sdk-test.yml up -d --wait   # 或独立的 docker-compose
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \
  uv run --locked --extra dev python -m pytest tests/sdk_core tests/p1b -q
uv run --locked --extra dev ruff check src/xiaowei tests/sdk_core tests/p1b
uv run --locked --extra dev mypy src/xiaowei
docker compose -p xiaowei-sdk-test -f compose.sdk-test.yml down -v
```

`SDK_TEST_POSTGRES_URL` 只能是上面这一个测试地址；未设置时数据库用例明确失败而不是跳过。CI 的 integration job 以同样方式运行完整测试。

正式交付提供两容器的配置、启动、停机、升级、清理及备份恢复说明。小维连接已有 StarRocks 和模型 API，不要求在本机部署模型；更新应用镜像时保留 PostgreSQL 数据卷。当前旧 Compose 文件仍属于历史实现，不代表上述部署方案已经落地。

## 设计与开发

采用 AI 辅助的小步开发：给出本次目标和完成判据，按当前计划实现、验证、审查、更新交接，再继续下一项。开发规则和风险分级统一见 [AGENTS.md](AGENTS.md)，不需要为每次改动重复填写五份文档。

换 AI 工具或新开任务时，明确要求它先按 AGENTS 的顺序读文档，并从 handoff 的下一项工作接续；不要假定它能看到旧对话。任务目标应写清是审查、修订计划还是实施代码。

- [AGENTS.md](AGENTS.md)：开发规则、SDK 优先原则与工程质量要求。
- [ARCHITECTURE.md](ARCHITECTURE.md)：完整产品设计、工具、双入口与执行边界。
- [AGENT_HANDOFF.md](AGENT_HANDOFF.md)：当前代码与已经验证的事实。
- [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)：逐步交付的顺序和验收目标。
- [P1-A 实施计划](docs/superpowers/plans/2026-09-29-p1a-sdk-governed-core.md)：先验证 SDK、模型 API、治理与 MCP 核心，再接真实数据库和双入口；当前执行到哪一步以 handoff 为准。
- [P1-B 实施计划](docs/superpowers/plans/2026-09-30-p1b-starrocks-dual-entry.md)：真实只读查询、请求状态、Web/飞书与正式入口的唯一详细切片计划；各切片的完成与验证状态以 handoff 为准。

设计直接使用 [OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk) 原生能力。旧实现只在有明确价值时提取少量业务素材，兼容旧框架不是新产品目标。

M5 是 Git 历史起点，不是必须保留的架构。仓库中尚未清理的旧代码、测试、ADR 和里程碑用于历史参考，不能与上述新设计共同作为新产品规范。
