# P1-A SDK 与治理执行核心 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付可独立安装与测试的运行核心：通过明确的模型 API 配置驱动真实 SDK Runner，调用受治理的本地合成工具和 MCP 工具，完成有权限边界的连续对话与证据校验。

**Architecture:** 新应用位于 `src/xiaowei/`，直接使用 SDK Agent/Runner/Session。小维在工具实际 I/O 前执行治理，在工具返回、Session 读写和最终回答出口分别过滤及校验证据；不另造运行引擎。MCP 首条接入使用 SDK 的 Streamable HTTP，空配置不连接任何服务。

模型服务覆盖 OpenAI、Gemini、DeepSeek 的接入目标：前者先验证 Responses，后两者先验证 Chat Completions 兼容路径；组合是否可用由实测决定。静态 Profile 装配 SDK 模型，一次运行一个模型，无自动跨服务 fallback。新增 Task 1B，执行顺序为 Task 1 → Task 1B → Task 2–5。

**Tech Stack:** Python 3.11、OpenAI Agents SDK、Pydantic、SDK SQLAlchemySession、PostgreSQL、SQLAlchemy / asyncpg、Docker Compose（隔离测试数据库）、uv、pytest/pytest-asyncio/pytest-socket、Ruff、mypy；SDK 与传递依赖在 Task 1 实装核对后锁定。FastAPI 和正式应用镜像在 P1-B 接入，两容器部署在 P3 实战验收。

**Spec:** [架构设计](../../../ARCHITECTURE.md)、[交付路线](../../../DEVELOPMENT_PLAN.md)。本文为待审阅的实施计划，复选框不代表已执行。

## Global Constraints

- **OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**
- 单进程、单 Agent；不引入 Worker、计划编译器、多 Agent、业务 MCP Server 或通用审批平台。
- RunContext 只放可信身份、Target Scope、Tool Scope、预算及 Evidence 标识/元数据；不放客户端、服务或凭据引用。
- SDK 公共 Session 为架构接口，SQLAlchemySession + PostgreSQL 为首版正式后端；SDK 表与应用表分别管理，不复制 SDK 会话机制，不假设共库即跨组件原子提交。
- 本地与 MCP 工具都必须在实际 I/O 前复核最新权限与预算；动态工具过滤不是最终授权。
- 本轮查询/诊断用途来自可信入口处理后的明确选择；生成 Tool Scope 时与用户权限及能力取交集，不从模型或历史中继承查询许可。本片用合成工具验证，渠道选择/指令在 P1-B 实现。
- 原始、模型、会话和渠道数据分别约束；默认关闭 tracing 与 trace 外发。
- 关键结果由代码从获准 Evidence 生成，模型只选引用和提供分析；历史超限提示新建，过期即拒绝读取，不用摘要或无来源文字补偿失效证据。
- 模型端点、模型 ID、参数及凭据引用来自可信 Profile；首版模型 HTTP 自动重试显式为 0。更换端点/协议/模型开启新会话，不自动重放历史或切换供应商。最终结构和 Evidence 校验不能为兼容 API 而取消。
- 本片只用合成数据和临时本地 MCP fixture；不开放通用 SQL、不接生产系统、不构建 Web/飞书入口。StarRocks SQLGuard/Adapter、两端真实收发、请求去重/重发及正式主入口在 P1-B 完成，P2/P3 仍保留全部验收要求。
- 旧 `src/xiaowei_agent/`、旧 CLI 和相关依赖本片保留为过渡历史；新包不得导入旧 Runtime。P1-B 明确删除范围并切换唯一正式入口。
- 测试使用本仓库独立 `.venv`，保留 `dev` optional extra，安装与执行命令显式带 `--extra dev`；禁止以原项目环境证明新产品通过。离线测试禁止外网，仅 PostgreSQL 与 MCP fixture 测试按需允许 loopback。存储用隔离的真实 PostgreSQL，不以 SQLite/fake 替代；缺环境明确失败，不静默 skip 成通过。

## Review Focus

1. 工具展示之后发生撤权：实际执行前拒绝，recording adapter / MCP fixture 收到零次工具执行请求（Task 2、4）。
2. SDK 自动写入工具结果和最终回答：禁止内容不能先落盘后擦除，回放还需复核当前权限，工具调用配对不能损坏（Task 3、5）。
3. MCP 名字冲突、schema 漂移与伪造 readonly/evidence 声明：拒绝未登记契约，远端标识不能成为可信本地证据（Task 4）。
4. 两个用户并发与工具并行：工具表、连接认证、Evidence、预算和 Session 不串用；同会话明确拒绝重入（Task 2、5）。
5. 模型/MCP API 超时、鉴权/限流、畸形/过大内容与 SDK 默认 trace：受限失败，不泄露原始错误/数据、不绕过过滤、不自动重试未知执行结果（Task 1、1B、4、5）。

新增约束分别进入现有任务：诊断范围与程序生成事实（Task 2、5），真实 PostgreSQL、版本检查与跨组件失败（Task 1–3、5），历史上限与过期（Task 3），阶段日志（Task 5）。业务口径、渠道指令、清理命令、健康端点属于 P1-B；正式部署与备份恢复属于 P3，不为这七项另建平台或并行路线。

## 文件与接口约定

不生成空目录或无消费者的接口。按任务增加以下少量模块；文件过大时才按职责拆分。

| 文件 | 职责 |
| --- | --- |
| `src/xiaowei/config.py` | 有限配置、静态 MCP 登记、默认 tracing 关闭 |
| `src/xiaowei/model_api.py` | 可信模型配置、SDK 模型与 HTTP 客户端装配、必要的响应格式适配 |
| `src/xiaowei/storage.py` | PostgreSQL 引擎生命周期、显式初始化/版本检查；后续加入应用表，不封装通用仓储框架 |
| `src/xiaowei/models.py` | 可信 context、工具结果、证据、结构化回答与渠道输出类型 |
| `src/xiaowei/governance.py` | 调用前权限/参数/预算检查，共享结果入口 |
| `src/xiaowei/evidence.py` | 证据记录、四种数据投影与最终回答校验 |
| `src/xiaowei/session.py` | SDK Session 薄策略包装与受限回放 |
| `src/xiaowei/mcp.py` | SDK MCP 公共接口的治理装配和生命周期 |
| `src/xiaowei/app.py` | 单轮应用函数、每轮 Agent 装配与有限并发 |
| `tests/sdk_core/` | 真 Runner + scripted Model、recording I/O、隔离 PostgreSQL、loopback MCP fixture |
| `compose.sdk-test.yml` | 仅供本地验证的临时 PostgreSQL，独立项目名/数据卷及 loopback 端口，不使用旧产品 Compose |

下列应用类型在 Task 2 定义，后续任务直接复用；SDK 类型直接导入。类型字段为此片的最小契约：

- `Channel = Literal["web", "feishu"]`；`Identity(subject_id: str, session_id: str, turn_id: str, channel: Channel)` 由可信入口构造。
- `Budget(max_turns: int, max_tool_calls: int, timeout_seconds: float)` 为正数上限；工具次数计数器按可信 subject/session/turn 键在治理对象内部隔离，并在轮次结束时清理，不是模型可改字段。
- `RunContext(identity: Identity, target_scope: frozenset[str], tool_scope: frozenset[str], budget: Budget, evidence_ids: tuple[str, ...])`；无任意对象扩展字段。用途选择在应用入口转换为本轮 `tool_scope`，不向 context 塞入客户端或额外的授权引擎。
- `ToolContract(tool_id: str, target_id: str, input_schema: dict[str, object], policy_id: str)` 为启动时获准的契约；`tool_id` 使用 `local/name` 或 `server_id/name`。参数与结果策略由 `policy_id` 对应的明确 Python 函数提供，不设计策略 DSL。
- `ToolRequest(tool_id: str, target_id: str, call_id: str, arguments: dict[str, object])`，`call_id` 取自 SDK 工具上下文；`ToolObservation(payload: dict[str, object], captured_at: datetime, truncated: bool)` 只容纳已限制读取大小的临时结果，不自动序列化给模型。
- `EvidenceRecord` 包含代码生成的 `evidence_id`、可信身份/会话、target/tool/call 标识、采集与过期时间、截断状态及各用途获准内容；持久化不保留原始结果。
- `ToolResult(evidence_id: str, model_content: str, truncated: bool)` 仅为模型可见内容。`AnswerInference(text: str, evidence_ids: tuple[str, ...])` 和 `AgentAnswer(evidence_ids: tuple[str, ...], inferences: list[AnswerInference], clarification: str | None)` 作为 `output_type`；不让模型提交“已核实数值/实际 SQL”等事实字段。每条分析必须有有效引用，且属于回答选择的 Evidence；代码从这些记录的获准投影生成事实区域，分析单独标注。澄清不允许展示查询结果或与结果/分析混用；自由文字的语义仍靠评测，不能把 schema 校验当作事实验证。
- `Delivery(content: str, evidence_ids: tuple[str, ...], channel: Channel)` 是通过验证的渠道输出；同一回答也要按接收渠道重新生成。

## Task 1：锁定 SDK 并验证公开扩展点

**Files:** Modify `pyproject.toml`、`uv.lock`、`tests/security/test_dependency_baseline.py`；Create `src/xiaowei/__init__.py`、`src/xiaowei/config.py`、`src/xiaowei/storage.py`、`compose.sdk-test.yml`、`tests/sdk_core/conftest.py`、`tests/sdk_core/postgres_harness.py`、`tests/sdk_core/test_sdk_contract.py`、`tests/sdk_core/test_postgres_harness.py`。

**Interfaces:** 消费官方 `Agent`、`Runner`、`Model`、`Session`、`SQLAlchemySession` 与 MCP 公共类型；产出 `configure_runtime() -> None`，负责显式关闭默认 tracing/export，以及可安装的新包。`storage.py` 提供 `open_engine(database_url: SecretStr) -> AsyncIterator[AsyncEngine]` 异步上下文管理器、显式部署入口使用的 `async initialize_storage(engine: AsyncEngine) -> None` 与运行时 `async check_storage(engine: AsyncEngine) -> None`；本任务只处理 SDK 表，Task 2 加入应用表及版本检查。URL 仅来自私有配置，不打印或进入 context；具体 SDK 初始化/Session 方法签名以锁定版本为准，在本任务测试中固定。

- [x] 编写 `test_real_runner_calls_tool_and_returns_typed_answer`、`test_postgres_session_public_roundtrip`、`test_storage_unavailable_or_uninitialized_is_rejected`、`test_public_mcp_interception_contract`、`test_default_tracing_has_no_export`。使用官方 Model 接口实现测试用 scripted Model；核心断言分别为 `tool_calls == 1`、类型化回答正确、SQLAlchemySession 在真实 PostgreSQL 上重建对象后仍可回放成对调用、存储未就绪时不调用模型、公开 MCP 边界可拦截、`trace_exports == []`。
- [ ] 运行 `uv run --extra dev python -m pytest tests/sdk_core/test_sdk_contract.py -q`；记录新依赖/模块缺失或能力不满足的失败，不把安装故障当行为测试通过。（实施时先写实现后写测试，未留下“先失败”的记录；改用反向验证证明测试有效，见下方实测记录。）
- [x] 安装核对当前 Python 兼容版本，将 `openai-agents[sqlalchemy]`、SQLAlchemy、asyncpg 和开发依赖锁入 `uv.lock`，核对 extra 的实际依赖；将 `src/xiaowei` 加入打包但不把旧 CLI 宣称为新产品。实现运行配置，验证 SDK Session、MCP 调用/结果拦截、HTTP transport 的超时与接收限额扩展点。只使用公开 API；若不能实现必要边界，记录具体 API 限制并调整本计划，不绕过治理启用该能力。
- [x] 建立测试专用 PostgreSQL Compose 与 fixture，锁定镜像、限制 loopback 访问，只清理本测试创建的项目/数据库，禁止指向已有运行数据。实现有界连接池、超时及退出释放，确认 SDK 的公开建表路径；正式运行不在普通请求中自动建表。记录初始化、启动、健康等待和清理的实际命令，SDK 与应用表不共享写事务的假设由后续失败测试覆盖。
- [x] 运行 `uv sync --locked --extra dev` 和 `uv run --locked --extra dev python -m pytest tests/sdk_core/test_sdk_contract.py -q`；预期全部 PASS。测试不得访问真实模型或读取既有凭据；新包 import 不加载旧 `xiaowei_agent`。
- [x] 仅暂存本任务文件，提交 `build: establish verified Agents SDK runtime`；记录锁定版本及 Python 版本。

**Task 1 实测记录（锁版后与计划的差异）：**

- 锁定 `openai-agents[sqlalchemy]` 0.22.3（约束 `>=0.22.3,<0.23`），传递依赖包括 `openai` 3.20.0、`mcp` 2.2.0、`httpx2` 2.13.1、SQLAlchemy 2.0.52、asyncpg 0.30.0；Python 3.11.16。旧 `tests/security/test_dependency_baseline.py` 的运行依赖白名单同步加入 `openai-agents`，保留集合相等的审查机制。
- 测试模型直接使用 SDK 公开的 `agents.testing.ScriptedModel`（它就是一个 `Model` 实现），不再自写 scripted Model。
- SDK 表初始化的公开路径是 `SQLAlchemySession(..., create_tables=True)`，在首次读写时建表；`initialize_storage` 用只读探针 `get_items(limit=0)` 触发，不写会话数据。默认表名 `agent_sessions` / `agent_messages`，时间列为无时区 `TIMESTAMP`。运行时会话保持 `create_tables=False`，缺表由 `check_storage` 拒绝。
- MCP 2.x 的 `httpx_client_factory` 必须返回 `httpx2.AsyncClient`（不是 `httpx`）；已验证 SDK 实际使用应用提供的工厂，超时参数得到传递，请求可在客户端观察。**接收字节上限尚未实测**，留给 Task 4。
- MCP 调用前检查与返回后过滤：`tool_output_guardrails` 只能放行、以固定消息拒绝或抛错，不能改写返回内容，不满足“过滤后交给模型”。已验证的路径是不把 `MCPServer` 挂到 Agent，而是用公开的 `list_tools()` / `call_tool()` 构造薄 `FunctionTool`：拒绝时零 `tools/call` 请求，放行时禁止字段不进入模型输入。`tool_input_guardrails` 与 `tool_filter` 存在，但本任务未验证。Task 4 按薄 FunctionTool 路径实现。
- Tracing 需要两步：`set_tracing_disabled(True)` 关闭生成，`set_trace_processors([])` 移除默认导出处理器；测试以默认处理器的对照组证明能观察到导出，再分别证明两步各自生效。
- 本机没有 `docker compose` 插件，使用独立的 `docker-compose` 5.5.1；命令见 `compose.sdk-test.yml` 文件头。
- 测试 PostgreSQL 的身份由代码核对（首轮审查后补充）：镜像按 `tag@sha256` 固定；`tests/sdk_core/postgres_harness.py` 要求环境变量与唯一管理地址逐字相同（SQLAlchemy 会用查询参数覆盖主字段，因此不能只比较解析后的字段），连接目标只取自该常量；每条 `CREATE/DROP DATABASE` 都在执行它的同一连接上先核对服务器 `cluster_name`。旧安全护栏中与架构无关的两条（禁止直接导入 asyncpg、禁止 `type: ignore`）改为扫描整个 `src/`。
- CI 集成沿用同一 harness：integration job 显式设置 `SDK_TEST_POSTGRES_URL`，通过 `compose.sdk-test.yml` 启停隔离数据库并运行完整 pytest；使用 shell `EXIT` trap 清理，不配置旧 PostgreSQL service。合同测试将工作流变量、固定 URL、Compose 端口和完整测试命令相互绑定；安全策略继续校验工作流摘要、命令闭集、无凭据环境变量及 Compose 镜像 digest。

## Task 1B：模型 API 配置与 SDK 接入

**Files:** Modify `src/xiaowei/config.py`、`AGENT_HANDOFF.md`、本计划；Create `src/xiaowei/model_api.py`、`tests/sdk_core/test_model_api.py`。本任务不引入通用网关、自动路由或第三方 Agent 运行引擎；直接使用已锁定 SDK 与其 OpenAI HTTP 客户端依赖。

**Interfaces:** `ModelProfile` 为 Pydantic 配置类型，字段固定为 `profile_id: str`、`provider: Literal["openai", "gemini", "deepseek", "openai_compatible"]`、`base_url: str`、`api_mode: Literal["responses", "chat_completions"]`、`model: str`、`api_key_ref: str`、`output_mode: Literal["json_schema", "json_object"]`、`request_timeout_seconds: float`、`max_output_tokens: int`、`max_request_bytes: int`、`max_response_bytes: int`、`data_policy_id: str`、`reasoning_effort: str | None`。期限/限额为正值，地址为可信 HTTPS 端点，模型 ID 必填，不使用 SDK 隐式模型默认值；推理参数仅接受所验证 Profile 的允许值。`open_model(profile: ModelProfile, *, api_key: SecretStr) -> AsyncIterator[Model]` 是异步上下文管理器，装配并关闭独立客户端，返回 SDK `Model`；`settings_for(profile: ModelProfile) -> ModelSettings` 返回 SDK 设置。`profile_fingerprint(profile: ModelProfile) -> str` 返回不含凭据的稳定配置版本，供会话绑定；`SecretStr` 来自 Pydantic，SDK 类型直接使用。

- [x] 编写 `test_sdk_responses_and_chat_completions_tool_roundtrip`、`test_json_object_still_validates_output_type`、`test_profiles_keep_keys_and_endpoints_separate`、`test_api_failure_has_no_retry_or_fallback`、`test_request_and_output_limits`。使用 HTTP mock transport 接真实 SDK Model 与 Runner，不以 scripted Model 冒充 API 适配测试；本任务最终回答先用测试文件内的简单 Pydantic schema，Task 5 再测真实 `AgentAnswer`。断言工具调用后确有模型续轮；JSON mode 缺字段/错类型/空内容不能返回成功；三个 Profile 的假密钥仅发往各自端点；401/403、429、503、超时和非法 JSON 无跨端点请求、无自动重试、无重复工具执行。
- [x] 运行 `uv run --locked --extra dev python -m pytest tests/sdk_core/test_model_api.py -q`；预期因模型配置与装配缺失失败，mock transport 不建立外网连接。
- [x] 实现上述配置与装配，使用锁定版 SDK 的公开 Responses / Chat Completions 模型接入。客户端设置明确请求超时、`max_retries=0` 与禁止跨端点重定向，凭据由应用解析安全引用后传入；不设置可变全局客户端。通过公开 HTTP hook 在发出前检查序列化请求字节上限；模型输出 token 上限映射到所选协议，在 HTTP 读取阶段落实 `max_response_bytes`，不能读完整超大响应后才切片；usage 缺失不记为 0。供应商专有参数通过少量经验证的映射生成，不开放任意配置透传。
- [x] 对 `json_object` 路径，只用 SDK 公开模型设置或薄 `Model` 委托适配请求编码及 schema 提示，保留外层 `output_type` 的类型校验；工具选择和循环仍由 Runner 完成。验证工具 strict 参数、最终输出模式与推理字段的组合；必要字段不能与 Session 策略兼容的模式保持禁用。若公开接口无法满足某组合，记录不兼容原因，不删除校验或改写 SDK 私有代码，不将该 Profile 标为已支持。
- [x] 重跑本任务命令；预期所有已实现路径及拒绝路径 PASS。核对请求无未支持参数，假密钥不进入日志/错误/trace，Profile 指纹不含密钥值，客户端正常关闭。记录 SDK 实际类名、方法签名与三家验证矩阵；HTTP mock 通过仍不等于供应商真实验证通过。
- [x] 仅暂存本任务文件，提交 `feat: configure SDK model API profiles`。

**Task 1B 实测记录（与计划的差异与发现）：**

- SDK 类：`OpenAIResponsesModel(model, openai_client)`、`OpenAIChatCompletionsModel(model, openai_client)`；客户端为 `openai.AsyncOpenAI`，底层是 `httpx2.AsyncClient`（不是 `httpx`）。`open_model` 比计划多一个仅限关键字的 `transport` 参数，只替换最底层发送，供 mock transport 使用；限额与请求头约束始终生效。
- **环境凭据泄漏（已修复）：** `AsyncOpenAI` 在构造时读取 `OPENAI_ORG_ID`、`OPENAI_PROJECT_ID`、`OPENAI_CUSTOM_HEADERS`，并随每个请求发送；`OPENAI_CUSTOM_HEADERS` 中的 `Authorization` 会覆盖显式传入的密钥。实测默认装配会把这些值发往 Gemini/DeepSeek 端点。首轮修复只按名字过滤请求头，审查证实同名环境头（`Host`、`Accept`、`Content-Type`、`Content-Length`）的值仍会外发。现 `_GuardedTransport` 不沿用 SDK 请求中的任何头值：`Host`、`Content-Length` 由 httpx 从可信 URL 与实际 body 生成，其余为固定协议值，`Authorization` 为本 Profile 的凭据。默认 `AsyncHTTPTransport` 显式 `trust_env=False`（外层客户端的 `trust_env` 不作用于它，否则 `SSL_CERT_FILE` 等会改变 TLS 配置）。
- 请求/响应字节限额都在 transport 上落实（计划写的是 HTTP hook）：发出前检查序列化后的请求体；响应按实际读取的字节逐块计数，超限即停止，不依赖可伪造的 `Content-Length`；请求固定 `Accept-Encoding: identity` 并拒绝压缩响应（含错误响应），避免压缩体解压后绕过上限。
- **Chat 终态（审查后修复）：** 锁定 SDK 只取第一个 choice，且只在没有任何文本、工具或 refusal 时拒绝 `finish_reason="length"`；转换为 `ModelResponse` 后终态丢失。因此截断但合法的 JSON 会成为成功答案，截断的工具调用会被执行。现 transport 在有界读取后、SDK 解析前要求每个 choice 的 `finish_reason` 属于 `stop`/`tool_calls`，截断、过滤、缺失或空 choices 一律拒绝。Responses 的 `status="incomplete"` 由 SDK 自身拒绝，不重复校验。成功响应必须是合法 JSON，流式（SSE）因此不开放。
- **并行工具调用（审查后修复）：** `parallel_tool_calls=None` 时 SDK 不发送该字段，供应商默认通常开启；现显式 `False`。这只是请求参数，供应商仍可能返回多个调用，Task 2 须保留原子预算检查。
- 重试与期限各有两层，任一层即可生效：`ModelSettings.retry=ModelRetrySettings(max_retries=0)` 会让 SDK 把客户端重试改为 0，客户端本身也是 `max_retries=0`；`AsyncOpenAI` 未传期限时沿用 `httpx2.AsyncClient` 的期限。反向验证只去掉一层时测试仍通过，两层同时去掉才失败。
- Responses 路径显式 `store=False`；推理强度映射为 Responses 的 `reasoning.effort` 与 Chat Completions 的 `reasoning_effort`。允许值按供应商公开文档设定（DeepSeek 与 `openai_compatible` 暂不开放），未经真实 API 验证。
- `api_key_ref` 只接受 `env:NAME`，`config.resolve_secret_ref` 解析；原始密钥误填入配置时校验失败。`data_policy_id` 已进入 Profile 与指纹，由 Task 5 装配到数据投影。
- `json_object` 仅用于 Chat Completions：薄 `Model` 委托不把 schema 交给内层模型，改用 `extra_args={"response_format": {"type": "json_object"}}`，并在 system 说明末尾附 JSON Schema；Runner 仍按 `output_type` 校验。非法 JSON、缺字段、错类型抛 `ModelBehaviorError`；空内容或 `null` 不被当作最终输出，Runner 继续请求直到 `MaxTurnsExceeded`。
- usage：SDK 把缺失的 usage 规范化为 0；`preserve_raw_usage=True` 后 `ModelResponse.raw_usage` 在未上报时为 `None`，Task 5 以此区分“未上报”和“为 0”。
- 失败路径：401/403/429/503 抛 `APIStatusError`，超时抛 `APITimeoutError`，非法 JSON 成功响应抛 `ModelResponseRejectedError`；跨端点 307 不跟随（`APIStatusError`）。均只发出一次请求、工具只执行一次、无其他端点请求。**SDK 客户端把上游错误体放进异常消息**，原始异常不能直接展示或记录；Task 5 的应用出口必须映射为固定的受控失败。
- 工具定义默认发送 `strict: true`；DeepSeek/Gemini 是否接受该字段及 JSON mode 与工具、推理字段的组合，只有 HTTP mock 证据。
- 验证矩阵：OpenAI Responses、Gemini Chat Completions（json_schema）、DeepSeek Chat Completions（json_object）均**仅 HTTP mock 通过，真实 API 未验证**。

## Task 2：本地受治理工具、数据投影与 Evidence

**Files:** Modify `src/xiaowei/storage.py`；Create `src/xiaowei/models.py`、`src/xiaowei/governance.py`、`src/xiaowei/evidence.py`、`src/xiaowei/migrations/001_initial.sql`（随 wheel 打包）、`tests/sdk_core/synthetic_tools.py`、`tests/sdk_core/test_governance.py`、`tests/sdk_core/test_evidence.py`。

**Interfaces:** 定义上文类型；`GovernedTools.allowed_contracts(ctx: RunContext) -> list[ToolContract]`、`async GovernedTools.invoke(ctx: RunContext, request: ToolRequest, execute: Callable[[], Awaitable[ToolObservation]]) -> ToolResult`。依赖通过构造器装配，`execute` 只能由应用绑定，模型不能提供。`async EvidenceStore.record(ctx: RunContext, request: ToolRequest, observation: ToolObservation) -> ToolResult`；`async EvidenceStore.project(evidence_id: str, ctx: RunContext, audience: Literal["model", "session", "web", "feishu"]) -> str`；`async EvidenceStore.validate_answer(answer: AgentAnswer, ctx: RunContext) -> Delivery`。存储使用 SQLAlchemy / asyncpg 操作独立的 PostgreSQL 应用表，当前授权查询由应用提供的回调执行。首次应用表含 Evidence、Session 元数据与 schema 版本；版本化 SQL 只作用于应用表，由显式初始化/升级路径执行并检查版本，不建设通用迁移框架。

- [x] 编写 `test_revoked_tool_never_reaches_io`、`test_parallel_calls_share_one_budget`、`test_context_rejects_dependencies`、`test_four_data_boundaries`、`test_forged_foreign_expired_evidence_is_denied`、`test_query_claim_without_evidence_is_denied`。关键断言：拒绝时 `recorded_calls == []`；上限 1 时两个并行请求总执行数为 1；原始合成字段 `private_note` 不出现在模型/Session/渠道；为四种投影设置不同字段/字节上限并分别满足；撤权后不能读取已保存内容。
- [x] 编写 `test_diagnose_scope_hides_and_denies_query`、`test_facts_are_rendered_from_evidence`、`test_app_schema_version_is_checked`。用合成查询工具验证：即使用户拥有查询权限，诊断轮也不展示且强行调用零 I/O；真实值 100 的事实区域只能由 Evidence 生成，模型额外提交事实数值字段被拒绝，分析里的文字不被放入事实区域；SDK 与应用表互不改写，版本不匹配拒绝使用。测试不声称能够识别任意自然语言中的错误推断。
- [x] 运行 `uv run --locked --extra dev python -m pytest tests/sdk_core/test_governance.py tests/sdk_core/test_evidence.py -q`；预期因缺少新类型/行为失败。
- [x] 实现上述类型与最少服务方法。允许集合来自可信配置并受本轮 Tool Scope 限制，实际调用时复核目标、契约、参数与最新权限，原子预占工具预算。用仅测试存在的合成 function tools/recording adapter 验证；不包装成伪 StarRocks。代码生成证据 ID，按用途投影后才写入 Evidence；未知投影规则拒绝。事实区域由代码从引用的获准投影生成，分析单独标注并验证引用；回答类型拒绝额外事实字段。错误转成有限、安全的工具失败，不携带 raw payload。
- [x] 重跑本任务命令；预期全部 PASS。确认不同用户/会话/渠道不能读取彼此证据，截断和空结果都有明确语义，真实 PostgreSQL 保存内容不含合成禁止字段，重建存储对象后归属/权限/过期检查仍成立。
- [x] 仅暂存本任务文件，提交 `feat: govern tool execution and evidence boundaries`。

**Task 2 实测记录（与计划的差异与发现）：**

- 迁移 SQL 放在包内 `src/xiaowei/migrations/001_initial.sql`（计划写 `migrations/sdk_app/`），随 wheel 发布，由 `importlib.resources` 读取。`initialize_storage` 在一个事务内加咨询锁、只在未安装时执行；SQLAlchemy 的 asyncpg 适配层不支持一次多语句，因此按行尾分号逐条执行。`check_storage` 同时要求 SDK 表、应用表与版本 1，版本不符抛 `StorageVersionMismatchError`，初始化也不自动升级。
- 首版应用表只有 `xiaowei_schema_version` 与 `xiaowei_evidence`。Session 元数据（Profile 绑定、失效、封存）没有 Task 2 消费者，移到 Task 3 与 `PolicySession` 一起定义（v1 尚未发布，Task 3 直接修改 001 或新增 002 由该任务决定）。
- `ToolRequest` 增加 `call_id`；`ToolPolicy(policy_id, arguments: type[BaseModel], projections)` 用 Pydantic 参数模型校验参数（须 `extra="forbid"`），`ToolCatalog` 启动时要求契约 `input_schema` 与参数模型的 JSON Schema 一致、每个策略恰好定义四种用途投影，否则拒绝装配。
- 投影按声明字段顺序整体纳入顶层字段，超出字节上限的字段整体省略并标 `truncated`，不切开字段值；`empty` 表示结果中没有任何获准字段；来源截断在各用途都保留。Evidence 行只保存四份投影与可信元数据，不含原始结果。
- 读取复核：归属（subject/session/channel）写在 SQL 条件中，再查过期（应用时钟）、当前 Target Scope 与应用授权回调；Web/飞书投影只交给相同渠道。伪造、他人、撤权、过期返回同一信息。Tool Scope 不参与读取：诊断轮可引用本会话已有证据，但不能执行查询。
- 治理顺序：契约/目标 → 本轮范围 → 参数 → 授权回调 → 预算预占 → 执行 → Evidence。结构性拒绝不调用授权回调、不耗预算；预占与检查之间没有 `await`，SDK 并行执行的两个调用只有一个越过上限。执行失败不退还预算，异常以固定信息抛出且不带原因链。计数在 `GovernedTools` 进程内，Task 5 在轮次结束调用 `end_turn`。
- `AgentAnswer` 禁止额外字段：模型多提交 `verified_total` 时 SDK 抛 `ModelBehaviorError`。`validate_answer` 要求非澄清回答至少一个引用、无重复、每条分析有引用且属于回答所选证据，澄清不能与引用或分析混用；事实区域只由接收渠道的投影生成，分析区单独标注为模型推断。自由文字中的错误推断无法由代码识别。
- 可信类型原计划启用 Pydantic `strict`；反向验证表明阻止依赖对象的是 `extra="forbid"` 与字段类型，`strict` 不承重，已去掉。
- **审查后修复（`cc8cbd3` 暂不通过的 3 项 P1）：**
  - 出口不是唯一关口：执行前授权一次后，`record` 直接返回模型投影；读取边界也不核对当前策略。现 `record` 写入后经同一个 `_readable` 取回模型内容，执行期间或写入等待期间撤权时结果不交给模型（I/O 已发生，抛 `EvidenceUnavailableError`，不退预算、不重试）。Evidence 行保存 `policy_fingerprint`（契约含参数 schema/目标/策略标识、四种投影字段与上限、投影规则版本），读取时与当前目录比较，策略收窄、换版或工具移除后旧证据不可读。
  - 模型可达内容不受渠道约束：模型可把它看到的字段写进分析或澄清文字，代码无法从自由文字中识别。现模型与 Session 投影（Session 会回放给模型）都与记录所在渠道取字段交集与较小上限；渠道允许但模型不可见的数据仍只由事实区生成。
  - v1 迁移尚未发布，直接在 `001_initial.sql` 增加 `policy_fingerprint` 列。

## Task 3：原生 Session 的存储前与回放前策略

**Files:** Create `src/xiaowei/session.py`、`tests/sdk_core/test_session_policy.py`。

**Interfaces:** 消费 `RunContext`、异步 `EvidenceStore.project`、Task 1B 的 Profile 指纹与 SDK `Session`；产出 `PolicySession(inner: Session, context: RunContext, evidence: EvidenceStore, profile_fingerprint: str, limits: SessionLimits)`（实现另有关键字参数 `input_policy`、`engine`、`clock`，见实测记录），实现锁定版 SDK 所需的公开 Session 接口并委托底层。`SessionLimits(max_history_turns: int, max_history_bytes: int, retention_seconds: int)` 均为正值，由可信配置提供，不把 SDK `max_turns` 当作历史上限；应用元数据记录会话失效时间。会话的 Profile 绑定保存在应用元数据中；不匹配时拒绝历史读取，要求新建会话。提供 `async discard_pending() -> None`、`async commit_validated() -> None`；后者只由验证成功的应用路径调用。

- [x] 编写 `test_runner_session_filters_before_write`、`test_replay_rechecks_permissions`、`test_valid_tool_pair_survives_followup`、`test_invalid_final_never_persists`、`test_partial_store_failure_invalidates_session`。用 SQLAlchemySession、隔离的真实 PostgreSQL 和真 Runner 断言：`forbidden_text not in stored_items`，撤权数据不在第二轮 Model 输入，SDK 能消费过滤后成对工具项，未经验证最终输出不会落盘。
- [x] 编写 `test_history_limit_stops_before_model_call`、`test_expired_history_or_evidence_cannot_replay`。轮数/字节达到配置上限时新轮零模型调用并提示新建；用可控时间验证未执行物理清理的过期记录也不能回放。若本轮暂存项仍使总历史超限，则拒绝提交、封存会话并返回受控的新建提示，不裁掉半组工具项，不自动重跑已执行工具。
- [x] 编写 `test_changed_model_profile_cannot_read_old_session`，断言端点/协议/模型或数据接收策略变化后，旧历史在首个模型 HTTP 请求前被拒绝；新建会话可独立运行，不复制旧内容。
- [x] 运行 `uv run --locked --extra dev python -m pytest tests/sdk_core/test_session_policy.py -q`；预期新包装/行为缺失失败。
- [x] 实现 `PolicySession`：在 SDK 写入请求时先按 Session 策略处理并暂存本轮项，最终校验后才委托写入；回放前重新检验证据和权限。敏感用户输入也受字段/字节策略约束；不能只过滤工具结果。保持 SDK item 类型与 call/result 配对，若无安全回放形式则拒绝继续该历史。失败丢弃待写项，底层部分写失败时隔离该会话，不能自动重跑或将其视作完整历史。
- [x] 重跑本任务命令；预期全部 PASS。验证 clear/pop 等 SDK 公共契约与待写项一致，应用元数据与 SDK 清理失败时仍保持不可回放状态；持久化和 replay 不是同一份未经处理的模型记录。物理清理命令及渠道新建会话在 P1-B 交付。
- [x] 仅暂存本任务文件，提交 `feat: enforce Session write and replay policy`。

**Task 3 实测记录（与计划的差异与发现）：**

- 锁定版 SDK 每轮开头调用一次 `get_items()`（异常原样抛出，模型零调用），并在 `Runner.run` 返回前依次 `add_items` 用户输入、工具调用/结果配对和最终消息，早于应用校验。因此 `add_items` 只把转换后的副本暂存在内存，底层写入只发生在 `commit_validated()`；运行失败、取消或校验失败调用 `discard_pending()`。一个 `PolicySession` 对象只服务一轮。
- 构造器在计划签名外增加关键字参数 `engine`、`clock`（应用元数据表与可控时间），并要求底层 Session 标识与可信身份的会话一致。`commit_validated()` 不信任调用方：取本轮最后一项解析为 `AgentAnswer`，经同一个 `EvidenceStore.validate_answer` 校验后才写入（架构要求最终持久化复用 Evidence 验证器）。
- 保存形式按类型白名单重建：用户/助手消息只保留纯文本（图片、文件、拒答等无文字片段整轮拒绝，system/developer 角色拒绝），工具调用只保留 `call_id/name/arguments`，推理项不保存，其他类型（托管工具等）整轮拒绝。工具结果只保存证据引用 `{"xiaowei_evidence_ref": ...}`；不含证据的输出（治理拒绝、执行失败）保存为固定文字，不保留原始错误文本。暂存时即核对证据属于本会话且由同一 `call_id` 生成（`EvidenceStore.project` 增加 `call_id` 参数），否则本轮不能提交。
- 回放把引用经 Evidence 读取边界换成当前 Session 投影（归属、渠道、过期、目标范围、策略指纹、当前授权、调用绑定），同时核对调用/结果一一配对。任一项不可回放时整段拒绝、提示新建，不删掉半组工具项，也不以无来源文字替代：模型后续文字可能复述过该证据，只删工具结果不能阻断。
- 应用表 `xiaowei_session`（v1 未部署过，直接加入 `001_initial.sql`；此前用旧 v1 初始化的开发库会被 `check_storage` 判为未初始化，需要重建）记录归属（subject、渠道）、Profile 指纹、创建/失效时间（创建时间 + 保留期，不滑动）、已提交轮数与状态。只有 `active` 可回放；提交先置 `writing`，底层写入与元数据更新都成功才回到 `active`，任一步失败会话停在 `writing` 不再回放；本轮使历史超出字节上限时置 `sealed` 并拒绝提交，不重跑工具。Profile 指纹包含端点、协议、模型、输出模式与 `data_policy_id`，任一变化都在首个模型请求前拒绝旧历史。
- SDK 公共契约：`get_items(limit)` 按条数截断会拆开配对，给出 `limit` 时拒绝（只影响 SDK 的 RunState 恢复路径，本片不使用）；`pop_item()` 只撤回本轮暂存项，已提交历史不能逐条删除；`clear_session()` 先把会话置为 `closed` 再清除底层历史，清除失败时会话仍不可回放，清除后该会话标识不再使用。
- **审查后修复（`5004951` 暂不通过的 3 项 P1）：**
  - 模型可达内容未受 Session 约束（双向）：模型文字、工具参数原样保存，Session 投影又直接回放给模型，Model-only 字段可落库、Session-only 字段可进入下一轮模型；原用例把回放 Session 投影写成了预期。代码不能从自由文字剔除字段，因此在 Evidence 投影的唯一计算点 `_limits` 让模型与 Session 投影都取模型、Session、记录渠道三者的字段交集与最小上限（投影规则升为 `/2`，旧规则证据不可读），与 Task 2 的渠道约束同一机制；`ARCHITECTURE.md` 数据边界一节同步说明。用户自己输入的文字按原文保存，不属于工具数据投影。
  - 元数据缺失时认领已有历史：登记新会话前先确认底层 Session 为空，`get_items`、`commit_validated`、`clear_session` 共用同一登记路径；孤立历史拒绝且不登记。
  - 证据只绑定 `call_id`：`ToolRequest` 增加取自 SDK 工具上下文的 `tool_name`，Evidence 保存 `tool_name` 与规范化参数摘要，`project(call=ToolCall)` 核对调用标识、函数名与参数；暂存与回放都传入历史中的完整调用，调用缺失或参数不是 JSON 对象时拒绝。暂存改为逐项进行，使同批次的工具结果能找到先到的调用项。
  - 修复前新增/调整用例 7 项按预期失败；修复后因规则改变更新 3 个旧断言（模型不再看到 Session 不允许的 `total`，模型与 Session 内容相同）。
- **增量复审后修复（`11f9b13` 暂不通过的 1 项 P1，两处同根）：**
  - 模型与 Session 投影仍分别计算：字段集合和上限相同，但各按自己的字段顺序取舍，容量只放得下一个字段时两者选出不同内容，模型可把 Session 未选中的字段写进文字落库。现 `record` 只计算一次模型可达投影（按模型字段优先级，取三方共同字段与最小上限），同一份内容同时存为模型与 Session 投影，投影规则升为 `/3`。
  - 用户输入没有 Session 数据策略：`PolicySession` 增加必填的 `input_policy: SessionInputPolicy(max_bytes, forbidden_patterns)`，由可信配置提供（Task 5 按 Profile 的 `data_policy_id` 装配）。锁定版 SDK 在首个模型调用前写入用户输入，因此不符合时本轮零模型调用、零工具执行、不落库；回放时按当前策略复核，策略收紧后旧历史整段拒绝。代码只按确定的字节与模式判断，不识别任意自由文字中的敏感内容。
  - 修复前新增用例 4 项按预期失败（模型看到 `total` 而 Session 选中 `rows`；超长与含禁止模式的输入未被拒绝；收紧策略后仍回放），修复后通过，未修改任何已有断言。
- 反向验证 24 项（去掉暂存、保存原始工具结果、回放不复核、归属/Profile/过期/状态检查、轮数/字节上限（开始与提交两处）、最终回答校验、`writing` 状态、pop 已提交历史、先清除后关闭、调用绑定、暂存时证据核对、角色/文字片段/未知类型白名单、配对两处检查、引用格式、保留错误原文）均使对应用例失败。首轮有 3 项未变红：归属检查（原用例被 Evidence 查询顺带拦下，补“无证据历史”用例）、消息片段类型检查（被文字字段检查覆盖，已删除）、孤立结果（原用例总留有未配对调用，补孤立输出用例）。审查修复后另加 9 项（`_limits` 分别去掉模型/Session/渠道约束、恢复旧规则、去掉最小上限，绑定分别去掉调用标识/函数名/参数，登记前不查空），并对调用无法解析的两处拒绝补用例后变红；暂存批次回滚经判断不承重，已删除。增量复审修复后按新结构调整模型可达投影的变异，并新增分别计算投影（含 `11f9b13` 的同集合不同顺序形态）、去掉输入准入/字节/模式检查、回放不复核输入策略等项；共 37 项均失败。

## Task 4：最小 MCP Client Integration

**Files:** Modify `src/xiaowei/config.py`；Create `src/xiaowei/mcp.py`、`tests/sdk_core/mcp_fixture.py`、`tests/sdk_core/test_mcp_integration.py`。

**Interfaces:** `MCPServerConfig(server_id: str, url: str, auth_ref: str | None, timeout_seconds: float, allowed_tools: dict[str, str])` 为静态可信登记，`allowed_tools` 将远端工具名映射到已有 `policy_id`，对应策略代码固定预期输入/输出 schema 与参数含义；`MCPIntegration(configs: tuple[MCPServerConfig, ...], governance: GovernedTools)` 支持异步生命周期；`async tools_for(ctx: RunContext) -> list[Tool]` 返回 SDK 原生可用工具。具体绑定采用 Task 1 已验证的公共 SDK 扩展点，所有远程调用走 SDK；若以薄 function tool 暴露，则只转交，不自写协议或模型调用循环。

- [x] 编写 `test_empty_config_performs_no_network`、`test_sdk_runner_calls_governed_mcp`、`test_revoked_mcp_tool_has_zero_calls`、`test_unknown_schema_and_collision_fail_closed`、`test_auth_timeout_and_shutdown`、`test_mcp_payload_filtered_before_model`。临时 fixture 仅绑定 loopback；断言未获准或撤权请求 `fixture.tool_calls == []`，未知/写工具不在 Model tools，文本和 structuredContent 的禁止字段、不合约内容及伪造的可信 evidence_id 不进入 Model/Session，注入文本不能改变执行权限/预算，关闭后无遗留任务/连接。
- [ ] 运行 `uv run --locked --extra dev python -m pytest tests/sdk_core/test_mcp_integration.py -q`；按 fixture 需要允许 MCP 与 PostgreSQL loopback，不修改全套测试为允许外网。预期新集成缺失失败。（实施时实现先于测试，未留下“先失败”的记录；以 30 项反向验证和实现中发现的缺陷先红后绿代替，见实测记录。）
- [x] 实现一个 Streamable HTTP 接入路径，认证首片只支持无认证的测试 fixture 和可信配置引用的 Bearer 认证；认证引用在 app 装配解析，值不进入 RunContext。远端须 HTTPS，HTTP 只允许 loopback 测试；重定向不得将认证转交其他端点。端点不能来自用户/模型参数。发现工具后核对登记 schema 与参数/结果策略，名字按 server/tool 唯一映射，缓存不能跨授权范围共用。
- [x] 接通 `GovernedTools.invoke` 和 Evidence 结果入口；检查 MCP 文本、结构化内容、错误与资源引用。首片只支持明确登记的文本/JSON 结果契约；不支持内容类型拒绝，不自动读取资源链接。用 Task 1 核对过的 transport 公共扩展点设置接收上限，解析后再限制内容大小，禁止只在全部读取后切片冒充读取上限。超时/未知执行结果不自动重试；单个 Server 不可用仅隐藏其工具，本地工具继续可用。
- [x] 重跑本任务命令；预期全部 PASS。追加协议断言覆盖认证失败、超时、超大响应、取消与关闭；测试用假凭据不得出现在 Model、PostgreSQL 保存内容、日志或错误中。远端 readonly 标注不授予权限，远端 Evidence 仅作来源属性，小维另建可信绑定。
- [x] 仅暂存本任务文件，提交 `feat: add governed SDK MCP client integration`。

**Task 4 实测记录（与计划的差异与发现）：**

- 接口：`MCPServerConfig` 比计划多 `max_response_bytes`（接收上限）；`server_id` 只允许小写字母、数字与连字符且不能为 `local`，SDK 函数名为 `<server_id>__<tool>`，因此能唯一还原到 server/tool，组合长度不超过 64。`MCPIntegration(configs, governance, *, resolve_secret, clock)` 是异步上下文管理器，`tools_for(ctx)` 是同步方法（筛选不需要 I/O）。`GovernedTools` 增加只读属性 `catalog`。
- 登记与目录对应：每个获准远端工具必须在工具目录中有 `server_id/tool` 契约且策略一致；`ToolPolicy` 增加可选的 `result` 结果模型，MCP 工具的策略必须提供；SDK 函数名与其他工具（本地工具以 `tool_id` 名字部分为函数名）冲突、`server_id` 重复时拒绝装配。`ToolContract` 增加可信的 `description`，交给模型的工具说明只取它，远端说明文字不交给模型。
- 发现时核对：远端缺少登记的工具、同名多个、参数 schema 与契约不符（比较前去掉 `title`、`description` 与布尔的 `additionalProperties`；发出的参数总是先经禁止额外字段的策略参数模型校验。值为 schema 的 `additionalProperties` 即映射值类型保留比较）时只隐藏该工具。未登记的工具（含远端自称只读的）不开放。远端输出 schema 不比较，改为每次结果按结果模型校验。
- 结果契约：`is_error`、任何非文本内容（图片、资源链接等，资源不读取）、非 JSON 对象、结构化内容缺失时文本不是恰好一段 JSON 对象、结果模型按 JSON 严格模式校验失败，一律拒绝；通过时只保留结果模型声明的字段，再进入 Evidence 投影。远端自带的 `evidence_id`/`xiaowei_evidence_ref` 等字段不在结果模型中，不保存也不交给模型，来源由 Evidence 的 `tool_id`（`server_id/tool`）记录。
- 调用：薄 `FunctionTool` 解析参数（不是 JSON 对象时在治理前拒绝，远端零请求），经 `GovernedTools.invoke` 执行 `call_tool`。治理与证据的受控失败按 SDK 公开的 `default_tool_error_function` 交给模型，与本地 function tool 的默认行为一致；直接构造的 FunctionTool 抛错时 SDK 会中止整轮（实测），因此不能直接抛出。（Task 5 审查修复后改为：只有 I/O 前的拒绝交给模型，执行开始后的失败直接抛出以中止整轮，见 Task 5 实测记录。）
- HTTP：`httpx_client_factory` 返回 `trust_env=False`、`follow_redirects=False` 的客户端，底层 transport 只向与登记端点完全相同的规范化 URL（含 userinfo、未解码路径与 query）发送，Bearer 凭据只在这里加上（认证引用在进入时解析，不进入 RunContext）。锁定版 MCP 客户端会自行跟随同源重定向、不跟随跨源重定向；同源不同路径由 transport 拒绝。响应按实际读取字节计数，超过上限即停止；请求 `Accept-Encoding: identity`，压缩响应拒绝（少量压缩字节可解压成很大的内容）。超限异常继承 `httpx2.StreamError`，MCP 客户端据此把本次请求解析为错误。
- **超时拖垮连接（实现中发现并修复）：** 起初 HTTP 读取期限等于调用期限，读取超时在 MCP 客户端的 POST 任务中抛出，会关闭整个连接，此后该 Server 的所有调用失败。现单次调用期限由 SDK `client_session_timeout_seconds` 执行（它会中止对应的 POST），HTTP 读取期限为其 2 倍作兜底。补充断言“超时后的下一次调用照常执行”，修复前失败、修复后通过。越出端点或压缩编码在响应头阶段拒绝时仍会关闭该连接（失败方向安全）。
- 生命周期：进入时逐个连接，认证引用无法解析、连接或列出工具失败只隐藏该 Server 的工具并记录类型（不记录异常消息）；退出时清空工具并关闭全部连接。锁定版 SDK 与 fixture 关闭时未见 `DELETE` 会话终止请求，关闭验证以服务端连接数归零和无遗留 asyncio 任务为准。
- **首轮独立审查修复（4 组 P1）：**
  - 端点比较只看解码后的路径：同源重定向到 `/mcp?other-service=1` 或 `/mcp%2Ftenant-a` 仍带 Bearer。改为比较完整规范化 URL，补 query 与编码路径重定向用例。
  - 结果契约宽松且未绑定证据：结果模型默认宽松校验会把 `"7"` 转成整数，`extra="allow"` 会保留未声明字段；证据指纹不含结果模型，却含工具说明。改为 JSON 严格模式校验；工具目录要求结果模型（含嵌套）不接收未声明字段、投影字段属于结果模型；指纹加入去掉标题/说明的结果 schema 与参数 schema，排除工具说明，投影规则版本升为 `model-reachable-shared/4`（旧证据失效）。
  - MCP 库的 DEBUG 日志在本地过滤前记录完整参数与远端结果。首轮修复把 `mcp` logger 改为不向上传递、经处理器转出固定信息；复审指出直接挂在 `mcp` 或 `mcp.*` 上的处理器仍收到原始记录（logging 在传播前就调用原 logger 上的处理器）。修复中又发现客户端会话模块的 logger 名为 `client`，按名字约束覆盖不到。最终改为包装进程的 LogRecord 工厂（标准库公开接口），按源文件是否位于 `mcp` 包判断，记录生成时只保留 logger 名字、级别与异常类型，任何位置、任何时间挂上的处理器都看不到原始内容。回归用例在 root、`mcp`、`mcp.*`（接入前后）各挂普通处理器，并让远端夹带未知通知、参数无效的通知与畸形消息，覆盖日志参数与异常文字。不用 caplog：pytest 会按自己的方式挂处理器。
  - FunctionTool 的严格 schema 转换在连接之后、单 Server 隔离之外执行，映射参数会中止整个接入：工具改在构造时（连接前）建好，不支持的形状作为登记错误拒绝；远端工具核对移入单 Server 的异常边界。映射参数暂不支持。
  - 审查修复反向验证 13 项均使对应用例失败：端点只比路径、只比解码路径加 query、宽松结果校验、允许结果模型额外字段、不检查嵌套模型、不检查投影字段、指纹不含结果 schema、指纹含工具说明、指纹用未规范化的结果 schema、不约束 `mcp` 日志、转出时保留原消息、工具构造错误不转为登记错误、契约一侧不忽略布尔 `additionalProperties`。另有一项只改远端一侧的变异无效（远端 schema 本无该字段），不计入。复审修复 5 项均使日志用例失败：不包装工厂、保留日志参数、保留异常、按 logger 名字判断、改回只关闭 `mcp` 传播。
- 反向验证 30 项均使对应用例失败：展示不按范围、绕过治理、去掉 schema 比较或比较忽略 `type`、去掉名字冲突/策略一致/结果模型/`server_id` 重复检查、接受 `is_error`、接受非文本内容、不按结果模型校验、去掉端点检查、不加认证、去掉接收上限、接受压缩、读取期限等于调用期限、受控失败直接抛出、去掉参数形状检查、退出后保留工具、退出不关闭、连接不登记关闭、认证引用或连接失败时中止启动，以及配置的 HTTP 非 loopback、用户信息、查询参数、原始凭据、`local`、名字过长、非法工具名。转发前删除已有 `Authorization` 头与运行时 `RunContext` 类型检查、`ensure_strict_json_schema` 转换经判断不承重，已删除。

## Task 5：组合可验证的运行核心并交接 P1-B

**Files:** Create `src/xiaowei/app.py`、`tests/sdk_core/test_app.py`；Modify `README.md`、`AGENT_HANDOFF.md`、`.github/workflows/ci.yml`。

**Interfaces:** `Application` 构造时接收可信配置、Task 1B 装配的 SDK Model/ModelSettings 与 Profile 指纹、治理/Evidence 与 Session 依赖；`async Application.run_turn(ctx: RunContext, message: str) -> Delivery` 是后续两渠道共用入口。每轮创建相应工具集合的 SDK Agent，`output_type=AgentAnswer`，调用原生 `Runner.run`；会话从可信身份定位，不能由用户请求任意指定他人的 session。模型接入凭据、客户端与 Profile 配置均不进入 RunContext。应用将 `data_policy_id` 装配到模型输入/结果与 Session 投影策略中，发送前按实际接收方检查数据权限。

应用提供方法 `Application.scope_for_turn(mode: Literal["query", "diagnose"], authorized_tools: frozenset[str], available_tools: frozenset[str]) -> frozenset[str]`，与构造时装配的可信用途允许表取交集；入口每条消息重新调用，模型不能调用它授权自己。本片测试以可信合成入口构造 context，P1-B 实现按钮/指令解析。关联编号复用应用生成的 `turn_id`，阶段日志通过标准库 logging 输出白名单字段。

- [x] 编写 `test_local_and_mcp_followup_through_real_runner`、`test_concurrent_sessions_are_isolated`、`test_same_session_reentry_is_rejected`、`test_invalid_answer_is_never_delivered_or_committed`、`test_timeout_does_not_replay_tools`。断言两轮工具结果可合法引用；不同渠道得到不同获准投影；一轮权限变化不能改写另一轮 Agent tools；超限/取消无成功 Delivery，未经校验内容既不提交 Session 也不发送。
- [x] 编写 `test_query_permission_is_not_inherited`、`test_commit_failure_does_not_replay_tools`、`test_stage_logs_contain_only_safe_metadata`。前一轮查询、下一轮默认诊断时，查询工具不出现且强行调用零执行；实际 PostgreSQL 提交边界注入保存失败后无成功交付，不自动补跑工具，必要时隔离会话；阶段日志共用请求编号且不含合成 SQL、结果、假凭据和原始异常。
- [x] 运行 `uv run --locked --extra dev python -m pytest tests/sdk_core/test_app.py -q`；预期新应用入口缺失失败。（实测：移走 `app.py` 时收集阶段失败。）
- [x] 实现上述 `run_turn`：存储就绪检查、总期限、有限并发、同会话互斥、每轮权限表与受限阶段日志；校验 SDK 最终结构与 Evidence 后才提交 Session，交付前再复核权限和渠道。校验不通过返回受控失败，不把模型完整回答当报错输出。本片不发送外部渠道消息，不提供未校验结论流。P1-B 另验证 Session 已提交但最终结果保存失败的双存储边界，以及渠道投递状态。
- [x] 运行 `uv run --locked --extra dev python -m pytest tests/sdk_core -q`、`uv run --locked --extra dev ruff check src/xiaowei tests/sdk_core`、`uv run --locked --extra dev mypy src/xiaowei`；预期全部通过。CI 已在 Task 1 集成中接入同一隔离 PostgreSQL harness 和完整 pytest 命令；新测试须继续进入该路径，数据库缺失/不可用不能 skip 成全绿。旧检查标识为历史检查，不用旧通过率代替新能力验证。
- [ ] 在已获准模型环境使用合成数据，按 OpenAI/Gemini/DeepSeek 的每个选定 Profile 分别验证本地和 MCP 工具调用、工具结果回传、真实 `AgentAnswer` 类型/Evidence 校验与下一轮 Session 追问；至少先完成一个 Profile。记录 SDK、端点标识、协议、模型/模式、用量和结果，其他组合如实标为未验证/不兼容；不因一家通过宣称全部支持，不用 scripted Model 或 HTTP mock 代替真实验证。更新 README 的核心开发验证命令、handoff 的精确 SHA/证据/缺口；保持正式 Web/飞书启动说明未交付的事实。
- [x] 仅暂存本任务文件，提交 `feat: compose verified SDK application core`。（P1-A 整体独立审查待进行。）对整个 P1-A 分支做一次独立审查，修复阻塞项后细化 P1-B；不自动合并、部署或归档。

**Task 5 实测记录（与计划的差异与发现）：**

- 接口：`AppConfig(instructions, purposes, data_policies, session_limits, max_concurrent_turns)` 为可信配置；`purposes` 必须同时给出 `query` 与 `diagnose`；`data_policies` 以 Profile 的 `data_policy_id` 为键，`DataPolicy(input: SessionInputPolicy, model_tools: frozenset[ToolId])` 给出用户输入准入与结果可交给该模型的工具。`Application(config, *, profile, model, engine, governance, evidence, local_tools, mcp=None, clock)`：接收 Profile 本身，由它计算 SDK 设置与指纹，避免三者不一致；`local_tools` 把已登记的 `local/` 工具映射到应用绑定的 I/O 函数，由应用构造受治理的 FunctionTool，调用方不能传入未经治理的工具。`available_tools` 返回本地工具与已核对的 MCP 工具（`MCPIntegration.available_tool_ids`）。`scope_for_turn` 按计划签名，另与数据策略的 `model_tools` 取交集。`run_turn` 失败时抛出 `TurnError(reason)`，原因代码固定，取消照常传播。
- 共享包装：本地工具与 MCP 工具共用新模块 `tools.py` 的 `governed_function_tool`（参数解析、`ToolRequest`、`GovernedTools.invoke`、受控失败交给模型）；`mcp.py` 改为调用它，行为不变（Task 4 的 22 项用例照常通过）。
- 每轮：同会话已在运行即拒绝（`session_busy`），并发达到上限即拒绝（`busy`），检查与登记之间没有 await；不排队。本轮 Tool Scope 超出数据策略时拒绝；存储就绪检查不通过时拒绝；二者都在模型调用前。之后新建本轮的 SDK Agent（工具列表只属于这一轮）与 `PolicySession`，调用 `Runner.run`（`max_turns` 取自预算，`RunConfig` 显式关闭 tracing）。最终回答先单独经 Evidence 校验（原因明确为 `answer_rejected`），再 `commit_validated`（其内再校验一次），提交后按接收渠道与当前权限重新生成 `Delivery`。总期限取 `Budget.timeout_seconds`，覆盖包括提交在内的整轮；期限或取消发生在提交过程中时会话停在 `writing`，不再回放（失败方向安全）。`finally` 中清理本轮工具计数并释放会话。
- 错误映射：会话、证据与存储错误沿用本包的固定信息；SDK、模型客户端与其他异常一律为固定的 `model_failed`，不带原因链——模型客户端会把上游错误体写进异常消息。模型强行调用本轮未展示的工具时，SDK 抛出 `ModelBehaviorError`，本轮失败且不提交。
- 阶段日志：`xiaowei.app` 输出 `turn=<turn_id> stage=<阶段> reason=<原因代码> elapsed_ms=<毫秒>`，阶段为 received、storage_ready、answered、committed、delivered，或 refused、failed、cancelled；不记录消息、回答、工具数据或异常。未替换 LogRecord 工厂，Task 4 的 MCP 日志约束保持。
- `ci.yml` 未修改：integration job 已在隔离 PostgreSQL 上运行完整 pytest，新用例自动进入该路径。
- 反向验证 21 项：去掉同会话检查、并发上限，检查与登记之间加 await，各轮共用同一工具列表，用途或数据策略不取交集，`run_turn` 不查数据策略，不查存储就绪，没有总期限，交付前不复核，提交前不单独校验，不清理轮次计数，透传下层异常消息，保留原因链，日志含消息，开启 tracing，本地工具不按范围，输入策略不按 Profile，不校验 `local/` 前缀，不校验用途登记。20 项使对应用例失败。“透传下层消息”起初未被发现（没有用例的下层异常消息含敏感内容），补充“上游错误”用例后失败。“失败时 `discard_pending`”经变异证明不承重（每轮新建 PolicySession，暂存随对象丢弃），已删除。另把共享包装改为绕过治理，Task 4 与 Task 5 共 15 项用例失败。
- **首轮独立审查修复（针对 `6d3464d` 的 4 组 P1）：**
  - 运行依赖未绑定：`Application` 分别接收 Profile、裸 Model、治理、证据与 MCP，错误装配可让数据发往另一端点而会话记录另一 Profile，或让回放使用另一授权来源。`open_model` 改为返回只能由它创建的 `ModelBinding`；`Application(config, *, model: ModelBinding, engine, governance, local_tools, mcp=None, clock)` 不再接收 Profile 与证据存储；`GovernedTools(evidence)` 的目录与授权取自证据存储；MCP 接入的治理对象必须相同。应用测试改为 `open_model` + `httpx2.MockTransport` 驱动真实 `OpenAIResponsesModel`。
  - 数据策略未绑定会话：会话绑定改为 Profile 指纹与规范化数据策略（输入准入、排序后的 `model_tools`）的组合摘要，写入原 `profile_fingerprint` 列；回放时逐条检查工具是否仍在策略内与此重复，未另加。
  - 参数校验会转换类型且结果被丢弃（Task 2 存量）：`invoke` 以 `json.dumps(allow_nan=False)` + `model_validate_json(strict=True)` 校验，`execute` 签名改为接收规范化后的 `ToolRequest`；工具目录要求参数模型（含嵌套）`additionalProperties: false` 且全部字段必填。证据摘要仍按模型原始参数计算，与会话历史中的调用一致。
  - 结果未知仍交给模型（Task 4 包装层）：`governed_function_tool` 只把 `ToolRejectedError` 交给模型；其他异常由 SDK 包装为 `UserError` 中止本轮，应用按原因链映射为 `tool_failed`。测试工具 `sdk_tool` 改为调用产品包装；Task 2 的执行期间撤权与 Task 4 的执行后失败用例相应改为断言本轮中止、模型只调用一次。
  - 反向验证 13 项均使对应用例失败：不检查模型绑定类型、绑定可直接构造、不检查 MCP 治理一致、会话只绑定 Profile、绑定不含工具、不含输入策略、非严格校验、允许非有限数值、执行收到原始参数、允许默认值、允许未声明参数、执行后失败交给模型、不映射工具失败。首轮 20 项在新装配下重跑仍全部失败。
  - 另发现（未扩项）：SDK 模型错误日志的内容隐去依赖 `OPENAI_AGENTS_DONT_LOG_MODEL_DATA` 的默认值，已加用例；显式关闭时会记录上游错误体。
- **增量复审修复（针对 `cd79114` 的 2 组 P1）：**
  - 凭据未绑定：`open_model(profile, *, transport=None)` 不再接收密钥，内部按 `profile.api_key_ref` 用 `resolve_secret_ref` 解析，失败时不创建客户端。测试经各 Profile 引用的环境变量提供假密钥。
  - 运行时行为取自可覆盖的 schema、执行与证据两个参数来源：`normalize_arguments(policy, arguments)` 为唯一规范化入口（`strict=True`、`extra="forbid"`、`allow_nan=False`），`invoke` 以它的结果构造单一有效请求，交给执行与 `EvidenceStore.record`；`EvidenceStore.project(call=...)` 先用同一函数规范化历史参数再比较摘要。MCP 结果校验加 `extra="ignore"`。工具目录删除基于 schema 的额外字段与必填检查（前者由校验调用强制，后者改为按 Pydantic 运行时字段递归检查，并限定参数类型为基本类型、枚举、Literal、嵌套模型及其容器）。原“参数/结果模型允许额外字段即登记失败”的目录用例相应改为运行时用例：额外参数在 I/O 前拒绝，额外结果字段被忽略。
  - 反向验证 8 项均使对应用例失败，见 handoff。
- **第三、四轮复审修复（针对 `bdae614`、`0ce5b2f` 各 1 项 P1，同根）：** 严格校验之后以 `model_dump` 的输出作为有效数据，计算字段与自定义 serializer 可以加入契约外字段或改变类型（参数与 MCP 结果同根）。第三轮改为排除计算字段并把输出交回同一模型校验，第四轮复审证明同一模型的 before validator 能在 `extra="forbid"` 之前删掉 serializer 加的字段，二次校验不能证明输出合约。现在 `governance.contract_dump(validated)` 不调用模型的序列化，也不交回同一模型校验：按声明字段与类型从已校验实例递归生成 JSON，每个值独立核对（严格类型、有限浮点、嵌套模型只取声明类型的字段、枚举取值），不符抛 `ValueError`。`normalize_arguments` 与 `mcp._payload` 都经它生成；工具目录把参数与结果模型的字段类型限定在生成规则之内（映射键只能是 `str`，枚举与 Literal 取值为 JSON 基本类型）。错误分类沿用：参数在 I/O 前拒绝（回放时证据不可读），结果不合约本轮中止。
- **第五轮复审修复（针对 `f79bba3` 的 2 项 P1、2 项 P2）：** 两个根因。（1）生成器按注解重新解释值，没有跟随校验出的实际值：根部取实例的运行时类型、联合类型按声明顺序取第一个能投影的分支、枚举/Literal 解包后绕过有限数值检查。改为 `contract_dump(model, validated)` 由调用方传入登记模型；模型、枚举与基本类型要求类型完全一致，联合类型因此只匹配实际分支；取字段失败转为 `ValueError`；`_json_scalar` 统一核对有限 JSON 基本值，登记（枚举成员、Literal 选项）与生成共用。与复审矩阵的一处差异：根对象被 validator 换成子类时，矩阵期望投影回登记字段，现实现受控拒绝（零 I/O）——子类投影与"联合类型跟随实际分支"冲突（`Base | Derived` 中 `Derived` 也是 `Base`），而替换登记模型只可能来自自定义 validator。（2）数据生成规则变化没有使旧证据失效：`_PROJECTION_RULE` 升为 `/5`，注释写明结果数据生成规则变化也须升级。
- 未完成：真实模型验证（本会话没有获准的模型端点与凭据，三个 Profile 均未验证）；第五轮修复版本的独立复审。

## 完成定义与覆盖边界

本片离线完成要求：真 Runner、SDK 模型 API 的 HTTP 契约、两条治理工具路径、真实 loopback MCP 协议、SQLAlchemySession + 真实 PostgreSQL 读写/回放、用途范围与代码生成事实均有对应测试；存储初始化/版本不符、历史超限/过期和保存失败路径通过；新包独立可安装且默认无 trace 外发。真实模型验证按 Profile 单列状态，缺失时仍为待验证；至少一个 Profile 通过实际工具闭环后才有真实模型运行证据。

本片产出是运行核心，不是首版可用产品。真实 StarRocks 的读取/SQL 限制与业务口径、Web 和飞书的用途选择/新建会话、渠道去重/发送失败/重发、正式历史读取、清理命令与健康端点属于 P1-B；EXPLAIN 诊断属于 P2；两容器部署、SSH 访问、备份恢复与用户接受属于 P3。不得把本片测试通过写成这些能力已经完成。
