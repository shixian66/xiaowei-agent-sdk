# 小维：当前交接

> 更新：2026-09-29，Asia/Shanghai。这里只记录当前事实、证据与下一项工作；设计见 [ARCHITECTURE.md](ARCHITECTURE.md)，路线见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)，协作规则见 [AGENTS.md](AGENTS.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 1. 当前快照

| 项目 | 已核对事实 |
| --- | --- |
| 仓库 | [shixian66/xiaowei-agent-sdk](https://github.com/shixian66/xiaowei-agent-sdk) |
| 本地目录 / 分支 | `/Users/kloenguyen/Desktop/agent-SDK` / `claude/p1a-task1-sdk-runtime`（从 `claude/sdk-core-docs` 的 `e6aa728` 分出） |
| M5 历史起点 | `372c381f44ecfa1fa53961f137d0058033cbd805`；不是远端当前 main 的核验结论 |
| 本次实施起点 | `e6aa728f46ca7d12229b2baba9865138fc3816c2`；开始时工作树干净 |
| 当前阶段 | P1-A Task 1 已本地提交，待 Codex 独立审查；Task 1B 未开始 |
| 当前源码与依赖 | 新包 `src/xiaowei/`（`config.py`、`storage.py`）与旧 `src/xiaowei_agent/` 并存；锁定 `openai-agents[sqlalchemy]` 0.22.3、`mcp` 2.2.0、`openai` 3.20.0、SQLAlchemy 2.0.52、asyncpg 0.30.0，Python 3.11.16；wheel 同时打包两个包，CLI 仍指向旧包 |
| 新产品入口 | 只有开发验证命令（见第 5 节）；尚无产品启动入口，旧 CLI/Compose 不算新入口 |
| 本次工作范围 | 只做 Task 1：新包、依赖锁定、测试用 PostgreSQL Compose、SDK 契约测试；未改 CI、正式 Compose、README |
| 外部操作 | 未调用真实模型、StarRocks 或飞书；只向 PyPI 查询包信息与漏洞库；未 push、合并、部署或归档 |

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

唯一详细计划：[P1-A：SDK 与治理执行核心](docs/superpowers/plans/2026-09-29-p1a-sdk-governed-core.md)。任务顺序为 **Task 1 → Task 1B → Task 2–5**。Task 1 已完成并本地提交，待独立审查；审查意见处理完之前不开始 Task 1B。锁版后与计划的差异记录在计划的“Task 1 实测记录”中。

**Task 1 已证明（离线、合成数据、scripted 模型）：**

- 新包在本仓库 `.venv` 中从零安装（`uv sync --locked --extra dev`），pytest、pytest-asyncio、pytest-socket、Ruff、mypy、pip-audit 齐全；导入新包不加载旧 `xiaowei_agent`。
- 真 `Runner` 调用一个合成 function tool 一次，工具结果回传后模型续轮，返回 Pydantic `output_type`。
- `SQLAlchemySession` 在隔离的真实 PostgreSQL 16.15 上写入工具调用配对；重建引擎和 Session 对象后回放，追问不重跑工具。未初始化或连接失败时 `check_storage` 拒绝，模型调用次数为 0，错误不含地址或凭据。
- MCP：使用 SDK `MCPServerStreamableHttp` 连接 loopback 测试服务，通过公开的 `list_tools()` / `call_tool()` 构造薄 FunctionTool。范围不允许时发出的 `tools/call` 请求为 0；允许时禁止字段不进入模型输入。`httpx_client_factory` 已确认被 SDK 使用。
- `configure_runtime()` 关闭 tracing 并移除默认导出处理器；对照组能观察到导出，处理后 `trace_exports == []`。
- 反向验证：缺少 `SDK_TEST_POSTGRES_URL` 时报错而不是跳过；去掉 tracing 两步中任一步、MCP 前检查或结果过滤中任一项，对应测试都会失败。

**Task 1 未覆盖、留给后续任务：** MCP HTTP 接收字节上限、认证、超时与关闭行为（Task 4）；`tool_input_guardrails` / `tool_filter` 未验证；应用表、表版本检查（Task 2）；Session 写入前过滤（Task 3）；模型 HTTP 接入与真实模型（Task 1B、Task 5）；CI 尚未加入新包检查与 PostgreSQL 服务（Task 5）。显式开启 tracing 时的字段限制未验证。

**下一项：** Codex 审查 Task 1 提交；通过后开始 Task 1B（模型 API 配置与 SDK 接入）。

P1-A 是内部核心。P1-B 才接真实查询与双入口并切换正式入口，P2 增加诊断，P3 做实际用户验收。环境缺失不阻塞独立离线任务，但不能跳过对应实战退出条件。

## 4. 实测环境与兼容性缺口

| 对象 | 当前状态 | 实测前所需信息 |
| --- | --- | --- |
| OpenAI 模型 API | 未实现、未实测 | 获准端点/协议/模型 ID、凭据安全引用、数据范围与预算 |
| Gemini 模型 API | 未实现、未实测 | 同上；单独验证工具续轮、结构化结果与协议字段 |
| DeepSeek 模型 API | 未实现、未实测 | 同上；单独验证 JSON mode、工具与推理参数组合 |
| PostgreSQL / SDK Session | Task 1 已在隔离测试库验证 SDK 表初始化、就绪检查与 Session 回放；应用表、策略包装未实现 | 后续补应用表版本、写入/回放过滤、保留期与备份恢复验证 |
| StarRocks | 新产品未联调 | 目标版本、测试连接、只读账号、获准库表/视图、数据投影范围与简短业务口径 |
| 飞书 | 新产品未联调 | 应用与事件配置、获准租户/单聊用户、可信身份来源 |
| 本机 Web | 新产品未实现 | P1-B 落实正式启动、身份/会话边界及浏览器实测 |
| Docker Compose | 新产品双容器尚未交付 | 应用镜像、PG 持久卷、loopback/SSH 访问、启动检查与备份恢复实战 |

凭据只在本机或获准部署环境安全配置，不粘贴到对话、仓库或日志。未提供的环境信息不是用户已授权向任意服务发数据。

## 5. 验证记录与限制

Task 1 验证环境：本仓库 `.venv`（删除后以 `uv sync --locked --extra dev` 重建），Python 3.11.16；测试 PostgreSQL 由 `compose.sdk-test.yml` 启动（`postgres:16.15-bookworm`，只绑定 `127.0.0.1:55432`，数据在 tmpfs）。

```bash
docker-compose -p xiaowei-sdk-test -f compose.sdk-test.yml up -d --wait
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \
  uv run --locked --extra dev python -m pytest tests/sdk_core/test_sdk_contract.py -q
uv run --locked --extra dev ruff check src/xiaowei tests/sdk_core
uv run --locked --extra dev mypy src/xiaowei tests/sdk_core
docker-compose -p xiaowei-sdk-test -f compose.sdk-test.yml down -v
```

| 检查 | 结果 |
| --- | --- |
| `tests/sdk_core/test_sdk_contract.py` | 6 passed；测试后没有残留的 `xw_sdk_test_*` 数据库 |
| 未设置 `SDK_TEST_POSTGRES_URL` | 2 个 PostgreSQL 用例 error（明确失败，不跳过） |
| 反向验证（tracing 两步、MCP 前检查、结果过滤） | 每一项去掉后，对应用例都失败 |
| Ruff（新包与测试）、`ruff check .` | 通过 |
| mypy strict：`src/xiaowei tests/sdk_core`；`mypy src` | 通过（5 个 / 105 个文件） |
| 旧套件 `pytest --ignore=tests/sdk_core` | 1714 passed，152 skipped（旧集成测试没有配置 DSN，属原有行为）；依赖白名单已更新 |
| `pip-audit --strict`（按 `uv export` 结果） | 没有已知漏洞 |

本地需使用独立的 `docker-compose`（本机没有 `docker compose` 插件）。本机正在运行的旧 `xiaowei-release` 容器未被触及。

尚无独立审查、真实模型、真实 StarRocks、正式浏览器、飞书运行或用户验收证据。离线测试通过不代表产品路径可用；未来生产 Action 仍只有设计约束。
