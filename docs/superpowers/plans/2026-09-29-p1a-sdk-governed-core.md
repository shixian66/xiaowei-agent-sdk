# P1-A SDK 与治理执行核心 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付可独立安装与测试的运行核心：一个真实 SDK Runner 能调用受治理的本地合成工具和 MCP 工具，完成有权限边界的连续对话与证据校验。

**Architecture:** 新应用位于 `src/xiaowei/`，直接使用 SDK Agent/Runner/Session。小维在工具实际 I/O 前执行治理，在工具返回、Session 读写和最终回答出口分别过滤及校验证据；不另造运行引擎。MCP 首条接入使用 SDK 的 Streamable HTTP，空配置不连接任何服务。

**Tech Stack:** Python 3.11、OpenAI Agents SDK、Pydantic、SDK SQLiteSession、标准库 sqlite3、uv、pytest/pytest-asyncio/pytest-socket、Ruff、mypy；SDK 与传递依赖在 Task 1 实装核对后锁定。

**Spec:** [架构设计](../../../ARCHITECTURE.md)、[交付路线](../../../DEVELOPMENT_PLAN.md)。本文为待审阅的实施计划，复选框不代表已执行。

## Global Constraints

- **OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**
- 单进程、单 Agent；不引入 Worker、计划编译器、多 Agent、业务 MCP Server 或通用审批平台。
- RunContext 只放可信身份、Target Scope、Tool Scope、预算及 Evidence 标识/元数据；不放客户端、服务或凭据引用。
- SDK Session 为架构接口，SQLiteSession 为首版实现；不复制 SDK 会话机制。
- 本地与 MCP 工具都必须在实际 I/O 前复核最新权限与预算；动态工具过滤不是最终授权。
- 原始、模型、会话和渠道数据分别约束；默认关闭 tracing 与 trace 外发。
- 本片只用合成数据和临时本地 MCP fixture；不开放通用 SQL、不接生产系统、不构建 Web/飞书入口。StarRocks SQLGuard/Adapter、两端真实收发、请求去重/重发及正式主入口在 P1-B 完成，P2/P3 仍保留全部验收要求。
- 旧 `src/xiaowei_agent/`、旧 CLI 和相关依赖本片保留为过渡历史；新包不得导入旧 Runtime。P1-B 明确删除范围并切换唯一正式入口。
- 测试使用本仓库独立 `.venv`；禁止以原项目环境证明新产品通过。离线测试禁止外网，仅 MCP fixture 测试显式允许 loopback。

## Review Focus

1. 工具展示之后发生撤权：实际执行前拒绝，recording adapter / MCP fixture 收到零次工具执行请求（Task 2、4）。
2. SDK 自动写入工具结果和最终回答：禁止内容不能先落盘后擦除，回放还需复核当前权限，工具调用配对不能损坏（Task 3、5）。
3. MCP 名字冲突、schema 漂移与伪造 readonly/evidence 声明：拒绝未登记契约，远端标识不能成为可信本地证据（Task 4）。
4. 两个用户并发与工具并行：工具表、连接认证、Evidence、预算和 Session 不串用；同会话明确拒绝重入（Task 2、5）。
5. MCP 超时、畸形/过大内容与 SDK 默认 trace：受限失败，不泄露原始错误/数据、不绕过过滤、不自动重试未知执行结果（Task 1、4、5）。

## 文件与接口约定

不生成空目录或无消费者的接口。按任务增加以下少量模块；文件过大时才按职责拆分。

| 文件 | 职责 |
| --- | --- |
| `src/xiaowei/config.py` | 有限配置、静态 MCP 登记、默认 tracing 关闭 |
| `src/xiaowei/models.py` | 可信 context、工具结果、证据、结构化回答与渠道输出类型 |
| `src/xiaowei/governance.py` | 调用前权限/参数/预算检查，共享结果入口 |
| `src/xiaowei/evidence.py` | 证据记录、四种数据投影与最终回答校验 |
| `src/xiaowei/session.py` | SDK Session 薄策略包装与受限回放 |
| `src/xiaowei/mcp.py` | SDK MCP 公共接口的治理装配和生命周期 |
| `src/xiaowei/app.py` | 单轮应用函数、每轮 Agent 装配与有限并发 |
| `tests/sdk_core/` | 真 Runner + scripted Model、recording I/O、loopback MCP fixture |

下列应用类型在 Task 2 定义，后续任务直接复用；SDK 类型直接导入。类型字段为此片的最小契约：

- `Channel = Literal["web", "feishu"]`；`Identity(subject_id: str, session_id: str, turn_id: str, channel: Channel)` 由可信入口构造。
- `Budget(max_turns: int, max_tool_calls: int, timeout_seconds: float)` 为正数上限；工具次数计数器按可信 subject/session/turn 键在治理对象内部隔离，并在轮次结束时清理，不是模型可改字段。
- `RunContext(identity: Identity, target_scope: frozenset[str], tool_scope: frozenset[str], budget: Budget, evidence_ids: tuple[str, ...])`；无任意对象扩展字段。
- `ToolContract(tool_id: str, target_id: str, input_schema: dict[str, object], policy_id: str)` 为启动时获准的契约；`tool_id` 使用 `local/name` 或 `server_id/name`。参数与结果策略由 `policy_id` 对应的明确 Python 函数提供，不设计策略 DSL。
- `ToolRequest(tool_id: str, target_id: str, arguments: dict[str, object])`；`ToolObservation(payload: dict[str, object], captured_at: datetime, truncated: bool)` 只容纳已限制读取大小的临时结果，不自动序列化给模型。
- `EvidenceRecord` 包含代码生成的 `evidence_id`、可信身份/会话、target/tool/call 标识、采集与过期时间、截断状态及各用途获准内容；持久化不保留原始结果。
- `ToolResult(evidence_id: str, model_content: str, truncated: bool)` 仅为模型可见内容。`AnswerClaim(text: str, evidence_ids: tuple[str, ...], kind: Literal["observation", "inference"])` 和 `AgentAnswer(claims: list[AnswerClaim], clarification: str | None)` 作为 `output_type`；查询事实与推断均须有效来源，不允许用 clarification 通道展示查询结果。
- `Delivery(content: str, evidence_ids: tuple[str, ...], channel: Channel)` 是通过验证的渠道输出；同一回答也要按接收渠道重新生成。

## Task 1：锁定 SDK 并验证公开扩展点

**Files:** Modify `pyproject.toml`、`uv.lock`；Create `src/xiaowei/__init__.py`、`src/xiaowei/config.py`、`tests/sdk_core/conftest.py`、`tests/sdk_core/test_sdk_contract.py`。

**Interfaces:** 消费官方 `Agent`、`Runner`、`Model`、`Session`、`SQLiteSession` 与 MCP 公共类型；产出 `configure_runtime() -> None`，负责显式关闭默认 tracing/export，以及可安装的新包。具体 SDK 方法签名以锁定版本为准，在本任务测试中固定。

- [ ] 编写 `test_real_runner_calls_tool_and_returns_typed_answer`、`test_sqlite_session_public_roundtrip`、`test_public_mcp_interception_contract`、`test_default_tracing_has_no_export`。使用官方 Model 接口实现测试用 scripted Model；核心断言分别为 `tool_calls == 1`、类型化回答正确、Session 调用/结果配对可继续使用、在公开 MCP 发送与返回边界能拦截、`trace_exports == []`。
- [ ] 运行 `uv run python -m pytest tests/sdk_core/test_sdk_contract.py -q`；记录新依赖/模块缺失或能力不满足的失败，不把安装故障当行为测试通过。
- [ ] 安装核对当前 Python 兼容版本，将 SDK 和传递依赖锁入 `uv.lock`；将 `src/xiaowei` 加入打包但不把旧 CLI 宣称为新产品。实现运行配置，验证 SDK Session、MCP 调用/结果拦截、HTTP transport 的超时与接收限额扩展点。只使用公开 API；若不能实现必要边界，记录具体 API 限制并调整本计划，不绕过治理启用该能力。
- [ ] 运行 `uv sync --locked` 和 `uv run --locked python -m pytest tests/sdk_core/test_sdk_contract.py -q`；预期全部 PASS。测试不得访问真实模型或读取既有凭据；新包 import 不加载旧 `xiaowei_agent`。
- [ ] 仅暂存本任务文件，提交 `build: establish verified Agents SDK runtime`；记录锁定版本及 Python 版本。

## Task 2：本地受治理工具、数据投影与 Evidence

**Files:** Create `src/xiaowei/models.py`、`src/xiaowei/governance.py`、`src/xiaowei/evidence.py`、`tests/sdk_core/test_governance.py`、`tests/sdk_core/test_evidence.py`。

**Interfaces:** 定义上文类型；`GovernedTools.allowed_contracts(ctx: RunContext) -> list[ToolContract]`、`async GovernedTools.invoke(ctx: RunContext, request: ToolRequest, execute: Callable[[], Awaitable[ToolObservation]]) -> ToolResult`。依赖通过构造器装配，`execute` 只能由应用绑定，模型不能提供。`EvidenceStore.record(ctx: RunContext, request: ToolRequest, observation: ToolObservation) -> ToolResult`；`EvidenceStore.project(evidence_id: str, ctx: RunContext, audience: Literal["model", "session", "web", "feishu"]) -> str`；`EvidenceStore.validate_answer(answer: AgentAnswer, ctx: RunContext) -> Delivery`。存储首版为独立 SQLite 表，当前授权查询由应用提供的回调执行。

- [ ] 编写 `test_revoked_tool_never_reaches_io`、`test_parallel_calls_share_one_budget`、`test_context_rejects_dependencies`、`test_four_data_boundaries`、`test_forged_foreign_expired_evidence_is_denied`、`test_query_claim_without_evidence_is_denied`。关键断言：拒绝时 `recorded_calls == []`；上限 1 时两个并行请求总执行数为 1；原始合成字段 `private_note` 不出现在模型/Session/渠道；为四种投影设置不同字段/字节上限并分别满足；撤权后不能读取已保存内容。
- [ ] 运行 `uv run --locked python -m pytest tests/sdk_core/test_governance.py tests/sdk_core/test_evidence.py -q`；预期因缺少新类型/行为失败。
- [ ] 实现上述类型与最少服务方法。允许集合来自可信配置，实际调用时复核目标、契约、参数与最新权限，原子预占工具预算。用一个仅测试存在的 `read_sample` function tool/recording adapter 验证；不包装成伪 StarRocks。代码生成证据 ID，按用途投影后才写入 Evidence；未知投影规则拒绝。错误转成有限、安全的工具失败，不携带 raw payload。
- [ ] 重跑本任务命令；预期全部 PASS。确认不同用户/会话/渠道不能读取彼此证据，截断和空结果都有明确语义，持久化文件不含合成禁止字段。
- [ ] 仅暂存本任务文件，提交 `feat: govern tool execution and evidence boundaries`。

## Task 3：原生 Session 的存储前与回放前策略

**Files:** Create `src/xiaowei/session.py`、`tests/sdk_core/test_session_policy.py`。

**Interfaces:** 消费 `RunContext`、`EvidenceStore.project` 与 SDK `Session`；产出 `PolicySession(inner: Session, context: RunContext, evidence: EvidenceStore)`，实现锁定版 SDK 所需的公开 Session 接口并委托底层。提供 `async discard_pending() -> None`、`async commit_validated() -> None`；后者只由验证成功的应用路径调用。

- [ ] 编写 `test_runner_session_filters_before_write`、`test_replay_rechecks_permissions`、`test_valid_tool_pair_survives_followup`、`test_invalid_final_never_persists`、`test_partial_store_failure_invalidates_session`。用临时真实 SQLiteSession 和真 Runner 断言：`forbidden_text not in stored_items`，撤权数据不在第二轮 Model 输入，SDK 能消费过滤后成对工具项，未经验证最终输出不会落盘。
- [ ] 运行 `uv run --locked python -m pytest tests/sdk_core/test_session_policy.py -q`；预期新包装/行为缺失失败。
- [ ] 实现 `PolicySession`：在 SDK 写入请求时先按 Session 策略处理并暂存本轮项，最终校验后才委托写入；回放前重新检验证据和权限。敏感用户输入也受字段/字节策略约束；不能只过滤工具结果。保持 SDK item 类型与 call/result 配对，若无安全回放形式则拒绝继续该历史。失败丢弃待写项，底层部分写失败时隔离该会话，不能自动重跑或将其视作完整历史。
- [ ] 重跑本任务命令；预期全部 PASS。验证 clear/pop 等 SDK 公共契约与待写项一致；持久化和 replay 不是同一份未经处理的模型记录。
- [ ] 仅暂存本任务文件，提交 `feat: enforce Session write and replay policy`。

## Task 4：最小 MCP Client Integration

**Files:** Modify `src/xiaowei/config.py`；Create `src/xiaowei/mcp.py`、`tests/sdk_core/mcp_fixture.py`、`tests/sdk_core/test_mcp_integration.py`。

**Interfaces:** `MCPServerConfig(server_id: str, url: str, auth_ref: str | None, timeout_seconds: float, allowed_tools: dict[str, str])` 为静态可信登记，`allowed_tools` 将远端工具名映射到已有 `policy_id`，对应策略代码固定预期输入/输出 schema 与参数含义；`MCPIntegration(configs: tuple[MCPServerConfig, ...], governance: GovernedTools)` 支持异步生命周期；`async tools_for(ctx: RunContext) -> list[Tool]` 返回 SDK 原生可用工具。具体绑定采用 Task 1 已验证的公共 SDK 扩展点，所有远程调用走 SDK；若以薄 function tool 暴露，则只转交，不自写协议或模型调用循环。

- [ ] 编写 `test_empty_config_performs_no_network`、`test_sdk_runner_calls_governed_mcp`、`test_revoked_mcp_tool_has_zero_calls`、`test_unknown_schema_and_collision_fail_closed`、`test_auth_timeout_and_shutdown`、`test_mcp_payload_filtered_before_model`。临时 fixture 仅绑定 loopback；断言未获准或撤权请求 `fixture.tool_calls == []`，未知/写工具不在 Model tools，文本和 structuredContent 的禁止字段、不合约内容及伪造的可信 evidence_id 不进入 Model/Session，注入文本不能改变执行权限/预算，关闭后无遗留任务/连接。
- [ ] 运行 `uv run --locked python -m pytest tests/sdk_core/test_mcp_integration.py -q`；仅本测试模块允许必要 loopback，不修改全套测试为允许外网。预期新集成缺失失败。
- [ ] 实现一个 Streamable HTTP 接入路径，认证首片只支持无认证的测试 fixture 和可信配置引用的 Bearer 认证；认证引用在 app 装配解析，值不进入 RunContext。远端须 HTTPS，HTTP 只允许 loopback 测试；重定向不得将认证转交其他端点。端点不能来自用户/模型参数。发现工具后核对登记 schema 与参数/结果策略，名字按 server/tool 唯一映射，缓存不能跨授权范围共用。
- [ ] 接通 `GovernedTools.invoke` 和 Evidence 结果入口；检查 MCP 文本、结构化内容、错误与资源引用。首片只支持明确登记的文本/JSON 结果契约；不支持内容类型拒绝，不自动读取资源链接。用 Task 1 核对过的 transport 公共扩展点设置接收上限，解析后再限制内容大小，禁止只在全部读取后切片冒充读取上限。超时/未知执行结果不自动重试；单个 Server 不可用仅隐藏其工具，本地工具继续可用。
- [ ] 重跑本任务命令；预期全部 PASS。追加协议断言覆盖认证失败、超时、超大响应、取消与关闭；测试用假凭据不得出现在 Model、SQLite、日志或错误中。远端 readonly 标注不授予权限，远端 Evidence 仅作来源属性，小维另建可信绑定。
- [ ] 仅暂存本任务文件，提交 `feat: add governed SDK MCP client integration`。

## Task 5：组合可验证的运行核心并交接 P1-B

**Files:** Create `src/xiaowei/app.py`、`tests/sdk_core/test_app.py`；Modify `README.md`、`AGENT_HANDOFF.md`、`.github/workflows/ci.yml`。

**Interfaces:** `Application` 构造时接收可信配置、SDK Model、治理/Evidence 与 Session 依赖；`async Application.run_turn(ctx: RunContext, message: str) -> Delivery` 是后续两渠道共用入口。每轮创建相应工具集合的 SDK Agent，`output_type=AgentAnswer`，调用原生 `Runner.run`；会话从可信身份定位，不能由用户请求任意指定他人的 session。

- [ ] 编写 `test_local_and_mcp_followup_through_real_runner`、`test_concurrent_sessions_are_isolated`、`test_same_session_reentry_is_rejected`、`test_invalid_answer_is_never_delivered_or_committed`、`test_timeout_does_not_replay_tools`。断言两轮工具结果可合法引用；不同渠道得到不同获准投影；一轮权限变化不能改写另一轮 Agent tools；超限/取消无成功 Delivery，未经校验内容既不提交 Session 也不发送。
- [ ] 运行 `uv run --locked python -m pytest tests/sdk_core/test_app.py -q`；预期新应用入口缺失失败。
- [ ] 实现上述 `run_turn`：总期限、有限并发、同会话互斥、每轮权限表；校验 SDK 最终结构与 Evidence 后才提交 Session，交付前再复核权限和渠道。校验不通过返回受控失败，不把模型完整回答当报错输出。本片不发送外部渠道消息，不提供未校验结论流。
- [ ] 运行 `uv run --locked python -m pytest tests/sdk_core -q`、`uv run --locked ruff check src/xiaowei tests/sdk_core`、`uv run --locked mypy src/xiaowei`；预期全部通过。CI 增加明确的新包检查，旧检查标识为历史检查，不用旧通过率代替新能力验证。
- [ ] 在已获准模型环境使用合成数据试跑一条本地和一条 MCP 调用，记录 SDK/模型、用量与结果；没有环境则明确留待验证，不能将 scripted Model 记作真实模型通过。更新 README 的核心开发验证命令、handoff 的精确 SHA/证据/缺口；保持正式 Web/飞书启动说明未交付的事实。
- [ ] 仅暂存本任务文件，提交 `feat: compose verified SDK application core`。对整个 P1-A 分支做一次独立审查，修复阻塞项后细化 P1-B；不自动合并、部署或归档。

## 完成定义与覆盖边界

本片离线完成要求：真 Runner、两条治理工具路径、真实 loopback MCP 协议、SQLiteSession 读写/回放与证据校验均有对应测试；新包独立可安装且默认无 trace 外发。真实模型验证单列状态，缺失时仍为待验证。

本片产出是运行核心，不是首版可用产品。真实 StarRocks 的读取/SQL 限制、Web 和飞书的真实用户路径、渠道去重/发送失败/重发以及正式历史读取入口属于 P1-B；EXPLAIN 诊断属于 P2；部署与用户接受属于 P3。不得把本片测试通过写成这些能力已经完成。
