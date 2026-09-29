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
- `ToolRequest(tool_id: str, target_id: str, arguments: dict[str, object])`；`ToolObservation(payload: dict[str, object], captured_at: datetime, truncated: bool)` 只容纳已限制读取大小的临时结果，不自动序列化给模型。
- `EvidenceRecord` 包含代码生成的 `evidence_id`、可信身份/会话、target/tool/call 标识、采集与过期时间、截断状态及各用途获准内容；持久化不保留原始结果。
- `ToolResult(evidence_id: str, model_content: str, truncated: bool)` 仅为模型可见内容。`AnswerInference(text: str, evidence_ids: tuple[str, ...])` 和 `AgentAnswer(evidence_ids: tuple[str, ...], inferences: list[AnswerInference], clarification: str | None)` 作为 `output_type`；不让模型提交“已核实数值/实际 SQL”等事实字段。每条分析必须有有效引用，且属于回答选择的 Evidence；代码从这些记录的获准投影生成事实区域，分析单独标注。澄清不允许展示查询结果或与结果/分析混用；自由文字的语义仍靠评测，不能把 schema 校验当作事实验证。
- `Delivery(content: str, evidence_ids: tuple[str, ...], channel: Channel)` 是通过验证的渠道输出；同一回答也要按接收渠道重新生成。

## Task 1：锁定 SDK 并验证公开扩展点

**Files:** Modify `pyproject.toml`、`uv.lock`；Create `src/xiaowei/__init__.py`、`src/xiaowei/config.py`、`src/xiaowei/storage.py`、`compose.sdk-test.yml`、`tests/sdk_core/conftest.py`、`tests/sdk_core/test_sdk_contract.py`。

**Interfaces:** 消费官方 `Agent`、`Runner`、`Model`、`Session`、`SQLAlchemySession` 与 MCP 公共类型；产出 `configure_runtime() -> None`，负责显式关闭默认 tracing/export，以及可安装的新包。`storage.py` 提供 `open_engine(database_url: SecretStr) -> AsyncIterator[AsyncEngine]` 异步上下文管理器、显式部署入口使用的 `async initialize_storage(engine: AsyncEngine) -> None` 与运行时 `async check_storage(engine: AsyncEngine) -> None`；本任务只处理 SDK 表，Task 2 加入应用表及版本检查。URL 仅来自私有配置，不打印或进入 context；具体 SDK 初始化/Session 方法签名以锁定版本为准，在本任务测试中固定。

- [ ] 编写 `test_real_runner_calls_tool_and_returns_typed_answer`、`test_postgres_session_public_roundtrip`、`test_storage_unavailable_or_uninitialized_is_rejected`、`test_public_mcp_interception_contract`、`test_default_tracing_has_no_export`。使用官方 Model 接口实现测试用 scripted Model；核心断言分别为 `tool_calls == 1`、类型化回答正确、SQLAlchemySession 在真实 PostgreSQL 上重建对象后仍可回放成对调用、存储未就绪时不调用模型、公开 MCP 边界可拦截、`trace_exports == []`。
- [ ] 运行 `uv run --extra dev python -m pytest tests/sdk_core/test_sdk_contract.py -q`；记录新依赖/模块缺失或能力不满足的失败，不把安装故障当行为测试通过。
- [ ] 安装核对当前 Python 兼容版本，将 `openai-agents[sqlalchemy]`、SQLAlchemy、asyncpg 和开发依赖锁入 `uv.lock`，核对 extra 的实际依赖；将 `src/xiaowei` 加入打包但不把旧 CLI 宣称为新产品。实现运行配置，验证 SDK Session、MCP 调用/结果拦截、HTTP transport 的超时与接收限额扩展点。只使用公开 API；若不能实现必要边界，记录具体 API 限制并调整本计划，不绕过治理启用该能力。
- [ ] 建立测试专用 PostgreSQL Compose 与 fixture，锁定镜像、限制 loopback 访问，只清理本测试创建的项目/数据库，禁止指向已有运行数据。实现有界连接池、超时及退出释放，确认 SDK 的公开建表路径；正式运行不在普通请求中自动建表。记录初始化、启动、健康等待和清理的实际命令，SDK 与应用表不共享写事务的假设由后续失败测试覆盖。
- [ ] 运行 `uv sync --locked --extra dev` 和 `uv run --locked --extra dev python -m pytest tests/sdk_core/test_sdk_contract.py -q`；预期全部 PASS。测试不得访问真实模型或读取既有凭据；新包 import 不加载旧 `xiaowei_agent`。
- [ ] 仅暂存本任务文件，提交 `build: establish verified Agents SDK runtime`；记录锁定版本及 Python 版本。

## Task 1B：模型 API 配置与 SDK 接入

**Files:** Modify `src/xiaowei/config.py`；Create `src/xiaowei/model_api.py`、`tests/sdk_core/test_model_api.py`。本任务不引入通用网关、自动路由或第三方 Agent 运行引擎；直接使用已锁定 SDK 与其 OpenAI HTTP 客户端依赖。

**Interfaces:** `ModelProfile` 为 Pydantic 配置类型，字段固定为 `profile_id: str`、`provider: Literal["openai", "gemini", "deepseek", "openai_compatible"]`、`base_url: str`、`api_mode: Literal["responses", "chat_completions"]`、`model: str`、`api_key_ref: str`、`output_mode: Literal["json_schema", "json_object"]`、`request_timeout_seconds: float`、`max_output_tokens: int`、`max_request_bytes: int`、`max_response_bytes: int`、`data_policy_id: str`、`reasoning_effort: str | None`。期限/限额为正值，地址为可信 HTTPS 端点，模型 ID 必填，不使用 SDK 隐式模型默认值；推理参数仅接受所验证 Profile 的允许值。`open_model(profile: ModelProfile, *, api_key: SecretStr) -> AsyncIterator[Model]` 是异步上下文管理器，装配并关闭独立客户端，返回 SDK `Model`；`settings_for(profile: ModelProfile) -> ModelSettings` 返回 SDK 设置。`profile_fingerprint(profile: ModelProfile) -> str` 返回不含凭据的稳定配置版本，供会话绑定；`SecretStr` 来自 Pydantic，SDK 类型直接使用。

- [ ] 编写 `test_sdk_responses_and_chat_completions_tool_roundtrip`、`test_json_object_still_validates_output_type`、`test_profiles_keep_keys_and_endpoints_separate`、`test_api_failure_has_no_retry_or_fallback`、`test_request_and_output_limits`。使用 HTTP mock transport 接真实 SDK Model 与 Runner，不以 scripted Model 冒充 API 适配测试；本任务最终回答先用测试文件内的简单 Pydantic schema，Task 5 再测真实 `AgentAnswer`。断言工具调用后确有模型续轮；JSON mode 缺字段/错类型/空内容不能返回成功；三个 Profile 的假密钥仅发往各自端点；401/403、429、503、超时和非法 JSON 无跨端点请求、无自动重试、无重复工具执行。
- [ ] 运行 `uv run --locked --extra dev python -m pytest tests/sdk_core/test_model_api.py -q`；预期因模型配置与装配缺失失败，mock transport 不建立外网连接。
- [ ] 实现上述配置与装配，使用锁定版 SDK 的公开 Responses / Chat Completions 模型接入。客户端设置明确请求超时、`max_retries=0` 与禁止跨端点重定向，凭据由应用解析安全引用后传入；不设置可变全局客户端。通过公开 HTTP hook 在发出前检查序列化请求字节上限；模型输出 token 上限映射到所选协议，在 HTTP 读取阶段落实 `max_response_bytes`，不能读完整超大响应后才切片；usage 缺失不记为 0。供应商专有参数通过少量经验证的映射生成，不开放任意配置透传。
- [ ] 对 `json_object` 路径，只用 SDK 公开模型设置或薄 `Model` 委托适配请求编码及 schema 提示，保留外层 `output_type` 的类型校验；工具选择和循环仍由 Runner 完成。验证工具 strict 参数、最终输出模式与推理字段的组合；必要字段不能与 Session 策略兼容的模式保持禁用。若公开接口无法满足某组合，记录不兼容原因，不删除校验或改写 SDK 私有代码，不将该 Profile 标为已支持。
- [ ] 重跑本任务命令；预期所有已实现路径及拒绝路径 PASS。核对请求无未支持参数，假密钥不进入日志/错误/trace，Profile 指纹不含密钥值，客户端正常关闭。记录 SDK 实际类名、方法签名与三家验证矩阵；HTTP mock 通过仍不等于供应商真实验证通过。
- [ ] 仅暂存本任务文件，提交 `feat: configure SDK model API profiles`。

## Task 2：本地受治理工具、数据投影与 Evidence

**Files:** Modify `src/xiaowei/storage.py`；Create `src/xiaowei/models.py`、`src/xiaowei/governance.py`、`src/xiaowei/evidence.py`、`migrations/sdk_app/001_initial.sql`、`tests/sdk_core/test_governance.py`、`tests/sdk_core/test_evidence.py`。

**Interfaces:** 定义上文类型；`GovernedTools.allowed_contracts(ctx: RunContext) -> list[ToolContract]`、`async GovernedTools.invoke(ctx: RunContext, request: ToolRequest, execute: Callable[[], Awaitable[ToolObservation]]) -> ToolResult`。依赖通过构造器装配，`execute` 只能由应用绑定，模型不能提供。`async EvidenceStore.record(ctx: RunContext, request: ToolRequest, observation: ToolObservation) -> ToolResult`；`async EvidenceStore.project(evidence_id: str, ctx: RunContext, audience: Literal["model", "session", "web", "feishu"]) -> str`；`async EvidenceStore.validate_answer(answer: AgentAnswer, ctx: RunContext) -> Delivery`。存储使用 SQLAlchemy / asyncpg 操作独立的 PostgreSQL 应用表，当前授权查询由应用提供的回调执行。首次应用表含 Evidence、Session 元数据与 schema 版本；版本化 SQL 只作用于应用表，由显式初始化/升级路径执行并检查版本，不建设通用迁移框架。

- [ ] 编写 `test_revoked_tool_never_reaches_io`、`test_parallel_calls_share_one_budget`、`test_context_rejects_dependencies`、`test_four_data_boundaries`、`test_forged_foreign_expired_evidence_is_denied`、`test_query_claim_without_evidence_is_denied`。关键断言：拒绝时 `recorded_calls == []`；上限 1 时两个并行请求总执行数为 1；原始合成字段 `private_note` 不出现在模型/Session/渠道；为四种投影设置不同字段/字节上限并分别满足；撤权后不能读取已保存内容。
- [ ] 编写 `test_diagnose_scope_hides_and_denies_query`、`test_facts_are_rendered_from_evidence`、`test_app_schema_version_is_checked`。用合成查询工具验证：即使用户拥有查询权限，诊断轮也不展示且强行调用零 I/O；真实值 100 的事实区域只能由 Evidence 生成，模型额外提交事实数值字段被拒绝，分析里的文字不被放入事实区域；SDK 与应用表互不改写，版本不匹配拒绝使用。测试不声称能够识别任意自然语言中的错误推断。
- [ ] 运行 `uv run --locked --extra dev python -m pytest tests/sdk_core/test_governance.py tests/sdk_core/test_evidence.py -q`；预期因缺少新类型/行为失败。
- [ ] 实现上述类型与最少服务方法。允许集合来自可信配置并受本轮 Tool Scope 限制，实际调用时复核目标、契约、参数与最新权限，原子预占工具预算。用仅测试存在的合成 function tools/recording adapter 验证；不包装成伪 StarRocks。代码生成证据 ID，按用途投影后才写入 Evidence；未知投影规则拒绝。事实区域由代码从引用的获准投影生成，分析单独标注并验证引用；回答类型拒绝额外事实字段。错误转成有限、安全的工具失败，不携带 raw payload。
- [ ] 重跑本任务命令；预期全部 PASS。确认不同用户/会话/渠道不能读取彼此证据，截断和空结果都有明确语义，真实 PostgreSQL 保存内容不含合成禁止字段，重建存储对象后归属/权限/过期检查仍成立。
- [ ] 仅暂存本任务文件，提交 `feat: govern tool execution and evidence boundaries`。

## Task 3：原生 Session 的存储前与回放前策略

**Files:** Create `src/xiaowei/session.py`、`tests/sdk_core/test_session_policy.py`。

**Interfaces:** 消费 `RunContext`、异步 `EvidenceStore.project`、Task 1B 的 Profile 指纹与 SDK `Session`；产出 `PolicySession(inner: Session, context: RunContext, evidence: EvidenceStore, profile_fingerprint: str, limits: SessionLimits)`，实现锁定版 SDK 所需的公开 Session 接口并委托底层。`SessionLimits(max_history_turns: int, max_history_bytes: int, retention_seconds: int)` 均为正值，由可信配置提供，不把 SDK `max_turns` 当作历史上限；应用元数据记录会话失效时间。会话的 Profile 绑定保存在应用元数据中；不匹配时拒绝历史读取，要求新建会话。提供 `async discard_pending() -> None`、`async commit_validated() -> None`；后者只由验证成功的应用路径调用。

- [ ] 编写 `test_runner_session_filters_before_write`、`test_replay_rechecks_permissions`、`test_valid_tool_pair_survives_followup`、`test_invalid_final_never_persists`、`test_partial_store_failure_invalidates_session`。用 SQLAlchemySession、隔离的真实 PostgreSQL 和真 Runner 断言：`forbidden_text not in stored_items`，撤权数据不在第二轮 Model 输入，SDK 能消费过滤后成对工具项，未经验证最终输出不会落盘。
- [ ] 编写 `test_history_limit_stops_before_model_call`、`test_expired_history_or_evidence_cannot_replay`。轮数/字节达到配置上限时新轮零模型调用并提示新建；用可控时间验证未执行物理清理的过期记录也不能回放。若本轮暂存项仍使总历史超限，则拒绝提交、封存会话并返回受控的新建提示，不裁掉半组工具项，不自动重跑已执行工具。
- [ ] 编写 `test_changed_model_profile_cannot_read_old_session`，断言端点/协议/模型或数据接收策略变化后，旧历史在首个模型 HTTP 请求前被拒绝；新建会话可独立运行，不复制旧内容。
- [ ] 运行 `uv run --locked --extra dev python -m pytest tests/sdk_core/test_session_policy.py -q`；预期新包装/行为缺失失败。
- [ ] 实现 `PolicySession`：在 SDK 写入请求时先按 Session 策略处理并暂存本轮项，最终校验后才委托写入；回放前重新检验证据和权限。敏感用户输入也受字段/字节策略约束；不能只过滤工具结果。保持 SDK item 类型与 call/result 配对，若无安全回放形式则拒绝继续该历史。失败丢弃待写项，底层部分写失败时隔离该会话，不能自动重跑或将其视作完整历史。
- [ ] 重跑本任务命令；预期全部 PASS。验证 clear/pop 等 SDK 公共契约与待写项一致，应用元数据与 SDK 清理失败时仍保持不可回放状态；持久化和 replay 不是同一份未经处理的模型记录。物理清理命令及渠道新建会话在 P1-B 交付。
- [ ] 仅暂存本任务文件，提交 `feat: enforce Session write and replay policy`。

## Task 4：最小 MCP Client Integration

**Files:** Modify `src/xiaowei/config.py`；Create `src/xiaowei/mcp.py`、`tests/sdk_core/mcp_fixture.py`、`tests/sdk_core/test_mcp_integration.py`。

**Interfaces:** `MCPServerConfig(server_id: str, url: str, auth_ref: str | None, timeout_seconds: float, allowed_tools: dict[str, str])` 为静态可信登记，`allowed_tools` 将远端工具名映射到已有 `policy_id`，对应策略代码固定预期输入/输出 schema 与参数含义；`MCPIntegration(configs: tuple[MCPServerConfig, ...], governance: GovernedTools)` 支持异步生命周期；`async tools_for(ctx: RunContext) -> list[Tool]` 返回 SDK 原生可用工具。具体绑定采用 Task 1 已验证的公共 SDK 扩展点，所有远程调用走 SDK；若以薄 function tool 暴露，则只转交，不自写协议或模型调用循环。

- [ ] 编写 `test_empty_config_performs_no_network`、`test_sdk_runner_calls_governed_mcp`、`test_revoked_mcp_tool_has_zero_calls`、`test_unknown_schema_and_collision_fail_closed`、`test_auth_timeout_and_shutdown`、`test_mcp_payload_filtered_before_model`。临时 fixture 仅绑定 loopback；断言未获准或撤权请求 `fixture.tool_calls == []`，未知/写工具不在 Model tools，文本和 structuredContent 的禁止字段、不合约内容及伪造的可信 evidence_id 不进入 Model/Session，注入文本不能改变执行权限/预算，关闭后无遗留任务/连接。
- [ ] 运行 `uv run --locked --extra dev python -m pytest tests/sdk_core/test_mcp_integration.py -q`；按 fixture 需要允许 MCP 与 PostgreSQL loopback，不修改全套测试为允许外网。预期新集成缺失失败。
- [ ] 实现一个 Streamable HTTP 接入路径，认证首片只支持无认证的测试 fixture 和可信配置引用的 Bearer 认证；认证引用在 app 装配解析，值不进入 RunContext。远端须 HTTPS，HTTP 只允许 loopback 测试；重定向不得将认证转交其他端点。端点不能来自用户/模型参数。发现工具后核对登记 schema 与参数/结果策略，名字按 server/tool 唯一映射，缓存不能跨授权范围共用。
- [ ] 接通 `GovernedTools.invoke` 和 Evidence 结果入口；检查 MCP 文本、结构化内容、错误与资源引用。首片只支持明确登记的文本/JSON 结果契约；不支持内容类型拒绝，不自动读取资源链接。用 Task 1 核对过的 transport 公共扩展点设置接收上限，解析后再限制内容大小，禁止只在全部读取后切片冒充读取上限。超时/未知执行结果不自动重试；单个 Server 不可用仅隐藏其工具，本地工具继续可用。
- [ ] 重跑本任务命令；预期全部 PASS。追加协议断言覆盖认证失败、超时、超大响应、取消与关闭；测试用假凭据不得出现在 Model、PostgreSQL 保存内容、日志或错误中。远端 readonly 标注不授予权限，远端 Evidence 仅作来源属性，小维另建可信绑定。
- [ ] 仅暂存本任务文件，提交 `feat: add governed SDK MCP client integration`。

## Task 5：组合可验证的运行核心并交接 P1-B

**Files:** Create `src/xiaowei/app.py`、`tests/sdk_core/test_app.py`；Modify `README.md`、`AGENT_HANDOFF.md`、`.github/workflows/ci.yml`。

**Interfaces:** `Application` 构造时接收可信配置、Task 1B 装配的 SDK Model/ModelSettings 与 Profile 指纹、治理/Evidence 与 Session 依赖；`async Application.run_turn(ctx: RunContext, message: str) -> Delivery` 是后续两渠道共用入口。每轮创建相应工具集合的 SDK Agent，`output_type=AgentAnswer`，调用原生 `Runner.run`；会话从可信身份定位，不能由用户请求任意指定他人的 session。模型接入凭据、客户端与 Profile 配置均不进入 RunContext。应用将 `data_policy_id` 装配到模型输入/结果与 Session 投影策略中，发送前按实际接收方检查数据权限。

应用提供方法 `Application.scope_for_turn(mode: Literal["query", "diagnose"], authorized_tools: frozenset[str], available_tools: frozenset[str]) -> frozenset[str]`，与构造时装配的可信用途允许表取交集；入口每条消息重新调用，模型不能调用它授权自己。本片测试以可信合成入口构造 context，P1-B 实现按钮/指令解析。关联编号复用应用生成的 `turn_id`，阶段日志通过标准库 logging 输出白名单字段。

- [ ] 编写 `test_local_and_mcp_followup_through_real_runner`、`test_concurrent_sessions_are_isolated`、`test_same_session_reentry_is_rejected`、`test_invalid_answer_is_never_delivered_or_committed`、`test_timeout_does_not_replay_tools`。断言两轮工具结果可合法引用；不同渠道得到不同获准投影；一轮权限变化不能改写另一轮 Agent tools；超限/取消无成功 Delivery，未经校验内容既不提交 Session 也不发送。
- [ ] 编写 `test_query_permission_is_not_inherited`、`test_commit_failure_does_not_replay_tools`、`test_stage_logs_contain_only_safe_metadata`。前一轮查询、下一轮默认诊断时，查询工具不出现且强行调用零执行；实际 PostgreSQL 提交边界注入保存失败后无成功交付，不自动补跑工具，必要时隔离会话；阶段日志共用请求编号且不含合成 SQL、结果、假凭据和原始异常。
- [ ] 运行 `uv run --locked --extra dev python -m pytest tests/sdk_core/test_app.py -q`；预期新应用入口缺失失败。
- [ ] 实现上述 `run_turn`：存储就绪检查、总期限、有限并发、同会话互斥、每轮权限表与受限阶段日志；校验 SDK 最终结构与 Evidence 后才提交 Session，交付前再复核权限和渠道。校验不通过返回受控失败，不把模型完整回答当报错输出。本片不发送外部渠道消息，不提供未校验结论流。P1-B 另验证 Session 已提交但最终结果保存失败的双存储边界，以及渠道投递状态。
- [ ] 运行 `uv run --locked --extra dev python -m pytest tests/sdk_core -q`、`uv run --locked --extra dev ruff check src/xiaowei tests/sdk_core`、`uv run --locked --extra dev mypy src/xiaowei`；预期全部通过。CI 增加隔离 PostgreSQL 服务与明确的新包检查；数据库缺失/不可用不能 skip 成全绿。旧检查标识为历史检查，不用旧通过率代替新能力验证。
- [ ] 在已获准模型环境使用合成数据，按 OpenAI/Gemini/DeepSeek 的每个选定 Profile 分别验证本地和 MCP 工具调用、工具结果回传、真实 `AgentAnswer` 类型/Evidence 校验与下一轮 Session 追问；至少先完成一个 Profile。记录 SDK、端点标识、协议、模型/模式、用量和结果，其他组合如实标为未验证/不兼容；不因一家通过宣称全部支持，不用 scripted Model 或 HTTP mock 代替真实验证。更新 README 的核心开发验证命令、handoff 的精确 SHA/证据/缺口；保持正式 Web/飞书启动说明未交付的事实。
- [ ] 仅暂存本任务文件，提交 `feat: compose verified SDK application core`。对整个 P1-A 分支做一次独立审查，修复阻塞项后细化 P1-B；不自动合并、部署或归档。

## 完成定义与覆盖边界

本片离线完成要求：真 Runner、SDK 模型 API 的 HTTP 契约、两条治理工具路径、真实 loopback MCP 协议、SQLAlchemySession + 真实 PostgreSQL 读写/回放、用途范围与代码生成事实均有对应测试；存储初始化/版本不符、历史超限/过期和保存失败路径通过；新包独立可安装且默认无 trace 外发。真实模型验证按 Profile 单列状态，缺失时仍为待验证；至少一个 Profile 通过实际工具闭环后才有真实模型运行证据。

本片产出是运行核心，不是首版可用产品。真实 StarRocks 的读取/SQL 限制与业务口径、Web 和飞书的用途选择/新建会话、渠道去重/发送失败/重发、正式历史读取、清理命令与健康端点属于 P1-B；EXPLAIN 诊断属于 P2；两容器部署、SSH 访问、备份恢复与用户接受属于 P3。不得把本片测试通过写成这些能力已经完成。
