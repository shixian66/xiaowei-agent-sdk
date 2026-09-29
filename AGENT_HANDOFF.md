# 小维：当前交接

> 更新：2026-09-29，Asia/Shanghai。这里只记录当前事实、证据与下一项工作；设计见 [ARCHITECTURE.md](ARCHITECTURE.md)，路线见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)，协作规则见 [AGENTS.md](AGENTS.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 1. 当前快照

| 项目 | 已核对事实 |
| --- | --- |
| 仓库 | [shixian66/xiaowei-agent-sdk](https://github.com/shixian66/xiaowei-agent-sdk) |
| 本地目录 / 分支 | `/Users/kloenguyen/Desktop/agent-SDK` / `claude/p1a-task1b-model-api`（从 `main` 的 `9b3517d` 分出；`main` 已包含 Task 1 与 CI harness） |
| M5 历史起点 | `372c381f44ecfa1fa53961f137d0058033cbd805`；不是远端当前 main 的核验结论 |
| 本次实施起点 | `9b3517d42051cec511dec95e58c6ee8dcb8bae70`（PR #2 合并后的 `main`）；开始时工作树干净 |
| 当前阶段 | P1-A Task 1 已合入 `main`；Task 1B（模型 API Profile 与 SDK 接入）已在本分支完成离线实现与 HTTP mock 验证，待独立审查；未推送、无 PR |
| 当前源码与依赖 | 新包 `src/xiaowei/`（`config.py`、`storage.py`、`model_api.py`）与旧 `src/xiaowei_agent/` 并存；锁定 `openai-agents[sqlalchemy]` 0.22.3、`mcp` 2.2.0、`openai` 3.20.0、SQLAlchemy 2.0.52、asyncpg 0.30.0，Python 3.11.16；wheel 同时打包两个包，CLI 仍指向旧包 |
| 新产品入口 | 只有开发验证命令（见第 5 节）；尚无产品启动入口，旧 CLI/Compose 不算新入口 |
| 本次工作范围 | Task 1B：`ModelProfile`、`open_model`、`settings_for`、`profile_fingerprint`、`resolve_secret_ref` 及 `tests/sdk_core/test_model_api.py`；无依赖变更，未改 Compose、CI 或 README |
| 外部操作 | 未调用真实模型、StarRocks 或飞书；供应商端点全部由 `httpx2.MockTransport` 模拟；Task 1B 仅本地提交；没有部署或用户验收 |

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

唯一详细计划：[P1-A：SDK 与治理执行核心](docs/superpowers/plans/2026-09-29-p1a-sdk-governed-core.md)。任务顺序为 **Task 1 → Task 1B → Task 2–5**。Task 1 已通过本地技术验收并经 PR #2 合入 `main`（`9b3517d`）。Task 1B 在本分支完成离线实现，待独立审查。与计划的差异记录在计划的“Task 1 实测记录”和“Task 1B 实测记录”中。

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
- 三个 Profile 的假密钥只发往各自端点；`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_ORG_ID`、`OPENAI_PROJECT_ID`、`OPENAI_CUSTOM_HEADERS` 等环境值不随请求发出（修复前 `OPENAI_CUSTOM_HEADERS` 的 `Authorization` 会覆盖 Profile 密钥）。Profile 指纹稳定、不含密钥，端点/协议/模型/输出模式任一变化都会变。
- 401/403/429/503、超时、非法 JSON 与跨端点 307：只发出一次请求、不重试、不跟随、不切换端点、工具只执行一次。
- 请求超限在发出前拒绝（端点收到 0 个请求）；响应超限在读取阶段停止（64 KiB 响应只读取不超过 5 块）；声明长度超限和压缩响应被拒绝。
- 缺失 usage 时 `raw_usage is None`，不与“为 0”混淆。Profile 校验拒绝非 HTTPS、带用户信息/查询、原始密钥、非正限额、未映射参数、不支持的协议/输出模式/推理强度组合。

**Task 1B 缺口：** 三家均无真实 API 证据；`strict` 工具、JSON mode 与推理字段的供应商兼容性只由 mock 覆盖。SDK 客户端把上游错误体写进异常消息，原始异常尚未映射为受控失败（Task 5 应用出口负责）。流式调用未验证。

**Task 1 未覆盖、留给后续任务：** MCP HTTP 接收字节上限、认证、超时与关闭行为（Task 4）；`tool_input_guardrails` / `tool_filter` 未验证；应用表、表版本检查（Task 2）；Session 写入前过滤（Task 3）；真实模型（Task 5）。显式开启 tracing 时的字段限制未验证。

CI integration job 设置 `SDK_TEST_POSTGRES_URL`，启动 `compose.sdk-test.yml`，运行完整 `python -m pytest -q`，并用 shell `EXIT` trap 清理测试项目；不保留旧 PostgreSQL service。SDK 地址缺失时 fixture 明确失败，避免数据库测试静默跳过。

**下一项：** Task 1B 独立审查；通过后按集成门推送、建 PR、核对 CI 并单独批准合并，再从合并后的 `main` 开始 Task 2（本地受治理工具、数据投影与 Evidence）。

P1-A 是内部核心。P1-B 才接真实查询与双入口并切换正式入口，P2 增加诊断，P3 做实际用户验收。环境缺失不阻塞独立离线任务，但不能跳过对应实战退出条件。

## 4. 实测环境与兼容性缺口

| 对象 | 当前状态 | 实测前所需信息 |
| --- | --- | --- |
| OpenAI 模型 API | Responses 路径已实现，HTTP mock 通过；未实测 | 获准端点/协议/模型 ID、凭据安全引用、数据范围与预算 |
| Gemini 模型 API | Chat Completions 路径已实现，HTTP mock 通过；未实测 | 同上；单独验证工具续轮、结构化结果与协议字段 |
| DeepSeek 模型 API | Chat Completions + json_object 已实现，HTTP mock 通过；未实测 | 同上；单独验证 JSON mode、工具与推理参数组合 |
| PostgreSQL / SDK Session | Task 1 已在隔离测试库验证 SDK 表初始化、就绪检查与 Session 回放；应用表、策略包装未实现 | 后续补应用表版本、写入/回放过滤、保留期与备份恢复验证 |
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
| `uv run --locked --extra dev python -m pytest tests/sdk_core/test_model_api.py -q` | 实现前因缺少接口在收集阶段失败；实现后 44 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest tests/sdk_core -q` | 72 passed |
| `SDK_TEST_POSTGRES_URL=$SDK_PG uv run --locked --extra dev python -m pytest -q` | 1786 passed，152 skipped；无残留测试库 |
| `uv run --locked --extra dev ruff check .`、`ruff format --check src/xiaowei tests/sdk_core`、`mypy src` | 通过 |

Task 1B 反向验证：分别去掉请求头白名单、密钥固定、`identity` 编码、声明长度检查、流式计数、请求上限、`follow_redirects=False`、`store=False`、`preserve_raw_usage`、`json_object` 委托、推理强度白名单、HTTPS 校验，对应用例均失败。重试与期限各有两层（SDK 设置与客户端），只去掉一层时测试仍通过；两层同时去掉时 429/503/超时与期限断言失败。

Task 1 反向验证（临时改动，验证后已恢复）：

- 去掉 tracing 两步中任一步、MCP 前检查或结果过滤中任一项，对应用例都失败。
- 冒名实例：在 55432 端口起一台没有 `cluster_name` 的普通 PostgreSQL，夹具在建库前失败，冒名库上 `xw_sdk_test_*` 为 0；去掉实例核对后，同一用例在冒名库上建库成功。
- 查询参数绕过：55432 不启动服务，只在 55433 起一台带相同 `cluster_name` 的实例，DSN 加 `?port=55433`，夹具在连接前失败，那台实例上没有测试库。把 `parse_admin_url` 退回字段比较后，7 个反例（查询参数覆盖、密码、非法端口）变红。
- 去掉 `_verified_ddl` 中的核对后，`test_every_ddl_connection_verifies_the_instance_first` 变红。
- 把删库移出 `finally` 后，`test_database_is_dropped_when_the_body_raises` 变红，并确实留下了测试库（验证后已手工删除）。
- 往 `src/xiaowei` 临时加入 `import asyncpg` 和 `type: ignore` 注释：修复前两个护栏都通过（假绿），修复后都失败。

测试代码的类型检查不属于必需检查：`mypy --explicit-package-bases src/xiaowei tests/sdk_core`（`MYPYPATH=src`）只报一处 `yaml` 缺少类型存根，与现有 `tests/contract/test_compose_contract.py` 情况相同，未为此增加依赖或放宽配置。本机正在运行的旧 `xiaowei-release` 容器未被触及。

独立审查：Codex 审查 `bf8963d`、`6dbbb6b`、`5aee8f5` 均为暂不通过，复审 `8dba33a` 为本地技术验收通过。本轮只同步 CI 与对应合同测试，未将自查称为独立审查；GitHub CI 是独立执行证据，不代表真实服务或产品运行。尚无真实模型、真实 StarRocks、正式浏览器、飞书运行或用户验收证据，未来生产 Action 仍只有设计约束。
